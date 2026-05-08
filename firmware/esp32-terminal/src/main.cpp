// ============================================================================
// Mesh-net Тропы — прошивка ESP32-S3 терминала туриста
// Этап 1b: PING + Wi-Fi AP + HTTPS + GPS из браузера + кнопка SOS
// ============================================================================
// Железо:   ESP32-S3 N16R8 + HT-RA62 (SX1262)
// Пины:     см. platformio.ini (build_flags)
// Частота:  868 МГц, SF=10, BW=125 кГц, CR=4/5
// Wi-Fi AP: MeshNet-XXX (без пароля), https://192.168.4.1/
// SOS:      нажатие кнопки → 3 LoRa-пакета с интервалом 500 мс (TTL=3)
// GPS:      браузер шлёт координаты по POST /api/gps каждые 2 сек
// ============================================================================
// Почему HTTPS, а не HTTP:
// браузеры разрешают navigator.geolocation только в "secure context"
// (HTTPS либо localhost). Локальный 192.168.4.1 secure не считается, поэтому
// поднимаем самоподписанный TLS. Сертификат в include/cert.h,
// регенерируется scripts/gen_cert.sh. WebSocket был, но ESP32-https
// его «из коробки» не умеет — заменён на POST + polling /api/status.
// ============================================================================

#include <Arduino.h>
#include <SPI.h>
#include <WiFi.h>
#include <RadioLib.h>
#include <math.h>

#include <HTTPSServer.hpp>
#include <SSLCert.hpp>
#include <HTTPRequest.hpp>
#include <HTTPResponse.hpp>

#include "MeshPacket.h"
#include "cert.h"

using namespace httpsserver;

// --- Настройки этого терминала ---------------------------------------------
static const uint16_t DEVICE_ID        = 0x0010;           // ID туриста (база=0x0001, инфо-точка=0x0100, туристы=0x00xx)
static const Channel  DEVICE_CHANNEL   = Channel::TOURIST; // канал
static const uint32_t PING_INTERVAL_MS = 10000;            // раз в 10 сек

// SOS — параметры из CLAUDE.md
static const uint8_t  SOS_REPEAT       = 3;     // 3 пакета подряд
static const uint32_t SOS_INTERVAL_MS  = 500;   // интервал между ними

// CHAT — повторяем 2 раза с интервалом 1.3 сек.
// Зачем: LoRa полу-дуплекс, два узла часто заходят в TX одновременно
// (оператор и турист пишут друг другу) — collision, оба пакета теряются.
// Повтор с разным от lora-station интервалом (там 1.0 сек) почти гарантирует
// что хотя бы одна копия дойдёт. Дедуп на стороне приёмника по hash payload
// уберёт лишнее (см. lora-station/mesh.py::DedupCache._key).
static const uint8_t  CHAT_REPEAT      = 2;
static const uint32_t CHAT_INTERVAL_MS = 1300;

// Координаты-заглушка пока браузер не прислал GPS. (0, 0) = "координаты неизвестны".
static const int32_t STUB_LAT_E6 = 0;
static const int32_t STUB_LON_E6 = 0;

// --- Параметры радио (должны совпадать с RPi5!) ----------------------------
static const float   RADIO_FREQ      = LORA_FREQ;  // 868.0 МГц из platformio.ini
static const float   RADIO_BW        = 125.0;
static const uint8_t RADIO_SF        = 10;
static const uint8_t RADIO_CR        = 5;
static const int8_t  RADIO_TX_POWER  = 14;
static const uint8_t RADIO_PREAMBLE  = 8;
static const float   RADIO_TCXO_V    = 1.8;

// --- Экземпляр радиомодуля -------------------------------------------------
SPIClass loraSPI(HSPI);
SX1262 radio = new Module(LORA_CS, LORA_DIO1, LORA_RESET, LORA_BUSY, loraSPI);

// --- HTTPS-сервер ----------------------------------------------------------
// SSLCert принимает не const-указатели, поэтому массивы из cert.h объявлены
// как unsigned char[], а длины приводим к uint16_t (cert ~769, key ~1216 — влезает).
static SSLCert      g_cert(mesh_cert_der, (uint16_t)mesh_cert_der_len,
                           mesh_key_der,  (uint16_t)mesh_key_der_len);
static HTTPSServer  g_server(&g_cert, 443, 2);   // 443/TLS, 2 коннекта максимум

static char   g_ap_ssid[16];     // SSID точки доступа: MeshNet-001
static String g_index_html;      // HTML, пререндеренный один раз при старте

// --- Состояние ------------------------------------------------------------
static uint16_t g_seq           = 0;
static uint32_t g_last_ping_ms  = 0;

// SOS-state-machine. Меняется только в loop(); HTTP-обработчик лишь взводит флаг.
static volatile bool    g_sos_requested  = false;  // взводится из web-task
static volatile uint8_t g_sos_type       = 0;      // SosType (0=UNKNOWN..4=WEATHER), задаётся web-обработчиком
static uint8_t          g_sos_pending    = 0;      // сколько SOS-пакетов осталось послать
static uint32_t         g_sos_next_ms    = 0;      // когда отправить следующий
static uint32_t         g_sos_done_until = 0;      // до этого ms показываем "done"

// GPS, присылаемый браузером. Записывается из web-task, читается из loop().
// int32 на ESP32 атомарен по чтению/записи, флаг ставится после координат.
static volatile bool    g_gps_valid    = false;
static volatile int32_t g_gps_lat_e6   = 0;
static volatile int32_t g_gps_lon_e6   = 0;
static volatile uint32_t g_gps_last_ms = 0;

