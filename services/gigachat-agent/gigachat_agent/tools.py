"""Инструменты для function calling: ходим в rescue-api по HTTP.

Каждый инструмент — это:
1. JSON Schema для GigaChat (см. `TOOL_DEFS`),
2. Python-функция, которую вызывает agent.py при finish_reason="function_call".

Дублировать прямой sqlite3-доступ не хотим — rescue-api уже агрегирует
"последний PING на устройство", считает active_threshold и т.п. Один
источник истины.

httpx используем sync (Client, не AsyncClient): FastAPI запускает sync
эндпоинты в threadpool, проблем с event loop нет, а код проще.
"""

import logging
from typing import Any

import httpx
from gigachat.models import Function, FunctionParameters

log = logging.getLogger("gigachat_agent.tools")


# ============================================================
# Описания инструментов для GigaChat (function calling schemas)
# ============================================================

TOOL_DEFS: list[Function] = [
    Function(
        name="get_active_tourists",
        description=(
            "Возвращает список туристов, которые СЕЙЧАС в эфире — слали PING "
            "за последние пару минут. Поля: device_id, name, position {lat,lon}, "
            "battery_pct, rssi, last_ping_at. Зови этот инструмент на запросы: "
            "'кто сейчас в эфире', 'кто на маршруте', 'кого мы видим', "
            "'сколько туристов в эфире', 'есть кто-нибудь', а также как часть "
            "общего отчёта об обстановке ('как обстановка', 'что происходит', "
            "'статус', 'что у нас')."
        ),
        parameters=FunctionParameters(
            type="object",
            properties={},
            required=[],
        ),
    ),
    Function(
        name="get_all_devices",
        description=(
            "Полный реестр зарегистрированных устройств — В ТОМ ЧИСЛЕ ОФФЛАЙН. "
            "В отличие от get_active_tourists возвращает и тех, кто уже не в эфире "
            "(но был раньше). Поля на каждое устройство: device_id, name, "
            "channel_label (TOURIST/RESCUE), first_seen_at, last_seen_at, "
            "is_active, position, battery_pct. Зови на: 'все устройства', "
            "'какие id у нас зарегистрированы', 'кто бывал на связи', "
            "'покажи весь список устройств'."
        ),
        parameters=FunctionParameters(
            type="object",
            properties={},
            required=[],
        ),
    ),
    Function(
        name="get_sos_events",
        description=(
            "Возвращает SOS-инциденты с фильтрами. Поля каждого: id, device_id, "
            "device_name, sos_type, sos_type_label, position, received_at, acked, "
            "acked_at, acked_by, resolved, resolved_at, message, notes. "
            "Зови на: 'какие SOS', 'тревоги', 'ЧС', 'все SOS за час', "
            "'медицинские SOS', 'SOS у Васи', 'всё спокойно?', а также как часть "
            "общего отчёта ('как обстановка', 'что происходит')."
        ),
        parameters=FunctionParameters(
            type="object",
            properties={
                "only_open": {
                    "type": "boolean",
                    "description": "Только незакрытые (resolved=0). Default true. "
                    "Поставь false для 'все SOS', 'история SOS', 'что было за день'.",
                },
                "device_id": {
                    "type": "integer",
                    "description": "Фильтр по конкретному устройству. "
                    "Используй когда оператор называет id или имя ('SOS у 16').",
                },
                "hours": {
                    "type": "number",
                    "description": "Только за последние N часов (1..720). "
                    "Используй для 'за последний час', 'за сегодня (24)'.",
                },
                "sos_type": {
                    "type": "integer",
                    "description": "Фильтр по типу: 0=неизвестно, 1=падение, "
                    "2=медицина, 3=заблудился, 4=погода. "
                    "Только если оператор явно назвал тип.",
                },
            },
            required=[],
        ),
    ),
    Function(
        name="get_sos_details",
        description=(
            "Полная информация об одном SOS по его id: координаты, тип, "
            "время получения, кто и когда подтвердил (acked_by/at), когда "
            "закрыли (resolved_at), notes оператора. Зови на 'расскажи "
            "подробнее про SOS 3', 'детали SOS #5', 'кто принял SOS 7'."
        ),
        parameters=FunctionParameters(
            type="object",
            properties={
                "sos_id": {
                    "type": "integer",
                    "description": "Числовой id SOS-события (из поля 'id' в "
                    "get_sos_events). НЕ device_id, а именно id записи SOS.",
                },
            },
            required=["sos_id"],
        ),
    ),
    Function(
        name="get_device_track",
        description=(
            "Возвращает последние PING-точки одного устройства (его трек). "
            "Используй когда оператор спрашивает 'где был N последний час', "
            "'покажи трек устройства X', 'куда движется 16'."
        ),
        parameters=FunctionParameters(
            type="object",
            properties={
                "device_id": {
                    "type": "integer",
                    "description": "Числовой ID устройства (десятичное)."
                },
                "hours": {
                    "type": "number",
                    "description": "Глубина выборки в часах (default: 1.0, max: 720).",
                },
            },
            required=["device_id"],
        ),
    ),
    Function(
        name="get_chat_history",
        description=(
            "История CHAT-сообщений турист↔база. Поля каждого: id, device_id, "
            "device_name, received_at, message. Без device_id — общая лента "
            "(все туристы + ответы базы). С device_id — диалог одного туриста "
            "и базы. Зови на: 'что писал 16', 'о чём общались с Васей', "
            "'последние сообщения', 'покажи переписку'. ВАЖНО: device_id=1 — "
            "это база спасателей, её сообщения = ответы оператора."
        ),
        parameters=FunctionParameters(
            type="object",
            properties={
                "device_id": {
                    "type": "integer",
                    "description": "Только диалог этого устройства (включая ответы базы). "
                    "Если не указан — общая лента.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Сколько последних сообщений вернуть (1..500, default 50).",
                },
            },
            required=[],
        ),
    ),
    Function(
        name="find_device",
        description=(
            "Поиск устройства по части имени или числовому id. Возвращает "
            "массив устройств (обычно 0..3 совпадений). Зови когда оператор "
            "называет имя или часть имени, а тебе нужен device_id для "
            "следующего вызова get_device_track / get_sos_events. "
            "Примеры запросов: 'где Вася', 'найди Иванова', 'кто такой 16', "
            "'покажи трек по имени Сергей'."
        ),
        parameters=FunctionParameters(
            type="object",
            properties={
                "query": {
                    "type": "string",
                    "description": "Часть имени или числовой id. Регистр не важен.",
                },
            },
            required=["query"],
        ),
    ),
    Function(
        name="get_stats",
        description=(
            "Полная статистика системы. Поля: pings_total, pings_24h, sos_total, "
            "sos_24h, sos_open, sos_acked, sos_resolved, devices_total, "
            "devices_online, sos_by_type (разбивка {label: count}), "
            "top_devices_by_pings (топ-3 [{device_id, name, pings}]). "
            "Зови на: 'что у нас в системе', 'сколько всего', 'счётчики', "
            "'статистика', 'разбивка по типам SOS', 'кто самый активный'."
        ),
        parameters=FunctionParameters(
            type="object",
            properties={},
            required=[],
        ),
    ),
]


