# CLAUDE.md — gigachat-agent

FastAPI-сервис, проксирующий вопросы оператора к GigaChat API с function calling. Все инструменты запрашивают данные из локальной SQLite через `rescue-api`.

## Стек

- **Python 3.11**, FastAPI, `gigachat` SDK (Sber), `httpx`
- GigaChat API: `https://gigachat.devices.sberbank.ru/api/v1`

## Структура

```
app/
  main.py        — FastAPI app, эндпоинт /ask
  agent.py       — логика GigaChat + function calling loop
  tools.py       — реализация инструментов (вызовы rescue-api)
  prompts.py     — системный промпт диспетчера
```

## Системный промпт

Находится в `app/prompts.py`. Контекст:

```
Ты — ИИ-диспетчер горного спасательного отряда. У тебя есть доступ к 
реальным данным системы слежения за туристами. Отвечай кратко и точно 
на русском языке. Используй доступные инструменты для получения актуальных данных.
```

## Function calling инструменты

```python
# tools.py — все вызовы идут в rescue-api по HTTP

async def get_tourists() -> list[dict]:
    """Возвращает активных туристов с координатами и временем последнего пинга"""
    
async def get_sos() -> list[dict]:
    """Возвращает активные (неподтверждённые) SOS-сигналы"""

async def get_location(device_id: int) -> dict:
    """Возвращает последние GPS-координаты конкретного устройства"""

async def get_stats() -> dict:
    """Общая статистика: кол-во туристов, активные SOS, пинги за час"""
```

## Запуск

```bash
source .venv/bin/activate
GIGACHAT_TOKEN=<token> RESCUE_API_URL=http://127.0.0.1:8000 \
  uvicorn app.main:app --host 127.0.0.1 --port 8001
```

## Важные правила

- **Токен GigaChat** — только из переменной окружения, никогда не хардкодить
- **Fallback**: если GigaChat API недоступен (нет интернета на базе), вернуть ответ-заглушку с данными из инструментов напрямую, без AI-обработки
- **Таймаут** на вызов GigaChat: 15 секунд. Если превышен — вернуть данные без AI
- Логировать все вызовы инструментов (какие функции были вызваны)
- **Не кэшировать** ответы — данные должны быть актуальными

## Переменные окружения

| Переменная | Default | Описание |
|------------|---------|----------|
| `GIGACHAT_TOKEN` | — | **Обязательно.** OAuth2 токен |
| `RESCUE_API_URL` | `http://127.0.0.1:8000` | URL rescue-api |
| `GIGACHAT_MODEL` | `GigaChat` | Модель (GigaChat / GigaChat-Plus) |
| `GIGACHAT_TIMEOUT` | `15` | Таймаут запроса в секундах |

## Пример добавления нового инструмента

```python
# 1. В tools.py
async def get_weather() -> dict:
    """Данные с погодного датчика инфо-точки"""
    resp = await httpx_client.get(f"{RESCUE_API_URL}/weather")
    return resp.json()

# 2. В agent.py — добавить в список tools для GigaChat
{
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Текущая погода на маршруте с датчика инфо-точки",
        "parameters": {"type": "object", "properties": {}, "required": []}
    }
}

# 3. В agent.py — добавить в dispatch (match tool_name)
case "get_weather":
    result = await tools.get_weather()
```
