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
    device_name: str
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
        # device_name берём из JOIN с devices (см. db.list_sos / db.get_sos).
        # Если row без него (старый запрос без JOIN) — fallback в "".
        try:
            name = row["device_name"] or ""
        except (KeyError, IndexError):
            name = ""
        return cls(
            id=row["id"],
            device_id=row["device_id"],
            device_name=name,
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

class TopDevice(BaseModel):
    device_id: int
    name: str
    pings: int


class Stats(BaseModel):
    pings_total: int
    sos_total: int
    sos_open: int
    sos_acked: int      # acked=1 AND resolved=0
    sos_resolved: int   # resolved=1
    devices_total: int
    devices_online: int
    pings_24h: int      # PING-ов за последние 24 часа
    sos_24h: int        # SOS-ов за последние 24 часа
    # Разбивка SOS по типам: {"падение": 2, "медицина": 1, ...}.
    # Ключ — sos_type_label (стрингой), для AI это удобнее: видно
    # сразу название типа, без таблицы id→label.
    sos_by_type: dict[str, int]
    # Топ-3 устройств по числу всех PING'ов в системе (всё время).
    top_devices_by_pings: list[TopDevice]

    @classmethod
    def from_dict(cls, d: dict) -> "Stats":
        # db.get_stats() отдаёт sos_by_type с числовыми ключами (sos_type),
        # для AI читаемее sos_by_type_label. Переводим здесь, чтобы тип
        # SOS_TYPE_LABELS оставался единственным источником истины (см. выше).
        by_type_int = d.get("sos_by_type", {}) or {}
        by_type_labeled: dict[str, int] = {}
        for tid, cnt in by_type_int.items():
            by_type_labeled[SOS_TYPE_LABELS.get(int(tid), f"тип {tid}")] = int(cnt)
        return cls(
            pings_total=d["pings_total"],
            sos_total=d["sos_total"],
            sos_open=d["sos_open"],
            sos_acked=d.get("sos_acked", 0),
            sos_resolved=d.get("sos_resolved", 0),
            devices_total=d["devices_total"],
            devices_online=d["devices_online"],
            pings_24h=d.get("pings_24h", 0),
            sos_24h=d.get("sos_24h", 0),
            sos_by_type=by_type_labeled,
            top_devices_by_pings=[
                TopDevice(**t) for t in d.get("top_devices_by_pings", [])
            ],
        )


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
