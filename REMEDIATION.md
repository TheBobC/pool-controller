# Pool Controller Remediation Plan

This is the complete fix list, derived from the audit of the codebase against `SPEC.md`. Items are ordered by safety priority. Work them sequentially. Do not skip ahead. Report back after each numbered item with what changed and what tests confirmed it works.

**Ground rules:**

- Read `SPEC.md` before each item. The spec is correct; the code is wrong.
- One item at a time. Commit per item with a clear message referencing the spec section (e.g., `fix(safety): latch faults per SPEC §7.1-2`).
- No drive-by changes. If you spot something else broken, note it, do not fix it as part of another item.
- If an item is ambiguous, stop and ask — do not invent behavior.
- Do not introduce new defaults like `65` or `100` anywhere. The spec's `cell_output_percent` calculation in §3.7 is the only source of truth.
- HA configuration is mounted on Jarvis at `/mnt/ha-config/` (Samba mount of `//10.0.0.16/config`). Phase 6 requires editing files there as well as in the sat4 repo. Both sets of edits ship together.

---

## Phase 1 — Safety foundations

These must land first. Without them, every other fix is on shaky ground.

### Item 1 — Fault latching (SPEC §7.1, §7.2, §7.11)

Faults currently do not latch. `handle_cell_trip()` is transient — cell can re-enable immediately after a trip. This is unsafe.

Implement:
- A `fault_state` value: `None` when no fault, otherwise the specific fault name (string).
- When any fault condition triggers:
  - Cell gate (CH1) de-energized immediately.
  - `cell_on` forced to `False` (so `_cell_requested = False`).
  - `cell_output_percent` forced to `0`.
  - `fault_state` set to the fault name.
  - State persisted to `state.json`.
  - Fault published to MQTT (per §7.11 — individual fault topic so HA can show which one).
- While `fault_state` is not `None`, all paths that could energize the cell must refuse (Enable Cell command, SC command, duty cycle gate logic).
- Persist `fault_state` in `state.json` so it survives reboot. A latched fault must remain latched across power cycles.

### Item 2 — Fault reset via MQTT (SPEC §7.10)

Add subscription to a `fault/reset` command topic (use whatever prefix the rest of the code currently uses — final topic structure is settled in Phase 6).

On receipt:
- Clear `fault_state` to `None`.
- Persist to `state.json`.
- Publish updated fault state to MQTT.
- Do NOT auto-re-enable `cell_on`. User must explicitly re-enable cell after reset (per §7.10 parenthetical).

### Item 3 — Enable Cell rejection gate (SPEC §3.4)

`handle_cell_set()` currently accepts unconditionally. This is the bug behind today's incident.

When a command to enable the cell arrives (`cell_on` → True):
- If flow absent → REJECT. Do not set `_cell_requested = True`. Publish error message naming the blocking condition.
- If `pump_stable` is False (per Item 6 below — for now, gate on the existing `pump_speed >= MIN` check until Item 6 lands) → REJECT, publish error.
- If `fault_state` is not None → REJECT, publish error naming the fault.
- Otherwise, accept normally.

Error messages should publish to a notifications topic (current prefix; final topic settled in Phase 6) with severity `error` and a clear human-readable message: e.g., `"Cell enable rejected: no flow detected"`.

### Item 4 — Super Chlorinate rejection gate (SPEC §4.8)

`handle_super_chlorinate_set(True)` currently accepts unconditionally and unconditionally sets `_cell_requested = True`. This is the other half of today's bug — SC bypasses everything Enable Cell respects.

Apply the same rejection rules as Item 3:
- If flow absent → REJECT, publish error.
- If `pump_stable` is False (or current pump_speed proxy until Item 6) → REJECT, publish error.
- If `fault_state` is not None → REJECT, publish error.
- Otherwise, accept. Set SC flag. If `cell_on` is False, set it to True (this is the only place SC is allowed to enable the cell, per §4.8 — but only after the gates above pass).

Critical: SC must NOT bypass `cell_on` when user has explicitly set it to False with no other reason — re-read §3.9 carefully. SC counter ticks but cell stays off in that scenario.

### Item 5 — Cell trip forces cell_on = False (SPEC §3.10, §7.2)

Currently `handle_cell_trip()` cancels SC but does not clear `_cell_requested`. After a trip, `cell_on` stays True, which means the moment the interlock condition clears the cell tries to re-energize. This must change.

On any fault trip:
- Set `_cell_requested = False`.
- Persist to `state.json`.
- Publish to MQTT.

