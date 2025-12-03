# Setting Mismatch Detection Plan (v2.29.0)

## Goal
Extend manual override detection beyond power state to include:
- **Temperature** changes
- **Fan speed** changes
- **Swing mode** changes
- **HVAC mode** changes

Detect changes from BOTH:
1. HA UI (user manually adjusts via dashboard)
2. Physical remote (user changes settings on AC remote)

---

## Architecture Overview

### Current State
- `expected_pow` parameter detects power state mismatch at command-time (BRP069)
- Poll-based detection catches power changes between polls (BRP084, BRP069)
- Event `daikin_physical_remote_override` fired when override detected

### Proposed Extension
Extend `expected_pow` to `expected_settings` dict containing all tracked settings:
```python
expected_settings = {
    'pow': '1',
    'stemp': '24',
    'f_rate': 'A',  # Auto
    'f_dir': '3',   # 3D swing
    'mode': '3'     # Cool
}
```

---

## Implementation Plan

### Step 1: Extend pydaikin `set()` to Accept `expected_settings`

**File:** `pydaikin/daikin_brp069.py`

Modify `set()` method signature:
```python
async def set(self, settings, expected_pow=None, expected_settings=None):
```

Add mismatch detection after fetching current state:
```python
# After: current_val = await self._update_settings(settings)
mismatches = {}
if expected_settings:
    for key, expected_val in expected_settings.items():
        actual_val = current_val.get(key)
        if actual_val is not None and str(actual_val) != str(expected_val):
            mismatches[key] = {'expected': expected_val, 'actual': actual_val}
```

Return mismatches in result:
```python
return {
    'detected_power_off': detected_power_off,
    'current_val': current_val,
    'mismatches': mismatches,  # NEW
    'aborted': False
}
```

### Step 2: Add Pre-Fetch to BRP084 (Optional but Recommended)

**File:** `pydaikin/daikin_brp084.py`

BRP084 currently does NOT pre-fetch state before sending commands. Options:
1. **Add pre-fetch** - Enables command-time detection, adds ~0.5s latency
2. **Poll-only detection** - Rely on coordinator polling (10s delay)

**Recommendation:** Add pre-fetch for consistency. The 0.5s latency is acceptable for better override detection.

### Step 3: Update climate.py to Build and Pass `expected_settings`

**File:** `custom_components/daikin/climate.py`

In `_set()` method, build expected_settings from current known state:
```python
# Build expected_settings from current device state (not optimistic)
expected_settings = {}
if self._last_known_pow:
    expected_settings['pow'] = self._last_known_pow

# Add actual device values (what we expect device to have)
device_stemp = self.device.values.get('stemp')
if device_stemp:
    expected_settings['stemp'] = device_stemp

device_f_rate = self.device.values.get('f_rate')
if device_f_rate:
    expected_settings['f_rate'] = device_f_rate

device_f_dir = self.device.values.get('f_dir')
if device_f_dir:
    expected_settings['f_dir'] = device_f_dir

device_mode = self.device.values.get('mode')
if device_mode:
    expected_settings['mode'] = device_mode

# Pass to pydaikin
result = await self.device.set(values, expected_settings=expected_settings)
```

### Step 4: Handle Mismatches and Fire Extended Event

**File:** `custom_components/daikin/climate.py`

After `device.set()` returns, check for mismatches:
```python
# Check for setting mismatches (physical remote changed settings)
if result and result.get('mismatches'):
    mismatches = result['mismatches']
    _LOGGER.warning(
        "SETTING MISMATCH DETECTED: Device settings differ from expected. "
        "entity=%s, mismatches=%s",
        self.entity_id, mismatches
    )

    # Fire detailed event for blueprint
    self.hass.bus.async_fire(
        "daikin_settings_mismatch",
        {
            "entity_id": self.entity_id,
            "device_name": self.name,
            "mismatches": mismatches,
            # Categorize the change type
            "change_type": self._categorize_mismatch(mismatches)
        }
    )
```

Add helper to categorize mismatch type:
```python
def _categorize_mismatch(self, mismatches: dict) -> str:
    """Categorize the type of setting mismatch."""
    if 'pow' in mismatches:
        return 'power'
    if 'stemp' in mismatches:
        return 'temperature'
    if 'f_rate' in mismatches:
        return 'fan_speed'
    if 'f_dir' in mismatches:
        return 'swing_mode'
    if 'mode' in mismatches:
        return 'hvac_mode'
    return 'unknown'
```

