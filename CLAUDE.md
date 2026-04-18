# Jarvis Pool Controller — Architecture Reference

Raspberry Pi 3B+ (`sat4`, `10.0.0.152`) running Raspberry Pi OS Bookworm.
Part of the Jarvis home-automation ecosystem.

---

## Hardware & Pinout

| Hardware | Interface | Address / Port | Notes |
|---|---|---|---|
| Waveshare 2-CH RS485 HAT | UART | `/dev/ttySC0` @ 19200 | Pump control bus |
| Hayward EcoStar SP3400VSP | RS-485 (via HAT) | pump addr `0x01` | **Display MUST be disconnected** |
| Atlas EZO-EC probe | UART | `/dev/serial0` @ 9600 | Conductivity in µS/cm |
| GeeekPi 4-ch relay HAT | I2C | `0x10`, per-channel regs | CH1 gate, CH2 polarity (A+B tied), CH3 fans, CH4 unused. 0xFF=ON, 0x00=OFF (per datasheet) |
| ADS1115 ADC | I2C | `0x48` | AIN0=air temp, AIN1=water temp, AIN2=polarity verify, AIN3=current |
| Thermistors (×2) | ADS1115 AIN0/1 | — | 10 kΩ NTC, B=3950, 10 kΩ divider to 3.3 V |
| ACS712 30A | ADS1115 AIN2 | — | 66 mV/A, zero = 2.5 V |
| Flow switch | GPIO 17 | — | Active LOW (closed = flowing) |

### ⚠️ Critical: EcoStar Display

The Hayward EcoStar display panel communicates on the same RS-485 bus.
**You must physically disconnect the display before running this controller.**
Two RS-485 masters on a half-duplex bus will corrupt packets and may damage
the pump drive board.

---

## Module Responsibilities

```
config.py       All settings — .env overrides, no other file touches env vars
state.py        Atomic JSON persistence (state.json) — survives power drops
pump.py         EcoStar RS-485 protocol driver — keepalive packet every 500 ms
cell.py         GeeekPi relay HAT I2C driver — read-modify-write relay byte
sensors.py      ADS1115, EZO-EC, flow switch — all non-fatal
safety.py       Cell interlock logic (stateless update function)
mqtt_client.py  paho v2 + HA auto-discovery + asyncio bridge
main.py         asyncio orchestration, SIGTERM/SIGINT clean shutdown
```

---

## EcoStar RS-485 Protocol

**19200 8N1, half-duplex.  Send every ≤ 500 ms or pump returns to panel control.**

Packet (10 bytes):

```
0x10  0x02  0x0C  0x01  0x00  SPEED  CSUM_H  CSUM_L  0x10  0x03
      ^DLE  ^STX  CTRL  PUMP              ^checksum      ^DLE ^ETX
```

- `CTRL` = controller address `0x0C`
- `PUMP` = pump address `0x01`
- `SPEED` = 0–100 % (0 = stop, 100 = full speed)
- `CSUM` = `sum(0x10, 0x02, 0x0C, 0x01, 0x00, SPEED)` split into two bytes

Example — speed 50 %:
```
10 02 0C 01 00 32 00 51 10 03
                   ^0x51 = 16+2+12+1+0+50
```

---

## Cell Safety Interlock

Implemented in `safety.py::update()`.  Called every 1 second from `main.py`.

```
ALLOW cell = cell_requested
          AND pump_speed >= 1 %
          AND flow_switch = ON
          AND above two conditions have been CONTINUOUSLY true for 60 s
```

Any violation → immediate relay de-energise.  The 60-second timer resets
whenever either condition is lost.

---

## MQTT Topics

Broker: `10.0.0.16:1883`  |  Prefix: `jarvis/pool/TudorPool`

| Topic | Dir | Payload | Retain |
|---|---|---|---|
| `jarvis/pool/TudorPool/status` | pub | `online` / `offline` (LWT) | ✓ |
| `jarvis/pool/TudorPool/pump/speed` | pub | `0`–`100` | ✓ |
| `jarvis/pool/TudorPool/pump/speed/set` | sub | `0`–`100` | — |
| `jarvis/pool/TudorPool/pump/running` | pub | `ON` / `OFF` | ✓ |
| `jarvis/pool/TudorPool/cell/state` | pub | `ON` / `OFF` | ✓ |
| `jarvis/pool/TudorPool/cell/set` | sub | `ON` / `OFF` | — |
| `jarvis/pool/TudorPool/sensors/water_temp` | pub | °C float | — |
| `jarvis/pool/TudorPool/sensors/air_temp` | pub | °C float | — |
| `jarvis/pool/TudorPool/sensors/current` | pub | A float (signed) | — |
| `jarvis/pool/TudorPool/sensors/conductivity` | pub | µS/cm float | — |
| `jarvis/pool/TudorPool/sensors/flow` | pub | `ON` / `OFF` | — |

HA auto-discovery prefix: `homeassistant/`

---

## Thermistor Calculation

Voltage divider: `VCC(5V) → 10kΩ → AINx → NTC → GND`

```python
R = R_REF * V / (VCC - V)
T = 1 / (1/T0_K + (1/B) * ln(R/R0)) - 273.15
# T0_K = 298.15 K (25°C), B = 3950, R0 = 10000 Ω, VCC = 5 V
```

---

## State Persistence

`state.json` — written atomically (temp file + `os.replace`):
```json
{
  "pump_speed": 0,
  "cell_on": false
}
```

---

## Systemd Deployment

```bash
sudo cp systemd/jarvis-pool.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now jarvis-pool
sudo journalctl -fu jarvis-pool
```

---

## Development

```bash
# Create / activate venv
python3 -m venv venv
source venv/bin/activate

# Install deps
pip install -r requirements.txt

# Run directly
python3 main.py

# All hardware failures are non-fatal; the controller starts even with
# no pump, no sensors, and no relay hat attached.
```

---

## Non-Fatal Hardware Guarantee

Every hardware module (`pump`, `cell`, `sensors`) wraps its `init()` in
`try/except` and degrades gracefully:

- Missing `/dev` node → logged as WARNING, module marks itself unavailable
- I2C device not found → logged as WARNING
- GPIO unavailable → logged as WARNING

MQTT always connects and the service always starts.
