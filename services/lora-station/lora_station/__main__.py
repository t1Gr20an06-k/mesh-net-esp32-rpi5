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
import logging
import os
import signal
import sys
import time

from .db import Database, DEFAULT_DB_PATH
from .dispatcher import Dispatcher
from .mesh import DedupCache, TxQueue
from .packet import PACKET_SIZE, decode
from .sx1262 import SX1262, Pins

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

    radio.start_receive()
    log.info("RX запущен, слушаем эфир (Ctrl-C для выхода)")

    rx_count = 0
    crc_bad  = 0

    try:
        while not stopped:
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
