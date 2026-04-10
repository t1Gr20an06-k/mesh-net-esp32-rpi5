#pragma once
#include <stdint.h>
#include <string.h>

// Размер пакета фиксированный — 64 байта
static const uint8_t MESH_PACKET_SIZE = 64;
static const uint8_t MESH_PAYLOAD_SIZE = 48;
static const uint8_t MESH_PROTO_VERSION = 1;

// Типы пакетов
enum class PacketType : uint8_t {
    PING = 0,
    CHAT = 1,
    SOS  = 2,
    ACK  = 3,
};

// Каналы
enum class Channel : uint8_t {
    TOURIST = 0,
    RESCUE  = 1,
};

// Типы SOS (первый байт payload у SOS-пакета)
enum class SosType : uint8_t {
    UNKNOWN  = 0,
    FALL     = 1,
    MEDICAL  = 2,
    LOST     = 3,
    WEATHER  = 4,
};

// Структура пакета в памяти
struct MeshPacket {
    uint8_t  version;                    // [0]
    PacketType type;                     // [1]
    uint16_t device_id;                  // [2-3]  big-endian на проводе
    Channel  channel;                    // [4]
    uint8_t  ttl;                        // [5]
    int32_t  latitude;                   // [6-9]  × 1e6, big-endian
    int32_t  longitude;                  // [10-13] × 1e6, big-endian
    uint8_t  payload[MESH_PAYLOAD_SIZE]; // [14-61]
    uint16_t crc16;                      // [62-63] big-endian

    // Инициализация с нулями
    MeshPacket() {
        version   = MESH_PROTO_VERSION;
        type      = PacketType::PING;
        device_id = 0;
        channel   = Channel::TOURIST;
        ttl       = 3;
        latitude  = 0;
        longitude = 0;
        memset(payload, 0, MESH_PAYLOAD_SIZE);
        crc16 = 0;
    }
};

// Вспомогательные структуры для payload

// Payload PING — первые 4 байта, остальное 0x00
struct PingPayload {
    uint8_t  battery_pct;  // заряд 0–100 %
    int8_t   rssi_last;    // RSSI последнего принятого пакета, 0=нет данных
    uint16_t seq;          // порядковый номер (big-endian на проводе)
};

// --- Кодек ---

class MeshCodec {
public:
    // Упаковать структуру в 64-байтный буфер (с расчётом CRC)
    static void encode(const MeshPacket& pkt, uint8_t out[MESH_PACKET_SIZE]);

    // Распаковать 64-байтный буфер в структуру
    // Возвращает false если CRC не совпадает
    static bool decode(const uint8_t in[MESH_PACKET_SIZE], MeshPacket& out);

    // Рассчитать CRC-16/CCITT-FALSE от первых 62 байт буфера
    static uint16_t crc16_ccitt(const uint8_t* data, uint16_t len);
};

// --- Вспомогательные функции для payload ---

// Заполнить payload PING
void make_ping_payload(uint8_t out[MESH_PAYLOAD_SIZE],
                       uint8_t battery_pct,
                       int8_t  rssi_last,
                       uint16_t seq);

// Заполнить payload CHAT (текст обрезается до 48 байт)
void make_chat_payload(uint8_t out[MESH_PAYLOAD_SIZE], const char* text);

// Заполнить payload SOS
void make_sos_payload(uint8_t out[MESH_PAYLOAD_SIZE],
                      SosType sos_type,
                      const char* message);

// Заполнить payload ACK
void make_ack_payload(uint8_t out[MESH_PAYLOAD_SIZE], uint16_t ack_device_id);
