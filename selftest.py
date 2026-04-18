"""
selftest.py — Pool Controller Component Self-Test

Invoked via:  python3 main.py st

Steps through every hardware component and prints a pass/fail ASCII table.
No relay is left energized after the test (try/finally guards each channel).
"""

import math
import os
import sys
import time

import config

# Column widths: COMPONENT, STATUS, NOTES
_CW = (22, 6, 0)

_PASS = "PASS"
_FAIL = "FAIL"
_SKIP = "SKIP"


def _row(component: str, status: str, notes: str) -> str:
    return f"  {component:<{_CW[0]}} | {status:<{_CW[1]}} | {notes}"


def _header() -> None:
    print(_row("COMPONENT", "STATUS", "NOTES"))
    print(f"  {'-' * _CW[0]}-+-{'-' * _CW[1]}-+-{'-' * 35}")


def _pr(component: str, passed, notes: str) -> None:
    if passed is None:
        status = _SKIP
    elif passed:
        status = _PASS
    else:
        status = _FAIL
    print(_row(component, status, notes))


def _steinhart(v: float):
    vcc = config.ADS_VCC
    if v <= 0.0 or v >= vcc:
        return None
    r = config.THERM_R_REF * v / (vcc - v)
    t0k = config.THERM_T0 + 273.15
    inv_t = 1.0 / t0k + (1.0 / config.THERM_B) * math.log(r / config.THERM_R0)
    return round(1.0 / inv_t - 273.15, 1)


def _c_to_f(c: float) -> float:
    return round(c * 9 / 5 + 32, 1)


