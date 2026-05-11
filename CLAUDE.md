# CLAUDE.md — MESH-NET ТРОПЫ

## Обзор проекта

Автономная система безопасности, навигации и связи для горных туристических маршрутов **без сотовой связи и интернета**.

Стек: ESP32-S3 N16R8 + LoRa SX1262 HT-RA62 (носимые терминалы) → LoRa mesh 868 МГц → Raspberry Pi 5 8GB (инфо-точки + база спасателей) + GigaChat AI.

```
Турист (ESP32-S3)  →  [LoRa mesh]  →  Инфо-точка (RPi5)  →  [LoRa]  →  База спасателей (RPi5)
                                            ↑ Wi-Fi captive portal                ↑ GigaChat AI + карта
```

---

## Структура репозитория

```
firmware/esp32-terminal/   — PlatformIO проект для ESP32-S3 N16R8
proto/                     — Protobuf-схема пакетов LoRa
scripts/                   — Утилиты: импорт тайлов, инит БД, тест пакетов
services/
  gigachat-agent/          — FastAPI-сервис: GigaChat + function calling → SQLite
  lora-station/            — Python-демон приёма/передачи LoRa (pigpio/spidev)
  relay-node/              — Сервис ретрансляции пакетов на инфо-точке
  rescue-api/             — REST API базы спасателей (треки, SOS, устройства)
tests/
  field/                   — Полевые тесты дальности и надёжности
  integration/             — Интеграционные тесты mesh-сети
web/
  info-portal/             — Captive Wi-Fi портал (offline HTML/JS, карты, контент)
  rescue-dashboard/        — Дашборд спасателей: vanilla HTML/JS + Leaflet + WebSocket (без сборщика)
  tourist-web/             — Мобильный веб-интерфейс туриста (GPS телефона → WebSocket → ESP32 → LoRa)
Docs/                      — Техническая документация
```

---

## Ключевые константы протокола

```python
LORA_FREQ       = 868_000_000   # Гц, ISM-диапазон
LORA_SF         = 10            # Spreading Factor
LORA_BW         = 125_000       # Bandwidth Гц
LORA_CR         = 5             # Coding Rate 4/5
PACKET_SIZE     = 64            # байт (фиксированный)
TTL_DEFAULT     = 3
CHANNEL_TOURIST = 0
CHANNEL_RESCUE  = 1
SOS_REPEAT      = 3             # SOS-пакет повторяется 3 раза
SOS_INTERVAL_MS = 500
```

Структура пакета — см. `proto/messages.proto` и `Docs/protocol.md`.

---

## Распиновка HT-RA62 (SX1262) → ESP32-S3

Назначены дефолтные пины (пользователь подключает железо по этой схеме):

| Сигнал | GPIO ESP32-S3 | Описание |
|--------|--------------|----------|
| MOSI   | 11           | SPI данные к модулю |
| MISO   | 13           | SPI данные от модуля |
| SCK    | 12           | SPI тактовый |
| CS     | 10           | Выбор чипа (активный LOW) |
| RESET  | 9            | Сброс модуля |
| DIO1   | 14           | Прерывание (IRQ) |
| BUSY   | 8            | Флаг занятости модуля |

Все пины прописаны в `firmware/esp32-terminal/platformio.ini` как `-D LORA_*` макросы.
Библиотека: `jgromes/RadioLib @ ^6.6.0`.

---

## Правила разработки

### Общие
- **Язык кода**: Python 3.11+ для сервисов RPi, C++17 для прошивки ESP32
- **Все логи** через `structlog` (Python) или `ESP_LOG` (ESP-IDF), уровень INFO в продакшне
- **Нет внешних зависимостей в runtime** — система полностью оффлайн
- При добавлении зависимости — обосновать в PR и проверить на совместимость с RPi5 ARM64

### ⚠ Обязательное правило: context7 для любой библиотеки
**Всегда** использовать MCP-сервер `context7` при работе с любой внешней библиотекой, фреймворком, SDK, API или CLI-утилитой (RadioLib, FastAPI, Leaflet, React, pigpio, spidev, GigaChat SDK, Protobuf и т.д.).

Зачем: база знаний модели устарела — API библиотек меняются, появляются новые версии, ломаются сигнатуры функций. `context7` даёт **актуальную документацию** на момент запроса, а не «как было полгода назад».

