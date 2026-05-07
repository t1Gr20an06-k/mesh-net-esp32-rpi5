# CLAUDE.md — rescue-api

REST + WebSocket-сервис на FastAPI. Лежит на RPi5 базы спасателей,
читает ту же `mesh.db`, что и `lora-station`. Отдаёт данные дашборду
(шаг 3) и AI-диспетчеру `gigachat-agent` (шаг 4).

Сервис пассивный: не получает пакеты от lora-station напрямую и не
шлёт ему команды. Связь только через общий SQLite-файл.

## Стек

- Python 3.11
- **FastAPI 0.128 + uvicorn[standard]** — async-фреймворк, авто-Swagger
- Стандартный `sqlite3` (без ORM, без aiosqlite)
- Стандартный `logging`
- Без авторизации — сервис локальный, на самом RPi5

### Почему sync sqlite3, а не aiosqlite

FastAPI выполняет sync-эндпоинты (`def …`) в threadpool автоматически,
SQLite-вызовы там не блокируют event loop. На нашем трафике (1 PING/10 сек,
несколько подключений к WS) разница незаметна, а зависимостей и ловушек
с `async with conn:` меньше.

## Структура

```
services/rescue-api/
├── rescue_api/
│   ├── __init__.py
│   ├── __main__.py      — точка входа: uvicorn.run("rescue_api.app:app", …)
│   ├── app.py           — FastAPI: REST-эндпоинты, /ws, lifespan
│   ├── db.py            — обёртка sqlite3: db_read / db_write + запросы
│   ├── models.py        — Pydantic-модели ответов (Position/Tourist/Ping/Sos/Stats)
│   └── ws.py            — Broadcaster: poll БД раз в секунду, push клиентам
├── requirements.txt
└── install.sh
```

## Запуск

### Руками
```bash
cd services/rescue-api
bash install.sh                  # один раз: создаст .venv и поставит deps
source .venv/bin/activate
python -m rescue_api
```

### Через systemd (продакшн на RPi5)

После `sudo bash scripts/systemd/install.sh` сервис стартует автоматически
при включении RPi5 (после `mesh-lora-station`, см. `After=` в юните).

```bash
# Статус
sudo systemctl status mesh-rescue-api

# Live-логи
sudo journalctl -u mesh-rescue-api -f

# Рестарт после `git pull` или правки кода
sudo systemctl restart mesh-rescue-api

# Остановить, не трогая autostart
sudo systemctl stop mesh-rescue-api

# Полностью отключить
sudo systemctl disable --now mesh-rescue-api
```

⚠ **Не запускать systemd-копию и `python -m rescue_api` руками одновременно** —
обе будут биться за порт `8000` (см. `RESCUE_API_PORT`), вторая упадёт
с `address already in use`. Перед ручным запуском: `sudo systemctl stop mesh-rescue-api`.

ENV редактируется в `/etc/systemd/system/mesh-rescue-api.service` или в
шаблоне `scripts/systemd/mesh-rescue-api.service` + `sudo bash scripts/systemd/install.sh mesh-rescue-api`.
После правки — `sudo systemctl daemon-reload && sudo systemctl restart mesh-rescue-api`.

## Эндпоинты

| Метод | URL | Что делает |
|------:|-----|-----------|
| GET   | `/`                          | `index.html` дашборда (если `DASHBOARD_DIR` существует) |
| GET   | `/style.css`, `/app.js`, `/lib/...` | статика дашборда |
| GET   | `/tiles/{z}/{x}/{y}.png`     | оффлайн-тайлы карты (если `TILES_DIR` существует) |
| GET   | `/api/health`                | живой ли сервис |
| GET   | `/api/stats`                 | счётчики (всего PING/SOS, активных устройств) |
| GET   | `/api/tourists`              | кто сейчас активен (PING за последние 10 мин) |
| GET   | `/api/devices`               | весь реестр устройств |
| GET   | `/api/pings?device_id=&hours=&limit=` | трек одного или всех |
| GET   | `/api/sos?only_open=true`    | SOS-события |
| GET   | `/api/sos/{id}`              | один SOS |
| POST  | `/api/sos/{id}/ack`          | подтвердить SOS, body `{"acked_by": <device_id>}` |
| POST  | `/api/sos/{id}/resolve`      | закрыть инцидент, body `{"notes": "..."}` |
| POST  | `/api/chat`                  | прокси к `gigachat-agent` (`http://127.0.0.1:8001/chat`), body `{message, history}` |
| WS    | `/ws`                        | push-канал, см. ниже |

Авто-документация Swagger UI: `http://<rpi5-ip>:8000/docs`

## WebSocket /ws

Push-only канал. После `connect()` сервер шлёт JSON-сообщения:

```json
{"event": "ping", "data": { /* модель Ping */ }}
{"event": "sos",  "data": { /* модель Sos  */ }}
```

Реализация: [`ws.py::Broadcaster`](rescue_api/ws.py) держит набор
подключённых WebSocket'ов и фоновую asyncio-задачу, которая раз в
секунду вычитывает из SQLite строки с `id > last_seen_id` и рассылает
их всем клиентам. Стартовая точка `last_seen_id` = `MAX(id)` на момент
запуска rescue-api — старые записи в WS не пушим, иначе дашборд при
открытии получит несколько тысяч PING-ов разом.

Один сервер — один Broadcaster — несколько подключений. Несколько
вкладок дашборда нормально работают параллельно.

## Статика дашборда