User must explicitly re-toggle Enable Cell after a fault is reset. No auto-recovery.

### Item 6 — Implement pump_stable (SPEC §2.11)

This is the foundational flag for Section 3's rejection logic. Currently absent.

Implement:
- `pump_stable` boolean attribute on the pump module.
- False when: pump is off, pump is in the 60s prime, OR within 30s after prime ends.
- True when: pump has been past prime for ≥30 seconds.
- Returns to False ONLY on pump → 0 / pump off. Slider changes between non-zero values do NOT reset it (a 60% → 80% change keeps `pump_stable = True`).
- Publish to MQTT on change.
- Update Items 3 and 4 above to use `pump_stable` directly instead of the pump_speed proxy.

### Item 7 — Polarity direction persistence (SPEC §6.7, §9.2)

Currently `cell.init()` always sets `_polarity = "forward"`, ignoring saved value. Every reboot resets the polarity cycle.

Implement:
- Add `polarity_direction` to `_DEFAULTS` (default `"forward"`).
- On any polarity flip, save direction to `state.json`.
- On boot, restore `_polarity` from saved state (with `"forward"` fallback if absent).
- After Item 1's `state.json` write trigger work, polarity direction changes should also trigger a write.

### Item 8 — Remaining fault conditions (SPEC §7.3)

Five of seven fault conditions are not implemented. Add:
- **Overcurrent**: cell current > 9A on ACS712 (ADS1115 AIN3) → trip.
- **Undervoltage**: cell DC bus < 18V while gate energized → trip.
- **Overvoltage**: cell DC bus > 28V → trip.
- **Flow sensor failure**: read error / timeout / unreadable while gate energized → trip (separate from "no flow" which already exists).
- **Polarity mismatch**: ADS1115 AIN2 reading does not match commanded `_polarity` → trip. (Currently marked Phase 2 — implement now.)

Each fault uses the latching machinery from Item 1.

### Item 9 — ACS712 / ADS1115 auto-retry (SPEC §7.4)

For ADS1115 read failures or implausible ACS712 readings:
- Log fault, wait 30s, retry. Up to 3 retries.
- If a retry succeeds: clear fault, publish "transient sensor fault — recovered" notification with retry count.
- If all 3 retries fail: latch as critical fault per Item 1, publish critical alert.

This applies ONLY to the ACS712/ADS1115 read path. Other sensor failures (EZO-EC, temp channels) per §7.5: log + notify, do not trip cell.

### Item 10 — Polarity reversal sequence corrections (SPEC §7.6, §7.7)

Current `POLARITY_SWITCH_DELAY_S = 3.0`. Spec is 10s. Update.

Add ADS1115 AIN2 verify step after CH2 toggle, before re-energizing CH1. If verify fails (mismatch), trip polarity-mismatch fault.

Cell commands received during the 10s discharge wait must be queued and applied after the sequence completes. Currently silently dropped.

---

## Phase 2 — Cell logic correctness

### Item 11 — cell_output_percent zeroing on trip (SPEC §3.2, §3.7)

Currently `cell_output_percent` is published as the saved value regardless of cell state. Must be 0 whenever cell is tripped (any condition: cell_on=False, no flow, pump_stable=False, fault, schedule says off).

Implement the §3.7 calculation as the single source of truth:
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

No fallback to 0 in env (`CELL_OUTPUT_DEFAULT`). No fallback to 65. There is no fourth case.

When trip clears, value returns to source-dictated value automatically.

### Item 12 — Duty cycle window 15 minutes (SPEC §3.8)

Window is currently 600s (10 min). Spec is 900s (15 min). Update.

Verify gate logic includes ALL conditions from §3.8:
```
desired_gate = cell_on AND target_on AND flow AND pump_stable AND no_fault
```

Currently `pump_stable` and `no_fault` (latched) are missing.

### Item 13 — cell_runtime_percent window (SPEC §3.11)

`actual_duty.py` uses 86400s (24 hours). Spec is 1800s (30 minutes). Update.

Topic name stays at `cell/actual_duty` until the rename phase.

---

## Phase 3 — Boot behavior

### Item 14 — Service not enabled at boot (SPEC §1.1)

Systemd unit currently has `WantedBy=multi-user.target`. Remove this. Disable auto-start. Service runs only on manual `systemctl start`.

**Verify with user before executing.** If the user wants the service to auto-start now that the system is permanently mounted, this item gets dropped and SPEC §1.1 is updated instead. Ask before disabling.

### Item 15 — Boot grace enforcement (SPEC §1.6, §1.7, §1.10)

