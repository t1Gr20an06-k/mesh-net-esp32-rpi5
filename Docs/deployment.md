# Развёртывание

Источник истины — bash-скрипты в `services/*/install.sh` и
`scripts/systemd/install.sh`. Этот документ — обзорная инструкция, для
деталей читай root [`CLAUDE.md`](../CLAUDE.md) и сервисные `CLAUDE.md`.

## Требования

| Компонент | Требования |
|-----------|------------|
| Raspberry Pi 5 | Raspberry Pi OS 64-bit (Bookworm), Python 3.11+ |
| ESP32-S3 N16R8 | PlatformIO Core 6+, Python 3.8+ для билда |
| Сеть развёртывания | Не требуется (всё офлайн) |

---

## Первый запуск на RPi5 базы

Выполняется один раз после клонирования. Каждый шаг идемпотентен.

```bash
git clone <repo_url> ~/mesh-net && cd ~/mesh-net

# 1. SPI должен быть включён (для HT-RA62)
ls /dev/spidev0.*    # ожидаем /dev/spidev0.0
# Если нет:
sudo raspi-config nonint do_spi 0 && sudo reboot

# 2. lora-station — apt-пакеты (lgpio через apt!) + venv + init БД
cd services/lora-station && bash install.sh && cd ../..

# 3. rescue-api — venv + Leaflet (через web/rescue-dashboard/install.sh)
cd services/rescue-api && bash install.sh && cd ../..

# 4. gigachat-agent — venv + GigaChat SDK
#    Положить Authorization key в services/gigachat-agent/token-key
cd services/gigachat-agent && bash install.sh && cd ../..

# 5. (опционально) Оффлайн-тайлы карты
#    Краснодар, zoom 5-14 (~1500 тайлов, ~25 мин с rate-limit 1 req/сек):
python3 scripts/import_tiles/download_tiles.py \
    --bbox 38.7,44.8,39.4,45.3 --zoom 5-14

# 6. systemd-юниты (autostart при включении RPi5)
sudo bash scripts/systemd/install.sh

# 7. Проверка
sudo systemctl status mesh-lora-station mesh-rescue-api mesh-gigachat-agent
curl http://localhost:8000/api/health    # {"status":"ok"}
curl http://localhost:8000/api/stats     # счётчики
curl http://127.0.0.1:8001/health        # gigachat-agent (только локально)

# Открыть в браузере: http://<rpi5-ip>:8000  — дашборд с картой и чатом
```

---

## Обновление (`git pull`)

```bash
git pull

# Если изменились зависимости — обновить venv'ы:
for svc in rescue-api gigachat-agent; do
    cd services/$svc && source .venv/bin/activate \
        && pip install -r requirements.txt && deactivate && cd ../..
done

# Перезапустить сервисы
sudo systemctl restart mesh-rescue-api mesh-gigachat-agent
# lora-station — только при правках железной логики или схемы БД
sudo systemctl restart mesh-lora-station

# Веб-фронтенд раздаётся StaticFiles — F5 в браузере (Ctrl+Shift+R чтобы
# сбросить кеш JS/CSS).
```

Авто-миграция БД срабатывает при старте `lora-station`
(см. `Database._migrate()`). Запускать `init.sh` повторно не нужно.

---

## Управление сервисами (systemd)

```bash
sudo systemctl status mesh-lora-station
sudo journalctl -u mesh-lora-station -f
sudo systemctl restart mesh-rescue-api

# Полностью отключить (на время отладки руками)
sudo systemctl disable --now mesh-lora-station

# Переустановить юнит после правки шаблона в scripts/systemd/
sudo bash scripts/systemd/install.sh mesh-rescue-api    # один сервис
sudo bash scripts/systemd/install.sh                    # все сразу
```

ENV-переменные правятся прямо в `/etc/systemd/system/mesh-*.service`
(или в шаблоне `scripts/systemd/`):
```bash
sudo systemctl edit --full mesh-rescue-api
sudo systemctl daemon-reload && sudo systemctl restart mesh-rescue-api
```

⚠ **Не запускать одновременно systemd-копию и `python -m <svc>` руками** —
обе будут биться за SPI / порт.

---

## Прошивка ESP32

```bash
cd firmware/esp32-terminal

# Если Wi-Fi/HTTPS параметры менялись (DEVICE_ID и т.п.) —
# отредактировать src/main.cpp вверху файла

# (опционально) Сгенерировать новый self-signed cert
bash scripts/gen_cert.sh    # обновит include/cert.h

# Сборка и прошивка через PlatformIO
pio run
pio run -t upload

# Мониторинг логов
pio device monitor -b 115200
```

**Подключение к терминалу:**
1. Wi-Fi: `MeshNet-016` (без пароля)
2. Браузер: `https://192.168.4.1/` — будет предупреждение про
   self-signed cert, нажать «Перейти всё равно»
3. Разрешить доступ к геолокации, чтобы PING'и шли с реальными координатами

---

## ENV-переменные

