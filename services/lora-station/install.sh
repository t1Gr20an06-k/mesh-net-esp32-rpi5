#!/usr/bin/env bash
# ============================================================================
# lora-station — установка на Raspberry Pi 5 (база спасателей)
# ============================================================================
# Что делает:
#   1. Проверяет, что включён SPI (/dev/spidev0.0)
#   2. Ставит системные python3-* пакеты через apt
#      (lgpio / gpiozero / spidev в pip требуют swig + компиляцию из C —
#       на RPi OS правильный путь — apt: они уже скомпилированы)
#   3. Создаёт venv С ДОСТУПОМ к системным пакетам (--system-site-packages),
#      чтобы lgpio/gpiozero/spidev были видны без повторной установки
#   4. Если БД ещё не существует — запускает scripts/db_init/init.sh
#
# Запускать ОДИН РАЗ после клонирования репо. Скрипт идемпотентный —
# повторный запуск ничего не ломает.
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

# 2. Системные python-пакеты (готовые .so, без сборки из C)
echo "[install] Устанавливаю системные python3-* пакеты через apt"
sudo apt-get update -qq
sudo apt-get install -y \
    python3 python3-venv python3-pip \
    python3-lgpio python3-gpiozero python3-spidev \
    sqlite3

# 3. venv с доступом к системным пакетам
if [ ! -d .venv ]; then
    echo "[install] Создаю .venv (--system-site-packages, чтобы видеть apt-пакеты)"
    python3 -m venv --system-site-packages .venv
fi

# Если в requirements.txt появятся pure-python зависимости (structlog и т.п.) —
# они поставятся в venv и не конфликтуют с системными.
if [ -s requirements.txt ]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
    pip install --upgrade pip >/dev/null
    pip install -r requirements.txt
    deactivate
fi
echo "[install] Зависимости готовы"

# 4. БД
DB_PATH="${DB_PATH:-/var/lib/mesh-net/mesh.db}"
if [ ! -f "$DB_PATH" ]; then
    echo "[install] БД не найдена ($DB_PATH) — запускаю scripts/db_init/init.sh"
    bash "$ROOT/scripts/db_init/init.sh"
else
    echo "[install] БД уже существует: $DB_PATH"
fi

# 5. Доступ к GPIO/SPI без sudo
NEED_GROUPS=""
groups "$USER" | grep -qE '\bgpio\b' || NEED_GROUPS="$NEED_GROUPS gpio"
groups "$USER" | grep -qE '\bspi\b'  || NEED_GROUPS="$NEED_GROUPS spi"
if [ -n "$NEED_GROUPS" ]; then
    echo "[install] ВНИМАНИЕ: пользователь $USER не в группах:$NEED_GROUPS"
    echo "          Добавь и перелогинься (или reboot):"
    echo "            sudo usermod -aG${NEED_GROUPS// /,} $USER"
fi

echo
echo "[install] Готово. Запуск:"
echo "    source .venv/bin/activate"
echo "    python -m lora_station -v"