Replace the existing `POWER_RECOVERY_GRACE_S = 120` mechanism with split, command-blocking grace:
- Pump grace: 60s from service start. During this window, pump enable commands are REJECTED (not queued, not delayed — rejected with error notification).
- Cell grace: 150s from service start. During this window, cell enable commands and SC commands are REJECTED.
- One-time, system-power-up only. After expiry, normal rules apply.
- Boot grace is NOT for command-during-resume — it is a hard lockout.

**HA implication:** if HA's schedule fires a command within the grace window after a reboot, the command will be rejected. HA must be ready for this. After Phase 6, HA's automations should retry on rejection or wait for the boot grace countdown to reach 0 before issuing commands. Until Phase 6, document that HA may see rejected commands during the first 60s/150s after sat4 restart.

### Item 16 — Boot grace countdown publishing (SPEC §1.8, §1.9)

Publish remaining seconds in pump/cell boot grace to dedicated countdown topics. Initial topic names (current prefix scheme): `pump/boot_grace_remaining_s` and `cell/boot_grace_remaining_s`. Stop publishing once expired.

### Item 17 — pump_stable countdown (SPEC §3.6)

Cell Power button needs to display countdown to `pump_stable` becoming True. Publish remaining seconds to a cell countdown topic. When `pump_stable` is True, publish 0 / null and stop.

---

## Phase 4 — State persistence cleanup

### Item 18 — state.json schema alignment (SPEC §9.2)

Rename keys to match spec. Add missing keys.

| Current | Spec |
|---|---|
| `pump_power_on` | `pump_on` |
| `pump_speed` | `pump_output_percent` |
| `polarity_on_time_s` | `polarity_on_time_accumulator` |
| `super_chlorinate_remaining_s` | `super_chlorinate_remaining` |
| (missing) | `polarity_direction` |
| (missing) | `fault_state` |
| (missing) | `last_user_override_pump` |
| (missing) | `last_user_override_cell` |

Provide a one-time migration: on load, if old key present and new key absent, rename. Keep migration logic for one release then remove.

**Before running migration:** back up the current `state.json` to `state.json.pre-migration-bak` so any data loss can be recovered. Show the user the proposed migration code and the current state.json contents before executing.

### Item 19 — Write triggers and atomicity (SPEC §9.3, §9.4)

- Polarity accumulator: write every 10s while gate energized. Currently 60s.
- Add write triggers for fault latch / reset (covered by Item 1, just verify).
- Atomic write: temp file + `fsync` + `os.replace()`. Currently no fsync — not power-safe under hard reset. Add explicit `os.fsync(fd)` before close+rename.

### Item 20 — Corrupt state.json handling (SPEC §9.5)

Current: falls back to defaults silently.

Required: when load fails or JSON is invalid:
- Load `_DEFAULTS`.
- Publish a critical notification on the notifications topic: `"state.json missing or corrupt — loaded defaults, awaiting HA schedule"`.
- Polarity defaults to `"forward"`, accumulator to 0.
- Log to syslog as ERROR.

---

## Phase 5 — Service Mode and SC corrections

### Item 21 — SC continues during Service Mode (SPEC §8.4)

Currently SC counter is gated by `not _service_mode`. Per spec, SC continues normally during service mode (its counter ticks per its own rules — cell-on time). Remove the service_mode gate from SC tick logic.

### Item 22 — Cell rejection rules apply in Service Mode (SPEC §8.4)

Service Mode does not bypass §3.4 rejection rules or §4.8 SC rejection rules. Verify all four (flow, pump_stable, fault, plus service mode override path) remain active when in service mode. After Items 3, 4, and 6, audit that none of those reject paths are short-circuited by `_service_mode`.

---

## Phase 6 — MQTT topic restructure + HA dashboard fixes (paired changes)

This phase is invasive. Every sat4 topic change has a matching HA edit. Both must ship together. HA is reachable on Jarvis at `/mnt/ha-config/`.

**Workflow for the entire phase:**

1. Before any code changes, scan HA config for all references to current sat4 topics:
   ```bash
   cd /mnt/ha-config
   grep -rn "jarvis/pool/TudorPool" --include="*.yaml" --include="*.yml" --include="*.json" .
   grep -rn "jarvis/pool/TudorPool" .storage/ 2>/dev/null
   ```
   Save the output. Every line is a place that needs editing when the corresponding topic is renamed.

2. Build a topic translation table (Item 24 below provides the canonical version) — verify every old topic appearing in HA is on the left side. If HA references a topic not in the table, stop and ask.

