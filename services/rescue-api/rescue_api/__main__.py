"""python -m rescue_api — запуск сервиса через uvicorn.

Параметры через ENV (см. services/rescue-api/CLAUDE.md):
  DB_PATH           путь к SQLite (по умолчанию /var/lib/mesh-net/mesh.db)
  RESCUE_API_HOST   интерфейс (0.0.0.0 — все, 127.0.0.1 — только localhost)
  RESCUE_API_PORT   порт (8000)
  LOG_LEVEL         INFO / DEBUG / WARNING
"""

import logging
import os
import sys

import uvicorn


def main() -> int:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    host = os.environ.get("RESCUE_API_HOST", "0.0.0.0")
    port = int(os.environ.get("RESCUE_API_PORT", "8000"))

    log = logging.getLogger("rescue_api")
    log.info("=== rescue-api запуск, http://%s:%d ===", host, port)

    uvicorn.run(
        "rescue_api.app:app",
        host=host,
        port=port,
        log_config=None,    # logging уже настроили выше — не даём uvicorn перетереть
        access_log=False,   # не пушим в journal по строке на каждый /api/health
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
