# API Reference

Источник истины: код в `services/rescue-api/rescue_api/app.py`.
Swagger UI доступен в браузере: `http://<rpi5-ip>:8000/docs`.

---

## rescue-api (порт 8000)

Base URL: `http://<rescue-base-ip>:8000` (по LAN) или `http://localhost:8000`.

CORS открыт по умолчанию (для разработки), управляется ENV `ALLOW_CORS`.

### Утилитарные

#### `GET /api/health`

Жив ли сервис. Не логируется.

```json
{"status": "ok"}
```

#### `GET /api/stats`

Счётчики из БД.

```json
{
  "pings_total": 142,
  "sos_total": 3,
  "sos_open": 1,
  "devices_total": 2,
  "devices_online": 1
}
```

`devices_online` — устройства, у которых был PING за последние
`ACTIVE_THRESHOLD_MIN=2` минуты (см. `db.py`).

---

### Туристы и устройства

#### `GET /api/tourists`

Кто сейчас в эфире. База (`NODE_DEVICE_ID`) исключена из выдачи.

```json
[
  {
    "device_id": 16,
    "name": "",
    "channel": 0,
    "channel_label": "TOURIST",
    "last_seen_at": "2026-05-09T22:13:51Z",
    "last_ping_at": "2026-05-09T22:13:51Z",
    "position": {"lat": 44.094948, "lon": 39.095679},
    "battery_pct": 100,
    "rssi": -42
  }
]
```

#### `GET /api/devices`

Весь реестр (кроме базы). Аналогично `/api/tourists`, но без фильтра
по «активности».

---

### Треки

#### `GET /api/pings?device_id=&hours=&limit=`

PING'и для рисования трека.

| Query | Default | Описание |
|-------|---------|----------|
| `device_id` | (без фильтра) | uint16 ID устройства |
| `hours` | `1.0` | Глубина выборки (max 720) |
| `limit` | `500` | Max количество точек (max 10000) |

```json
[
  {
    "id": 142,
    "device_id": 16,
    "received_at": "2026-05-09T22:13:51Z",
    "position": {"lat": 44.094948, "lon": 39.095679},
    "battery_pct": 100,
    "rssi": -42,
    "rssi_last": 0,
    "seq": 23
  }
]
```

---

### SOS

#### `GET /api/sos?only_open=true`

Список SOS-событий. По умолчанию только незакрытые.

```json
[
  {
    "id": 1,
    "device_id": 16,
    "received_at": "2026-05-09T22:00:31Z",
    "position": {"lat": 44.0949, "lon": 39.0956},
    "sos_type": 1,
    "sos_type_label": "падение",
    "message": "",
    "acked": false,
    "acked_at": null,
    "acked_by": null,
    "resolved": false,
    "resolved_at": null,
    "notes": ""
  }
]
```

`sos_type_label`: `неизвестно` / `падение` / `медицина` / `заблудился` / `погода`.

#### `GET /api/sos/{id}`

Один SOS по id. 404 если не найден.

#### `POST /api/sos/{id}/ack`

Подтвердить SOS (оператор увидел). Идемпотентно: повторный ack не
перезаписывает `acked_at` — важно юридически (время первого ack).

```json
// body
{"acked_by": 16}
```

Возвращает обновлённый объект SOS.

#### `POST /api/sos/{id}/resolve`

Закрыть инцидент. Можно сразу после `ack` или вообще без него.

```json
// body
{"notes": "Помощь оказана, турист найден"}
```

---

### Чат турист↔база

#### `GET /api/messages?limit=100`

Последние N сообщений в хронологическом порядке (старые → новые).
Включает и сообщения от туристов, и ответы базы (от `device_id=NODE_DEVICE_ID`).

```json
[
  {
    "id": 5,
    "device_id": 16,
    "device_name": "",
    "received_at": "2026-05-09T22:13:46Z",
    "position": {"lat": 44.0949, "lon": 39.0956},
    "channel": 0,
    "channel_label": "TOURIST",
    "message": "Дошли до приюта"
  },
  {
    "id": 6,
    "device_id": 1,
    "device_name": "База спасателей",
    "received_at": "2026-05-09T22:14:02Z",
    "position": null,
    "channel": 0,
    "channel_label": "TOURIST",
    "message": "Принял, оставайтесь на связи"
  }
]
```

`position: null` — если координат нет (пакеты от базы) или фикс `(0,0)`.

#### `POST /api/messages`

Ответ оператора туристам. Текст ≤ 48 байт UTF-8 (≈ 24 русских буквы).

```json
// body
{"text": "Принял, оставайтесь на связи"}
```

Возвращает созданный объект `ChatMessage`. Параллельно:
- `INSERT INTO chat_messages` (для UI, появится в WS-event `chat`)
- `INSERT INTO outgoing_chat × 3` (для retransmit, lora-station выгребает
  по 1 в сек и шлёт в эфир)

Ошибки: 400 при пустом тексте или > 48 байт UTF-8.

---

### AI-диспетчер

#### `POST /api/chat`

Прокси на gigachat-agent (`http://127.0.0.1:8001/chat`). Используется
дашбордом для верхней чат-панели.

```json
// body
{
  "message": "Кто сейчас в эфире?",
  "history": [
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "..."}
  ]
}
```

