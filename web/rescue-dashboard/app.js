/* ===========================================================================
 * Mesh-net Тропы — клиентский код дашборда базы спасателей
 *
 * Зачем один файл: ~250 строк, ES-модули в браузере без сборщика требуют
 * настройки CORS даже на localhost, а через обычный <script src=> — работает
 * сразу. Структура сверху вниз: конфиг → состояние → утилиты → рендер →
 * WebSocket → init.
 *
 * Источники данных:
 *   GET /api/tourists      — кто сейчас активен (PING < 10 мин)
 *   GET /api/sos           — все SOS (открытые + закрытые)
 *   GET /api/stats         — счётчики для шапки
 *   WS  /ws                — push новых ping/sos из БД
 * =========================================================================== */

// --- Конфигурация ----------------------------------------------------------

// Источник тайлов: оффлайн-кеш в rescue-api (StaticFiles на /tiles).
// Сами тайлы скачиваются один раз через scripts/import_tiles/download_tiles.py
// и лежат в /var/lib/mesh-net/tiles/. Без интернета карта работает.
//
// Если тайлов нет (не запускал скрипт скачки) — будет серый фон, маркеры
// всё равно отрисуются.
//
// Для отладки можно временно вернуться на OSM-CDN, раскомментировав вторую строку:
const TILES_URL = '/tiles/{z}/{x}/{y}.png';
// const TILES_URL = 'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png';
const TILES_ATTRIBUTION = '© <a href="https://www.openstreetmap.org/" target="_blank">OpenStreetMap</a>';

// Стартовая точка карты (Москва). При наличии данных init() центрирует на
// первого активного туриста — так что эти координаты заметны только пока
// в БД пусто.
const DEFAULT_CENTER = [55.75, 37.62];
const DEFAULT_ZOOM   = 5;

// --- Состояние приложения --------------------------------------------------

const map = L.map('map').setView(DEFAULT_CENTER, DEFAULT_ZOOM);
L.tileLayer(TILES_URL, { attribution: TILES_ATTRIBUTION, maxZoom: 19 }).addTo(map);

const tourMarkers = new Map();   // device_id -> L.marker
const sosMarkers  = new Map();   // sos_id    -> L.circleMarker

let touristsCache = [];
let sosCache = [];

let ws = null;
let wsReconnectTimer = null;

// --- Утилиты ---------------------------------------------------------------

const $ = (sel) => document.querySelector(sel);

function setWsStatus(state) {
    const el = $('#ws-status');
    el.className = state === 'online' ? 'status-online' : 'status-offline';
    el.textContent = `WS: ${state}`;
}

function fmtTime(iso) {
    if (!iso) return '—';
    // ISO от sqlite вида "2026-05-06T12:34:56" — берём только HH:MM:SS
    return iso.replace('T', ' ').slice(11, 19) || iso;
}

function batteryStr(pct) {
    if (pct == null) return '🔋 ?';
    return pct < 20 ? `🪫 ${pct}%` : `🔋 ${pct}%`;
}

// (0, 0) — заглушка от ESP32 без GPS-фикса. На карте такого не показываем.
function hasFix(pos) {
    return pos && (pos.lat !== 0 || pos.lon !== 0);
}

// --- Рендер списков в сайдбаре ---------------------------------------------

function renderTourists() {
    $('#tourists-count').textContent = touristsCache.length;
    const ul = $('#tourists-list');
    if (touristsCache.length === 0) {
        ul.innerHTML = '<li class="empty">никого в эфире</li>';
        return;
    }
    ul.innerHTML = '';
    for (const t of touristsCache) {
        const li = document.createElement('li');
        const name = t.name || `Device ${t.device_id}`;
        li.innerHTML = `
            <div><b>${name}</b> · ${batteryStr(t.battery_pct)}</div>
            <div class="meta">RSSI ${t.rssi ?? '?'} dBm · ${fmtTime(t.last_ping_at)}</div>
        `;
        if (hasFix(t.position)) {
            li.onclick = () => map.setView([t.position.lat, t.position.lon], 15);
        } else {
            li.style.opacity = '0.6';
            li.title = 'GPS-фикса пока нет';
        }
        ul.appendChild(li);
    }
}

function renderSos() {
    const open = sosCache.filter(s => !s.resolved);
    $('#sos-count').textContent = open.length;
    const ul = $('#sos-list');
    if (open.length === 0) {
        ul.innerHTML = '<li class="empty">нет открытых</li>';
        return;
    }
    ul.innerHTML = '';
    for (const s of open) {
        const li = document.createElement('li');
        li.className = 'sos' + (s.acked ? ' acked' : '');
        li.innerHTML = `
            <div><b>SOS #${s.id}</b> · ${s.sos_type_label}</div>
            <div class="meta">device ${s.device_id} · ${fmtTime(s.received_at)}${s.acked ? ' · ack' : ''}</div>
        `;
        if (hasFix(s.position)) {
            li.onclick = () => map.setView([s.position.lat, s.position.lon], 16);
        }
        ul.appendChild(li);
    }
}

