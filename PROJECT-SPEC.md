# PROJECT-SPEC.md — Автономная система безопасности горных маршрутов

## Проблема

В горных районах России (Приэльбрусье, Архыз, Домбай и др.) отсутствует
сотовая связь на значительной части маршрутов. Это создаёт три критические
проблемы:

- **Нет связи** — турист в нештатной ситуации не может вызвать помощь
- **Нет навигации** — при разделении группы спасатели не видят координаты
- **Нет информации** — карты, маршруты, POI недоступны без интернета

## Решение

Автономная mesh-сеть на базе LoRa 868 МГц с тремя типами узлов:

| Узел | Железо | Роль |
|------|--------|------|
| Пользовательский терминал | ESP32-S3 N16R8 + HT-RA62 (SX1262) | Трекинг, SOS, чат |
| Инфо-точка | RPi5 + SX1262 + солнечная панель | Ретрансляция, Wi-Fi портал *(этап 5)* |
| База спасателей | RPi5 + SX1262 | Мониторинг, GigaChat AI, чат, карта |

---

## Аппаратные компоненты прототипа

| Компонент | Кол-во | Спецификация |
|-----------|--------|--------------|
| Raspberry Pi 5 | 2 | Quad-core ARM Cortex-A76, 8 ГБ RAM |
| ESP32-S3 N16R8 | 1+ | 240 МГц, 512 КБ SRAM + 8 МБ PSRAM, 16 МБ Flash, Wi-Fi 2.4G |
| LoRa HT-RA62 (SX1262) | 3 | 868 МГц, +14 дБм TX, чувствительность −136 дБм, TCXO 1.8 В |
| АКБ 18650 | 2+ | Питание терминала, до 30 ч |
| Солнечная панель *(этап 5)* | 2 | 20–30 Вт, контроллер заряда MPPT |

**GPS:** ESP32 не имеет своего GPS-модуля. Координаты берутся со
смартфона туриста через Geolocation API → POST /api/gps по HTTPS на
192.168.4.1 (см. `web/tourist-web` / встроенный в прошивку HTML).

**Подключение HT-RA62 → ESP32-S3:**

| Сигнал | GPIO | Сигнал | GPIO |
|--------|------|--------|------|
| MOSI | 11 | RESET | 9 |
| MISO | 13 | DIO1 | 14 |
| SCK | 12 | BUSY | 8 |
| CS | 10 | | |

**Подключение HT-RA62 → RPi5 (SPI0):**

```
HT-RA62 SCK   → RPi GPIO 11 (SPI0_CLK)
HT-RA62 MOSI  → RPi GPIO 10 (SPI0_MOSI)
HT-RA62 MISO  → RPi GPIO 9  (SPI0_MISO)
HT-RA62 NSS   → RPi GPIO 8  (но kernel-CS уехал на GPIO 27, см. lora-station/CLAUDE.md)
HT-RA62 RESET → RPi GPIO 22
HT-RA62 DIO1  → RPi GPIO 23
HT-RA62 BUSY  → RPi GPIO 24
```

---

## Протокол

Подробнее: [Docs/protocol.md](Docs/protocol.md)

**Структура пакета (64 байта, big-endian, CRC-16/CCITT-FALSE):**

| Поле | Байты | Описание |
|------|-------|----------|
| version | 1 | Всегда `1` |
| type | 1 | PING=0, CHAT=1, SOS=2, ACK=3 |
| device_id | 2 | uint16, ID отправителя |
| channel | 1 | 0=TOURIST (open), 1=RESCUE (whitelist) |
| ttl | 1 | По умолчанию 3, декрементируется ретранслятором |
| latitude | 4 | int32 × 1e6 (градусы × 1 000 000) |
| longitude | 4 | int32 × 1e6 |
| payload | 48 | Зависит от типа, остаток `\x00` |
| crc16 | 2 | CRC от первых 62 байт |

**Mesh (flooding):** каждый узел при получении пакета проверяет CRC,
дедуплицирует по `(type, device_id, hash(payload))` в окне 30 сек,
декрементирует TTL и ретранслирует. Эхо собственных пакетов фильтруется
по `device_id == NODE_DEVICE_ID`.

**SOS-приоритет:** ESP32 шлёт SOS бёрстом (3 пакета × 500 мс), на стороне
lora-station SOS никогда не дропается ни в TxQueue, ни в БД.

**CSMA/LBT (Meshtastic-стиль):** перед каждым TX узел проверяет канал
(CAD на ESP32, мгновенный RSSI на lora-station) и отступает с
экспоненциальным backoff'ом, если эфир занят. Pre-CAD jitter 0–400 мс
расходит синхронные передачи во времени.

---

## Сервисы

### `services/lora-station`

Python-демон, работает на каждом RPi5. Pure-Python драйвер SX1262 поверх
**`lgpio`** (libgpiod v2): GPIO + SPI одним handle. На RPi5 RP1 это самый
стабильный путь — `RPi.GPIO` несовместим, `spidev` глючит с hardware-CS.

