# CLAUDE.md — info-portal

Статический captive Wi-Fi портал для инфо-точек. Открывается автоматически при подключении смартфона к Wi-Fi точки. Никаких зависимостей от npm/build-системы — только чистый HTML/CSS/JS.

## Ограничения

- **Статика**: никакого Node.js, никакого build-шага
- **Совместимость**: iOS 13+, Android 8+, включая режим captive portal (ограниченный браузер)
- **Размер**: весь портал < 5 МБ (включая офлайн-карту и фото)
- Без внешних шрифтов и CDN — всё локально

## Структура

```
info-portal/
  index.html          — главная страница (карта, контент, SOS)
  style.css
  app.js              — SOS-кнопка, загрузка контента
  map/
    map.js            — Leaflet (локальная копия)
    leaflet.css
    tiles/            — офлайн тайлы для этого участка маршрута
  content/
    location.json     — описание локации, достопримечательности
    photos/           — фото локации (JPEG, max 200 КБ каждое)
  audio/
    sos-confirm.mp3   — звук подтверждения SOS
```

## SOS-кнопка (критически важно)

```js
// app.js
async function sendSOS() {
    const confirmed = confirm("Отправить сигнал SOS? Спасатели будут оповещены.");
    if (!confirmed) return;

    try {
        const resp = await fetch('/api/sos', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({payload: "SOS с инфо-точки"})
        });
        // relay-node обработает и отправит по LoRa
        showSOSSent();
    } catch (e) {
        showSOSError();
    }
}
```

## Контент локации (content/location.json)

```json
{
  "name": "Ущелье Архыз — Стоянка №3",
  "elevation": 2150,
  "description": "...",
  "pois": [
    {"name": "Водопад Кизгыч", "lat": 43.45, "lon": 41.21, "distance_m": 800},
    {"name": "Следующая стоянка", "lat": 43.47, "lon": 41.25, "distance_m": 3200}
  ],
  "warnings": ["Переход через реку — только при сухой погоде"],
  "emergency_contacts": "Спасатели: через SOS-кнопку"
}
```

## Что должна показывать главная страница

1. Название локации и высота
2. Мини-карта с текущей точкой и ближайшими POI
3. Описание достопримечательностей с фото
4. Предупреждения и важная информация
5. Кнопка SOS (всегда видна, внизу страницы)
6. Информация о маршруте (следующая стоянка, расстояние)

## Важные правила

- Кнопка SOS должна работать даже если JS-карта не загрузилась
- Все фото через `<img loading="lazy">` чтобы не тормозить открытие
- Captive portal часто открывается без адресной строки — не использовать `history.pushState`
- Тестировать на реальном iOS (Safari WebView в captive portal очень ограничен)
