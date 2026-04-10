# CLAUDE.md — relay-node

Упрощённый сервис для инфо-точки: принимает пакеты от `lora-station`, ретранслирует в mesh, предоставляет минимальный API для captive portal (SOS кнопка, статус).

## Отличия от rescue-api

- **Нет** GigaChat, нет карты спасателей
- **Нет** сохранения всех треков (только последняя позиция каждого устройства в памяти)
- **Есть** маленький FastAPI для `info-portal` (SOS, статус батареи, статус сети)

## Структура

```
app/
  main.py        — FastAPI, /sos, /status
  state.py       — in-memory state (последние позиции, статус)
  battery.py     — чтение уровня заряда (GPIO / INA219)
```

## Запуск

```bash
source .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8002
```

## API (только для info-portal)

```
POST /sos              — отправить SOS через lora-station
GET  /status           — статус точки (battery, uptime, last_rssi)
GET  /devices/nearby   — устройства, видимые в радиусе (из in-memory state)
```

## Переменные окружения

| Переменная | Default | Описание |
|------------|---------|----------|
| `LORA_STATION_URL` | `http://127.0.0.1:8003` | URL lora-station TX endpoint |
| `NODE_DEVICE_ID` | — | **Обязательно.** ID этой инфо-точки |
| `NODE_NAME` | `"Инфо-точка"` | Название точки (показывается на портале) |
| `DB_PATH` | — | Если задан — сохранять события в SQLite |
