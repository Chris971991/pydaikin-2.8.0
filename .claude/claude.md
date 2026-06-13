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

## Physical Remote Override Detection System (v2.40.0)

### Purpose
When automation is controlling the AC and a user presses the **physical IR remote** to turn it OFF (or ON), the system detects this and activates **Override Mode** - pausing all automation for that room.

### How It Works (The Core Logic)

**Key Principle:** `_last_known_pow` must ONLY reflect what the device has CONFIRMED, not what we asked it to do.

1. **Automation sends a command:**
   - `_last_any_command_time` is stamped BEFORE `device.set()` and re-stamped
     by a done-callback when the (shielded) call actually completes — the 45s
     grace is measured from completion, covering slow devices and BRP084
     clipping retries
   - `_last_known_pow` is **NOT changed** (stays at previous confirmed value)

2. **Device confirms the new state:**
   - Coordinator polls device, sees the new `pow`
   - `_last_known_pow` is updated (coordinator is the ONLY writer)

3. **User presses OFF on physical remote (>45s after the last command):**
   - Device turns off
   - Coordinator polls, sees `pow=0`

4. **Override Detection fires:**
   - Sees: `_last_known_pow='1'` (confirmed ON) but `current_pow='0'` (now OFF)
   - No command inside the 45s grace, not in startup/reconnect grace
   - **Result: Override fires!** → `daikin_physical_remote_override` event

A remote press WITHIN 45s of any command is silently synced (not detected);
the blueprint's periodic_check is the documented safety net for that window.

### Critical Variables in `climate.py`

| Variable | Purpose | Updated By |
|----------|---------|------------|
| `_last_known_pow` | Last CONFIRMED device power state | Coordinator ONLY |
| `_last_any_command_time` | 45s any-command grace (mode-transition pow bounces) | Public handlers pre-call + set-completion callback |
| `_last_active_hvac_mode` | Last coordinator-confirmed active mode (turn_on restore) | Coordinator ONLY |
| `_last_override_event_time` | Debounce duplicate events (5s) | Override detection |
| `_entity_init_timestamp` | 60s startup grace | `__init__` |
| `_last_coordinator_recovery_time` | 60s reconnect grace (v2.36.0) | Coordinator transition |

(v2.40.0 removed `_last_on_command_time`/`_last_off_command_time` and the
v2.33.0 "device confirmed → fire anyway" arms: they were unreachable dead code —
every path that armed the 30s windows also armed the 45s grace microseconds
earlier, so the 45s branch always swallowed the poll first.)

### Protection Windows (Prevent False Positives)

There is ONE command protection window: the **45s any-command grace**
(`_last_any_command_time`). While a poll lands inside it, pow changes are
silently synced into `_last_known_pow` without firing an override. The 60s
startup grace and the 60s reconnect grace (v2.36.0) behave the same way.

### What NOT To Do

1. **DO NOT set `_last_known_pow` in `_set()`** - This caused remote detection to never fire because it thought device already confirmed
2. **DO NOT remove the 45s any-command grace** - Causes false overrides during mode transitions (Daikin units bounce pow 1→0→1)
3. **DO NOT check `expected_pow` in pydaikin** - Too many race conditions, use coordinator polling only
4. **DO NOT re-add per-direction 30s ON/OFF windows** - They are strictly subsumed by the 45s grace (removed in v2.40.0 as unreachable)

### Blueprint Integration

The blueprint listens for `daikin_physical_remote_override` event and:
1. Sets `control_mode` to `Override`
2. Records timestamp in `input_datetime.climate_override_time_<room>`
3. Pauses all automation for configured timeout (default 2 hours)

### Testing Remote Override

1. Put room in Smart mode with presence
2. Wait for automation to turn AC ON
3. Wait **>45 seconds after the LAST automation command** (the 45s grace
   silently syncs earlier presses — check logs for command activity; on
   high-trigger setups temporarily pause the automation to get a quiet window)
4. Press OFF on physical remote
5. Override should fire within 10 seconds (next coordinator poll)

### v2.37.0 Mode-Transition Grace Tradeoff

