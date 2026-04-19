# Jarvis Pool Controller — Architecture Reference

---

## ⚠️ ABSOLUTE HARDWARE RULE — Polarity Switching

**NEVER switch the polarity relay (CH2) while the gate relay (CH1) is energised.**

The mandatory sequence, every time, no exceptions, regardless of safety state or test mode:

1. CH1 (gate) → OFF (`0x00`)
2. Wait `config.POLARITY_SWITCH_DELAY_S` (default 3 s)
3. CH2 (polarity) → target (`0x00` = A/forward, `0xFF` = B/reverse)
4. Wait `config.POLARITY_SWITCH_DELAY_S`
5. CH1 (gate) → ON (`0xFF`) — only if it should be on

This applies to ALL code paths: normal operation, MQTT commands, test scripts, direct I2C writes, and any future tooling. Switching polarity under load will damage the cell and relay.

**Enforcement:** This sequence is now enforced in code (`cell.py::set_polarity()`). `_write_polarity_relay()` re-reads CH1 immediately before any CH2 write and raises `RuntimeError` if CH1 is still energised. Any PR adding a direct CH2 write outside `set_polarity()` must be rejected.

## ⚠️ ACS712 Power Sequencing — CH4

**CH4 gates ACS712 Vcc.** The ACS712 output holds the Pi's boot rail low if powered at startup, preventing a clean boot. CH4 must therefore start de-energised (init drives all channels OFF) and be energised only after the service is fully up.

Mandatory sequence:
1. Service starts — all relay channels OFF (enforced by `cell.init()`)
2. MQTT connects, all task loops launch
3. **5 seconds later** — `cell.set_acs712_power(True)` energises CH4; `sensors.set_acs712_powered(True)` unlocks `read_current()`
4. At shutdown — `sensors.set_acs712_powered(False)` called first, then `cell.close()` de-energises CH4

`sensors.read_current()` returns `None` (not an error) while `_acs712_powered` is False. The `ACS712_POWER_CHANNEL` config key (default 4) allows remapping without code changes.

---

## System Overview

Raspberry Pi 3B+ (`sat4`, `10.0.0.152`) running Raspberry Pi OS Bookworm. Controls a salt-water chlorinator cell (polarity, gate, current sensing) and a Hayward EcoStar variable-speed pump via RS-485, publishing all telemetry to the Jarvis home-automation MQTT broker at `10.0.0.16:1883`. Repo URL: TBD — confirm with user.

---

## Relay HAT Channel Map

| Channel | Function | Register Value When Energized/ON | Notes |
|---|---|---|---|
| CH1 | Gate — salt cell H-bridge enable | `0xFF` | Must be OFF (`0x00`) during any polarity change |
| CH2 | Polarity — drives coils of Polarity Relay B and Polarity Relay C simultaneously | `0xFF` = reverse; `0x00` = forward | Physical H-bridge cross-wiring; see H-bridge section |
| CH3 | Fans — enclosure cooling | `0xFF` | Controlled by `fans.py` |
| CH4 | ACS712 power gate | `0xFF` | OFF at boot (enforced by `cell.init()`); energized 5 s after startup by `acs712_power_on_task` |

---

## ⚠️ GeeekPi HAT Logic

`0xFF` written to a channel register = **energized / relay ON**.
`0x00` written to a channel register = **de-energized / relay OFF**.

This is the correct, code-authoritative mapping. The comment block in `config.py` lines 35–40 states the reverse and is **stale/incorrect** — `cell.py` (`RELAY_ON = 0xFF`, `RELAY_OFF = 0x00`) and `fans.py` are authoritative. Any new direct I2C writes must use `0xFF` to energize and `0x00` to de-energize.

**Silkscreen NO/NC labels are also reversed on this HAT.**

---

## ADS1115 Channel Map

ADS1115 initialized with `gain=2/3` (±6.144 V FSR).

