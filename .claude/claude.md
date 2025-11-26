# pydaikin Development Notes

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

### Releasing pydaikin Updates

**IMPORTANT:** You cannot just copy pydaikin files to Y: drive. HA installs pydaikin from GitHub based on the version in manifest.json. To deploy pydaikin changes:

1. **Bump version** in `pyproject.toml` (e.g., 2.24.0 → 2.25.0)
2. **Commit and push** to GitHub
3. **Create git tag** matching the version (e.g., `git tag v2.25.0 && git push --tags`)
4. **Update manifest.json** in both repos to reference new version
5. **Copy manifest.json and climate.py** to Y: drive
6. **Restart HA** - it will download the new pydaikin from GitHub

```bash
# Example release workflow
cd C:\Users\Chris\Documents\pydaikin-2.8.0
# Edit pyproject.toml to bump version
git add . && git commit -m "Release v2.25.0 - Fix physical remote override"
git push
git tag v2.25.0 && git push --tags

# Update manifest.json in both repos to use new version
# Then copy to Y: drive and restart HA
```
