# Протокол LoRa Mesh Mesh-net Тропы (v1)

Источник истины:
- Документация формата: [`proto/messages.proto`](../proto/messages.proto)
- C++ кодек: [`firmware/esp32-terminal/lib/mesh_packet/`](../firmware/esp32-terminal/lib/mesh_packet/)
- Python кодек: [`services/lora-station/lora_station/packet.py`](../services/lora-station/lora_station/packet.py)

---

## Радиоуровень

| Параметр | Значение |
|----------|----------|
| Частота | 868.0 МГц (EU ISM) |
| Spreading Factor | SF10 |
| Bandwidth | 125 кГц |
| Coding Rate | 4/5 |
| Преамбула | 8 символов |
| TX power | +14 дБм |
| Sync word | PRIVATE (0x14 / 0x24) |
| LoRa CRC | включён (хардварный) |
| TCXO | 1.8 В (HT-RA62) |
| DIO2 | RF switch |
| Время передачи 64 байт | ~706 мс |
| Чувствительность | около −136 дБм @ SF10/BW125 |

Эти значения захардкожены и должны 1-в-1 совпадать на ВСЕХ узлах:
- ESP32: [`src/main.cpp`](../firmware/esp32-terminal/src/main.cpp) — макросы `RADIO_*`
- RPi5: [`sx1262.py`](../services/lora-station/lora_station/sx1262.py) — `begin()`

---

## Структура пакета (64 байта, big-endian)

ВНИМАНИЕ: это НЕ protobuf на проводе — `proto/messages.proto` лежит как
документация формата, реальная сериализация — побайтовая.

```
смещение  размер  поле        тип        описание
─────────────────────────────────────────────────────────────────
   0      1       version     uint8      Версия протокола (1)
   1      1       type        uint8      0=PING, 1=CHAT, 2=SOS, 3=ACK
   2      2       device_id   uint16 BE  ID отправителя
   4      1       channel     uint8      0=TOURIST, 1=RESCUE
   5      1       ttl         uint8      Time-to-live (default 3)
   6      4       latitude    int32 BE   Широта × 1e6
  10      4       longitude   int32 BE   Долгота × 1e6
  14     48       payload     bytes      Зависит от type, остаток 0x00
  62      2       crc16       uint16 BE  CRC-16/CCITT-FALSE от байт [0..61]
─────────────────────────────────────────────────────────────────
                                         ИТОГО: 64 байта
```

### Раскладка `payload[48]` по типам

**PING (type=0):**
```
[0]    battery_pct  (uint8, 0..100 %)
[1]    rssi_last    (int8, RSSI последнего принятого пакета на терминале; 0 = н/д)
[2-3]  seq          (uint16 BE, порядковый номер для дедупликации)
[4-47] зарезервировано (0x00)
```

**CHAT (type=1):**
```
[0-47] UTF-8 текст, остаток 0x00
       Максимум 48 байт — это ~24 русских символа (UTF-8 multibyte).
```

**SOS (type=2):**
```
[0]    sos_type  (uint8: 0=неизвестно, 1=падение, 2=медицина, 3=заблудился, 4=погода)
[1-47] UTF-8 сообщение, остаток 0x00
```

**ACK (type=3):**
```
[0-1]  ack_device_id (uint16 BE — чей SOS подтверждаем)
[2-47] зарезервировано (0x00)
```

---

## Sanity-проверки декодера

После проверки CRC декодер дополнительно валидирует поля. Зачем: LoRa-CRC
чипа + наш CRC-16 — это всё ещё ~1/65536 шанс случайного совпадения на
шуме (был реальный инцидент с фантомным `device_id=12345`). Без
валидации в БД заводятся «фантомные» устройства.

Пакет дропается если:
- `version != 1`
- `type` вне диапазона `0..3`
- `channel` вне диапазона `0..1`
- `ttl == 0` или `ttl > 8`

См. [`packet.py::decode`](../services/lora-station/lora_station/packet.py)
и [`MeshPacket.cpp::decode`](../firmware/esp32-terminal/lib/mesh_packet/MeshPacket.cpp).

---

## CRC-16/CCITT-FALSE

```
Poly: 0x1021,  Init: 0xFFFF,  RefIn: false,  RefOut: false,  XorOut: 0x0000
```

Считается от байт `[0..61]`, результат пишется big-endian в `[62..63]`.

```python
def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if (crc & 0x8000) else (crc << 1)
            crc &= 0xFFFF
    return crc
```

---

## Логика mesh (flooding)

### Приём пакета

1. Аппаратный LoRa CRC проверяется чипом (RX_CRC_ERROR → дроп ещё до нас)
2. Драйвер читает 64 байта из FIFO
3. Декодер: размер == 64, наш CRC-16, sanity полей
4. Эхо собственных пакетов — `device_id == NODE_DEVICE_ID` → дроп
5. Дедупликация по ключу:
   - PING: `(type, device_id, seq)` — seq из payload[2..4]
   - SOS/CHAT/ACK: `(type, device_id, hash(payload))` — hash payload, окно 30 сек
