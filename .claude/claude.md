# pydaikin Development Notes

## CRITICAL: Use `cp` for File Copy Operations (HIGH PRIORITY!)

**ALWAYS use `cp` command for copying files, NOT `copy` or `cmd /c copy`.**

```bash
# CORRECT - Use this:
cp "source/path/file.py" "Y:/destination/path/file.py"

# WRONG - Never use these:
copy "source" "dest"           # Windows copy doesn't work reliably
cmd /c copy "source" "dest"    # Same issue
```

This applies to ALL file copy operations, especially when deploying to Y: drive.

---

## Physical Remote Override Detection System (v2.33.0)

### Purpose
When automation is controlling the AC and a user presses the **physical IR remote** to turn it OFF (or ON), the system detects this and activates **Override Mode** - pausing all automation for that room.

### How It Works (The Core Logic)

**Key Principle:** `_last_known_pow` must ONLY reflect what the device has CONFIRMED, not what we asked it to do.

1. **Automation sends ON command:**
   - `_last_on_command_time` is set (for 30s protection window)
   - `_last_known_pow` is **NOT changed** (stays at previous confirmed value)

2. **Device confirms ON:**
   - Coordinator polls device, sees `pow=1`
   - `_last_known_pow` is updated to `'1'`

3. **User presses OFF on physical remote:**
   - Device turns off
   - Coordinator polls, sees `pow=0`

4. **Override Detection fires:**
   - Sees: `_last_known_pow='1'` (confirmed ON) but `current_pow='0'` (now OFF)
   - Check: Did we send ON recently? Yes, but device already confirmed ON
   - **Result: Override fires!** → `daikin_physical_remote_override` event

### Critical Variables in `climate.py`

| Variable | Purpose | Updated By |
|----------|---------|------------|
| `_last_known_pow` | Last CONFIRMED device power state | Coordinator ONLY |
| `_last_on_command_time` | When we sent an ON command | `_set()` method |
| `_last_off_command_time` | When we sent an OFF command | `_set()` method |
| `_last_override_event_time` | Debounce duplicate events | Override detection |

### Protection Windows (Prevent False Positives)

The 30-second protection window prevents false overrides when:
- We send ON but device is slow to turn on
- We send OFF but device is slow to turn off

**Key insight:** Only skip detection if device hasn't confirmed our command yet.

```python
# Example: We sent ON, device is slow
if _last_on_command_time < 30s ago:
    if _last_known_pow == '0':  # Device hasn't confirmed ON yet
        skip detection  # Might be slow device, not remote
    else:  # _last_known_pow == '1', device confirmed ON
        fire override!  # This is a REAL remote press
```

### What NOT To Do

1. **DO NOT set `_last_known_pow` in `_set()`** - This caused remote detection to never fire because it thought device already confirmed
2. **DO NOT remove the 30s protection window** - Causes false overrides on slow devices
3. **DO NOT check `expected_pow` in pydaikin** - Too many race conditions, use coordinator polling only

### Blueprint Integration

The blueprint listens for `daikin_physical_remote_override` event and:
1. Sets `control_mode` to `Override`
2. Records timestamp in `input_datetime.climate_override_time_<room>`
3. Pauses all automation for configured timeout (default 2 hours)

### Testing Remote Override

1. Put room in Smart mode with presence
2. Wait for automation to turn AC ON
3. Wait ~15 seconds for device to confirm ON (check logs)
4. Press OFF on physical remote
5. Override should fire within 10 seconds (next coordinator poll)

---

## SSH Access to Home Assistant (CRITICAL - USE THIS!)

**SSH is configured and working. Use this for all HA operations:**

```bash
# The magic incantation to run ha commands via SSH:
ssh -o StrictHostKeyChecking=no hassio@homeassistant.local 'for f in /run/s6/container_environment/*; do export "$(basename $f)"="$(cat $f)"; done; <COMMAND>'
```

**Why the weird `for f in ...` stuff?**
The SSH add-on doesn't automatically load the `SUPERVISOR_TOKEN` environment variable. The loop sources it from `/run/s6/container_environment/` so `ha` commands work.

### Common SSH Commands

