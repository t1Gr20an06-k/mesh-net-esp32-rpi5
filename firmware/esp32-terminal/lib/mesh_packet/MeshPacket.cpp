#include "MeshPacket.h"

// --- CRC-16/CCITT-FALSE ---
// Poly: 0x1021, Init: 0xFFFF, RefIn: false, RefOut: false, XorOut: 0x0000

uint16_t MeshCodec::crc16_ccitt(const uint8_t* data, uint16_t len) {
    uint16_t crc = 0xFFFF;
    for (uint16_t i = 0; i < len; i++) {
        crc ^= (uint16_t)data[i] << 8;
        for (uint8_t bit = 0; bit < 8; bit++) {
            if (crc & 0x8000) {
                crc = (crc << 1) ^ 0x1021;
            } else {
                crc <<= 1;
            }
        }
    }
    return crc;
}

static uint8_t build_flags(const MeshPacket& pkt) {
    uint8_t f = 0;
    if (pkt.want_ack) f |= FLAG_WANT_ACK;
    if (pkt.is_ack)   f |= FLAG_IS_ACK;
    f |= (((uint8_t)pkt.channel & 0x03) << FLAG_CHANNEL_SHIFT) & FLAG_CHANNEL_MASK;
    return f;
}

// --- Encode ---
// Layout см. в MeshPacket.h и proto/messages.proto.

void MeshCodec::encode(const MeshPacket& pkt, uint8_t out[MESH_PACKET_SIZE]) {
    memset(out, 0, MESH_PACKET_SIZE);

    out[0] = pkt.version;
    out[1] = (uint8_t)pkt.type;
    out[2] = build_flags(pkt);
    out[3] = pkt.ttl;

    // device_id big-endian
    out[4] = (pkt.device_id >> 8) & 0xFF;
    out[5] =  pkt.device_id       & 0xFF;

    // packet_id big-endian
    out[6] = (pkt.packet_id >> 8) & 0xFF;
    out[7] =  pkt.packet_id       & 0xFF;

    // latitude big-endian int32
    out[8]  = (pkt.latitude >> 24) & 0xFF;
    out[9]  = (pkt.latitude >> 16) & 0xFF;
    out[10] = (pkt.latitude >>  8) & 0xFF;
    out[11] =  pkt.latitude        & 0xFF;

    // longitude big-endian int32
    out[12] = (pkt.longitude >> 24) & 0xFF;
    out[13] = (pkt.longitude >> 16) & 0xFF;
    out[14] = (pkt.longitude >>  8) & 0xFF;
    out[15] =  pkt.longitude        & 0xFF;

    // payload (64 байта)
    memcpy(&out[16], pkt.payload, MESH_PAYLOAD_SIZE);

    // CRC от первых 80 байт
    uint16_t crc = crc16_ccitt(out, 80);
    out[80] = (crc >> 8) & 0xFF;
    out[81] =  crc       & 0xFF;
}

// --- Decode ---

bool MeshCodec::decode(const uint8_t in[MESH_PACKET_SIZE], MeshPacket& out) {
    // Проверяем CRC
    uint16_t crc_calc = crc16_ccitt(in, 80);
    uint16_t crc_recv = ((uint16_t)in[80] << 8) | in[81];
    if (crc_calc != crc_recv) {
        return false;
    }

    // Sanity-проверка полей: LoRa-CRC + наш CRC-16 — это всё ещё ~1/65536
    // шанс совпадения на шуме. Без валидации в нашу сеть пролезает «фантом».
    uint8_t v   = in[0];
    uint8_t t   = in[1];
    uint8_t fl  = in[2];
    uint8_t ttl = in[3];
    if (v != MESH_PROTO_VERSION) return false;
    if (t > (uint8_t)PacketType::ACK) return false;        // 0..3
    uint8_t ch_val = (fl & FLAG_CHANNEL_MASK) >> FLAG_CHANNEL_SHIFT;
    if (ch_val > (uint8_t)Channel::RESCUE) return false;   // 0..1
    if (ttl == 0 || ttl > 8) return false;                 // дефолт 3, запас x2

    out.version   = v;
    out.type      = (PacketType)t;
    out.ttl       = ttl;
    out.device_id = ((uint16_t)in[4] << 8) | in[5];
    out.packet_id = ((uint16_t)in[6] << 8) | in[7];
    out.channel   = (Channel)ch_val;
    out.want_ack  = (fl & FLAG_WANT_ACK) != 0;
    out.is_ack    = (fl & FLAG_IS_ACK)   != 0;

    // latitude — восстанавливаем знак через каст int8_t на старший байт
    out.latitude  = ((int32_t)(int8_t)in[8]  << 24)
                  | ((int32_t)in[9]  << 16)
                  | ((int32_t)in[10] <<  8)
                  |  (int32_t)in[11];

    out.longitude = ((int32_t)(int8_t)in[12] << 24)
                  | ((int32_t)in[13] << 16)
                  | ((int32_t)in[14] <<  8)
                  |  (int32_t)in[15];

    memcpy(out.payload, &in[16], MESH_PAYLOAD_SIZE);
    out.crc16 = crc_recv;

    return true;
}

// --- Вспомогательные функции payload ---

void make_ping_payload(uint8_t out[MESH_PAYLOAD_SIZE],
                       uint8_t battery_pct,
                       int8_t  rssi_last,
                       uint16_t seq) {
    memset(out, 0, MESH_PAYLOAD_SIZE);
    out[0] = battery_pct;
    out[1] = (uint8_t)rssi_last;
    out[2] = (seq >> 8) & 0xFF;
    out[3] =  seq       & 0xFF;
}

void make_chat_payload(uint8_t out[MESH_PAYLOAD_SIZE], const char* text) {
    memset(out, 0, MESH_PAYLOAD_SIZE);
    size_t len = strlen(text);
    if (len > MESH_PAYLOAD_SIZE) len = MESH_PAYLOAD_SIZE;
    memcpy(out, text, len);
}

void make_sos_payload(uint8_t out[MESH_PAYLOAD_SIZE],
                      SosType sos_type,
                      const char* message) {
    memset(out, 0, MESH_PAYLOAD_SIZE);
    out[0] = (uint8_t)sos_type;
    size_t len = strlen(message);
    if (len > MESH_PAYLOAD_SIZE - 1) len = MESH_PAYLOAD_SIZE - 1;
    memcpy(&out[1], message, len);
}

void make_ack_payload(uint8_t out[MESH_PAYLOAD_SIZE],
                      uint16_t ack_for_device_id,
                      uint16_t ack_for_packet_id) {
    memset(out, 0, MESH_PAYLOAD_SIZE);
    out[0] = (ack_for_device_id >> 8) & 0xFF;
    out[1] =  ack_for_device_id       & 0xFF;
    out[2] = (ack_for_packet_id >> 8) & 0xFF;
    out[3] =  ack_for_packet_id       & 0xFF;
}

void parse_ack_payload(const uint8_t in[MESH_PAYLOAD_SIZE],
                       uint16_t& ack_for_device_id,
                       uint16_t& ack_for_packet_id) {
    ack_for_device_id = ((uint16_t)in[0] << 8) | in[1];
    ack_for_packet_id = ((uint16_t)in[2] << 8) | in[3];
}
