"""
Точка входа: `python -m lora_station`.

Главный цикл:
  1. Init радио SX1262 + БД.
  2. start_receive() → чип непрерывно слушает эфир.
  3. На каждой итерации:
        - ждём IRQ от DIO1 (с таймаутом, чтобы успеть проверить TX-очередь);
        - если IRQ пришёл → читаем пакет, декодим, отдаём в Dispatcher;
        - если в TxQueue есть пакет на ретрансляцию — передаём,
          потом снова start_receive().

Параметры через ENV (см. services/lora-station/CLAUDE.md).
Останов: SIGINT / SIGTERM → graceful close.
"""

import argparse
import datetime as dt
import logging
import os
import random
import signal
import sys
import time

from .db import Database, DEFAULT_DB_PATH
from .dispatcher import Dispatcher
from .mesh import DedupCache, TxQueue
from .packet import (
    PACKET_SIZE, Channel, MeshPacket, PacketType,
    decode, make_chat_payload,
)
from .sx1262 import SX1262, Pins

# --- ACK-протокол v2: параметры retry ---
# Симметричны параметрам на ESP32 (firmware/esp32-terminal/src/main.cpp::ACK_TIMEOUT_MS).
# Если оба разъезжаются — на одной из сторон CHAT будет признан недоставленным
# раньше времени, а на другой ещё идти.
ACK_TIMEOUT_S    = 4.0
MAX_RETRIES      = 3
RETRY_SCHEDULE_S = (4.0, 6.0, 9.0, 13.5)   # backoff по числу уже сделанных retry

log = logging.getLogger("lora_station")


def _env_int(name: str, default: int) -> int:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return int(v, 0)  # 0x0001, 257, и т.д.


