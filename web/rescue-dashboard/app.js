/* ===========================================================================
 * Mesh-net Тропы — клиентский код дашборда базы спасателей
 *
 * Зачем один файл: ~250 строк, ES-модули в браузере без сборщика требуют
 * настройки CORS даже на localhost, а через обычный <script src=> — работает
 * сразу. Структура сверху вниз: конфиг → состояние → утилиты → рендер →
 * WebSocket → init.
 *
 * Источники данных:
 *   GET /api/tourists      — кто сейчас активен (PING недавно, см. ACTIVE_THRESHOLD_MIN в rescue-api)
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

// Стартовая точка карты — окрестности Краснодара (наш тестовый район,
// для которого скачаны оффлайн-тайлы через scripts/import_tiles/download_tiles.py).
// При наличии активных туристов в БД init() переезжает на первого из них
// автоматически. Если меняешь регион — синхронизируй с --bbox в скрипте скачки.
const DEFAULT_CENTER = [45.04, 39.03];
const DEFAULT_ZOOM   = 11;

// --- Состояние приложения --------------------------------------------------

const map = L.map('map').setView(DEFAULT_CENTER, DEFAULT_ZOOM);
L.tileLayer(TILES_URL, { attribution: TILES_ATTRIBUTION, maxZoom: 19 }).addTo(map);

const tourMarkers = new Map();   // device_id -> L.marker
const sosMarkers  = new Map();   // sos_id    -> L.circleMarker

let touristsCache = [];
let sosCache = [];
let tourMessagesCache = [];
let lastTourMsgId = 0;

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
    // В БД таймстампы в UTC ('2026-05-08T16:57:56Z'). Если 'Z' или
    // смещение пропущены — добавим, иначе Date() трактует строку как
    // локальное время и оператор в Москве увидит UTC-часы.
    const s = /Z$|[+-]\d\d:?\d\d$/.test(iso) ? iso : iso + 'Z';
    const d = new Date(s);
    if (isNaN(d.getTime())) return iso;
    return d.toLocaleTimeString('ru-RU', { hour12: false });
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
        // Сначала добавляем/обновляем маркеры активных,
        // потом сносим маркеры тех, кого больше нет в /api/tourists
        // (порог в БД: ACTIVE_THRESHOLD_MIN). Иначе после выключения
        // ESP32 синий маркер висит на карте до перезагрузки страницы.
        const activeIds = new Set(touristsCache.map(t => t.device_id));
        for (const t of touristsCache) upsertTouristMarker(t);
        for (const [id, m] of tourMarkers) {
            if (!activeIds.has(id)) {
                map.removeLayer(m);
                tourMarkers.delete(id);
            }
        }
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

// --- Чат с туристами (CHAT-пакеты от ESP32) -------------------------------
// Полностью отдельная панель снизу — НЕ путать с AI-диспетчером выше.
// Сообщения от базы отрисованы с другим цветом и выровнены справа,
// чтобы оператор сразу видел свой собственный поток в ленте.

// device_id базы. Должен совпадать с NODE_DEVICE_ID в lora-station и
// rescue-api/app.py::BASE_DEVICE_ID. Если когда-нибудь поменяется в одном
// месте — поменять и здесь.
const BASE_DEVICE_ID = 1;

function tourMsgBubble(m) {
    const isBase = m.device_id === BASE_DEVICE_ID;
    const name = m.device_name || (isBase ? 'База' : `Device ${m.device_id}`);
    const div = document.createElement('div');
    div.className = isBase ? 'tour-msg from-base' : 'tour-msg';
    const who = document.createElement('div');
    who.className = 'who';
    who.textContent = isBase ? `🛟 ${name}` : `${name} · #${m.device_id}`;
    const body = document.createElement('div');
    body.textContent = m.message;
    const when = document.createElement('div');
    when.className = 'when';
    when.textContent = fmtTime(m.received_at);
    div.append(who, body, when);
    return div;
}

function appendTourMessage(m) {
    if (m.id <= lastTourMsgId) return;          // дедуп: WS+refresh могут дублировать
    lastTourMsgId = m.id;
    tourMessagesCache.push(m);
    const log = $('#tour-chat-log');
    // На первом сообщении — убираем placeholder «загрузка истории…»
    const placeholder = log.querySelector('.chat-system');
    if (placeholder) placeholder.remove();
    log.appendChild(tourMsgBubble(m));
    log.scrollTop = log.scrollHeight;
    $('#tour-chat-count').textContent = tourMessagesCache.length;
}

async function refreshTourChat() {
    try {
        const r = await fetch('/api/messages?limit=100');
        const list = await r.json();
        tourMessagesCache = list;
        // После purge на сервере sqlite_sequence сбрасывается и id пойдут от 1.
        // Если бы lastTourMsgId остался большим, новые сообщения не пройдут
        // через дедуп appendTourMessage. Сбрасываем здесь, дальше в цикле
        // обновим до фактического MAX(id).
        lastTourMsgId = 0;
        const log = $('#tour-chat-log');
        log.innerHTML = '';
        if (list.length === 0) {
            const empty = document.createElement('div');
            empty.className = 'chat-msg chat-system';
            empty.textContent = 'пока нет сообщений';
            log.appendChild(empty);
        } else {
            for (const m of list) {
                log.appendChild(tourMsgBubble(m));
                if (m.id > lastTourMsgId) lastTourMsgId = m.id;
            }
            log.scrollTop = log.scrollHeight;
        }
        $('#tour-chat-count').textContent = list.length;
    } catch (e) {
        console.warn('refreshTourChat fail', e);
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
        } else if (msg.event === 'chat') {
            // У сообщений нет агрегации — просто дописываем в DOM по одному.
            appendTourMessage(msg.data);
        }
    };
}

// --- Чат с ИИ-диспетчером ---------------------------------------------------
// История живёт в localStorage — F5 НЕ стирает её. Чистится только по
// кнопке ✕ (см. initChat). На сервере по-прежнему ничего не хранится:
// каждый /api/chat шлёт нужный кусок истории отдельно.

const CHAT_STORAGE_KEY = 'mesh-ai-chat-history';
const CHAT_HISTORY_LIMIT = 20;   // последние N ходов в каждый /api/chat
// Полную историю чата держим без ограничений в localStorage; CHAT_HISTORY_LIMIT
// относится только к тому, сколько последних ходов уезжает в GigaChat.

let chatHistory = [];   // [{role:'user'|'assistant', content:'...'}, ...]

function loadChatHistory() {
    try {
        const raw = localStorage.getItem(CHAT_STORAGE_KEY);
        if (!raw) return [];
        const arr = JSON.parse(raw);
        // Защита от мусорного содержимого: оставляем только записи с правильной формой.
        return Array.isArray(arr) ? arr.filter(
            t => t && (t.role === 'user' || t.role === 'assistant')
                 && typeof t.content === 'string') : [];
    } catch {
        return [];
    }
}

function saveChatHistory() {
    try {
        localStorage.setItem(CHAT_STORAGE_KEY, JSON.stringify(chatHistory));
    } catch {
        // localStorage может отвалиться (приватный режим, квота). Чат продолжит
        // работать в памяти страницы, F5 просто потеряет историю — допустимо.
    }
}

function chatAppend(node) {
    const log = $('#chat-log');
    log.appendChild(node);
    log.scrollTop = log.scrollHeight;
    return node;
}

function chatBubble(cls, text) {
    const el = document.createElement('div');
    el.className = 'chat-msg ' + cls;
    el.textContent = text;
    return el;
}

async function sendChatMessage(text) {
    const sendBtn = $('#chat-send');
    const input = $('#chat-input');

    chatAppend(chatBubble('chat-user', text));
    chatHistory.push({ role: 'user', content: text });
    saveChatHistory();

    const thinking = chatAppend(chatBubble('chat-thinking', 'AI думает…'));

    sendBtn.disabled = true;
    input.disabled = true;

    try {
        const r = await fetch('/api/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                message: text,
                history: chatHistory.slice(-CHAT_HISTORY_LIMIT - 1, -1),  // без только что добавленного
            }),
        });
        const data = await r.json();
        thinking.remove();

        if (data.error) {
            chatAppend(chatBubble('chat-error', data.error));
            // Ошибочный ответ в историю не кладём — следующий вопрос пойдёт без него
        } else {
            const bubble = chatBubble('chat-assistant', data.reply || '(пустой ответ)');
            if (Array.isArray(data.tools_used) && data.tools_used.length) {
                const tools = document.createElement('div');
                tools.className = 'chat-tools';
                tools.textContent = '⚙ ' + data.tools_used.join(', ');
                bubble.appendChild(tools);
            }
            chatAppend(bubble);
            chatHistory.push({ role: 'assistant', content: data.reply || '' });
            saveChatHistory();
        }
    } catch (e) {
        thinking.remove();
        chatAppend(chatBubble('chat-error', `AI недоступен (сетевая ошибка: ${e.message})`));
    } finally {
        sendBtn.disabled = false;
        input.disabled = false;
        input.focus();
    }
}

function initChat() {
    const form = $('#chat-form');
    const input = $('#chat-input');
    const clearBtn = $('#chat-clear');

    // Восстанавливаем сохранённую историю из localStorage. Без этого блока
    // F5 стирал бы переписку с AI — оператору неудобно, если он отвлёкся
    // на инцидент и обновил страницу.
    chatHistory = loadChatHistory();
    const log = $('#chat-log');
    for (const t of chatHistory) {
        const cls = t.role === 'user' ? 'chat-user' : 'chat-assistant';
        log.appendChild(chatBubble(cls, t.content));
    }
    log.scrollTop = log.scrollHeight;

    form.addEventListener('submit', (e) => {
        e.preventDefault();
        const text = input.value.trim();
        if (!text) return;
        input.value = '';
        sendChatMessage(text);
    });

    // Enter — отправить, Shift+Enter — перенос строки. Стандартный UX мессенджера.
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            form.requestSubmit();
        }
    });

    clearBtn.addEventListener('click', () => {
        if (chatHistory.length > 0 &&
            !confirm('Очистить историю переписки с AI? Это необратимо.')) return;
        chatHistory.length = 0;
        try { localStorage.removeItem(CHAT_STORAGE_KEY); } catch {}
        $('#chat-log').innerHTML = '';
        chatAppend(chatBubble('chat-system', 'История очищена.'));
    });
}

// --- Очистка БД (отладка) ---------------------------------------------------
// Чекбоксы выбирают что чистить. Подтверждение: confirm() со списком +
// prompt() с вводом ОЧИСТИТЬ. Сервер тоже валидирует confirm и
// список таблиц — UI один без сервера БД не снесёт.
const TABLE_LABELS = {
    pings:          'PING-и',
    sos_events:     'SOS-инциденты',
    chat_messages:  'чат-сообщения',
    outgoing_chat:  'очередь ответов от базы',
    devices:        'устройства (+ всё что на них ссылается)',
};

// Чат с туристами — отправка ответа от базы. POST /api/messages → бэкенд
// пишет в chat_messages (WS event 'chat' дашборду) и в outgoing_chat (lora-
// station выгребает и шлёт в эфир). Сообщение появится в ленте через ~1 сек
// само через WS — мы здесь только очищаем поле.
const TOUR_CHAT_MAX_BYTES = 48;

function initTourChat() {
    const form  = $('#tour-chat-form');
    const input = $('#tour-chat-input');
    const send  = $('#tour-chat-send');
    if (!form || !input || !send) return;

    function utf8Len(s) { return new TextEncoder().encode(s).length; }

    function updateState() {
        const len = utf8Len(input.value.trim());
        send.disabled = len === 0 || len > TOUR_CHAT_MAX_BYTES;
        // Подсветим textarea красным если оператор перебрал лимит — иначе
        // он узнает только из 400 ответа после нажатия отправить.
        input.style.borderColor = len > TOUR_CHAT_MAX_BYTES ? '#dc2626' : '';
    }
    input.addEventListener('input', updateState);
    updateState();

    // Enter — отправить, Shift+Enter — перенос строки.
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            if (!send.disabled) form.requestSubmit();
        }
    });

    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        const text = input.value.trim();
        if (!text) return;
        if (utf8Len(text) > TOUR_CHAT_MAX_BYTES) {
            alert(`Сообщение длиннее ${TOUR_CHAT_MAX_BYTES} байт UTF-8 — не влезет в LoRa-пакет.`);
            return;
        }
        send.disabled = true;
        input.disabled = true;
        try {
            const r = await fetch('/api/messages', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({text}),
            });
            if (!r.ok) {
                const txt = await r.text();
                throw new Error(`HTTP ${r.status}: ${txt}`);
            }
            // Само сообщение появится в ленте через WS-event 'chat'.
            input.value = '';
            updateState();
        } catch (err) {
            alert('Не удалось отправить: ' + err.message);
        } finally {
            input.disabled = false;
            input.focus();
            updateState();
        }
    });
}

function initPurge() {
    const btn = $('#purge-db');
    if (!btn) return;
    btn.addEventListener('click', async () => {
        const checks = document.querySelectorAll('input[name="purge"]:checked');
        const tables = Array.from(checks).map(c => c.value);
        if (tables.length === 0) {
            alert('Выбери хотя бы одну таблицу для очистки.');
            return;
        }
        const human = tables.map(t => '• ' + (TABLE_LABELS[t] || t)).join('\n');
        if (!confirm('Удалить из БД:\n' + human + '\n\nЭто необратимо.')) return;
        const phrase = prompt('Введи слово ОЧИСТИТЬ для подтверждения:');
        if (phrase !== 'ОЧИСТИТЬ') {
            alert('Подтверждение не совпало — БД не тронута.');
            return;
        }
        btn.disabled = true;
        btn.textContent = 'очистка…';
        try {
            const r = await fetch('/api/admin/purge', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({confirm: 'ОЧИСТИТЬ', tables}),
            });
            if (!r.ok) {
                const txt = await r.text();
                throw new Error(`HTTP ${r.status}: ${txt}`);
            }
            const data = await r.json();
            alert('Готово. Удалено:\n' + JSON.stringify(data.deleted, null, 2));
            // Маркеры/списки чистим вручную — WS-эвенты сразу не прилетят,
            // а «пустая БД, но карта в маркерах» выглядит уродливо.
            for (const [, m] of tourMarkers) map.removeLayer(m);
            tourMarkers.clear();
            for (const [, m] of sosMarkers)  map.removeLayer(m);
            sosMarkers.clear();
            await Promise.all([refreshTourists(), refreshSos(), refreshStats(), refreshTourChat()]);
        } catch (e) {
            alert('Ошибка: ' + e.message);
        } finally {
            btn.disabled = false;
            btn.textContent = 'Очистить выбранное';
        }
    });
}

// --- Старт ------------------------------------------------------------------

async function init() {
    await Promise.all([refreshTourists(), refreshSos(), refreshStats(), refreshTourChat()]);

    // Если есть активные туристы с GPS — центруем карту на первом из них.
    const withPos = touristsCache.find(t => hasFix(t.position));
    if (withPos) {
        map.setView([withPos.position.lat, withPos.position.lon], 13);
    }

    connectWS();
    initChat();
    initTourChat();
    initPurge();

    // Подстраховка: раз в 30 сек пересчитываем статы (если WS «online» молчит,
    // увидим расхождение в счётчиках).
    setInterval(refreshStats, 30000);

    // Турист может ВЫПАСТЬ с эфира (выключили ESP32, разрядился аккум,
    // ушёл из зоны LoRa). WS-событие 'ping' в этом случае не приходит,
    // и список туристов "залипает" с устаревшими записями. Поэтому раз
    // в 30 сек принудительно рефрешим списки — серверный фильтр по
    // ACTIVE_THRESHOLD_MIN сам выкинет тех, у кого PING устарел.
    setInterval(() => {
        refreshTourists();
        refreshSos();
    }, 30000);
}

init();
