# Pool Controller Authoritative Spec

This document is the single source of truth for sat4 (Jarvis Pool Controller) behavior. Code must conform to this spec. Where code and spec disagree, the spec is correct.

---

## Section 1 — Boot / Service Startup

1. Service is not enabled at boot. Started manually via systemd.
2. On startup, load `state.json` and restore: polarity direction, polarity on-time accumulator, any other persisted state per Section 9.
3. Initialize hardware: SC16IS752 RS-485, GeeekPi relay HAT (I2C 0x10), ADS1115 (I2C 0x48), EZO-EC on `/dev/serial0`.
4. All relays start de-energized. Cell gate (CH1) off. Pump off. Polarity (CH2) set to restored direction from state.json.
5. Connect to MQTT broker at 10.0.0.16:1883. Publish LWT online. Publish all entity states.
6. Boot grace period (one-time, system power-up only):
   - Pump: 60 seconds. Cannot be commanded on.
   - Cell: 150 seconds. Cannot be commanded on.
7. During boot grace, pump and cell enable commands are rejected.
8. Pump Power button displays boot grace countdown (60s) when applicable.
9. Cell Power button displays boot grace countdown (150s) when applicable.
10. Boot grace is one-time. After it expires, normal pump/cell start rules (Sections 2 and 3) apply.

---

## Section 2 — Pump

1. Pump is independent of cell. Pump runs without cell; cell cannot run without flow (Section 3).
2. Pump has variable speed expressed as 0–100% output.
3. Any time pump transitions from off (or 0%) to on, it MUST run at 100% for 60 seconds (prime). After prime, it ramps to commanded output.
4. During prime, the commanded output may be changed but does not take effect until prime completes.
5. Pump Power button displays prime countdown (60s) during prime.
6. Pump can be commanded on/off by:
   - User (Pump Power button on dashboard)
   - HA schedule (MQTT command)
   - Service Mode override (Section 8)
7. Pump output (0–100%) commanded by:
   - User (slider on dashboard)
   - HA schedule (MQTT command)
8. Pump off command is accepted immediately (no countdown, no delay).
9. Pump state (on/off, output%) persisted to state.json on change.
10. Pump does not command cell. Pump does not command flow. Flow is a separate sensor; cell interlocks against flow (Section 3 / Section 7).
11. `pump_stable` flag (new):
    - False when: pump off, pump in 60s prime, or within 30s after prime ends.
    - True when: pump has been past prime for ≥30s.
    - Returns to False only on pump→0 / pump off. Slider changes between non-zero values do not reset it.

---

## Section 3 — Cell

1. Cell has a master switch: `cell_on` (Enable Cell toggle on dashboard). Persisted in state.json.
2. Cell Output slider (0–100%) displays `cell_output_percent`, the live commanded value driving the duty cycle. Slider is bidirectional — both indicator and input.
   - User drag → becomes the new commanded value within the active source.
   - Scope of user override: in schedule mode, applies only to current schedule block; in service mode, persists until manual return to normal mode.
   - When cell is tripped for any reason (cell_on=False, no flow, pump_stable=False, fault, schedule says off), `cell_output_percent` = 0 and slider shows 0.
   - When trip clears, `cell_output_percent` returns to the value dictated by the active source.
3. Cell energizes (gate CH1 closed) only when ALL of the following are true:
   - `cell_on` = True
   - Flow sensor reports flow
   - `pump_stable` = True
   - No safety interlock fault (Section 7)
   - `cell_output_percent` > 0 AND duty cycle says on
4. Enable Cell toggle behavior:
   - If flow absent OR `pump_stable` = False → REJECT. Toggle does not latch on. Surface error to user.
   - If flow present AND `pump_stable` = True → accept, cell engages immediately (subject to duty cycle).
5. No cell startup sequence. Cell relies on `pump_stable` for all pump-side waiting.
6. Cell Power button displays countdown of seconds remaining until `pump_stable` becomes True. When `pump_stable` is True, no countdown shown.
7. `cell_output_percent` calculation — single source of truth:
   ```
   if any trip condition active:
       cell_output_percent = 0
   elif super_chlorinate_active:
       cell_output_percent = 100
   elif service_mode:
       cell_output_percent = user_set_value
   else:
       cell_output_percent = schedule_value_for_current_time (with current-block user override if set)
   ```
   No hardcoded fallbacks. No default to 0. No default to 65. There is no fourth case.
