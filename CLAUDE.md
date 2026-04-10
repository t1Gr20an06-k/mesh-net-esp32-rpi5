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
  rescue-dashboard/        — React-дашборд спасателей (Leaflet + WebSocket)
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
git clone <repo> && cd mesh-net-tropy

# База данных
bash scripts/db_init/init.sh

# Убедиться что SPI включён
ls /dev/spidev0.*   # должно быть spidev0.0
# Если нет: sudo raspi-config nonint do_spi 0 && sudo reboot

# Установить зависимости всех сервисов
cd services/lora-station   && python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt && deactivate && cd ../..
cd services/rescue-api    && python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt && deactivate && cd ../..
cd services/gigachat-agent && python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt && deactivate && cd ../..
cd web/rescue-dashboard    && npm install && npm run build && cd ../..

# Прописать переменные окружения (один раз)
sudo cp scripts/systemd/mesh-net.env /etc/mesh-net/env
sudo nano /etc/mesh-net/env   # вставить GIGACHAT_TOKEN и остальное

# Установить и запустить systemd-сервисы
sudo cp scripts/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mesh-lora-station mesh-rescue-api mesh-gigachat-agent

# Проверить что всё живо
sudo systemctl status mesh-*
curl http://localhost:8000/stats
```

После этого **сервисы стартуют автоматически** при каждом включении RPi5.

## Обновление кода (после git pull)

```bash
git pull

# Если изменились Python-зависимости:
cd services/rescue-api && source .venv/bin/activate && pip install -r requirements.txt

# Если изменился фронтенд:
cd web/rescue-dashboard && npm run build

# Перезапустить сервисы
sudo systemctl restart mesh-rescue-api mesh-gigachat-agent

# lora-station перезапускать только если менялась логика работы с железом
sudo systemctl restart mesh-lora-station
```

## Логи в реальном времени

```bash
# Все сервисы сразу
sudo journalctl -u "mesh-*" -f

# Конкретный сервис
sudo journalctl -u mesh-lora-station -f

# Только ошибки
sudo journalctl -u "mesh-*" -p err -f
```

---

## Переменные окружения

| Переменная | Где используется | Описание |
|---|---|---|
| `GIGACHAT_TOKEN` | gigachat-agent | OAuth2 токен GigaChat API |
| `DB_PATH` | rescue-api, gigachat-agent | Путь к SQLite (default: `/var/lib/mesh-net/mesh.db`) |
| `LORA_SPI_BUS` | lora-station | SPI шина (default: `0`) |
| `LORA_SPI_CS` | lora-station | CS пин (default: `0`) |
| `LORA_RESET_PIN` | lora-station | GPIO reset (default: `22`) |
| `LORA_DIO1_PIN` | lora-station | GPIO DIO1/IRQ (default: `23`) |
| `RESCUE_WHITELIST` | lora-station | Comma-separated device_id для RESCUE-канала |

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

### Этап 0.2 — Схема БД ⬅ СЛЕДУЮЩИЙ ШАГ
Файлы: `scripts/db_init/init.sql`, `scripts/db_init/init.sh`
- Таблицы: `devices`, `pings`, `sos_events`, `chat_messages`
- Скрипт инициализации для RPi5

### Этап 1 — Прошивка ESP32-S3
Файлы: `firmware/esp32-terminal/src/`
- Инициализация LoRa HT-RA62 (SX1262) через SPI
- Отправка PING-пакета каждые N секунд с GPS-координатами
- SOS по нажатию кнопки (3 повтора × 500 мс)
- Приём ACK и CHAT-сообщений

### Этап 2 — lora-station (RPi5)
Файлы: `services/lora-station/`
- Python-демон: приём пакетов через SPI (pigpio / spidev)
- Парсинг пакета → запись в SQLite
- Ретрансляция: forwarding с TTL-1

### Этап 3 — relay-node + info-portal
Файлы: `services/relay-node/`, `web/info-portal/`
- Ретранслятор на промежуточном узле (инфо-точке)
- Captive Wi-Fi портал: офлайн HTML со статичной информацией о маршруте + карта

### Этап 4 — rescue-api + rescue-dashboard + gigachat-agent
Файлы: `services/rescue-api/`, `web/rescue-dashboard/`, `services/gigachat-agent/`
- REST API + WebSocket для дашборда
- React-дашборд: карта Leaflet, SOS-алерты, список устройств
- GigaChat function calling с инструментами get_tourists / get_sos / get_location / get_stats

### Этап 5 — Интеграция и деплой
Файлы: `tests/`, `scripts/systemd/`
- Полевые тесты дальности LoRa
- systemd-юниты для автозапуска
- Проверка критических сценариев

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
| `firmware/esp32-terminal/platformio.ini` | ✅ RadioLib + пин-макросы + monitor_speed |
| `firmware/esp32-terminal/src/main.cpp` | ⬜ Заглушка — нужно писать (Этап 1) |
| `scripts/db_init/init.sql` | ⬜ Не создан (Этап 0.2 — следующий) |
| Python-сервисы (lora-station демон, rescue-api) | ⬜ Не написаны |
| React-дашборд | ⬜ Не написан |

---

## Критические сценарии (всегда тестировать)

1. **SOS-пакет** доставляется до базы спасателей через 2 ретранслятора за < 5 сек
2. **Потеря узла** — mesh продолжает работу, пакеты идут в обход
3. **Переполнение очереди** — старые PING-пакеты дропаются, SOS — нет
4. **Отключение питания RPi** — при рестарте БД консистентна, сервисы поднимаются автоматически (systemd)
5. **Wi-Fi captive portal** открывается на iPhone и Android без дополнительных настроек
