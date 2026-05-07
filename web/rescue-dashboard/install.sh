#!/usr/bin/env bash
# ============================================================================
# rescue-dashboard — установка вендорных JS-зависимостей (Leaflet)
# ============================================================================
# Зачем: дашборд работает без сборщика, Leaflet подключаем как обычный
# <script src="/lib/leaflet.js">. Сами файлы в репо не коммитим (вендор-код)
# — этот скрипт скачивает их в ./lib/ при установке.
#
# Запуск (на RPi5 или на разработке):
#   bash web/rescue-dashboard/install.sh
#
# Идемпотентный — если файлы уже на месте, пропускает скачивание.
# ============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LIB_DIR="$SCRIPT_DIR/lib"
IMG_DIR="$LIB_DIR/images"

# Закрепляем версию явно — если позже понадобится обновить, меняется в одном месте.
LEAFLET_VERSION="1.9.4"
BASE="https://unpkg.com/leaflet@$LEAFLET_VERSION/dist"

mkdir -p "$IMG_DIR"

echo "[install] Leaflet $LEAFLET_VERSION → $LIB_DIR"

download() {
    local url="$1"
    local dst="$2"
    if [ -s "$dst" ]; then
        echo "  [skip] $(basename "$dst") (уже есть)"
        return
    fi
    echo "  [get]  $url"
    curl -fsSL "$url" -o "$dst"
}

download "$BASE/leaflet.css"               "$LIB_DIR/leaflet.css"
download "$BASE/leaflet.js"                "$LIB_DIR/leaflet.js"
# Иконки маркеров — Leaflet CSS ссылается на них как url(images/...) относительно css
download "$BASE/images/marker-icon.png"    "$IMG_DIR/marker-icon.png"
download "$BASE/images/marker-icon-2x.png" "$IMG_DIR/marker-icon-2x.png"
download "$BASE/images/marker-shadow.png"  "$IMG_DIR/marker-shadow.png"

echo
echo "[install] Leaflet установлен. Дашборд работает оффлайн целиком,"
echo "          включая тайлы карты — они отдельно скачиваются скриптом:"
echo "            python3 scripts/import_tiles/download_tiles.py --bbox ... --zoom 10-14"
echo "          (см. scripts/import_tiles/CLAUDE.md)"