Порядок:
1. `mcp__context7__resolve-library-id` — найти ID библиотеки
2. `mcp__context7__query-docs` — получить свежий фрагмент документации
3. Только после этого писать код, использующий её API

Не использовать `context7` можно только для: рефакторинга, работы с бизнес-логикой, объяснения общих концепций программирования.

### Python-сервисы
- Виртуальное окружение: `python -m venv .venv && source .venv/bin/activate`
- Зависимости фиксировать в `requirements.txt` с точными версиями
- Запуск через `uvicorn` (FastAPI) или `python -m service`
- Тесты: `pytest tests/` — обязательно перед коммитом

### Firmware (ESP32)
- Сборка: `pio run` в папке `firmware/esp32-terminal/`
- Прошивка: `pio run -t upload`
- Мониторинг: `pio device monitor -b 115200`
- Изменения в `lib/` требуют ревью на предмет RAM-бюджета (ESP32-S3 N16R8: 512 КБ SRAM + 8 МБ PSRAM)

### База данных
- SQLite, файл: `/var/lib/mesh-net/mesh.db`
- Схема: `scripts/db_init/init.sql`
- Миграции: нумерованные файлы `scripts/db_init/migrate_NNN.sql`
- Никогда не удалять данные треков — только архивировать

---

## Первый запуск на RPi5

Выполняется один раз после клонирования репозитория.

```bash
git clone <repo> && cd "Mesh-net тропы"

# 1. Убедиться что SPI включён (для lora-station)
ls /dev/spidev0.*   # должно быть /dev/spidev0.0
# Если нет: sudo raspi-config nonint do_spi 0 && sudo reboot

# 2. lora-station — apt-пакеты (lgpio через apt, не pip!) + venv + init БД
cd services/lora-station && bash install.sh && cd ../..

# 3. rescue-api — venv + Leaflet для дашборда (вызывает web/rescue-dashboard/install.sh)
cd services/rescue-api && bash install.sh && cd ../..

# 4. gigachat-agent — venv + GigaChat SDK
#    Положить Authorization key в services/gigachat-agent/token-key
#    (формат — см. services/gigachat-agent/CLAUDE.md). Без ключа сервис стартует,
#    но чат ответит «AI недоступен (ключ не задан)».
cd services/gigachat-agent && bash install.sh && cd ../..

# 5. (опционально) Оффлайн-тайлы карты для дашборда
#    Без этого шага карта будет серой, маркеры всё равно рисуются.
#    Краснодар, zoom 5-14 (~1500 тайлов, ~25 мин):
python3 scripts/import_tiles/download_tiles.py \
    --bbox 38.7,44.8,39.4,45.3 --zoom 5-14

# 6. systemd-юниты (autostart при включении RPi5)
sudo bash scripts/systemd/install.sh
# Скрипт сам подставит __USER__/__REPO__, проверит venv, группы gpio/spi,
# не запущен ли lora-station руками — и активирует только то что готово.

# 7. Проверить что всё живо
sudo systemctl status mesh-lora-station mesh-rescue-api mesh-gigachat-agent
curl http://localhost:8000/api/health   # {"status":"ok"}
curl http://localhost:8000/api/stats    # счётчики из БД
curl http://127.0.0.1:8001/health       # gigachat-agent (только локально)
# Открыть в браузере: http://<rpi5-ip>:8000  — дашборд спасателей с чатом
```

После этого **сервисы стартуют автоматически** при каждом включении RPi5.

## Обновление кода (после git pull)

```bash
git pull

# Если изменились Python-зависимости — для каждого тронутого сервиса:
cd services/rescue-api    && source .venv/bin/activate && pip install -r requirements.txt && deactivate && cd ../..
cd services/gigachat-agent && source .venv/bin/activate && pip install -r requirements.txt && deactivate && cd ../..

# Перезапустить сервисы
sudo systemctl restart mesh-rescue-api mesh-gigachat-agent
# lora-station — только при правках железной логики
# sudo systemctl restart mesh-lora-station

# Web-фронтенд (vanilla HTML/JS) — раздаётся StaticFiles, перезапуск не нужен,
# достаточно F5 в браузере. Если изменился install.sh дашборда (новая версия
# Leaflet) — перезапусти один раз: bash web/rescue-dashboard/install.sh
```

## Управление systemd-сервисами

Юниты — `mesh-lora-station.service`, `mesh-rescue-api.service`,
`mesh-gigachat-agent.service`. Шаблоны лежат в `scripts/systemd/`,
подставляет их `scripts/systemd/install.sh` (плейсхолдеры `__USER__`/`__REPO__`).

