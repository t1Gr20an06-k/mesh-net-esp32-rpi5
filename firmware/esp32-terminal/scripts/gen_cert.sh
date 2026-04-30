#!/usr/bin/env bash
# ============================================================================
# Mesh-net Тропы — генерация самоподписанного TLS-сертификата для ESP32
# ============================================================================
# Зачем нужен:
#   Браузеры разрешают navigator.geolocation только в "secure context"
#   (HTTPS или localhost). Локальный IP 192.168.4.1 secure-контекстом не
#   считается, поэтому терминал поднимает HTTPS — и для этого нужен
#   сертификат. Используется ОДИН раз в жизни прошивки: при сборке.
#
# Что делает:
#   1. Создаёт RSA-2048 ключ
#   2. Генерирует самоподписанный X.509-сертификат на 10 лет
#      (CN=192.168.4.1, SAN: IP:192.168.4.1)
#   3. Конвертирует cert и key в DER
#   4. Вшивает байтовые массивы в include/cert.h
#
# Почему DER (а не PEM):
#   fhessel/esp32_https_server в конструкторе SSLCert требует именно DER
#   (см. README библиотеки). PEM — это base64-обёртка над DER, ESP32 её
#   декодировать не будет.
#
# Когда запускать:
#   - Один раз перед первой сборкой прошивки
#   - При смене IP точки доступа (если когда-нибудь поменяем 192.168.4.1)
#   - При окончании срока действия (через 10 лет — вряд ли актуально)
#
# Безопасность:
#   "Приватный" ключ embedded в каждое устройство одинаков, и при
#   физическом доступе к ESP32 его легко достать дампом флеша. Считаем
#   его НЕ секретом — это локальный mesh без внешней сети, угроза MITM
#   гипотетическая. Всё ради того, чтобы браузер дал доступ к GPS.
# ============================================================================

set -euo pipefail

cd "$(dirname "$0")/.."   # firmware/esp32-terminal/
ROOT="$(pwd)"
TMP="$(mktemp -d)"
trap "rm -rf '$TMP'" EXIT

OUT_HEADER="$ROOT/include/cert.h"
CN="192.168.4.1"
DAYS=3650

echo "[gen_cert] Рабочая папка: $TMP"

# 1. Приватный ключ
openssl genrsa -out "$TMP/key.pem" 2048 2>/dev/null
echo "[gen_cert] RSA-2048 ключ сгенерирован"

# 2. openssl-конфиг — через файл, чтобы Git-Bash на Windows не корёжил
# командную строку (MSYS превращает "/CN=..." в путь к Git-папке).
cat > "$TMP/openssl.cnf" <<EOF
[req]
distinguished_name = dn
x509_extensions    = v3_req
prompt             = no

[dn]
CN = $CN

[v3_req]
subjectAltName = IP:$CN
basicConstraints = critical,CA:TRUE
EOF

# 3. Самоподписанный сертификат
openssl req -new -x509 -key "$TMP/key.pem" -out "$TMP/cert.pem" \
    -days "$DAYS" -config "$TMP/openssl.cnf" 2>/dev/null
echo "[gen_cert] Сертификат: CN=$CN, срок $DAYS дней"

# 4. PEM → DER
openssl x509 -in "$TMP/cert.pem" -out "$TMP/cert.der" -outform DER
openssl rsa  -in "$TMP/key.pem"  -out "$TMP/key.der"  -outform DER 2>/dev/null

CERT_LEN=$(wc -c < "$TMP/cert.der")
KEY_LEN=$(wc -c < "$TMP/key.der")
echo "[gen_cert] DER: cert=$CERT_LEN байт, key=$KEY_LEN байт"

# 5. xxd -i выводит уже C-массив; объединяем в один заголовок
{
    cat <<EOF
// ============================================================================
// АВТОГЕНЕРАЦИЯ. Не править вручную.
// Источник: firmware/esp32-terminal/scripts/gen_cert.sh
// CN: $CN, валидность: $DAYS дней.
// ============================================================================
#pragma once
#include <stdint.h>

EOF
    # xxd -i создаёт `unsigned char NAME[]` и `unsigned int NAME_len`
    xxd -i -n mesh_cert_der "$TMP/cert.der"
    echo
    xxd -i -n mesh_key_der  "$TMP/key.der"
} > "$OUT_HEADER"

echo "[gen_cert] Готово: $OUT_HEADER"
