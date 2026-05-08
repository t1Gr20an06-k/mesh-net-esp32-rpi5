"""SQLite-обёртка для rescue-api.

Принципы:
- БД пишет lora-station, мы в основном читаем. WAL-режим (его включил
  lora-station ещё в init.sql) разрешает concurrent readers + 1 writer
  без блокировок.
- Открываем БД read-only (`?mode=ro`) — гарантия что REST случайно не
  напишет лишнего. Read-write только для /api/sos/.../ack|resolve.
- Connection per request: на нашем трафике (1 PING / 10 сек) дёшево;
  избавляет от headache с шарингом sqlite3.Connection между потоками
  asyncio (sqlite3 хочет либо один поток на conn, либо
  check_same_thread=False — путей минимум).
"""

import sqlite3
from contextlib import contextmanager
from typing import Iterator, List, Optional, Tuple

DEFAULT_DB_PATH = "/var/lib/mesh-net/mesh.db"

# Турист считается «активным», если PING был не позже стольких минут назад.
# Используется в /api/tourists и в /api/stats (devices_online).
# ESP32 пингает раз в ~10 сек, 2 минуты дают запас в 12 потерянных пакетов
# подряд — если все они не дошли, явно проблема с устройством, оператор
# должен это видеть, а не думать что турист "ещё на связи".
ACTIVE_THRESHOLD_MIN = 2


# ============================================================
# Подключение
# ============================================================

def _connect(db_path: str, read_only: bool) -> sqlite3.Connection:
    if read_only:
        # URI-режим, чтобы можно было ?mode=ro. PRAGMA на ro-conn не делаем —
        # foreign_keys уже включён в БД при инициализации, на чтение не влияет.
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    else:
        conn = sqlite3.connect(db_path, timeout=5.0)
        conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def db_read(db_path: str = DEFAULT_DB_PATH) -> Iterator[sqlite3.Connection]:
    conn = _connect(db_path, read_only=True)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def db_write(db_path: str = DEFAULT_DB_PATH) -> Iterator[sqlite3.Connection]:
    conn = _connect(db_path, read_only=False)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ============================================================
# Запросы — общая статистика
# ============================================================

def get_stats(conn: sqlite3.Connection) -> dict:
    # ⚠ Таймстампы в БД пишутся как '2026-05-07T20:03:36Z' (см. init.sql).
    # SQLite сравнивает строки лексикографически. datetime('now', '-X minutes')
    # возвращает формат '2026-05-07 20:01:36' (с пробелом, без Z), а 'T' (0x54)
    # лексикографически больше ' ' (0x20) — фильтр '> datetime(...)' окажется
    # ИСТИНОЙ для ВСЕХ записей. Поэтому правую часть тоже формируем через
    # strftime в формате 'T...Z'.
    row = conn.execute("""
        SELECT
            (SELECT COUNT(*) FROM pings)                                      AS pings_total,
            (SELECT COUNT(*) FROM sos_events)                                 AS sos_total,
            (SELECT COUNT(*) FROM sos_events WHERE resolved = 0)              AS sos_open,
            (SELECT COUNT(*) FROM devices)                                    AS devices_total,
            (SELECT COUNT(*) FROM devices
              WHERE last_seen_at > strftime('%Y-%m-%dT%H:%M:%SZ',
                                            'now', '-' || :m || ' minutes')) AS devices_online
    """, {"m": ACTIVE_THRESHOLD_MIN}).fetchone()
    return dict(row)


# ============================================================
# Запросы — туристы и устройства
# ============================================================

