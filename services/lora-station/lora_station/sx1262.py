"""
Драйвер SX1262 (Semtech) для модуля HT-RA62 на Raspberry Pi 5.

Pure-Python поверх:
  * spidev      — SPI обмен с чипом (/dev/spidev0.0)
  * gpiozero    — RESET / BUSY / DIO1 (на RPi5 gpiozero сам подхватит lgpio)

Параметры радио должны 1-в-1 совпадать с прошивкой ESP32 и проверенным
C++ снифером (tests/field/lora-sniffer/main.cpp):
    868 МГц, SF=10, BW=125 кГц, CR=4/5, preamble=8, TX power=14 дБм,
    TCXO=1.8 В, sync word=PRIVATE, CRC=2, DIO2 как RF-switch.

Драйвер не претендует на полноту RadioLib — реализуем ровно то, что
нужно для непрерывного RX, передачи 64-байтных пакетов и чтения
RSSI/SNR. Datasheet: Semtech SX1261/2 v2.1.
"""

import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

import lgpio

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Опкоды команд SX1262 (datasheet §13)
# ---------------------------------------------------------------------------
_CMD_SET_SLEEP                = 0x84
_CMD_SET_STANDBY              = 0x80
_CMD_SET_TX                   = 0x83
_CMD_SET_RX                   = 0x82
_CMD_SET_REGULATOR_MODE       = 0x96
_CMD_CALIBRATE                = 0x89
_CMD_CALIBRATE_IMAGE          = 0x98
_CMD_SET_PA_CONFIG            = 0x95
_CMD_WRITE_REGISTER           = 0x0D
_CMD_READ_REGISTER            = 0x1D
_CMD_WRITE_BUFFER             = 0x0E
_CMD_READ_BUFFER              = 0x1E
_CMD_SET_DIO_IRQ_PARAMS       = 0x08
_CMD_GET_IRQ_STATUS           = 0x12
_CMD_CLEAR_IRQ_STATUS         = 0x02
_CMD_SET_DIO2_AS_RF_SWITCH    = 0x9D
_CMD_SET_DIO3_AS_TCXO_CTRL    = 0x97
_CMD_SET_RF_FREQUENCY         = 0x86
_CMD_SET_PACKET_TYPE          = 0x8A
_CMD_SET_TX_PARAMS            = 0x8E
_CMD_SET_MODULATION_PARAMS    = 0x8B
_CMD_SET_PACKET_PARAMS        = 0x8C
_CMD_SET_BUFFER_BASE_ADDRESS  = 0x8F
_CMD_GET_RX_BUFFER_STATUS     = 0x13
_CMD_GET_PACKET_STATUS        = 0x14
_CMD_GET_DEVICE_ERRORS        = 0x17
_CMD_CLEAR_DEVICE_ERRORS      = 0x07
_CMD_GET_STATUS               = 0xC0

# Standby modes
_STDBY_RC   = 0x00
_STDBY_XOSC = 0x01

# Regulator
_REG_DCDC = 0x01

# Packet types
_PKT_TYPE_LORA = 0x01

# IRQ-биты (16 бит)
IRQ_TX_DONE          = 0x0001
IRQ_RX_DONE          = 0x0002
IRQ_HEADER_VALID     = 0x0010
IRQ_HEADER_ERROR     = 0x0020
IRQ_CRC_ERROR        = 0x0040
IRQ_TIMEOUT          = 0x0200
IRQ_ALL              = 0x03FF

# Регистры
_REG_LORA_SYNC_WORD_MSB = 0x0740
_REG_LORA_SYNC_WORD_LSB = 0x0741

# Sync word: PRIVATE — для совместимости с обычной LoRa (не LoRaWAN).
# RadioLib SX126X_SYNC_WORD_PRIVATE = 0x12 → MSB nibble 0x1, LSB nibble 0x2,
# реально регистры заполняются 0x14/0x24.
_SYNC_WORD_PRIVATE_MSB = 0x14
_SYNC_WORD_PRIVATE_LSB = 0x24

