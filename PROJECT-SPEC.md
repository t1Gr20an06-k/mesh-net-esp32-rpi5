# PROJECT-SPEC.md — Автономная система безопасности горных маршрутов

## Проблема

В горных районах России (Приэльбрусье, Архыз, Домбай и др.) отсутствует сотовая связь на значительной части маршрутов. Это создаёт три критические проблемы:

- **Нет связи** — турист в нештатной ситуации не может вызвать помощь
- **Нет навигации** — при разделении группы спасатели не видят координаты
- **Нет информации** — карты, маршруты, POI недоступны без интернета

## Решение

Автономная mesh-сеть на базе LoRa 868 МГц с тремя типами узлов:

| Узел | Железо | Роль |
|------|--------|------|
| Пользовательский терминал | ESP32 WROOM + SX1262 | Трекинг, SOS, чат |
| Инфо-точка | RPi5 + SX1262 + солнечная панель | Ретрансляция, Wi-Fi портал |
| База спасателей | RPi5 + SX1262 | Мониторинг, GigaChat AI, карта |

---

## Аппаратные компоненты прототипа

| Компонент | Кол-во | Спецификация |
|-----------|--------|--------------|
| Raspberry Pi 5 | 2 | Quad-core ARM Cortex-A76, 8 ГБ RAM |
| ESP32 S3-N16 R8 | 1 | 240 МГц, 8 MB SRAM, Wi-Fi 2.4G + BT 5.0|
| LoRa SX1262 | 3 | 868 МГц, +22 дБм, чувствительность −129 дБм |
| Дисплей TFT | 1 | 3.5", SPI |
| АКБ 18650 | 2+ | Питание терминала, до 30 ч |
| ~~GPS NEO-6M~~ | — | **Не используется.** GPS берётся со смартфона туриста через Geolocation API → WebSocket → ESP32 |
| Солнечная панель | 2 | 20–30 Вт, контроллер заряда MPPT |

**Подключение SX1262 к RPi5 (SPI0):**
```
SX1262 SCK  → RPi GPIO 11 (SPI0_CLK)
SX1262 MOSI → RPi GPIO 10 (SPI0_MOSI)
SX1262 MISO → RPi GPIO 9  (SPI0_MISO)
SX1262 NSS  → RPi GPIO 8  (SPI0_CE0)
SX1262 RESET→ RPi GPIO 22
SX1262 DIO1 → RPi GPIO 23
SX1262 BUSY → RPi GPIO 24
```

---

## Протокол

Подробнее: `Docs/protocol.md`

**Структура пакета (64 байта фиксированно):**

| Поле | Байты | Описание |
|------|-------|----------|
| version | 1 | Версия протокола (текущая: 1) |
| type | 1 | PING=0, CHAT=1, SOS=2, ACK=3 |
| device_id | 2 | Уникальный ID устройства |
| channel | 1 | 0=TOURIST, 1=RESCUE |
| ttl | 1 | 3–5, декрементируется при ретрансляции |
| lat | 4 | Широта × 1e6, int32 |
| lon | 4 | Долгота × 1e6, int32 |
| timestamp | 4 | Unix timestamp |
| payload | 44 | Текст или данные |
| crc | 2 | CRC-16/CCITT |

**Mesh (flooding):** каждый узел ретранслирует пакет с TTL-1, кэш последних 50 packet_hash для дедупликации.

**SOS-приоритет:** пакет type=SOS отправляется 3 раза с интервалом 500 мс, обрабатывается вне очереди на всех узлах.

---

## Сервисы

### `services/lora-station`
Демон на Python, работает на каждом RPi. Общается с SX1262 через SPI (pigpio).
- Принимает пакеты → парсит → публикует в локальный MQTT или напрямую в БД
- Ретранслирует (mesh logic): дедупликация по hash(device_id + timestamp)
- Фильтрует RESCUE-канал по whitelist

### `services/rescue-api`
FastAPI REST API, работает на базе спасателей.

Эндпоинты:
```
GET  /devices          — список активных устройств
GET  /devices/{id}/track — трек устройства
GET  /sos              — активные SOS-сигналы
POST /sos/{id}/ack     — подтверждение SOS
GET  /stats            — общая статистика
WS   /ws               — WebSocket для дашборда (real-time события)
```

### `services/gigachat-agent`
FastAPI-сервис. Принимает текстовый вопрос оператора → GigaChat API с function calling → локальный Tool API → ответ на русском.

**Доступные инструменты GigaChat:**
```python
get_tourists()          # список активных туристов
get_sos()               # активные SOS-сигналы
get_location(device_id) # последние координаты устройства
get_stats()             # общая статистика маршрута
```

### `services/relay-node`
Упрощённый вариант lora-station для инфо-точки. Только ретрансляция, без сохранения в БД.

---

## Веб-интерфейсы

### `web/rescue-dashboard`
React + Leaflet.js + WebSocket. Работает на RPi5 базы спасателей, доступен по локальной сети.
- Офлайн-карта (тайлы предзагружены через `scripts/import_tiles`)
- Маркеры всех активных устройств, обновление в реальном времени
- Панель SOS-алертов со звуковым уведомлением
- Чат с GigaChat AI (через gigachat-agent API)

### `web/info-portal`
Статический HTML/JS/CSS, captive Wi-Fi портал на инфо-точке.
- Открывается автоматически при подключении к Wi-Fi точки
- Офлайн-карта участка маршрута
- Фото и описание достопримечательностей
- Кнопка SOS (отправляет запрос на relay-node API)
- Работает без JavaScript-фреймворков (чистый HTML для совместимости)

### `web/tourist-web`
Мобильный веб-интерфейс, доступный через Wi-Fi AP терминала ESP32. **Является источником GPS** — ESP32 не имеет GPS-модуля.
- Читает координаты через `navigator.geolocation.watchPosition` и отправляет на ESP32 по WebSocket каждые 30 с
- ESP32 упаковывает координаты в LoRa PING и отправляет в mesh
- При SOS — запрашивает свежую позицию перед отправкой
- Показывает GPS-точность, чат с группой, список участников
- Кнопка SOS с подтверждением

---

## База данных (SQLite)

Схема: `scripts/db_init/init.sql`

```sql
-- Устройства
CREATE TABLE devices (
    device_id    INTEGER PRIMARY KEY,
    name         TEXT,
    type         TEXT CHECK(type IN ('tourist', 'rescue', 'relay')),
    registered   INTEGER  -- unix timestamp
);

-- GPS-треки
CREATE TABLE tracks (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id INTEGER REFERENCES devices(device_id),
    lat       REAL,
    lon       REAL,
    ts        INTEGER,  -- unix timestamp
    rssi      INTEGER,
    snr       REAL
);

-- SOS-события
CREATE TABLE sos_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id    INTEGER REFERENCES devices(device_id),
    lat          REAL,
    lon          REAL,
    ts           INTEGER,
    acknowledged INTEGER DEFAULT 0,
    ack_ts       INTEGER
);

-- Сообщения чата
CREATE TABLE messages (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id INTEGER,
    channel   INTEGER,
    payload   TEXT,
    ts        INTEGER
);
```

---

## Развёртывание

Подробнее: `Docs/deployment.md`

На RPi5 каждый сервис запускается как systemd unit:
```
mesh-lora-station.service
mesh-rescue-api.service
mesh-gigachat-agent.service   # только на базе спасателей
mesh-relay-node.service       # только на инфо-точке
nginx.service                 # reverse proxy для веб-интерфейсов
```
