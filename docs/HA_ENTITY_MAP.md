# Home Assistant Entity Map

Maps every HA entity (by entity_id) to its sat4 MQTT topic.  
Entity IDs are derived from `unique_id` via HA auto-discovery and may be overridden by the user in HA.  
The `unique_id` is stable; if the entity_id shown below differs from what you see in HA, look it up by unique_id.

**Discovery prefix**: `homeassistant/` (HA_DISCOVERY_PREFIX)  
**Device**: `Jarvis Pool Controller` (identifier: `jarvis_pool`)

---

## Switches (bidirectional — state + command)

| HA Entity ID | unique_id | State Topic | Command Topic |
|---|---|---|---|
| `switch.jarvis_pool_controller_pump_power` | jarvis_pool_pump_power_on | `pool/pump/state` | `pool/pump/set` |
| `switch.jarvis_pool_controller_salt_cell` | jarvis_pool_cell | `pool/cell/on` | `pool/cell/on/set` |
| `switch.jarvis_pool_controller_super_chlorinate` | jarvis_pool_super_chlorinate | `pool/sc/active` | `pool/sc/set` |
| `switch.jarvis_pool_controller_service_mode` | jarvis_pool_service_mode | `pool/service_mode` | `pool/service_mode/set` |

## Numbers (bidirectional — state + command)

| HA Entity ID | unique_id | State Topic | Command Topic | Range |
|---|---|---|---|---|
| `number.jarvis_pool_controller_pool_pump_speed` | jarvis_pool_pump_speed | `pool/pump/output_percent` | `pool/pump/output_percent/set` | 0–100 % |
| `number.jarvis_pool_controller_cell_output` | jarvis_pool_cell_output | `pool/cell/output_percent` | `pool/cell/output_percent/set` | 0–100 % |

## Buttons

| HA Entity ID | unique_id | Command Topic | Payload |
|---|---|---|---|
| `button.jarvis_pool_controller_salt_cell_polarity_toggle` | jarvis_pool_cell_polarity_toggle | `pool/cell/cmd/polarity` | "toggle" |
| `button.jarvis_pool_controller_pool_fault_reset` | jarvis_pool_fault_reset | `pool/fault/reset` | "reset" |

## Binary Sensors

| HA Entity ID | unique_id | State Topic | On Payload |
|---|---|---|---|
| `binary_sensor.jarvis_pool_controller_pool_controller_online` | jarvis_pool_controller_online | `pool/lwt` | "online" |
| `binary_sensor.jarvis_pool_controller_pump_preload_active` | jarvis_pool_pump_preload_active | `pool/pump/preload_active` | "ON" |
| `binary_sensor.jarvis_pool_controller_pump_stable` | jarvis_pool_pump_stable | `pool/pump/stable` | "ON" |
| `binary_sensor.jarvis_pool_controller_pump_running` | jarvis_pool_pump_running | `pool/pump/running` | "ON" |
| `binary_sensor.jarvis_pool_controller_pool_flow` | jarvis_pool_flow | `pool/sensors/flow` | "ON" |
| `binary_sensor.jarvis_pool_controller_cell_interlock` | jarvis_pool_cell_interlock | `pool/cell/interlock` | "ON" |
| `binary_sensor.jarvis_pool_controller_cell_gate_state` | jarvis_pool_cell_gate_state | `pool/cell/gate_state` | "ON" |
| `binary_sensor.jarvis_pool_controller_pool_enclosure_fans` | jarvis_pool_fans | `pool/fans/state` | "ON" |

## Sensors

