"""
Dispatcher: один декодированный пакет → решение.

  1. Дедупликация (DedupCache).
  2. Запись в БД по типу пакета (PING / SOS / CHAT / ACK).
  3. Подготовка ретрансляции (TTL−1) и постановка в TxQueue.

Принципы:
  * SOS никогда не дропается на уровне БД и очереди.
  * ACK не пишется в таблицы; только ретранслируем (логика подтверждений
    SOS на стороне rescue-api / dashboard будет в этапе 4).
  * Если device_id == NODE_DEVICE_ID — это эхо нашего же пакета, дропаем.
"""

import logging
from typing import Optional

from .db import Database
from .mesh import DedupCache, TxQueue, make_forward
from .packet import MeshPacket, PacketType

log = logging.getLogger(__name__)


class Dispatcher:
    def __init__(
        self,
        db: Database,
        dedup: DedupCache,
        tx: TxQueue,
        node_device_id: int,
    ):
        self.db = db
        self.dedup = dedup
        self.tx = tx
        self.node_device_id = node_device_id

    def handle(self, pkt: MeshPacket, receiver_rssi: Optional[int] = None) -> None:
        # Эхо собственных пакетов — игнорируем.
        if pkt.device_id == self.node_device_id:
            return

        # Дедуп.
        if self.dedup.seen(pkt):
            log.debug("dup dev=%d type=%s ttl=%d — пропускаем",
                      pkt.device_id, pkt.type.name, pkt.ttl)
            return

        # БД.
        try:
            if pkt.type == PacketType.PING:
                self.db.insert_ping(pkt, receiver_rssi=receiver_rssi)
            elif pkt.type == PacketType.SOS:
                self.db.insert_sos(pkt, receiver_rssi=receiver_rssi)
                log.warning("SOS dev=%d lat=%d lon=%d ttl=%d",
                            pkt.device_id, pkt.latitude, pkt.longitude, pkt.ttl)
            elif pkt.type == PacketType.CHAT:
                self.db.insert_chat(pkt)
            elif pkt.type == PacketType.ACK:
                # ACK обрабатывает rescue-api (этап 4); сейчас просто логируем.
                log.info("ACK dev=%d ttl=%d", pkt.device_id, pkt.ttl)
        except Exception as exc:  # noqa: BLE001
            log.error("Ошибка записи в БД: %s (dev=%d type=%s)",
                      exc, pkt.device_id, pkt.type.name)
            # Не выходим — ретрансляция важнее БД.

        # Ретрансляция.
        fwd = make_forward(pkt)
        if fwd is None:
            return
        ok = self.tx.push(fwd)
        if not ok:
            log.warning("TX-очередь переполнена, дропнули %s dev=%d",
                        pkt.type.name, pkt.device_id)
