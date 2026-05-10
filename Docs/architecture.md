# Архитектура системы

## Диаграмма

```
┌────────────────────┐     LoRa 868 МГц      ┌──────────────────────┐     LoRa 868 МГц     ┌─────────────────────────┐
│  ТЕРМИНАЛ          │ ─────────────────────► │  ИНФО-ТОЧКА          │ ───────────────────► │  БАЗА СПАСАТЕЛЕЙ        │
│  ESP32-S3 N16R8    │                        │  Raspberry Pi 5      │                      │  Raspberry Pi 5         │
│                    │                        │  (этап 5)            │                      │                         │
│ • HT-RA62 (SX1262) │ ◄───────────────────── │ • SX1262             │ ◄─────────────────── │ • SX1262                │
│ • Wi-Fi AP HTTPS   │     LoRa (mesh)        │ • mesh-relay-node    │     LoRa (mesh)      │ • mesh-lora-station     │
│ • CSMA/CAD LBT     │                        │ • info-portal HTML   │                      │ • mesh-rescue-api       │
│ • PING/SOS/CHAT    │                        │ • dnsmasq + nginx    │                      │ • mesh-gigachat-agent   │
│ • inbox            │                        │   (captive portal)   │                      │ • SQLite + WAL          │
└─────────┬──────────┘                        └──────────────────────┘                      │ • Leaflet + tiles       │
          │ Wi-Fi                                                                            │ • GigaChat AI           │
          │ HTTPS:443                                                                        └─────────────┬───────────┘
          ▼                                                                                                │
┌────────────────────┐                                                                                     │ LAN / Wi-Fi
│  СМАРТФОН ТУРИСТА  │                                                                                     ▼
│ • GPS из браузера  │                                                                       ┌─────────────────────────┐
│   (geolocation)    │                                                                       │  НОУТБУК ОПЕРАТОРА      │
│ • POST /api/gps    │                                                                       │  rescue-dashboard       │
│ • POST /api/chat   │                                                                       │  (браузер)              │
│ • GET /api/inbox   │                                                                       └─────────────────────────┘
└────────────────────┘
```

## Компоненты

### 1. Терминал туриста (ESP32-S3 N16R8 + HT-RA62)

**Прошивка:** PlatformIO + Arduino framework, C++17. Один файл
`firmware/esp32-terminal/src/main.cpp`. Dual-core:
- **Core 0** — Wi-Fi + HTTPS-сервер
- **Core 1** — `loop()`: LoRa TX/RX, SOS state machine, CHAT-бёрст, PING

**Веб-интерфейс** (HTML вшит в прошивку как `INDEX_HTML`, отдаётся на
`https://192.168.4.1/`):
- Кнопки SOS трёх типов (падение / медицина / заблудился)
- Текстовое поле чата (до 48 байт UTF-8)
- Включение GPS (`navigator.geolocation.watchPosition`)
- Inbox: входящие сообщения от базы и других туристов (polling 5 сек)

**HTTP-эндпоинты:** `GET /` (HTML), `POST /api/sos` (1 байт = sos_type),
`POST /api/gps` (`lat,lon`), `POST /api/chat` (UTF-8 текст), `GET /api/status`,
`GET /api/inbox?since=N` (JSON). Только HTTPS (для secure-context требования
браузера к geolocation).

**LoRa:** 868 МГц, SF10, BW125, CR4/5, +14 дБм. CSMA/CA: pre-CAD jitter +
`scanChannel()` + экспоненциальный backoff (см. `Docs/protocol.md`).

### 2. Инфо-точка (RPi5) — *этап 5*

Только ретрансляция и captive Wi-Fi портал. БД и AI здесь не нужны —
весь трафик идёт через эту точку транзитом до базы.

### 3. База спасателей (RPi5 + HT-RA62 на SPI)

| Сервис | Порт | Описание |
|--------|------|----------|
| `mesh-lora-station` | — | Демон LoRa: RX → SQLite + ретрансляция, outbox-poller для исходящих CHAT |
| `mesh-rescue-api` | 8000 | FastAPI: REST + WebSocket + статика дашборда + tiles |
| `mesh-gigachat-agent` | 127.0.0.1:8001 | Прокси к GigaChat с function calling |

**SQLite** `/var/lib/mesh-net/mesh.db` (WAL): треки, SOS, чат, очередь
исходящих сообщений. Таблицы: `devices`, `pings`, `sos_events`,
`chat_messages`, `outgoing_chat`. Подробнее: [`database.md`](database.md).

---

## Потоки данных

### Трекинг (PING каждые 20 сек)

```
ESP32 (с GPS из браузера)
  → CSMA/CAD → LoRa TX
                  ↓
            lora-station: RX → decode → dedup → upsert devices, insert pings
                  ↓
            ретрансляция (TTL−1) обратно в эфир
                  ↓
            rescue-api WS poller (раз в сек) → событие "ping" → дашборд
                  ↓
            обновление маркера на карте, строки в списке туристов
```

### SOS