3. Make sat4 + HA edits in the same commit boundary. Do not push sat4 changes to production until HA has matching edits staged.

4. Test in a maintenance window: stop sat4 service, deploy both sets of changes, reload HA YAML (`Developer Tools → YAML → All YAML configuration`), restart sat4, verify entities populate.

5. Rollback path: keep the dual-publish bridge (Item 23) running for 24 hours after cutover. If anything breaks, the old topics are still being published.

### Item 23 — Topic prefix rename with dual-publish bridge

Current prefix: `jarvis/pool/TudorPool/`
Target prefix: `pool/`

Strategy:
- Introduce `MQTT_TOPIC_PREFIX` config value, default still `jarvis/pool/TudorPool`.
- Route all publish/subscribe through `f"{prefix}/{topic_leaf}"`.
- Add `MQTT_BRIDGE_OLD_PREFIX` flag — when True, every publish also goes to the old prefix; subscribes listen on both.
- Deploy with bridge enabled, change default prefix to `pool`, verify HA receives on both. After 24 hours, set bridge to False.

### Item 24 — Topic name alignment (canonical translation table)

For each renamed topic, both sat4 code AND HA config must change.

#### State topics

| Old (full path) | New (full path) | HA edit required |
|---|---|---|
| `jarvis/pool/TudorPool/pump/power_on` | `pool/pump/state` | Yes |
| `jarvis/pool/TudorPool/pump/speed` | `pool/pump/output_percent` | Yes |
| (new) | `pool/pump/stable` | New entity (binary_sensor) |
| (new) | `pool/pump/countdown` | New entity (sensor, seconds) |
| `jarvis/pool/TudorPool/cell/state` | split into `pool/cell/on` (enable toggle, bool) AND `pool/cell/gate_state` (bool) | Yes — two separate entities now |
| `jarvis/pool/TudorPool/cell/output` | `pool/cell/output_percent` | Yes |
| `jarvis/pool/TudorPool/cell/actual_duty` | `pool/cell/runtime_percent` | Yes |
| (new) | `pool/cell/countdown` | New entity (sensor, seconds) |
| `jarvis/pool/TudorPool/cell/polarity` | `pool/cell/polarity_direction` | Yes |
| `jarvis/pool/TudorPool/cell/polarity_on_time_s` | `pool/cell/polarity_accumulator` | Yes |
| `jarvis/pool/TudorPool/sensors/cell_current` | `pool/cell/current_amps` | Yes (moves out of `sensors/` namespace) |
| (new) | `pool/cell/voltage` | New entity (sensor) |
| `jarvis/pool/TudorPool/cell/super_chlorinate` | `pool/sc/active` | Yes |
| `jarvis/pool/TudorPool/cell/super_chlorinate_remaining_s` | `pool/sc/remaining` | Yes |
| `jarvis/pool/TudorPool/system/service_mode` | `pool/service_mode` | Yes |
| (new) | `pool/fault/state` | New entity (sensor, string) |
| `jarvis/pool/TudorPool/sensors/conductivity` | `pool/sensors/ec` | Yes |
| `jarvis/pool/TudorPool/sensors/water_temp` | `pool/sensors/water_temp` | Prefix only |
| `jarvis/pool/TudorPool/sensors/air_temp` | `pool/sensors/air_temp` | Prefix only |
| `jarvis/pool/TudorPool/sensors/flow` | `pool/sensors/flow` | Prefix only |
| `jarvis/pool/TudorPool/system/cpu` | `pool/system/cpu` | Prefix only |
| `jarvis/pool/TudorPool/system/temp` | `pool/system/temp` | Prefix only |
| `jarvis/pool/TudorPool/system/memory` | `pool/system/memory` | Prefix only |
| `jarvis/pool/TudorPool/system/disk` | `pool/system/disk` | Prefix only |
| `jarvis/pool/TudorPool/system/wifi_signal` | `pool/system/wifi_signal` | Prefix only |
| `jarvis/pool/TudorPool/system/uptime` | `pool/system/uptime` | Prefix only |
| `jarvis/pool/TudorPool/system/status` | `pool/lwt` | Yes |
| (new) | `pool/notifications` | New entity (sensor for notifications) |

#### Command topics