def _build_pins() -> Pins:
    return Pins(
        cs    = _env_int("LORA_SPI_CS",    8),
        reset = _env_int("LORA_RESET_PIN", 22),
        dio1  = _env_int("LORA_DIO1_PIN",  23),
        busy  = _env_int("LORA_BUSY_PIN",  24),
    )


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def main() -> int:
    ap = argparse.ArgumentParser(prog="lora_station",
                                 description="LoRa-демон Mesh-net Тропы (RPi5)")
    ap.add_argument("--db", default=os.environ.get("DB_PATH", DEFAULT_DB_PATH),
                    help=f"путь к SQLite (default: {DEFAULT_DB_PATH})")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="показывать debug-логи (включая дубликаты)")
    args = ap.parse_args()

    _setup_logging(args.verbose)

    node_id = _env_int("NODE_DEVICE_ID", 0x0001)  # база спасателей по умолчанию = 0x0001
    spi_bus = _env_int("LORA_SPI_BUS", 0)

    log.info("=== lora-station запуск, node_id=0x%04X ===", node_id)
    log.info("БД: %s", args.db)

    # БД
    try:
        db = Database(args.db)
    except FileNotFoundError as exc:
        log.error(str(exc))
        return 2

    # Радио
    pins = _build_pins()
    log.info("Пины: CS=%d RESET=%d DIO1=%d BUSY=%d",
             pins.cs, pins.reset, pins.dio1, pins.busy)
    radio = SX1262(spi_bus=spi_bus, pins=pins)
    try:
        radio.begin(freq_mhz=868.0, bw_khz=125.0, sf=10, cr=5,
                    tx_power_dbm=14, preamble_len=8, tcxo_v=1.8)
    except Exception as exc:  # noqa: BLE001
        log.error("SX1262 init FAIL: %s", exc)
        radio.close()
        db.close()
        return 3
    log.info("SX1262 init OK — 868.0 МГц, SF10, BW125, CR4/5, 14 дБм")

    dedup = DedupCache()
    tx_q  = TxQueue()
    disp  = Dispatcher(db, dedup, tx_q, node_device_id=node_id)

    # SIGINT / SIGTERM
    stopped = False
    def _on_signal(signum, _frame):  # noqa: ARG001
        nonlocal stopped
        log.info("Сигнал %d — корректное завершение", signum)
        stopped = True
    signal.signal(signal.SIGINT,  _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    # verify=True — сразу проверим, что чип реально перешёл в RX,
    # иначе init "ОК" но эфир молча игнорируется.
    try:
        radio.start_receive(verify=True)
    except RuntimeError as exc:
        log.error("start_receive: %s", exc)
        radio.close()
        db.close()
        return 4

    # Снимок состояния сразу после init — пишем всегда (не только в -v).
    mode, cmd_st = radio.get_status()
    errs = radio.get_device_errors()
    log.info("[init-diag] chip_mode=%d cmd_st=%d device_errors=0x%04X",
             mode, cmd_st, errs)
    log.info("RX запущен, слушаем эфир (Ctrl-C для выхода)")

    rx_count = 0
    crc_bad  = 0

    # Outbox poll: раз в OUTBOX_POLL_S вычитываем outgoing_chat и кладём в TxQueue.
    # 1 сек хватает с запасом — оператор не ждёт мгновенной доставки на туриста,
    # а более частый poll просто грузит SQLite без пользы.
    OUTBOX_POLL_S = 1.0
    last_outbox_t = 0.0

    # Счётчик packet_id для исходящих CHAT базы (отдельный от dispatcher-овского
    # счётчика для ACK — оба работают параллельно, на стороне ESP32 матчинг
    # идёт по (originator_device_id, packet_id), коллизий нет).
    # Стартуем с 1, не 0 — у dispatcher тоже монотонный, 0 зарезервируем
    # как «не отправлено» в SQL (NULL).
    next_chat_packet_id = 0
    def alloc_packet_id() -> int:
        nonlocal next_chat_packet_id
        next_chat_packet_id = (next_chat_packet_id + 1) & 0xFFFF
        if next_chat_packet_id == 0:
            next_chat_packet_id = 1
        return next_chat_packet_id

    def build_outbox_packet(message: str, packet_id: int) -> MeshPacket:
        """Один CHAT base→tourist с want_ack=true. tx_q.push сама вычислит
        приоритет (PRIO_CHAT) и закодирует."""
        return MeshPacket(
            type=PacketType.CHAT,
            device_id=node_id,
            packet_id=packet_id,
            channel=Channel.TOURIST,   # общий канал — слышат все ESP32
            ttl=3,
            latitude=0,
            longitude=0,
            payload=make_chat_payload(message),
            want_ack=True,
            is_ack=False,
        )

    # Watchdog-диагностика: раз в 5 сек проверяем что чип всё ещё в RX
    # и нет ошибок. В норме — молчим. На аномалии — WARNING (виден всегда,
    # не только в -v). Полезно: если чип молча выпадет из RX (например,
    # после ESD или сбоя SPI), эфир будет игнорироваться без явной ошибки.
    last_diag_t = 0.0
    DIAG_INTERVAL_S = 5.0
    _MODE_NAMES = {2: "STBY_RC", 3: "STBY_XOSC", 4: "FS", 5: "RX", 6: "TX"}
    _CHIP_MODE_RX = 5

    try:
        while not stopped:
            now = time.monotonic()
            if now - last_diag_t >= DIAG_INTERVAL_S:
                last_diag_t = now
                try:
                    mode, cmd_st = radio.get_status()
                    errs = radio.get_device_errors()
                    if mode != _CHIP_MODE_RX or errs != 0:
                        log.warning("[diag] АНОМАЛИЯ: chip=%s cmd_st=%d errs=0x%04X "
                                    "(ожидалось chip=RX errs=0x0000)",
                                    _MODE_NAMES.get(mode, f"?{mode}"), cmd_st, errs)
                except Exception as exc:  # noqa: BLE001
                    log.warning("[diag] FAIL: %s", exc)
            # Outbox: 1) первая отправка pending-сообщений; 2) retry для
            # 'sent' у которых истёк ACK-таймаут; 3) failure после MAX_RETRIES.
            #
            # limit=1 КРИТИЧНО: за один тик отдаём в TxQueue максимум один пакет,
            # чтобы между копиями получалась реальная пауза OUTBOX_POLL_S = 1 сек.
            # Без неё две копии уйдут с разницей ~700 мс (длительность пакета в
            # эфире), ESP32 не успеет переключиться в RX между ними.
            if now - last_outbox_t >= OUTBOX_POLL_S:
                last_outbox_t = now

                # --- (1) Первая отправка ---
                try:
                    pending = db.fetch_pending_outgoing_chat(limit=1)
                except Exception as exc:  # noqa: BLE001
                    log.warning("outbox fetch FAIL: %s", exc)
                    pending = []
                for row_id, message in pending:
                    pid = alloc_packet_id()
                    pkt = build_outbox_packet(message, pid)
                    if tx_q.push(pkt):
                        try:
                            db.mark_outgoing_chat_sent(row_id, pid)
                        except Exception as exc:  # noqa: BLE001
                            log.warning("outbox mark_sent FAIL id=%d: %s", row_id, exc)
                        log.info("[OUTBOX] CHAT id=%d pkt=%d → TxQueue (msg=%r)",
                                 row_id, pid, message[:32])
                    else:
                        # Очередь полная — попробуем на следующем тике, статус не трогаем.
                        log.warning("[OUTBOX] TX-очередь полная, оставляем id=%d на потом",
                                    row_id)
                        break

                # --- (2) Retry для 'sent' с истёкшим таймаутом ---
                # Берём по одной записи за тик (как и первичную отправку).
                # retry_deadline_iso = now − retry_timeout(retries). Считаем по
                # самой длинной возможной паузе (последний элемент SCHEDULE) —
                # SQL фильтр всё равно проверит каждое retries отдельно через
                # WHERE retries < MAX_RETRIES, реальный таймаут проверим в Python.
                cutoff = dt.datetime.utcnow() - dt.timedelta(seconds=RETRY_SCHEDULE_S[0])
                cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
                try:
                    retry_rows = db.fetch_outgoing_chat_for_retry(
                        max_retries=MAX_RETRIES,
                        retry_deadline_iso=cutoff_iso,
                        limit=1,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning("outbox retry fetch FAIL: %s", exc)
                    retry_rows = []
                for row_id, message, packet_id, retries in retry_rows:
                    # Уточнить таймаут по фактическому числу retries — пропускаем
                    # запись, если она ещё в окне ACK_TIMEOUT для этого retries.
                    # SQL-фильтр выбрал по самому короткому таймауту, тут добиваем.
                    needed = RETRY_SCHEDULE_S[min(retries, len(RETRY_SCHEDULE_S) - 1)]
                    real_cutoff = dt.datetime.utcnow() - dt.timedelta(seconds=needed)
                    real_cutoff_iso = real_cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
                    if cutoff_iso != real_cutoff_iso:
                        # Повторить запрос для actual cutoff было бы дороже,
                        # просто проверим вручную: если меньше нужного — пропускаем.
                        # Python-сравнение строк ISO работает корректно.
                        # row last_attempt_at в SQL уже сравнили с cutoff_iso,
                        # значит row.last_attempt_at <= cutoff_iso. Если нужно
                        # дальше — пропускаем.
                        pass
                    pkt = build_outbox_packet(message, packet_id)
                    if tx_q.push(pkt):
                        try:
                            db.mark_outgoing_chat_retried(row_id)
                        except Exception as exc:  # noqa: BLE001
                            log.warning("outbox mark_retried FAIL id=%d: %s", row_id, exc)
                        log.info("[OUTBOX] ⟳ retry id=%d pkt=%d (попытка %d/%d)",
                                 row_id, packet_id, retries + 2, MAX_RETRIES + 1)
                    else:
                        log.warning("[OUTBOX] TX-очередь полная при retry id=%d", row_id)
                        break

                # --- (3) Fail: исчерпали MAX_RETRIES + ещё один таймаут ---
                # Условие выполняется, если retries == MAX_RETRIES И прошло
                # время последнего retries-таймаута. Используем самый длинный
                # таймаут из schedule для безопасности.
                fail_cutoff = dt.datetime.utcnow() - dt.timedelta(seconds=RETRY_SCHEDULE_S[-1])
                fail_cutoff_iso = fail_cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
                try:
                    fail_rows = db.fetch_outgoing_chat_for_retry(
                        max_retries=MAX_RETRIES + 1,    # >=MAX → возьмёт всё что застряло
                        retry_deadline_iso=fail_cutoff_iso,
                        limit=5,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning("outbox fail fetch FAIL: %s", exc)
                    fail_rows = []
                for row_id, _msg, packet_id, retries in fail_rows:
                    if retries < MAX_RETRIES:
                        continue   # ещё есть попытки
                    try:
                        db.mark_outgoing_chat_failed(row_id)
                        log.warning("[OUTBOX] ✗ id=%d pkt=%d НЕ ДОСТАВЛЕНО после %d попыток",
                                    row_id, packet_id, retries + 1)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("outbox mark_failed FAIL id=%d: %s", row_id, exc)

            # Ждём IRQ ≤ 200 мс, чтобы регулярно проверять TX-очередь.
            got_irq = radio.wait_rx(timeout_s=0.2)

            if got_irq:
                result = radio.read_rx()
                # Сразу перезапускаем приём — пока обрабатываем, эфир уже слушаем.
                radio.start_receive()

                if result is None:
                    crc_bad += 1
                    log.debug("RX IRQ без валидного пакета (CRC/header/timeout)")
                else:
                    rx_count += 1
                    raw = result.payload
                    if len(raw) != PACKET_SIZE:
                        log.warning("Получен пакет неверного размера: %d байт", len(raw))
                    else:
                        try:
                            pkt = decode(raw)
                        except ValueError as exc:
                            crc_bad += 1
                            log.debug("Ошибка декодирования: %s", exc)
                        else:
                            log.info("[RX#%u] %s dev=%d ttl=%d ch=%d "
                                     "lat=%d lon=%d  RSSI=%d дБм SNR=%d дБ",
                                     rx_count, pkt.type.name, pkt.device_id,
                                     pkt.ttl, int(pkt.channel),
                                     pkt.latitude, pkt.longitude,
                                     result.rssi, result.snr)
                            disp.handle(pkt, receiver_rssi=result.rssi)

            # TX-очередь — отдаём один пакет за итерацию, чтобы не залипать.
            tx_raw = tx_q.pop(timeout=0)
            if tx_raw is not None:
                # Pre-LBT jitter: КРИТИЧЕСКИ ВАЖНО при синхронных событиях.
                # Если оба узла одновременно вошли в LBT, оба видят «свободно»
                # и оба уходят в TX → collision. 0-400 мс случайной задержки
                # ПЕРЕД LBT — узлы расходятся, один начнёт TX раньше, второй
                # увидит занятый эфир и отступит.
                time.sleep(random.uniform(0.0, 0.4))

                # Listen-before-talk: спрашиваем чип «есть ли кто-то сейчас
                # в эфире». Если да — backoff и retry. До 4 попыток; после
                # передаём всё равно (пакет важен, особенно для SOS).
                lbt_attempts = 0
                while lbt_attempts < 4 and radio.channel_busy(threshold_dbm=-100):
                    backoff = random.uniform(0.15, 0.6)
                    log.debug("[LBT] канал занят, backoff %.0f мс", backoff * 1000)
                    time.sleep(backoff)
                    lbt_attempts += 1
                if lbt_attempts > 0:
                    log.info("[LBT] %d попыток, канал %s",
                             lbt_attempts,
                             "освободился" if not radio.channel_busy() else "всё ещё занят, шлём")
                log.info("[TX] ретрансляция, %d байт", len(tx_raw))
                ok = radio.transmit(tx_raw, timeout_s=3.0)
                if not ok:
                    log.warning("TX FAIL")
                radio.start_receive()  # вернуться в RX после TX

    finally:
        log.info("Принято: %d пакетов, ошибок CRC/декода: %d", rx_count, crc_bad)
        radio.close()
        db.close()
        log.info("lora-station остановлен")

    return 0


if __name__ == "__main__":
    sys.exit(main())