| AIN | Sensor | Scaling | Wired Via | Notes |
|---|---|---|---|---|
| AIN0 | Polarity verify | 100 kΩ / 10 kΩ voltage divider from DPS5015 OUT+; ~2.18 V max | Mini terminal M1–M3 | "Malfunctioning AIN0" — close enough for polarity sign detection |
| AIN1 | Air temp thermistor | 10 kΩ NTC + 10 kΩ fixed divider; 3.3 V Vcc | Term 7 / 10 / 11 | Steinhart-Hart B = 3950 |
| AIN2 | Water temp thermistor | 10 kΩ NTC + 10 kΩ fixed divider; 3.3 V Vcc | Term 7 / 8 / 12 | **Not connected** |
| AIN3 | ACS712 OUT | 66 mV/A, zero at 2.5 V | Direct | ACS712 powered from 5 V via CH4 gate |

---

## I2C Device Map

| Address | Device | Purpose |
|---|---|---|
| `0x10` | GeeekPi 4-channel relay HAT | Controls CH1–CH4 (gate, polarity, fans, ACS712 power) |
| `0x48` | ADS1115 ADC | Reads AIN0–AIN3; ADDR pin tied via M3 bottom → Term 13 top return path |

---

## Serial Ports

| Device | Hardware | Baud | Purpose |
|---|---|---|---|
| `/dev/ttyUSB0` | Waveshare USB-to-RS485 adapter | 19200 | EcoStar SP3400VSP pump; pump addr `0x01`, controller `0x0C`, 500 ms packet interval. **Display MUST be disconnected.** |
| `/dev/serial0` (ttyS0 mini UART) | Atlas Scientific EZO-EC | 9600 | Conductivity in µS/cm; UART mode (carrier board pending) |

---

## Pi GPIO Usage and Wire Color Map

| Pin | GPIO# | Function | Wire Color | Destination |
|---|---|---|---|---|
| 1 | 3.3 V | Power | Orange | ADS1115 VDD + thermistor dividers (via Term 7 and Term 10 jumper) |
| 2 | 5 V | Power | Red | DROK OUT+ → 5 V bus fan-out to ACS712 Vcc, EZO-EC Vcc, ADS1115 via 5 V bus |
| 3 | GPIO 2 (SDA) | I2C data | Blue | ADS1115 + Relay HAT |
| 4 | 5 V | Power | Red | Additional 5 V distribution path (TBD — verify which device) |
| 5 | GPIO 3 (SCL) | I2C clock | Blue | ADS1115 + Relay HAT |
| 6 | GND | Ground | Black | DROK OUT− |
| 8 | GPIO 14 (TX) | UART TX | Purple | EZO-EC RX (via carrier when installed) |
| 10 | GPIO 15 (RX) | UART RX | Purple | EZO-EC TX (via carrier when installed) |
| 11 | GPIO 17 | Flow switch input | Teal | Flow switch (software pull-up) |

---

## Component Inventory

| Component | Bus or Location | Address or Terminal | Notes |
|---|---|---|---|
| Raspberry Pi 3B+ (`sat4`) | — | 10.0.0.152 | Bookworm; pool controller host |
| Waveshare USB-to-RS485 dongle | USB | `/dev/ttyUSB0` | Replaced original 2-CH RS485 HAT |
| GeeekPi 4-ch relay HAT | I2C | `0x10` | CH1–CH4; shares I2C bus with ADS1115 |
| ADS1115 ADC | I2C | `0x48` | gain=2/3 (±6.144 V FSR) |
| ACS712-30A current sensor | ADS1115 AIN3 | — | 66 mV/A, 5 V supply gated by CH4 |
| Mean Well LRS-350-24 | AC mains | — | 24 V DC main supply |
| DROK buck converter | 24 V → 5 V | Term 15 (OUT−) | Powers Pi, ACS712, EZO-EC |
| DPS5015 (24 V / 8 A CC) | 24 V bus | M1 (OUT+) | CC supply for salt cell |
| Inline 10 A slow-blow fuse | DPS5015 OUT+ series | — | Protects cell current path |
| 3× VAMRONE YJ2N-LY / MY2NJ DPDT 24 V relays | 24 V coils | Term 16 (GND) | Gate relay A, Polarity relay B, Polarity relay C |
| Atlas EZO-EC | UART `/dev/serial0` | — | Conductivity; carrier board pending |
| K1.0 probe | EZO-EC | — | Pending |
| 2× 10 kΩ NTC thermistors | ADS1115 AIN1/AIN2 | Term 8 / 11 | B=3950; air and water temp |
| Paddle flow switch | GPIO 17 | Term 9 | Active LOW |

