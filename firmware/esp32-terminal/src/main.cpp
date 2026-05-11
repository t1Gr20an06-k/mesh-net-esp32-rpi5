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
static const uint32_t PING_INTERVAL_MS = 20000;            // раз в 20 сек (реже = меньше collision-окон)

// SOS — параметры из CLAUDE.md
static const uint8_t  SOS_REPEAT       = 3;     // 3 пакета подряд
static const uint32_t SOS_INTERVAL_MS  = 500;   // интервал между ними

// CHAT — теперь с ACK-протоколом v2. Шлём 1 копию с want_ack=true,
// если ACK не пришёл за ACK_TIMEOUT_MS — retry (до MAX_RETRIES раз).
// Старый CHAT_REPEAT-бёрст убран: при наличии ACK слать 3 копии
// одновременно — двойной перерасход эфира.
static const uint32_t ACK_TIMEOUT_MS   = 4000;     // ожидание ACK после TX
static const uint8_t  MAX_RETRIES      = 3;        // после 1 + 3 = 4 попыток сдаёмся
static const uint8_t  PENDING_SIZE     = 4;        // одновременно отслеживаемых want_ack пакетов

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

// CHAT с ACK-протоколом v2: одна копия, потом retry по таймауту (см. g_pending).
// Без отдельного бёрста — было `g_chat_burst` / `CHAT_INTERVAL_MS` в v1.

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

// --- ACK-протокол: pending-таблица для CHAT (и SOS) с want_ack -------------
// Логика: отправили пакет с want_ack=true → положили копию в g_pending →
// ждём ACK с тем же packet_id. Если ACK пришёл — снимаем из pending.
// Если за ACK_TIMEOUT_MS не пришёл — retry с тем же packet_id (база
// дедупит копии через hash payload, не пишет дубль в БД).
// После MAX_RETRIES — сдаёмся, лог "не доставлено".
//
// pending хранится в RAM, при reset ESP32 теряется. Это допустимо: чат —
// не критичный канал, повторно отправит оператор.
struct PendingPacket {
    bool       used;            // занят ли слот
    uint16_t   packet_id;
    PacketType type;
    uint32_t   sent_ms;         // millis() последней (повторной) отправки
    uint8_t    retries;         // сколько уже было retry (0 — только первая отправка)
    MeshPacket pkt;             // полная копия для retransmit
};
static PendingPacket g_pending[PENDING_SIZE] = {};

// Монотонный счётчик исходящих, wraparound каждые 65536. На скорости
// 1 PING/20 сек + редкие CHAT — ~30 дней до первого пересечения, ACK
// за это время точно догонит или потеряется по таймауту.
static uint16_t g_packet_id = 0;

// Зарезервировать слот в pending. Если все заняты — возвращает -1 и кидаем
// warning; оператор увидит "не доставлено" только по таймауту последнего ACK.
static int8_t pending_alloc() {
    for (uint8_t i = 0; i < PENDING_SIZE; i++) {
        if (!g_pending[i].used) return (int8_t)i;
    }
    return -1;
}

// Снять слот по packet_id (вызывается при приёме ACK).
// Возвращает true если слот был найден и освобождён.
static bool pending_release(uint16_t pid) {
    for (uint8_t i = 0; i < PENDING_SIZE; i++) {
        if (g_pending[i].used && g_pending[i].packet_id == pid) {
            g_pending[i].used = false;
            return true;
        }
    }
    return false;
}

// Backoff между попытками: 4s → 6s → 9s. Фактически — base * 1.5^retries
// с округлением. Жёстко прибито табличкой чтобы не зависеть от float-арифметики.
static uint32_t retry_timeout_ms(uint8_t retries) {
    static const uint32_t SCHED[] = {4000, 6000, 9000, 13500};
    if (retries >= sizeof(SCHED)/sizeof(SCHED[0])) return SCHED[sizeof(SCHED)/sizeof(SCHED[0]) - 1];
    return SCHED[retries];
}

