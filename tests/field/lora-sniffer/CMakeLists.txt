# ============================================================================
# lora-sniffer — тестовый приёмник LoRa-пакетов для RPi5
# Собирается на самом RPi5 (ARM64). На Windows НЕ собирается.
# ============================================================================

cmake_minimum_required(VERSION 3.18)
project(lora-sniffer CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# --- Путь до исходников RadioLib --------------------------------------------
# По умолчанию ждём, что RadioLib склонирован в ~/RadioLib
# Можно переопределить: cmake -DRADIOLIB_DIR=/path/to/RadioLib ..
if(NOT DEFINED RADIOLIB_DIR)
    set(RADIOLIB_DIR "$ENV{HOME}/RadioLib" CACHE PATH "Путь к исходникам RadioLib")
endif()

if(NOT EXISTS "${RADIOLIB_DIR}/CMakeLists.txt")
    message(FATAL_ERROR
        "RadioLib не найден в ${RADIOLIB_DIR}. "
        "Склонируй: git clone https://github.com/jgromes/RadioLib.git ~/RadioLib")
endif()

# --- Путь до нашего кодека (живёт в firmware/) ------------------------------
set(MESH_PACKET_DIR "${CMAKE_CURRENT_SOURCE_DIR}/../../../firmware/esp32-terminal/lib/mesh_packet")

# --- Путь до RadioLib HAL для Raspberry Pi ---------------------------------
# В свежем RadioLib PiHal.h лежит в src/hal/RPi/ (раньше был в examples/)
set(PI_HAL_DIR "${RADIOLIB_DIR}/src/hal/RPi")

# --- Подключаем RadioLib как сабпроект --------------------------------------
add_subdirectory(${RADIOLIB_DIR} radiolib_build EXCLUDE_FROM_ALL)

# --- Наш бинарник -----------------------------------------------------------
add_executable(${PROJECT_NAME}
    main.cpp
    ${MESH_PACKET_DIR}/MeshPacket.cpp
)

target_include_directories(${PROJECT_NAME} PRIVATE
    ${MESH_PACKET_DIR}
    ${RADIOLIB_DIR}/src
    ${PI_HAL_DIR}
)

target_link_libraries(${PROJECT_NAME} PRIVATE
    RadioLib
    lgpio
)
