# CLAUDE.md — lora-station

Python-демон для работы с LoRa SX1262 через SPI на RPi5. Принимает пакеты, парсит, ретранслирует (mesh), сохраняет в БД или передаёт relay-node.

## Стек

- **Python 3.11**, `spidev` или `pigpio`, `structlog`
- SX1262 driver: собственный (`lib/sx1262.py`) или `pyLoRa`
- Взаимодействие с `rescue-api`: HTTP POST `/internal/packet`

## Структура

```
lora_station/
  __main__.py      — точка входа, argparse, запуск event loop
  radio.py         — низкоуровневый драйвер SX1262 (SPI)
  packet.py        — парсинг/сериализация пакетов протокола v1
  mesh.py          — логика ретрансляции (dedup cache, очередь TX)
  dispatcher.py    — маршрутизация входящих пакетов (в БД / relay)
  mock_radio.py    — заглушка для запуска без железа (--mock флаг)
```

## Запуск

```bash
# С реальным железом
python -m lora_station

# Mock-режим (тестирование без RPi/SX1262)
python -m lora_station --mock

# Подробный вывод
python -m lora_station --mock --verbose
```

## Параметры SX1262 (должны совпадать на всех узлах!)

```python
FREQ    = 868_000_000   # Гц
SF      = 10
BW      = 125_000       # Гц  
CR      = 5             # 4/5
PREAMBLE= 8
TX_POWER= 22            # дБм
```

## Важные правила

- **Thread safety**: radio.py работает в отдельном потоке; `mesh.py` использует `asyncio.Queue`
- **Dedup cache**: `dict[str, float]` с ключом `f"{device_id}:{timestamp}"`, очистка записей старше 30 сек
- **Очередь TX**: максимум 10 пакетов. При переполнении — дропать PING, SOS никогда не дропать
- **BUSY pin**: перед каждой TX проверять GPIO BUSY, таймаут 1 сек
- В mock-режиме логировать все "отправленные" пакеты в stdout с пометкой `[MOCK TX]`
- Никаких глобальных переменных — всё через `LoraStation` класс

## Переменные окружения

| Переменная | Default | Описание |
|------------|---------|----------|
| `LORA_SPI_BUS` | `0` | SPI шина |
| `LORA_SPI_CS` | `0` | CS пин |
| `LORA_RESET_PIN` | `22` | GPIO reset |
| `LORA_DIO1_PIN` | `23` | GPIO DIO1/IRQ |
| `LORA_BUSY_PIN` | `24` | GPIO BUSY |
| `RESCUE_WHITELIST` | `""` | Через запятую device_id для RESCUE-канала |
| `UPSTREAM_URL` | `http://127.0.0.1:8000` | rescue-api или relay-node URL |
| `NODE_DEVICE_ID` | `0x0000` | ID этого узла (для ACK-ответов) |

## Тестирование mock-режима

```bash
# Терминал 1: запустить lora-station в mock
python -m lora_station --mock --verbose

# Терминал 2: отправить тестовый пакет
python scripts/test_packets/send_ping.py --device-id 0x0047 --lat 43.355 --lon 42.514

# Терминал 3: отправить SOS
python scripts/test_packets/send_sos.py --device-id 0x0047 --payload "Test SOS"
```
