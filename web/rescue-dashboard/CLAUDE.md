# CLAUDE.md — rescue-dashboard

React-дашборд для спасателей. Работает в браузере на ноутбуке/планшете в локальной сети базы. Офлайн — все тайлы карт предзагружены.

## Стек

- **React 18** + Vite
- **Leaflet.js** — карта с офлайн-тайлами
- **WebSocket** — real-time события от `rescue-api`
- **Tailwind CSS** (CDN или bundled)

## Структура src/

```
src/
  main.jsx
  App.jsx
  components/
    Map.jsx          — Leaflet карта, маркеры устройств
    SosAlerts.jsx    — панель активных SOS (звук + подсветка)
    DeviceList.jsx   — список устройств с треками
    AiChat.jsx       — чат с GigaChat (через gigachat-agent)
    StatusBar.jsx    — статус соединения, время последнего пакета
  hooks/
    useWebSocket.js  — WS подключение к rescue-api /ws
    useDevices.js    — state устройств
    useSos.js        — state SOS-событий
  api.js             — HTTP запросы к rescue-api
```

## Карта (Leaflet + офлайн тайлы)

```jsx
// Тайлы загружены в public/tiles/{z}/{x}/{y}.png
// scripts/import_tiles/download_tiles.py загружает их заранее
const tileUrl = '/tiles/{z}/{x}/{y}.png';

// Центр карты и зум — настраивается под конкретный маршрут
const MAP_CENTER = [43.45, 41.20];  // Архыз
const MAP_ZOOM = 13;
```

## WebSocket события

```js
// useWebSocket.js слушает rescue-api WS
// При событии "sos" — играть звук + показать alert
// При событии "ping" — обновить маркер на карте
```

## SOS звук

```js
// public/sos-alert.mp3 — короткий сигнал тревоги
// Воспроизводить при каждом новом SOS-событии
// Требует взаимодействия пользователя для первого воспроизведения (autoplay policy)
```

## Запуск (dev)

```bash
npm install
VITE_API_URL=http://localhost:8000 npm run dev
```

## Сборка (prod)

```bash
npm run build
# Результат в dist/ — nginx раздаёт из этой папки
```

## Переменные окружения (Vite)

```
VITE_API_URL=http://localhost:8000      # rescue-api
VITE_AI_URL=http://localhost:8001       # gigachat-agent
VITE_MAP_CENTER=43.45,41.20             # lat,lon
VITE_MAP_ZOOM=13
```

## Важные правила

- **Офлайн-first**: карта должна работать полностью без интернета
- При недоступности `rescue-api` — показывать banner "Нет соединения с сервером"
- SOS-маркеры — красные, мигающие, кликабельны (открывают панель подтверждения)
- Обычные маркеры — зелёные (online < 10 мин) / серые (offline)
- При ack SOS — маркер меняется на синий, звук прекращается
- Компонент `AiChat` — не блокировать UI при ожидании ответа GigaChat (индикатор загрузки)
