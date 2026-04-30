"""
SQLite-обёртка для lora-station.

Схема — scripts/db_init/init.sql (4 таблицы: devices, pings, sos_events, chat_messages).
Координаты хранятся как int32 × 1e6, ровно как в пакете — без конвертации.

Никаких ORM. Чистый sqlite3 + параметризованные запросы.
"""

import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Optional

from .packet import MeshPacket, parse_ping_payload, parse_sos_payload, parse_chat_payload

DEFAULT_DB_PATH = "/var/lib/mesh-net/mesh.db"


class Database:
    """
    Тонкая обёртка над sqlite3.

    Один экземпляр на процесс, потокобезопасен через RLock — sqlite3 в Python
    позволяет одно соединение из разных потоков, но запросы должны быть
    сериализованы. lora-station пишет из главного цикла + потенциально из
    callback'а IRQ, поэтому Lock обязателен.
    """

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        if not os.path.exists(db_path):
            raise FileNotFoundError(
                f"БД не найдена: {db_path}. Запусти scripts/db_init/init.sh"
            )
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            db_path,
            check_same_thread=False,
            timeout=5.0,
        )
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _cursor(self):
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            finally:
                cur.close()

    # ------------------------------------------------------------------
    # devices: upsert при каждом принятом пакете
    # ------------------------------------------------------------------
    def upsert_device(
        self,
        device_id: int,
        channel: int,
        latitude: int,
        longitude: int,
        battery_pct: Optional[int] = None,
    ) -> None:
        """
        Создать запись об устройстве, если её нет; обновить last_seen, координаты,
        батарею. first_seen_at не трогаем.
        """
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO devices (device_id, channel, last_latitude, last_longitude, battery_pct)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET
                    last_seen_at   = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
                    last_latitude  = excluded.last_latitude,
                    last_longitude = excluded.last_longitude,
                    battery_pct    = COALESCE(excluded.battery_pct, devices.battery_pct),
                    is_active      = 1
                """,
                (device_id, channel, latitude, longitude, battery_pct),
            )

    # ------------------------------------------------------------------
    # pings, sos_events, chat_messages: insert
    # ------------------------------------------------------------------
    def insert_ping(
        self,
        pkt: MeshPacket,
        receiver_rssi: Optional[int] = None,
    ) -> None:
        battery, rssi_last, seq = parse_ping_payload(pkt.payload)
        # devices ссылка по FK — сначала upsert.
        self.upsert_device(
            pkt.device_id, int(pkt.channel),
            pkt.latitude, pkt.longitude, battery_pct=battery,
        )
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO pings (device_id, latitude, longitude,
                                   battery_pct, rssi_last, seq, receiver_rssi)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (pkt.device_id, pkt.latitude, pkt.longitude,
                 battery, rssi_last, seq, receiver_rssi),
            )

    def insert_sos(
        self,
        pkt: MeshPacket,
        receiver_rssi: Optional[int] = None,  # noqa: ARG002 — пока не пишем, схема не имеет колонки
    ) -> None:
        sos_type, message = parse_sos_payload(pkt.payload)
        self.upsert_device(
            pkt.device_id, int(pkt.channel),
            pkt.latitude, pkt.longitude,
        )
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO sos_events (device_id, latitude, longitude, sos_type, message)
                VALUES (?, ?, ?, ?, ?)
                """,
                (pkt.device_id, pkt.latitude, pkt.longitude, int(sos_type), message),
            )

    def insert_chat(self, pkt: MeshPacket) -> None:
        text = parse_chat_payload(pkt.payload)
        self.upsert_device(
            pkt.device_id, int(pkt.channel),
            pkt.latitude, pkt.longitude,
        )
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO chat_messages (device_id, latitude, longitude, channel, message)
                VALUES (?, ?, ?, ?, ?)
                """,
                (pkt.device_id, pkt.latitude, pkt.longitude, int(pkt.channel), text),
            )