```bash
# Статус
sudo systemctl status mesh-lora-station
sudo systemctl status mesh-rescue-api
sudo systemctl status mesh-gigachat-agent

# Перезапуск (после изменения кода или ENV в юните)
sudo systemctl restart mesh-rescue-api

# Остановить / запустить
sudo systemctl stop mesh-lora-station
sudo systemctl start mesh-lora-station

# Выключить автозапуск (но не трогать текущий процесс)
sudo systemctl disable mesh-rescue-api

# Полностью отключить и остановить
sudo systemctl disable --now mesh-lora-station

# Переустановить юнит после правки шаблона в scripts/systemd/
sudo bash scripts/systemd/install.sh mesh-rescue-api    # один сервис
sudo bash scripts/systemd/install.sh                    # все сразу
```

Если правишь ENV прямо в `/etc/systemd/system/mesh-*.service` (например,
поменять `LOG_LEVEL` или `RESCUE_API_PORT`):

```bash
sudo systemctl edit --full mesh-rescue-api    # откроет в редакторе
sudo systemctl daemon-reload
sudo systemctl restart mesh-rescue-api
```

## Логи в реальном времени

```bash
# Все сервисы сразу
sudo journalctl -u "mesh-*" -f

# Конкретный сервис
sudo journalctl -u mesh-lora-station -f
sudo journalctl -u mesh-rescue-api -f

# Только ошибки
sudo journalctl -u "mesh-*" -p err -f

# За последние 10 минут
sudo journalctl -u mesh-lora-station --since "10 min ago"

# Последние 200 строк без follow
sudo journalctl -u mesh-rescue-api -n 200 --no-pager
```

---

## Переменные окружения

| Переменная | Где используется | Описание |
|---|---|---|
| `GIGACHAT_AUTHORIZATION_KEY` | gigachat-agent | Authorization key (base64), перетирает token-key. Рекомендуемый способ для systemd-overrides |
| `GIGACHAT_SCOPE` | gigachat-agent | `GIGACHAT_API_PERS` (default) / `_B2B` / `_CORP` |
| `GIGACHAT_MODEL` | gigachat-agent | `GigaChat` (default) / `GigaChat-Pro` / `GigaChat-Plus` |
| `RESCUE_API_URL` | gigachat-agent | URL rescue-api для инструментов (default: `http://127.0.0.1:8000`) |
| `GIGACHAT_AGENT_URL` | rescue-api | URL gigachat-agent для прокси `/api/chat` (default: `http://127.0.0.1:8001`) |
| `DB_PATH` | rescue-api, lora-station | Путь к SQLite (default: `/var/lib/mesh-net/mesh.db`) |
| `LORA_SPI_BUS` | lora-station | SPI шина (default: `0`) |
| `LORA_SPI_CS` | lora-station | CS пин (default: `8`) |
| `LORA_RESET_PIN` | lora-station | GPIO reset (default: `22`) |
| `LORA_DIO1_PIN` | lora-station | GPIO DIO1/IRQ (default: `23`) |
| `NODE_DEVICE_ID` | lora-station, rescue-api | ID базы для CHAT-пакетов (default: `0x0001`) |
| `BASE_DEVICE_NAME` | rescue-api | Имя базы для записи в `devices` при первом ответе оператора (default: `База спасателей`) |

---

## План разработки (поэтапный)

Проект собирается «пазл по кусочкам» — каждый этап даёт рабочий результат.

### Этап 0.1 — Протокол пакета ✅ ВЫПОЛНЕН
Файлы: `proto/messages.proto`, `firmware/esp32-terminal/lib/mesh_packet/`, `services/lora-station/mesh_packet.py`
- ✅ `proto/messages.proto` — документация 64-байтного формата + proto3 схема (не кодируется protobuf на проводе!)
- ✅ `lib/mesh_packet/MeshPacket.h` + `MeshPacket.cpp` — C++ кодек: CRC-16/CCITT-FALSE (poly=0x1021, init=0xFFFF, big-endian), encode/decode, все 4 make_*_payload()
- ✅ `services/lora-station/mesh_packet.py` — Python кодек: dataclass MeshPacket, struct.pack/unpack ('>BBHBBii48s'), CRC-валидация, make_*_payload(), lat_lon_to_int/from_int
- ✅ `platformio.ini` — RadioLib @ ^6.6.0, monitor_speed=115200, все 8 пин-макросов
- ✅ Тест Python кодека прошёл: 64 байт, lat/lon roundtrip, CRC=0x3784

