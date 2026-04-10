"""
Кодек бинарного пакета LoRa — Mesh-net Тропы.
Формат: фиксированные 64 байта, схема — см. proto/messages.proto
"""

import struct
from dataclasses import dataclass, field
from enum import IntEnum

PACKET_SIZE   = 64
PAYLOAD_SIZE  = 48
PROTO_VERSION = 1


class PacketType(IntEnum):
    PING = 0
    CHAT = 1
    SOS  = 2
    ACK  = 3


class Channel(IntEnum):
    TOURIST = 0
    RESCUE  = 1


class SosType(IntEnum):
    UNKNOWN = 0
    FALL    = 1
    MEDICAL = 2
    LOST    = 3
    WEATHER = 4


@dataclass
class MeshPacket:
    version:   int        = PROTO_VERSION
    type:      PacketType = PacketType.PING
    device_id: int        = 0          # uint16
    channel:   Channel    = Channel.TOURIST
    ttl:       int        = 3          # uint8
    latitude:  int        = 0          # int32, × 1e6
    longitude: int        = 0          # int32, × 1e6
    payload:   bytes      = field(default_factory=lambda: bytes(PAYLOAD_SIZE))
    crc16:     int        = 0          # uint16, заполняется при encode


# --- CRC-16/CCITT-FALSE ---
# Poly: 0x1021, Init: 0xFFFF

def _crc16_ccitt(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc <<= 1
            crc &= 0xFFFF
    return crc


def encode(pkt: MeshPacket) -> bytes:
    """Упаковать MeshPacket в 64 байта. CRC рассчитывается автоматически."""
    # Payload должен быть ровно 48 байт
    payload = pkt.payload[:PAYLOAD_SIZE].ljust(PAYLOAD_SIZE, b'\x00')

    # Собираем первые 62 байта без CRC
    # Формат: B B H B B i i 48s
    #         ver type dev_id chan ttl lat lon payload
    body = struct.pack(
        '>BBHBBii48s',
        pkt.version,
        int(pkt.type),
        pkt.device_id,
        int(pkt.channel),
        pkt.ttl,
        pkt.latitude,
        pkt.longitude,
        payload,
    )
    assert len(body) == 62, f"body len={len(body)}, expected 62"

    crc = _crc16_ccitt(body)
    return body + struct.pack('>H', crc)


def decode(raw: bytes) -> MeshPacket:
    """
    Распаковать 64 байта в MeshPacket.
    Бросает ValueError если размер или CRC неверны.
    """
    if len(raw) != PACKET_SIZE:
        raise ValueError(f"Неверный размер пакета: {len(raw)}, ожидается {PACKET_SIZE}")

    crc_recv = struct.unpack('>H', raw[62:64])[0]
    crc_calc = _crc16_ccitt(raw[:62])
    if crc_recv != crc_calc:
        raise ValueError(f"CRC ошибка: получен 0x{crc_recv:04X}, рассчитан 0x{crc_calc:04X}")

    version, ptype, device_id, channel, ttl, lat, lon, payload = struct.unpack(
        '>BBHBBii48s', raw[:62]
    )

    return MeshPacket(
        version   = version,
        type      = PacketType(ptype),
        device_id = device_id,
        channel   = Channel(channel),
        ttl       = ttl,
        latitude  = lat,
        longitude = lon,
        payload   = payload,
        crc16     = crc_recv,
    )


# --- Вспомогательные функции для payload ---

def make_ping_payload(battery_pct: int, rssi_last: int, seq: int) -> bytes:
    """battery_pct: 0–100, rssi_last: int8 (0=нет данных), seq: uint16"""
    data = struct.pack('>Bbh', battery_pct, rssi_last, seq)  # 4 байта
    return data.ljust(PAYLOAD_SIZE, b'\x00')


def make_chat_payload(text: str) -> bytes:
    encoded = text.encode('utf-8')[:PAYLOAD_SIZE]
    return encoded.ljust(PAYLOAD_SIZE, b'\x00')


def make_sos_payload(sos_type: SosType, message: str) -> bytes:
    msg = message.encode('utf-8')[: PAYLOAD_SIZE - 1]
    return bytes([int(sos_type)]) + msg.ljust(PAYLOAD_SIZE - 1, b'\x00')


def make_ack_payload(ack_device_id: int) -> bytes:
    return struct.pack('>H', ack_device_id) + bytes(PAYLOAD_SIZE - 2)


# --- Вспомогательные функции для координат ---

def lat_lon_to_int(degrees: float) -> int:
    """Перевести градусы в int32 × 1e6"""
    return int(round(degrees * 1_000_000))


def int_to_lat_lon(value: int) -> float:
    """Обратно из int32 × 1e6 в градусы"""
    return value / 1_000_000


if __name__ == '__main__':
    # Быстрый тест кодека
    pkt = MeshPacket(
        type      = PacketType.PING,
        device_id = 42,
        channel   = Channel.TOURIST,
        ttl       = 3,
        latitude  = lat_lon_to_int(43.45),
        longitude = lat_lon_to_int(41.20),
        payload   = make_ping_payload(battery_pct=85, rssi_last=-90, seq=1),
    )

    raw = encode(pkt)
    assert len(raw) == PACKET_SIZE, "Размер пакета неверный!"

    pkt2 = decode(raw)
    assert pkt2.device_id == 42
    assert pkt2.latitude  == lat_lon_to_int(43.45)
    assert pkt2.longitude == lat_lon_to_int(41.20)
    assert pkt2.type      == PacketType.PING

    print(f"OK: {PACKET_SIZE} байт, device_id={pkt2.device_id}, "
          f"lat={int_to_lat_lon(pkt2.latitude):.6f}, "
          f"lon={int_to_lat_lon(pkt2.longitude):.6f}, "
          f"CRC=0x{pkt2.crc16:04X}")
