# CLAUDE.md — gigachat-agent

ИИ-диспетчер для оператора базы спасателей. FastAPI-сервис на той же
RPi5, что и `rescue-api`. Принимает чат-запросы от дашборда (через
прокси rescue-api), вызывает GigaChat с function calling, инструменты
ходят в rescue-api по HTTP и достают свежие данные из SQLite.

## Стек

- Python 3.11
- **`gigachat` 0.2.0** — официальный SDK Сбера, поддерживает function
  calling и автообновление access_token из Authorization key
- FastAPI 0.128 + uvicorn (как и rescue-api — общий cache pip)
- httpx — клиент к rescue-api в инструментах
- Без БД — состояние только в памяти процесса (один Agent на воркер)

## Структура

```
services/gigachat-agent/
├── gigachat_agent/
│   ├── __init__.py
│   ├── __main__.py     — точка входа: uvicorn.run("gigachat_agent.app:app")
│   ├── app.py          — FastAPI: POST /chat, GET /health, lifespan
│   ├── agent.py        — Agent: function-calling loop поверх SDK
│   ├── tools.py        — TOOL_DEFS + RescueApi (httpx.Client) + dispatch
│   ├── prompts.py      — SYSTEM_PROMPT диспетчера
│   └── config.py       — load_config() из ENV + token-key
├── token-key           — ⚠ в .gitignore. Authorization key из ЛК Сбера
├── requirements.txt
└── install.sh
```

## Авторизация GigaChat — ВАЖНО

GigaChat выдаёт **access_token (JWT) на ~30 минут**. Если положить его
напрямую — сервис будет ломаться каждые полчаса. Поэтому используем
**Authorization key** — длинная base64-строка `client_id:client_secret`,
SDK с ней сам делает OAuth-обмен и обновляет access_token прозрачно.

### Где взять Authorization key

1. https://developers.sber.ru/studio → Мои проекты → твой проект
2. «API-ключи» (или «Авторизационные данные»)
3. Кнопка **«Скопировать ключ»** — даёт длинную base64-строку

### Формат `services/gigachat-agent/token-key`

Поддерживается 3 формата (парсер в `config.py::_load_token_key`):

**1. key=value (рекомендуется)** — как раз то, что отдаёт ЛК:

```
client_id="019d7740-d1f3-73cd-ba3b-7dd65221f184"
scope="GIGACHAT_API_PERS"
Authorization_key="MDE5ZDc3NDAtZDFmMy03M2NkLWJhM2ItN2RkNjUyMjFmMTg0OmQ4..."
```

**2. голая строка** — только Authorization key одной строкой:

```
MDE5ZDc3NDAtZDFmMy03M2NkLWJhM2ItN2RkNjUyMjFmMTg0OmQ4...
```

**3. JSON со старым access_token** (только для отладки, ~30 мин):

```json
{"access_token":"eyJ...","expires_at":1775828035649}
```

В формате (3) сервис стартанёт, но через 30 минут начнёт ругаться
`AuthenticationError`. Для прода — только формат (1) или (2).

### Через ENV

ENV-переменные перетирают файл (удобно для systemd-overrides):

- `GIGACHAT_AUTHORIZATION_KEY` — рекомендуемый
- `GIGACHAT_ACCESS_TOKEN` — fallback (короткоживущий)
- `GIGACHAT_SCOPE` — `GIGACHAT_API_PERS` / `_B2B` / `_CORP`
- `GIGACHAT_MODEL` — `GigaChat` / `GigaChat-Pro` / `GigaChat-Plus`
- `GIGACHAT_TIMEOUT` — секунды на один вызов GigaChat (default 20)
- `GIGACHAT_MAX_ITER` — макс. итераций function calling в одном /chat (default 5)
- `GIGACHAT_TOKEN_FILE` — переопределить путь к token-key
- `RESCUE_API_URL` — URL rescue-api (default `http://127.0.0.1:8000`)
- `GIGACHAT_AGENT_HOST` / `GIGACHAT_AGENT_PORT` — где слушать (default 127.0.0.1:8001)

## Архитектура

```
Дашборд (браузер)
    │
    │ POST /api/chat  {message, history}
    ▼
rescue-api :8000           (прокси, без CORS — single-origin для фронта)
    │
    │ POST /chat  (тот же body)
    ▼
gigachat-agent :8001       (только 127.0.0.1, наружу не светится)
    │
    │ HTTPS
    ▼
api.sberbank.ru/gigachat   (function calling)
    │
    │ обратные вызовы инструментов:
    │   get_active_tourists → GET rescue-api/api/tourists
    │   get_sos_events      → GET rescue-api/api/sos
    │   get_device_track    → GET rescue-api/api/pings?device_id=
    │   get_stats           → GET rescue-api/api/stats
```

## Function calling — 4 инструмента

