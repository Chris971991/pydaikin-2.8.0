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
