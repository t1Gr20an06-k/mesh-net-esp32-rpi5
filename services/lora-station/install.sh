#!/usr/bin/env bash
# ============================================================================
# lora-station — установка на Raspberry Pi 5 (база спасателей)
# ============================================================================
# Что делает:
#   1. Проверяет, что включён SPI (/dev/spidev0.0)
#   2. Создаёт venv в .venv/ и ставит зависимости (spidev, gpiozero, lgpio)
#   3. Если БД ещё не существует — запускает scripts/db_init/init.sh
#
# Запускать ОДИН РАЗ после клонирования репо. Дальше — `python -m lora_station`.
# Скрипт идемпотентный: повторный запуск ничего не ломает.
# ============================================================================

set -euo pipefail

cd "$(dirname "$0")"
SVC_DIR="$(pwd)"
ROOT="$(cd ../.. && pwd)"

echo "[install] Каталог сервиса: $SVC_DIR"

# 1. SPI
if [ ! -e /dev/spidev0.0 ]; then
    echo "[install] /dev/spidev0.0 нет — включи SPI:"
    echo "          sudo raspi-config nonint do_spi 0 && sudo reboot"
    exit 1
fi
echo "[install] SPI ok: /dev/spidev0.0"

# 2. venv
if [ ! -d .venv ]; then
    echo "[install] Создаю .venv (Python 3)"
    python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --upgrade pip >/dev/null
pip install -r requirements.txt
deactivate
echo "[install] Зависимости поставлены"

# 3. БД
DB_PATH="${DB_PATH:-/var/lib/mesh-net/mesh.db}"
if [ ! -f "$DB_PATH" ]; then
    echo "[install] БД не найдена ($DB_PATH) — запускаю scripts/db_init/init.sh"
    bash "$ROOT/scripts/db_init/init.sh"
else
    echo "[install] БД уже существует: $DB_PATH"
fi

# 4. Доступ к GPIO/SPI без sudo
if ! groups "$USER" | grep -q '\bgpio\b' || ! groups "$USER" | grep -q '\bspi\b'; then
    echo "[install] ВНИМАНИЕ: пользователь $USER не в группах gpio/spi."
    echo "          Добавь и перелогинься:"
    echo "            sudo usermod -aG gpio,spi $USER"
fi

echo
echo "[install] Готово. Запуск:"
echo "    source .venv/bin/activate"
echo "    python -m lora_station"