// Forward declarations — process_rx() ниже зовёт TX-функции, которые
// определены ещё дальше. В C++ без этих объявлений будет undefined identifier.
static void fill_coords(MeshPacket& pkt);
static bool transmit_packet(const MeshPacket& pkt, const char* tag);

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
    <textarea id="chatInput" rows="2" maxlength="64"
      placeholder="до 64 байт UTF-8 (~32 рус. букв)"></textarea>
    <div class="row">
      <span class="counter" id="chatCounter">0 / 64</span>
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
    chatCounter.textContent = len + ' / 64';
    chatCounter.style.color = len > 64 ? '#e57373' : '#888';
    chatBtn.disabled = !connOk || len === 0 || len > 64;
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
    Serial.printf("[RX] type=%u dev=%u pid=%u ttl=%u flags=0x%02X  RSSI=%.0f дБм\n",
                  (unsigned)pkt.type, pkt.device_id, pkt.packet_id, pkt.ttl,
                  (unsigned)(pkt.want_ack | (pkt.is_ack << 1)),
                  rssi);

    // 1) Пришёл ACK на наш пакет? Снимаем из pending.
    if (pkt.is_ack && pkt.type == PacketType::ACK) {
        uint16_t ack_dev, ack_pid;
        parse_ack_payload(pkt.payload, ack_dev, ack_pid);
        if (ack_dev != DEVICE_ID) {
            // ACK предназначен другому узлу — игнор (через нас ретранслировали).
            return;
        }
        if (pending_release(ack_pid)) {
            Serial.printf("[ACK] ✓ pkt=%u подтверждён базой dev=%u\n",
                          ack_pid, pkt.device_id);
        } else {
            // ACK на пакет которого нет в pending — либо мы его уже сняли
            // (повторный ACK от retry с другой стороны), либо после reset.
            Serial.printf("[ACK] pkt=%u — нет в pending (или поздно)\n", ack_pid);
        }
        return;
    }

    // 2) Обычный CHAT (или SOS, но для туриста SOS малоинтересен).
    //    PING от других туристов нам тоже не нужен — этим занимается база.
    if (pkt.type != PacketType::CHAT) return;

    // Дедуп ретрансляций (один CHAT может прийти 2-3 раза: напрямую,
    // через ретранслятор, а также при retry если первый ACK потерялся).
    uint32_t h = hash_payload(pkt.payload, MESH_PAYLOAD_SIZE);
    bool duplicate = seen_recently(pkt.device_id, h);

    if (!duplicate) {
        // Достаём текст. Payload zero-padded — strnlen остановится на первом 0
        // или на конце буфера. text[64] всегда null после копии.
        char text[MESH_PAYLOAD_SIZE + 1];
        memcpy(text, pkt.payload, MESH_PAYLOAD_SIZE);
        text[MESH_PAYLOAD_SIZE] = 0;

        Serial.printf("[RX] CHAT dev=%u: %s\n", pkt.device_id, text);
        inbox_push(pkt.device_id, text);
    } else {
        Serial.printf("[RX] CHAT dev=%u pkt=%u — дубль (но ACK всё равно шлём)\n",
                      pkt.device_id, pkt.packet_id);
    }

    // 3) Если отправитель просил ACK — шлём подтверждение, даже на дубль.
    //    Дубль обычно значит что ACK на предыдущую копию не дошёл; шлём
    //    снова, иначе отправитель так и будет ретраить до MAX_RETRIES.
    if (pkt.want_ack) {
        MeshPacket ack;
        ack.type      = PacketType::ACK;
        ack.device_id = DEVICE_ID;
        ack.packet_id = ++g_packet_id;
        ack.channel   = DEVICE_CHANNEL;
        ack.ttl       = 3;
        ack.is_ack    = true;
        ack.want_ack  = false;             // ACK сам не подтверждается
        fill_coords(ack);
        make_ack_payload(ack.payload, pkt.device_id, pkt.packet_id);
        transmit_packet(ack, "ACK");
    }
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
// Listen-before-talk по Meshtastic-стилю (RadioLibInterface::canSendImmediately).
// Перед TX делаем CAD-scan: чип ловит LoRa-преамбулу за ~2 символа (≈8 мс
// на SF10/BW125). Если активность есть — экспоненциальный backoff и retry.
// Если CW исчерпался — передаём всё равно (пакет важен, особенно SOS).
//
// КРИТИЧНО: scanChannel() переключает IRQ-маску чипа на CAD-события и
// затирает наш RX-callback из setPacketReceivedAction. Без явного восстановления
// после CAD приёмник физически работает, но ISR не дёргается → g_rx_flag не
// взводится → process_rx() не зовётся → сообщения от базы теряются.
// Поэтому в конце восстанавливаем callback. Это то место, где у нас в
// прошлой попытке RX «молча умирал».
static bool wait_for_clear_channel() {
    // Pre-CAD jitter: КРИТИЧЕСКИ ВАЖНО при синхронных нажатиях. Без него
    // оба узла в момент T делают CAD одновременно, оба видят «свободно»,
    // оба уходят в TX → collision, оба пакета теряются.
    // 0-400 мс случайной задержки гарантирует что CAD у узлов разъезжается
    // по времени: если один начнёт TX через 50 мс, второй сделает CAD на
    // 250 мс и УВИДИТ преамбулу первого, отступит → пакет первого дойдёт.
    delay(esp_random() % 400);

    const uint16_t CW_MS_MIN = 60;     // contention window нижняя граница
    uint16_t cw_ms_max       = 250;    // верхняя граница, растёт при retry
    bool was_clear = false;
    for (int i = 0; i < 5; i++) {
        int state = radio.scanChannel();
        if (state == RADIOLIB_CHANNEL_FREE) { was_clear = true; break; }
        // Канал занят — случайный backoff в текущем CW. Окно растёт
        // экспоненциально (250/500/1000/2000): уменьшает шанс что узлы
        // снова попадут в один такт после ожидания.
        uint16_t span = cw_ms_max - CW_MS_MIN;
        delay(CW_MS_MIN + (esp_random() % span));
        cw_ms_max = (cw_ms_max < 2000) ? cw_ms_max * 2 : 2000;
    }
    // Восстанавливаем RX-callback после CAD (см. комментарий выше).
    radio.setPacketReceivedAction(on_rx_done);
    return was_clear;
}

