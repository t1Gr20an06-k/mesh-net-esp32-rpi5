// ============================================================================
// Mesh-net Тропы — LoRa-снифер на Raspberry Pi 5
// Этап 1a: приём 64-байтных пакетов, декодирование через общий кодек,
//          печать в консоль с RSSI/SNR.
// ============================================================================
// Железо:    RPi5 + HT-RA62 (SX1262), подключение по SPI0
// Библиотеки: RadioLib + lgpio (через PiHal из RadioLib/examples/NonArduino/Raspberry/)
// Частота:   868 МГц, SF=10, BW=125 кГц, CR=4/5 — должны совпадать с ESP32!
// ============================================================================

#include <cstdio>
#include <cstdint>
#include <cstring>
#include <cinttypes>
#include <unistd.h>
#include <signal.h>

#include "PiHal.h"           // из RadioLib/examples/NonArduino/Raspberry
#include <RadioLib.h>
#include "MeshPacket.h"      // наш общий кодек из firmware/esp32-terminal/lib/mesh_packet/

// --- Пины RPi5 (BCM GPIO, НЕ физические номера пинов!) --------------------
// Соответствие распиновке в CLAUDE.md.
static const uint32_t PIN_CS    = 8;   // GPIO 8  — SPI0 CE0, физ. пин 24
static const uint32_t PIN_RESET = 22;  // GPIO 22 — физ. пин 15
static const uint32_t PIN_DIO1  = 23;  // GPIO 23 — физ. пин 16
static const uint32_t PIN_BUSY  = 24;  // GPIO 24 — физ. пин 18

// --- Параметры радио (должны совпадать с ESP32!) ---------------------------
static const float   RADIO_FREQ     = 868.0;
static const float   RADIO_BW       = 125.0;
static const uint8_t RADIO_SF       = 10;
static const uint8_t RADIO_CR       = 5;
static const int8_t  RADIO_TX_POWER = 14;
static const uint8_t RADIO_PREAMBLE = 8;
static const float   RADIO_TCXO_V   = 1.8;

// --- Глобальные объекты ----------------------------------------------------
// PiHal(0) = SPI шина 0 (spidev0.0)
static PiHal* hal = new PiHal(0);
static SX1262 radio = new Module(hal, PIN_CS, PIN_DIO1, PIN_RESET, PIN_BUSY);

static volatile bool g_stop = false;
static void on_sigint(int) { g_stop = true; }

// ---------------------------------------------------------------------------
static const char* pkt_type_name(uint8_t t) {
    switch ((PacketType)t) {
        case PacketType::PING: return "PING";
        case PacketType::CHAT: return "CHAT";
        case PacketType::SOS:  return "SOS ";
        case PacketType::ACK:  return "ACK ";
        default:               return "?   ";
    }
}

static void print_hex(const uint8_t* buf, int len) {
    for (int i = 0; i < len; i++) printf("%02X", buf[i]);
}

// ---------------------------------------------------------------------------
int main() {
    signal(SIGINT, on_sigint);
    printf("=== Mesh-net Тропы — LoRa снифер (RPi5) ===\n");
    printf("Слушаем %.1f МГц, SF%u, BW%.0f кГц, CR 4/%u\n",
           RADIO_FREQ, RADIO_SF, RADIO_BW, RADIO_CR);

    printf("[SX1262] init ... ");
    fflush(stdout);
    int state = radio.begin(RADIO_FREQ, RADIO_BW, RADIO_SF, RADIO_CR,
                            RADIOLIB_SX126X_SYNC_WORD_PRIVATE,
                            RADIO_TX_POWER, RADIO_PREAMBLE, RADIO_TCXO_V);
    if (state != RADIOLIB_ERR_NONE) {
        printf("FAIL, code %d\n", state);
        printf("Проверь: SPI включён (ls /dev/spidev0.*), пины подключены, питание 3.3В.\n");
        return 1;
    }
    printf("OK\n");

    radio.setCRC(2);
    radio.setDio2AsRfSwitch(true);

    printf("[RX] ожидание пакетов (Ctrl-C для выхода)...\n\n");

    uint8_t buf[MESH_PACKET_SIZE];
    uint32_t rx_count = 0, crc_bad = 0;

    while (!g_stop) {
        // Приём с таймаутом, чтобы можно было корректно выйти по Ctrl-C
        int st = radio.receive(buf, MESH_PACKET_SIZE);

        if (st == RADIOLIB_ERR_NONE) {
            float rssi = radio.getRSSI();
            float snr  = radio.getSNR();
            rx_count++;

            MeshPacket pkt;
            bool ok = MeshCodec::decode(buf, pkt);
            if (!ok) crc_bad++;

            printf("[%6u] %s  %s  dev=%u ch=%u ttl=%u lat=%d lon=%d  "
                   "RSSI=%.1f дБм  SNR=%.1f дБ\n",
                   rx_count,
                   ok ? "OK " : "CRC",
                   pkt_type_name((uint8_t)pkt.type),
                   pkt.device_id,
                   (uint8_t)pkt.channel,
                   pkt.ttl,
                   pkt.latitude,
                   pkt.longitude,
                   rssi, snr);

            if (!ok) {
                printf("         raw: "); print_hex(buf, MESH_PACKET_SIZE); printf("\n");
            }
        } else if (st == RADIOLIB_ERR_RX_TIMEOUT) {
            // норма, просто ещё ничего не прилетело
        } else if (st == RADIOLIB_ERR_CRC_MISMATCH) {
            printf("[RX] LoRa-CRC error\n");
        } else {
            printf("[RX] error %d\n", st);
            usleep(100000);
        }
    }

    printf("\nПринято: %u пакетов, CRC-ошибок на уровне пакета: %u\n",
           rx_count, crc_bad);
    return 0;
}