| Переменная | Сервис | Default | Описание |
|------------|--------|---------|----------|
| `GIGACHAT_AUTHORIZATION_KEY` | gigachat-agent | — | Authorization key (base64). Перетирает `token-key` |
| `GIGACHAT_SCOPE` | gigachat-agent | `GIGACHAT_API_PERS` | `_PERS` / `_B2B` / `_CORP` |
| `GIGACHAT_MODEL` | gigachat-agent | `GigaChat` | `GigaChat-Pro` / `GigaChat-Plus` |
| `GIGACHAT_TIMEOUT` | gigachat-agent | `20` | Секунды на один вызов GigaChat |
| `GIGACHAT_MAX_ITER` | gigachat-agent | `8` | Макс. итераций function calling |
| `RESCUE_API_URL` | gigachat-agent | `http://127.0.0.1:8000` | URL rescue-api для tools |
| `GIGACHAT_AGENT_URL` | rescue-api | `http://127.0.0.1:8001` | URL для прокси `/api/chat` |
| `DB_PATH` | lora-station, rescue-api | `/var/lib/mesh-net/mesh.db` | Путь к SQLite |
| `LORA_SPI_BUS` | lora-station | `0` | Шина SPI |
| `LORA_SPI_CS` | lora-station | `8` | BCM GPIO для CS (дёргаем сами) |
| `LORA_RESET_PIN` | lora-station | `22` | BCM GPIO для NRST |
| `LORA_DIO1_PIN` | lora-station | `23` | BCM GPIO для DIO1 |
| `LORA_BUSY_PIN` | lora-station | `24` | BCM GPIO для BUSY |
| `NODE_DEVICE_ID` | lora-station, rescue-api | `0x0001` | ID этого узла (база/инфо-точка) |
| `BASE_DEVICE_NAME` | rescue-api | `База спасателей` | Имя при первой записи в `devices` |
| `RESCUE_API_HOST` | rescue-api | `0.0.0.0` | Интерфейс |
| `RESCUE_API_PORT` | rescue-api | `8000` | Порт |
| `TILES_DIR` | rescue-api | `/var/lib/mesh-net/tiles` | Каталог оффлайн-тайлов |
| `DASHBOARD_DIR` | rescue-api | `<repo>/web/rescue-dashboard` | Статика дашборда |
| `ALLOW_CORS` | rescue-api | `1` | Открыть CORS (для разработки) |
| `LOG_LEVEL` | все | `INFO` | Уровень логов |

---

## Архитектура развёртывания

**База спасателей** — три сервиса на одном RPi5:
- `mesh-lora-station` — SPI к HT-RA62, RX/TX, БД
- `mesh-rescue-api` — REST + WS + StaticFiles (дашборд) + tiles
- `mesh-gigachat-agent` — на 127.0.0.1, наружу не светится

**nginx не используется**: rescue-api сам раздаёт статику дашборда через
FastAPI `StaticFiles`. Это упрощает деплой: один сервис, один порт.
Если когда-нибудь понадобится HTTPS / basic-auth / rate-limit — поставить
nginx как reverse proxy и завернуть `/api/*` + `/ws` + `/` через него.

**Инфо-точка** *(этап 5)* — отдельный RPi5 с упрощённым стэком:
`mesh-lora-station` (с другим NODE_DEVICE_ID) + `mesh-relay-node` +
`nginx` + `dnsmasq` для captive Wi-Fi портала.

---

## Проверка работоспособности

```bash
# Статус всех сервисов
sudo systemctl status "mesh-*"

# Последние 50 строк логов
sudo journalctl -u mesh-lora-station -n 50 --no-pager
sudo journalctl -u "mesh-*" -p warning --since "1 hour ago"

# REST
curl http://localhost:8000/api/health
curl http://localhost:8000/api/stats | python3 -m json.tool
curl http://localhost:8000/api/tourists | python3 -m json.tool

# WebSocket (требует wscat или python скрипт — см. rescue-api/CLAUDE.md)
python3 -c "
import asyncio, websockets, json
async def go():
    async with websockets.connect('ws://localhost:8000/ws') as ws:
        while True:
            print(json.loads(await ws.recv()))
asyncio.run(go())
"

# В БД должны быть записи через минуту-две после старта ESP32
sqlite3 /var/lib/mesh-net/mesh.db 'SELECT COUNT(*) FROM pings;'
sqlite3 /var/lib/mesh-net/mesh.db 'SELECT * FROM devices;'
```

---

## Типовые проблемы при развёртывании

| Симптом | Причина | Решение |
|---------|---------|---------|
| `pip install lgpio` тащит swig + сборку из C | lgpio не должен ставиться через pip | `apt install python3-lgpio`, venv с `--system-site-packages` (так и делает `lora-station/install.sh`) |
| `Permission denied: /dev/spidev0.0` | Юзер не в группе `spi` | `sudo usermod -aG spi,gpio $USER`, перелогиниться |
| `address already in use :8000` | systemd-копия уже работает | `sudo systemctl stop mesh-rescue-api` перед `python -m rescue_api` |
| `address already in use :443` (на ESP32) | предыдущая прошивка ещё в эфире | reset через USB или подождать reboot |
| `auth_mode=none` в `/health` gigachat | пустой `token-key` | положить Authorization key в `services/gigachat-agent/token-key` |
| Карта серая, маркеры есть | tiles не скачаны | `python3 scripts/import_tiles/download_tiles.py --bbox ... --zoom ...` |
| Дашборд ругается `L is not defined` | Leaflet не установлен | `bash web/rescue-dashboard/install.sh` (rescue-api install.sh уже это делает) |
| `mesh-lora-station` стартует, но IRQ от DIO1 не приходит | callback на RP1 залипает | в коде есть SPI-fallback (poll IRQ-регистра каждые 50 мс), это норма |