См. `tools.py::TOOL_DEFS`. Описания пишем подробно — модель решает что
позвать **исходя из текста description** (плюс контекст). Кратко:

| Инструмент | Что делает | rescue-api |
|---|---|---|
| `get_active_tourists` | Кто в эфире (PING < 10 мин) | `GET /api/tourists` |
| `get_sos_events`      | SOS-события (по умолчанию открытые) | `GET /api/sos?only_open=` |
| `get_device_track`    | Трек одного устройства за N часов | `GET /api/pings?device_id=&hours=` |
| `get_stats`           | Общие счётчики системы | `GET /api/stats` |

Цикл в `agent.py::Agent.ask`:

1. `giga.chat(messages + functions)` — модель думает
2. Если `finish_reason == "function_call"` — выполняем `dispatch()`,
   результат шлём обратно как `MessagesRole.FUNCTION`, повторяем
3. Если `finish_reason == "stop"` — берём `message.content`, отдаём
4. Защита: не больше `max_iterations` итераций (default 5).

## API сервиса

| Метод | URL | Body | Ответ |
|---|---|---|---|
| GET  | `/health` | — | `{status, auth_mode, model, scope}` |
| POST | `/chat`   | `{message, history?: [{role,content}, ...]}` | `{reply, tools_used, error?}` |

`error` в ответе — человекочитаемая причина «AI недоступен (...)»:

- `(ключ авторизации не задан, см. token-key)` — пустой `token-key`
- `(ошибка авторизации: ...)` — Authorization key неверный
- `(нет связи с GigaChat: ...)` — нет интернета на RPi5
- `(rescue-api недоступен: ...)` — функция упала на HTTP-запросе

В этих случаях `reply == ""` и дашборд показывает сообщение красным.

## Запуск

### Руками (отладка)

```bash
cd services/gigachat-agent
bash install.sh                  # один раз: создаст .venv и поставит deps

# Положить Authorization key в token-key (см. выше)

source .venv/bin/activate
python -m gigachat_agent

# Проверить что жив:
curl http://127.0.0.1:8001/health
# {"status":"ok","auth_mode":"authorization_key","model":"GigaChat","scope":"GIGACHAT_API_PERS"}

# Прямой вызов /chat (минуя rescue-api):
curl -X POST http://127.0.0.1:8001/chat \
     -H 'Content-Type: application/json' \
     -d '{"message":"Кто сейчас в эфире?","history":[]}'
```

### Через systemd (продакшн)

После `sudo bash scripts/systemd/install.sh` сервис стартует автоматически
(после `mesh-rescue-api`, см. `After=` в юните):

```bash
sudo systemctl status mesh-gigachat-agent
sudo journalctl -u mesh-gigachat-agent -f
sudo systemctl restart mesh-gigachat-agent
```

⚠ **Не запускать systemd-копию и `python -m gigachat_agent` руками одновременно** —
обе будут биться за `127.0.0.1:8001`.

## Важные правила

- **Authorization key — только в `token-key` или ENV**, никогда не в коде.
  Файл уже в `.gitignore` через `**/token-key`.
- **`verify_ssl_certs=False`** в SDK — у Сбера свой root CA. Для
  дев-окружения проще выключить проверку. На полноценном проде
  поставить корневой сертификат Сбера и включить.
- **Ничего не кешируем** — данные должны быть актуальными. На каждый
  /chat все инструменты ходят свежим запросом в rescue-api.
- **Системный промпт** в `prompts.py` определяет тон — короткий,
  фактический, без «Конечно, я могу помочь». Если меняешь — проверь
  что модель не стала разговорчивее.
- **History limit** — дашборд шлёт max 20 последних ходов. SDK сам не
  обрезает контекст, GigaChat отвалится по `max_tokens`. Если
  понадобится длинный диалог — добавить summarization.

## Типовые проблемы

| Симптом | Причина | Что делать |
|---|---|---|
| `auth_mode=none` в /health | пустой `token-key` | положить Authorization key |
| `AI недоступен (ошибка авторизации: ...)` | неверный ключ или неправильный `scope` | проверь скоп: PERS для физлиц |
| `AI недоступен (нет связи с GigaChat: ...)` | нет интернета / DNS / SSL | `curl https://gigachat.devices.sberbank.ru` с RPi5 |
| `AI недоступен (rescue-api недоступен)` | rescue-api упал | `sudo systemctl status mesh-rescue-api` |
| Модель отвечает по памяти, не вызывает функции | плохой промпт инструмента | расширить description в `TOOL_DEFS` |
| `превышен лимит итераций function calling` | модель ходит по кругу | поднять `GIGACHAT_MAX_ITER` или починить промпт |
| `address already in use :8001` | systemd-копия уже работает | `sudo systemctl stop mesh-gigachat-agent` |
