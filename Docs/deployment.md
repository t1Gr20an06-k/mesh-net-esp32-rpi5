# Развёртывание

## Требования

| Компонент | Требования |
|-----------|------------|
| Raspberry Pi 5 | Raspberry Pi OS 64-bit (Bookworm), Python 3.11+, Node.js 20+ |
| ESP32 | PlatformIO Core 6+, Python 3.8+ |
| Сеть развёртывания | Не требуется (всё офлайн) |

---

## RPi5 — первоначальная настройка

```bash
# Обновление системы
sudo apt update && sudo apt upgrade -y

# Зависимости
sudo apt install -y python3-pip python3-venv git sqlite3 nginx dnsmasq nodejs npm

# Включить SPI для SX1262
sudo raspi-config nonint do_spi 0

# Проверить
ls /dev/spidev0.*   # должно быть spidev0.0

# Создать директорию для данных
sudo mkdir -p /var/lib/mesh-net
sudo chown $USER:$USER /var/lib/mesh-net

# Клонировать репозиторий
git clone <repo_url> /opt/mesh-net
cd /opt/mesh-net
```

---

## Инициализация БД

```bash
cd /opt/mesh-net
bash scripts/db_init/init.sh
# База создана: /var/lib/mesh-net/mesh.db
```

---

## Установка сервисов

### lora-station (на обоих RPi)

```bash
cd /opt/mesh-net/services/lora-station
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Тест (mock режим без железа)
python -m lora_station --mock --verbose
```

### rescue-api (только на базе спасателей)

```bash
cd /opt/mesh-net/services/rescue-api
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Тест
uvicorn app.main:app --host 0.0.0.0 --port 8000
# Проверить: curl http://localhost:8000/devices
```

### gigachat-agent (только на базе спасателей)

```bash
cd /opt/mesh-net/services/gigachat-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Требуется токен GigaChat
export GIGACHAT_TOKEN="your_token_here"
uvicorn app.main:app --host 127.0.0.1 --port 8001
```

### relay-node (только на инфо-точке)

```bash
cd /opt/mesh-net/services/relay-node
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m relay_node
```

---

## Systemd units

Скопировать все `.service` файлы:

```bash
sudo cp /opt/mesh-net/scripts/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
```

### База спасателей

```bash
sudo systemctl enable --now mesh-lora-station
sudo systemctl enable --now mesh-rescue-api
sudo systemctl enable --now mesh-gigachat-agent
```

### Инфо-точка

```bash
sudo systemctl enable --now mesh-lora-station
sudo systemctl enable --now mesh-relay-node
sudo systemctl enable --now mesh-captive-portal  # nginx + dnsmasq
```

---

## Nginx — база спасателей

`/etc/nginx/sites-available/mesh-rescue`:

```nginx
server {
    listen 80;
    server_name _;

    # Дашборд спасателей
    location / {
        root /opt/mesh-net/web/rescue-dashboard/dist;
        try_files $uri $uri/ /index.html;
    }

    # API
    location /api/ {
        proxy_pass http://127.0.0.1:8000/;
        proxy_set_header Host $host;
    }

    # WebSocket
    location /ws {
        proxy_pass http://127.0.0.1:8000/ws;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }

    # GigaChat
    location /ai/ {
        proxy_pass http://127.0.0.1:8001/;
    }
}
```

---

## Nginx + dnsmasq — инфо-точка (captive portal)

`/etc/dnsmasq.conf` (добавить):
```
interface=wlan0
dhcp-range=192.168.10.10,192.168.10.100,24h
address=/#/192.168.10.1
```

`/etc/nginx/sites-available/mesh-portal`:
```nginx
server {
    listen 80 default_server;
    server_name _;

    # Captive portal redirect
    location /generate_204    { return 302 http://192.168.10.1/; }
    location /hotspot-detect  { return 302 http://192.168.10.1/; }
    location /ncsi.txt        { return 302 http://192.168.10.1/; }

    location / {
        root /opt/mesh-net/web/info-portal;
        index index.html;
    }

    location /api/ {
        proxy_pass http://127.0.0.1:8002/;  # relay-node API
    }
}
```

---

## Сборка и прошивка ESP32

```bash
cd /opt/mesh-net/firmware/esp32-terminal

# Настроить device_id и параметры в include/config.h
# DEVICE_ID, WIFI_SSID, WIFI_PASS (для AP-режима)

# Сборка
pio run

# Прошивка (подключить ESP32 по USB)
pio run -t upload

# Мониторинг
pio device monitor -b 115200
```

---

## Импорт офлайн-карт

```bash
# Скачать тайлы OSM для нужного района (zoom 10–16)
# Пример: Архыз
python scripts/import_tiles/download_tiles.py \
    --bbox "43.4,41.1,43.6,41.5" \
    --zoom "10-16" \
    --output /opt/mesh-net/web/rescue-dashboard/public/tiles/
```

---

## Переменные окружения (production)

Создать `/etc/mesh-net/env`:
```bash
GIGACHAT_TOKEN=ваш_токен
DB_PATH=/var/lib/mesh-net/mesh.db
LORA_SPI_BUS=0
LORA_SPI_CS=0
LORA_RESET_PIN=22
LORA_DIO1_PIN=23
LORA_BUSY_PIN=24
RESCUE_WHITELIST=0x0100,0x0101,0x0102
LOG_LEVEL=INFO
```

---

## Проверка работоспособности

```bash
# Статус сервисов
sudo systemctl status mesh-*

# Последние логи
sudo journalctl -u mesh-lora-station -n 50 --no-pager

# Тест API
curl http://localhost:8000/stats | python3 -m json.tool

# Тест отправки тестового пакета
python scripts/test_packets/send_ping.py --device-id 0x0001 --mock
```
