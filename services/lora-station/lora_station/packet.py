"""
Кодек бинарного пакета LoRa — Mesh-net Тропы (протокол v2).
Формат: фиксированные 82 байта, схема — см. proto/messages.proto

Должен 1-в-1 совпадать с C++ кодеком в firmware/esp32-terminal/lib/mesh_packet/
(CRC-16/CCITT-FALSE, big-endian).

⚠ Изменение vs v1: пакет вырос с 64 до 82 байт, payload 48→64, добавлены
`packet_id` (uint16) и `flags` (uint8 — want_ack/is_ack/channel). Старая версия
протокола (version=1) не поддерживается — decode её отвергнет sanity-чеком.

Channel переехал в flags (биты 2-3), отдельного байта в layout нет —
это позволило сохранить header в 16 байт и оставить payload ровно 64.
"""

import struct
from dataclasses import dataclass, field
from enum import IntEnum

PACKET_SIZE   = 82
PAYLOAD_SIZE  = 64
PROTO_VERSION = 2

# Биты в поле flags. Документация — proto/messages.proto.
FLAG_WANT_ACK   = 0x01
FLAG_IS_ACK     = 0x02
FLAG_CHANNEL_LO = 0x04   # бит 2: младший бит channel
FLAG_CHANNEL_HI = 0x08   # бит 3: старший бит channel
_CHANNEL_MASK   = 0x0C   # биты 2-3
_CHANNEL_SHIFT  = 2


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
    packet_id: int        = 0          # uint16, монотонный счётчик у источника
    channel:   Channel    = Channel.TOURIST
    ttl:       int        = 3          # uint8
    latitude:  int        = 0          # int32, × 1e6
    longitude: int        = 0          # int32, × 1e6
    payload:   bytes      = field(default_factory=lambda: bytes(PAYLOAD_SIZE))
    crc16:     int        = 0          # uint16, заполняется при encode
    # flags-биты — хранятся не сырыми, а через свойства, чтобы случайно
    # не записать в "reserved" и не сломать на декоде sanity-чек.
    want_ack:  bool       = False
    is_ack:    bool       = False


def _crc16_ccitt(data: bytes) -> int:
    """CRC-16/CCITT-FALSE: poly=0x1021, init=0xFFFF, big-endian."""
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


def _build_flags(pkt: MeshPacket) -> int:
    f = 0
    if pkt.want_ack:
        f |= FLAG_WANT_ACK
    if pkt.is_ack:
        f |= FLAG_IS_ACK
    f |= (int(pkt.channel) & 0x03) << _CHANNEL_SHIFT
    return f & 0xFF


def encode(pkt: MeshPacket) -> bytes:
    payload = pkt.payload[:PAYLOAD_SIZE].ljust(PAYLOAD_SIZE, b'\x00')
    # struct format:
    #   B  version
    #   B  type
    #   B  flags
    #   B  ttl
    #   H  device_id
    #   H  packet_id
    #   i  latitude
    #   i  longitude
    #   64s payload
    # = 16 + 64 = 80 байт; CRC даёт +2 = 82.
    body = struct.pack(
        '>BBBBHHii64s',
        pkt.version,
        int(pkt.type),
        _build_flags(pkt),
        pkt.ttl,
        pkt.device_id,
        pkt.packet_id,
        pkt.latitude,
        pkt.longitude,
        payload,
    )
    crc = _crc16_ccitt(body)
    return body + struct.pack('>H', crc)


def decode(raw: bytes) -> MeshPacket:
    """Распаковать 82 байта. ValueError при неверном размере, CRC или
    бессмысленных значениях полей.

    Sanity-проверки (version/type/channel/ttl) защищают от ложных пакетов:
    LoRa-CRC чипа + наш CRC-16 — это всё ещё ~1/65536 шанс случайного
    совпадения на шуме. Без валидации в БД появлялись «фантомные» устройства
    с device_id из мусорных байт (см. инцидент с device_id=12345)."""
    if len(raw) != PACKET_SIZE:
        raise ValueError(f"Неверный размер пакета: {len(raw)}, ожидается {PACKET_SIZE}")

    crc_recv = struct.unpack('>H', raw[80:82])[0]
    crc_calc = _crc16_ccitt(raw[:80])
    if crc_recv != crc_calc:
        raise ValueError(f"CRC ошибка: получен 0x{crc_recv:04X}, рассчитан 0x{crc_calc:04X}")

    version, ptype, flags, ttl, device_id, packet_id, lat, lon, payload = struct.unpack(
        '>BBBBHHii64s', raw[:80]
    )

    if version != PROTO_VERSION:
        # v1-пакеты (64 байта) сюда не дойдут из-за разного размера raw, но
        # если когда-то будет v3+ — пусть сразу падает с понятным текстом.
        raise ValueError(f"Неподдерживаемая версия протокола: {version}")
    if ptype not in PacketType._value2member_map_:
        raise ValueError(f"Неизвестный тип пакета: {ptype}")
    ch_val = (flags & _CHANNEL_MASK) >> _CHANNEL_SHIFT
    if ch_val not in Channel._value2member_map_:
        raise ValueError(f"Неизвестный канал: {ch_val}")
    # TTL стартует с 3 и уменьшается. Значения >8 явно мусор.
    if ttl == 0 or ttl > 8:
        raise ValueError(f"Подозрительный TTL: {ttl}")

    return MeshPacket(
        version   = version,
        type      = PacketType(ptype),
        device_id = device_id,
        packet_id = packet_id,
        channel   = Channel(ch_val),
        ttl       = ttl,
        latitude  = lat,
        longitude = lon,
        payload   = payload,
        crc16     = crc_recv,
        want_ack  = bool(flags & FLAG_WANT_ACK),
        is_ack    = bool(flags & FLAG_IS_ACK),
    )