// CHAT — текст до 48 байт UTF-8 + флаг «надо отправить».
// Записывается в handle_chat() (Core 0, web-task), читается в send_chat()
// (Core 1, loop). Двойного запроса подряд можно не бояться — HTTPS-сервер
// в одной задаче, обрабатывает запросы последовательно.
static volatile bool g_chat_requested = false;
static char          g_chat_pending[64] = {0};

// CHAT-бёрст в loop() (как для SOS): пока g_chat_burst > 0, шлём по графику.
static uint8_t  g_chat_burst   = 0;        // сколько копий ещё надо послать
static uint32_t g_chat_next_ms = 0;        // когда отправить следующую

// --- RX (приём пакетов) ---------------------------------------------------
// SX1262 → DIO1 (RX_DONE) → ISR ставит флаг → loop() читает пакет
// и обрабатывает CHAT-сообщения (от базы или других туристов).
static volatile bool g_rx_flag = false;
static IRAM_ATTR void on_rx_done() { g_rx_flag = true; }

// id базы (= NODE_DEVICE_ID lora-station). От неё приходят ответы оператора —
// показываем красным «Спасатели:», от других туристов — «Турист #N».
static const uint16_t BASE_DEVICE_ID = 0x0001;

// Дедуп ретрансляций: один и тот же CHAT придёт напрямую И через ретранслятор
// (с TTL−1). Без фильтра пользователь увидит N копий «дошли до приюта».
// Окно 30 сек — за это время бёрст ретрансляций гарантированно отстреляется.
struct RecentRx { uint16_t from_id; uint32_t hash; uint32_t ms; };
static const uint8_t RECENT_RX_SIZE = 4;
static RecentRx g_recent_rx[RECENT_RX_SIZE] = {};
static uint8_t  g_recent_rx_idx = 0;

static uint32_t hash_payload(const uint8_t* data, size_t len) {
    // FNV-1a 32-bit. Хорошо рассеивает короткие строки, реализация в одну строку.
    uint32_t h = 2166136261u;
    for (size_t i = 0; i < len; i++) { h ^= data[i]; h *= 16777619u; }
    return h;
}

static bool seen_recently(uint16_t from, uint32_t hash) {
    uint32_t now = millis();
    for (uint8_t i = 0; i < RECENT_RX_SIZE; i++) {
        if (g_recent_rx[i].from_id == from &&
            g_recent_rx[i].hash    == hash &&
            (now - g_recent_rx[i].ms) < 30000) {
            return true;
        }
    }
    g_recent_rx[g_recent_rx_idx] = { from, hash, now };
    g_recent_rx_idx = (g_recent_rx_idx + 1) % RECENT_RX_SIZE;
    return false;
}

// --- Inbox: входящие CHAT-сообщения ---------------------------------------
// Кольцевой буфер. id монотонно растёт; страница знает свой last_id и
// тащит /api/inbox?since=last_id раз в 5 сек.
struct InboxMsg {
    uint32_t id;
    uint16_t from_id;
    uint32_t received_ms;       // millis() на момент приёма
    char     text[MESH_PAYLOAD_SIZE + 1];   // 48 + терминатор
};
static const uint8_t INBOX_SIZE = 8;
static InboxMsg g_inbox[INBOX_SIZE] = {};
static uint8_t  g_inbox_pos = 0;
static uint32_t g_inbox_last_id = 0;

// Mutex для работы с g_inbox: пишет loop() (Core 1), читает handle_inbox
// (Core 0). portMUX_TYPE — самая лёгкая критическая секция в FreeRTOS.
static portMUX_TYPE g_inbox_mux = portMUX_INITIALIZER_UNLOCKED;

static void inbox_push(uint16_t from, const char* text) {
    portENTER_CRITICAL(&g_inbox_mux);
    g_inbox_last_id++;
    InboxMsg& m = g_inbox[g_inbox_pos];
    m.id          = g_inbox_last_id;
    m.from_id     = from;
    m.received_ms = millis();
    strncpy(m.text, text, sizeof(m.text) - 1);
    m.text[sizeof(m.text) - 1] = 0;
    g_inbox_pos = (g_inbox_pos + 1) % INBOX_SIZE;
    portEXIT_CRITICAL(&g_inbox_mux);
}

