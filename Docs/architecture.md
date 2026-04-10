# Архитектура системы

## Диаграмма

```
┌─────────────────┐     LoRa 868 МГц      ┌──────────────────────┐     LoRa 868 МГц     ┌─────────────────────────┐
│  ТЕРМИНАЛ       │ ─────────────────────► │  ИНФО-ТОЧКА          │ ───────────────────► │  БАЗА СПАСАТЕЛЕЙ        │
│  ESP32 WROOM    │                        │  Raspberry Pi 5      │                      │  Raspberry Pi 5         │
│                 │                        │                      │                      │                         │
│ • SX1262        │ ◄───────────────────── │ • SX1262             │ ◄─────────────────── │ • SX1262                │
│                 │     LoRa (mesh)        │ • relay-node svc     │     LoRa (mesh)      │ • lora-station svc      │
│ • TFT 3.5"      │                        │ • lora-station svc   │                      │ • rescue-api svc       │
│ • Wi-Fi AP      │                        │ • nginx              │                      │ • gigachat-agent svc    │
│ • Кнопка SOS    │                        │ • info-portal (HTML) │                      │ • rescue-dashboard      │
└────────┬────────┘                        │ • Captive portal     │                      │ • SQLite DB             │
         │ Wi-Fi                           │                      │                      │ • Leaflet + OSM tiles   │
         ▼                                 └──────────────────────┘                      │ • GigaChat AI           │
┌─────────────────┐                                                                      └─────────────────────────┘
│  СМАРТФОН       │                                                                                  │
│  gps(из телефона)
|  tourist-web    │                                                                                  │ LAN / Wi-Fi
│  (PWA/браузер)  │                                                                      ┌─────────────────────────┐
└─────────────────┘                                                                      │  НОУТБУК СПАСАТЕЛЯ      │
                                                                                         │  rescue-dashboard       │
                                                                                         │  (браузер)              │
                                                                                         └─────────────────────────┘
```

## Компоненты

### 1. Пользовательский терминал (ESP32)

**Железо:** ESP32 WROOM + LoRa SX1262 + GPS(из телефона) + TFT ILI9488 3.5"

**Функции:**
- Периодическая отправка PING с GPS-координатами (каждые 60 с)
- Аппаратная кнопка SOS → отправка SOS-пакета × 3
- Wi-Fi Access Point: SSID `TrailMesh-{device_id}`, IP `192.168.4.1`
- WebSocket-сервер на `ws://192.168.4.1:81` → интерфейс `tourist-web`
- Ретрансляция входящих LoRa пакетов (mesh)
- Дисплей: карта участка, список устройств в сети, чат

**Прошивка:** PlatformIO, Arduino framework, C++17

### 2. Инфо-точка (RPi5)

**Железо:** Raspberry Pi 5 8ГБ + SX1262 (SPI)

**Сервисы:**
- `mesh-lora-station` — приём/передача LoRa пакетов
- `mesh-relay-node` — ретрансляция, логирование в local SQLite (без GigaChat)
- `nginx` — captive portal redirect + раздача `info-portal`
- `dnsmasq` — DHCP + DNS для captive portal

**Wi-Fi:** Hotspot `Тропа-[Название]`, автоматический redirect на портал

**Captive portal (`info-portal`):** статический HTML, офлайн-карта, фото/описания локации, кнопка SOS

### 3. База спасателей (RPi5)

**Железо:** Raspberry Pi 5 8ГБ + SX1262 (SPI) + монитор/ноутбук по LAN

**Сервисы:**

| Сервис | Порт | Описание |
|--------|------|----------|
| `mesh-lora-station` | — | Демон SPI-связи с SX1262 |
| `mesh-rescue-api` | 8000 | FastAPI REST + WebSocket |
| `mesh-gigachat-agent` | 8001 | GigaChat proxy + function calling |
| `nginx` | 80 | Reverse proxy + раздача `rescue-dashboard` |

**SQLite** `/var/lib/mesh-net/mesh.db` — все треки, события SOS, сообщения

---

## Потоки данных

### Трекинг (штатный режим)

```
ESP32 → [LoRa PING] → Инфо-точка → [LoRa] → База спасателей
                          │                        │
                     relay-node              lora-station
                          │                        │
                     local log                 SQLite tracks
                                                   │
                                            rescue-api WS
                                                   │
                                         rescue-dashboard (карта)
```

### SOS

```
Турист нажимает кнопку
    │
ESP32 → [LoRa SOS × 3] → все узлы в радиусе → База спасателей
                                                    │
                                              lora-station
                                              (приоритетная обработка)
                                                    │
                                              SQLite sos_events
                                                    │
                                           rescue-api → WS broadcast
                                                    │
                                        rescue-dashboard: ALARM
                                        звуковой сигнал + маркер на карте
                                                    │
                                            gigachat-agent:
                                            автоматическое сообщение
```

### Запрос к GigaChat

```
Спасатель вводит вопрос в rescue-dashboard
    │
POST /gigachat/ask {"question": "Сколько туристов на маршруте?"}
    │
gigachat-agent → GigaChat API (если есть интернет) или local stub
    │
Function calling: get_tourists() → rescue-api → SQLite
    │
Ответ: "На маршруте 7 активных туристов, последний PING 3 минуты назад"
```

---

## Безопасность

- **RESCUE-канал** фильтруется по whitelist `device_id` — туристские терминалы не могут отправлять служебные пакеты
- **SQLite** не экспортируется наружу, только через `rescue-api`
- **GigaChat API** — только на базе спасателей, токен в переменной окружения
- **Wi-Fi портал** не требует авторизации (публичный доступ для туристов)

---

## Масштабирование

Для покрытия длинного маршрута (> 10 км) добавляются промежуточные инфо-точки. Каждая точка автоматически участвует в mesh при включении — дополнительная настройка не требуется (flooding mesh, TTL=3 покрывает 3 прыжка = ~15–30 км).
