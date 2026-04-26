# MQTT Topic Inventory

**Topic prefix**: `pool/` (MQTT_TOPIC_PREFIX env var, default `pool`)  
**Bridge prefix**: `jarvis/pool/TudorPool/` (MQTT_BRIDGE_OLD_PREFIX, set during 24h cutover window)  
**LWT**: `pool/lwt`  → `"online"` / `"offline"` (retained, QoS 1)

---

## State topics (published by sat4)

### Pump

| Topic | Type | Cadence | Retain | Notes |
|---|---|---|---|---|
| `pool/lwt` | string | on-connect/disconnect | yes | LWT; "online"/"offline" |
| `pool/pump/state` | "ON"/"OFF" | on-change | yes | User-requested pump power |
| `pool/pump/output_percent` | 0–100 | on-change | yes | Target speed % |
| `pool/pump/running` | "ON"/"OFF" | on-change | yes | Pump actually spinning |
| `pool/pump/stable` | "ON"/"OFF" | on-change | yes | pump_stable flag (SPEC §2.11) |
| `pool/pump/countdown` | int s | on-change | no | Pump-stable countdown |
| `pool/pump/preload_active` | "ON"/"OFF" | ~500ms | no | Priming in progress |
| `pool/pump/preload_remaining_s` | int s | ~500ms | no | Priming countdown |
| `pool/pump/rpm` | int RPM | ~500ms | no | EcoStar RS-485 telemetry |
| `pool/pump/power` | float W | ~500ms | no | EcoStar RS-485 telemetry |
| `pool/pump/boot_grace_remaining_s` | int s | 60s + on-reject | no | Informational; not in discovery |

### Cell

| Topic | Type | Cadence | Retain | Notes |
|---|---|---|---|---|
| `pool/cell/on` | "ON"/"OFF" | on-change | yes | User-requested cell enable |
| `pool/cell/gate_state` | "ON"/"OFF" | on-change | yes | Physical relay state |
| `pool/cell/output_percent` | 0–100 | on-change | yes | Effective duty (SPEC §3.7) |
| `pool/cell/runtime_percent` | 0–100 | 60s | yes | Rolling 30-min ACS712 duty |
| `pool/cell/actual_duty_confidence` | 0–100 | 60s | yes | 0%=just reset, 100%=full window |
| `pool/cell/countdown` | int s | 60s + on-reject | no | Boot grace countdown |
| `pool/cell/interlock` | "ON"/"OFF" | 1s | no | safety.is_interlock_ok() |
| `pool/cell/cant_enable_reason` | string | on-change | yes | Empty string = no block |
| `pool/cell/polarity_direction` | "forward"/"reverse" | 15s | yes | Current polarity |
| `pool/cell/polarity_accumulator` | int s | 60s | no | On-time this polarity period |
| `pool/cell/polarity_accumulated_s` | "H:MM" | 60s | no | Formatted accumulated time |
| `pool/cell/polarity_remaining_s` | "H:MM" | 60s | no | Formatted time until auto-reverse |
| `pool/cell/current_amps` | float A | 15s | no | ACS712 cell current |

### Super Chlorinate

| Topic | Type | Cadence | Retain | Notes |
|---|---|---|---|---|
| `pool/sc/active` | "ON"/"OFF" | on-change | yes | SC running |
| `pool/sc/remaining` | int s | on-change + 60s | yes | Cell-on time remaining |

### Faults + Notifications

| Topic | Type | Cadence | Retain | Notes |
|---|---|---|---|---|
| `pool/fault/state` | string | on-change | yes | Fault name or "none" |
| `pool/notifications` | JSON | on-event | no | `{"severity":…,"message":…}` |
| `pool/events/cell_trip` | JSON | on-fault | no | `{"reason":…,"pump_speed":…,"flow_ok":…}` — HA automation trigger |

### Service Mode

| Topic | Type | Cadence | Retain | Notes |
|---|---|---|---|---|
| `pool/service_mode` | "ON"/"OFF" | on-change | yes | Service mode active |
| `pool/system/mode` | "on"/"off"/"service" | on-change | yes | System operating mode |

### Sensors (60s)

| Topic | Type | Retain | Notes |
|---|---|---|---|
| `pool/sensors/water_temp` | float °F | no | ADS1115 AIN2 — not connected |
| `pool/sensors/air_temp` | float °F | no | ADS1115 AIN1 thermistor |
| `pool/sensors/pump_current` | float A | no | Derived: pump watts / 230V |
| `pool/sensors/ec` | float µS/cm | no | Atlas EZO-EC conductivity |
| `pool/sensors/flow` | "ON"/"OFF" | no | GPIO17 flow switch |
| `pool/fans/state` | "ON"/"OFF" | yes | Enclosure cooling fans |

### System Health (60s)

| Topic | Type | Retain | Notes |
|---|---|---|---|
| `pool/system/cpu` | float % | no | psutil cpu_percent |
| `pool/system/temp` | float °F | no | /sys/class/thermal SoC temp |
| `pool/system/memory` | float % | no | psutil virtual_memory |
| `pool/system/disk` | float % | no | psutil disk_usage("/") |
| `pool/system/wifi_signal` | int dBm | no | iwconfig wlan0 |
| `pool/system/uptime` | int s | no | psutil boot_time delta |
| `pool/system/power_recovery` | JSON | yes | Post-restart resume event |

---

## Command topics (subscribed by sat4)

| Topic | Payload | Handler |
|---|---|---|
| `pool/pump/set` | "ON"/"OFF" | handle_pump_power_set |
| `pool/pump/output_percent/set` | 0–100 | handle_speed_set |
| `pool/cell/on/set` | "ON"/"OFF" | handle_cell_set |
| `pool/cell/output_percent/set` | 0–100 | handle_output_set |
| `pool/cell/cmd/polarity` | "toggle" | handle_polarity_toggle |
| `pool/sc/set` | "ON"/"OFF" | handle_super_chlorinate_set |
| `pool/service_mode/set` | "ON"/"OFF" | handle_service_mode_set |
| `pool/fault/reset` | any | handle_fault_reset |

---

## Tombstoned entities (empty payload sent on connect to clean broker)

| Type | unique_id | Reason |
|---|---|---|
| binary_sensor | jarvis_pool_cell_allowed | v1 duplicate of cell_interlock |
| sensor | jarvis_pool_pump_watts | v1 duplicate of pump_power |
| number | jarvis_pool_pump_set_rpm | v1 RPM control — removed |
| sensor | jarvis_pool_spa_temp | no spa in system |

---

## Spec gaps (not yet implemented)

| Topic | Reason |
|---|---|
| `pool/cell/voltage` | No HW channel — ADS1115 AIN0-3 are all assigned (polarity verify, air temp, water temp, ACS712) |
| `pool/cell/polarity_voltage` | AIN0 polarity verify read but not published; thresholds uncalibrated |