// ---------------------------------------------------------------------------
// HTML-страница: тёмный фон, большая красная кнопка, GPS-блок.
// %DEVICE_ID% подменяется при отдаче.
static const char INDEX_HTML[] PROGMEM = R"HTML(
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mesh-net Тропы</title>
<style>
  *{box-sizing:border-box}
  body{font-family:system-ui,sans-serif;margin:0;padding:24px;
       background:#1a1a1a;color:#eee;text-align:center;min-height:100vh;
       display:flex;flex-direction:column;align-items:center}
  h1{font-size:1.4em;margin:.4em 0}
  .id{font-size:2.4em;color:#4fc3f7;margin:0 0 .4em}
  .conn{font-size:.85em;margin-bottom:2em}
  .conn.ok{color:#81c784}
  .conn.bad{color:#e57373}
  .sos-grid{display:flex;flex-direction:column;gap:14px;
    width:90vw;max-width:380px;margin-top:.5em}
  button.sos{
    width:100%;padding:22px 12px;border-radius:18px;
    border:5px solid #b71c1c;background:#d32f2f;color:#fff;
    font-size:1.6em;font-weight:bold;letter-spacing:.05em;
    box-shadow:0 0 22px rgba(211,47,47,.45);
    cursor:pointer;transition:transform .1s,background .1s;
    -webkit-tap-highlight-color:transparent;user-select:none
  }
  button.sos .ico{font-size:1.4em;display:block;margin-bottom:4px}
  button.sos:active{transform:scale(.97);background:#b71c1c}
  button.sos:disabled{background:#555;border-color:#333;
    box-shadow:none;color:#aaa;cursor:default}
  .status{margin-top:1.5em;min-height:1.4em;font-size:.95em;color:#ffb74d}
  .gps{margin-top:1.2em;font-size:.85em;color:#bbb}
  .gps.ok{color:#81c784}
  .gps.bad{color:#e57373}
  button.gps-btn{margin-top:.6em;padding:.5em 1.1em;font-size:.9em;
    background:#333;color:#eee;border:1px solid #555;border-radius:6px;
    cursor:pointer}
  button.gps-btn:disabled{opacity:.5;cursor:default}
  .chat-box{margin-top:1.6em;width:90vw;max-width:380px;
    display:flex;flex-direction:column;gap:6px;text-align:left}
  .chat-box h3{font-size:.9em;color:#bbb;font-weight:600;margin:0;text-align:center}
  .chat-box textarea{padding:8px;border-radius:6px;border:1px solid #555;
    background:#222;color:#eee;font-family:inherit;font-size:.95em;resize:vertical}
  .chat-box textarea:focus{outline:none;border-color:#1976d2}
  .chat-box .row{display:flex;gap:6px;align-items:center}
  .chat-box .counter{font-size:.75em;color:#888;flex:1;text-align:left}
  .chat-box button{padding:.55em 1em;font-size:.95em;
    background:#1976d2;color:#fff;border:none;border-radius:6px;cursor:pointer}
  .chat-box button:hover{background:#1565c0}
  .chat-box button:disabled{opacity:.45;cursor:default;background:#555}
  .chat-status{font-size:.85em;color:#bbb;min-height:1em;text-align:center}
  .chat-status.ok{color:#81c784}
  .chat-status.bad{color:#e57373}
  .inbox-box{margin-top:1.6em;width:90vw;max-width:380px;text-align:left}
  .inbox-box h3{font-size:.9em;color:#bbb;font-weight:600;margin:0 0 .4em;text-align:center}
  .inbox-list{display:flex;flex-direction:column;gap:6px;max-height:240px;
    overflow-y:auto;padding-right:4px}
  .inbox-empty{font-size:.85em;color:#666;font-style:italic;text-align:center;
    padding:.6em 0}
  .inbox-msg{padding:8px 10px;border-radius:8px;font-size:.9em;line-height:1.35;
    background:#262626;border-left:3px solid #555;word-break:break-word;
    white-space:pre-wrap}
  .inbox-msg.from-base{border-left-color:#fbc02d;background:#3a2a0e}
  .inbox-msg .who{font-size:.7em;font-weight:600;color:#bbb;margin-bottom:3px}
  .inbox-msg.from-base .who{color:#ffca28}
  .inbox-msg .when{font-size:.7em;color:#777;margin-top:3px;
    font-family:ui-monospace,monospace}
</style>
</head>
<body>
  <h1>Mesh-net Тропы</h1>
  <div>терминал №</div>
  <div class="id">%DEVICE_ID%</div>
  <div id="conn" class="conn bad">подключение…</div>
  <div class="sos-grid">
    <button class="sos" data-type="1" disabled><span class="ico">🪂</span>ПАДЕНИЕ</button>
    <button class="sos" data-type="2" disabled><span class="ico">🤕</span>МЕДИЦИНА</button>
    <button class="sos" data-type="3" disabled><span class="ico">🧭</span>ЗАБЛУДИЛСЯ</button>
  </div>
  <div class="status" id="status"></div>
  <div class="gps bad" id="gps">GPS: выключен</div>
  <button class="gps-btn" id="gpsBtn">включить GPS</button>

  <div class="chat-box">
    <h3>Сообщение спасателям</h3>
    <textarea id="chatInput" rows="2" maxlength="48"
      placeholder="до 48 байт UTF-8 (~24 рус. букв)"></textarea>
    <div class="row">
      <span class="counter" id="chatCounter">0 / 48</span>
      <button id="chatBtn" disabled>отправить</button>
    </div>
    <div class="chat-status" id="chatStatus"></div>
  </div>

  <div class="inbox-box">
    <h3>Сообщения от базы</h3>
    <div id="inboxList" class="inbox-list">
      <div class="inbox-empty">пока нет сообщений</div>
    </div>
  </div>

<script>
  const conn        = document.getElementById('conn');
  const sosBtns     = document.querySelectorAll('button.sos');
  const status      = document.getElementById('status');
  const gpsEl       = document.getElementById('gps');
  const gpsBtn      = document.getElementById('gpsBtn');
  const chatInput   = document.getElementById('chatInput');
  const chatBtn     = document.getElementById('chatBtn');
  const chatStatus  = document.getElementById('chatStatus');
  const chatCounter = document.getElementById('chatCounter');

  let watchId       = null;
  let lastGpsSendMs = 0;
  let pollTimer     = null;
  let connOk        = false;

  // SOS-кнопки + кнопка чата живут от одного флага «связь есть».
  // Чат дополнительно требует непустой текст — это решает updateChatBtn().
  function setSosEnabled(en) {
    connOk = en;
    sosBtns.forEach(b => { b.disabled = !en; });
    updateChatBtn();
  }

  // UTF-8 длина текста + блокировка кнопки если связи нет / текст пустой.
  function updateChatBtn() {
    const txt = chatInput.value;
    const len = new TextEncoder().encode(txt).length;
    chatCounter.textContent = len + ' / 48';
    chatCounter.style.color = len > 48 ? '#e57373' : '#888';
    chatBtn.disabled = !connOk || len === 0 || len > 48;
  }

  // Простой "пинг" сервера — показывает зелёный/красный индикатор связи
  async function pingServer() {
    try {
      const r = await fetch('/api/status', {cache:'no-store'});
      if (!r.ok) throw new Error('http ' + r.status);
      conn.textContent = 'связь установлена';
      conn.className   = 'conn ok';
      setSosEnabled(true);
    } catch (e) {
      conn.textContent = 'нет связи с терминалом';
      conn.className   = 'conn bad';
      setSosEnabled(false);
    }
  }

  // Опрос статуса бёрста — крутится только пока SOS летит
  async function pollStatus() {
    try {
      const r = await fetch('/api/status', {cache:'no-store'});
      const t = (await r.text()).trim();
      if (t.startsWith('tx:')) {
        status.textContent = 'передача SOS — пакет ' + t.slice(3);
      } else if (t === 'done') {
        status.textContent = 'SOS отправлен (3 пакета)';
        clearInterval(pollTimer); pollTimer = null;
        setTimeout(() => { setSosEnabled(true); status.textContent = ''; }, 5000);
      }
    } catch (e) { /* бывает во время handshake — следующий тик пройдёт */ }
  }

  // Шлём тип в body (одна цифра: 1=падение, 2=медицина, 3=заблудился).
  // ESP32 при отсутствии/неверном body fallback-нёт в UNKNOWN (0).
  async function sendSos(type) {
    setSosEnabled(false);
    status.textContent = 'отправка SOS…';
    try {
      const r = await fetch('/api/sos', {method:'POST', body: String(type)});
      if (!r.ok) throw new Error('http ' + r.status);
      status.textContent = 'SOS принят терминалом';
      if (!pollTimer) pollTimer = setInterval(pollStatus, 300);
    } catch (e) {
      status.textContent = 'ошибка: ' + e.message;
      setSosEnabled(true);
    }
  }
  sosBtns.forEach(b => {
    b.addEventListener('click', () => sendSos(parseInt(b.dataset.type, 10)));
  });

  function startGps() {
    if (!('geolocation' in navigator)) {
      gpsEl.textContent = 'GPS: браузер не поддерживает';
      gpsEl.className   = 'gps bad';
      return;
    }
    gpsEl.textContent = 'GPS: запрос разрешения…';
    gpsBtn.disabled   = true;

    watchId = navigator.geolocation.watchPosition(
      async (pos) => {
        const lat = pos.coords.latitude.toFixed(6);
        const lon = pos.coords.longitude.toFixed(6);
        const acc = Math.round(pos.coords.accuracy);
        gpsEl.textContent = 'GPS: ' + lat + ', ' + lon + ' (±' + acc + ' м)';
        gpsEl.className   = 'gps ok';

        // Шлём не чаще раза в 2 сек
        const now = Date.now();
        if (now - lastGpsSendMs >= 2000) {
          lastGpsSendMs = now;
          try { await fetch('/api/gps', {method:'POST', body: lat + ',' + lon}); }
          catch (e) { /* следующий тик попробует снова */ }
        }
      },
      (err) => {
        gpsEl.textContent = 'GPS: ошибка — ' + err.message;
        gpsEl.className   = 'gps bad';
        gpsBtn.disabled   = false;
        gpsBtn.textContent = 'повторить';
      },
      { enableHighAccuracy: true, maximumAge: 5000, timeout: 30000 }
    );
  }
  gpsBtn.addEventListener('click', startGps);

  // --- Чат: отправить произвольный текст одним CHAT-пакетом ---
  // Сервер возвращает 200 как только TX поставлен в очередь. Дальше
  // pollStatus не используем — чат не такой критичный как SOS.
  chatInput.addEventListener('input', updateChatBtn);
  chatInput.addEventListener('keydown', (e) => {
    // Ctrl+Enter / Cmd+Enter — отправка. Просто Enter оставляем для переноса
    // строки (textarea, на смартфоне с экранной клавиатурой это удобнее).
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      if (!chatBtn.disabled) chatBtn.click();
    }
  });
  chatBtn.addEventListener('click', async () => {
    const text = chatInput.value.trim();
    if (!text) return;
    chatBtn.disabled = true;
    chatStatus.textContent = 'отправка…';
    chatStatus.className   = 'chat-status';
    try {
      const r = await fetch('/api/chat', {method: 'POST', body: text});
      if (!r.ok) throw new Error('http ' + r.status);
      chatInput.value = '';
      chatStatus.textContent = '✓ передано в эфир';
      chatStatus.className   = 'chat-status ok';
      setTimeout(() => {
        chatStatus.textContent = '';
        chatStatus.className   = 'chat-status';
      }, 3000);
    } catch (e) {
      chatStatus.textContent = 'ошибка: ' + e.message;
      chatStatus.className   = 'chat-status bad';
    } finally {
      updateChatBtn();
    }
  });

  // --- Inbox: polling входящих CHAT-сообщений (от базы или других туристов) ---
  // Дёргаем /api/inbox?since=last каждые 5 сек. Если ESP32 рестартанул —
  // его счётчик начнётся с 1, а наш last в браузере остался большим: тогда
  // сбрасываем last на 0 и подгружаем заново.
  const inboxList = document.getElementById('inboxList');
  let inboxLastId = 0;

  function fmtAge(ms) {
    const s = Math.floor(ms / 1000);
    if (s < 60)   return s + ' сек назад';
    if (s < 3600) return Math.floor(s / 60) + ' мин назад';
    return Math.floor(s / 3600) + ' ч назад';
  }

  function whoLabel(from) {
    if (from === 1) return '🛟 База спасателей';
    return 'Турист #' + from;
  }

  function renderInbox(messages) {
    // Очистим placeholder при первом сообщении
    const empty = inboxList.querySelector('.inbox-empty');
    if (empty && messages.length) empty.remove();
    for (const m of messages) {
      const div = document.createElement('div');
      div.className = 'inbox-msg' + (m.from === 1 ? ' from-base' : '');
      const who = document.createElement('div');
      who.className = 'who';
      who.textContent = whoLabel(m.from);
      const body = document.createElement('div');
      body.textContent = m.text;
      const when = document.createElement('div');
      when.className = 'when';
      when.textContent = fmtAge(m.age_ms);
      div.append(who, body, when);
      inboxList.appendChild(div);
    }
    if (messages.length) inboxList.scrollTop = inboxList.scrollHeight;
  }

  async function pollInbox() {
    try {
      const r = await fetch('/api/inbox?since=' + inboxLastId, {cache:'no-store'});
      if (!r.ok) return;
      const data = await r.json();
      // ESP32 перезагрузился? latest «съехал» назад — заново подтянем всё.
      if (typeof data.latest === 'number' && data.latest < inboxLastId) {
        inboxLastId = 0;
        inboxList.innerHTML = '<div class="inbox-empty">пока нет сообщений</div>';
        return;
      }
      if (Array.isArray(data.messages) && data.messages.length) {
        renderInbox(data.messages);
        for (const m of data.messages) {
          if (m.id > inboxLastId) inboxLastId = m.id;
        }
      }
    } catch (e) { /* следующий тик попробует снова */ }
  }
  pollInbox();
  setInterval(pollInbox, 5000);

  pingServer();
  setInterval(pingServer, 5000);
</script>
</body>
</html>
)HTML";

// Готовим HTML один раз при старте
static void prepare_index() {
    g_index_html = INDEX_HTML;
    g_index_html.replace("%DEVICE_ID%", String(DEVICE_ID));
    Serial.printf("[HTTP] index.html подготовлен (%u байт)\n", g_index_html.length());
}

// ---------------------------------------------------------------------------
// HTTPS-обработчики. Все три должны возвращаться быстро: TX-пакеты
// гонятся в loop()/Core 1, тут мы только взводим флаги.

static void handle_root(HTTPRequest* /*req*/, HTTPResponse* res) {
    res->setHeader("Content-Type", "text/html; charset=utf-8");
    res->print(g_index_html.c_str());
}

// POST body: одна цифра — SosType (0..4). Пустое body / мусор → UNKNOWN.
// Тип используется только для текущего бёрста, потом сбрасывается обратно
// в UNKNOWN при следующем POST.
static void handle_sos(HTTPRequest* req, HTTPResponse* res) {
    char body[8];
    size_t n = req->readChars(body, sizeof(body) - 1);
    body[n] = 0;
    int t = atoi(body);
    if (t < 0 || t > 4) t = 0;   // не угадал — пусть будет UNKNOWN
    g_sos_type      = (uint8_t)t;
    g_sos_requested = true;
    res->setHeader("Content-Type", "text/plain; charset=utf-8");
    res->println("queued");
    Serial.printf("[HTTP] /api/sos: бёрст запрошен, тип=%d\n", t);
}

// POST body: "lat,lon" в десятичных градусах. Принимаем только валидные.
static void handle_gps(HTTPRequest* req, HTTPResponse* res) {
    char body[64];
    size_t n = req->readChars(body, sizeof(body) - 1);
    body[n] = 0;

    double lat = 0, lon = 0;
    if (sscanf(body, "%lf,%lf", &lat, &lon) == 2 &&
        lat >= -90.0 && lat <= 90.0 &&
        lon >= -180.0 && lon <= 180.0) {
        g_gps_lat_e6  = (int32_t)lround(lat * 1e6);
        g_gps_lon_e6  = (int32_t)lround(lon * 1e6);
        g_gps_last_ms = millis();
        g_gps_valid   = true;
        res->setHeader("Content-Type", "text/plain; charset=utf-8");
        res->println("ok");
    } else {
        res->setStatusCode(400);
        res->setHeader("Content-Type", "text/plain; charset=utf-8");
        res->println("bad");
    }
}

// POST body: произвольный UTF-8 текст до 48 байт.
// Кладём в g_chat_pending и взводим флаг — отправка пойдёт из loop().
// Если пришёл новый текст до того как loop() забрал старый — старый
// перезаписывается. Допустимо: пользователь нажал «отправить» дважды,
// последнее сообщение перетёрло предыдущее.
static void handle_chat(HTTPRequest* req, HTTPResponse* res) {
    char body[sizeof(g_chat_pending)];
    size_t n = req->readChars(body, sizeof(body) - 1);
    body[n] = 0;

    // Триммим хвостовые \r\n / пробелы — некоторые HTTP-клиенты добавляют.
    while (n > 0 && (body[n-1] == '\r' || body[n-1] == '\n' ||
                     body[n-1] == ' '  || body[n-1] == '\t')) {
        body[--n] = 0;
    }
    if (n == 0) {
        res->setStatusCode(400);
        res->setHeader("Content-Type", "text/plain; charset=utf-8");
        res->println("empty");
        return;
    }

    memcpy(g_chat_pending, body, sizeof(g_chat_pending));
    g_chat_pending[sizeof(g_chat_pending) - 1] = 0;
    g_chat_requested = true;

    res->setHeader("Content-Type", "text/plain; charset=utf-8");
    res->println("queued");
    Serial.printf("[HTTP] /api/chat: %u байт в очереди\n", (unsigned)n);
}

// Минимальное JSON-экранирование строки в существующий String.
// UTF-8 байты ≥ 0x80 — это валидный JSON, ничего не делаем; экранируем
// только ", \\, контрольные символы.
static void json_append_escaped(String& out, const char* s) {
    for (const unsigned char* p = (const unsigned char*)s; *p; p++) {
        if (*p == '"')      out += "\\\"";
        else if (*p == '\\') out += "\\\\";
        else if (*p == '\n') out += "\\n";
        else if (*p == '\r') out += "\\r";
        else if (*p == '\t') out += "\\t";
        else if (*p < 0x20) {
            char buf[8];
            snprintf(buf, sizeof(buf), "\\u%04X", *p);
            out += buf;
        }
        else out += (char)*p;
    }
}

// GET /api/inbox?since=N → JSON-список сообщений с id > N.
// Страница в браузере держит свой last_id и опрашивает раз в 5 сек.
// Возвращаем максимум INBOX_SIZE записей (буфер кольцевой, старые
// перезаписываются — это допустимо: оператор не пишет 100 сообщений в минуту).
static void handle_inbox(HTTPRequest* req, HTTPResponse* res) {
    // Параметр since из query-string. esp32_https_server v1.0.0 отдаёт
    // значение через out-параметр и bool: true если параметр был.
    uint32_t since = 0;
    std::string s;
    if (req->getParams()->getQueryParameter("since", s) && !s.empty()) {
        since = (uint32_t)strtoul(s.c_str(), nullptr, 10);
    }

    // Снимок буфера под мьютексом — строим JSON уже без блокировок.
    InboxMsg snap[INBOX_SIZE];
    uint32_t latest;
    portENTER_CRITICAL(&g_inbox_mux);
    memcpy(snap, g_inbox, sizeof(snap));
    latest = g_inbox_last_id;
    portEXIT_CRITICAL(&g_inbox_mux);

    String body;
    body.reserve(256);
    body += "{\"latest\":";
    body += latest;
    body += ",\"messages\":[";
    bool first = true;
    uint32_t now = millis();
    for (uint8_t i = 0; i < INBOX_SIZE; i++) {
        if (snap[i].id == 0 || snap[i].id <= since) continue;
        if (!first) body += ",";
        first = false;
        body += "{\"id\":";
        body += snap[i].id;
        body += ",\"from\":";
        body += snap[i].from_id;
        body += ",\"age_ms\":";
        body += (uint32_t)(now - snap[i].received_ms);
        body += ",\"text\":\"";
        json_append_escaped(body, snap[i].text);
        body += "\"}";
    }
    body += "]}";

    res->setHeader("Content-Type", "application/json; charset=utf-8");
    res->setHeader("Cache-Control", "no-store");
    res->print(body.c_str());
}

// idle | tx:N | done — простой текст для polling-а
static void handle_status(HTTPRequest* /*req*/, HTTPResponse* res) {
    res->setHeader("Content-Type", "text/plain; charset=utf-8");
    res->setHeader("Cache-Control", "no-store");
    if (g_sos_pending > 0) {
        uint8_t idx = SOS_REPEAT - g_sos_pending + 1;
        res->printf("tx:%u\n", idx);
    } else if ((int32_t)(g_sos_done_until - millis()) > 0) {
        res->println("done");
    } else {
        res->println("idle");
    }
}

static void handle_404(HTTPRequest* req, HTTPResponse* res) {
    res->setStatusCode(404);
    res->setHeader("Content-Type", "text/plain; charset=utf-8");
    res->printf("404 %s\n", req->getRequestString().c_str());
}

// ---------------------------------------------------------------------------
// Прочитать принятый пакет: декодируем, фильтруем эхо, сохраняем CHAT в inbox.
// Вызывается из loop() сразу как только g_rx_flag взведён ISR-ом DIO1.
static void process_rx() {
    uint8_t buf[MESH_PACKET_SIZE];
    int state = radio.readData(buf, MESH_PACKET_SIZE);

    // Сразу возвращаемся в RX, чтобы не пропустить следующий пакет
    // пока возимся с этим. Если RX уже crashed — просто будем в STBY,
    // следующий transmit_packet() всё равно вернёт в RX.
    int rx_state = radio.startReceive();
    if (rx_state != RADIOLIB_ERR_NONE) {
        Serial.printf("[RX] startReceive после readData FAIL code=%d\n", rx_state);
    }

    if (state != RADIOLIB_ERR_NONE) {
        if (state == RADIOLIB_ERR_CRC_MISMATCH) {
            Serial.println(F("[RX] CRC mismatch — дроп"));
        } else {
            Serial.printf("[RX] readData FAIL code=%d\n", state);
        }
        return;
    }

    MeshPacket pkt;
    if (!MeshCodec::decode(buf, pkt)) {
        Serial.println(F("[RX] decode FAIL (CRC body)"));
        return;
    }

    // Эхо своих пакетов (ретранслятор отослал нам обратно).
    if (pkt.device_id == DEVICE_ID) {
        return;
    }

    float rssi = radio.getRSSI();
    Serial.printf("[RX] type=%u dev=%u ttl=%u  RSSI=%.0f дБм\n",
                  (unsigned)pkt.type, pkt.device_id, pkt.ttl, rssi);

    // Пока обрабатываем только CHAT — PING / SOS от других туристов
    // нам неинтересны (этим занимается база). Можно расширить позже.
    if (pkt.type != PacketType::CHAT) return;

    // Дедуп ретрансляций (один CHAT может прийти 2-3 раза с разным TTL).
    uint32_t h = hash_payload(pkt.payload, MESH_PAYLOAD_SIZE);
    if (seen_recently(pkt.device_id, h)) {
        Serial.printf("[RX] CHAT dev=%u — дубликат ретрансляции\n", pkt.device_id);
        return;
    }

    // Достаём текст. Payload zero-padded — strnlen остановится на первом 0
    // или на конце буфера. text[48] всегда null после копии (см. ниже).
    char text[MESH_PAYLOAD_SIZE + 1];
    memcpy(text, pkt.payload, MESH_PAYLOAD_SIZE);
    text[MESH_PAYLOAD_SIZE] = 0;

    Serial.printf("[RX] CHAT dev=%u: %s\n", pkt.device_id, text);
    inbox_push(pkt.device_id, text);
}

// ---------------------------------------------------------------------------
static void setup_radio() {
    loraSPI.begin(LORA_SCK, LORA_MISO, LORA_MOSI, LORA_CS);

    Serial.print(F("[SX1262] init ... "));
    int state = radio.begin(RADIO_FREQ, RADIO_BW, RADIO_SF, RADIO_CR,
                            RADIOLIB_SX126X_SYNC_WORD_PRIVATE,
                            RADIO_TX_POWER, RADIO_PREAMBLE, RADIO_TCXO_V);
    if (state != RADIOLIB_ERR_NONE) {
        Serial.printf("FAIL, code %d\n", state);
        while (true) { delay(1000); }
    }
    Serial.println(F("OK"));

    radio.setCRC(2);
    radio.setDio2AsRfSwitch(true);

    Serial.printf("[RADIO] %.1f МГц, SF%u, BW%.0f кГц, TX=%d дБм\n",
                  RADIO_FREQ, RADIO_SF, RADIO_BW, RADIO_TX_POWER);

    // RX-цикл: ISR на RX_DONE + сразу переходим в continuous receive.
    // setPacketReceivedAction внутри настроит DIO1 на нужный IRQ.
    radio.setPacketReceivedAction(on_rx_done);
    int rx_state = radio.startReceive();
    if (rx_state != RADIOLIB_ERR_NONE) {
        Serial.printf("[RX] startReceive FAIL code=%d — приём отключён\n", rx_state);
    } else {
        Serial.println(F("[RX] слушаем эфир (ответы от базы пойдут в inbox)"));
    }
}

// ---------------------------------------------------------------------------
static void setup_wifi_ap() {
    snprintf(g_ap_ssid, sizeof(g_ap_ssid), "MeshNet-%03u", DEVICE_ID);

    WiFi.mode(WIFI_AP);
    bool ok = WiFi.softAP(g_ap_ssid);
    if (!ok) {
        Serial.println(F("[WiFi] softAP FAIL"));
        return;
    }
    IPAddress ip = WiFi.softAPIP();
    Serial.printf("[WiFi] AP \"%s\" поднята, IP %s\n",
                  g_ap_ssid, ip.toString().c_str());
}

// ---------------------------------------------------------------------------
// HTTPS-сервер крутим в отдельной задаче на Core 0 (там же сидит Wi-Fi-стек).
// loop() с LoRa остаётся на Core 1 — TLS-handshake его не замораживает.
static void web_task(void* /*arg*/) {
    prepare_index();

    g_server.registerNode(new ResourceNode("/",            "GET",  &handle_root));
    g_server.registerNode(new ResourceNode("/api/sos",     "POST", &handle_sos));
    g_server.registerNode(new ResourceNode("/api/gps",     "POST", &handle_gps));
    g_server.registerNode(new ResourceNode("/api/chat",    "POST", &handle_chat));
    g_server.registerNode(new ResourceNode("/api/inbox",   "GET",  &handle_inbox));
    g_server.registerNode(new ResourceNode("/api/status",  "GET",  &handle_status));
    g_server.setDefaultNode(new ResourceNode("",           "GET",  &handle_404));

    g_server.start();
    if (g_server.isRunning()) {
        Serial.println(F("[HTTPS] сервер на 443 запущен"));
    } else {
        Serial.println(F("[HTTPS] FAIL — сервер не запустился"));
    }

    for (;;) {
        g_server.loop();
        delay(1);
    }
}

// ---------------------------------------------------------------------------
// Передать один пакет в эфир. Возвращает true при успехе.
// Лог печатаем ОДНОЙ строкой после transmit() — иначе HTTPS-таск с Core 0
// успевает влезть в середину "[TX] ... OK" пока радио занято ~700 мс.
static bool transmit_packet(const MeshPacket& pkt, const char* tag) {
    uint8_t buf[MESH_PACKET_SIZE];
    MeshCodec::encode(pkt, buf);

    unsigned long t0 = millis();
    int state = radio.transmit(buf, MESH_PACKET_SIZE);
    unsigned long dt = millis() - t0;

    char coords[40];
    if (g_gps_valid) {
        snprintf(coords, sizeof(coords), "%.6f, %.6f",
                 pkt.latitude / 1e6, pkt.longitude / 1e6);
    } else {
        strncpy(coords, "GPS нет", sizeof(coords));
    }

    if (state == RADIOLIB_ERR_NONE) {
        Serial.printf("[TX] %s OK %lu мс  @ %s\n", tag, dt, coords);
    } else {
        Serial.printf("[TX] %s FAIL code=%d  @ %s\n", tag, state, coords);
    }

    // Чип после transmit() — в STDBY. Возвращаем в RX, иначе пропустим
    // следующий ответ от базы. Делаем это всегда, даже если TX FAIL —
    // приёмник полезнее зависшего «ничего не делаем».
    int rx_state = radio.startReceive();
    if (rx_state != RADIOLIB_ERR_NONE) {
        Serial.printf("[RX] startReceive после TX FAIL code=%d\n", rx_state);
    }

    return state == RADIOLIB_ERR_NONE;
}

// ---------------------------------------------------------------------------
// Берём текущие координаты: GPS из браузера (если приходил) либо (0, 0).
static void fill_coords(MeshPacket& pkt) {
    if (g_gps_valid) {
        pkt.latitude  = g_gps_lat_e6;
        pkt.longitude = g_gps_lon_e6;
    } else {
        pkt.latitude  = STUB_LAT_E6;
        pkt.longitude = STUB_LON_E6;
    }
}

static void send_ping() {
    MeshPacket pkt;
    pkt.type      = PacketType::PING;
    pkt.device_id = DEVICE_ID;
    pkt.channel   = DEVICE_CHANNEL;
    pkt.ttl       = 3;
    fill_coords(pkt);
    make_ping_payload(pkt.payload, /*battery=*/100, /*rssi=*/0, /*seq=*/g_seq);

    char tag[16];
    snprintf(tag, sizeof(tag), "PING #%u", g_seq);
    transmit_packet(pkt, tag);
    g_seq++;
}

static void send_sos_one() {
    MeshPacket pkt;
    pkt.type      = PacketType::SOS;
    pkt.device_id = DEVICE_ID;
    pkt.channel   = DEVICE_CHANNEL;
    pkt.ttl       = 3;
    fill_coords(pkt);
    // Тип взвёл web-обработчик при POST /api/sos (см. handle_sos).
    make_sos_payload(pkt.payload, (SosType)g_sos_type, "");

    char tag[16];
    snprintf(tag, sizeof(tag), "SOS t=%u", g_sos_type);
    transmit_packet(pkt, tag);
}

static void send_chat() {
    // Локальная копия — чтобы web-task не успел перетереть пока transmit идёт.
    char text[sizeof(g_chat_pending)];
    memcpy(text, (const void*)g_chat_pending, sizeof(text));
    text[sizeof(text) - 1] = 0;

    MeshPacket pkt;
    pkt.type      = PacketType::CHAT;
    pkt.device_id = DEVICE_ID;
    pkt.channel   = DEVICE_CHANNEL;
    pkt.ttl       = 3;
    fill_coords(pkt);
    make_chat_payload(pkt.payload, text);

    transmit_packet(pkt, "CHAT");
}

// ---------------------------------------------------------------------------
void setup() {
    Serial.begin(115200);
    delay(500);
    Serial.println();
    Serial.println(F("=== Mesh-net Тропы — ESP32 терминал ==="));
    Serial.printf("Device ID: %u, канал: %s\n",
                  DEVICE_ID,
                  DEVICE_CHANNEL == Channel::TOURIST ? "TOURIST" : "RESCUE");

    setup_radio();
    setup_wifi_ap();

    // HTTPS — в отдельной задаче на Core 0. Стек 12 КБ: TLS-handshake
    // на mbedTLS требует около 8 КБ + наш буфер запросов.
    xTaskCreatePinnedToCore(web_task, "web_https", 12288, NULL, 1, NULL, 0);
}

// ---------------------------------------------------------------------------
void loop() {
    uint32_t now = millis();

    // 0. Принят пакет? Обрабатываем сразу — сообщения от базы важнее
    // очередного PING. process_rx() сам перезапускает приёмник.
    if (g_rx_flag) {
        g_rx_flag = false;
        process_rx();
    }

    // 1. HTTP-обработчик попросил SOS — взводим бёрст
    if (g_sos_requested) {
        g_sos_requested = false;
        g_sos_pending   = SOS_REPEAT;
        g_sos_next_ms   = now;  // первый пакет сразу
        Serial.printf("[SOS] начало бёрста: %u пакетов через %u мс\n",
                      SOS_REPEAT, SOS_INTERVAL_MS);
    }

    // 2. Идёт SOS-бёрст? Шлём пакеты по графику, всё остальное тормозим
    if (g_sos_pending > 0) {
        if ((int32_t)(now - g_sos_next_ms) >= 0) {
            send_sos_one();
            g_sos_pending--;
            g_sos_next_ms = now + SOS_INTERVAL_MS;

            if (g_sos_pending == 0) {
                g_sos_done_until = millis() + 5000;  // /api/status вернёт "done" 5 сек
                Serial.println(F("[SOS] бёрст завершён"));
            }
        }
    } else if (g_chat_requested || g_chat_burst > 0) {
        // 3. CHAT-бёрст: 2 повтора с разным от базы интервалом, чтобы пробить
        // одновременную TX-коллизию. Текст один и тот же — дедуп на стороне
        // lora-station по hash payload.
        if (g_chat_requested) {
            g_chat_requested = false;
            g_chat_burst   = CHAT_REPEAT;
            g_chat_next_ms = now;     // первая копия сразу
        }
        if ((int32_t)(now - g_chat_next_ms) >= 0) {
            send_chat();
            g_chat_burst--;
            g_chat_next_ms = now + CHAT_INTERVAL_MS;
        }
    } else {
        // 4. Обычный PING — только когда не идёт SOS и нет чат-сообщений
        if (now - g_last_ping_ms >= PING_INTERVAL_MS) {
            g_last_ping_ms = now;
            send_ping();
        }
    }

    delay(10);
}
