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

// --- Encode ---

void MeshCodec::encode(const MeshPacket& pkt, uint8_t out[MESH_PACKET_SIZE]) {
    memset(out, 0, MESH_PACKET_SIZE);

    out[0] = pkt.version;
    out[1] = (uint8_t)pkt.type;

    // device_id big-endian
    out[2] = (pkt.device_id >> 8) & 0xFF;
    out[3] =  pkt.device_id       & 0xFF;

    out[4] = (uint8_t)pkt.channel;
    out[5] = pkt.ttl;

    // latitude big-endian int32
    out[6]  = (pkt.latitude >> 24) & 0xFF;
    out[7]  = (pkt.latitude >> 16) & 0xFF;
    out[8]  = (pkt.latitude >>  8) & 0xFF;
    out[9]  =  pkt.latitude        & 0xFF;

    // longitude big-endian int32
    out[10] = (pkt.longitude >> 24) & 0xFF;
    out[11] = (pkt.longitude >> 16) & 0xFF;
    out[12] = (pkt.longitude >>  8) & 0xFF;
    out[13] =  pkt.longitude        & 0xFF;

    // payload
    memcpy(&out[14], pkt.payload, MESH_PAYLOAD_SIZE);

    // CRC от первых 62 байт
    uint16_t crc = crc16_ccitt(out, 62);
    out[62] = (crc >> 8) & 0xFF;
    out[63] =  crc       & 0xFF;
}

// --- Decode ---

bool MeshCodec::decode(const uint8_t in[MESH_PACKET_SIZE], MeshPacket& out) {
    // Проверяем CRC
    uint16_t crc_calc = crc16_ccitt(in, 62);
    uint16_t crc_recv = ((uint16_t)in[62] << 8) | in[63];
    if (crc_calc != crc_recv) {
        return false;
    }

    out.version   = in[0];
    out.type      = (PacketType)in[1];
    out.device_id = ((uint16_t)in[2] << 8) | in[3];
    out.channel   = (Channel)in[4];
    out.ttl       = in[5];

    // latitude — восстанавливаем знак через union-трюк (int32 из 4 байт BE)
    out.latitude  = ((int32_t)(int8_t)in[6] << 24)
                  | ((int32_t)in[7] << 16)
                  | ((int32_t)in[8] <<  8)
                  |  (int32_t)in[9];

    out.longitude = ((int32_t)(int8_t)in[10] << 24)
                  | ((int32_t)in[11] << 16)
                  | ((int32_t)in[12] <<  8)
                  |  (int32_t)in[13];

    memcpy(out.payload, &in[14], MESH_PAYLOAD_SIZE);
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
    // strncpy без null-terminator (весь буфер под текст)
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

void make_ack_payload(uint8_t out[MESH_PAYLOAD_SIZE], uint16_t ack_device_id) {
    memset(out, 0, MESH_PAYLOAD_SIZE);
    out[0] = (ack_device_id >> 8) & 0xFF;
    out[1] =  ack_device_id       & 0xFF;
}
