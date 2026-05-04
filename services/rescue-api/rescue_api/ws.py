"""WebSocket-broadcaster.

Один Broadcaster на всё приложение. Держит набор подключённых клиентов
и фоновую asyncio-задачу, которая раз в секунду опрашивает SQLite
на новые pings/sos и пушит их всем.

Polling, а не «лора-станция шлёт нам» — потому что:
1. lora-station не должна знать про наличие rescue-api. Это упрощает
   отладку и позволяет рестартить любой из двух сервисов независимо.
2. Для 1 PING / 10 сек polling раз в секунду — копейки на CPU.
3. SQLite в WAL-режиме разрешает читать параллельно с записью без
   блокировок.
"""

import asyncio
import json
import logging
from typing import Any, Set

from fastapi import WebSocket

from . import db
from .models import Ping, Sos

log = logging.getLogger("rescue_api.ws")

POLL_INTERVAL_S = 1.0
# Сколько новых записей за один тик максимум вытащим. На норме <10,
# но на старте после долгого простоя rescue-api база может содержать
# тысячи PING-ов — мы их не пушим (стартовая точка = MAX(id)), но
# на всякий случай ограничим, чтобы один тик не залип.
BATCH_LIMIT = 200


class Broadcaster:
    """Singleton-style: один экземпляр держит все WS-подключения и
    единственного поллера БД."""

    def __init__(self, db_path: str):
        self._clients: Set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self._db_path = db_path
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._last_ping_id: int = 0
        self._last_sos_id: int = 0

    # --- управление подключениями --------------------------------

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)
        log.info("WS клиент подключился (всего: %d)", len(self._clients))

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)
        log.info("WS клиент отключился (всего: %d)", len(self._clients))

    async def _broadcast(self, event: str, payload: dict[str, Any]) -> None:
        msg = json.dumps({"event": event, "data": payload}, default=str, ensure_ascii=False)
        async with self._lock:
            dead = []
            for ws in self._clients:
                try:
                    await ws.send_text(msg)
                except Exception as exc:
                    log.debug("send fail (%s) — пометил клиент мёртвым", exc)
                    dead.append(ws)
            for ws in dead:
                self._clients.discard(ws)
            if dead:
                log.info("WS: убрал %d мёртвых клиентов (осталось %d)",
                         len(dead), len(self._clients))

    # --- фоновый поллер БД ---------------------------------------

    async def _poll_loop(self) -> None:
        # Стартовая точка: всё, что было ДО старта rescue-api, в WS не пушим
        # (иначе свежеподключённый дашборд получит тысячи старых PING-ов).
        try:
            with db.db_read(self._db_path) as conn:
                self._last_ping_id, self._last_sos_id = db.get_max_ids(conn)
            log.info("WS poller старт: last_ping_id=%d last_sos_id=%d",
                     self._last_ping_id, self._last_sos_id)
        except Exception as exc:  # noqa: BLE001
            log.error("WS poller init FAIL: %s", exc)
            return

        while not self._stop.is_set():
            try:
                with db.db_read(self._db_path) as conn:
                    new_pings = db.get_new_pings(conn, self._last_ping_id, BATCH_LIMIT)
                    new_sos   = db.get_new_sos(conn,   self._last_sos_id,   BATCH_LIMIT)
            except Exception as exc:  # noqa: BLE001
                log.warning("WS poll FAIL: %s", exc)
                # Спим интервал и пробуем снова. БД могла моргнуть на reopen WAL.
                await self._sleep_or_stop(POLL_INTERVAL_S)
                continue

            for r in new_pings:
                await self._broadcast("ping", Ping.from_row(r).model_dump())
                if r["id"] > self._last_ping_id:
                    self._last_ping_id = r["id"]

            for r in new_sos:
                await self._broadcast("sos", Sos.from_row(r).model_dump())
                if r["id"] > self._last_sos_id:
                    self._last_sos_id = r["id"]

            await self._sleep_or_stop(POLL_INTERVAL_S)

    async def _sleep_or_stop(self, seconds: float) -> None:
        """Sleep, но прерывается мгновенно если выставлен self._stop."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    # --- lifecycle -----------------------------------------------

    def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=3.0)
            except asyncio.TimeoutError:
                self._task.cancel()
            self._task = None