| HA Entity ID | unique_id | State Topic | Unit |
|---|---|---|---|
| `sensor.jarvis_pool_controller_pool_water_temperature` | jarvis_pool_water_temp | `pool/sensors/water_temp` | °F |
| `sensor.jarvis_pool_controller_pool_air_temperature` | jarvis_pool_air_temp | `pool/sensors/air_temp` | °F |
| `sensor.jarvis_pool_controller_pump_rpm` | jarvis_pool_pump_rpm | `pool/pump/rpm` | RPM |
| `sensor.jarvis_pool_controller_pump_power` | jarvis_pool_pump_power | `pool/pump/power` | W |
| `sensor.jarvis_pool_controller_pool_pump_current` | jarvis_pool_current | `pool/sensors/pump_current` | A |
| `sensor.jarvis_pool_controller_salt_cell_current` | jarvis_pool_cell_current | `pool/cell/current_amps` | A |
| `sensor.jarvis_pool_controller_pool_conductivity` | jarvis_pool_conductivity | `pool/sensors/ec` | µS/cm |
| `sensor.jarvis_pool_controller_pump_preload_remaining` | jarvis_pool_pump_preload_remaining_s | `pool/pump/preload_remaining_s` | s |
| `sensor.jarvis_pool_controller_pump_stable_countdown` | jarvis_pool_pump_stable_countdown | `pool/pump/countdown` | s |
| `sensor.jarvis_pool_controller_cell_boot_grace_countdown` | jarvis_pool_cell_countdown | `pool/cell/countdown` | s |
| `sensor.jarvis_pool_controller_salt_cell_polarity` | jarvis_pool_cell_polarity | `pool/cell/polarity_direction` | — |
| `sensor.jarvis_pool_controller_salt_cell_polarity_timer` | jarvis_pool_polarity_on_time | `pool/cell/polarity_accumulator` | s |
| `sensor.jarvis_pool_controller_salt_cell_polarity_accumulated` | jarvis_pool_polarity_accumulated | `pool/cell/polarity_accumulated_s` | — |
| `sensor.jarvis_pool_controller_salt_cell_polarity_remaining` | jarvis_pool_polarity_remaining | `pool/cell/polarity_remaining_s` | — |
| `sensor.jarvis_pool_controller_cell_actual_duty` | jarvis_pool_cell_actual_duty | `pool/cell/runtime_percent` | % |
| `sensor.jarvis_pool_controller_cell_actual_confidence` | jarvis_pool_cell_actual_confidence | `pool/cell/actual_duty_confidence` | % |
| `sensor.jarvis_pool_controller_super_chlorinate_remaining` | jarvis_pool_super_chlorinate_remaining | `pool/sc/remaining` | s |
| `sensor.jarvis_pool_controller_pool_system_mode` | jarvis_pool_system_mode | `pool/system/mode` | — |
| `sensor.jarvis_pool_controller_pool_fault_state` | jarvis_pool_fault_state | `pool/fault/state` | — |
| `sensor.jarvis_pool_controller_pool_notifications` | jarvis_pool_notifications | `pool/notifications` | — |
| `sensor.jarvis_pool_controller_pool_controller_cpu` | jarvis_pool_system_cpu_percent | `pool/system/cpu` | % |
| `sensor.jarvis_pool_controller_pool_controller_cpu_temperature` | jarvis_pool_system_cpu_temp | `pool/system/temp` | °F |
| `sensor.jarvis_pool_controller_pool_controller_memory` | jarvis_pool_system_memory_percent | `pool/system/memory` | % |
| `sensor.jarvis_pool_controller_pool_controller_disk` | jarvis_pool_system_disk_percent | `pool/system/disk` | % |
| `sensor.jarvis_pool_controller_pool_controller_wi_fi_signal` | jarvis_pool_system_wifi_signal | `pool/system/wifi_signal` | dBm |
| `sensor.jarvis_pool_controller_pool_controller_uptime` | jarvis_pool_system_uptime_seconds | `pool/system/uptime` | s |

---

## HA Automations that reference raw MQTT topics

These automations trigger on MQTT topics directly (not entity state). Update whenever topics change.

| File | Automation | MQTT Topic |
|---|---|---|
| `includes/automation_sources/pool_notifications.yaml` | Pool Cell Safety Trip Notification | `pool/events/cell_trip` |

## HA Automations that reference entity IDs

Entity IDs are stable (based on unique_id). These automations do NOT need updates when topics change.

| File | Automation | Entities Used |
|---|---|---|
| `includes/automation_sources/pool_schedule.yaml` | Morning Start | `switch.jarvis_pool_controller_pump_power`, `number.jarvis_pool_controller_pool_pump_speed`, `switch.jarvis_pool_controller_salt_cell`, `number.jarvis_pool_controller_cell_output` |
| `includes/automation_sources/pool_schedule.yaml` | Skim End | `switch.jarvis_pool_controller_pump_power`, `number.jarvis_pool_controller_pool_pump_speed` |
| `includes/automation_sources/pool_schedule.yaml` | Evening Stop | `switch.jarvis_pool_controller_salt_cell`, `switch.jarvis_pool_controller_pump_power` |
| `includes/automation_sources/pool_schedule.yaml` | Continuous Enforcement | `number.jarvis_pool_controller_pool_pump_speed`, `number.jarvis_pool_controller_cell_output` |
| `includes/automation_sources/pool_schedule.yaml` | SC Start/End | `switch.jarvis_pool_controller_super_chlorinate`, `switch.jarvis_pool_controller_pump_power`, `number.jarvis_pool_controller_pool_pump_speed` |

---

*Generated after Items 23–26 (Phase 6 MQTT restructure). Update this file whenever topics or entities change.*
