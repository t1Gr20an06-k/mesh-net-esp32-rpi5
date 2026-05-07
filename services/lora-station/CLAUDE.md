# CLAUDE.md — lora-station

Python-демон для работы с LoRa SX1262 (модуль HT-RA62) на Raspberry Pi 5.
Принимает 64-байтные пакеты Mesh-net Тропы, дедуплицирует, ретранслирует
(TTL−1) и пишет в SQLite. Работает на RPi5 базы спасателей; на инфо-точке
может использоваться тот же код с другим `NODE_DEVICE_ID`.

## Стек

- Python 3.11
- **`lgpio`** (libgpiod v2) — единственная зависимость для железа: GPIO
  (RESET / BUSY / DIO1 / CS) и SPI идут одним handle через `lgpio.spi_*`.
  Так делает RadioLib PiHal в C++; на RPi5 RP1 это самый стабильный путь —
  `RPi.GPIO` несовместим с RP1, а `spidev` иногда тупит с hardware-CS
- Системный пакет `python3-lgpio` через apt + venv с `--system-site-packages`
  (см. `install.sh`) — НЕ ставим через pip, иначе тащит swig + сборку из C
- Стандартный `sqlite3` — без ORM
- Стандартный `logging` — пока без structlog

## Структура

```
services/lora-station/
├── lora_station/
│   ├── __init__.py
│   ├── __main__.py     — точка входа: argparse, главный цикл, SIGINT/SIGTERM
│   ├── packet.py       — кодек 64-байтного пакета (CRC-16/CCITT-FALSE)
│   ├── sx1262.py       — драйвер SX1262: init, RX, TX, IRQ через lgpio
│   ├── db.py           — SQLite-обёртка (upsert device, insert ping/sos/chat)
│   ├── mesh.py         — DedupCache, TxQueue (приоритеты), make_forward
│   └── dispatcher.py   — decoded packet → БД + ретрансляция
├── requirements.txt    — пусто (lgpio тащим apt'ом, см. install.sh)
└── install.sh          — установка на RPi5 (apt + venv, init БД, проверка SPI)
```

## Запуск

### Руками (отладка)

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

### Через systemd (продакшн на RPi5)

После `sudo bash scripts/systemd/install.sh` демон стартует автоматически
при включении RPi5. Управление:

```bash
# Статус и последние логи
sudo systemctl status mesh-lora-station

# Live-логи
sudo journalctl -u mesh-lora-station -f

# Рестарт (после правок кода или ENV в юните)
sudo systemctl restart mesh-lora-station

# Остановить, не выключая автозапуск
sudo systemctl stop mesh-lora-station

# Полностью отключить (на время отладки руками)
sudo systemctl disable --now mesh-lora-station
```

⚠ **Не запускать одновременно systemd-копию и `python -m lora_station` руками** —
обе попытаются открыть `/dev/spidev0.0`, та что вторая упадёт с `Device or resource busy`.
Перед ручным запуском: `sudo systemctl stop mesh-lora-station`.

ENV-переменные юнита редактируются в `/etc/systemd/system/mesh-lora-station.service`
(или в шаблоне `scripts/systemd/mesh-lora-station.service` + переустановка
через `sudo bash scripts/systemd/install.sh mesh-lora-station`). После правки —
`sudo systemctl daemon-reload && sudo systemctl restart mesh-lora-station`.

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
- **Поток lgpio (DIO1 IRQ)**: при rising edge на DIO1 ставит
  `threading.Event`. SPI здесь НЕ трогаем — иначе race на шине.
- **Fallback-поллинг IRQ через SPI** (внутри `wait_rx`): на RPi5 RP1
  `lgpio.callback` на DIO1 в отладке этапа 2 пару раз залипал, поэтому
  раз в 50 мс главный поток сам читает IRQ-регистр чипа. Стоимость —
  ~20 SPI-чтений в секунду, копейки. Отключается `poll_irq=False`.
- **DB**: `sqlite3.connect(check_same_thread=False)` + `RLock` на запросы.
  Сейчас всё пишется из главного потока, но Lock оставлен на будущее
  (HTTP-эндпоинт от rescue-api).

## Костыль с CS: hardware-CS уехал на GPIO 27

На этой плате `/boot/firmware/config.txt` содержит:

```
dtoverlay=spi0-1cs,cs0_pin=27
```

Это переназначает hardware-CS0 SPI0 на GPIO 27. HT-RA62 при этом
физически подключён к GPIO 8 (стандартный CE0). В результате:

- kernel-driver SPI дёргает CS на GPIO 27 (в пустоту, ничего не подключено)
- HT-RA62 на GPIO 8 не получает CS при `lgpio.spi_xfer(...)`
- SX1262 видит шину в Hi-Z → MISO просто эхо MOSI → "чип не отвечает"

**Решение** (`sx1262.py::_spi_xfer`): дёргаем CS на GPIO 8 вручную через
`lgpio.gpio_write` до и после `spi_xfer`. Kernel-CS на GPIO 27 при этом
бьёт в пустоту — не мешает.

Проверено на RPi5 Bookworm + lgpio 0.2.2.0. Снифер `tests/field/lora-sniffer/`
делает то же самое (RadioLib PiHal с `manual_cs=true`).

Если когда-нибудь будет непонятно почему в начале нет ответа от чипа —
смотреть `/boot/firmware/config.txt` и сравнивать `cs0_pin` с фактическим
GPIO HT-RA62.

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
| `Чип не выходит в STDBY_RC ... last chip_mode=0` | На SPI приходит мусор / эхо MOSI | См. раздел "Костыль с CS": `cs0_pin=27` в `config.txt` ↔ HT-RA62 на GPIO 8 |
| `Sync word не записался: ...` | SPI работает в одну сторону, чип не слышит запись | То же самое — проверь CS-маршрутизацию |
| `SQLITE_CANTOPEN` | БД не создана | `bash scripts/db_init/init.sh` |
| `Permission denied: /dev/spidev0.0` | Нет в группе spi | `sudo usermod -aG spi,gpio $USER`, перелогинься |
| Пакеты от ESP32 не приходят, но `chip_mode=5` | Разные параметры радио или антенна | Проверь freq/SF/BW/CR/sync, подключи антенну, отнеси ESP ≥ 1 м (близко = насыщение приёмника) |
| `IRQ без валидного пакета` повторяется | Шум в эфире / антенна не подключена | Подключи антенну, унеси с источников помех |
| Не работает `pip install lgpio` | Требует swig + сборку из C | НЕ ставить через pip — используй `apt install python3-lgpio` (см. `install.sh`) |
