# CLAUDE.md — rescue-dashboard

Веб-интерфейс оператора базы спасателей. Открывается прямо на RPi5 в
браузере (`http://localhost:8000/` или с другой машины в LAN). Раздаётся
как статика прямо из `rescue-api` (FastAPI mount StaticFiles на `/`).

## Стек

- **Чистый HTML/CSS/JS** — без сборщика, без npm, без node_modules.
- **Leaflet 1.9.4** — карта (один скрипт + CSS, локально в `lib/`).
- **WebSocket** к `/ws` — push новых PING/SOS из rescue-api.
- **Fetch API** — REST к `/api/*`.

### Почему не React/Vite

На дашборде ~3 списка, одна карта и WebSocket — для этого vanilla хватает
с запасом. Без сборщика проще: открыл `app.js`, видишь живой код, F5 в
браузере перезагружает. Деплой — `cp` файлов, никаких build-артефактов.
Если интерфейс разрастётся (вкладки, формы редактирования инцидентов и
т.п.) — переезд на Vite + React делается за день.

## Структура

```
web/rescue-dashboard/
├── index.html          — разметка: header, sidebar, map, chat-panel
├── style.css           — тёмная тема диспетчерской
├── app.js              — состояние, WS, рендер, маркеры
├── install.sh          — скачивает Leaflet в ./lib/ (вендор не коммитим)
└── lib/                — gitignore: Leaflet 1.9.4
    ├── leaflet.css
    ├── leaflet.js
    └── images/         — marker-icon.png и т.д.
```

## Установка

```bash
# Один раз, чтобы скачать Leaflet:
bash web/rescue-dashboard/install.sh

# rescue-api при `bash install.sh` зовёт этот скрипт автоматически —
# отдельно его обычно запускать не нужно.
```

## Раздача файлов

Происходит из `rescue-api`: `app.mount("/", StaticFiles(directory=…, html=True))`
поднимается ПОСЛЕ всех `/api/*` и `/ws` маршрутов. То есть:
- `GET /`           → `index.html`
- `GET /style.css`  → `style.css`
- `GET /lib/leaflet.js` → `lib/leaflet.js`
- `GET /api/...`    → REST rescue-api (приоритет, т.к. зарегистрирован раньше)
- `WS  /ws`         → WebSocket rescue-api

## Что показывает дашборд

| Зона | Что | Источник |
|-----|-----|----------|
| Шапка | статус WS, счётчики устройств/PING/SOS | `/api/stats` + WS state |
| Левая панель ↑ | открытые SOS-инциденты, мигают красным | `/api/sos?only_open=false`, фильтр `!resolved` |
| Левая панель ↕ | туристы в эфире (PING < 2 мин, без базы) | `/api/tourists` |
| Левая панель ↓ | админ-секция «Очистить БД» (чекбоксы + кнопка) | `POST /api/admin/purge` |
| Карта | маркеры туристов (синие), SOS (красные → оранжевые при ack → зелёные при resolve) | те же эндпоинты + WS |
| Правая панель ↑ | AI-диспетчер: чат с GigaChat | `POST /api/chat`, история в памяти страницы |
| Правая панель ↓ | Чат с туристами: ответы оператора + входящие CHAT-пакеты | `GET/POST /api/messages` + WS `event: chat` |

Клик по элементу списка центрирует карту на устройстве. Маркеры
кликабельны — popup с battery/RSSI/временем.

## WebSocket-протокол (приём)

```json
{"event": "ping", "data": { /* модель Ping из rescue-api */ }}
{"event": "sos",  "data": { /* модель Sos  из rescue-api */ }}
{"event": "chat", "data": { /* модель ChatMessage из rescue-api */ }}
```

Для `ping` / `sos` `app.js` не парсит payload руками: при событии
вызывает `refreshTourists()` или `refreshSos()` — HTTP к агрегирующему
эндпоинту. Ценой одного round-trip (5–10 мс) получаем гарантированно
консистентное состояние «маркер на карте = строка в списке = последняя
запись в БД».

Для `chat` — наоборот, `appendTourMessage(data)` дописывает пузырь
прямо в DOM по одному (нет агрегации, дедуп по `lastTourMsgId`).

## Координаты

API отдаёт координаты как float-градусы в `position: {lat, lon}`. На
проводе LoRa они int×1e6 — конверсия в `rescue_api/models.py::_coord`.
Маркеры с `(0, 0)` (ESP32 без GPS-фикса) не рисуем — `hasFix(pos)` в
`app.js` фильтрует их.

## Карта и тайлы

Дашборд берёт тайлы **локально** через rescue-api:
```js
const TILES_URL = '/tiles/{z}/{x}/{y}.png';
```

rescue-api монтирует `/var/lib/mesh-net/tiles/` (env `TILES_DIR`) на путь
`/tiles`. Файлы туда кладёт `scripts/import_tiles/download_tiles.py` —
один раз перед использованием:

```bash
# Краснодар, 50×50 км, zoom 10-14 (~1500 тайлов, ~25 мин)
python3 scripts/import_tiles/download_tiles.py \
    --bbox 38.7,44.8,39.4,45.3 \
    --zoom 10-14
```