// Передать один пакет в эфир. Возвращает true при успехе.
// Лог печатаем ОДНОЙ строкой после transmit() — иначе HTTPS-таск с Core 0
// успевает влезть в середину "[TX] ... OK" пока радио занято ~700 мс.
static bool transmit_packet(const MeshPacket& pkt, const char* tag) {
    uint8_t buf[MESH_PACKET_SIZE];
    MeshCodec::encode(pkt, buf);

    // CSMA/CA: дождёмся пока эфир освободится, или передадим как есть
    // если так и не освободился. После TX мы всё равно вернёмся в RX.
    bool clear = wait_for_clear_channel();
    if (!clear) {
        Serial.println(F("[LBT] канал занят, шлём всё равно"));
    }

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
    // Перестраховка: переустановим RX callback. wait_for_clear_channel()
    // его уже восстановил, но сам transmit() / startReceive() в RadioLib
    // тоже трогают IRQ-маску — без этой строки изредка ловили «чип в RX,
    // ISR молчит». Стоит копейки.
    radio.setPacketReceivedAction(on_rx_done);

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
    pkt.packet_id = ++g_packet_id;
    pkt.channel   = DEVICE_CHANNEL;
    pkt.ttl       = 3;
    // PING — broadcast «я тут», want_ack не нужен. Потеря одного PING
    // ничем не страшна: следующий придёт через 20 сек.
    pkt.want_ack  = false;
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
    pkt.packet_id = ++g_packet_id;
    pkt.channel   = DEVICE_CHANNEL;
    pkt.ttl       = 3;
    // SOS параноидально: 3 копии бёрста с want_ack=true. Каждая получит свой
    // packet_id, база ответит 3 ACK (которые мы залогируем). retry-через-ACK
    // для SOS НЕ делаем — он и так идёт 3 копии бёрстом, дополнительный retry
    // съест эфир на 30+ сек, тогда как бёрст укладывается в 1.5 сек.
    pkt.want_ack  = true;
    fill_coords(pkt);
    // Тип взвёл web-обработчик при POST /api/sos (см. handle_sos).
    make_sos_payload(pkt.payload, (SosType)g_sos_type, "");

    char tag[16];
    snprintf(tag, sizeof(tag), "SOS t=%u pkt=%u", g_sos_type, pkt.packet_id);
    transmit_packet(pkt, tag);
}

