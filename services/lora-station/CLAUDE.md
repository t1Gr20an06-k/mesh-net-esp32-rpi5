# CLAUDE.md — lora-station

Python-демон для работы с LoRa SX1262 (модуль HT-RA62) на Raspberry Pi 5.
Принимает 64-байтные пакеты Mesh-net Тропы, дедуплицирует, ретранслирует
(TTL−1) и пишет в SQLite. Работает на RPi5 базы спасателей; на инфо-точке
может использоваться тот же код с другим `NODE_DEVICE_ID`.

## Стек

- Python 3.11
- `spidev` — SPI обмен с чипом (`/dev/spidev0.0`)
- `gpiozero` — GPIO для RESET / BUSY / DIO1 (на RPi5 Bookworm gpiozero
  использует backend `lgpio`; `RPi.GPIO` несовместим с RP1-чипом RPi5)
- Стандартный `sqlite3` — без ORM
- Стандартный `logging` — пока без structlog

## Структура

```
services/lora-station/
├── lora_station/
│   ├── __init__.py
│   ├── __main__.py     — точка входа: argparse, главный цикл, SIGINT/SIGTERM
│   ├── packet.py       — кодек 64-байтного пакета (CRC-16/CCITT-FALSE)
│   ├── sx1262.py       — драйвер SX1262: init, RX, TX, IRQ через gpiozero
│   ├── db.py           — SQLite-обёртка (upsert device, insert ping/sos/chat)
│   ├── mesh.py         — DedupCache, TxQueue (приоритеты), make_forward
│   └── dispatcher.py   — decoded packet → БД + ретрансляция
├── requirements.txt    — spidev, gpiozero, lgpio
└── install.sh          — установка на RPi5 (venv, pip, init БД, проверка SPI)
```

## Запуск

```bash
# Один раз после клонирования репо
bash install.sh

# Дальше
source .venv/bin/activate
python -m lora_station

# Подробные логи (включая дубликаты)
python -m lora_station --verbose

# Альтернативный путь к БД
python -m lora_station --db /custom/path/mesh.db
```

## Параметры радио (должны совпадать на ВСЕХ узлах!)

```
freq           = 868.0 МГц
SF             = 10
BW             = 125 кГц
CR             = 4/5
preamble       = 8 символов
TX power       = 14 дБм
TCXO voltage   = 1.8 В
sync word      = PRIVATE (0x14 / 0x24)
LoRa CRC       = on
DIO2           = RF switch
header type    = explicit (variable)
```

Эти значения захардкожены в `sx1262.py` `begin()`. Изменение требует
синхронной правки прошивки ESP32 (`firmware/esp32-terminal/src/main.cpp`)
и C++ снифера (`tests/field/lora-sniffer/main.cpp`).

## Архитектура потоков

- **Главный поток**: `python -m lora_station`. Цикл `wait_rx → read_rx →
  dispatcher.handle → tx_q.pop → radio.transmit → start_receive`. Все SPI-
  обращения к чипу идут отсюда.
- **Поток gpiozero (DIO1 IRQ)**: при rising edge на DIO1 ставит
  `threading.Event`. SPI здесь НЕ трогаем — иначе race на шине.
- **DB**: `sqlite3.connect(check_same_thread=False)` + `RLock` на запросы.
  Сейчас всё пишется из главного потока, но Lock оставлен на будущее
  (HTTP-эндпоинт от rescue-api).

## Важные правила

- **SOS никогда не дропается** — ни в `TxQueue` (приоритет 0), ни в БД
- **Дедупликация**: ключ `(type, device_id, seq)` для PING; для остальных —
  `(type, device_id, секунды)`. TTL кеша = 30 с
- **Эхо собственных пакетов**: если `pkt.device_id == NODE_DEVICE_ID` —
  игнорируем (мы только что сами это передавали)
- **TTL−1**: `make_forward` создаёт новый пакет с уменьшенным TTL; если
  было 1 — не ретранслируем
- **CRC-ошибки**: считаются отдельно (`crc_bad`), в БД не пишутся
- **Ничего не пишем в `pings.receiver_rssi`** для SOS — в схеме БД
  колонка `receiver_rssi` есть только в `pings`, в `sos_events` — нет.
  При желании добавить — миграция через `scripts/db_init/migrate_NNN.sql`

## Переменные окружения

| Переменная | Default | Описание |
|------------|---------|----------|
| `DB_PATH` | `/var/lib/mesh-net/mesh.db` | путь к SQLite |
| `LORA_SPI_BUS` | `0` | SPI шина (`/dev/spidev0.X`) |
| `LORA_SPI_CS` | `8` | BCM GPIO для CS (по дефолту = SPI0 CE0) |
| `LORA_RESET_PIN` | `22` | BCM GPIO для NRST |
| `LORA_DIO1_PIN` | `23` | BCM GPIO для DIO1 (IRQ) |
| `LORA_BUSY_PIN` | `24` | BCM GPIO для BUSY |
| `NODE_DEVICE_ID` | `0x0001` | ID этого узла. База спасателей = 0x0001, инфо-точка = например 0x0100 |

## Проверка работоспособности

```bash
# 1. SPI и GPIO группы
ls /dev/spidev0.*           # должен быть /dev/spidev0.0
groups                      # должны быть gpio и spi

# 2. Запуск с verbose
python -m lora_station -v

# Ожидаемый вывод (если ESP32 рядом и пингует):
# === lora-station запуск, node_id=0x0001 ===
# Пины: CS=8 RESET=22 DIO1=23 BUSY=24
# SX1262 init OK — 868.0 МГц, SF10, BW125, CR4/5, 14 дБм
# RX запущен, слушаем эфир (Ctrl-C для выхода)
# [RX#1] PING dev=1 ttl=3 ch=0 lat=45019741 lon=39032218  RSSI=-42 дБм SNR=9 дБ

# 3. Проверить, что записи легли в БД
sqlite3 /var/lib/mesh-net/mesh.db 'SELECT * FROM pings ORDER BY id DESC LIMIT 5;'
sqlite3 /var/lib/mesh-net/mesh.db 'SELECT * FROM devices;'
```

## Типовые проблемы

| Симптом | Что это | Что делать |
|---|---|---|
| `SX1262 init FAIL: SX1262 BUSY висит` | Чип не отвечает, питание/SPI | Проверь VCC=3.3 В, SPI включён, пины (см. CLAUDE.md root) |
| `SQLITE_CANTOPEN` | БД не создана | `bash scripts/db_init/init.sh` |
| `Permission denied: /dev/spidev0.0` | Нет в группе spi | `sudo usermod -aG spi,gpio $USER`, перелогинься |
| Пакеты от ESP32 не приходят | Разные параметры радио | Проверь, что freq/SF/BW/CR/sync совпадают с прошивкой |
| `IRQ без валидного пакета` повторяется | Шум в эфире / антенна не подключена | Подключи антенну, унеси с источников помех |