Если тайлов нет (скрипт не запускали) — карта будет серой, маркеры
всё равно рисуются. Это полностью допустимый режим, просто без фона.

Подробности про формат тайлов, объёмы по регионам и переход на свой
tile-сервер для гранта — см. `scripts/import_tiles/CLAUDE.md`.

### Тайлы всего мира — нереально

z0–z19 ≈ 100+ ТБ. Стандартный подход во всех оффлайн-картах
(Maps.me, OsmAnd) — пакет на регион, который оператор скачивает заранее.

## Чат-панель (правая часть, две секции)

**Верхняя секция — AI-диспетчер** (`#chat-form` / `#chat-log`):
- Прокси через `POST /api/chat` к gigachat-agent
- История держится в `chatHistory` и зеркалится в `localStorage`
  (ключ `mesh-ai-chat-history`) — F5 НЕ стирает переписку.
  `initChat()` при старте восстанавливает пузыри из localStorage в DOM
- В `/api/chat` шлём последние `CHAT_HISTORY_LIMIT = 20` ходов (а не
  всю историю — иначе GigaChat упрётся в `max_tokens`). В localStorage
  хранится без ограничений
- Кнопка ✕ чистит массив + localStorage + DOM с подтверждением
  `confirm()`. На сервере истории нет — её стирать нечего

**Нижняя секция — Чат с туристами** (`#tour-chat-form` / `#tour-chat-log`):
- При старте `refreshTourChat()` грузит последние 100 сообщений через
  `GET /api/messages?limit=100`
- WS `event: chat` дописывает новые пузыри `appendTourMessage()` с
  дедупом по `lastTourMsgId`
- Submit формы → `POST /api/messages` → ответ от базы появится в ленте
  через WS (как и сообщения от туристов)
- Лимит на ввод — 48 байт UTF-8 (это размер CHAT-payload в LoRa-пакете).
  Подсчёт через `new TextEncoder().encode(text).length`. Если оператор
  превысил — текстарея краснеет, кнопка disabled, на сервере 400
- Сообщения **от базы** (`device_id === BASE_DEVICE_ID = 1`) рисуются
  иначе: класс `.from-base`, выровнены справа, золотой акцент. Нужно
  оператору сразу видеть свой собственный поток в ленте

`BASE_DEVICE_ID = 1` захардкожен в `app.js`. Должен совпадать с
`NODE_DEVICE_ID` в lora-station и `BASE_DEVICE_ID` в rescue-api.

## Админ-секция (очистка БД)

Чекбоксы для каждой таблицы (`pings`, `sos_events`, `chat_messages`,
`outgoing_chat`, `devices`) + кнопка «Очистить выбранное». Двойное
подтверждение:
1. `confirm()` со списком выбранных таблиц
2. `prompt()` — оператор должен вручную ввести слово `ОЧИСТИТЬ`

Сервер тоже валидирует — `POST /api/admin/purge` принимает только
`confirm: "ОЧИСТИТЬ"` и известные имена таблиц.

После успешной очистки: `tourMarkers` и `sosMarkers` снимаются с карты,
`lastTourMsgId` сбрасывается в 0 (после purge новые id пойдут с 1, без
сброса дашборд бы их фильтровал), вызывается `refreshTourists/Sos/Stats/TourChat`.

## Важные правила

- **Маркер с координатами (0, 0) не рисовать** — это «GPS-фикса нет», не
  «турист на экваторе у Гринвича». В списке такие отображаем серым.
- **WS переподключаться бесконечно** — `rescue-api` могут рестартануть
  через `systemctl restart`, дашборд должен сам подняться (`onclose` →
  `setTimeout(connectWS, 2000)`).
- **Иконки маркеров (marker-icon.png и т.д.)** — обязаны быть в
  `lib/images/`, иначе Leaflet рендерит пустой синий маркер. CSS
  ссылается на них как `url(images/marker-icon.png)` относительно
  `leaflet.css`.
- **Лимит чата 48 байт UTF-8** — это размер LoRa CHAT-payload. Изменишь
  на сервере — обнови `TOUR_CHAT_MAX_BYTES` в `app.js` синхронно.

## Типовые проблемы

| Симптом | Причина | Что делать |
|---|---|---|
| Карта не отображается, серый фон | Нет интернета и тайлы с OSM-CDN не приходят | Перейти на локальный tile-сервер или включить интернет |
| `L is not defined` в консоли | `lib/leaflet.js` не скачан | `bash install.sh` |
| Маркеры — пустые синие квадратики | Нет `lib/images/marker-icon.png` | `bash install.sh` (перекачает) |
| WS постоянно offline | rescue-api не запущен или упал | `sudo systemctl status mesh-rescue-api` |
| Cписок туристов пуст, но в БД данные есть | PING-и старше 10 мин (порог `ACTIVE_THRESHOLD_MIN` в `db.py`) | поправить в lora-station или подождать новый PING |
| `404 /lib/leaflet.css` | rescue-api не примонтировал static dir | проверь логи: `journalctl -u mesh-rescue-api`, должно быть `dashboard mounted at /` |