# ============================================================
# Реализация — вызовы rescue-api
# ============================================================

class RescueApi:
    """Тонкая обёртка над httpx.Client для походов в rescue-api.

    Один экземпляр на весь процесс, держим открытое соединение.
    Все методы синхронные — запускаются в threadpool через FastAPI.
    """

    def __init__(self, base_url: str, timeout: float = 5.0):
        self._client = httpx.Client(base_url=base_url, timeout=timeout)
        self._base = base_url

    def close(self):
        self._client.close()

    def _get(self, path: str, **params) -> Any:
        # Чистим None-параметры — httpx их шлёт как пустые, что иногда мешает.
        params = {k: v for k, v in params.items() if v is not None}
        r = self._client.get(path, params=params)
        r.raise_for_status()
        return r.json()

    # --- инструменты ---

    def get_active_tourists(self) -> list[dict]:
        return self._get("/api/tourists")

    def get_all_devices(self) -> list[dict]:
        return self._get("/api/devices")

    def get_sos_events(
        self,
        only_open: bool = True,
        device_id: int | None = None,
        hours: float | None = None,
        sos_type: int | None = None,
    ) -> list[dict]:
        return self._get(
            "/api/sos",
            only_open=str(only_open).lower(),
            device_id=device_id,
            hours=hours,
            sos_type=sos_type,
        )

    def get_sos_details(self, sos_id: int) -> dict:
        return self._get(f"/api/sos/{sos_id}")

    def get_device_track(self, device_id: int, hours: float = 1.0) -> list[dict]:
        # rescue-api хочет limit, ставим адекватный — больше 200 точек GigaChat
        # всё равно не переварит осмысленно.
        return self._get("/api/pings", device_id=device_id, hours=hours, limit=200)

    def get_chat_history(self, device_id: int | None = None, limit: int = 50) -> list[dict]:
        return self._get("/api/messages", limit=limit, device_id=device_id)

    def find_device(self, query: str) -> list[dict]:
        return self._get("/api/devices/find", q=query)

    def get_stats(self) -> dict:
        return self._get("/api/stats")


