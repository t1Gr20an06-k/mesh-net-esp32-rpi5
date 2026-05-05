"""FastAPI-приложение rescue-api.

REST под /api/, плюс WebSocket /ws.
Все REST-эндпоинты — sync `def`, FastAPI запускает их в threadpool, и
sqlite3-вызовы там event loop не блокируют. WebSocket — async, как и
положено в Starlette.

Авторизации нет: подразумевается что сервис локальный, на самом RPi5
базы спасателей. Когда понадобится открыть наружу — поставим перед
ним nginx + basic auth.
"""

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import db, models
from .ws import Broadcaster

log = logging.getLogger("rescue_api")

DB_PATH    = os.environ.get("DB_PATH", db.DEFAULT_DB_PATH)
ALLOW_CORS = os.environ.get("ALLOW_CORS", "1") not in ("0", "false", "")

# Путь до статики дашборда: services/rescue-api/rescue_api/app.py → подняться 3 раза → web/rescue-dashboard
DASHBOARD_DIR = Path(
    os.environ.get("DASHBOARD_DIR")
    or (Path(__file__).resolve().parents[3] / "web" / "rescue-dashboard")
)

# Каталог оффлайн-тайлов карты. Заполняется через scripts/import_tiles/download_tiles.py.
# Если пуст или не существует — карта в дашборде будет серой, но маркеры рисуются.
TILES_DIR = Path(os.environ.get("TILES_DIR", "/var/lib/mesh-net/tiles"))

# Один Broadcaster на всё приложение
_broadcaster = Broadcaster(DB_PATH)


@asynccontextmanager
async def lifespan(_: FastAPI):
    # startup
    log.info("rescue-api старт, DB=%s", DB_PATH)
    _broadcaster.start()
    yield
    # shutdown
    log.info("rescue-api shutdown")
    await _broadcaster.stop()


app = FastAPI(
    title="Mesh-net Тропы — rescue-api",
    description="REST + WebSocket для дашборда базы спасателей",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS открыт по умолчанию — удобно при разработке (дашборд на 5173,
# API на 8000). Перед боевой выкаткой сузить до своих доменов.
if ALLOW_CORS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )


# ============================================================
# Утилитарные
# ============================================================

@app.get("/api/health")
def health():
    """Жив ли сервис. Вызывается часто из дашборда — не логируется (см. access_log=False)."""
    return {"status": "ok"}


@app.get("/api/stats", response_model=models.Stats)
def stats():
    with db.db_read(DB_PATH) as conn:
        return models.Stats(**db.get_stats(conn))


# ============================================================
# Туристы и устройства
# ============================================================

@app.get("/api/tourists", response_model=list[models.Tourist])
def tourists():
    """Кто сейчас 'в эфире' — был PING за последние 10 минут."""
    with db.db_read(DB_PATH) as conn:
        return [models.Tourist.from_row(r) for r in db.list_active_tourists(conn)]


@app.get("/api/devices", response_model=list[models.Device])
def devices():
    """Весь реестр устройств, включая давно ушедших с эфира."""
    with db.db_read(DB_PATH) as conn:
        return [models.Device.from_row(r) for r in db.list_devices(conn)]


# ============================================================
# Pings (треки)
# ============================================================

@app.get("/api/pings", response_model=list[models.Ping])
def pings(
    device_id: int | None = Query(None, description="Фильтр по device_id"),
    hours: float          = Query(1.0,   gt=0, le=720, description="Глубина выборки в часах"),
    limit: int            = Query(500,   gt=0, le=10000),
):
    """Список PING-ов для рисования трека.
    Без device_id — все устройства."""
    with db.db_read(DB_PATH) as conn:
        return [models.Ping.from_row(r) for r in db.list_pings(conn, device_id, hours, limit)]


# ============================================================
# SOS
# ============================================================

@app.get("/api/sos", response_model=list[models.Sos])
def sos(only_open: bool = Query(True, description="Только незакрытые инциденты")):
    with db.db_read(DB_PATH) as conn:
        return [models.Sos.from_row(r) for r in db.list_sos(conn, only_open)]


@app.get("/api/sos/{sos_id}", response_model=models.Sos)
def sos_one(sos_id: int):
    with db.db_read(DB_PATH) as conn:
        row = db.get_sos(conn, sos_id)
        if not row:
            raise HTTPException(404, "SOS не найден")
        return models.Sos.from_row(row)


@app.post("/api/sos/{sos_id}/ack", response_model=models.Sos)
def sos_ack(sos_id: int, body: models.AckRequest):
    """Спасатель подтвердил, что увидел SOS. Повторный ack игнорируется
    (поле acked_at сохраняется от первого вызова — это важно юридически)."""
    with db.db_write(DB_PATH) as conn:
        row = db.ack_sos(conn, sos_id, body.acked_by)
        if not row:
            raise HTTPException(404, "SOS не найден")
        return models.Sos.from_row(row)


@app.post("/api/sos/{sos_id}/resolve", response_model=models.Sos)
def sos_resolve(sos_id: int, body: models.ResolveRequest):
    """Инцидент закрыт. Можно прямо после ack или вообще без ack
    (бывает: пострадавшего нашли, формальности позже)."""
    with db.db_write(DB_PATH) as conn:
        row = db.resolve_sos(conn, sos_id, body.notes)
        if not row:
            raise HTTPException(404, "SOS не найден")
        return models.Sos.from_row(row)


# ============================================================
# WebSocket — push новых событий дашборду
# ============================================================

@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    """Push-only канал. Сервер шлёт {event, data}; клиенту ничего слать
    не обязательно. Но если клиент закроет соединение — мы об этом
    узнаём через WebSocketDisconnect в receive_text()."""
    await _broadcaster.connect(websocket)
    try:
        while True:
            # receive_text() блокируется до отключения клиента или приёма
            # данных. Пинги-понги клиент шлёт по своему усмотрению, серверу
            # всё равно — главное, что disconnect ловится сразу.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await _broadcaster.disconnect(websocket)


# ============================================================
# Оффлайн-тайлы карты — /tiles/{z}/{x}/{y}.png
# ============================================================
# Монтируем ДО mount("/", ...), иначе корневой StaticFiles перехватит /tiles
# и вернёт 404 для отсутствующего файла внутри dashboard-каталога.
if TILES_DIR.exists():
    app.mount("/tiles", StaticFiles(directory=TILES_DIR), name="tiles")
    log.info("tiles mounted at /tiles  (dir=%s)", TILES_DIR)
else:
    log.info("tiles dir not found: %s — оффлайн-карта пустая (см. scripts/import_tiles/)",
             TILES_DIR)


# ============================================================
# Статика дашборда — монтируется ПОСЛЕДНЕЙ
# ============================================================
# Важно: app.mount("/", StaticFiles) перехватит любой путь, не пойманный
# выше. Поэтому /api/*, /ws, /tiles, /docs, /openapi.json должны быть
# зарегистрированы РАНЬШЕ — что у нас и так выполнено.
#
# html=True означает: GET / отдаёт index.html, GET /foo без файла → 404.
if DASHBOARD_DIR.exists():
    app.mount("/", StaticFiles(directory=DASHBOARD_DIR, html=True), name="dashboard")
    log.info("dashboard mounted at /  (dir=%s)", DASHBOARD_DIR)
else:
    log.warning("dashboard dir not found: %s — статика не примонтирована", DASHBOARD_DIR)