# TCXO voltage
_TCXO_1_8V = 0x02

# Модуляция: BW таблица из datasheet (для LoRa)
_BW_125 = 0x04
# CR: 4/5 = 1
_CR_4_5 = 0x01

# Калибровка image для 868 МГц (datasheet §13.1.10)
_CAL_IMG_868_F1 = 0xD7
_CAL_IMG_868_F2 = 0xDB

# Базовые адреса буферов
_TX_BASE_ADDR = 0x00
_RX_BASE_ADDR = 0x00


@dataclass
class RxResult:
    payload: bytes
    rssi: int   # дБм (отрицательное)
    snr:  int   # дБ * 4 / 4 → конвертим в дБ


@dataclass
class Pins:
    """BCM-номера GPIO. Defaults — те же, что и в C++ снифере."""
    cs:     int = 8       # SPI0 CE0 — управляется ядром, но spidev умеет manual
    reset:  int = 22
    dio1:   int = 23
    busy:   int = 24


class SX1262:
    """
    Минимальный драйвер SX1262 для непрерывного RX и одиночных TX.

    Используется в одном потоке (главный цикл), но IRQ от DIO1 ставит
    threading.Event, который главный поток ждёт через wait().
    """

    def __init__(
        self,
        spi_bus: int = 0,
        spi_dev: int = 0,
        spi_speed_hz: int = 1_000_000,
        pins: Pins = Pins(),
    ):
        # GPIO + SPI — всё через lgpio (libgpiod), как делает RadioLib PiHal.
        # ВАЖНО: на этой системе config.txt переназначил hardware CS0 на
        # GPIO 27 через `dtoverlay=spi0-1cs,cs0_pin=27`, но HT-RA62 физически
        # подключён к GPIO 8. Поэтому CS дёргаем сами через GPIO, а
        # автоматический CS от kernel driver пусть свободно дёргает GPIO 27
        # в пустоту — нам не мешает.
        self._chip = lgpio.gpiochip_open(0)
        self._reset_gpio = pins.reset
        self._busy_gpio  = pins.busy
        self._dio1_gpio  = pins.dio1
        self._cs_gpio    = pins.cs

        lgpio.gpio_claim_output(self._chip, self._reset_gpio, 1)
        lgpio.gpio_claim_output(self._chip, self._cs_gpio, 1)   # CS inactive (HIGH)
        lgpio.gpio_claim_input(self._chip, self._busy_gpio)
        lgpio.gpio_claim_alert(self._chip, self._dio1_gpio, lgpio.RISING_EDGE)
        self._dio1_cb = lgpio.callback(
            self._chip, self._dio1_gpio, lgpio.RISING_EDGE, self._on_dio1_rising
        )

        # Событие "DIO1 поднялся" — IRQ от чипа (RxDone / TxDone / etc.)
        self._irq_event = threading.Event()

        # SPI через lgpio. Hardware-CS уйдёт на GPIO 27 (overlay), но мы его
        # не используем — управляем CS вручную через GPIO 8 в _spi_xfer.
        self._spi_handle = lgpio.spi_open(spi_bus, spi_dev, spi_speed_hz, 0)
        if self._spi_handle < 0:
            raise OSError(f"lgpio.spi_open вернул ошибку: {self._spi_handle}")
        log.debug("SPI открыт через lgpio: bus=%d dev=%d speed=%d handle=%d, "
                  "ручной CS на GPIO %d",
                  spi_bus, spi_dev, spi_speed_hz, self._spi_handle, self._cs_gpio)

        # SPI bus — внешне один, но обращаемся из главного потока + IRQ-callback;
        # на всякий случай защитим Lock'ом.
        self._spi_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Низкий уровень: SPI + BUSY
    # ------------------------------------------------------------------
    def _busy_value(self) -> int:
        return lgpio.gpio_read(self._chip, self._busy_gpio)

    def _wait_busy_low(self, timeout_s: float = 1.0) -> None:
        t0 = time.monotonic()
        while self._busy_value() == 1:
            if time.monotonic() - t0 > timeout_s:
                raise TimeoutError("SX1262 BUSY висит — нет ответа от чипа")
            time.sleep(0.0001)

    def _spi_xfer(self, data: list[int]) -> list[int]:
        # Ручной CS: LOW (active) → xfer → HIGH (inactive). Параллельно
        # kernel дёргает hardware CS на GPIO 27 в пустоту, нам не мешает.
        with self._spi_lock:
            lgpio.gpio_write(self._chip, self._cs_gpio, 0)
            try:
                count, rx = lgpio.spi_xfer(self._spi_handle, list(data))
            finally:
                lgpio.gpio_write(self._chip, self._cs_gpio, 1)
        rx_list = list(rx)
        if log.isEnabledFor(logging.DEBUG):
            tx_hex = " ".join(f"{b:02X}" for b in data)
            rx_hex = " ".join(f"{b:02X}" for b in rx_list)
            log.debug("SPI tx=%s rx=%s", tx_hex, rx_hex)
        return rx_list

    def _cmd(self, opcode: int, params: bytes = b'', read_len: int = 0) -> bytes:
        """
        Послать команду. Если read_len > 0 — после параметров отправляем
        столько же 0x00, и ответ пишется чипом в эти позиции.
        """
        self._wait_busy_low()
        tx = [opcode] + list(params) + [0x00] * read_len
        rx = self._spi_xfer(tx)
        # Полезная часть ответа после opcode + len(params).
        return bytes(rx[1 + len(params):]) if read_len else b''

    def _write_register(self, addr: int, value: int) -> None:
        self._write_register_burst(addr, bytes([value & 0xFF]))

    def _read_register(self, addr: int) -> int:
        return self._read_register_burst(addr, 1)[0]

    def _write_register_burst(self, addr: int, data: bytes) -> None:
        """WriteRegister (0x0D) с N последовательных байт — как делает RadioLib."""
        self._cmd(_CMD_WRITE_REGISTER,
                  bytes([(addr >> 8) & 0xFF, addr & 0xFF]) + data)

    def _read_register_burst(self, addr: int, n: int) -> bytes:
        """
        ReadRegister (0x1D): TX = opcode | addr_hi | addr_lo | NOP | NOP*n,
        data приходит на байтах с 4-го (нумерация с 0).
        """
        self._wait_busy_low()
        tx = [_CMD_READ_REGISTER, (addr >> 8) & 0xFF, addr & 0xFF, 0x00] + [0x00] * n
        rx = self._spi_xfer(tx)
        return bytes(rx[4:4 + n])

    def _write_buffer(self, offset: int, data: bytes) -> None:
        self._cmd(_CMD_WRITE_BUFFER, bytes([offset & 0xFF]) + data)

    def _read_buffer(self, offset: int, length: int) -> bytes:
        # Layout: opcode 1E | offset | NOP | <length bytes>
        self._wait_busy_low()
        tx = [_CMD_READ_BUFFER, offset & 0xFF, 0x00] + [0x00] * length
        rx = self._spi_xfer(tx)
        return bytes(rx[3:3 + length])

    # ------------------------------------------------------------------
    # IRQ
    # ------------------------------------------------------------------
    def _on_dio1_rising(self, chip, gpio, level, tick) -> None:  # noqa: ARG002
        # Вызывается потоком lgpio при rising edge на DIO1.
        # Никакого SPI здесь — только взвести event для главного цикла.
        if level == 1:
            self._irq_event.set()

    def _read_irq_status(self) -> int:
        # Layout: opcode 12 | NOP | <stat> | <stat>
        self._wait_busy_low()
        rx = self._spi_xfer([_CMD_GET_IRQ_STATUS, 0x00, 0x00, 0x00])
        return (rx[2] << 8) | rx[3]

    def _clear_irq(self, mask: int = IRQ_ALL) -> None:
        self._cmd(_CMD_CLEAR_IRQ_STATUS,
                  bytes([(mask >> 8) & 0xFF, mask & 0xFF]))

    # ------------------------------------------------------------------
    # Высокий уровень: init
    # ------------------------------------------------------------------
    def reset(self) -> None:
        """
        Аппаратный сброс + перевод в STDBY_RC.

        После reset SX126x проходит startup-sequence ~3.5 мс на внутреннем
        RC-oscillator. Сразу после этого первый GET_STATUS может вернуть
        мусор (mode=0 = UNUSED) — чип реально перейдёт в STDBY_RC только
        после команды SetStandby. Поэтому делаем reset → 25 мс пауза →
        SetStandby(RC) → проверяем mode=2 (STDBY_RC), несколько попыток.
        """
        lgpio.gpio_write(self._chip, self._reset_gpio, 1)
        time.sleep(0.001)
        log.debug("reset: BUSY перед reset = %d", self._busy_value())
        lgpio.gpio_write(self._chip, self._reset_gpio, 0)   # LOW — активный reset
        time.sleep(0.010)
        log.debug("reset: BUSY во время LOW reset = %d", self._busy_value())
        lgpio.gpio_write(self._chip, self._reset_gpio, 1)   # release
        time.sleep(0.025)
        log.debug("reset: BUSY после release = %d (ждём LOW)", self._busy_value())
        self._wait_busy_low(timeout_s=2.0)
        log.debug("reset: BUSY стал LOW")

        last_mode = -1
        for attempt in range(5):
            self._cmd(_CMD_SET_STANDBY, bytes([_STDBY_RC]))
            time.sleep(0.002)
            self._wait_busy_low()
            mode, cmd_st = self.get_status()
            log.debug("reset attempt %d: chip_mode=%d cmd_st=%d",
                      attempt + 1, mode, cmd_st)
            if mode == 2:  # STDBY_RC — то, что нужно
                return
            last_mode = mode
            time.sleep(0.020)

        raise RuntimeError(
            f"Чип не выходит в STDBY_RC после reset (chip_mode={last_mode}). "
            "Проверь питание 3.3 В, проводку SPI/RESET, и что снифер не запущен."
        )

    def begin(
        self,
        freq_mhz: float = 868.0,
        bw_khz:   float = 125.0,
        sf:       int   = 10,
        cr:       int   = 5,
        tx_power_dbm: int = 14,
        preamble_len: int = 8,
        tcxo_v:   float = 1.8,
    ) -> None:
        if bw_khz != 125.0:
            raise NotImplementedError("Сейчас поддерживаем только BW=125 кГц")
        if cr != 5:
            raise NotImplementedError("Сейчас поддерживаем только CR=4/5")

        # reset() уже переводит чип в STDBY_RC и проверяет это.
        self.reset()

        # 2) TCXO control. На HT-RA62 кварц через DIO3 = TCXO 1.8 В.
        if abs(tcxo_v - 1.8) > 0.01:
            raise NotImplementedError("Сейчас захардкожен TCXO=1.8 В (HT-RA62)")
        # delay 64 мс = 64000 / 15.625 = 4096 = 0x001000
        self._cmd(_CMD_SET_DIO3_AS_TCXO_CTRL,
                  bytes([_TCXO_1_8V, 0x00, 0x10, 0x00]))

        # 3) Сброс ошибок устройства, иначе get_device_errors потом мусорит
        self._cmd(_CMD_CLEAR_DEVICE_ERRORS, bytes([0x00, 0x00]))

        # 4) Калибровка всего (datasheet §13.1.12, mask=0x7F = все блоки).
        # После CALIBRATE BUSY поднимается высоко на ~3.5 мс (RC64k+RC13M),
        # ждём с запасом; затем BUSY low.
        self._cmd(_CMD_CALIBRATE, bytes([0x7F]))
        time.sleep(0.025)
        self._wait_busy_low(timeout_s=2.0)

        # 5) Регулятор — DC-DC (на HT-RA62 катушка стоит)
        self._cmd(_CMD_SET_REGULATOR_MODE, bytes([_REG_DCDC]))

        # 6) Базовые адреса буферов
        self._cmd(_CMD_SET_BUFFER_BASE_ADDRESS,
                  bytes([_TX_BASE_ADDR, _RX_BASE_ADDR]))

        # 7) Тип пакета: LoRa
        self._cmd(_CMD_SET_PACKET_TYPE, bytes([_PKT_TYPE_LORA]))

        # 8) Частота. freq_raw = freq * 2^25 / 32_000_000
        freq_raw = int(freq_mhz * 1_000_000 * (1 << 25) / 32_000_000)
        self._cmd(_CMD_SET_RF_FREQUENCY, bytes([
            (freq_raw >> 24) & 0xFF,
            (freq_raw >> 16) & 0xFF,
            (freq_raw >>  8) & 0xFF,
            (freq_raw      ) & 0xFF,
        ]))

        # 9) Calibrate image для 868 МГц
        self._cmd(_CMD_CALIBRATE_IMAGE, bytes([_CAL_IMG_868_F1, _CAL_IMG_868_F2]))

        # 10) PA config для SX1262 на 14 дБм (datasheet table 13-21)
        # paDutyCycle=0x02, hpMax=0x02, deviceSel=0x00 (SX1262), paLut=0x01
        self._cmd(_CMD_SET_PA_CONFIG, bytes([0x02, 0x02, 0x00, 0x01]))

        # 11) Tx params: power 14 dBm, ramp 200 мкс (0x04)
        self._cmd(_CMD_SET_TX_PARAMS, bytes([tx_power_dbm & 0xFF, 0x04]))

        # 12) Modulation params: SF / BW / CR / low_data_rate_optim
        # LDRO = 1 если symbol_time > 16 мс (SF=11/12 на BW=125). У нас SF=10 → 0.
        ldro = 0
        self._cmd(_CMD_SET_MODULATION_PARAMS,
                  bytes([sf & 0xFF, _BW_125, _CR_4_5, ldro]))

        # 13) Packet params: preamble (16 bit) | header_type=variable | payload | CRC on | iq=normal
        self._cmd(_CMD_SET_PACKET_PARAMS, bytes([
            (preamble_len >> 8) & 0xFF,
            preamble_len & 0xFF,
            0x00,         # header_type: variable (explicit) — длину передаём
            64,           # payload length, max — наш фиксированный размер
            0x01,         # CRC включён (LoRa-CRC)
            0x00,         # IQ нормальный
        ]))

        # 14) Sync word PRIVATE — burst-запись (как RadioLib), затем read-back
        self._write_register_burst(
            _REG_LORA_SYNC_WORD_MSB,
            bytes([_SYNC_WORD_PRIVATE_MSB, _SYNC_WORD_PRIVATE_LSB]),
        )
        sw = self._read_register_burst(_REG_LORA_SYNC_WORD_MSB, 2)
        if sw[0] != _SYNC_WORD_PRIVATE_MSB or sw[1] != _SYNC_WORD_PRIVATE_LSB:
            raise RuntimeError(
                f"Sync word не записался: записали 0x{_SYNC_WORD_PRIVATE_MSB:02X}/"
                f"0x{_SYNC_WORD_PRIVATE_LSB:02X}, прочитали 0x{sw[0]:02X}/0x{sw[1]:02X}"
            )

        # 15) DIO2 как RF switch — модули HT-RA62 используют это для T/R-переключения
        self._cmd(_CMD_SET_DIO2_AS_RF_SWITCH, bytes([0x01]))

        # 16) Workaround §15.2 datasheet — лучшая устойчивость к рассогласованию антенны
        prev = self._read_register(0x08D8)
        self._write_register(0x08D8, prev | 0x1E)

        # 17) Маршрутизация IRQ: DIO1 = RxDone | TxDone | Timeout | CRC error
        irq_mask = IRQ_RX_DONE | IRQ_TX_DONE | IRQ_TIMEOUT | IRQ_CRC_ERROR
        self._cmd(_CMD_SET_DIO_IRQ_PARAMS, bytes([
            (irq_mask >> 8) & 0xFF, irq_mask & 0xFF,   # IRQ mask
            (irq_mask >> 8) & 0xFF, irq_mask & 0xFF,   # DIO1
            0x00, 0x00,                                # DIO2 (мы используем как RF switch)
            0x00, 0x00,                                # DIO3 (мы используем как TCXO)
        ]))
        self._clear_irq()

    # ------------------------------------------------------------------
    # Высокий уровень: RX
    # ------------------------------------------------------------------
    def start_receive(self, verify: bool = False) -> None:
        """
        Включить непрерывный RX (timeout 0xFFFFFF = continuous).
        При verify=True проверим, что чип реально в RX (chip_mode=5).
        """
        self._irq_event.clear()
        self._clear_irq()
        self._cmd(_CMD_SET_RX, bytes([0xFF, 0xFF, 0xFF]))
        if verify:
            time.sleep(0.001)
            mode, _ = self.get_status()
            if mode != 5:
                raise RuntimeError(
                    f"setRx не перевёл чип в RX (chip_mode={mode}). "
                    f"errs=0x{self.get_device_errors():04X}"
                )

    def wait_rx(self, timeout_s: Optional[float] = None,
                poll_irq: bool = True, poll_interval_s: float = 0.05) -> bool:
        """
        Ждать готовности RX. Возвращает True если IRQ обнаружено.

        Опрашиваем DIO1 через gpiozero (быстрый путь) И параллельно
        периодически читаем IRQ-регистр через SPI (fallback). На RPi5
        gpiozero+lgpio иногда не ловит rising edge от SX1262 — polling
        SPI это компенсирует, ценой ~5–20 мкс SPI-обмена раз в 50 мс.
        """
        if not poll_irq:
            return self._irq_event.wait(timeout=timeout_s)

        deadline = None if timeout_s is None else (time.monotonic() + timeout_s)
        while True:
            # Быстрый путь: callback от gpiozero уже взвёл event.
            wait_left = poll_interval_s
            if deadline is not None:
                wait_left = min(wait_left, max(0.0, deadline - time.monotonic()))
            if self._irq_event.wait(timeout=wait_left):
                return True
            # SPI-polling: чип реально что-то поймал?
            irq = self._read_irq_status()
            if irq & (IRQ_RX_DONE | IRQ_TX_DONE | IRQ_TIMEOUT |
                      IRQ_CRC_ERROR | IRQ_HEADER_ERROR):
                # Эмулируем "пришло прерывание" — read_rx разберёт irq и сбросит.
                self._irq_event.set()
                return True
            if deadline is not None and time.monotonic() >= deadline:
                return False

    def get_status(self) -> tuple[int, int]:
        """
        GET_STATUS (0xC0): вернуть (chip_mode, cmd_status).
        chip_mode: 2=STBY_RC, 3=STBY_XOSC, 4=FS, 5=RX, 6=TX
        cmd_status: 1=RFU, 2=Data available, 3=Cmd timeout, 4=Cmd error,
                    5=Failure to execute, 6=TX done.

        По datasheet §13.2 status-байт SX126x приходит ПЕРВЫМ на MISO,
        одновременно с opcode на MOSI. Поэтому читаем rx[0], а не rx[1].
        """
        self._wait_busy_low()
        rx = self._spi_xfer([_CMD_GET_STATUS, 0x00])
        st = rx[0]
        return ((st >> 4) & 0x07, (st >> 1) & 0x07)

    def get_device_errors(self) -> int:
        """GetDeviceErrors (0x17): 16-битная маска ошибок."""
        self._wait_busy_low()
        rx = self._spi_xfer([_CMD_GET_DEVICE_ERRORS, 0x00, 0x00, 0x00])
        return ((rx[2] & 0xFF) << 8) | (rx[3] & 0xFF)

    def get_irq_raw(self) -> int:
        """Сырое значение IRQ-регистра (без сброса) — для отладки."""
        return self._read_irq_status()

    def read_rx(self) -> Optional[RxResult]:
        """
        Прочитать пакет после прихода RxDone IRQ.
        Возвращает None если был CRC error / timeout / лишний IRQ.
        Сам по себе НЕ перезапускает RX — это делает caller.
        """
        self._irq_event.clear()
        irq = self._read_irq_status()
        self._clear_irq()

        if irq & (IRQ_CRC_ERROR | IRQ_HEADER_ERROR | IRQ_TIMEOUT):
            return None
        if not (irq & IRQ_RX_DONE):
            return None

        # GET_RX_BUFFER_STATUS: payload_len, buf_start_ptr
        self._wait_busy_low()
        rx = self._spi_xfer([_CMD_GET_RX_BUFFER_STATUS, 0x00, 0x00, 0x00])
        payload_len = rx[2]
        start_ptr   = rx[3]
        payload = self._read_buffer(start_ptr, payload_len)

        # GET_PACKET_STATUS: rssi_pkt, snr_pkt, signal_rssi
        self._wait_busy_low()
        rx = self._spi_xfer([_CMD_GET_PACKET_STATUS, 0x00, 0x00, 0x00, 0x00])
        rssi_raw = rx[2]   # uint8: rssi = -rssi_raw / 2
        snr_raw  = rx[3]   # int8 : snr  =  snr_raw / 4
        rssi = -rssi_raw // 2
        snr  = (snr_raw if snr_raw < 128 else snr_raw - 256) // 4

        return RxResult(payload=payload, rssi=rssi, snr=snr)

    # ------------------------------------------------------------------
    # Высокий уровень: TX
    # ------------------------------------------------------------------
    def transmit(self, data: bytes, timeout_s: float = 5.0) -> bool:
        """
        Передать data, дождаться TxDone IRQ. Возвращает True при успехе.
        После TX чип переходит в STDBY_RC — caller должен снова вызвать
        start_receive() если хочет продолжать слушать.
        """
        if len(data) == 0 or len(data) > 255:
            raise ValueError(f"Длина пакета вне диапазона: {len(data)}")

        # Перевод payload_length для корректной TX (variable header сам передаст).
        # Меняем только payload_len в SET_PACKET_PARAMS.
        self._cmd(_CMD_SET_PACKET_PARAMS, bytes([
            0x00, 0x08,        # preamble = 8
            0x00,              # header type = variable
            len(data) & 0xFF,  # payload length
            0x01,              # CRC on
            0x00,              # IQ normal
        ]))

        self._write_buffer(_TX_BASE_ADDR, data)
        self._irq_event.clear()
        self._clear_irq()

        # SET_TX timeout: 3 байта timeout * 15.625 us. 0 = no timeout.
        # 5 секунд = 5_000_000 us / 15.625 = 320000 = 0x04E200
        timeout_units = int(timeout_s * 1_000_000 / 15.625)
        timeout_units = min(timeout_units, 0xFFFFFF)
        self._cmd(_CMD_SET_TX, bytes([
            (timeout_units >> 16) & 0xFF,
            (timeout_units >>  8) & 0xFF,
            (timeout_units      ) & 0xFF,
        ]))

        # Ждём DIO1 IRQ
        if not self._irq_event.wait(timeout=timeout_s + 0.5):
            return False
        irq = self._read_irq_status()
        self._clear_irq()
        return bool(irq & IRQ_TX_DONE)

    # ------------------------------------------------------------------
    def close(self) -> None:
        try:
            lgpio.spi_close(self._spi_handle)
        except Exception:  # noqa: BLE001
            pass
        try:
            self._dio1_cb.cancel()
        except Exception:  # noqa: BLE001
            pass
        for gp in (self._reset_gpio, self._cs_gpio, self._busy_gpio, self._dio1_gpio):
            try:
                lgpio.gpio_free(self._chip, gp)
            except Exception:  # noqa: BLE001
                pass
        try:
            lgpio.gpiochip_close(self._chip)
        except Exception:  # noqa: BLE001
            pass