| Old | New | HA edit required |
|---|---|---|
| `jarvis/pool/TudorPool/pump/power_on/set` | `pool/pump/set` | Yes |
| `jarvis/pool/TudorPool/pump/speed/set` | `pool/pump/output_percent/set` | Yes |
| `jarvis/pool/TudorPool/cell/set` | `pool/cell/on/set` | Yes |
| `jarvis/pool/TudorPool/cell/output/set` | `pool/cell/output_percent/set` | Yes |
| `jarvis/pool/TudorPool/cell/super_chlorinate/set` | `pool/sc/set` | Yes |
| `jarvis/pool/TudorPool/system/service_mode/set` | `pool/service_mode/set` | Yes |
| (new) | `pool/fault/reset` | New button entity |

#### HA edit procedure

For each row marked "Yes" or "Prefix only" or "New entity":

1. Locate every reference in `/mnt/ha-config/` (use the grep results from the workflow Step 1).
2. Update the `state_topic`, `command_topic`, `availability_topic`, etc. in the matching MQTT entity definition to the new path.
3. For "split" entries (the `cell/state` row): remove the original entity, define two new entities (`cell/on` as a switch, `cell/gate_state` as a binary_sensor).
4. For "New entity" rows: add new MQTT entity definitions in the appropriate include file (likely `includes/pool-control.yaml`).
5. Find every reference in dashboards (Lovelace YAML and `.storage/lovelace*` JSON) and update entity IDs if any changed.
6. Reload HA YAML when sat4 is publishing on the new topics.

### Item 24a — Restore Cell Output slider on dashboard

The Cell Output slider has gone missing from the pool dashboard. It must be restored.

Layout requirement:
- Cell Power button and Cell Output slider sit side-by-side as peers.
- Cell Power resizes down from its current oversized state to match the size of Cell Output, Actual Output, and the other small status tiles.
- The Cell Output slider entity is the new `pool/cell/output_percent` MQTT number entity defined in Item 24 above. It is bidirectional per SPEC §3.2 — both indicator and input.

Procedure:
1. Locate the pool dashboard YAML in `/mnt/ha-config/` (likely `includes/pool-control.yaml` or referenced from `configuration.yaml` under `lovelace.dashboards`).
2. Confirm the MQTT number entity for `pool/cell/output_percent` exists (defined in Item 24).
3. Edit the dashboard layout: place a slider/number card for the cell output entity adjacent to the Cell Power tile. Resize Cell Power to the smaller tile dimensions used by Actual Output, Polarity, etc.
4. Before saving, produce a YAML diff showing the dashboard changes. Print the before/after layout for the pool tile group. User reviews before commit.
5. After save, reload Lovelace and verify visually that Cell Power and Cell Output are now peer-sized and adjacent.

### Item 24b — HA edit summary

At the end of Phase 6, print a diff summary listing:
- Every HA file edited.
- Every entity created, renamed, or deleted.
- Every dashboard card touched.
- The before/after dashboard layout for the pool tile group (Cell Power + Cell Output positioning).

User reviews before final cutover.

### Item 25 — Publish cadence (SPEC §10.4)

Three cadence groups:
- **15-second**: `pool/cell/current_amps`, `pool/cell/voltage`, `pool/cell/gate_state`, `pool/cell/polarity_direction`.
- **60-second**: `pool/cell/runtime_percent`, `pool/cell/polarity_accumulator`, all `pool/sensors/*`, all `pool/system/*`.
- **On-change**: everything else.

Currently the old `cell/state` publishes every 1s — fix to on-change after the cell/on vs cell/gate_state split. Sensors at 30s → move to 60s. Add 15s group.

---

## Phase 7 — Final reconciliation

### Item 26 — Entity inventory reconciliation

After all topic renames, audit the actual published topic count. Memory note says "19 published, 2 tombstoned" but the spec list is longer. Determine:
- Which of the spec's listed topics are now published.
- Which were in the old set but should be tombstoned (after the 24h dual-publish window from Item 23 closes).
- Which spec topics still aren't published (gaps to close).

Produce `docs/MQTT_INVENTORY.md` with the final inventory.

Also produce `docs/HA_ENTITY_MAP.md` listing every HA entity ID that maps to a sat4 topic. This document lives alongside the spec and gets updated whenever topics change in the future.

---

## Done criteria

- All FAIL items in audit are PASS.
- All GAP items are PASS.
- UNCLEAR items either confirmed PASS or moved to FAIL with a follow-up.
- Re-run the audit (read SPEC.md, compare to code) and report new totals.
- HA dashboards still functional. No entities showing as `unavailable` for more than the 24h dual-publish window.
- Cell Power and Cell Output are visible and peer-sized on the dashboard.

After remediation, the system should refuse to do something dangerous before it does it — not after a fault has already occurred. That is the core ask.