The 45s grace (`_last_any_command_time`, the `< 45` check in
`_handle_coordinator_update`) suppresses ALL override detection for 45 seconds
after ANY automation command — and since v2.40.0 the timestamp is re-stamped
when `device.set()` completes, so the effective suppression is
45s-from-completion.

**Intentional:** Daikin units bounce `pow 1→0→1` during mode transitions
(e.g., `cool→fan_only`). Without this grace, every transition would fire a false override.

**Side effect:** Real remote presses within 45s of any automation command are NOT
detected as overrides immediately. They will be picked up via:
1. Periodic_check in blueprint (60s cadence) — see `manual_override_detection` template
2. Next state mismatch when automation queries expected vs actual

**Setups with high trigger volume:** Continuously refreshing automations (10K+
triggers/day) keep this grace continuously active. By design — periodic_check
is the safety net.

**If you need to tune:** search climate.py for the `< 45` check in
`_handle_coordinator_update`. Decreasing risks false overrides during mode
transitions; increasing further may starve real-remote detection. The blueprint
override windows (50s as of v9.9.0) must stay >= this constant — change them
together.

### v2.31.0 Post-Set Verification Removed (BRP084)

The BRP084 post-set power verification raise (was at `daikin_brp084.py:771`) was
removed in pydaikin v2.31.0 per the same principle: rely on coordinator polling
for state reconciliation, not in-call verification. Slow devices (BRP072C with
30s integration timeout) were causing cascading false-override chains via the
`raise → service-error → optimistic-state-cleared → next-poll-detects-mismatch`
sequence.

**Post-fix behavior:** If a device truly ignores a set command, the warning logs
"Power state not yet reflected after set()" and the coordinator's next poll
(within 10s) reconciles state. Optimistic state expires after 30s, so HA UI
self-heals without raising service errors.

### v2.36.0 Coordinator Reconnect Grace (climate.py)

`_entity_init_timestamp` was previously set ONLY in `__init__` and never reset.
After power outage / device reboot / network outage, the entity persists in HA
memory but the device may report fresh `pow=0` while `_last_known_pow='1'`,
firing a false override.

**v2.36.0 fix:** Track `_last_coordinator_success` and `_last_coordinator_recovery_time`.
On coordinator transition `failed → success`, if `_last_known_pow != current_pow`,
apply a 60s reconnect grace (same as startup grace) to silently sync state.

**Critical:** The grace only applies when pow ACTUALLY CHANGED across the outage —
benign network blips where state didn't change shouldn't suppress real-remote
detection that happens AFTER recovery.

**Tradeoff:** A real remote press DURING the outage window is silently synced
(missed). Acceptable: false-positive overrides cost hours of broken automation;
missed presses cost one cycle until next mismatch detected.

---

## Home Assistant Access — MCP First, SSH as Fallback

**The Home Assistant MCP is installed globally (`uvx ha-mcp@latest`). Always prefer MCP tools — they are faster, structured, and don't need the SSH/SUPERVISOR_TOKEN dance.**

### MCP Tool Reference (use these by default)

| Task | MCP Tool | Notes |
|------|----------|-------|
| Reload automations | `ha_call_service("automation", "reload")` | After blueprint YAML edits |
| Restart HA | `ha_restart(confirm=True)` | Run `ha_check_config()` first |
| Validate config | `ha_check_config()` | |
| Recent error logs | `ha_get_logs(source="error_log", limit=200)` | |
| Logs filtered (e.g. OVERRIDE_LOG) | `ha_get_logs(source="error_log", search="OVERRIDE_LOG")` | |
| System log entries | `ha_get_logs(source="system", level="ERROR")` | Structured errors/warnings |
| Logbook (state changes) | `ha_get_logs(source="logbook", entity_id="...")` | |
| Entity state | `ha_get_state("climate.master_bedroom_a_c")` | Single or list of IDs |
| Entity history | `ha_get_history(entity_ids="...", start_time="24h")` | Relative times supported |
| Find entities | `ha_search_entities(...)` / `ha_deep_search(...)` | |
| Call any service | `ha_call_service(domain, service, entity_id, data)` | |

### MCP-Only Capabilities (no SSH equivalent — big upgrade for AC debugging)

