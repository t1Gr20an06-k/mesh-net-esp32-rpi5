#!/usr/bin/env bash
# ============================================================================
# gigachat-agent — установка на Raspberry Pi 5 (та же машина, что и rescue-api)
# ============================================================================
# Что делает:
#   1. Создаёт venv (БЕЗ --system-site-packages: всё через pip, конфликтов нет)
#   2. Ставит requirements.txt (gigachat, fastapi, uvicorn, httpx)
#   3. Проверяет наличие token-key (предупреждает если нет)
#
# token-key должен содержать Authorization key из ЛК GigaChat — см. CLAUDE.md.
# Без него сервис стартанёт, но /chat будет возвращать "AI недоступен (...)".
#
# Запускать ОДИН РАЗ после клонирования (или после правки requirements.txt).
# Идемпотентный.
# ============================================================================

set -euo pipefail

cd "$(dirname "$0")"
echo "[install] Каталог сервиса: $(pwd)"

# 1. python
if ! command -v python3 >/dev/null; then
    echo "[install] python3 не найден — sudo apt install python3 python3-venv"
    exit 1
fi

# 2. venv
if [ ! -d .venv ]; then
    echo "[install] Создаю .venv"
    python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# 3. Зависимости
pip install --upgrade pip >/dev/null
pip install -r requirements.txt
deactivate

# 4. token-key — предупреждаем, не валим установку
if [ ! -f token-key ]; then
    echo
    echo "[install] (warn) token-key не найден."
    echo "          Без него /chat вернёт 'AI недоступен (ключ не задан)'."
    echo "          Возьми Authorization key в ЛК https://developers.sber.ru/studio"
    echo "          и положи сюда — см. services/gigachat-agent/CLAUDE.md."
fi

echo
echo "[install] Готово. Запуск руками:"
echo "    source .venv/bin/activate"
echo "    python -m gigachat_agent"
echo
echo "Проверить (на этой же машине):"
echo "    curl http://127.0.0.1:8001/health"
echo
echo "Через дашборд: http://<rpi5-ip>:8000/  (rescue-api проксирует /api/chat)"