```bash
# Reload automations (for blueprint changes - NO restart needed!)
ssh -o StrictHostKeyChecking=no hassio@homeassistant.local 'for f in /run/s6/container_environment/*; do export "$(basename $f)"="$(cat $f)"; done; curl -s -X POST -H "Authorization: Bearer $SUPERVISOR_TOKEN" -H "Content-Type: application/json" http://supervisor/core/api/services/automation/reload'

# Restart Home Assistant (only needed for integration/pydaikin changes)
ssh -o StrictHostKeyChecking=no hassio@homeassistant.local 'for f in /run/s6/container_environment/*; do export "$(basename $f)"="$(cat $f)"; done; ha core restart'

# View logs (live follow)
ssh -o StrictHostKeyChecking=no hassio@homeassistant.local 'for f in /run/s6/container_environment/*; do export "$(basename $f)"="$(cat $f)"; done; ha core logs --follow'

# View recent logs
ssh -o StrictHostKeyChecking=no hassio@homeassistant.local 'for f in /run/s6/container_environment/*; do export "$(basename $f)"="$(cat $f)"; done; ha core logs 2>&1 | tail -200'

# Get entity state via API
ssh -o StrictHostKeyChecking=no hassio@homeassistant.local 'for f in /run/s6/container_environment/*; do export "$(basename $f)"="$(cat $f)"; done; curl -s -H "Authorization: Bearer $SUPERVISOR_TOKEN" -H "Content-Type: application/json" http://supervisor/core/api/states/climate.master_bedroom_a_c'

# Get entity history
ssh -o StrictHostKeyChecking=no hassio@homeassistant.local 'for f in /run/s6/container_environment/*; do export "$(basename $f)"="$(cat $f)"; done; curl -s -H "Authorization: Bearer $SUPERVISOR_TOKEN" -H "Content-Type: application/json" "http://supervisor/core/api/history/period/2025-11-28T00:30:00+00:00?filter_entity_id=climate.master_bedroom_a_c"'

# Check HA config
ssh -o StrictHostKeyChecking=no hassio@homeassistant.local 'for f in /run/s6/container_environment/*; do export "$(basename $f)"="$(cat $f)"; done; ha core check'
```

### SSH Add-on Configuration

- **Username:** hassio
- **Password:** hassio
- **Port:** 22
- **SSH Key:** Already configured in add-on (ed25519 key from this machine)

### Troubleshooting SSH

If you get `401: Unauthorized`:
- The `SUPERVISOR_TOKEN` isn't being loaded
- Make sure to use the `for f in /run/s6/container_environment/*` wrapper

If connection fails:
- Try `homeassistant.local` or the IP `192.168.50.45`
- Check SSH add-on is running in HA

### Override Log Viewing

When manual override is triggered, the blueprint writes detailed logs with `[OVERRIDE_LOG]` prefix:

```bash
# View all override logs
ssh -o StrictHostKeyChecking=no hassio@homeassistant.local 'for f in /run/s6/container_environment/*; do export "$(basename $f)"="$(cat $f)"; done; ha core logs 2>&1 | grep "OVERRIDE_LOG"'
```

The log includes:
- **TRIGGER**: trigger ID, from/to states
- **CONTEXT**: user_id, parent_id (identifies automation vs manual)
- **INTEGRATION**: device type, expected HVAC/temp/fan, last command time, entity init time
- **ACTUAL**: current HVAC mode, temperature, fan, swing
- **LOGIC**: manual_override flag, control_mode, actual_ac_state, time since change
- **CHECKSUM**: actual vs stored checksums, state machine

---

## Project Structure

This repository is part of a two-repository system for the Daikin Home Assistant integration:

### Repository 1: pydaikin Library
**Repository**: https://github.com/Chris971991/pydaikin-2.8.0
- Core library for communicating with Daikin AC units
- Supports multiple firmware versions (BRP069, BRP072C, BRP084/2.8.0, AirBase, SkyFi)
- Used by Home Assistant's Daikin integration

### Repository 2: Home Assistant Daikin Integration
**Repository**: https://github.com/Chris971991/homeassistant-daikin-optimized
- Home Assistant integration using pydaikin
- Provides climate entities, sensors, and switches
- Optimized with optimistic updates for instant UI response

## Working on Both Repositories Together

When making changes to the Daikin integration, you often need to edit **both repositories in conjunction**:

1. **pydaikin changes** (this repo):
   - Add/modify device communication protocols
   - Add new properties or methods
   - Fix parsing issues
   - Example: Adding `inside_temperature` property for HA compatibility

2. **HA integration changes** (homeassistant-daikin-optimized):
   - Use the new pydaikin features
   - Update UI/UX
   - Add optimizations
   - Example: Using `device.inside_temperature` in climate entity