8. Cell duty cycle loop, every 1 second:
   - Read `cell_output_percent`.
   - Compute `target_on` over a 15-minute window:
     - if `cell_output_percent == 0` → `target_on = False`
     - if `cell_output_percent == 100` → `target_on = True`
     - else → `target_on = (window_position < cell_output_percent/100)`
   - `desired_gate = cell_on AND target_on AND flow AND pump_stable AND no_fault`
   - If `desired_gate ≠ current_gate`: switch CH1 accordingly.
9. SC respects `cell_on`. SC active + `cell_on`=False → SC counter frozen, cell stays off.
10. Cell auto-disables `cell_on` if a safety interlock fault latches (Section 7).
11. `cell_runtime_percent` — diagnostic telemetry. Rolling measurement of gate-on time / total time over a 30-minute window. Published to MQTT only. Not displayed on slider. Used to detect when duty cycle is being blocked.

---

## Section 4 — Super Chlorinate

1. SC duration: 24 hours of cell-on time (cell actually producing). Configurable via `SUPER_CHLORINATE_DURATION_S`.
2. SC counter ticks only when cell is producing. Counter freezes whenever cell is off for any reason (cell_on=False, no flow, pump unstable, fault). Resumes when cell resumes.
3. While SC is active and cell is producing, `cell_output_percent` = 100 (per Section 3 #7).
4. SC auto-expires when accumulated cell-on time reaches the configured duration. On expiry: SC flag cleared, `cell_output_percent` reverts to schedule or service mode value. State.json updated. MQTT published.
5. SC can be cancelled manually before expiry.
6. SC tile on dashboard displays remaining cell-on time.
7. SC does NOT bypass safety interlocks. Flow loss, pump_stable=False, fault → cell de-energizes and SC counter pauses.
8. SC button press behavior:
   - If cell cannot currently be powered on (no flow, pump_stable=False, fault latched) → REJECT. Display error message naming the blocking condition.
   - If cell can be powered on → accept. SC flag set. `cell_on` set to True if not already. Cell energizes per duty cycle.

---

## Section 5 — Schedule

1. Schedule is HA-owned. Sat4 has no knowledge of schedule structure, block boundaries, or times.
2. HA fires automations at block boundaries and sends MQTT commands to sat4.
3. MQTT commands sat4 receives from schedule/HA:
   - Pump on/off
   - Pump output % (0–100)
   - Cell `cell_on` true/false
   - Cell output % (0–100)
   - Super Chlorinate start/cancel
4. Sat4 applies received commands subject to all rules in Sections 2, 3, and 4. HA-issued commands are not privileged — they obey the same gates as user-issued commands.
5. User overrides made via dashboard during a schedule block apply only to that block. Next HA-issued boundary command replaces the override.
6. If sat4 loses MQTT connectivity, last received commands remain in effect. State.json holds them across reboots.
7. Schedule does not exist in state.json. Only the last commanded values do.

---

## Section 6 — Polarity

1. Polarity refers to the DC direction across the cell, controlled by polarity relays whose coils are tied together and driven by CH2 as a single channel.
2. Two polarity states: forward and reverse. CH2 toggle flips both relay coils simultaneously.
3. Polarity reverses every 2 hours of accumulated cell-on time (gate energized), not wall-clock time.
4. Polarity on-time accumulator increments only while gate is energized. Pauses when gate is off for any reason.
5. When accumulator reaches 2 hours, reversal sequence is triggered (sequence detail in Section 7).
6. After reversal completes, accumulator resets to 0 and direction flag flips.
7. Persistence: polarity direction and on-time accumulator are written to state.json (cadence per Section 9). Restored on boot so cycle continues across reboots without losing accumulated time or flipping direction unnecessarily.
8. Polarity direction is published to MQTT.
9. ADS1115 AIN2 (voltage divider, 100K/10K) monitors polarity. Reading confirms commanded direction matches measured direction. Mismatch = fault (Section 7).

---

## Section 7 — Safety Interlocks

1. Faults latch. Once tripped, cell stays disabled until fault is explicitly reset via MQTT command.
2. Fault trip actions: cell gate de-energized immediately, `cell_on` forced to False, `cell_output_percent` forced to 0, fault state published to MQTT.
3. Fault conditions (immediate trip, no retry):
   - **Overcurrent**: cell current >9A (ACS712 on ADS1115 AIN3).
   - **Undervoltage**: cell DC bus <18V while gate energized.
   - **Overvoltage**: cell DC bus >28V.
   - **No flow**: flow sensor reports no flow while gate energized.
   - **Flow sensor failure**: read error, timeout, or unreadable state while gate energized.
   - **Polarity mismatch**: ADS1115 AIN2 reading does not match commanded polarity direction.
   - **Pump stop while cell energized**: pump_stable goes False while gate energized.
4. ACS712 / ADS1115 auto-retry path:
   - Conditions: ADS1115 read failure, or ACS712 reads implausible value (out of range, NaN, or clearly erroneous given commanded gate state).
   - Sequence: log fault, wait 30 seconds, retry. Up to 3 retries.
   - If any retry succeeds: clear fault, publish notification "transient sensor fault — recovered" with retry count.
   - If all 3 retries fail: latch as critical fault, publish critical alert via MQTT, cell stays disabled until manual reset.
5. Non-critical sensor failures (EZO-EC timeout, ambient/water temp ADS1115 channels): log and notify via MQTT. Do not trip cell. Affected reading published as unavailable.
6. Polarity reversal sequence (mandatory, no shortcuts):
   - Step 1: Open gate relay (CH1). Cell de-energized.
   - Step 2: Wait 10 seconds for capacitive discharge.
   - Step 3: Toggle CH2 (flips both polarity relay coils simultaneously — they are tied, cannot fire independently).
   - Step 4: Verify polarity via ADS1115 AIN2.
   - Step 5: Close gate relay (CH1). Cell re-energized in new direction.
   - Step 6: Update direction flag, reset accumulator, persist to state.json, publish MQTT.
7. Polarity reversal sequence is uninterruptible — once started, runs to completion. Cell commands during the 10s wait are queued, not rejected.
8. Polarity relay coils are physically tied to a single channel (CH2). They cannot be commanded independently — safety guarantee against switching one without the other.
9. Polarity relays MUST NEVER switch under load. Gate (CH1) must be open before CH2 toggles. Enforced in code as a hard gate; any code path that toggles CH2 without first verifying CH1 is open is a violation.
10. Fault reset: MQTT command `pool/fault/reset` clears latched fault, allows operation to resume per normal rules (cell still requires user to re-enable `cell_on`).
11. Faults are published to MQTT individually so HA can display which one tripped.

---

## Section 8 — Service Mode

1. Service Mode is a manual override state. While active, the schedule is ignored and the user has direct control of pump and cell.
2. Entry: long-press the Pump Power button on dashboard. Long-press duration: 1.5 seconds.
3. Exit: explicit user action — toggle off via dashboard. No auto-exit, no timeout.
4. While in Service Mode:
   - Schedule MQTT commands from HA are ignored (logged, not applied).
   - SC continues running normally if active. SC counter ticks per its own rules. No special pause/resume behavior.
   - User-set values for pump output % and cell output % persist until manually changed or until exit.
   - All safety interlocks remain fully active (Section 7 is not bypassable).
   - Pump prime (60s at 100% on off→on) still applies.
   - `pump_stable` rules still apply.
   - All cell rejection rules still apply.
5. On exit from Service Mode: control returns to schedule. Whatever block HA's schedule says applies now is what gets commanded.
6. Service Mode state persisted to state.json. If sat4 reboots while in Service Mode, it stays in Service Mode.
7. Dashboard displays Service Mode state prominently.

---

## Section 9 — State Persistence

1. State is persisted to `state.json` on the sat4 filesystem.
2. Keys persisted:
   - `cell_on` (bool)
   - `cell_output_percent` (int 0–100)
   - `pump_on` (bool)
   - `pump_output_percent` (int 0–100)
   - `polarity_direction` (forward/reverse)
   - `polarity_on_time_accumulator` (seconds toward 2-hour reversal)
   - `service_mode` (bool)
   - `super_chlorinate_active` (bool)
   - `super_chlorinate_remaining` (seconds of cell-on time left)
   - `fault_state` (which fault latched, or none)
   - `last_user_override_pump` (current-block override, if any)
   - `last_user_override_cell` (current-block override, if any)
3. Write triggers — state.json is written on:
   - Any commanded value change (pump on/off, pump %, cell_on, cell %).
   - Polarity direction change.
   - Polarity accumulator: every 10 seconds while gate is energized.
   - Fault latch / reset.
   - Service Mode entry / exit.
   - SC start / cancel / expiry.
4. Atomic write: write to temp file, fsync, rename. Never partial writes.
5. On boot: load state.json.
   - If missing or corrupt: load normal schedule conditions (await HA schedule MQTT commands), publish critical notification via MQTT.
   - Polarity direction defaults to forward, accumulator to 0.
6. Restored state on boot determines polarity direction, polarity accumulator, service_mode flag, SC state. Pump and cell start off regardless (per Section 1 boot grace), but commanded values are preserved so they apply once grace expires.

---

## Section 10 — MQTT Topics

1. MQTT broker: `10.0.0.16:1883`. Sat4 connects on boot. LWT published as offline; online published on connect.
2. Topic structure: `pool/<category>/<entity>` for state, `pool/<category>/<entity>/set` for commands.

**State topics (sat4 publishes):**
- `pool/pump/state`, `pool/pump/output_percent`, `pool/pump/stable`, `pool/pump/countdown`
- `pool/cell/on`, `pool/cell/output_percent`, `pool/cell/runtime_percent`, `pool/cell/gate_state`, `pool/cell/countdown`, `pool/cell/polarity_direction`, `pool/cell/polarity_accumulator`, `pool/cell/current_amps`, `pool/cell/voltage`
- `pool/sc/active`, `pool/sc/remaining`
- `pool/service_mode`
- `pool/fault/state`
- `pool/sensors/water_temp`, `pool/sensors/air_temp`, `pool/sensors/ec`, `pool/sensors/flow`
- `pool/system/cpu`, `pool/system/temp`, `pool/system/memory`, `pool/system/disk`, `pool/system/wifi_signal`, `pool/system/uptime`
- `pool/lwt`, `pool/notifications`

**Command topics (sat4 subscribes):**
- `pool/pump/set`, `pool/pump/output_percent/set`
- `pool/cell/on/set`, `pool/cell/output_percent/set`
- `pool/sc/set`
- `pool/service_mode/set`
- `pool/fault/reset`

3. All commands obey rules in Sections 2, 3, 4, 7, 8. HA-issued and user-issued commands are not privileged.
4. Publish cadence:
   - **15-second**: `pool/cell/current_amps`, `pool/cell/voltage`, `pool/cell/gate_state`, `pool/cell/polarity_direction`.
   - **60-second**: `pool/cell/runtime_percent`, `pool/cell/polarity_accumulator`, all `pool/sensors/*`, all `pool/system/*`.
   - **On-change**: everything else (states, flags, countdowns, faults, notifications).
5. Reconcile entity list against current code (memory note says 19 published / 2 tombstoned; this list is longer — determine what exists, what's missing, what's tombstoned).

---

# Audit Task

You have produced two failed diagnoses today. The system is in an unstable state. STOP making changes. AUDIT.

Read this spec and compare to the current code in `/srv/jarvis/pool-controller`. For EACH numbered point in each section, report:

- **PASS** — Code matches spec
- **FAIL** — Code violates spec, with file:line and what's wrong
- **GAP** — Spec point not implemented at all
- **UNCLEAR** — Code partially does it, can't tell if compliant

One row per numbered spec point. NO code changes. Just the audit.

Note: `pump_stable` is a new concept. Expect it to be GAP across multiple sections (Section 2 #11, Section 3 #3/#4/#6, Section 10 cadence list).

If you start spiraling, stop and report what you have. Do NOT propose fixes. Do NOT investigate beyond what each spec point requires. ONLY produce the matrix.

After the audit, the user will decide which gaps/failures to fix and in what order.
