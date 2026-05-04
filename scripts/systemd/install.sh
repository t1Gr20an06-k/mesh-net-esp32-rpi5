#!/usr/bin/env bash
# ============================================================================
# Mesh-net Тропы — установка systemd-юнитов
# ============================================================================
# Что делает:
#   1. Идёт по всем *.service в scripts/systemd/
#   2. В каждом подставляет __USER__ (твой логин) и __REPO__ (путь к репо)
#   3. Проверяет, что venv для этого сервиса собран — иначе пропускает
#   4. Кладёт результат в /etc/systemd/system/, daemon-reload + enable + restart
#
# Запуск (на RPi5):
#   sudo bash scripts/systemd/install.sh                    # все доступные юниты
#   sudo bash scripts/systemd/install.sh mesh-rescue-api    # только один
#
# Идемпотентный — повторный запуск перезапишет юниты и перезапустит сервисы.
# ============================================================================

set -euo pipefail

# --- 0. Определяем пути и пользователя --------------------------------------
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Когда запускаем через sudo, $USER = root. Нам нужен реальный пользователь —
# тот, под которым крутятся сервисы (он в группах gpio/spi для lora-station).
TARGET_USER="${SUDO_USER:-$USER}"
if [ "$TARGET_USER" = "root" ]; then
    echo "[err] Запусти через sudo от обычного пользователя:"
    echo "      sudo bash $0"
    exit 1
fi

echo "[install] Репо:         $REPO_DIR"
echo "[install] Пользователь: $TARGET_USER"

# --- Утилиты ----------------------------------------------------------------

# Достаём из шаблона ExecStart, подставляем __REPO__, возвращаем путь к exe
exec_path_of() {
    local unit="$1"
    grep -E '^ExecStart=' "$unit" | head -1 \
        | sed -e 's|^ExecStart=||' -e "s|__REPO__|$REPO_DIR|g" \
        | awk '{print $1}'
}

# Спец-проверки перед установкой конкретного юнита.
# Возвращает 0 — устанавливать, 1 — пропустить (не криминал).
preflight() {
    local svc_name="$1"
    local unit="$2"

    # 1. venv должен существовать
    local exe
    exe=$(exec_path_of "$unit")
    if [ ! -x "$exe" ]; then
        echo "[skip] $svc_name: $exe не найден."
        echo "       Сначала установи сервис вручную, например:"
        case "$svc_name" in
            mesh-lora-station.service)
                echo "         cd services/lora-station && bash install.sh" ;;
            mesh-rescue-api.service)
                echo "         cd services/rescue-api && bash install.sh" ;;
            mesh-gigachat-agent.service)
                echo "         cd services/gigachat-agent && bash install.sh" ;;
            *)
                echo "         (см. CLAUDE.md соответствующего сервиса)" ;;
        esac
        return 1
    fi

    # 2. lora-station — отдельные проверки (группы + не запущен ли руками)
    if [ "$svc_name" = "mesh-lora-station.service" ]; then
        for grp in gpio spi; do
            if ! id -nG "$TARGET_USER" | grep -qw "$grp"; then
                echo "[warn] Пользователь $TARGET_USER не в группе $grp."
                echo "       Демон lora-station не сможет дёргать SPI/GPIO. Поправь:"
                echo "         sudo usermod -aG gpio,spi $TARGET_USER"
                echo "       и перелогинься (или reboot)."
            fi
        done
        if pgrep -f "python -m lora_station" >/dev/null 2>&1; then
            echo "[warn] lora_station запущен руками (python -m lora_station)."
            echo "       Останови (Ctrl-C в той консоли), иначе systemd-копия"
            echo "       не сможет открыть /dev/spidev0.0 и упадёт."
            read -r -p "       Продолжить установку всё равно? [y/N] " ans
            if [ "$ans" != "y" ] && [ "$ans" != "Y" ]; then
                echo "[skip] $svc_name: отменено пользователем."
                return 1
            fi
        fi
    fi

    return 0
}

install_unit() {
    local unit="$1"
    local svc_name
    svc_name=$(basename "$unit")

    echo
    echo "[install] === $svc_name ==="

    if ! preflight "$svc_name" "$unit"; then
        return 0
    fi

    local dst="/etc/systemd/system/$svc_name"
    local tmp="/tmp/$svc_name"

    sed -e "s|__USER__|$TARGET_USER|g" \
        -e "s|__REPO__|$REPO_DIR|g" \
        "$unit" > "$tmp"
    sudo install -m 0644 "$tmp" "$dst"
    rm -f "$tmp"

    sudo systemctl daemon-reload
    sudo systemctl enable "$svc_name" >/dev/null 2>&1
    sudo systemctl restart "$svc_name"
    sleep 2
    sudo systemctl status "$svc_name" --no-pager -l --lines=12 || true
}

# --- Список юнитов: либо аргументы, либо все *.service ----------------------

UNITS=()
if [ "$#" -gt 0 ]; then
    for name in "$@"; do
        # Принимаем "mesh-rescue-api" или "mesh-rescue-api.service"
        unit="$SCRIPT_DIR/${name%.service}.service"
        if [ ! -f "$unit" ]; then
            echo "[err] Не найден шаблон $unit"
            exit 2
        fi
        UNITS+=("$unit")
    done
else
    # shellcheck disable=SC2207
    UNITS=( $(ls "$SCRIPT_DIR"/*.service 2>/dev/null) )
    if [ "${#UNITS[@]}" -eq 0 ]; then
        echo "[err] В $SCRIPT_DIR нет ни одного *.service шаблона"
        exit 3
    fi
fi

for unit in "${UNITS[@]}"; do
    install_unit "$unit"
done

echo
echo "============================================================"
echo "[install] Готово. Полезные команды:"
echo "  sudo journalctl -u 'mesh-*' -f          # live-логи всех сервисов"
echo "  sudo systemctl status mesh-*            # текущее состояние"
echo "  sudo systemctl restart mesh-rescue-api  # перезапустить конкретный"
echo "  sudo systemctl disable --now mesh-rescue-api  # выключить автозапуск"