---

## Main Terminal Strip Schedule (16 positions)

**Legend — combined terminals:** "Combined via screws and bars" means two or more conductors share the same screw clamp or a bus bar bridging adjacent terminals. This is a mechanical assembly detail — no solder. Color groups indicate terminals bridged as a bus: <span style="color:darkorange">**orange group**</span>, <span style="color:blue">**blue group**</span>, <span style="color:purple">**purple group**</span>.

| Term | Origin | Bottom | Top |
|---|---|---|---|
| 1 | Neutral Feed | Neutral Feed In | Neutral Feed Out |
| 2 | Earth Ground | Earth Ground In | Earth Ground Out |
| 3 | TCell Cable Plug | TCell Plug Plate 1 Black | EMPTY |
| 4 | TCell Cable Plug | TCell Plug Plate 2 Black | Out to Relay B(6) |
| 5 | TCell Cable Plug | TCell Plug Plate 1 White | Out to Relay B(5) |
| 6 | TCell Cable Plug | TCell Plug Plate 2 White | EMPTY |
| 7 | (internal tie) | 10K Resistor #1 Leg 1 | GPIO Pin 1 (3.3V) + Jumper to Term 10 |
| 8 | | 10K Resistor #1 Leg 2 + Water Temp Wire 1 | ADS1115 Pin A2 |
| 9 | | Flow Switch Wire 1 | Pi GPIO 11 |
| 10 | | 10K Resistor #2 Leg 1 | Jumper from Term 7 (3.3V) + ADS1115 VCC |
| 11 | | 10K Resistor #2 Leg 2 | ADS1115 Pin A1 + Air Temp Wire 1 |
| 12 | | Flow Switch Wire 2 + Water Temp Wire 2 | (top empty) |
| 13 | | Air Temp Wire 2 | M3 Bottom + ADS1115 ADDR |
| 14 | | | EZO-EC GND + ACS712 GND |
| 15 | | | DROK Out − |
| 16 | HAT Relay A,B,C | HAT Relay A,B,C | Mean Well Ground |

---

## Mini Terminal Schedule (M1/M2/M3)

| Position | Top | Bottom |
|---|---|---|
| M1 | 10K Resistor (red heat shrink) Leg 1 | DPS5015 OUT+ Direct |
| M2 | 10K Resistor (red) Leg 2 + 100K Resistor (blue) Leg 1 (twisted bundle) | ADS1115 AIN0 |
| M3 | 100K Resistor (blue heat shrink) Leg 2 | Term Strip 13 Top |

Polarity divider: 100 kΩ (blue) high-side + 10 kΩ (red) low-side. 24 V × (10/110) = 2.18 V max at AIN0 — in-spec for ADS1115 at 3.3 V Vdd.

---

## Resistor Reference

4 resistors total in the system.

| Qty | Value | Heat Shrink Color | Location | 5-band color code |
|---|---|---|---|---|
| 3 | 10 kΩ | Red | Air temp divider (Term 7/11), water temp divider (Term 7/8), polarity divider low-side (M1/M2) | Brown-Black-Black-Red-Brown |
| 1 | 100 kΩ | Blue | Polarity divider high-side (M2/M3) | Brown-Black-Black-Orange-Brown |

---

## Direct Connection Schedule

Connections **not** routed through the main terminal strip or mini terminal.