// CHAT с гарантированной доставкой: 1 копия с want_ack=true, ACK от базы
// разблокирует слот pending. Retry при таймауте обрабатывает loop().
static void send_chat() {
    // Локальная копия — чтобы web-task не успел перетереть пока transmit идёт.
    char text[sizeof(g_chat_pending)];
    memcpy(text, (const void*)g_chat_pending, sizeof(text));
    text[sizeof(text) - 1] = 0;

    MeshPacket pkt;
    pkt.type      = PacketType::CHAT;
    pkt.device_id = DEVICE_ID;
    pkt.packet_id = ++g_packet_id;
    pkt.channel   = DEVICE_CHANNEL;
    pkt.ttl       = 3;
    pkt.want_ack  = true;
    fill_coords(pkt);
    make_chat_payload(pkt.payload, text);

    // Резервируем слот ДО transmit — иначе если ACK успеет прилететь между
    // transmit() и pending_alloc() (≪ маловероятно, но), мы запишем
    // pending уже после освобождения.
    int8_t slot = pending_alloc();
    if (slot >= 0) {
        g_pending[slot].used      = true;
        g_pending[slot].packet_id = pkt.packet_id;
        g_pending[slot].type      = pkt.type;
        g_pending[slot].retries   = 0;
        g_pending[slot].pkt       = pkt;     // полная копия для retry
        g_pending[slot].sent_ms   = millis();
    } else {
        Serial.println(F("[CHAT] pending переполнен — без ACK-отслеживания"));
    }

    char tag[24];
    snprintf(tag, sizeof(tag), "CHAT pkt=%u", pkt.packet_id);
    transmit_packet(pkt, tag);
}

// Проверка retry для pending-таблицы. Зовётся раз в ~200 мс из loop().
// Если slot.sent_ms + retry_timeout < now — переотправляем (тот же
// packet_id, want_ack=true). После MAX_RETRIES — drop с warning'ом.
static void check_pending_retries() {
    uint32_t now = millis();
    for (uint8_t i = 0; i < PENDING_SIZE; i++) {
        PendingPacket& p = g_pending[i];
        if (!p.used) continue;
        uint32_t deadline = p.sent_ms + retry_timeout_ms(p.retries);
        if ((int32_t)(now - deadline) < 0) continue;     // ещё ждём

        if (p.retries >= MAX_RETRIES) {
            Serial.printf("[CHAT] ✗ pkt=%u — НЕ ДОСТАВЛЕНО после %u попыток\n",
                          p.packet_id, p.retries + 1);
            p.used = false;
            continue;
        }
        p.retries++;
        p.sent_ms = now;
        Serial.printf("[CHAT] ⟳ retry pkt=%u (попытка %u/%u)\n",
                      p.packet_id, p.retries + 1, MAX_RETRIES + 1);
        // Тот же packet_id — приёмник дедупит, ACK тоже придёт на тот же pid.
        char tag[24];
        snprintf(tag, sizeof(tag), "CHAT pkt=%u r=%u", p.packet_id, p.retries);
        transmit_packet(p.pkt, tag);
    }
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
    } else if (g_chat_requested) {
        // 3. CHAT — одна копия с want_ack. retry через таймаут в check_pending_retries.
        g_chat_requested = false;
        send_chat();
    } else {
        // 3.5. Retry для pending CHAT (не упирается в else-if выше, чтобы
        // retry работал параллельно с PING-расписанием — пустой такт не блокируется).
        check_pending_retries();
        // 4. Обычный PING — только когда не идёт SOS и нет чат-сообщений.
        // Случайный jitter ±1.5 сек на интервале: без него два узла синхронно
        // тикают каждые 10 сек и постоянно сталкиваются. Лимит 1500 мс на 10000 —
        // это 15% разброса, хватает чтобы окна расходились.
        uint32_t jitter = esp_random() % 3000;       // 0..2999
        uint32_t target = PING_INTERVAL_MS + jitter - 1500;  // ±1500 мс
        if (now - g_last_ping_ms >= target) {
            g_last_ping_ms = now;
            send_ping();
        }
    }

    delay(10);
}