# ============================================================
# Payload helpers по типам
# ============================================================

def make_ping_payload(battery_pct: int, rssi_last: int, seq: int) -> bytes:
    """battery_pct: 0–100, rssi_last: int8 (0=нет данных), seq: uint16."""
    data = struct.pack('>Bbh', battery_pct, rssi_last, seq)
    return data.ljust(PAYLOAD_SIZE, b'\x00')


def parse_ping_payload(payload: bytes) -> tuple[int, int, int]:
    """Вернуть (battery_pct, rssi_last, seq) из payload PING."""
    battery, rssi, seq = struct.unpack('>Bbh', payload[:4])
    return battery, rssi, seq


def make_chat_payload(text: str) -> bytes:
    encoded = text.encode('utf-8')[:PAYLOAD_SIZE]
    return encoded.ljust(PAYLOAD_SIZE, b'\x00')


def parse_chat_payload(payload: bytes) -> str:
    return payload.rstrip(b'\x00').decode('utf-8', errors='replace')


def make_sos_payload(sos_type: SosType, message: str) -> bytes:
    msg = message.encode('utf-8')[: PAYLOAD_SIZE - 1]
    return bytes([int(sos_type)]) + msg.ljust(PAYLOAD_SIZE - 1, b'\x00')


def parse_sos_payload(payload: bytes) -> tuple[SosType, str]:
    stype = SosType(payload[0]) if payload[0] in SosType._value2member_map_ else SosType.UNKNOWN
    msg = payload[1:].rstrip(b'\x00').decode('utf-8', errors='replace')
    return stype, msg


def make_ack_payload(ack_for_device_id: int, ack_for_packet_id: int) -> bytes:
    """ACK-payload: кому ACK предназначен + какой packet_id подтверждаем.

    Source (отправитель ACK) указывается в основном поле device_id —
    его не нужно дублировать в payload. Здесь только адресат и id."""
    data = struct.pack('>HH', ack_for_device_id & 0xFFFF, ack_for_packet_id & 0xFFFF)
    return data.ljust(PAYLOAD_SIZE, b'\x00')


def parse_ack_payload(payload: bytes) -> tuple[int, int]:
    """Вернуть (ack_for_device_id, ack_for_packet_id) из payload ACK."""
    return struct.unpack('>HH', payload[:4])


# ============================================================
# Coords
# ============================================================

def lat_lon_to_int(degrees: float) -> int:
    return int(round(degrees * 1_000_000))


def int_to_lat_lon(value: int) -> float:
    return value / 1_000_000


# ============================================================
# Self-test (запуск: python -m lora_station.packet)
# ============================================================

if __name__ == '__main__':
    # PING — обычный поток
    pkt = MeshPacket(
        type=PacketType.PING, device_id=42, packet_id=100, channel=Channel.TOURIST,
        ttl=3, latitude=lat_lon_to_int(43.45), longitude=lat_lon_to_int(41.20),
        payload=make_ping_payload(85, -90, 1),
    )
    raw = encode(pkt)
    assert len(raw) == PACKET_SIZE, f"размер {len(raw)} != {PACKET_SIZE}"
    pkt2 = decode(raw)
    assert pkt2.device_id == 42 and pkt2.latitude == lat_lon_to_int(43.45)
    assert pkt2.packet_id == 100
    assert pkt2.want_ack is False and pkt2.is_ack is False
    print(f"PING OK: {len(raw)} байт, CRC=0x{pkt2.crc16:04X}")

    # CHAT с want_ack — то ради чего весь рефакторинг
    long_text = "Это сообщение длиннее 48 байт UTF-8 — тестируем расширенный payload v2"
    pkt = MeshPacket(
        type=PacketType.CHAT, device_id=16, packet_id=42, want_ack=True,
        channel=Channel.TOURIST, ttl=3,
        payload=make_chat_payload(long_text),
    )
    raw = encode(pkt)
    pkt2 = decode(raw)
    assert pkt2.want_ack is True, "want_ack должен быть True"
    assert pkt2.is_ack is False
    txt = parse_chat_payload(pkt2.payload)
    # UTF-8 обрезка может попасть на середину символа, parse_chat_payload
    # использует errors='replace' — текст будет начинаться с того же что задавали
    assert txt.startswith("Это сообщение"), f"CHAT текст: {txt!r}"
    print(f"CHAT(want_ack=1) OK: {len(raw)} байт, текст {len(txt)} симв., packet_id={pkt2.packet_id}")

    # ACK от базы к ESP32
    ack = MeshPacket(
        type=PacketType.ACK, device_id=1, packet_id=500, is_ack=True,
        channel=Channel.RESCUE, ttl=3,
        payload=make_ack_payload(ack_for_device_id=16, ack_for_packet_id=42),
    )
    raw_ack = encode(ack)
    ack2 = decode(raw_ack)
    assert ack2.is_ack is True and ack2.want_ack is False
    ack_for_dev, ack_for_pid = parse_ack_payload(ack2.payload)
    assert ack_for_dev == 16 and ack_for_pid == 42
    print(f"ACK OK: {len(raw_ack)} байт, ack_for=(dev={ack_for_dev}, pkt={ack_for_pid})")

    # Битый CRC — sanity-чек должен сработать
    bad = bytearray(raw)
    bad[81] ^= 0xFF
    try:
        decode(bytes(bad))
        assert False, "decode должен был кинуть на битом CRC"
    except ValueError as e:
        assert "CRC" in str(e)
        print(f"Битый CRC корректно отвергнут: {e}")

    print("Все тесты прошли.")