### Typical Workflow

```bash
# Working on pydaikin
cd C:\Users\Chris\Documents\pydaikin-2.8.0
# Make changes to pydaikin/daikin_brp084.py or other files
git add .
git commit -m "Add new feature"
git push

# Working on HA integration
cd C:\Users\Chris\Documents\homeassistant-daikin-optimized
# Make changes to custom_components/daikin/climate.py or other files
git add .
git commit -m "Use new pydaikin feature"
git push
```

### Version Management

- **pydaikin version**: Update in `pyproject.toml`
- **HA integration requirement**: Update in `custom_components/daikin/manifest.json`

When you add features to pydaikin:
1. Bump pydaikin version (e.g., 2.16.1 → 2.17.0)
2. Update HA integration's manifest.json to require new version

## Recent Changes

### Version 2.17.0
- Added compatibility properties for HA integration:
  - `inside_temperature` property
  - `target_temperature` property
  - `outside_temperature` property
- Optimized temperature clipping algorithm with "quick tries"

### HA Integration Optimizations
- Added optimistic state updates for instant UI response
- Removed redundant coordinator refresh calls (50% fewer HTTP requests)
- Performance improvement: 1-4s → <0.1s command response

## Testing

When testing changes across both repositories:

1. Install updated pydaikin locally:
   ```bash
   pip install -e C:\Users\Chris\Documents\pydaikin-2.8.0
   ```

2. Copy HA integration to Home Assistant:
   ```bash
   cp -r C:\Users\Chris\Documents\homeassistant-daikin-optimized\custom_components\daikin \
         /config/custom_components/
   ```

3. Restart Home Assistant and test

## Pull Requests

Both repositories may need PRs to their upstream projects:

1. **pydaikin**: Submit to https://github.com/fredrike/pydaikin
2. **HA Integration**: Submit to https://github.com/home-assistant/core

## Important Notes

- Always test changes with multiple firmware versions (old firmware + 2.8.0)
- Keep pydaikin changes backwards compatible
- Document breaking changes clearly
- Run tests before committing

## Local Paths

- pydaikin: `C:\Users\Chris\Documents\pydaikin-2.8.0`
- HA integration: `C:\Users\Chris\Documents\homeassistant-daikin-optimized`
- Custom daikin_2_8_0: `C:\Users\Chris\Documents\daikin_2_8_0` (deprecated, use optimized version)

## Blueprint Integration

The Daikin integration works in conjunction with the **Ultimate Climate Control Blueprint**:

- **Blueprint Location**: `C:\Users\Chris\Smart-Climate-Control-V5\Smart-Climate-Control\ultimate_climate_control.yaml`
- **Repository**: https://github.com/Chris971991/Smart-Climate-Control

### Current Feature: Physical Remote Override Detection (v6.2.0)

The integration and blueprint work together to detect when a user turns off the AC using the physical remote while automation is running:

1. **pydaikin** (`daikin_brp069.py`): The `set()` method returns `detected_power_off: True` when device reports `pow=0` but we're trying to set `pow=1`
2. **climate.py**: Checks this flag and fires `daikin_physical_remote_override` HA event
3. **Blueprint**: Listens for this event and immediately activates Override mode

### CRITICAL: Deployment Workflow

**ALWAYS edit files in local repos first, then copy to Y: drive for HA to pick up:**

```bash
# 1. Edit in local repos
# - pydaikin: C:\Users\Chris\Documents\pydaikin-2.8.0\pydaikin\*.py
# - climate.py: C:\Users\Chris\Documents\homeassistant-daikin-optimized\custom_components\daikin\climate.py
# - blueprint: C:\Users\Chris\Smart-Climate-Control-V5\Smart-Climate-Control\ultimate_climate_control.yaml

# 2. Copy to Y: drive (HA's config folder mounted as network share)
cp "C:\Users\Chris\Documents\homeassistant-daikin-optimized\custom_components\daikin\climate.py" "Y:\custom_components\daikin\climate.py"
cp "C:\Users\Chris\Smart-Climate-Control-V5\Smart-Climate-Control\ultimate_climate_control.yaml" "Y:\blueprints\automation\Chris971991\ultimate_climate_control.yaml"

# 3. Restart HA to pick up changes
```