### Этап 0.2 — Схема БД ✅ ВЫПОЛНЕН
Файлы: `scripts/db_init/init.sql`, `scripts/db_init/init.sh`
- ✅ `init.sql` — 4 таблицы (devices, pings, sos_events, chat_messages), WAL-режим, индексы, FK
- ✅ `init.sh` — скрипт инициализации для RPi5: создаёт /var/lib/mesh-net/, применяет схему, проверяет результат
- Координаты хранятся как int32 × 1e6 (без конвертации из формата пакета)
- SOS-записи никогда не удаляются — поля acked/resolved для отслеживания реакции

### Этап 1 — Прошивка ESP32-S3 ✅ ВЫПОЛНЕН
Файлы: `firmware/esp32-terminal/src/main.cpp`, `firmware/esp32-terminal/include/cert.h`, `firmware/esp32-terminal/scripts/gen_cert.sh`, `firmware/esp32-terminal/scripts/patch_esp32_https_server.py`
- ✅ Инициализация LoRa HT-RA62 (SX1262) через SPI с TCXO 1.8 В
- ✅ Радио-режим: 868 МГц, SF=10, BW=125 кГц, CR=4/5, TX power 14 дБм
- ✅ PING-пакет каждые N секунд с актуальными GPS-координатами от телефона
- ✅ SOS-бёрст по запросу с веб-страницы: 3 пакета × 500 мс
- ✅ Dual-core архитектура: Core 0 = Wi-Fi/HTTPS-сервер, Core 1 = LoRa main-loop
- ✅ Wi-Fi точка доступа `MeshNet-001` на 192.168.4.1
- ✅ HTTPS-сервер на 443 (self-signed cert, CN=192.168.4.1) — нужен для `navigator.geolocation`, который требует secure context
- ✅ Веб-интерфейс туриста: `GET /`, `GET /api/status`, `POST /api/gps`, `POST /api/sos`
- ✅ Pre-build patch для `fhessel/esp32_https_server` v1.0.0 (замена удалённого `<hwcrypto/sha.h>` на mbedtls)
- ✅ Скрипт `gen_cert.sh` для генерации DER-сертификата (RSA-2048, 10 лет)
- ✅ Атомарный `[TX]` лог с координатами (`@ 45.019741, 39.032218`), без межъядерного interleave-а
- ⬜ Приём ACK и CHAT-сообщений (отложено до этапа 2 — нужна вторая сторона)

Бюджет ресурсов после сборки: RAM 16.3 % (53 КБ из 327 КБ), Flash 17.1 % (1.1 МБ из 6.5 МБ).

### Этап 2 — lora-station (RPi5) ✅ ВЫПОЛНЕН
Файлы: `services/lora-station/lora_station/`
- ✅ Python-демон, pure-Python драйвер SX1262 поверх `lgpio` (libgpiod v2): GPIO + SPI одним handle через `lgpio.spi_*`. На RPi5 RP1 это самый стабильный путь: `RPi.GPIO` несовместим, `spidev` глючит с hardware-CS
- ✅ `packet.py` — кодек 64-байтного пакета (перенесён из `services/lora-station/mesh_packet.py`)
- ✅ `sx1262.py` — драйвер: reset, init (TCXO 1.8 В, DCDC, 868 МГц, SF10/BW125/CR4/5, sync PRIVATE, DIO2=RF switch), startReceive, readData (RSSI/SNR), transmit, IRQ через DIO1 + SPI-fallback
- ✅ `db.py` — SQLite-обёртка: upsert `devices`, insert `pings` / `sos_events` / `chat_messages`, потокобезопасно через RLock
- ✅ `mesh.py` — DedupCache (TTL 30 с), TxQueue (приоритеты SOS=0 / ACK=1 / CHAT=2 / PING=3, SOS не дропается), make_forward (TTL−1)
- ✅ `dispatcher.py` — decoded packet → БД + ретрансляция, эхо своих пакетов фильтруется по `NODE_DEVICE_ID`
- ✅ `__main__.py` — argparse, главный цикл (wait_rx → read_rx → handle → tx_q.pop → transmit → start_receive), graceful shutdown по SIGINT/SIGTERM
- ✅ `install.sh` + `requirements.txt` — apt-пакеты + venv с `--system-site-packages` (lgpio через apt, не pip), проверка SPI, проверка групп gpio/spi, инициализация БД
- ✅ Полевой тест пройден: SOS (3 пакета) и PING долетают от ESP32 до RPi5 за ~1 сек, RSSI=-41 дБм, SNR=8-9 дБ, CRC=0 ошибок (см. `firmware/esp32-terminal/logs`)
- ⚠ Костыль: на этой плате `dtoverlay=spi0-1cs,cs0_pin=27` отправил kernel-CS на GPIO 27, поэтому CS на HT-RA62 (GPIO 8) дёргаем сами через `lgpio.gpio_write` в `_spi_xfer` — подробнее в `services/lora-station/CLAUDE.md`

