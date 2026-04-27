# lora-sniffer — тестовый приёмник LoRa-пакетов

**Что это:** минимальный C++ приёмник на RPi5, который слушает LoRa на 868 МГц,
декодирует 64-байтные пакеты общим кодеком `MeshPacket` и печатает их в консоль.

**Зачем:** первая проверка, что канал ESP32 → RPi5 реально работает. НЕ замена
полноценному `services/lora-station/` демону — это временный тест.

---

## 0. Железо и подключение

HT-RA62 → RPi5 (пины по схеме в `CLAUDE.md`):

| Модуль | Физ. пин RPi5 | BCM GPIO |
|---|---|---|
| VCC (3.3V) | 17 | — |
| GND | 20 | — |
| MOSI | 19 | GPIO 10 |
| MISO | 21 | GPIO 9 |
| SCK  | 23 | GPIO 11 |
| NSS  | 24 | GPIO 8 |
| NRST | 15 | GPIO 22 |
| DIO1 | 16 | GPIO 23 |
| BUSY | 18 | GPIO 24 |

⚠ **Питание строго 3.3В** (пин 17), не 5В — модуль сгорит от 5В.

## 1. Подготовка RPi5 (разовая)

```bash
# Включить SPI
sudo raspi-config nonint do_spi 0
sudo reboot

# После перезагрузки проверить
ls /dev/spidev0.*      # должно быть /dev/spidev0.0

# Установить зависимости сборки
sudo apt update
sudo apt install -y build-essential cmake git liblgpio-dev

# Склонировать RadioLib (по умолчанию в ~/RadioLib)
git clone https://github.com/jgromes/RadioLib.git ~/RadioLib
```

## 2. Сборка снифера

```bash
cd ~/mesh-net/tests/field/lora-sniffer   # путь до этого каталога в твоём репо
mkdir -p build && cd build
cmake ..
make -j4
```

Если `RadioLib` лежит не в `~/RadioLib`:
```bash
cmake -DRADIOLIB_DIR=/своя/папка/RadioLib ..
```

## 3. Запуск

```bash
sudo ./lora-sniffer
```

`sudo` нужен для доступа к `/dev/spidev0.0` и GPIO.

Ожидаемый вывод:
```
=== Mesh-net Тропы — LoRa снифер (RPi5) ===
Слушаем 868.0 МГц, SF10, BW125 кГц, CR 4/5
[SX1262] init ... OK
[RX] ожидание пакетов (Ctrl-C для выхода)...

[     1] OK  PING  dev=1 ch=0 ttl=3 lat=55750000 lon=37620000  RSSI=-42.5 дБм  SNR=9.2 дБ
[     2] OK  PING  dev=1 ch=0 ttl=3 lat=55750000 lon=37620000  RSSI=-42.0 дБм  SNR=9.5 дБ
```

Выход — **Ctrl-C**.

## 4. Что проверять

- [ ] `[SX1262] init ... OK` — железо подключено правильно, SPI работает
- [ ] Пакеты приходят раз в ~10 секунд (интервал PING на ESP32)
- [ ] `OK PING dev=1` — CRC-16 совпадает → кодеки C++/ESP32 и C++/RPi5 согласованы
- [ ] `lat=55750000 lon=37620000` — координаты-заглушка из ESP32 (Москва)
- [ ] RSSI разумный: −30 до −100 дБм на расстоянии в одну квартиру

## 5. Типовые проблемы

| Симптом | Что это | Что делать |
|---|---|---|
| `init FAIL, code -2` | SPI не виден | `ls /dev/spidev0.*`, включить через `raspi-config` |
| `init FAIL, code -707` | TCXO voltage не та | Попробовать `RADIO_TCXO_V = 1.6` или `0.0` в main.cpp |
| Пакеты не идут совсем | Разные настройки радио | Убедиться что частота/SF/BW/CR совпадают с ESP32 |
| `CRC` вместо `OK` | Биты искажаются | Проверить антенну и расстояние (близко тоже плохо — перегруз) |
| Ничего не собирается | Нет lgpio | `sudo apt install liblgpio-dev` |