// --- Рендер маркеров на карте ---------------------------------------------

function upsertTouristMarker(t) {
    if (!hasFix(t.position)) return;
    const ll = [t.position.lat, t.position.lon];
    const name = t.name || `Device ${t.device_id}`;
    const popup = `
        <b>${name}</b><br>
        ${batteryStr(t.battery_pct)} · RSSI ${t.rssi ?? '?'} dBm<br>
        <small>${fmtTime(t.last_ping_at)}</small>
    `;
    let m = tourMarkers.get(t.device_id);
    if (m) {
        m.setLatLng(ll);
        m.setPopupContent(popup);
    } else {
        m = L.marker(ll).addTo(map).bindPopup(popup);
        tourMarkers.set(t.device_id, m);
    }
}

function upsertSosMarker(s) {
    if (!hasFix(s.position)) return;
    const ll = [s.position.lat, s.position.lon];
    const popup = `
        <b>SOS #${s.id} — ${s.sos_type_label}</b><br>
        Device ${s.device_id}<br>
        ${fmtTime(s.received_at)}<br>
        ${s.message ? `<i>${s.message}</i><br>` : ''}
        ${s.resolved ? '✅ resolved' : (s.acked ? '✓ acked' : '⏳ ожидает ack')}
    `;
    // Цвет: красный — открытый, оранжевый — acked, зелёный — resolved
    const color = s.resolved ? '#16a34a' : (s.acked ? '#f59e0b' : '#dc2626');
    const opts = {
        radius: s.resolved ? 8 : 14,
        color, fillColor: color, fillOpacity: 0.5, weight: 3,
    };
    let m = sosMarkers.get(s.id);
    if (m) {
        m.setLatLng(ll);
        m.setStyle(opts);
        m.setPopupContent(popup);
    } else {
        m = L.circleMarker(ll, opts).addTo(map).bindPopup(popup);
        sosMarkers.set(s.id, m);
    }
}

// --- Полный refresh с API --------------------------------------------------

async function refreshTourists() {
    try {
        const r = await fetch('/api/tourists');
        touristsCache = await r.json();
        renderTourists();
        for (const t of touristsCache) upsertTouristMarker(t);
    } catch (e) {
        console.warn('refreshTourists fail', e);
    }
}

async function refreshSos() {
    try {
        const r = await fetch('/api/sos?only_open=false');
        sosCache = await r.json();
        renderSos();
        for (const s of sosCache) upsertSosMarker(s);
    } catch (e) {
        console.warn('refreshSos fail', e);
    }
}

async function refreshStats() {
    try {
        const r = await fetch('/api/stats');
        const s = await r.json();
        $('#stats-line').textContent =
            `· устройств: ${s.devices_total} (${s.devices_online} в эфире) · PING всего: ${s.pings_total} · SOS: ${s.sos_total}`;
    } catch { /* пофиг — шапка не критична */ }
}

// --- WebSocket с авто-переподключением -------------------------------------

function connectWS() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const url = `${proto}://${location.host}/ws`;
    ws = new WebSocket(url);

    ws.onopen = () => {
        setWsStatus('online');
        if (wsReconnectTimer) { clearTimeout(wsReconnectTimer); wsReconnectTimer = null; }
    };

    ws.onclose = () => {
        setWsStatus('offline');
        // Бесконечно реконнектимся раз в 2 сек — rescue-api мог быть рестартован.
        wsReconnectTimer = setTimeout(connectWS, 2000);
    };

    ws.onerror = () => { /* close сработает следом, обработка там */ };

    ws.onmessage = (e) => {
        let msg;
        try { msg = JSON.parse(e.data); } catch { return; }

        // Не парсим payload руками — проще целиком перезапросить
        // /api/tourists или /api/sos: там уже агрегировано «последний по
        // устройству». Ценой одного HTTP-запроса (5–10 мс) получаем
        // консистентный кеш — никогда не будет «маркер впереди списка».
        if (msg.event === 'ping') {
            refreshTourists();
            refreshStats();
        } else if (msg.event === 'sos') {
            refreshSos();
            refreshStats();
        }
    };
}

// --- Старт ------------------------------------------------------------------

async function init() {
    await Promise.all([refreshTourists(), refreshSos(), refreshStats()]);

    // Если есть активные туристы с GPS — центруем карту на первом из них.
    const withPos = touristsCache.find(t => hasFix(t.position));
    if (withPos) {
        map.setView([withPos.position.lat, withPos.position.lon], 13);
    }

    connectWS();

    // Подстраховка: раз в 30 сек пересчитываем статы (если WS «online» молчит,
    // увидим расхождение в счётчиках).
    setInterval(refreshStats, 30000);
}

init();