def dispatch(api: RescueApi, name: str, arguments: dict) -> Any:
    """Один диспетчер на инструменты. Возвращает то, что отдала функция,
    или dict {"error": "..."} — это попадёт в ответ модели как контекст."""
    try:
        if name == "get_active_tourists":
            return api.get_active_tourists()
        if name == "get_all_devices":
            return api.get_all_devices()
        if name == "get_sos_events":
            return api.get_sos_events(
                only_open=bool(arguments.get("only_open", True)),
                device_id=_int_or_none(arguments.get("device_id")),
                hours=_float_or_none(arguments.get("hours")),
                sos_type=_int_or_none(arguments.get("sos_type")),
            )
        if name == "get_sos_details":
            return api.get_sos_details(sos_id=int(arguments["sos_id"]))
        if name == "get_device_track":
            return api.get_device_track(
                device_id=int(arguments["device_id"]),
                hours=float(arguments.get("hours", 1.0)),
            )
        if name == "get_chat_history":
            return api.get_chat_history(
                device_id=_int_or_none(arguments.get("device_id")),
                limit=int(arguments.get("limit", 50)),
            )
        if name == "find_device":
            q = str(arguments.get("query", "")).strip()
            if not q:
                return {"error": "пустой query"}
            return api.find_device(query=q)
        if name == "get_stats":
            return api.get_stats()
    except httpx.HTTPStatusError as e:
        # 404 от /api/sos/{id} — нормальный сигнал «нет такого SOS», не падаем
        if e.response.status_code == 404:
            return {"error": "не найдено", "status": 404}
        log.warning("tool %s: rescue-api %s", name, e)
        return {"error": f"rescue-api ответил {e.response.status_code}: {e}"}
    except httpx.HTTPError as e:
        log.warning("tool %s: rescue-api недоступен: %s", name, e)
        return {"error": f"rescue-api недоступен: {e}"}
    except (KeyError, ValueError, TypeError) as e:
        log.warning("tool %s: плохие аргументы %s: %s", name, arguments, e)
        return {"error": f"плохие аргументы: {e}"}

    log.warning("tool %s: неизвестный инструмент", name)
    return {"error": f"неизвестный инструмент: {name}"}


def _int_or_none(v: Any) -> int | None:
    """Модель иногда отдаёт null/empty/строку — приводим к int или None,
    чтобы dispatch не падал на пустых аргументах."""
    if v is None or v == "" or v == "null":
        return None
    return int(v)


def _float_or_none(v: Any) -> float | None:
    if v is None or v == "" or v == "null":
        return None
    return float(v)