```
Турист нажимает кнопку (одну из трёх — падение/медицина/заблудился)
  → ESP32: бёрст 3 пакета × 500 мс, payload[0] = sos_type
                  ↓
            lora-station: dedup по hash(payload) — 3 копии схлопываются в 1
                  ↓
            INSERT INTO sos_events
                  ↓
            WS "sos" → дашборд: красный мигающий маркер + звуковой сигнал
                  ↓
            оператор → POST /api/sos/{id}/ack или /resolve
                  ↓
            UI цвет: красный → оранжевый (acked) → зелёный (resolved)
```

### Чат: турист → база

```
Турист: textarea на странице ESP32 → POST /api/chat (текст)
  → ESP32 ставит флаг + буфер
                  ↓
            loop(): SOS (если есть) → CHAT-бёрст 3 копии × 2.1 сек
                  ↓ (CSMA/CAD перед каждым)
            LoRa TX
                  ↓
            lora-station: dedup → INSERT INTO chat_messages
                  ↓
            WS "chat" → дашборд: пузырь в правой нижней панели
```

### Чат: база → турист

```
Оператор: textarea «Чат с туристами» в дашборде → POST /api/messages {text}
  → rescue-api:
      - INSERT INTO chat_messages (от device_id=NODE_DEVICE_ID, для UI)
      - 3 × INSERT INTO outgoing_chat (для retransmit)
                  ↓
            WS "chat" → дашборд видит свой ответ сразу
                  ↓
            lora-station outbox-poller (раз в сек):
              SELECT WHERE sent_at IS NULL ORDER BY id LIMIT 1
                  ↓
            формирует CHAT-пакет (device_id=NODE_DEVICE_ID, channel=TOURIST)
                  ↓ (CSMA/RSSI-LBT перед каждым TX)
            LoRa TX → UPDATE outgoing_chat SET sent_at=now
                  ↓
            ESP32 RX → process_rx → inbox_push
                  ↓
            страница туриста polling /api/inbox → новый пузырь в DOM
```

### Запрос к AI-диспетчеру

```
Оператор: вводит вопрос в верхнюю чат-панель дашборда
  → дашборд → POST /api/chat {message, history}
                  ↓
            rescue-api проксирует на 127.0.0.1:8001/chat
                  ↓
            gigachat-agent: GigaChat API (с function calling)
                  ↓
            tool calls (tourists/sos/track/stats) → HTTP к rescue-api
                  ↓
            модель синтезирует ответ из результатов инструментов
                  ↓
            JSON {reply, tools_used} обратно в дашборд
```

---

## Half-duplex и CSMA

LoRa SX1262 — **полудуплекс**: чип в момент TX физически не принимает.
Если оба узла начинают передавать одновременно — оба пакета теряются
(никто не слышит другого).

**Что у нас защищает от collision:**

1. **Pre-CAD jitter** 0–400 мс — расходит синхронные передачи во времени
2. **Carrier-sense (LBT)** перед TX:
   - ESP32: `radio.scanChannel()` — детекция LoRa-преамбулы за ~8 мс
   - lora-station: `GetRssiInst` — мгновенный RSSI, порог −100 дБм
3. **Экспоненциальный backoff** при «занято»: CW растёт 250→500→1000→2000 мс
4. **Retransmit** 3 копии CHAT-пакета на обеих сторонах
5. **Дедуп** по `(type, device_id, hash(payload))` в окне 30 сек

**Что не закрыто:** при идеально синхронных нажатиях обоих узлов CSMA
не успевает развести во времени, все 3 retransmit-копии могут
схлопываться в коллизионные окна. См. [CLAUDE.md → Известные
ограничения](../CLAUDE.md). Полное решение — ACK + retry-по-таймауту,
отложено до полевых испытаний.

---

## Безопасность

- **HTTPS на ESP32** — self-signed cert, требуется браузером для
  `navigator.geolocation`. Сертификат генерируется
  `firmware/esp32-terminal/scripts/gen_cert.sh`, лежит в `include/cert.h`
  как DER (RSA-2048, 10 лет).
- **gigachat-agent** биндится только на 127.0.0.1 — наружу не светится,
  rescue-api проксирует через `/api/chat`.
- **rescue-api** открыт на 0.0.0.0:8000 без авторизации (локальная
  сеть базы). Перед боевой выкаткой — добавить basic-auth или сузить до
  внутреннего интерфейса.
- **token-key GigaChat** — в `.gitignore` (`**/token-key`), не попадает
  в коммиты.
- **БД read-only** для всех GET-эндпоинтов rescue-api (`?mode=ro`),
  read-write только в `/sos/{id}/{ack,resolve}`, `/api/messages`,
  `/api/admin/purge`.

---

## Масштабирование

Для покрытия длинного маршрута (> 5 км) добавляются промежуточные
инфо-точки. Каждая участвует в mesh-flooding автоматически: получила
пакет — ретранслировала с TTL−1. TTL=3 покрывает 3 прыжка ≈ 10–15 км.

Порог дедупа в lora-station — 30 сек, что позволяет одному и тому же
сообщению пройти через несколько ретрансляторов без зацикливания.
