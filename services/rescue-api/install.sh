#!/usr/bin/env bash
# ============================================================================
# rescue-api — установка на Raspberry Pi 5 (база спасателей)
# ============================================================================
# Что делает:
#   1. Проверяет, что БД уже создана (её делает scripts/db_init/init.sh,
#      запускается из services/lora-station/install.sh)
#   2. Создаёт venv (БЕЗ --system-site-packages: FastAPI/uvicorn ставятся
#      pip-ом, конфликтов с системой нет)
#   3. Ставит requirements.txt
#
# Запускать ОДИН РАЗ после клонирования (или после правки requirements.txt).
# Идемпотентный.
# ============================================================================

set -euo pipefail

cd "$(dirname "$0")"
echo "[install] Каталог сервиса: $(pwd)"

# 1. БД должна существовать
DB_PATH="${DB_PATH:-/var/lib/mesh-net/mesh.db}"
if [ ! -f "$DB_PATH" ]; then
    echo "[install] БД не найдена: $DB_PATH"
    echo "          Сначала установи lora-station — он создаст БД:"
    echo "            cd ../lora-station && bash install.sh"
    exit 1
fi
echo "[install] БД найдена: $DB_PATH"

# 2. Системный python (нужен 3.11+, на RPi OS Bookworm — 3.11)
if ! command -v python3 >/dev/null; then
    echo "[install] python3 не найден — sudo apt install python3 python3-venv"
    exit 2
fi

# 3. venv
if [ ! -d .venv ]; then
    echo "[install] Создаю .venv"
    python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# 4. Зависимости
# --timeout 60 --retries 10: PyPI-CDN на больших бинарных whl-ах
# (pydantic_core, uvloop) у нас в РФ периодически отваливается по
# дефолтному 15-сек таймауту. Лучше подождать дольше, чем падать.
pip install --upgrade pip >/dev/null
pip install --timeout 60 --retries 10 -r requirements.txt
deactivate

# 5. Статика дашборда: скачать Leaflet в web/rescue-dashboard/lib/
# rescue-api сам отдаёт эти файлы (StaticFiles на /), так что без них
# на http://<rpi5-ip>:8000/ будет 404 на /lib/leaflet.js.
DASHBOARD_INSTALL="$(cd ../../web/rescue-dashboard && pwd)/install.sh"
if [ -x "$DASHBOARD_INSTALL" ] || [ -f "$DASHBOARD_INSTALL" ]; then
    echo
    echo "[install] Запускаю установщик дашборда (Leaflet)..."
    bash "$DASHBOARD_INSTALL"
else
    echo "[install] (warn) $DASHBOARD_INSTALL не найден — пропускаю"
fi

echo
echo "[install] Готово. Запуск руками:"
echo "    source .venv/bin/activate"
echo "    python -m rescue_api"
echo
echo "Проверить:"
echo "    curl http://localhost:8000/api/stats"
echo "    открой http://<rpi5-ip>:8000/        (дашборд с картой)"
echo "    открой http://<rpi5-ip>:8000/docs    (swagger UI)"