```json
// response
{
  "reply": "Один турист, device_id=16, последний PING 30 сек назад.",
  "tools_used": ["get_active_tourists"]
}
```

При недоступности AI возвращает 200 с `{"reply": "", "tools_used": [], "error": "AI недоступен (...)"}` — дашборд показывает красным.

---

### Админ

#### `POST /api/admin/purge`

Очистка таблиц БД для отладки. Защита от случайного вызова — поле
`confirm` должно быть строкой `ОЧИСТИТЬ`.

```json
// body
{
  "confirm": "ОЧИСТИТЬ",
  "tables": ["pings", "sos_events", "chat_messages", "outgoing_chat", "devices"]
}
```

Допустимые таблицы: `pings`, `sos_events`, `chat_messages`,
`outgoing_chat`, `devices`. Если в списке `devices` — каскадно
зачищаются все дочерние таблицы (FK constraint).

Сбрасывает `sqlite_sequence` для затронутых таблиц + сбрасывает
`last_seen_id` в WS-broadcaster (иначе после purge новые ID < 0 и
push'и теряются).

```json
// response
{"ok": true, "deleted": {"pings": 142, "outgoing_chat": 3}}
```

Ошибки: 400 при пустом `tables` или неизвестных именах, 403 при
неверном `confirm`.

---

### WebSocket `/ws`

Push-канал для дашборда. Сервер шлёт JSON, клиент ничего слать не
обязан (но может — игнорируется).

```json
{"event": "ping", "data": { /* объект Ping */ }}
{"event": "sos",  "data": { /* объект Sos  */ }}
{"event": "chat", "data": { /* объект ChatMessage */ }}
```

Реализация: `Broadcaster` в [`ws.py`](../services/rescue-api/rescue_api/ws.py).
Раз в секунду читает `MAX(id)` из `pings`/`sos_events`/`chat_messages` и
пушит новые строки. Стартовая точка — текущий `MAX(id)`, чтобы при
подключении дашборд не получил тысячи старых записей.

---

### Тайлы карты

#### `GET /tiles/{z}/{x}/{y}.png`

Оффлайн-тайлы Leaflet. Каталог монтируется через `StaticFiles` из
`TILES_DIR` (default `/var/lib/mesh-net/tiles`). Файлы скачиваются один
раз через `scripts/import_tiles/download_tiles.py`. Если каталога нет —
эндпоинт не монтируется, дашборд показывает серый фон с маркерами.

---

## gigachat-agent (127.0.0.1:8001)

Не выставляется наружу — все обращения идут через прокси rescue-api
`/api/chat`. Прямой доступ нужен только для отладки на самом RPi5.

#### `GET /health`

```json
{"status": "ok", "auth_mode": "authorization_key", "model": "GigaChat", "scope": "GIGACHAT_API_PERS"}
```

`auth_mode`: `authorization_key` / `access_token` / `none`.

#### `POST /chat`

Идентичный body/response с `POST /api/chat` rescue-api.

**Function calling — 4 инструмента:**

| Имя | Описание | Идёт в rescue-api |
|-----|----------|-------------------|
| `get_active_tourists` | Кто в эфире | `GET /api/tourists` |
| `get_sos_events` | SOS-инциденты | `GET /api/sos?only_open=` |
| `get_device_track` | Трек устройства | `GET /api/pings?device_id=&hours=` |
| `get_stats` | Общие счётчики | `GET /api/stats` |

Подробнее: [`tools.py`](../services/gigachat-agent/gigachat_agent/tools.py).

---

## ESP32 — встроенный HTTPS-сервер (192.168.4.1:443)

Self-signed cert. Доступен после подключения к Wi-Fi `MeshNet-016`.

| Метод | URL | Что делает |
|-------|-----|------------|
| GET | `/` | HTML-страница терминала туриста (вшита в прошивку) |
| POST | `/api/sos` | body = одна цифра (0..4) — sos_type, взводит бёрст |
| POST | `/api/gps` | body = `lat,lon` — обновляет координаты для следующего PING |
| POST | `/api/chat` | body = UTF-8 текст до 48 байт — взводит CHAT-бёрст |
| GET | `/api/status` | `idle` / `tx:N` / `done` — для polling SOS-индикатора |
| GET | `/api/inbox?since=N` | JSON `{latest, messages[]}` — входящие CHAT'ы |

`/api/inbox` ответ:

```json
{
  "latest": 3,
  "messages": [
    {
      "id": 3,
      "from": 1,
      "age_ms": 12340,
      "text": "Принял, оставайтесь на связи"
    }
  ]
}
```

`from` — device_id отправителя; `1` означает базу (показывается с золотым
акцентом «🛟 База спасателей»).

---

## Коды ошибок

| Код | Описание |
|-----|----------|
| 200 | OK |
| 400 | Невалидный body (пустое сообщение, не JSON, плохие параметры) |
| 403 | Подтверждение purge не совпало (`confirm != "ОЧИСТИТЬ"`) |
| 404 | Объект не найден (`/api/sos/{id}` для несуществующего id) |
| 422 | FastAPI validation (`pydantic`) — типы в body |
| 500 | Внутренняя ошибка (например БД недоступна) |