**Y: Drive Structure:**
- `Y:\custom_components\daikin\` - Daikin integration files
- `Y:\blueprints\automation\Chris971991\` - Blueprint files
- `Y:\deps\lib\python3.13\site-packages\pydaikin\` - pydaikin library (installed by HA)

**DO NOT edit files directly on Y: drive** - always edit in local repos and copy over.

### When to Reload vs Restart

| Change Type | Action Required |
|-------------|-----------------|
| Blueprint YAML changes | **Reload automations** (fast, ~2 seconds) |
| Package YAML changes (helpers) | **Restart HA** (helpers need full reload) |
| Integration (climate.py, etc.) | **Restart HA** |
| pydaikin library | **Reinstall pydaikin + Restart HA** |

**Reload automations command:**
```bash
ssh -o StrictHostKeyChecking=no hassio@homeassistant.local 'for f in /run/s6/container_environment/*; do export "$(basename $f)"="$(cat $f)"; done; curl -s -X POST -H "Authorization: Bearer $SUPERVISOR_TOKEN" -H "Content-Type: application/json" http://supervisor/core/api/services/automation/reload'
```

### Releasing pydaikin Updates

**IMPORTANT:** You cannot just copy pydaikin files to Y: drive. HA installs pydaikin from GitHub based on the version in manifest.json. To deploy pydaikin changes:

1. **Bump version** in `pyproject.toml` (e.g., 2.24.0 → 2.25.0)
2. **Commit and push** to GitHub
3. **Create git tag** matching the version (e.g., `git tag v2.25.0 && git push --tags`)
4. **Update manifest.json** in both repos to reference new commit hash and version
5. **Copy manifest.json and climate.py** to Y: drive
6. **SSH into HA and reinstall pydaikin** (see commands below)
7. **Restart HA** to pick up the new pydaikin

```bash
# Example release workflow
cd C:\Users\Chris\Documents\pydaikin-2.8.0
# Edit pyproject.toml to bump version
git add . && git commit -m "Release v2.25.0 - Fix physical remote override"
git push
git tag v2.25.0 && git push --tags

# Get the new commit hash for manifest.json
git rev-parse HEAD

# Update manifest.json in both repos to use new commit hash
# Then copy to Y: drive
```

### SSH Commands to Reinstall pydaikin

**After pushing pydaikin changes to GitHub, run these commands via SSH/Terminal on Home Assistant:**

```bash
# Stop Home Assistant
ha core stop

# Uninstall old version and install new version
pip uninstall pydaikin -y
pip install git+https://github.com/Chris971991/pydaikin-2.8.0.git@<COMMIT_HASH>

# Start Home Assistant
ha core start
```

**Or as a single command chain:**
```bash
ha core stop && pip uninstall pydaikin -y && pip install git+https://github.com/Chris971991/pydaikin-2.8.0.git@<COMMIT_HASH> && ha core start
```

**Replace `<COMMIT_HASH>` with the actual commit hash from `git rev-parse HEAD`**

**Note:** If using Home Assistant OS container:
```bash
docker exec -it homeassistant pip uninstall pydaikin -y
docker exec -it homeassistant pip install git+https://github.com/Chris971991/pydaikin-2.8.0.git@<COMMIT_HASH>
```

## Performance Tuning Options

If BRP084 devices experience network timeouts or instability, these optional fixes can help:

### Option 1: Skip Post-Set Refresh (Reduces HTTP traffic ~50%)
**File:** `pydaikin/daikin_brp084.py`

Add `skip_refresh` parameter to `set()` method:
```python
async def set(self, settings, expected_pow=None, skip_refresh=False):
    # ... existing code ...
    if requests:
        # ... send request ...

        # Skip refresh if caller will handle it (coordinator polls within 10s anyway)
        if not skip_refresh:
            await self.update_status()
```

Then in `climate.py`, call with `skip_refresh=True` to rely on coordinator polling instead.

### Option 2: Increase Polling Interval (10s → 15s)
**File:** `custom_components/daikin/const.py`

```python
# Change from:
DEFAULT_UPDATE_INTERVAL = 10

# To:
DEFAULT_UPDATE_INTERVAL = 15  # Reduces request pile-ups on slow devices
```

**Trade-off:** Physical remote detection takes 15s instead of 10s.

### Current Settings (v2.28.0)
- BRP084 HTTP timeout: 20s (increased from 15s)
- BRP069 HTTP timeout: 20s (base class default)
- Polling interval: 10s
- MAX_CONCURRENT_REQUESTS: 4