### Step 5: Add Poll-Based Setting Mismatch Detection

**File:** `custom_components/daikin/climate.py`

In `_handle_coordinator_update()`, track and compare settings:
```python
# Track last known settings for mismatch detection
if not hasattr(self, '_last_known_settings'):
    self._last_known_settings = {}

current_settings = {
    'stemp': self.device.values.get('stemp'),
    'f_rate': self.device.values.get('f_rate'),
    'f_dir': self.device.values.get('f_dir'),
    'mode': self.device.values.get('mode'),
}

# Detect unexpected changes (not from our commands)
if self._last_known_settings and self._optimistic_set_time is None:
    # No pending optimistic updates = we didn't initiate this change
    for key, current_val in current_settings.items():
        last_val = self._last_known_settings.get(key)
        if last_val and current_val and str(current_val) != str(last_val):
            _LOGGER.warning(
                "POLL DETECTED SETTING CHANGE: %s changed from %s to %s",
                key, last_val, current_val
            )
            # Fire event for blueprint
            self.hass.bus.async_fire(
                "daikin_settings_mismatch",
                {
                    "entity_id": self.entity_id,
                    "device_name": self.name,
                    "mismatches": {key: {'expected': last_val, 'actual': current_val}},
                    "change_type": self._categorize_mismatch({key: True})
                }
            )

self._last_known_settings = current_settings
```

---

## Event Format

### New Event: `daikin_settings_mismatch`
```yaml
event_type: daikin_settings_mismatch
data:
  entity_id: climate.living_room_a_c
  device_name: Living Room AC
  mismatches:
    stemp:
      expected: "24"
      actual: "22"
    f_rate:
      expected: "A"
      actual: "5"
  change_type: temperature  # or fan_speed, swing_mode, hvac_mode, power
```

### Existing Event: `daikin_physical_remote_override`
Keep existing event for power-off detection (backwards compatibility).

---

## Blueprint Integration

The blueprint can listen for both events:
```yaml
trigger:
  - platform: event
    event_type: daikin_physical_remote_override
    event_data:
      entity_id: !input climate_entity
  - platform: event
    event_type: daikin_settings_mismatch
    event_data:
      entity_id: !input climate_entity
```

---

## Files to Modify

| File | Change | Priority |
|------|--------|----------|
| `pydaikin/daikin_brp069.py` | Add `expected_settings` parameter and mismatch detection | HIGH |
| `pydaikin/daikin_brp084.py` | Add pre-fetch and `expected_settings` support | HIGH |
| `climate.py` | Build expected_settings, handle mismatches, fire events | HIGH |
| `pyproject.toml` | Bump version to 2.29.0 | HIGH |
| `manifest.json` | Update commit hash | HIGH |

---

## Version Release Workflow

1. Implement changes in pydaikin (daikin_brp069.py, daikin_brp084.py)
2. Bump version in `pyproject.toml` (2.28.0 → 2.29.0)
3. Commit and push to GitHub
4. Create git tag `v2.29.0`
5. Update climate.py with new event handling
6. Update `manifest.json` with new commit hash
7. Copy files to Y: drive
8. Restart HA

---

## Testing Plan

1. **BRP069 command-time detection:** Change temp on physical remote, then send command from HA → should detect mismatch
2. **BRP084 detection:** Same test for Living Room AC
3. **Poll-based detection:** Change setting on remote, wait for poll → should fire event
4. **Backwards compatibility:** Verify existing power-off detection still works
5. **Blueprint integration:** Verify blueprint receives and handles new events

---

## Open Questions for Further Research

1. **BRP084 Pre-Fetch Trade-off:** Should we add pre-fetch to BRP084 for command-time detection (adds ~0.5s latency) or rely solely on poll-based detection (10s delay)?

2. **False Positive Prevention:** How do we prevent false positives when:
   - Device rounds temperature values (e.g., 23.5 → 24)?
   - Fan mode has different representations (e.g., "Auto" vs "A")?

3. **HA UI Change Detection:** Currently the plan detects ALL mismatches. Should we distinguish between:
   - Changes from physical remote (fire override event)
   - Changes from HA UI by another user (don't fire override event)?

4. **Multiple Mismatches:** If user changes multiple settings at once, should we:
   - Fire one event with all mismatches?
   - Fire separate events for each mismatch?
   - Current plan: One event with all mismatches listed