6. Запись в БД (PING / SOS / CHAT в свои таблицы; ACK пока не пишется)
7. Подготовка ретрансляции: `make_forward(pkt)` создаёт новый пакет с TTL−1.
   Если TTL был 1 — не ретранслируем.
8. Постановка в TxQueue с приоритетом:
   - SOS (PRIO=0) — никогда не дропается
   - ACK (PRIO=1)
   - CHAT (PRIO=2)
   - PING (PRIO=3) — дропается первым при переполнении (TX_QUEUE_MAX=16)

### Отправка пакета (CSMA/CA, Meshtastic-стиль)

Перед каждым `radio.transmit()`:

1. **Pre-CAD jitter** — случайная задержка 0–400 мс. Без этого узлы,
   которые одновременно вошли в LBT, оба видят «свободно» и оба уходят
   в TX → collision. Случайная задержка расходит их по времени.
2. **Listen-Before-Talk:**
   - На ESP32 — `radio.scanChannel()` (CAD, ~8 мс на SF10/BW125)
   - На lora-station — мгновенный RSSI через `GetRssiInst (0x15)`,
     порог занятости −100 дБм
3. Если канал занят — backoff в текущем contention window (CW),
   CW удваивается: 250 → 500 → 1000 → 2000 мс. До 5 попыток.
4. После успешного CAD на ESP32 — обязательно
   `radio.setPacketReceivedAction(on_rx_done)`. CAD меняет IRQ-маску
   чипа и без этого RX-callback больше не срабатывает (поймали в
   процессе отладки).
5. Если CW исчерпался — передаём всё равно (пакет важен, особенно SOS).

---

## Особые случаи

### SOS-бёрст

ESP32 при нажатии кнопки шлёт SOS **3 пакета подряд × 500 мс**. Все три
имеют одинаковый payload, поэтому дедуп по `hash(payload)` на стороне
базы оставит только одну запись в `sos_events` и одно WS-уведомление.

### CHAT retransmit

ESP32 → база: 3 копии × 2.1 сек интервал.
База → ESP32: rescue-api пишет 3 строки в `outgoing_chat`, lora-station
выгребает по 1 в секунду.

Зачем 3: LoRa полу-дуплекс. При синхронном TX обоих узлов оба пакета
теряются. CSMA расходит большинство случаев, но не 100% — три копии с
разными интервалами почти гарантируют что хотя бы одна дойдёт.

См. [CLAUDE.md → Известные ограничения](../CLAUDE.md) для деталей.

### Эхо своих пакетов от ретранслятора

Ретранслятор (lora-station инфо-точки или базы) принимает пакет и
пересылает его с TTL−1. Если в сети только 2 узла (ESP32 + база), то
ESP32 услышит свой собственный пакет от базы. Фильтр в `process_rx`
на стороне ESP32:
```cpp
if (pkt.device_id == DEVICE_ID) return;
```

---

## Каналы

| Код | Имя | Кто принимает | Назначение |
|-----|-----|---------------|------------|
| 0 | TOURIST | все узлы | Открытый канал — PING, SOS, чат туристов |
| 1 | RESCUE | только узлы с device_id в whitelist *(этап 5)* | Служебный канал спасателей, ACK |

На текущем этапе RESCUE-канал используется только для маркировки имени
самого узла-базы при upsert в `devices` (`channel=1`). Whitelist-фильтр
на приёме — этап 5.

---

## Версионирование

При изменении структуры пакета — инкрементировать `version`. Узлы с
несовместимой версией дропают пакет (см. sanity-проверки декодера выше).
Текущая версия: **1**.

---

## Пример: PING от устройства 16

```
HEX (64 байта):
01 00 00 10 00 03 02 A0 EB 24 02 54 7F 9F 64 00 00 17 00 00 ...
^^ ^^ ^^^^^ ^^ ^^ ^^^^^^^^^^^ ^^^^^^^^^^^ ^^ ^^ ^^^^^ ^^^^^
║  ║   ║    ║  ║   lat=44.094948  lon=39.095679  bat seq  zeros
║  ║   ║    ║  ttl=3
║  ║   ║    channel=TOURIST
║  ║   device_id=0x0010 (16)
║  type=PING
version=1

Расшифровка payload:
  battery_pct = 0x64 = 100 %
  rssi_last   = 0x00 = н/д
  seq         = 0x0017 = 23

CRC от первых 62 байт пишется в байты [62..63].
```

См. также `if __name__ == '__main__'` в
[`packet.py`](../services/lora-station/lora_station/packet.py) — самотест
декодера с проверкой round-trip.
