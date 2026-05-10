# База данных Mesh-net Тропы

SQLite 3, файл: `/var/lib/mesh-net/mesh.db`
Источник правды для схемы: [`scripts/db_init/init.sql`](../scripts/db_init/init.sql)

Инициализация (один раз после клонирования репо):
```bash
bash scripts/db_init/init.sh
```

WAL-режим включён → одновременно с файлом `.db` живут `mesh.db-wal` и
`mesh.db-shm`. Это нормально, копировать их вместе при бэкапе.

---

## Схема (5 таблиц)

| Таблица         | Что хранит                          | Можно чистить? |
|-----------------|-------------------------------------|----------------|
| `devices`       | реестр всех увиденных устройств     | да (пересоздаст себя; запись о базе создаётся при первом ответе оператора) |
| `pings`         | PING-пакеты с координатами          | да, по дате |
| `sos_events`    | SOS-события (acked / resolved)      | **НЕТ** в проде — только архивировать |
| `chat_messages` | CHAT-сообщения (от туристов и от базы — `device_id=NODE_DEVICE_ID`) | да, по дате |
| `outgoing_chat` | Очередь ответов базы → туристы. `sent_at IS NULL` = pending | да |

Координаты везде хранятся как `INTEGER` × 1e6 (из пакета). Чтобы получить
градусы — делить на 1 000 000:

```sql
SELECT device_id, latitude / 1000000.0 AS lat, longitude / 1000000.0 AS lon FROM pings;
```

Полный текст схемы — в `scripts/db_init/init.sql`.

---

## Чтение данных

Перед командами **остановить демон** не обязательно — SQLite в WAL-режиме
позволяет читать параллельно с записью.

### Интерактивная сессия

```bash
sqlite3 /var/lib/mesh-net/mesh.db
sqlite> .mode column
sqlite> .headers on
sqlite> .tables
sqlite> SELECT * FROM pings ORDER BY id DESC LIMIT 10;
sqlite> .quit
```

### Однострочные команды

```bash
# Последние 10 PING-ов
sqlite3 -header -column /var/lib/mesh-net/mesh.db \
  'SELECT id, device_id, latitude/1000000.0 AS lat, longitude/1000000.0 AS lon,
          battery_pct, seq, receiver_rssi, received_at
   FROM pings ORDER BY id DESC LIMIT 10;'

# Все известные устройства
sqlite3 -header -column /var/lib/mesh-net/mesh.db 'SELECT * FROM devices;'

# Активные SOS (не подтверждённые)
sqlite3 -header -column /var/lib/mesh-net/mesh.db \
  'SELECT id, device_id, latitude/1000000.0 AS lat, longitude/1000000.0 AS lon,
          sos_type, message, received_at
   FROM sos_events WHERE acked = 0 ORDER BY received_at DESC;'

# Счётчики по таблицам
sqlite3 /var/lib/mesh-net/mesh.db \
  'SELECT "pings", COUNT(*) FROM pings
   UNION ALL SELECT "sos", COUNT(*) FROM sos_events
   UNION ALL SELECT "chat", COUNT(*) FROM chat_messages
   UNION ALL SELECT "devices", COUNT(*) FROM devices;'
```

### Полезные SQL-запросы

```sql
-- Активные туристы — последний пакет от каждого за последние 10 минут
SELECT d.device_id, d.name,
       p.latitude/1000000.0 AS lat, p.longitude/1000000.0 AS lon,
       p.received_at, p.receiver_rssi
FROM devices d
JOIN pings p ON p.id = (
    SELECT id FROM pings WHERE device_id = d.device_id
    ORDER BY id DESC LIMIT 1
)
WHERE p.received_at > datetime('now', '-10 minutes')
ORDER BY p.received_at DESC;

-- Трек одного устройства за последний час
SELECT latitude/1000000.0 AS lat, longitude/1000000.0 AS lon, received_at
FROM pings
WHERE device_id = 16  -- 0x0010
  AND received_at > datetime('now', '-1 hour')
ORDER BY id ASC;

-- Сколько пакетов в час за сегодня
SELECT strftime('%H', received_at) AS hour, COUNT(*) AS pings
FROM pings WHERE date(received_at) = date('now')
GROUP BY hour ORDER BY hour;

-- Что висит в исходящей очереди базы (не отправлено в эфир)
SELECT id, message, created_at FROM outgoing_chat
WHERE sent_at IS NULL ORDER BY id;

-- Полная история чата с туристами (с именами через JOIN)
SELECT c.received_at, c.device_id, COALESCE(d.name, '') AS name, c.message
FROM chat_messages c
LEFT JOIN devices d ON d.device_id = c.device_id
ORDER BY c.id DESC LIMIT 50;
```

---

## Экспорт в текстовые файлы

### CSV (Excel/Numbers)

```bash
sqlite3 -header -csv /var/lib/mesh-net/mesh.db 'SELECT * FROM pings'         > /tmp/pings.csv
sqlite3 -header -csv /var/lib/mesh-net/mesh.db 'SELECT * FROM devices'       > /tmp/devices.csv
sqlite3 -header -csv /var/lib/mesh-net/mesh.db 'SELECT * FROM sos_events'    > /tmp/sos.csv
sqlite3 -header -csv /var/lib/mesh-net/mesh.db 'SELECT * FROM chat_messages' > /tmp/chat.csv
```

### Полный SQL-дамп (можно потом импортировать обратно)

```bash
sqlite3 /var/lib/mesh-net/mesh.db .dump > /tmp/mesh-dump.sql
```

