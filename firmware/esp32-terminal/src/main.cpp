// ============================================================================
// Mesh-net Тропы — прошивка ESP32-S3 терминала туриста
// Этап 1a: PING-передатчик (отправка каждые 10 сек, координаты-заглушка)
// ============================================================================
// Железо:   ESP32-S3 N16R8 + HT-RA62 (SX1262)
// Пины:     см. platformio.ini (build_flags)
// Частота:  868 МГц, SF=10, BW=125 кГц, CR=4/5
// ============================================================================

#include <Arduino.h>
#include <SPI.h>
#include <RadioLib.h>
#include "MeshPacket.h"

// --- Настройки этого терминала ---------------------------------------------
static const uint16_t DEVICE_ID        = 1;                // ID туриста
static const Channel  DEVICE_CHANNEL   = Channel::TOURIST; // канал
static const uint32_t PING_INTERVAL_MS = 10000;            // раз в 10 сек

// Координаты-заглушка (район Москвы). Позже их будет слать телефон по WebSocket.
static const int32_t STUB_LAT_E6 = 55750000;
static const int32_t STUB_LON_E6 = 37620000;

// --- Параметры радио (должны совпадать с RPi5!) ----------------------------
static const float   RADIO_FREQ      = LORA_FREQ;  // 868.0 МГц из platformio.ini
static const float   RADIO_BW        = 125.0;      // кГц
static const uint8_t RADIO_SF        = 10;         // spreading factor
static const uint8_t RADIO_CR        = 5;          // 4/5 coding rate
static const int8_t  RADIO_TX_POWER  = 14;         // дБм, EU/RU ISM limit
static const uint8_t RADIO_PREAMBLE  = 8;          // symbols
static const float   RADIO_TCXO_V    = 1.8;        // В, стандарт для HT-RA62

// --- Экземпляр радиомодуля -------------------------------------------------
// RadioLib хочет отдельный SPIClass для не-дефолтных пинов
SPIClass loraSPI(HSPI);
SX1262 radio = new Module(LORA_CS, LORA_DIO1, LORA_RESET, LORA_BUSY, loraSPI);

static uint16_t g_seq = 0;

// ---------------------------------------------------------------------------
void setup() {
    Serial.begin(115200);
    delay(500);
    Serial.println();
    Serial.println(F("=== Mesh-net Тропы — ESP32 терминал ==="));
    Serial.printf("Device ID: %u, канал: %s\n",
                  DEVICE_ID,
                  DEVICE_CHANNEL == Channel::TOURIST ? "TOURIST" : "RESCUE");

    // SPI на кастомных пинах ESP32-S3
    loraSPI.begin(LORA_SCK, LORA_MISO, LORA_MOSI, LORA_CS);

    Serial.print(F("[SX1262] init ... "));
    int state = radio.begin(RADIO_FREQ, RADIO_BW, RADIO_SF, RADIO_CR,
                            RADIOLIB_SX126X_SYNC_WORD_PRIVATE,
                            RADIO_TX_POWER, RADIO_PREAMBLE, RADIO_TCXO_V);
    if (state != RADIOLIB_ERR_NONE) {
        Serial.printf("FAIL, code %d\n", state);
        Serial.println(F("Проверь пины в platformio.ini и питание модуля 3.3В."));
        while (true) { delay(1000); }
    }
    Serial.println(F("OK"));

    // CRC LoRa (отдельно от нашего CRC-16 в пакете)
    radio.setCRC(2);
    // DIO2 управляет RF-свитчом на HT-RA62
    radio.setDio2AsRfSwitch(true);

    Serial.printf("[RADIO] %.1f МГц, SF%u, BW%.0f кГц, TX=%d дБм\n",
                  RADIO_FREQ, RADIO_SF, RADIO_BW, RADIO_TX_POWER);
}

// ---------------------------------------------------------------------------
void loop() {
    // 1. Собрать PING-пакет
    MeshPacket pkt;
    pkt.type      = PacketType::PING;
    pkt.device_id = DEVICE_ID;
    pkt.channel   = DEVICE_CHANNEL;
    pkt.ttl       = 3;
    pkt.latitude  = STUB_LAT_E6;
    pkt.longitude = STUB_LON_E6;
    make_ping_payload(pkt.payload,
                      /*battery_pct=*/100,
                      /*rssi_last=*/0,
                      /*seq=*/g_seq);

    // 2. Закодировать в 64 байта (с CRC)
    uint8_t buf[MESH_PACKET_SIZE];
    MeshCodec::encode(pkt, buf);

    // 3. Передать в эфир
    Serial.printf("[TX #%u] PING dev=%u lat=%d lon=%d ... ",
                  g_seq, DEVICE_ID, STUB_LAT_E6, STUB_LON_E6);
    unsigned long t0 = millis();
    int state = radio.transmit(buf, MESH_PACKET_SIZE);
    unsigned long dt = millis() - t0;
    if (state == RADIOLIB_ERR_NONE) {
        Serial.printf("OK (%lu мс)\n", dt);
    } else {
        Serial.printf("FAIL code=%d\n", state);
    }

    g_seq++;
    delay(PING_INTERVAL_MS);
}
