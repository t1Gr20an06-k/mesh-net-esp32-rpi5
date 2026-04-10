#!/usr/bin/env bash
# ============================================================
# Mesh-net Тропы — инициализация базы данных
# Запуск: bash scripts/db_init/init.sh
#
# Что делает:
#   1. Создаёт директорию /var/lib/mesh-net/ (если нет)
#   2. Создаёт SQLite базу mesh.db с таблицами из init.sql
#   3. Ставит права доступа
#
# Безопасен для повторного запуска — CREATE TABLE IF NOT EXISTS.
# ============================================================

set -euo pipefail

DB_DIR="/var/lib/mesh-net"
DB_PATH="${DB_DIR}/mesh.db"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SQL_FILE="${SCRIPT_DIR}/init.sql"

# --- Проверка sqlite3 ---
if ! command -v sqlite3 &> /dev/null; then
    echo "ОШИБКА: sqlite3 не найден. Установите: sudo apt install sqlite3"
    exit 1
fi

# --- Проверка файла схемы ---
if [ ! -f "$SQL_FILE" ]; then
    echo "ОШИБКА: не найден ${SQL_FILE}"
    exit 1
fi

# --- Создание директории ---
echo ">>> Создаю директорию ${DB_DIR} ..."
sudo mkdir -p "$DB_DIR"
sudo chown "$(whoami):$(whoami)" "$DB_DIR"

# --- Применение схемы ---
echo ">>> Применяю схему из ${SQL_FILE} ..."
sqlite3 "$DB_PATH" < "$SQL_FILE"

# --- Проверка ---
TABLE_COUNT=$(sqlite3 "$DB_PATH" "SELECT count(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';")
echo ">>> Готово! База: ${DB_PATH}"
echo ">>> Таблиц создано: ${TABLE_COUNT}"
echo ""
echo "Таблицы:"
sqlite3 "$DB_PATH" ".tables"
echo ""
echo "Для проверки: sqlite3 ${DB_PATH} '.schema'"