Восстановить из дампа в новый файл:
```bash
sqlite3 /tmp/restored.db < /tmp/mesh-dump.sql
```

### Читаемая таблица (txt)

```bash
sqlite3 -header -column /var/lib/mesh-net/mesh.db \
  'SELECT * FROM pings ORDER BY id DESC' > /tmp/pings.txt
```

### Markdown (для отчётов)

```bash
sqlite3 -markdown /var/lib/mesh-net/mesh.db \
  'SELECT id, device_id, received_at, receiver_rssi FROM pings ORDER BY id DESC LIMIT 20'
```

---

## Очистка данных

⚠ **Перед очисткой остановить демон** (`Ctrl-C` или `sudo systemctl stop mesh-lora-station`),
иначе он держит файл открытым.

⚠ **`sos_events` в боевом режиме не удаляем** — только архивируем. Юридически
данные о ЧС могут понадобиться. На этапе разработки чистить можно.

### Только PING-и (devices и SOS оставить)

```bash
sqlite3 /var/lib/mesh-net/mesh.db <<'SQL'
DELETE FROM pings;
DELETE FROM sqlite_sequence WHERE name = 'pings';   -- сброс AUTOINCREMENT
VACUUM;                                             -- сожмёт файл
SQL
```

### Все данные, схему оставить (для тестов)

```bash
sqlite3 /var/lib/mesh-net/mesh.db <<'SQL'
DELETE FROM pings;
DELETE FROM sos_events;
DELETE FROM chat_messages;
DELETE FROM outgoing_chat;
DELETE FROM devices;
DELETE FROM sqlite_sequence;
VACUUM;
SQL
```

**Проще через UI:** в правой нижней панели дашборда есть админ-секция
«Отладка — очистить БД» с чекбоксами. Эндпоинт `POST /api/admin/purge`
сам учитывает FK-каскад (если выбрать `devices`, дочерние таблицы
очищаются автоматически) и сбрасывает счётчики WS-broadcaster — после
очистки новые пакеты сразу появятся в дашборде без рестарта.

### Старые PING-и (> N дней)

```bash
sqlite3 /var/lib/mesh-net/mesh.db \
  "DELETE FROM pings WHERE received_at < datetime('now', '-30 days'); VACUUM;"
```

### Снести БД целиком и пересоздать

```bash
sudo rm /var/lib/mesh-net/mesh.db /var/lib/mesh-net/mesh.db-wal /var/lib/mesh-net/mesh.db-shm
bash scripts/db_init/init.sh
```

---

## Бэкап

### Безопасный бэкап на работающей БД (online backup)

```bash
sqlite3 /var/lib/mesh-net/mesh.db ".backup /var/lib/mesh-net/backup-$(date +%Y%m%d).db"
```

Это правильный способ на живой базе — SQLite берёт консистентный снимок,
не блокируя писателей надолго. Не путать с `cp` (может скопировать
несогласованное состояние, если идёт запись).

### Архивный SQL-дамп (txt, переносится между версиями SQLite)

```bash
sqlite3 /var/lib/mesh-net/mesh.db .dump | gzip > /var/lib/mesh-net/backup-$(date +%Y%m%d).sql.gz
```

### Бэкап по расписанию

Например, ежедневный cron на пользователе с правами на каталог:
```cron
0 3 * * * sqlite3 /var/lib/mesh-net/mesh.db ".backup /var/lib/mesh-net/backup-$(date +\%Y\%m\%d).db"
```

---

## Логи демона vs данные

Это разные вещи:

| | Где | Как читать |
|---|---|---|
| **Данные** (PING/SOS/CHAT/devices) | `/var/lib/mesh-net/mesh.db` | команды выше |
| **Логи демона** (`[RX#1] PING ...`) | stdout / journalctl | см. ниже |

Когда демон запущен из терминала — логи никуда не сохраняются, пропадают
при закрытии shell. Сохранить в файл:
```bash
python -m lora_station -v 2>&1 | tee /var/log/lora-station.log
```

Когда демон поднят через systemd-юнит (этап 5) — логи автоматически
уходят в journal:
```bash
sudo journalctl -u mesh-lora-station -f                   # live tail
sudo journalctl -u mesh-lora-station --since today        # за сегодня
sudo journalctl -u mesh-lora-station -p warning           # только WARNING+ERROR
sudo journalctl -u mesh-lora-station --since "1 hour ago" # за последний час
```

---

## Миграции

Текущая стратегия — **идемпотентная авто-миграция при старте сервиса**:
`Database.__init__` в `services/lora-station/lora_station/db.py` вызывает
`_migrate()`, который выполняет `CREATE TABLE IF NOT EXISTS` /
`CREATE INDEX IF NOT EXISTS` для всех новых таблиц. Так после `git pull`
без перезапуска `init.sh` БД получит новую схему сама.

Пример (выдержка из `_migrate()`):
```python
self._conn.execute("""
    CREATE TABLE IF NOT EXISTS outgoing_chat (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        message         TEXT    NOT NULL,
        created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        sent_at         TEXT    DEFAULT NULL,
        chat_message_id INTEGER DEFAULT NULL
    )
""")
```

При **разрушительных** изменениях схемы (DROP COLUMN, RENAME TABLE,
изменение типа) этот подход не сработает — нужен будет нумерованный
`migrate_NNN.sql` и ручной запуск:
```bash
sqlite3 /var/lib/mesh-net/mesh.db < scripts/db_init/migrate_001.sql
```

Текущая версия схемы — `PRAGMA user_version` пока не используется,
зарезервировано на будущее.