### Этап 3 — rescue-api + rescue-dashboard ✅ ВЫПОЛНЕН
Файлы: `services/rescue-api/`, `web/rescue-dashboard/`, `scripts/import_tiles/`, `scripts/systemd/`
- ✅ `rescue-api` — FastAPI + WebSocket, REST `/api/{tourists,sos,devices,pings,stats,health}`, WS `/ws` (push при новых PING/SOS)
- ✅ Read-only SQLite (`?mode=ro`) для GET-эндпоинтов, read-write только для ack/resolve
- ✅ Дашборд `web/rescue-dashboard/` — vanilla HTML/JS + Leaflet 1.9.4 (без сборщика, без npm), тёмная тема
- ✅ Карта офлайн: тайлы скачиваются `scripts/import_tiles/download_tiles.py` в `/var/lib/mesh-net/tiles/`, rescue-api монтирует на `/tiles`
- ✅ Маркеры: туристы (синие, blue marker), SOS (красные → оранжевые при ack → зелёные при resolve), популяр с battery/RSSI/timestamp
- ✅ Фильтр `(0, 0)` от ESP32 без GPS-фикса — на карте не рисуем, в списке выводим серым
- ✅ WS auto-reconnect раз в 2 сек после `onclose`
- ✅ systemd-юниты `mesh-lora-station.service` + `mesh-rescue-api.service` + `scripts/systemd/install.sh` (подставляет __USER__/__REPO__, проверяет venv и группы gpio/spi)

### Этап 4 — gigachat-agent + чат в дашборде ✅ ВЫПОЛНЕН
Файлы: `services/gigachat-agent/`, правки в `web/rescue-dashboard/` и `services/rescue-api/`
- ✅ FastAPI-сервис на 127.0.0.1:8001 на той же RPi5 (наружу не светится)
- ✅ Auth: SDK `gigachat` 0.2.0 с Authorization key из `token-key` — токен обновляется автоматически
- ✅ Function calling: 8 инструментов — `get_active_tourists`, `get_all_devices`, `get_sos_events` (с фильтрами `only_open` / `device_id` / `hours` / `sos_type`), `get_sos_details`, `get_device_track`, `get_chat_history`, `find_device`, `get_stats` (расширенный: 24ч, разбивка по типам, топ-3 по pings) — все через HTTP к rescue-api
- ✅ Прокси `POST /api/chat` в rescue-api — дашборд ходит через single-origin без CORS
- ✅ Чат-панель в дашборде: пузыри user/assistant, индикатор «AI думает…», история сохраняется в `localStorage` (F5 не стирает переписку), кнопка ✕ с `confirm()` чистит и память, и localStorage
- ✅ Обработка ошибок: модель/сеть/rescue-api/таймаут — всё возвращается как `error` в ответе с человекочитаемой причиной, дашборд красит красным
- ✅ systemd-юнит `mesh-gigachat-agent.service` (After mesh-rescue-api)

