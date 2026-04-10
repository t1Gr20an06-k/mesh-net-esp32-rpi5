# CLAUDE.md — rescue-api

FastAPI REST API базы спасателей. Единственный источник данных для `rescue-dashboard` и `gigachat-agent`.

## Стек

- **Python 3.11**, FastAPI, SQLite (через `aiosqlite`)
- WebSocket для real-time событий дашборда
- Uvicorn в продакшне

## Структура

```
app/
  main.py          — FastAPI app, роутеры, WebSocket manager
  models.py        — Pydantic-модели запросов/ответов
  db.py            — aiosqlite-соединение, вспомогательные функции
  routers/
    devices.py     — /devices
    sos.py         — /sos
    stats.py       — /stats
  ws_manager.py    — менеджер WebSocket-соединений (broadcast)
```

## Запуск

```bash
source .venv/bin/activate
DB_PATH=/var/lib/mesh-net/mesh.db uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Важные правила

- **Все запросы к БД — async** через `aiosqlite`. Никаких синхронных `sqlite3` вызовов в async-контексте
- При получении нового пакета от `lora-station` (через внутренний HTTP или очередь) — вызывать `ws_manager.broadcast()` для рассылки дашборду
- SOS-события никогда не удалять — только помечать `acknowledged=1`
- `GET /devices` возвращает `online: true` только если последний трек < 600 секунд назад
- WebSocket `/ws` — клиент может подключаться несколько раз (несколько вкладок дашборда)

## Тесты

```bash
pytest tests/
# или конкретно
pytest tests/test_sos.py -v
```

## Переменные окружения

| Переменная | Default | Описание |
|------------|---------|----------|
| `DB_PATH` | `/var/lib/mesh-net/mesh.db` | Путь к SQLite |
| `HOST` | `0.0.0.0` | Bind host |
| `PORT` | `8000` | Bind port |
| `LORA_STATION_URL` | `http://127.0.0.1:8003` | URL lora-station для команд TX |

## Пример добавления нового эндпоинта

```python
# app/routers/messages.py
from fastapi import APIRouter
from app.db import get_db
from app.models import MessageResponse

router = APIRouter(prefix="/messages", tags=["messages"])

@router.get("/", response_model=list[MessageResponse])
async def get_messages(channel: int = 0, limit: int = 50):
    async with get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT * FROM messages WHERE channel=? ORDER BY ts DESC LIMIT ?",
            (channel, limit)
        )
    return [MessageResponse(**dict(r)) for r in rows]
```

Зарегистрировать в `main.py`: `app.include_router(messages.router)`
