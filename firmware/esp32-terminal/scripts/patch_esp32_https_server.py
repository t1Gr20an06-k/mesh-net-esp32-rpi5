# ============================================================================
# pre-build patch для fhessel/esp32_https_server v1.0.0
# ============================================================================
# Зачем: библиотека не обновлялась с 2022 года, она #include'ит
#   <hwcrypto/sha.h> — заголовок удалён из ESP-IDF 5.x, а в ESP-IDF 4.4
#   (Arduino-ESP32 v2.0.x) уже отсутствует. Используется он ровно в одной
#   функции — websocketKeyResponseHash() для рукопожатия WebSocket.
#   Мы WebSocket не задействуем, но компилироваться код всё равно должен.
#
# Что делаем: меняем хэш-вызов на mbedtls (который точно есть в любой
#   сборке Arduino-ESP32, т.к. через него идёт NetworkClientSecure):
#       #include <hwcrypto/sha.h>     →  #include <mbedtls/sha1.h>
#       esp_sha(SHA1, in, len, out)   →  mbedtls_sha1(in, len, out)
#
# Идемпотентно: если патч уже применён, выходим молча.
# ============================================================================

# pylint: disable=undefined-variable
Import("env")  # noqa: F821

import os

LIB = os.path.join(
    env.subst("$PROJECT_DIR"),
    ".pio", "libdeps", env.subst("$PIOENV"),
    "esp32_https_server", "src",
)

PATCHES = {
    "HTTPConnection.hpp": [
        ("#include <hwcrypto/sha.h>", "#include <mbedtls/sha1.h>"),
    ],
    "HTTPConnection.cpp": [
        # esp_sha принимал тип хэша первым аргументом, mbedtls_sha1 — нет
        ("esp_sha(SHA1, ", "mbedtls_sha1("),
    ],
}


def patch(filename, replacements):
    path = os.path.join(LIB, filename)
    if not os.path.exists(path):
        # Библиотека ещё не скачана (первый прогон) — выйдем, на следующем
        # запуске LDF её установит и патч применится.
        return
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    new = text
    for old, repl in replacements:
        new = new.replace(old, repl)
    if new != text:
        with open(path, "w", encoding="utf-8") as f:
            f.write(new)
        print(f"[patch_esp32_https_server] {filename}: применён патч")


for fname, repls in PATCHES.items():
    patch(fname, repls)