### Этап 4.5 — Двусторонний чат турист↔база ✅ ВЫПОЛНЕН (с известным ограничением, см. ниже)
Файлы: правки в `firmware/esp32-terminal/`, `services/lora-station/`, `services/rescue-api/`, `web/rescue-dashboard/`
- ✅ ESP32 → база: textarea на странице терминала, POST `/api/chat` → CHAT-пакет в эфир (бёрст ×3 с ~2 сек интервалом)
- ✅ База → ESP32: tour-chat в дашборде (нижняя секция), POST `/api/messages` → таблица `outgoing_chat` → outbox-poller lora-station выгребает по 1 в сек → LoRa
- ✅ ESP32 RX: непрерывный приём через `setPacketReceivedAction` + `startReceive`, входящие CHAT в кольцевой inbox (8 сообщений), GET `/api/inbox?since=N`
- ✅ Веб-страница ESP32: блок «Сообщения от базы», polling раз в 5 сек, золотой акцент для пакетов от device_id=BASE
- ✅ CSMA/LBT: ESP32 — `radio.scanChannel()` с pre-CAD jitter и экспоненциальным backoff (Meshtastic-стиль). Lora-station — мгновенный RSSI с тем же подходом
- ✅ Дедуп ретрансляций: `(type, device_id, hash(payload))` в окне 30 сек — на обеих сторонах
- ✅ Усиление декодера: после CRC проверяются `version=1`, `type∈[0..3]`, `channel∈[0..1]`, `ttl∈[1..8]` — отсекает мусорные пакеты со случайным CRC-совпадением
- ✅ Админ-эндпоинт `POST /api/admin/purge` + UI с чекбоксами для очистки БД (`pings`/`sos_events`/`chat_messages`/`outgoing_chat`/`devices`)

### Этап 5 — relay-node + info-portal
Файлы: `services/relay-node/`, `web/info-portal/`
- Ретранслятор на промежуточном узле (инфо-точке) — отдельная RPi5 в горах
- Captive Wi-Fi портал: офлайн HTML со статичной информацией о маршруте + карта
- Полевые тесты дальности LoRa, проверка критических сценариев

---

## Текущий статус разработки

| Что | Статус |
|-----|--------|
| Структура репозитория | ✅ Готово |
| CLAUDE.md файлы всех сервисов | ✅ Готово |
| `.gitignore` с защитой токенов | ✅ Готово |
| Распиновка HT-RA62 → ESP32-S3 | ✅ Назначены дефолтные пины (GPIO 8-14) |
| `proto/messages.proto` | ✅ Заполнен (документация 64-байтного формата) |
| `firmware/esp32-terminal/lib/mesh_packet/` | ✅ C++ кодек (MeshPacket.h + MeshPacket.cpp) |
| `services/lora-station/mesh_packet.py` | ✅ Python кодек, тест пройден |
| `firmware/esp32-terminal/platformio.ini` | ✅ RadioLib + пин-макросы + HTTPS lib + pre-build patch |
| `firmware/esp32-terminal/src/main.cpp` | ✅ LoRa + Wi-Fi AP + HTTPS + PING/SOS работает на железе |
| `firmware/esp32-terminal/include/cert.h` | ✅ Self-signed TLS cert (DER, через `gen_cert.sh`) |
| `firmware/esp32-terminal/scripts/` | ✅ `gen_cert.sh` + `patch_esp32_https_server.py` |
| `scripts/db_init/init.sql` + `init.sh` | ✅ 4 таблицы + скрипт инициализации |
| `services/lora-station/` (демон) | ✅ Полевой тест пройден на железе (SOS + PING от ESP32 → RPi5 → SQLite) |
| `services/rescue-api/` | ✅ FastAPI + WS, оффлайн-тайлы карты на `/tiles`, статика дашборда на `/` |
| `web/rescue-dashboard/` | ✅ Vanilla HTML/JS + Leaflet, маркеры/SOS/WS-reconnect — работает на железе |
| `scripts/import_tiles/download_tiles.py` | ✅ Стандартный stdlib, идемпотентный, rate-limit 1 req/сек |
| systemd: `mesh-lora-station`, `mesh-rescue-api`, `mesh-gigachat-agent` | ✅ Шаблоны + `scripts/systemd/install.sh` (autostart на RPi5) |
| `services/gigachat-agent/` + чат-панель в дашборде | ✅ GigaChat function calling (8 инструментов), прокси `/api/chat`, UI с историей в localStorage |
| Чат турист↔база (CHAT-пакеты, inbox, outgoing_chat, CSMA/LBT) | ✅ Двусторонний (этап 4.5), есть известное ограничение при синхронной TX — см. ниже |
| Усиление декодера (sanity-проверки полей) | ✅ Отсекает мусор со случайным CRC-совпадением |
| Очистка БД через UI (purge) | ✅ Чекбоксы в дашборде, эндпоинт `POST /api/admin/purge` |
| `services/relay-node/`, `web/info-portal/` | ⬜ Этап 5 |
| Полевые тесты дальности | ⬜ Этап 5 |

---

## Критические сценарии (всегда тестировать)

