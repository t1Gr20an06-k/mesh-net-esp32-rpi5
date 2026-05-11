#pragma once
#include <stdint.h>
#include <string.h>

// Размер пакета фиксированный — 82 байта (v2, с ACK-протоколом).
// v1 был 64 байта — НЕ совместимо, все узлы должны быть перепрошиты синхронно.
static const uint8_t MESH_PACKET_SIZE  = 82;
static const uint8_t MESH_PAYLOAD_SIZE = 64;
static const uint8_t MESH_PROTO_VERSION = 2;

// Биты в поле flags на проводе (см. proto/messages.proto и packet.py).
// В структуре MeshPacket поля channel/want_ack/is_ack отдельные —
// flags-байт собирается только в encode/decode, чтобы пользовательский
// код мог писать `pkt.channel = Channel::TOURIST` как раньше.
static const uint8_t FLAG_WANT_ACK      = 0x01;
static const uint8_t FLAG_IS_ACK        = 0x02;
static const uint8_t FLAG_CHANNEL_MASK  = 0x0C;   // биты 2-3
static const uint8_t FLAG_CHANNEL_SHIFT = 2;

// Типы пакетов
enum class PacketType : uint8_t {
    PING = 0,
    CHAT = 1,
    SOS  = 2,
    ACK  = 3,
};

// Каналы — на проводе живут в битах 2-3 поля flags, не отдельным байтом.
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

// Структура пакета в памяти.
// На проводе layout другой (см. encode/decode):
//   [0]      version       — 2
//   [1]      type
//   [2]      flags         — want_ack/is_ack/channel битами
//   [3]      ttl
//   [4-5]    device_id     — big-endian
//   [6-7]    packet_id     — big-endian, монотонный счётчик у источника
//   [8-11]   latitude      — × 1e6, big-endian
//   [12-15]  longitude     — × 1e6, big-endian
//   [16-79]  payload       — 64 байта
//   [80-81]  crc16         — big-endian
struct MeshPacket {
    uint8_t  version;
    PacketType type;
    uint16_t device_id;
    uint16_t packet_id;       // монотонный счётчик у источника, для ACK-matching
    Channel  channel;
    uint8_t  ttl;
    bool     want_ack;        // отправитель ждёт ACK
    bool     is_ack;          // этот пакет САМ — ACK
    int32_t  latitude;
    int32_t  longitude;
    uint8_t  payload[MESH_PAYLOAD_SIZE];
    uint16_t crc16;

    MeshPacket() {
        version   = MESH_PROTO_VERSION;
        type      = PacketType::PING;
        device_id = 0;
        packet_id = 0;
        channel   = Channel::TOURIST;
        ttl       = 3;
        want_ack  = false;
        is_ack    = false;
        latitude  = 0;
        longitude = 0;
        memset(payload, 0, MESH_PAYLOAD_SIZE);
        crc16 = 0;
    }
};

// Payload PING — первые 4 байта, остальное 0x00
struct PingPayload {
    uint8_t  battery_pct;  // заряд 0–100 %
    int8_t   rssi_last;    // RSSI последнего принятого пакета, 0=нет данных
    uint16_t seq;          // порядковый номер (big-endian на проводе) — НЕ путать с packet_id
};

// --- Кодек ---

class MeshCodec {
public:
    // Упаковать структуру в 82-байтный буфер (с расчётом CRC)
    static void encode(const MeshPacket& pkt, uint8_t out[MESH_PACKET_SIZE]);

    // Распаковать 82-байтный буфер в структуру.
    // Возвращает false если CRC не совпадает или поля невалидны.
    static bool decode(const uint8_t in[MESH_PACKET_SIZE], MeshPacket& out);

    // Рассчитать CRC-16/CCITT-FALSE от первых 80 байт буфера
    static uint16_t crc16_ccitt(const uint8_t* data, uint16_t len);
};

// --- Вспомогательные функции для payload ---

void make_ping_payload(uint8_t out[MESH_PAYLOAD_SIZE],
                       uint8_t battery_pct,
                       int8_t  rssi_last,
                       uint16_t seq);

void make_chat_payload(uint8_t out[MESH_PAYLOAD_SIZE], const char* text);

void make_sos_payload(uint8_t out[MESH_PAYLOAD_SIZE],
                      SosType sos_type,
                      const char* message);

// ACK-payload: кому ACK предназначен (originator) + какой packet_id подтверждаем.
// Source ACK (наш device_id) идёт в основном поле pkt.device_id, дублировать не надо.
void make_ack_payload(uint8_t out[MESH_PAYLOAD_SIZE],
                      uint16_t ack_for_device_id,
                      uint16_t ack_for_packet_id);

// Распаковать ACK-payload: вернуть (ack_for_device_id, ack_for_packet_id) через ссылки.
void parse_ack_payload(const uint8_t in[MESH_PAYLOAD_SIZE],
                       uint16_t& ack_for_device_id,
                       uint16_t& ack_for_packet_id);
