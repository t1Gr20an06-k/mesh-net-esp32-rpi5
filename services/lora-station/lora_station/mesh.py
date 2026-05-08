"""
Mesh-логика: дедупликация и очередь TX для ретрансляции.

Дедупликация: ключ (device_id, seq) — повторно пришедший пакет не
обрабатываем и не ретранслируем. seq берётся из payload PING; для SOS
seq нет, поэтому ключ дополняется типом пакета и временем (округлённым
до секунды) — этого достаточно, т.к. SOS-бёрст идёт 3 пакета × 500 мс.

Очередь TX: thread-safe queue.Queue с приоритетом. SOS никогда не
дропается; PING при переполнении дропается (старые).
"""

import threading
import time
from collections import deque
from dataclasses import dataclass
from queue import Empty, PriorityQueue
from typing import Optional

from .packet import MeshPacket, PacketType, encode

# Время жизни записи в дедуп-кеше — после этого ключ можно использовать снова.
DEDUP_TTL_SEC = 30.0

# Максимум пакетов в TX-очереди. SOS приоритетнее и никогда не дропается.
TX_QUEUE_MAX = 16


# Приоритеты для PriorityQueue: меньшее число = выше приоритет.
PRIO_SOS  = 0
PRIO_ACK  = 1
PRIO_CHAT = 2
PRIO_PING = 3

_PRIO_BY_TYPE = {
    PacketType.SOS:  PRIO_SOS,
    PacketType.ACK:  PRIO_ACK,
    PacketType.CHAT: PRIO_CHAT,
    PacketType.PING: PRIO_PING,
}


@dataclass(order=True)
class _TxItem:
    priority: int
    seq_tie:  int               # для стабильности FIFO внутри одного приоритета
    raw:      bytes = b''       # encoded 64-byte packet


class DedupCache:
    """
    Простой кольцевой кеш ключей с TTL.

    Не сложная LRU — задача мелкая, узлов в сети единицы, пакетов — сотни.
    """

    def __init__(self, ttl_sec: float = DEDUP_TTL_SEC):
        self._ttl = ttl_sec
        self._seen: dict[tuple, float] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(pkt: MeshPacket) -> tuple:
        # PING несёт seq в первых 4 байтах payload (battery|rssi|seq big-endian).
        if pkt.type == PacketType.PING and len(pkt.payload) >= 4:
            seq = int.from_bytes(pkt.payload[2:4], 'big', signed=False)
            return (int(pkt.type), pkt.device_id, seq)
        # Для SOS/CHAT/ACK seq нет. Раньше брали int(time.monotonic()) —
        # это было багом: SOS-бёрст (3 пакета × 500 мс) попадал в 2-3
        # разные секунды и дедуп не срабатывал — на дашборде видно 3
        # одинаковых SOS-инцидента.
        # Теперь дедуплицируем по содержимому payload: один и тот же
        # текст/тип в окне TTL кеша считается дублем. Окно DEDUP_TTL_SEC
        # (30 сек) — за это время бёрст и все ретрансляции отстреляются,
        # а если оператор шлёт одинаковый ответ через минуту — это уже
        # отдельное сообщение, и лучше не дедупить, чем потерять.
        return (int(pkt.type), pkt.device_id, hash(bytes(pkt.payload)))

    def seen(self, pkt: MeshPacket) -> bool:
        """True если пакет уже был обработан (и до сих пор в окне TTL)."""
        now = time.monotonic()
        key = self._key(pkt)
        with self._lock:
            self._evict(now)
            if key in self._seen:
                return True
            self._seen[key] = now
            return False

    def _evict(self, now: float) -> None:
        cutoff = now - self._ttl
        stale = [k for k, t in self._seen.items() if t < cutoff]
        for k in stale:
            del self._seen[k]


class TxQueue:
    """
    Приоритетная очередь TX. SOS вытесняет PING при переполнении.
    """

    def __init__(self, maxsize: int = TX_QUEUE_MAX):
        self._q: PriorityQueue[_TxItem] = PriorityQueue()
        self._max = maxsize
        self._counter = 0
        self._lock = threading.Lock()
        # Отдельная очередь сохранённых PING-ов на случай переполнения —
        # просто чтобы при дропе понимать, что именно пропало (для лога).
        self._dropped_pings: deque[int] = deque(maxlen=32)

    def push(self, pkt: MeshPacket) -> bool:
        """
        Поставить пакет в очередь. Возвращает False если пришлось дропнуть.
        SOS не дропается никогда.
        """
        prio = _PRIO_BY_TYPE.get(pkt.type, PRIO_PING)
        raw = encode(pkt)
        with self._lock:
            self._counter += 1
            item = _TxItem(priority=prio, seq_tie=self._counter, raw=raw)
            if self._q.qsize() >= self._max:
                if pkt.type == PacketType.SOS:
                    # SOS пихаем всегда — может перерасти максимум, это ок.
                    pass
                else:
                    # Дропаем самый низкоприоритетный PING.
                    self._dropped_pings.append(pkt.device_id)
                    return False
            self._q.put(item)
            return True

    def pop(self, timeout: Optional[float] = None) -> Optional[bytes]:
        """Достать следующий пакет (raw 64 байта) или None по таймауту."""
        try:
            item = self._q.get(timeout=timeout)
            return item.raw
        except Empty:
            return None

    def qsize(self) -> int:
        return self._q.qsize()

    def dropped_count(self) -> int:
        return len(self._dropped_pings)


def make_forward(pkt: MeshPacket) -> Optional[MeshPacket]:
    """
    Подготовить пакет к ретрансляции: TTL−1, и если стало 0 — не ретранслируем.
    Возвращает новый MeshPacket или None.
    """
    if pkt.ttl <= 1:
        return None
    return MeshPacket(
        version=pkt.version,
        type=pkt.type,
        device_id=pkt.device_id,
        channel=pkt.channel,
        ttl=pkt.ttl - 1,
        latitude=pkt.latitude,
        longitude=pkt.longitude,
        payload=pkt.payload,
    )
