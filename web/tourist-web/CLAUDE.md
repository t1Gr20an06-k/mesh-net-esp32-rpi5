# CLAUDE.md — tourist-web

Мобильный веб-интерфейс туриста. Раздаётся ESP32 через Wi-Fi AP, открывается в браузере смартфона.

## Технические ограничения (критически важно!)

- Раздаётся **ESP32 SPIFFS** — максимум ~1.4 МБ на все файлы
- **Нет Node.js/npm на ESP32** — только статические файлы (HTML/CSS/JS)
- Минимизировать зависимости: никаких тяжёлых фреймворков
- JS бандл после минификации: **не более 100 КБ**
- Работает на медленном WebSocket через Wi-Fi AP ESP32

## Стек

- **Vanilla JS** или Preact (< 4 КБ) — никаких React/Vue
- CSS без препроцессоров (чистый CSS или минимальный Tailwind CDN)
- WebSocket: `ws://192.168.4.1:81`

## Структура src/

```
src/
  index.html     — единственная страница (SPA)
  app.js         — WebSocket клиент, UI логика
  map.js         — простая SVG-карта (не Leaflet — слишком тяжёлый)
  style.css
```

## GPS — берём из браузера телефона (Geolocation API)

ESP32 **не имеет GPS-модуля**. Координаты определяет смартфон через браузерный `navigator.geolocation` и отправляет на ESP32 по WebSocket. ESP32 упаковывает их в LoRa-пакет.

```js
// gps.js — непрерывное слежение за позицией
navigator.geolocation.watchPosition(
    (pos) => {
        ws.send(JSON.stringify({
            type: "gps",
            lat: pos.coords.latitude,
            lon: pos.coords.longitude,
            accuracy: pos.coords.accuracy,   // метры
            ts: Math.floor(pos.timestamp / 1000)
        }));
    },
    (err) => showGpsError(err.code),         // 1=denied, 2=unavailable, 3=timeout
    {
        enableHighAccuracy: true,
        maximumAge: 10000,   // принять кэш не старше 10 сек
        timeout: 15000
    }
);
```

**Важно:** браузер запрашивает разрешение на геолокацию. Если пользователь отказал — показать инструкцию как включить, SOS отправляется без координат (lat=0, lon=0, esp32 ставит флаг no_gps=1).

**Периодичность:** отправлять GPS на ESP32 каждые **30 секунд** (или при изменении позиции > 20 м) — экономия батареи телефона.

## WebSocket протокол (ESP32 ↔ browser)

```js
// Browser → ESP32
ws.send(JSON.stringify({type: "gps", lat: 43.355, lon: 42.514, accuracy: 8, ts: 1700001234}))
ws.send(JSON.stringify({type: "chat", text: "Все в порядке"}))
ws.send(JSON.stringify({type: "sos"}))
ws.send(JSON.stringify({type: "sos", text: "Травма ноги"}))  // SOS с описанием

// ESP32 → Browser
// {type: "ping", device_id: 71, lat: 43.355, lon: 42.514, name: "Иванов"}  — входящий от другого участника
// {type: "chat", device_id: 71, text: "Мы на перевале", ts: 1700001234}
// {type: "sos_ack", device_id: 71}
// {type: "gps_ack", ts: 1700001234}   — ESP32 подтвердил, что упаковал и отправил пакет
// {type: "status", battery: 87, rssi: -85, packets_sent: 42}
```

## UX требования

- **GPS-индикатор** — показывать точность в метрах (зелёный < 20м, жёлтый < 50м, красный > 50м или нет фикса)
- Кнопка SOS — большая, красная, с подтверждением ("Вы уверены?")
- При нажатии SOS — сразу запросить свежую позицию (`getCurrentPosition`) перед отправкой
- Список участников группы с временем последнего пинга
- Мини-карта (SVG) с маркерами устройств — только если GPS данные есть
- Индикатор: есть ли соединение с ESP32 (WebSocket статус)
- Адаптивный дизайн — только мобильный (min-width: нет)
- Предупреждение если геолокация отключена или точность плохая (> 100 м)

## Сборка

```bash
# Минификация для SPIFFS
npm run build
# Результат в dist/ — скопировать в firmware/esp32-terminal/data/
cp -r dist/* ../firmware/esp32-terminal/data/

# Загрузить на ESP32
cd ../firmware/esp32-terminal
pio run -t uploadfs
```

## Важные правила

- **Никаких внешних запросов** — всё через WebSocket к ESP32
- При потере WebSocket — автоматически переподключаться каждые 3 секунды
- Показывать пользователю статус соединения (подключён / нет соединения)
- Сохранять последние 20 сообщений чата в `sessionStorage`