def run() -> None:
    print()
    print("● Here are the real results:")
    print()
    _header()

    # ------------------------------------------------------------------
    # RELAY HAT — I2C detection
    # ------------------------------------------------------------------
    hat_bus = None
    hat_ok  = False
    try:
        import smbus2
        b = smbus2.SMBus(1)
        b.read_byte(config.CELL_I2C_ADDR)
        hat_bus = b
        hat_ok  = True
        _pr("RELAY HAT I2C", True, f"Found at 0x{config.CELL_I2C_ADDR:02X}")
    except Exception as exc:
        _pr("RELAY HAT I2C", False, f"0x{config.CELL_I2C_ADDR:02X} not found: {exc}")

    # ------------------------------------------------------------------
    # ADS1115 — I2C detection + read all 4 channels
    # ------------------------------------------------------------------
    ads_ok   = False
    voltages = [None] * 4
    try:
        import board
        import busio
        import adafruit_ads1x15.ads1115 as ADS
        from adafruit_ads1x15.analog_in import AnalogIn

        i2c  = busio.I2C(board.SCL, board.SDA)
        ads  = ADS.ADS1115(i2c, address=config.ADS_I2C_ADDR)
        chans = [AnalogIn(ads, ch) for ch in range(4)]
        for i, ch in enumerate(chans):
            try:
                voltages[i] = round(ch.voltage, 3)
            except Exception:
                pass
        ads_ok = True
        _pr("ADS1115 I2C", True,
            f"0x{config.ADS_I2C_ADDR:02X}  "
            f"AIN0={voltages[0]}V AIN1={voltages[1]}V "
            f"AIN2={voltages[2]}V AIN3={voltages[3]}V")
    except Exception as exc:
        _pr("ADS1115 I2C", False, f"0x{config.ADS_I2C_ADDR:02X} not found: {exc}")

    # ------------------------------------------------------------------
    # AIR TEMP THERMISTOR — AIN0
    # ------------------------------------------------------------------
    v = voltages[config.ADS_CH_AIR_TEMP]
    if v is not None:
        t_c = _steinhart(v)
        if t_c is not None:
            valid = 0.05 < v < (config.ADS_VCC - 0.05)
            _pr("AIR TEMP (AIN0)", valid,
                f"{_c_to_f(t_c)}°F  ({t_c}°C, V={v}V)")
        else:
            _pr("AIR TEMP (AIN0)", False, f"Voltage out of range: {v}V")
    else:
        _pr("AIR TEMP (AIN0)", False, "ADS unavailable")

    # ------------------------------------------------------------------
    # WATER TEMP THERMISTOR — AIN1
    # ------------------------------------------------------------------
    v = voltages[config.ADS_CH_WATER_TEMP]
    if v is not None:
        t_c = _steinhart(v)
        if t_c is not None:
            valid = 0.05 < v < (config.ADS_VCC - 0.05)
            _pr("WATER TEMP (AIN1)", valid,
                f"{_c_to_f(t_c)}°F  ({t_c}°C, V={v}V)")
        else:
            _pr("WATER TEMP (AIN1)", False, f"Voltage out of range: {v}V")
    else:
        _pr("WATER TEMP (AIN1)", False, "ADS unavailable")

    # ------------------------------------------------------------------
    # POLARITY MONITOR — AIN2
    # ------------------------------------------------------------------
    v = voltages[config.ADS_CH_POLARITY]
    if v is not None:
        _pr("POLARITY MON (AIN2)", True, f"V={v}V")
    else:
        _pr("POLARITY MON (AIN2)", False, "ADS unavailable")

    # ------------------------------------------------------------------
    # CURRENT SENSOR ACS712 — AIN3
    # ------------------------------------------------------------------
    v = voltages[config.ADS_CH_CURRENT]
    if v is not None:
        amps  = round((v - config.ACS_ZERO_V) / config.ACS_SENSITIVITY, 3)
        valid = abs(v - config.ACS_ZERO_V) < 0.5  # ±7.5 A from zero
        _pr("ACS712 (AIN3)", valid,
            f"{amps:+.3f}A  V={v}V (zero≈{config.ACS_ZERO_V}V)")
    else:
        _pr("ACS712 (AIN3)", False, "ADS unavailable")

    # ------------------------------------------------------------------
    # EZO-EC — UART /dev/serial0
    # ------------------------------------------------------------------
    try:
        import serial
        ser = serial.Serial(config.EC_PORT, baudrate=config.EC_BAUD, timeout=2.0)
        ser.reset_input_buffer()
        ser.reset_output_buffer()
        ser.write(b"I\r")
        ser.flush()
        time.sleep(0.4)
        line = ser.read_until(b"\r").decode("ascii", errors="replace").strip()
        ser.close()
        if "EC" in line.upper():
            parts   = line.split(",")
            version = parts[-1] if len(parts) >= 3 else "?"
            _pr("EZO-EC UART", True, f"FW {version}  ({line})")
        else:
            _pr("EZO-EC UART", False, f"Unexpected: {line!r}")
    except Exception as exc:
        _pr("EZO-EC UART", False, str(exc))

    # ------------------------------------------------------------------
    # RS485 PUMP PORT — port exists and opens cleanly
    # ------------------------------------------------------------------
    port = config.PUMP_PORT
    if not os.path.exists(port):
        _pr("RS485 PUMP PORT", False, f"{port} not present")
    else:
        try:
            import serial
            s = serial.Serial(port, baudrate=config.PUMP_BAUD, timeout=0.5)
            s.close()
            _pr("RS485 PUMP PORT", True, f"{port} opens cleanly")
        except Exception as exc:
            _pr("RS485 PUMP PORT", False, f"{port}: {exc}")

    # ------------------------------------------------------------------
    # Relay channel write / readback tests
    # Each: energize → readback 0xFF, de-energize → readback 0x00
    # try/finally guarantees de-energize even on failure
    # ------------------------------------------------------------------
    relay_tests = [
        ("G RELAY (CH1)",    config.CELL_RELAY_CH_GATE),
        ("A+B RELAY (CH2)",  config.CELL_RELAY_CH_POLARITY),
        ("FAN RELAY (CH3)",  config.FAN_RELAY_CH),
    ]

    if hat_ok and hat_bus is not None:
        addr = config.CELL_I2C_ADDR
        for label, ch in relay_tests:
            rb_on = rb_off = None
            try:
                hat_bus.write_byte_data(addr, ch, 0xFF)
                time.sleep(0.05)
                rb_on = hat_bus.read_byte_data(addr, ch)
            except Exception as exc:
                _pr(label, False, f"Energize failed: {exc}")
                continue
            finally:
                try:
                    hat_bus.write_byte_data(addr, ch, 0x00)
                    time.sleep(0.05)
                    rb_off = hat_bus.read_byte_data(addr, ch)
                except Exception:
                    pass

            if rb_on == 0xFF and rb_off == 0x00:
                _pr(label, True, f"ON=0x{rb_on:02X} OFF=0x{rb_off:02X}")
            else:
                on_s  = f"0x{rb_on:02X}"  if rb_on  is not None else "err"
                off_s = f"0x{rb_off:02X}" if rb_off is not None else "err"
                _pr(label, False,
                    f"Readback mismatch: ON={on_s} (exp 0xFF), OFF={off_s} (exp 0x00)")
        try:
            hat_bus.close()
        except Exception:
            pass
    else:
        for label, _ in relay_tests:
            _pr(label, False, "HAT not available")

    # ------------------------------------------------------------------
    # DROK 5V rail — inferred from ADS1115 availability
    # ------------------------------------------------------------------
    if ads_ok:
        _pr("DROK 5V RAIL", True, "ADS1115 responding → 5V present")
    else:
        _pr("DROK 5V RAIL", False, "ADS1115 not responding — check 5V rail")

    # ------------------------------------------------------------------
    # Mean Well / DPS5015 — not testable via software
    # ------------------------------------------------------------------
    _pr("MEAN WELL/DPS5015", None, "Verify manually — no software test interface")

    print()
