-- ============================================================
-- Mesh-net Тропы — схема SQLite
-- Файл: scripts/db_init/init.sql
-- Запуск: sqlite3 /var/lib/mesh-net/mesh.db < init.sql
-- ============================================================

PRAGMA journal_mode = WAL;          -- Write-Ahead Log: быстрее запись, безопаснее при сбоях питания
PRAGMA foreign_keys = ON;

-- ============================================================
-- 1. devices — реестр всех известных устройств
--    Обновляется при каждом принятом пакете (last_seen, координаты, батарея).
-- ============================================================

CREATE TABLE IF NOT EXISTS devices (
    device_id       INTEGER PRIMARY KEY,            -- uint16 из пакета (0–65535)
    name            TEXT    DEFAULT '',              -- человекочитаемое имя, задаётся вручную
    channel         INTEGER NOT NULL DEFAULT 0,     -- 0=TOURIST, 1=RESCUE
    first_seen_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    last_seen_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    last_latitude   INTEGER DEFAULT 0,              -- int32, × 1e6
    last_longitude  INTEGER DEFAULT 0,              -- int32, × 1e6
    battery_pct     INTEGER DEFAULT NULL,           -- 0–100, NULL = неизвестно
    is_active       INTEGER NOT NULL DEFAULT 1      -- 1 = онлайн, 0 = архив
);

-- ============================================================
-- 2. pings — PING-пакеты (маяки)
--    Каждые ~30 сек от каждого терминала.
--    Самая объёмная таблица — старые записи можно чистить по дате.
-- ============================================================

CREATE TABLE IF NOT EXISTS pings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id       INTEGER NOT NULL REFERENCES devices(device_id),
    received_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    latitude        INTEGER NOT NULL,               -- int32, × 1e6
    longitude       INTEGER NOT NULL,               -- int32, × 1e6
    battery_pct     INTEGER,                        -- 0–100
    rssi_last       INTEGER,                        -- int8: RSSI последнего пакета на стороне терминала
    seq             INTEGER,                        -- uint16: порядковый номер
    receiver_rssi   INTEGER                         -- RSSI на стороне приёмника (RPi)
);

CREATE INDEX IF NOT EXISTS idx_pings_device    ON pings(device_id);
CREATE INDEX IF NOT EXISTS idx_pings_time      ON pings(received_at);

-- ============================================================
-- 3. sos_events — SOS-сигналы
--    Критическая таблица — НИКОГДА не удалять записи!
-- ============================================================

CREATE TABLE IF NOT EXISTS sos_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id       INTEGER NOT NULL REFERENCES devices(device_id),
    received_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    latitude        INTEGER NOT NULL,               -- int32, × 1e6
    longitude       INTEGER NOT NULL,               -- int32, × 1e6
    sos_type        INTEGER NOT NULL DEFAULT 0,     -- 0=неизвестно, 1=падение, 2=медицина, 3=заблудился, 4=погода
    message         TEXT    DEFAULT '',              -- UTF-8 из payload[1..47]
    acked           INTEGER NOT NULL DEFAULT 0,     -- 0 = не подтверждён, 1 = подтверждён
    acked_at        TEXT    DEFAULT NULL,            -- когда подтвердили
    acked_by        INTEGER DEFAULT NULL,            -- device_id спасателя, подтвердившего SOS
    resolved        INTEGER NOT NULL DEFAULT 0,     -- 0 = открыт, 1 = инцидент закрыт
    resolved_at     TEXT    DEFAULT NULL,
    notes           TEXT    DEFAULT ''               -- заметки спасателя
);

CREATE INDEX IF NOT EXISTS idx_sos_device      ON sos_events(device_id);
CREATE INDEX IF NOT EXISTS idx_sos_unresolved  ON sos_events(resolved) WHERE resolved = 0;

-- ============================================================
-- 4. chat_messages — текстовые сообщения CHAT
-- ============================================================

CREATE TABLE IF NOT EXISTS chat_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id       INTEGER NOT NULL REFERENCES devices(device_id),
    received_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    latitude        INTEGER NOT NULL,               -- int32, × 1e6
    longitude       INTEGER NOT NULL,               -- int32, × 1e6
    channel         INTEGER NOT NULL DEFAULT 0,     -- 0=TOURIST, 1=RESCUE
    message         TEXT    NOT NULL DEFAULT ''      -- UTF-8, до 48 байт
);

CREATE INDEX IF NOT EXISTS idx_chat_device     ON chat_messages(device_id);
CREATE INDEX IF NOT EXISTS idx_chat_time       ON chat_messages(received_at);
CREATE INDEX IF NOT EXISTS idx_chat_channel    ON chat_messages(channel);
