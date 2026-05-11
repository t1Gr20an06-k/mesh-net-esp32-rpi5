"""
Dispatcher: один декодированный пакет → решение.

  1. Дедупликация (DedupCache).
  2. Запись в БД по типу пакета (PING / SOS / CHAT).
  3. Подготовка ретрансляции (TTL−1) и постановка в TxQueue.
  4. **ACK-протокол v2**:
     - если пришёл CHAT/SOS с want_ack=true И мы — конечный получатель
       (база, не ретранслятор), шлём ACK обратно. Шлём даже на дубль —
       иначе при потере первого ACK ESP32 будет ретраить до фейла.
     - если пришёл is_ack=true, парсим payload и обновляем
       outgoing_chat.delivery_status (для CHAT база→турист).

Принципы:
  * SOS никогда не дропается на уровне БД и очереди.
  * Эхо собственных пакетов (device_id == NODE_DEVICE_ID) игнорируется.
  * ACK сам в БД не пишется, только освобождает pending у выводящей стороны.
  * Ретрансляция ACK тоже работает (через make_forward) — без этого ACK не
    дойдёт до туриста через инфо-точку.
"""

import logging
from typing import Optional

from .db import Database
from .mesh import DedupCache, TxQueue, make_forward
from .packet import (
    Channel,
    MeshPacket,
    PacketType,
    make_ack_payload,
    parse_ack_payload,
)

log = logging.getLogger(__name__)


class Dispatcher:
    def __init__(
        self,
        db: Database,
        dedup: DedupCache,
        tx: TxQueue,
        node_device_id: int,
        is_base: bool = True,
    ):
        self.db = db
        self.dedup = dedup
        self.tx = tx
        self.node_device_id = node_device_id
        # is_base=True — мы конечный получатель CHAT/SOS, шлём ACK.
        # is_base=False — мы инфо-точка, только ретранслируем (ACK шлёт база).
        self.is_base = is_base
        # Монотонный счётчик исходящих packet_id (для ACK от базы).
        # wraparound каждые 65536; на скорости «1 ACK на каждый CHAT/SOS»
        # это сотни часов работы. Хранится только в RAM — после рестарта
        # начинаем с нуля, что не страшно: dedup на ESP32 не привязан к id.
        self._packet_id_counter = 0

    def _next_packet_id(self) -> int:
        self._packet_id_counter = (self._packet_id_counter + 1) & 0xFFFF
        return self._packet_id_counter

    def _send_ack(self, original: MeshPacket) -> None:
        """Сформировать и поставить в очередь ACK на пакет original.
        device_id ACK = наш NODE_DEVICE_ID, payload — кому и какой pkt подтверждаем."""
        ack = MeshPacket(
            type=PacketType.ACK,
            device_id=self.node_device_id,
            packet_id=self._next_packet_id(),
            channel=Channel.RESCUE if self.is_base else original.channel,
            ttl=3,
            # Координаты в ACK не несут смысла, оставляем 0 — на стороне
            # ESP32 поле lat/lon у ACK не парсится.
            latitude=0,
            longitude=0,
            payload=make_ack_payload(original.device_id, original.packet_id),
            want_ack=False,
            is_ack=True,
        )
        if not self.tx.push(ack):
            log.warning("TX-очередь переполнена при отправке ACK для dev=%d pkt=%d",
                        original.device_id, original.packet_id)
        else:
            log.info("→ ACK для dev=%d pkt=%d", original.device_id, original.packet_id)

    def handle(self, pkt: MeshPacket, receiver_rssi: Optional[int] = None) -> None:
        # Эхо собственных пакетов — игнорируем.
        if pkt.device_id == self.node_device_id:
            return

        # --- ACK-приём ---
        # ACK сам в БД не пишется. Если адресован нам — снимаем pending.
        if pkt.is_ack and pkt.type == PacketType.ACK:
            try:
                ack_for_dev, ack_for_pid = parse_ack_payload(pkt.payload)
            except Exception as exc:  # noqa: BLE001
                log.warning("ACK не распарсился: %s", exc)
                return
            if ack_for_dev == self.node_device_id:
                marked = self.db.mark_outgoing_chat_acked(ack_for_pid)
                if marked:
                    log.info("✓ ACK получен на наш pkt=%d от dev=%d",
                             ack_for_pid, pkt.device_id)
                else:
                    # ACK на сообщение, которого мы уже не отслеживаем (поздно
                    # или дубль) — норма, не ошибка.
                    log.debug("ACK на неизвестный pkt=%d от dev=%d (поздно или дубль)",
                              ack_for_pid, pkt.device_id)
            else:
                log.debug("ACK не нам (для dev=%d), ретранслируем", ack_for_dev)
            # ACK ретранслируем, чтобы он долетел через инфо-точку.
            fwd = make_forward(pkt)
            if fwd is not None:
                self.tx.push(fwd)
            return

        # --- Дедуп для остальных типов ---
        is_dup = self.dedup.seen(pkt)
        if is_dup:
            log.debug("dup dev=%d type=%s ttl=%d pkt=%d — пропускаем в БД/ретрансляции",
                      pkt.device_id, pkt.type.name, pkt.ttl, pkt.packet_id)
            # На дубль ACK ВСЁ РАВНО ШЛЁМ — это значит первый ACK не дошёл,
            # ESP32 ретраит. Без повторного ACK retry будут продолжаться
            # до MAX_RETRIES и сообщение «не дойдёт» хотя в БД оно уже есть.
            if self.is_base and pkt.want_ack and pkt.type in (PacketType.CHAT, PacketType.SOS):
                self._send_ack(pkt)
            return

        # --- Запись в БД ---
        try:
            if pkt.type == PacketType.PING:
                self.db.insert_ping(pkt, receiver_rssi=receiver_rssi)
            elif pkt.type == PacketType.SOS:
                self.db.insert_sos(pkt, receiver_rssi=receiver_rssi)
                log.warning("SOS dev=%d lat=%d lon=%d ttl=%d pkt=%d",
                            pkt.device_id, pkt.latitude, pkt.longitude,
                            pkt.ttl, pkt.packet_id)
            elif pkt.type == PacketType.CHAT:
                self.db.insert_chat(pkt)
        except Exception as exc:  # noqa: BLE001
            log.error("Ошибка записи в БД: %s (dev=%d type=%s)",
                      exc, pkt.device_id, pkt.type.name)
            # Не выходим — ACK и ретрансляция важнее БД.

        # --- ACK на новый CHAT/SOS с want_ack ---
        if self.is_base and pkt.want_ack and pkt.type in (PacketType.CHAT, PacketType.SOS):
            self._send_ack(pkt)

        # --- Ретрансляция ---
        fwd = make_forward(pkt)
        if fwd is None:
            return
        ok = self.tx.push(fwd)
        if not ok:
            log.warning("TX-очередь переполнена, дропнули %s dev=%d",
                        pkt.type.name, pkt.device_id)
