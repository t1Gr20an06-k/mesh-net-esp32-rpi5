# API Reference

## rescue-api (порт 8000)

Base URL: `http://<rescue-base-ip>:8000` или `http://localhost:8000`

---

### GET /devices

Список всех зарегистрированных устройств.

**Response:**
```json
[
  {
    "device_id": 71,
    "name": "Иванов П.",
    "type": "tourist",
    "registered": 1700000000,
    "last_seen": 1700001234,
    "last_lat": 43.355123,
    "last_lon": 42.514567,
    "online": true
  }
]
```

`online = true` если последний PING < 10 минут назад.

---

### GET /devices/{device_id}/track

GPS-трек устройства.

**Query params:**
- `hours` (int, default=24) — за последние N часов

**Response:**
```json
{
  "device_id": 71,
  "name": "Иванов П.",
  "points": [
    {"lat": 43.355, "lon": 42.514, "ts": 1700001000, "rssi": -85},
    {"lat": 43.356, "lon": 42.515, "ts": 1700001060, "rssi": -82}
  ]
}
```

---

### GET /sos

Список SOS-событий.

**Query params:**
- `active_only` (bool, default=true) — только неподтверждённые

**Response:**
```json
[
  {
    "id": 1,
    "device_id": 71,
    "name": "Иванов П.",
    "lat": 43.355,
    "lon": 42.514,
    "ts": 1700001000,
    "payload": "Травма ноги",
    "acknowledged": false,
    "minutes_ago": 14
  }
]
```

---

### POST /sos/{sos_id}/ack

Подтвердить SOS (помощь выслана).

**Body:**
```json
{"rescuer_device_id": 256}
```

**Response:** `{"status": "ok"}`

---

### GET /stats

Общая статистика маршрута.

**Response:**
```json
{
  "active_tourists": 7,
  "active_sos": 1,
  "pings_last_hour": 42,
  "last_ping_ts": 1700001234,
  "total_devices": 12
}
```

---

### POST /devices

Зарегистрировать новое устройство.

**Body:**
```json
{"device_id": 72, "name": "Петрова А.", "type": "tourist"}
```

---

### WebSocket /ws

Real-time события для дашборда.

**Подключение:** `ws://<host>/ws`

**События (JSON):**

```json
// Новый PING
{"event": "ping", "device_id": 71, "lat": 43.355, "lon": 42.514, "ts": 1700001300}

// SOS
{"event": "sos", "device_id": 71, "lat": 43.355, "lon": 42.514, "ts": 1700001400, "payload": "Травма"}

// ACK SOS
{"event": "sos_ack", "sos_id": 1, "device_id": 71}

// Новое сообщение чата
{"event": "chat", "device_id": 71, "channel": 0, "payload": "Все в порядке", "ts": 1700001350}
```

---

## gigachat-agent (порт 8001)

### POST /ask

Текстовый запрос к GigaChat AI.

**Body:**
```json
{"question": "Сколько туристов сейчас на маршруте?"}
```

**Response:**
```json
{
  "answer": "На маршруте 7 активных туристов. Последний сигнал получен 3 минуты назад от устройства #047 (Сидоров В.).",
  "tool_calls": ["get_tourists", "get_stats"],
  "latency_ms": 1240
}
```

**Доступные function-calling инструменты:**

| Функция | Описание |
|---------|----------|
| `get_tourists()` | Список активных туристов с координатами |
| `get_sos()` | Активные SOS-сигналы |
| `get_location(device_id: int)` | Последние координаты устройства |
| `get_stats()` | Общая статистика маршрута |

---

## relay-node API (порт 8002, только на инфо-точке)

### POST /sos

Отправить SOS с инфо-точки (через кнопку на портале).

**Body:**
```json
{"payload": "Нужна помощь у информационной точки"}
```

**Response:** `{"status": "sent", "packet_id": "abc123"}`

### GET /status

Статус инфо-точки: заряд батареи, RSSI последнего пакета, uptime.

```json
{
  "uptime_seconds": 86400,
  "battery_percent": 78,
  "last_rssi": -91,
  "packets_relayed_today": 234
}
```

---

## Коды ошибок

| Код | Описание |
|-----|----------|
| 200 | OK |
| 404 | Устройство / событие не найдено |
| 422 | Ошибка валидации параметров |
| 503 | GigaChat API недоступен (только для /ask) |
