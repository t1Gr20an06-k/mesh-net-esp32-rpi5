"""Pydantic-модели ответов rescue-api.

Конвертация координат int × 1e6 → float-градусы делается ровно тут
(в `_coord` и `from_row`). В эндпоинтах и в WebSocket-broadcaster
больше нигде с raw-int не работаем — иначе легко забыть деление и
получить «турист в Атлантике».
"""

from typing import Optional

from pydantic import BaseModel


def _coord(e6: Optional[int]) -> Optional[float]:
    if e6 is None:
        return None
    return e6 / 1_000_000.0


# ============================================================
# Базовые типы
# ============================================================

class Position(BaseModel):
    lat: float
    lon: float


SOS_TYPE_LABELS = {
    0: "неизвестно",
    1: "падение",
    2: "медицина",
    3: "заблудился",
    4: "погода",
}

CHANNEL_LABELS = {0: "TOURIST", 1: "RESCUE"}


# ============================================================
# Tourist — устройство «прямо сейчас» (последний PING)
# ============================================================

class Tourist(BaseModel):
    device_id: int
    name: str
    channel: int
    channel_label: str
    last_seen_at: str
    last_ping_at: Optional[str]
    position: Optional[Position]
    battery_pct: Optional[int]
    rssi: Optional[int]   # receiver_rssi: дБм на стороне базы

    @classmethod
    def from_row(cls, row) -> "Tourist":
        lat = _coord(row["lat_e6"])
        lon = _coord(row["lon_e6"])
        # (0, 0) — заглушка от ESP32 без GPS-фикса. Отдаём как Position(0,0),
        # дашборд сам решает не рисовать такого на карте.
        pos = Position(lat=lat, lon=lon) if lat is not None and lon is not None else None
        ch = row["channel"]
        return cls(
            device_id=row["device_id"],
            name=row["name"] or "",
            channel=ch,
            channel_label=CHANNEL_LABELS.get(ch, "?"),
            last_seen_at=row["last_seen_at"],
            last_ping_at=row["last_ping_at"],
            position=pos,
            battery_pct=row["battery_pct"],
            rssi=row["receiver_rssi"],
        )


# ============================================================
# Device — запись из реестра devices (без последнего PING)
# ============================================================

class Device(BaseModel):
    device_id: int
    name: str
    channel: int
    channel_label: str
    first_seen_at: str
    last_seen_at: str
    position: Optional[Position]
    battery_pct: Optional[int]
    is_active: bool

    @classmethod
    def from_row(cls, row) -> "Device":
        lat = _coord(row["last_latitude"])
        lon = _coord(row["last_longitude"])
        pos = Position(lat=lat, lon=lon) if lat is not None and lon is not None else None
        ch = row["channel"]
        return cls(
            device_id=row["device_id"],
            name=row["name"] or "",
            channel=ch,
            channel_label=CHANNEL_LABELS.get(ch, "?"),
            first_seen_at=row["first_seen_at"],
            last_seen_at=row["last_seen_at"],
            position=pos,
            battery_pct=row["battery_pct"],
            is_active=bool(row["is_active"]),
        )


# ============================================================
# Ping — одна строка из таблицы pings
# ============================================================

class Ping(BaseModel):
    id: int
    device_id: int
    received_at: str
    position: Position
    battery_pct: Optional[int]
    rssi: Optional[int]      # receiver_rssi
    rssi_last: Optional[int] # rssi_last (что слышал сам терминал)
    seq: Optional[int]

    @classmethod
    def from_row(cls, row) -> "Ping":
        return cls(
            id=row["id"],
            device_id=row["device_id"],
            received_at=row["received_at"],
            position=Position(lat=_coord(row["latitude"]),
                              lon=_coord(row["longitude"])),
            battery_pct=row["battery_pct"],
            rssi=row["receiver_rssi"],
            rssi_last=row["rssi_last"],
            seq=row["seq"],
        )


# ============================================================
# Sos — одна строка из таблицы sos_events
# ============================================================

class Sos(BaseModel):
    id: int
    device_id: int
    received_at: str
    position: Position
    sos_type: int
    sos_type_label: str
    message: str
    acked: bool
    acked_at: Optional[str]
    acked_by: Optional[int]
    resolved: bool
    resolved_at: Optional[str]
    notes: str

    @classmethod
    def from_row(cls, row) -> "Sos":
        t = row["sos_type"]
        return cls(
            id=row["id"],
            device_id=row["device_id"],
            received_at=row["received_at"],
            position=Position(lat=_coord(row["latitude"]),
                              lon=_coord(row["longitude"])),
            sos_type=t,
            sos_type_label=SOS_TYPE_LABELS.get(t, "?"),
            message=row["message"] or "",
            acked=bool(row["acked"]),
            acked_at=row["acked_at"],
            acked_by=row["acked_by"],
            resolved=bool(row["resolved"]),
            resolved_at=row["resolved_at"],
            notes=row["notes"] or "",
        )


# ============================================================
# ChatMessage — одна строка из таблицы chat_messages
# ============================================================
# device_name достаётся JOIN-ом из devices; пустое имя — оставляем "",
# дашборд сам подставит "Device <id>".

class ChatMessage(BaseModel):
    id: int
    device_id: int
    device_name: str
    received_at: str
    position: Optional[Position]
    channel: int
    channel_label: str
    message: str

    @classmethod
    def from_row(cls, row) -> "ChatMessage":
        lat = _coord(row["latitude"])
        lon = _coord(row["longitude"])
        # (0, 0) — нет GPS-фикса. Не отдаём как Position, чтобы фронт не пытался
        # рисовать сообщение на карте у Гринвича.
        pos = (Position(lat=lat, lon=lon)
               if lat is not None and lon is not None and (lat != 0 or lon != 0)
               else None)
        ch = row["channel"]
        # device_name появляется только из JOIN (см. db.list_chat). Если row
        # без него — fallback в пустую строку.
        try:
            name = row["device_name"] or ""
        except (KeyError, IndexError):
            name = ""
        return cls(
            id=row["id"],
            device_id=row["device_id"],
            device_name=name,
            received_at=row["received_at"],
            position=pos,
            channel=ch,
            channel_label=CHANNEL_LABELS.get(ch, "?"),
            message=row["message"] or "",
        )


# ============================================================
# Stats / запросы
# ============================================================

class Stats(BaseModel):
    pings_total: int
    sos_total: int
    sos_open: int
    devices_total: int
    devices_online: int


class AckRequest(BaseModel):
    acked_by: Optional[int] = None  # device_id спасателя; пока опционально


class ResolveRequest(BaseModel):
    notes: str = ""


class ChatSendRequest(BaseModel):
    """POST /api/messages — ответ оператора туристу.

    Длина сообщения валидируется на уровне эндпоинта:
    максимум 48 байт UTF-8 (это размер CHAT-payload в LoRa-пакете)."""
    text: str


class PurgeRequest(BaseModel):
    """Тело POST /api/admin/purge.

    confirm — должен быть строго 'ОЧИСТИТЬ' (UI вводит вручную). Защита
    от случайного curl и от XSS-формы где-нибудь.

    tables — какие таблицы чистить, подмножество
    ('pings','sos_events','chat_messages','devices'). Пустой список —
    400. Если выбран devices, дети подтянутся каскадом (см. db.purge_tables).
    """
    confirm: str
    tables: list[str]