def list_active_tourists(
    conn: sqlite3.Connection,
    exclude_device_id: Optional[int] = None,
) -> List[sqlite3.Row]:
    """Устройства, от которых был PING за последние ACTIVE_THRESHOLD_MIN минут.
    Подтягиваем последний PING (координаты, RSSI, батарея).

    `exclude_device_id` — id самой базы (NODE_DEVICE_ID). Когда оператор
    отвечает в чат, в `devices` появляется запись о базе (см. ensure_base_device
    в rescue-api/db.py). Не хочется видеть «База спасателей» в списке туристов.

    Про `strftime('%Y-%m-%dT%H:%M:%SZ', ...)` — см. комментарий в get_stats."""
    return conn.execute("""
        SELECT
            d.device_id, d.name, d.channel, d.last_seen_at,
            p.latitude      AS lat_e6,
            p.longitude     AS lon_e6,
            p.battery_pct   AS battery_pct,
            p.receiver_rssi AS receiver_rssi,
            p.received_at   AS last_ping_at
        FROM devices d
        LEFT JOIN pings p ON p.id = (
            SELECT id FROM pings
            WHERE device_id = d.device_id
            ORDER BY id DESC LIMIT 1
        )
        WHERE d.last_seen_at > strftime('%Y-%m-%dT%H:%M:%SZ',
                                        'now', '-' || :m || ' minutes')
          AND (:excl IS NULL OR d.device_id != :excl)
        ORDER BY d.last_seen_at DESC
    """, {"m": ACTIVE_THRESHOLD_MIN, "excl": exclude_device_id}).fetchall()


def list_devices(
    conn: sqlite3.Connection,
    exclude_device_id: Optional[int] = None,
) -> List[sqlite3.Row]:
    if exclude_device_id is None:
        return conn.execute("SELECT * FROM devices ORDER BY device_id").fetchall()
    return conn.execute(
        "SELECT * FROM devices WHERE device_id != ? ORDER BY device_id",
        (exclude_device_id,),
    ).fetchall()


# ============================================================
# Запросы — pings
# ============================================================

def list_pings(
    conn: sqlite3.Connection,
    device_id: Optional[int],
    hours: float,
    limit: int,
) -> List[sqlite3.Row]:
    # См. комментарий в get_stats про strftime — формат таймстампа в БД 'T...Z'.
    if device_id is not None:
        return conn.execute("""
            SELECT * FROM pings
            WHERE device_id = :did
              AND received_at > strftime('%Y-%m-%dT%H:%M:%SZ',
                                          'now', '-' || :h || ' hours')
            ORDER BY id DESC LIMIT :limit
        """, {"did": device_id, "h": hours, "limit": limit}).fetchall()
    return conn.execute("""
        SELECT * FROM pings
        WHERE received_at > strftime('%Y-%m-%dT%H:%M:%SZ',
                                      'now', '-' || :h || ' hours')
        ORDER BY id DESC LIMIT :limit
    """, {"h": hours, "limit": limit}).fetchall()


# ============================================================
# Запросы — SOS
# ============================================================

def list_sos(conn: sqlite3.Connection, only_open: bool, limit: int = 200) -> List[sqlite3.Row]:
    if only_open:
        return conn.execute("""
            SELECT * FROM sos_events
            WHERE resolved = 0
            ORDER BY received_at DESC
            LIMIT :limit
        """, {"limit": limit}).fetchall()
    return conn.execute("""
        SELECT * FROM sos_events
        ORDER BY received_at DESC
        LIMIT :limit
    """, {"limit": limit}).fetchall()