| Task | MCP Tool | Why it matters |
|------|----------|----------------|
| **Automation traces** | `ha_get_automation_traces("automation.<id>")` then with `run_id=...` | Step-by-step view of which trigger fired, conditions passed/failed, actions ran. **Primary tool** for debugging override-detection misfires in the blueprint. |
| **Live Jinja eval** | `ha_eval_template("{{ ... }}")` | Test the blueprint's checksum/state-machine templates against live state without YAML round-trips. |
| **Get/set automation YAML** | `ha_config_get_automation` / `ha_config_set_automation` | Read or apply automation config without file edit + reload dance. |

### When MCP cannot help — SSH still required

Two workflows the MCP **cannot** do; use SSH for these:

1. **Live log tailing (`--follow`)** — MCP gets snapshots only. For real-time monitoring during a test:
   ```bash
   ssh -o StrictHostKeyChecking=no hassio@homeassistant.local 'for f in /run/s6/container_environment/*; do export "$(basename $f)"="$(cat $f)"; done; ha core logs --follow'
   ```
2. **Reinstall pydaikin via pip** — package management is not exposed via MCP. See "SSH Commands to Reinstall pydaikin" further down.

### SSH Reference (fallback only)

Wrap any HA CLI / Supervisor API command in this incantation (the loop sources `SUPERVISOR_TOKEN` from `/run/s6/container_environment/`):

```bash
ssh -o StrictHostKeyChecking=no hassio@homeassistant.local 'for f in /run/s6/container_environment/*; do export "$(basename $f)"="$(cat $f)"; done; <COMMAND>'
```

- **SSH credentials:** user `hassio`, password `hassio`, port 22 (ed25519 key already configured)
- **If 401 Unauthorized:** the `SUPERVISOR_TOKEN` wrapper isn't loaded — re-add the `for f in ...` prefix
- **If connection fails:** try IP `192.168.50.45` instead of `homeassistant.local`; check SSH add-on is running

### Override Log Format Reference

Blueprint writes `[OVERRIDE_LOG]` lines when override fires. Filter via:
```python
ha_get_logs(source="error_log", search="OVERRIDE_LOG")
```

Each entry includes:
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

**Reload automations:**
- **Preferred (MCP):** `ha_call_service("automation", "reload")`
- **Fallback (SSH):**
  ```bash
  ssh -o StrictHostKeyChecking=no hassio@homeassistant.local 'for f in /run/s6/container_environment/*; do export "$(basename $f)"="$(cat $f)"; done; curl -s -X POST -H "Authorization: Bearer $SUPERVISOR_TOKEN" -H "Content-Type: application/json" http://supervisor/core/api/services/automation/reload'
  ```

### Releasing pydaikin Updates

**IMPORTANT:** You cannot just copy pydaikin files to Y: drive. HA installs pydaikin from GitHub based on the version in manifest.json. To deploy pydaikin changes:

1. **Bump version** in `pyproject.toml` (e.g., 2.24.0 → 2.25.0)
2. **Commit and push** to GitHub
3. **Create git tag** matching the version (e.g., `git tag v2.25.0 && git push --tags`) — the tag push triggers the GitHub release workflow, which verifies the tag matches the pyproject version, builds, and publishes the release automatically
4. **Update manifest.json** in both repos to reference new commit hash and version. Bump the manifest version in the SAME commit that changes the requirements git hash so the installed integration version always identifies which pydaikin pin it expects. Note: HA/pip CANNOT detect a changed git hash for an already-installed pydaikin at any version — the manual `ha core stop && pip uninstall pydaikin -y && pip install git+...@<hash> && ha core start` step is always mandatory.
5. **Copy manifest.json and climate.py** (and any other changed integration files) to Y: drive
6. **SSH into HA and reinstall pydaikin** (see commands below)
7. **Restart HA** to pick up the new pydaikin

**Versioning scheme (since 2.40.0):** pydaikin and the HA integration release with ALIGNED version numbers — one number per deployment event, incremented together even when one repo's change is trivial. (Before 2.40.0 the two streams interleaved confusingly: 'v2.31.0' was a pydaikin event while 'v2.36.0/v2.37.0' were climate.py events.)

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