1. **SOS-пакет** доставляется до базы спасателей через 2 ретранслятора за < 5 сек
2. **Потеря узла** — mesh продолжает работу, пакеты идут в обход
3. **Переполнение очереди** — старые PING-пакеты дропаются, SOS — нет
4. **Отключение питания RPi** — при рестарте БД консистентна, сервисы поднимаются автоматически (systemd)
5. **Wi-Fi captive portal** открывается на iPhone и Android без дополнительных настроек

---

## ACK-протокол v2 (гарантированная доставка)

С v2 пакет 82 байта (было 64), payload 64 (было 48), добавлены:
- `packet_id` (uint16) — монотонный счётчик исходящих на каждом узле
- `flags` (uint8) — `WANT_ACK`, `IS_ACK`, channel переехал в биты 2-3
  (поэтому отдельного байта `channel` больше нет — экономия)

**Логика:** CHAT и SOS шлются с `want_ack=1`. Конечный получатель (база)
после обработки шлёт ACK-пакет с `is_ack=1` и `ack_for_packet_id` в
payload. Отправитель ловит ACK → снимает запись из pending. Если ACK
не пришёл за `ACK_TIMEOUT` (4 сек по умолчанию) — retry с тем же
`packet_id` (приёмник дедупит). До `MAX_RETRIES=3`, итого 4 попытки.

**Что заменило старое:**
- ESP32 раньше слал CHAT 3 копии бёрстом → теперь 1 копию + retry по таймауту
- rescue-api писал в `outgoing_chat` 3 копии → теперь 1, lora-station сам ретраит
- Дедуп на приёмнике по `hash(payload)` остался — нужен на retry-копии при
  потерянном ACK

**Где смотреть код:**
- `proto/messages.proto` — формат
- `firmware/esp32-terminal/lib/mesh_packet/MeshPacket.{h,cpp}` — C++ кодек
- `services/lora-station/lora_station/packet.py` — Python кодек (есть `__main__` self-test)
- `firmware/esp32-terminal/src/main.cpp` — `g_pending[PENDING_SIZE]`,
  `pending_alloc`/`pending_release`, `check_pending_retries`, `send_ack`
- `services/lora-station/lora_station/dispatcher.py` — `_send_ack`, обработка `is_ack`
- `services/lora-station/lora_station/__main__.py` — outbox-poller с retry/fail
- `services/rescue-api/rescue_api/ws.py` — push `event: chat_status`
  при изменении `delivery_status`
- `web/rescue-dashboard/app.js` — `updateTourMsgStatus`, значки ⏳/✅/❌

**Параметры retry — синхронны между узлами:**
- ESP32 (`main.cpp`): `ACK_TIMEOUT_MS=4000`, `MAX_RETRIES=3`,
  расписание 4s → 6s → 9s → 13.5s
- lora-station (`__main__.py`): те же значения

Если меняешь — поправь в обоих местах, иначе одна сторона признает CHAT
failed раньше другой.

---

## Известные ограничения

### Collision при синхронной отправке CHAT с обеих сторон ✅ РЕШЕНО (v2)

С введением ACK-протокола (см. раздел выше) синхронные нажатия больше не
теряют сообщения навсегда: при collision первая копия пропадает, retry
через 4 сек идёт в другое временное окно — пакет долетает. Видим в логах
`[CHAT] ⟳ retry pkt=N` и потом `[ACK] ✓ pkt=N`.

Что осталось из v1 как mitigation (всё ещё полезно, чтобы retry
понадобился реже):
- **CAD перед TX** на ESP32 (`radio.scanChannel()`) и мгновенный **RSSI**
  на lora-station — отступаем, если кто-то уже передаёт.
- **Pre-CAD jitter** 0–400 мс случайной задержки **до** CAD — узлы не
  входят в проверку синхронно.
- **Экспоненциальный backoff** при «занято»: contention window 250→2000 мс.

Дедуп `hash(payload)` в окне 30 сек по-прежнему нужен — на retry-копию
после потерянного ACK не должен задвоиться `chat_messages` в БД.

**Что осталось как минус half-duplex:** SOS-бёрст (3 копии × 500 мс) не
имеет retry — он по-прежнему «параноидальный режим». Каждая SOS-копия
имеет свой `packet_id` и шлёт ACK; если хотя бы одна из 3 копий и одного
ACK дойдёт — SOS считается полученным оператором (через дашборд).