def get_sos(conn: sqlite3.Connection, sos_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM sos_events WHERE id = ?", (sos_id,)).fetchone()


def ack_sos(conn: sqlite3.Connection, sos_id: int, acked_by: Optional[int]) -> Optional[sqlite3.Row]:
    """Помечаем SOS как acked. Если уже acked — не перезаписываем acked_at/by
    (важно юридически: время первого ack должно сохраниться)."""
    conn.execute("""
        UPDATE sos_events
        SET acked    = 1,
            acked_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
            acked_by = :by
        WHERE id = :id AND acked = 0
    """, {"by": acked_by, "id": sos_id})
    return conn.execute("SELECT * FROM sos_events WHERE id = ?", (sos_id,)).fetchone()


def resolve_sos(conn: sqlite3.Connection, sos_id: int, notes: str) -> Optional[sqlite3.Row]:
    """Закрываем инцидент. resolve можно делать без предварительного ack
    (бывает — спасатели уже на месте, формальности потом)."""
    conn.execute("""
        UPDATE sos_events
        SET resolved    = 1,
            resolved_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
            notes       = :notes
        WHERE id = :id
    """, {"notes": notes, "id": sos_id})
    return conn.execute("SELECT * FROM sos_events WHERE id = ?", (sos_id,)).fetchone()


# ============================================================
# Запросы для WebSocket-поллера
# ============================================================

def get_max_ids(conn: sqlite3.Connection) -> Tuple[int, int, int]:
    """Текущие MAX(id) в pings, sos_events, chat_messages. Используем как
    стартовую точку для поллера: события до этих id уже в БД на момент
    старта rescue-api, их в WS не пушим (иначе дашборд при подключении
    захлебнётся)."""
    p = conn.execute("SELECT IFNULL(MAX(id), 0) FROM pings").fetchone()[0]
    s = conn.execute("SELECT IFNULL(MAX(id), 0) FROM sos_events").fetchone()[0]
    c = conn.execute("SELECT IFNULL(MAX(id), 0) FROM chat_messages").fetchone()[0]
    return p, s, c


def get_new_pings(conn: sqlite3.Connection, since_id: int, limit: int = 100) -> List[sqlite3.Row]:
    return conn.execute("""
        SELECT * FROM pings WHERE id > ? ORDER BY id ASC LIMIT ?
    """, (since_id, limit)).fetchall()


def get_new_sos(conn: sqlite3.Connection, since_id: int, limit: int = 100) -> List[sqlite3.Row]:
    return conn.execute("""
        SELECT * FROM sos_events WHERE id > ? ORDER BY id ASC LIMIT ?
    """, (since_id, limit)).fetchall()


# ============================================================
# Запросы — chat_messages
# ============================================================
# В таблице нет колонки name, поэтому JOIN-им devices для имени
# отправителя — фронт сразу получает "Вася (#16): привет" вместо
# "device 16: привет".

def list_chat(conn: sqlite3.Connection, limit: int = 100) -> List[sqlite3.Row]:
    """Последние N сообщений в хронологическом порядке (старые → новые)."""
    return conn.execute("""
        SELECT c.*, d.name AS device_name
        FROM chat_messages c
        LEFT JOIN devices d ON d.device_id = c.device_id
        ORDER BY c.id DESC
        LIMIT :limit
    """, {"limit": limit}).fetchall()[::-1]


# --- запись от базы (этап Б чата): сообщение оператора в эфир ---
# rescue-api использует две вставки в одной транзакции:
#   1. chat_messages — чтобы дашборд сразу увидел в WS push'е
#   2. outgoing_chat — очередь для lora-station, она это вычитает и передаст
# device_id у сообщения = NODE_DEVICE_ID базы (по умолчанию 0x0001).
# Чтобы FK chat_messages.device_id → devices не упал, гарантируем запись в devices.

def ensure_base_device(
    conn: sqlite3.Connection,
    device_id: int,
    name: str = "База спасателей",
    channel: int = 1,  # RESCUE
) -> None:
    """Создаёт запись в devices для базы, если её ещё нет. Идемпотентно.
    Без этого первый POST /api/messages упадёт с FOREIGN KEY constraint:
    у chat_messages.device_id есть REFERENCES devices(device_id)."""
    conn.execute("""
        INSERT INTO devices (device_id, name, channel, last_latitude, last_longitude)
        VALUES (:id, :name, :ch, 0, 0)
        ON CONFLICT(device_id) DO NOTHING
    """, {"id": device_id, "name": name, "ch": channel})


def insert_base_chat(
    conn: sqlite3.Connection,
    base_device_id: int,
    message: str,
) -> int:
    """Запись chat_messages от базы (без координат). Возвращает id новой строки.
    Поднимет WS event 'chat' → дашборд увидит ответ в общей ленте."""
    cur = conn.execute("""
        INSERT INTO chat_messages (device_id, latitude, longitude, channel, message)
        VALUES (:id, 0, 0, 0, :msg)
    """, {"id": base_device_id, "msg": message})
    return cur.lastrowid


def insert_outgoing_chat(
    conn: sqlite3.Connection,
    message: str,
    chat_message_id: Optional[int] = None,
) -> int:
    """Поставить сообщение в очередь outgoing_chat. lora-station периодически
    вычитывает её и шлёт в эфир."""
    cur = conn.execute("""
        INSERT INTO outgoing_chat (message, chat_message_id)
        VALUES (:msg, :cid)
    """, {"msg": message, "cid": chat_message_id})
    return cur.lastrowid


def get_new_chat(conn: sqlite3.Connection, since_id: int, limit: int = 100) -> List[sqlite3.Row]:
    return conn.execute("""
        SELECT c.*, d.name AS device_name
        FROM chat_messages c
        LEFT JOIN devices d ON d.device_id = c.device_id
        WHERE c.id > ? ORDER BY c.id ASC LIMIT ?
    """, (since_id, limit)).fetchall()


# ============================================================
# Админ — полная очистка БД (для отладки)
# ============================================================

PURGEABLE_TABLES = ("pings", "sos_events", "chat_messages", "outgoing_chat", "devices")


def purge_tables(conn: sqlite3.Connection, tables: list[str]) -> dict:
    """Удаляет переданные таблицы из mesh.db. Возвращает {table: rowcount}.

    Аргумент `tables` — подмножество PURGEABLE_TABLES. Неизвестные имена —
    ValueError (защита от SQL-инъекции через f-string DELETE FROM).

    FK-каскад: pings/sos_events/chat_messages ссылаются на devices(device_id).
    Если просят почистить devices — обязательно надо подчистить и детей,
    иначе DELETE FROM devices упадёт с FOREIGN KEY constraint failed.
    Молча включаем «детей» — иначе UI получает невнятную 500-ку.

    sqlite_sequence сбрасываем только для тех таблиц, которые реально
    почистили — иначе можно случайно «обнулить» счётчик той таблицы,
    которую трогать не просили.

    ⚠ WS-broadcaster хранит last_seen_id в памяти — его сбрасывает
    admin_purge() в app.py сразу после этого вызова.
    """
    requested = set(tables)
    bad = requested - set(PURGEABLE_TABLES)
    if bad:
        raise ValueError(f"неизвестные таблицы: {sorted(bad)}")

    # FK-каскад: devices обязан тащить за собой всех детей.
    # outgoing_chat не имеет FK на devices, но логически тоже относится к чатам —
    # при тотальной очистке устройств чистим и его, чтобы не остался хвост
    # неотправленных сообщений «в никуда».
    if "devices" in requested:
        requested |= {"pings", "sos_events", "chat_messages", "outgoing_chat"}

    # Порядок строгий — сначала «дети», потом «родители».
    # outgoing_chat без FK, но ставим перед chat_messages для консистентности.
    res: dict[str, int] = {}
    for table in ("pings", "sos_events", "outgoing_chat", "chat_messages", "devices"):
        if table in requested:
            cur = conn.execute(f"DELETE FROM {table}")
            res[table] = cur.rowcount

    # sqlite_sequence существует только для AUTOINCREMENT-таблиц.
    seq = [t for t in requested if t in ("pings", "sos_events", "chat_messages", "outgoing_chat")]
    if seq:
        placeholders = ",".join("?" for _ in seq)
        try:
            conn.execute(
                f"DELETE FROM sqlite_sequence WHERE name IN ({placeholders})",
                seq,
            )
        except sqlite3.OperationalError:
            pass
    return res