- Принимает пакеты, дедуплицирует, ретранслирует с TTL−1
- Пишет в SQLite (`devices`, `pings`, `sos_events`, `chat_messages`)
- Outbox-poller: раз в секунду выгребает `outgoing_chat` (ответы оператора
  туристам) и шлёт CHAT-пакетом от имени NODE_DEVICE_ID
- LBT через GetRssiInst (порог −100 дБм), backoff 100–600 мс

### `services/rescue-api`

FastAPI на порту 8000. Только на базе спасателей.

```
GET  /api/health           — жив ли сервис
GET  /api/stats            — счётчики (pings/sos/devices/online)
GET  /api/tourists         — кто в эфире (PING < 2 мин)
GET  /api/devices          — все устройства (NODE_DEVICE_ID скрыт)
GET  /api/pings            — треки (фильтр device_id, hours, limit)
GET  /api/sos              — SOS-инциденты
POST /api/sos/{id}/ack     — подтверждение
POST /api/sos/{id}/resolve — закрытие
GET  /api/messages         — лента CHAT-сообщений (с JOIN на devices.name)
POST /api/messages         — ответ оператора туристам (×3 копии в outgoing_chat)
POST /api/chat             — прокси на gigachat-agent (single-origin для UI)
POST /api/admin/purge      — очистка таблиц (с подтверждением `confirm: ОЧИСТИТЬ`)
WS   /ws                   — push: ping/sos/chat
GET  /tiles/{z}/{x}/{y}.png — оффлайн-тайлы Leaflet
GET  /, /style.css, /app.js — статика дашборда (StaticFiles)
```

### `services/gigachat-agent`

FastAPI на 127.0.0.1:8001. SDK `gigachat` 0.2.0 с Authorization key
(token обновляется автоматически). Function calling, 4 инструмента
ходят в rescue-api по HTTP.

| Функция | Описание |
|---------|----------|
| `get_active_tourists()` | Кто в эфире сейчас |
| `get_sos_events()` | Активные SOS |
| `get_device_track(device_id, hours)` | Трек одного устройства |
| `get_stats()` | Общие счётчики |

### `services/relay-node` *(этап 5)*

Упрощённый lora-station для инфо-точки. Только ретрансляция, без БД и
без CHAT-outbox.

---

## Веб-интерфейсы

### `web/rescue-dashboard`

Vanilla HTML/JS + Leaflet 1.9.4, без сборщика, без npm. Раздаётся через
`StaticFiles` прямо из rescue-api на корне `/`. Тёмная тема диспетчерской.

- Сайдбар: SOS-инциденты, список туристов в эфире, админ-панель очистки БД
- Карта: маркеры туристов и SOS, оффлайн-тайлы из `/tiles`
- Правая панель: AI-диспетчер (GigaChat) сверху, чат с туристами снизу
- WebSocket auto-reconnect 2 сек

### `web/info-portal` *(этап 5)*

Captive Wi-Fi портал на инфо-точке: офлайн-карта, фото маршрута,
кнопка SOS.

### `web/tourist-web` (встроен в прошивку ESP32 как `INDEX_HTML`)

Открывается автоматически при подключении к `MeshNet-016` (HTTPS на
192.168.4.1). Берёт GPS из браузера, шлёт на ESP32:
- Кнопки SOS трёх типов (падение / медицина / заблудился)
- Поле ввода чата (до 48 байт UTF-8 ≈ 24 русских буквы)
- Inbox: входящие сообщения от базы и других туристов (polling 5 сек)

---

## База данных (SQLite WAL)

Файл: `/var/lib/mesh-net/mesh.db`. Схема: [`scripts/db_init/init.sql`](scripts/db_init/init.sql)

| Таблица | Что хранит | Очистка |
|---------|------------|---------|
| `devices` | Реестр всех увиденных устройств | через purge UI |
| `pings` | PING-пакеты с координатами | через purge UI / по дате |
| `sos_events` | SOS-события (acked/resolved) | в проде — только архив |
| `chat_messages` | Текстовые сообщения CHAT (входящие + ответы базы) | через purge UI |
| `outgoing_chat` | Очередь ответов базы → турист (sent_at IS NULL = pending) | через purge UI |

Координаты везде — `INTEGER × 1e6` (формат пакета, без конверсии).

---

## Развёртывание

Подробнее: [Docs/deployment.md](Docs/deployment.md)

На RPi5 базы спасателей три systemd-юнита (после `bash scripts/systemd/install.sh`):
```
mesh-lora-station.service    — Демон LoRa, всегда первый
mesh-rescue-api.service      — REST + WS + статика дашборда
mesh-gigachat-agent.service  — AI-диспетчер (после rescue-api)
```

ESP32 прошивается через PlatformIO: `pio run -t upload` в
`firmware/esp32-terminal/`. Self-signed TLS-сертификат генерируется
скриптом `scripts/gen_cert.sh` (DER в `include/cert.h`).
