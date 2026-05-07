"""Точка входа: `python -m gigachat_agent`.

Параметры — через ENV (GIGACHAT_AGENT_HOST/PORT, RESCUE_API_URL и т.д.),
см. config.py. CLI-аргументов нет, чтобы systemd-юнит был максимально
тонким.
"""

import logging
import os

import uvicorn

from .config import load_config


def main():
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    cfg = load_config()
    uvicorn.run(
        "gigachat_agent.app:app",
        host=cfg.host,
        port=cfg.port,
        log_level=os.environ.get("LOG_LEVEL", "info").lower(),
        access_log=False,    # для health-чек'ов и chat'ов одна строка избыточна
    )


if __name__ == "__main__":
    main()