| Source | Destination | Notes |
|---|---|---|
| Mains L | Mean Well L terminal | Via breaker |
| Mains N | Mean Well N terminal | Via breaker |
| Mains Earth | Mean Well Earth terminal | Via breaker |
| Mean Well +V | DPS5015 IN+ | 24 V bus |
| Mean Well +V | DROK IN+ | 24 V bus |
| Mean Well +V | Relay HAT coil power (CH2/CH3) | TBD — confirm exact terminal |
| Mean Well −V | DPS5015 IN− | 24 V return |
| Mean Well −V | DROK IN− | Via Term 15 |
| Mean Well −V | Jumper bar destination | TBD — confirm with user |
| DROK OUT+ (5 V) | Pi Pin 2 | 5 V supply to Pi |
| DROK OUT+ (5 V) | 5 V bus bar | Feeds ACS712 Vcc \*1, EZO-EC Vcc; note: ADS1115 powered from 3.3 V (Pi Pin 1) per current state — verify |
| 5 V bus bar | ACS712 Vcc \*1 | Via CH4 gate relay |
| 5 V bus bar | EZO-EC Vcc | Direct |
| 5 V bus bar GND | All 5 V− grounds | Single bus bar |
| DPS5015 OUT+ | Inline 10 A fuse IN | Cell current path |
| Inline fuse OUT | ACS712 IP+ | Cell current path |
| ACS712 IP− | Gate Relay A COM | Cell current path |
| Gate Relay A NO | Polarity Relay B Pin 9 COM \*2 | H-bridge feed |
| Gate Relay A NO | Polarity Relay C Pin 9 COM \*2 | H-bridge feed |
| Polarity Relay B coil A1 | Relay HAT CH2 NO | Parallel with C coil |
| Polarity Relay C coil A1 | Relay HAT CH2 NO | Parallel with B coil |
| Polarity Relay B coil A2 | Term 16 Top (GND) | |
| Polarity Relay C coil A2 | Term 16 Top (GND) | |
| Waveshare USB dongle A | EcoStar pump terminal A | RS-485 bus |
| Waveshare USB dongle B | EcoStar pump terminal B | RS-485 bus |

---

## H-Bridge Wiring Detail

**Gate Relay A** (driven by CH1):

- A9 COM → DPS5015 OUT+ (via inline fuse + ACS712 IP+/IP−)
- A5 NO → Polarity Relay B Pin 9 COM \*2 AND Polarity Relay C Pin 9 COM
- Coil A1 → Relay HAT CH1 NO
- Coil A2 → Term 16 Top (ground)

**Polarity Relay B** (driven by CH2; coil tied in parallel with C):

- B9 COM → Gate A5
- B12 COM → DPS5015 OUT− (via C12)
- B5 NO → C1 NC → [T-Cell Lead 1]
- B8 NO → C4 NC → [T-Cell Lead 2]
- B1 NC → C5 NO
- B4 NC → C8 NO
- Coil A1 → Relay HAT CH2 NO
- Coil A2 → Term 16 Top

**Polarity Relay C** (coil tied in parallel with B on CH2):

- C9 COM → Gate A5
- C12 COM → DPS5015 OUT− (tied to B12)
- Cross-links per B above
- Coil A1 → Relay HAT CH2 NO (parallel with B)
- Coil A2 → Term 16 Top

B and C coils fire simultaneously on CH2 energization. Software controls one channel; hardware cross-wiring performs the H-bridge polarity flip.

---

## Safety Interlocks

- Pump speed ≥ 1 % AND flow switch closed AND 60-second continuous timer elapsed → permit CH1 (gate) energize
- Any violation during cell operation → immediate CH1 (gate) de-energize
- Polarity switch sequence: gate OFF → delay (`POLARITY_SWITCH_DELAY_S`, default 3 s) → toggle CH2 → delay → gate ON; enforced in `cell.py::set_polarity()`
- ACS712 power gate (CH4): de-energized at boot (enforced by `cell.init()` writing `RELAY_OFF` to all channels on first I2C operation); service energizes CH4 after 5 s startup delay via `acs712_power_on_task`

---

## Items To Resolve

| Item | Detail |
|---|---|
| `/etc/rc.local` SC16IS752 overlay | Line `/bin/dtoverlay sc16is75x-spi sc16is752 spi1-1cs int_pin=24` is **OBSOLETE** — Waveshare 2-CH RS485 HAT replaced by USB dongle. TO BE CLEANED UP. |
| Repo URL | Not found in existing docs. Confirm with user. |
| Mean Well +V → HAT coil power | Exact terminal connection not confirmed. TBD. |
| Mean Well −V jumper bar | Destination not confirmed. TBD. |
| DROK OUT+ / ADS1115 power | ADS1115 listed as 3.3 V (Pi Pin 1) in current config but 5 V bus also runs near it. Verify in-situ. |
| Pi Pin 4 destination | Exact device not confirmed. TBD. |
| `config.py` lines 35–40 comment | States inverted logic (`0x00` = ON) — **wrong**. Should be updated to match `cell.py` (`0xFF` = ON). Out of scope for this task but flagged. |