`app.py` в самом конце делает `app.mount("/", StaticFiles(directory=…))`.
Каталог по умолчанию — `web/rescue-dashboard/` (вычисляется относительно
`app.py`), переопределяется переменной `DASHBOARD_DIR`.

Mount именно на `/` (а не на `/dashboard`): чтобы оператор открыл
`http://<rpi5-ip>:8000` и сразу видел карту. Все `/api/*` и `/ws`
зарегистрированы декораторами **выше** в файле — у них приоритет, так
что `/api/health` не уйдёт в StaticFiles. То же с автогенеренными
`/docs` и `/openapi.json`.

Сами файлы дашборда раздаются как есть; `bash services/rescue-api/install.sh`
перед сборкой venv ещё дёргает `web/rescue-dashboard/install.sh` —
он скачивает Leaflet 1.9.4 в `web/rescue-dashboard/lib/`. Без этого
шага на `/` будет HTML, но без карты (`L is not defined` в консоли).

## Координаты

В БД `latitude` / `longitude` хранятся как `INTEGER × 1e6` (формат
LoRa-пакета). Наружу отдаём в float-градусах через `Position {lat, lon}`.

Для пакетов от ESP32 без GPS-фикса координаты = `(0, 0)`. Дашборд
сам решает, как это рисовать (например, не показывать на карте, но
в списке выводить значок «GPS нет»).

## Архитектура связи с lora-station

```
lora-station ─── пишет PING/SOS ──> SQLite (WAL) <─── читает rescue-api
                                          ↑                    │
                                          └── WS poller ───────┘
```

WAL-режим включил lora-station ещё в `init.sql`. SQLite разрешает
один писатель + сколько угодно читателей без блокировок. rescue-api
открывает БД read-only (`?mode=ro`) — гарантия что REST случайно не
напишет в чужие таблицы. Read-write только в эндпоинтах ack/resolve.

Polling SQLite раз в секунду — компромисс. Для 1 PING / 10 сек это
копейки (~1.5% CPU на одно чтение MAX(id)), задержка до пуша в WS до
1 сек что для SOS более чем достаточно (3 пакета × 500 мс = 1.5 сек,
система всё равно увидит первый из трёх). Если упрёмся — переедем на
inotify по wal-файлу или UNIX socket из lora-station.

## Важные правила

- **SOS никогда не удалять** — только `acked` / `resolved`. Юридически
  данные о ЧС хранить обязательно (см. `Docs/database.md`)
- **Координаты на проводе int×1e6** — конверсия в `models.py::_coord`,
  не дублировать в эндпоинтах
- **CORS open** для разработки (см. `ALLOW_CORS`). Перед выкаткой в
  прод закрыть до доменов дашборда
- **Никаких блокирующих операций в async-эндпоинтах** — у нас все
  REST-эндпоинты `def`, они идут в threadpool, проблем нет; но если
  кто-то добавит `async def` с прямым `sqlite3` — вылезет блокировка
  event loop

## Переменные окружения

| Переменная | Default | Описание |
|------------|---------|----------|
| `DB_PATH` | `/var/lib/mesh-net/mesh.db` | путь к SQLite |
| `RESCUE_API_HOST` | `0.0.0.0` | интерфейс (0.0.0.0 — все, 127.0.0.1 — только localhost) |
| `RESCUE_API_PORT` | `8000` | порт |
| `LOG_LEVEL` | `INFO` | уровень логов |
| `ALLOW_CORS` | `1` | разрешить CORS (для dashboard на другом порту, для разработки) |
| `DASHBOARD_DIR` | `<repo>/web/rescue-dashboard` | каталог статики дашборда (mount на `/`) |
| `TILES_DIR` | `/var/lib/mesh-net/tiles` | каталог оффлайн-тайлов карты (mount на `/tiles`) |
| `GIGACHAT_AGENT_URL` | `http://127.0.0.1:8001` | URL gigachat-agent (прокси `POST /api/chat`) |
| `GIGACHAT_AGENT_TIMEOUT` | `25` | секунды на один запрос к gigachat-agent |

## Проверка работоспособности

```bash
# 1. Сервис отвечает
curl http://localhost:8000/api/health
# {"status":"ok"}

# 2. Статистика
curl http://localhost:8000/api/stats
# {"pings_total":..., "sos_total":..., ...}

# 3. Кто сейчас активен
curl http://localhost:8000/api/tourists | python3 -m json.tool

# 4. Swagger UI в браузере
# http://<rpi5-ip>:8000/docs

# 5. Пустить WebSocket руками (нужен wscat или python script)
python3 -c "
import asyncio, websockets, json
async def go():
    async with websockets.connect('ws://localhost:8000/ws') as ws:
        while True:
            msg = await ws.recv()
            print(json.loads(msg))
asyncio.run(go())
"
```

## Типовые проблемы

| Симптом | Причина | Что делать |
|---|---|---|
| `Could not open database` | БД не создана | `bash scripts/db_init/init.sh` или `cd ../lora-station && bash install.sh` |
| `attempt to write a readonly database` | через ack/resolve пытаемся писать в read-only conn | проверь, что используется `db_write` (а не `db_read`) — это бага в коде |
| Дашборд не видит новых PING-ов в /ws | poller не стартовал или БД не открылась | `journalctl -u mesh-rescue-api`, должен быть лог `WS poller старт: last_ping_id=...` |
| 200 на /api/health, но 500 на /api/stats | повреждена БД (редко) | `sqlite3 mesh.db 'PRAGMA integrity_check;'` |
| `address already in use :8000` | другой сервис занял порт | `sudo ss -tlnp \| grep :8000`, поменяй `RESCUE_API_PORT` |
