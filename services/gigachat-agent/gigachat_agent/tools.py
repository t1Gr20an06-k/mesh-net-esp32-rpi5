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
        name="get_sos_events",
        description=(
            "Возвращает SOS-инциденты. По умолчанию только открытые "
            "(не resolved). Поля: id, device_id, sos_type_label, position, "
            "received_at, acked, resolved, message. Зови на вопросы про SOS / "
            "тревоги / ЧС / 'всё спокойно?', а также как часть общего отчёта "
            "об обстановке ('как обстановка', 'что происходит', 'статус')."
        ),
        parameters=FunctionParameters(
            type="object",
            properties={
                "only_open": {
                    "type": "boolean",
                    "description": "Только незакрытые SOS (default: true). "
                    "Поставь false если оператор спрашивает 'все SOS за сегодня' и т.п."
                },
            },
            required=[],
        ),
    ),
    Function(
        name="get_device_track",
        description=(
            "Возвращает последние PING-точки одного устройства (его трек). "
            "Используй когда оператор спрашивает 'где был N последний час', "
            "'покажи трек устройства X'."
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
        name="get_stats",
        description=(
            "Общие счётчики системы: всего устройств, сколько в эфире, "
            "всего PING'ов, всего SOS. Используй для вопросов вида "
            "'что у нас в системе', 'сколько туристов всего'."
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

    def get_sos_events(self, only_open: bool = True) -> list[dict]:
        return self._get("/api/sos", only_open=str(only_open).lower())

    def get_device_track(self, device_id: int, hours: float = 1.0) -> list[dict]:
        # rescue-api хочет limit, ставим адекватный — больше 200 точек GigaChat
        # всё равно не переварит осмысленно.
        return self._get("/api/pings", device_id=device_id, hours=hours, limit=200)

    def get_stats(self) -> dict:
        return self._get("/api/stats")


def dispatch(api: RescueApi, name: str, arguments: dict) -> Any:
    """Один диспетчер на 4 инструмента. Возвращает то, что отдала функция,
    или dict {"error": "..."} — это попадёт в ответ модели как контекст."""
    try:
        if name == "get_active_tourists":
            return api.get_active_tourists()
        if name == "get_sos_events":
            return api.get_sos_events(only_open=bool(arguments.get("only_open", True)))
        if name == "get_device_track":
            return api.get_device_track(
                device_id=int(arguments["device_id"]),
                hours=float(arguments.get("hours", 1.0)),
            )
        if name == "get_stats":
            return api.get_stats()
    except httpx.HTTPError as e:
        log.warning("tool %s: rescue-api недоступен: %s", name, e)
        return {"error": f"rescue-api недоступен: {e}"}
    except (KeyError, ValueError, TypeError) as e:
        log.warning("tool %s: плохие аргументы %s: %s", name, arguments, e)
        return {"error": f"плохие аргументы: {e}"}

    log.warning("tool %s: неизвестный инструмент", name)
    return {"error": f"неизвестный инструмент: {name}"}
