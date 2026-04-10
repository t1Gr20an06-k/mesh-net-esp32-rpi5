# База данных

SQLite 3, файл: `/var/lib/mesh-net/mesh.db`

Инициализация: `bash scripts/db_init/init.sh`

---

## Схема

```sql
-- Зарегистрированные устройства
CREATE TABLE devices (
    device_id    INTEGER PRIMARY KEY,          -- 0x0001 – 0xFFFF
    name         TEXT NOT NULL DEFAULT '',     -- "Иванов П." / "Инфо-точка Архыз"
    type         TEXT NOT NULL                 -- 'tourist' | 'rescue' | 'relay'
                 CHECK(type IN ('tourist','rescue','relay')),
    registered   INTEGER NOT NULL              -- unix timestamp
);

-- GPS-треки (каждый PING/SOS пишет запись)
CREATE TABLE tracks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id    INTEGER NOT NULL REFERENCES devices(device_id),
    lat          REAL    NOT NULL,
    lon          REAL    NOT NULL,
    ts           INTEGER NOT NULL,             -- unix timestamp (из пакета)
    received_ts  INTEGER NOT NULL,             -- unix timestamp (когда получен на базе)
    rssi         INTEGER,                      -- dBm
    snr          REAL,                         -- dB
    packet_type  TEXT DEFAULT 'PING'
);
CREATE INDEX idx_tracks_device_ts ON tracks(device_id, ts DESC);

-- SOS-события
CREATE TABLE sos_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id    INTEGER NOT NULL REFERENCES devices(device_id),
    lat          REAL    NOT NULL,
    lon          REAL    NOT NULL,
    ts           INTEGER NOT NULL,
    received_ts  INTEGER NOT NULL,
    payload      TEXT    DEFAULT '',           -- текст из пакета (причина, если указана)
    acknowledged INTEGER NOT NULL DEFAULT 0,  -- 0=активный, 1=подтверждён
    ack_ts       INTEGER,
    ack_by       INTEGER                       -- device_id спасателя, который подтвердил
);
CREATE INDEX idx_sos_active ON sos_events(acknowledged, ts DESC);

-- Сообщения чата
CREATE TABLE messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id    INTEGER NOT NULL,
    channel      INTEGER NOT NULL DEFAULT 0,   -- 0=tourist, 1=rescue
    payload      TEXT    NOT NULL,
    ts           INTEGER NOT NULL
);
CREATE INDEX idx_messages_ts ON messages(ts DESC);

-- Кэш дедупликации (очищается при рестарте lora-station)
CREATE TABLE dedup_cache (
    packet_hash  TEXT PRIMARY KEY,             -- sha1(device_id || timestamp)
    received_ts  INTEGER NOT NULL
);
```

---

## Основные запросы

### Активные туристы (PING за последние 10 минут)

```sql
SELECT d.device_id, d.name, t.lat, t.lon, t.ts, t.rssi
FROM devices d
JOIN tracks t ON t.id = (
    SELECT id FROM tracks
    WHERE device_id = d.device_id
    ORDER BY ts DESC LIMIT 1
)
WHERE d.type = 'tourist'
  AND t.ts > strftime('%s','now') - 600
ORDER BY t.ts DESC;
```

### Активные SOS

```sql
SELECT s.id, d.name, s.lat, s.lon, s.ts, s.payload,
       (strftime('%s','now') - s.ts) / 60 AS minutes_ago
FROM sos_events s
JOIN devices d ON d.device_id = s.device_id
WHERE s.acknowledged = 0
ORDER BY s.ts DESC;
```

### Трек устройства за последние N часов

```sql
SELECT lat, lon, ts FROM tracks
WHERE device_id = :device_id
  AND ts > strftime('%s','now') - :hours * 3600
ORDER BY ts ASC;
```

### Статистика маршрута

```sql
SELECT
    (SELECT COUNT(DISTINCT device_id) FROM tracks
     WHERE ts > strftime('%s','now') - 600) AS active_tourists,
    (SELECT COUNT(*) FROM sos_events WHERE acknowledged = 0) AS active_sos,
    (SELECT COUNT(*) FROM tracks
     WHERE ts > strftime('%s','now') - 3600) AS pings_last_hour,
    (SELECT MAX(ts) FROM tracks) AS last_ping_ts;
```

---

## Обслуживание

### Архивирование старых треков (> 30 дней)

```bash
sqlite3 /var/lib/mesh-net/mesh.db \
  "INSERT INTO tracks_archive SELECT * FROM tracks WHERE ts < strftime('%s','now') - 2592000;
   DELETE FROM tracks WHERE ts < strftime('%s','now') - 2592000;"
```

### Очистка кэша дедупликации

```bash
sqlite3 /var/lib/mesh-net/mesh.db \
  "DELETE FROM dedup_cache WHERE received_ts < strftime('%s','now') - 3600;"
```

### Бэкап

```bash
sqlite3 /var/lib/mesh-net/mesh.db ".backup /var/lib/mesh-net/backup-$(date +%Y%m%d).db"
```

Рекомендуется запускать через cron ежедневно.

---

## Миграции

Нумерованные файлы в `scripts/db_init/`:
- `init.sql` — начальная схема
- `migrate_001.sql` — и т.д.

Скрипт применения:
```bash
bash scripts/db_init/migrate.sh
```

Текущая версия схемы хранится в `PRAGMA user_version`.
