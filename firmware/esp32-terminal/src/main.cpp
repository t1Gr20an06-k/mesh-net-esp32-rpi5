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

<script>
  const conn    = document.getElementById('conn');
  const sosBtns = document.querySelectorAll('button.sos');
  const status  = document.getElementById('status');
  const gpsEl   = document.getElementById('gps');
  const gpsBtn  = document.getElementById('gpsBtn');

  let watchId       = null;
  let lastGpsSendMs = 0;
  let pollTimer     = null;

  function setSosEnabled(en) {
    sosBtns.forEach(b => { b.disabled = !en; });
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
        return true;
    } else {
        Serial.printf("[TX] %s FAIL code=%d  @ %s\n", tag, state, coords);
        return false;
    }
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

    // 1. HTTP-обработчик попросил SOS — взводим бёрст
    if (g_sos_requested) {
        g_sos_requested = false;
        g_sos_pending   = SOS_REPEAT;
        g_sos_next_ms   = now;  // первый пакет сразу
        Serial.printf("[SOS] начало бёрста: %u пакетов через %u мс\n",
                      SOS_REPEAT, SOS_INTERVAL_MS);
    }

    // 2. Идёт SOS-бёрст? Шлём пакеты по графику, PING-и временно тормозим
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
    } else {
        // 3. Обычный PING — только когда не идёт SOS
        if (now - g_last_ping_ms >= PING_INTERVAL_MS) {
            g_last_ping_ms = now;
            send_ping();
        }
    }

    delay(10);
}
