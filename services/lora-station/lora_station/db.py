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
        self._migrate()

    def _migrate(self) -> None:
        """Идемпотентные DDL для новых таблиц/колонок.
        Так свежий `git pull` без перезапуска init.sh всё равно получит новую схему.

        Сюда же добавлять любые будущие изменения схемы — каждое CREATE/ALTER
        должно быть через IF NOT EXISTS / try-except.
        """
        # outgoing_chat — очередь сообщений ОТ базы К туристам (этап Б чата).
        # Schema повторяет init.sql, FK на chat_messages намеренно нет.
        with self._lock:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS outgoing_chat (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    message         TEXT    NOT NULL,
                    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                    sent_at         TEXT    DEFAULT NULL,
                    chat_message_id INTEGER DEFAULT NULL
                )
            """)
            self._conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_outgoing_pending
                    ON outgoing_chat(id) WHERE sent_at IS NULL
            """)
            self._conn.commit()

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

    # ------------------------------------------------------------------
    # outgoing_chat — очередь сообщений ОТ базы К туристам
    # ------------------------------------------------------------------
    # Заполняет rescue-api при POST /api/messages. Мы здесь только читаем
    # pending-записи (sent_at IS NULL) и помечаем отправленные после
    # успешной передачи в эфир.

    def fetch_pending_outgoing_chat(self, limit: int = 5) -> list[tuple[int, str]]:
        """Вернуть [(id, message), ...] — сообщения, которые ещё не ушли в эфир.
        Лимит — чтобы за один тик не пересушивать TxQueue (если в очереди
        накопилось много, отправим частями)."""
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT id, message FROM outgoing_chat
                WHERE sent_at IS NULL
                ORDER BY id ASC
                LIMIT ?
                """,
                (limit,),
            )
            return [(row[0], row[1]) for row in cur.fetchall()]

    def mark_outgoing_chat_sent(self, row_id: int) -> None:
        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE outgoing_chat
                SET sent_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
                WHERE id = ?
                """,
                (row_id,),
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
