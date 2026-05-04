#!/usr/bin/env bash
# ============================================================================
# Mesh-net Тропы — установка systemd-юнитов
# ============================================================================
# Что делает:
#   1. Берёт шаблоны *.service.template в этом каталоге
#   2. Подставляет в них __USER__ (твой логин) и __REPO__ (путь к этому репо)
#   3. Кладёт результат в /etc/systemd/system/
#   4. systemctl daemon-reload + enable + restart
#
# Запуск (на RPi5 из корня репозитория):
#   sudo bash scripts/systemd/install.sh
#
# Идемпотентный — повторный запуск перезапишет юниты и перезапустит сервисы.
# ============================================================================

set -euo pipefail

# --- 0. Определяем пути и пользователя --------------------------------------
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Когда запускаем через sudo, $USER = root. Нам нужен реальный пользователь —
# тот, под которым крутится lora-station (он в группах gpio/spi).
TARGET_USER="${SUDO_USER:-$USER}"
if [ "$TARGET_USER" = "root" ]; then
    echo "[err] Запусти через sudo от обычного пользователя:"
    echo "      sudo bash $0"
    exit 1
fi

echo "[install] Репо:          $REPO_DIR"
echo "[install] Пользователь:  $TARGET_USER"

# --- 1. Sanity-check: venv должен существовать ------------------------------
VENV_PY="$REPO_DIR/services/lora-station/.venv/bin/python"
if [ ! -x "$VENV_PY" ]; then
    echo "[err] Не найден $VENV_PY"
    echo "      Сначала установи lora-station:"
    echo "        cd services/lora-station && bash install.sh"
    exit 2
fi

# --- 2. Sanity-check: пользователь в нужных группах -------------------------
for grp in gpio spi; do
    if ! id -nG "$TARGET_USER" | grep -qw "$grp"; then
        echo "[warn] Пользователь $TARGET_USER не в группе $grp."
        echo "       Демон не сможет дёргать SPI/GPIO. Поправь:"
        echo "         sudo usermod -aG gpio,spi $TARGET_USER"
        echo "       и перелогинься (или reboot)."
    fi
done

# --- 3. Sanity-check: не запущен ли демон руками? ---------------------------
if pgrep -f "python -m lora_station" >/dev/null 2>&1; then
    echo "[warn] Демон уже запущен руками (python -m lora_station)."
    echo "       Останови его (Ctrl-C в той консоли), иначе systemd-копия"
    echo "       не сможет открыть /dev/spidev0.0 и упадёт."
    read -r -p "       Продолжить установку юнита всё равно? [y/N] " ans
    [ "$ans" = "y" ] || [ "$ans" = "Y" ] || { echo "Отменено."; exit 3; }
fi

# --- 4. Подстановка плейсхолдеров и установка юнита -------------------------
SVC_NAME="mesh-lora-station.service"
SRC="$SCRIPT_DIR/$SVC_NAME"
DST="/etc/systemd/system/$SVC_NAME"

if [ ! -f "$SRC" ]; then
    echo "[err] Не найден шаблон $SRC"
    exit 4
fi

echo "[install] Генерирую $DST"
sed -e "s|__USER__|$TARGET_USER|g" \
    -e "s|__REPO__|$REPO_DIR|g" \
    "$SRC" > /tmp/$SVC_NAME
sudo install -m 0644 /tmp/$SVC_NAME "$DST"
rm -f /tmp/$SVC_NAME

# --- 5. Перечитать unit-файлы и запустить -----------------------------------
echo "[install] systemctl daemon-reload"
sudo systemctl daemon-reload

echo "[install] systemctl enable $SVC_NAME (автозапуск при загрузке)"
sudo systemctl enable "$SVC_NAME"

echo "[install] systemctl restart $SVC_NAME"
sudo systemctl restart "$SVC_NAME"

# Дать пару секунд на старт, потом показать статус
sleep 2
echo
echo "============================================================"
sudo systemctl status "$SVC_NAME" --no-pager -l || true
echo "============================================================"
echo
echo "[install] Готово. Полезные команды:"
echo "  sudo journalctl -u $SVC_NAME -f         # live-логи"
echo "  sudo systemctl status $SVC_NAME         # текущее состояние"
echo "  sudo systemctl restart $SVC_NAME        # перезапустить"
echo "  sudo systemctl disable --now $SVC_NAME  # выключить автозапуск"
