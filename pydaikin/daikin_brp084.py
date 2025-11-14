"""Pydaikin appliance, represent a Daikin BRP device with firmware 2.8.0."""

import asyncio
from dataclasses import dataclass, field
import json
import logging
from typing import Any, Dict, List, Optional

from aiohttp import ClientSession

from .daikin_base import Appliance
from .exceptions import DaikinException

_LOGGER = logging.getLogger(__name__)


@dataclass
class DaikinAttribute:
    """Represent a Daikin attribute for firmware 2.8.0."""

    name: str
    value: Any
    path: List[str]
    to: str

    def format(self) -> Dict:
        """Format the attribute for the API request."""
        return {"pn": self.name, "pv": self.value}


@dataclass
class DaikinRequest:
    """Represent a Daikin request for firmware 2.8.0."""

    attributes: List[DaikinAttribute] = field(default_factory=list)

    def serialize(self, payload=None) -> Dict:
        """Serialize the request to JSON payload."""
        if payload is None:
            payload = {'requests': []}

        def get_existing_index(name: str, children: List[Dict]) -> int:
            for index, child in enumerate(children):
                if child.get("pn") == name:
                    return index
            return -1

        def get_existing_to(to: str, requests: List[Dict]) -> Optional[Dict]:
            for request in requests:
                this_to = request.get("to")
                if this_to == to:
                    return request
            return None

        for attribute in self.attributes:
            to = get_existing_to(attribute.to, payload['requests'])
            if to is None:
                payload['requests'].append(
                    {'op': 3, 'pc': {"pn": "dgc_status", "pch": []}, "to": attribute.to}
                )
                to = payload['requests'][-1]
            entry = to['pc']['pch']
            for pn in attribute.path:
                index = get_existing_index(pn, entry)
                if index == -1:
                    entry.append({"pn": pn, "pch": []})
                entry = entry[-1]['pch']
            entry.append(attribute.format())
        return payload


# pylint: disable=abstract-method
class DaikinBRP084(Appliance):
    """Daikin class for BRP devices with firmware 2.8.0."""

    # Base path constants for reducing duplication
    _E_1002_BASE = ["/dsiot/edge/adr_0100.dgc_status", "dgc_status", "e_1002"]
    _E_1002_E_3001_BASE = _E_1002_BASE + ["e_3001"]
    _E_1003_BASE = ["/dsiot/edge/adr_0200.dgc_status", "dgc_status", "e_1003"]
    _ENERGY_BASE = ["/dsiot/edge/adr_0100.i_power.week_power", "week_power"]

    # Centralized API paths for easier maintenance and better organization
    API_PATHS = {
        # Basic paths
        "power": _E_1002_BASE + ["e_A002", "p_01"],
        "mode": _E_1002_E_3001_BASE + ["p_01"],
        "indoor_temp": _E_1002_BASE + ["e_A00B", "p_01"],
        "indoor_humidity": _E_1002_BASE + ["e_A00B", "p_02"],
        "outdoor_temp": _E_1003_BASE + ["e_A00D", "p_01"],
        "mac_address": ["/dsiot/edge.adp_i", "adp_i", "mac"],
        "model": _E_1002_BASE + ["e_A001", "p_0D"],
        # Mode-specific paths for temperature settings
        "temp_settings": {
            "cool": _E_1002_E_3001_BASE + ["p_02"],
            "heat": _E_1002_E_3001_BASE + ["p_03"],
            "auto": _E_1002_E_3001_BASE + ["p_1D"],
        },
        # Fan settings organized by mode
        "fan_settings": {
            "auto": _E_1002_E_3001_BASE + ["p_26"],
            "cool": _E_1002_E_3001_BASE + ["p_09"],
            "heat": _E_1002_E_3001_BASE + ["p_0A"],
            "fan": _E_1002_E_3001_BASE + ["p_28"],
        },
        # Swing settings organized by mode
        "swing_settings": {
            "auto": {
                "vertical": _E_1002_E_3001_BASE + ["p_20"],
                "horizontal": _E_1002_E_3001_BASE + ["p_21"],
            },
            "cool": {
                "vertical": _E_1002_E_3001_BASE + ["p_05"],
                "horizontal": _E_1002_E_3001_BASE + ["p_06"],
            },
            "heat": {
                "vertical": _E_1002_E_3001_BASE + ["p_07"],
                "horizontal": _E_1002_E_3001_BASE + ["p_08"],
            },
            "fan": {
                "vertical": _E_1002_E_3001_BASE + ["p_24"],
                "horizontal": _E_1002_E_3001_BASE + ["p_25"],
            },
            "dry": {
                "vertical": _E_1002_E_3001_BASE + ["p_22"],
                "horizontal": _E_1002_E_3001_BASE + ["p_23"],
            },
        },
        # Energy data
        "energy": {
            "today_runtime": _ENERGY_BASE + ["today_runtime"],
            "weekly_data": _ENERGY_BASE + ["datas"],
        },
    }

    TRANSLATIONS = {
        'mode': {
            '0300': 'auto',
            '0200': 'cool',
            '0100': 'heat',
            '0000': 'fan',
            '0500': 'dry',
            '00': 'off',
            '01': 'on',
        },
        'f_rate': {
            '0A00': 'auto',
            '0B00': 'quiet',
            '0300': '1',
            '0400': '2',
            '0500': '3',
            '0600': '4',
            '0700': '5',
        },
        'f_dir': {
            'off': 'off',
            'vertical': 'vertical',
            'horizontal': 'horizontal',
            'both': '3d',
        },
        'en_hol': {
            '0': 'off',
            '1': 'on',
        },
    }

    # Mapping between the values from firmware 2.8.0 to traditional API values
    MODE_MAP = {
        '0300': 'auto',
        '0200': 'cool',
        '0100': 'heat',
        '0000': 'fan',
        '0500': 'dry',
    }

    FAN_MODE_MAP = {
        '0A00': 'auto',
        '0B00': 'quiet',
        '0300': '1',
        '0400': '2',
        '0500': '3',
        '0600': '4',
        '0700': '5',
    }

    # These mappings are now handled by the API_PATHS dictionary

    # The values for turning swing axis on/off
    TURN_OFF_SWING_AXIS = "000000"
    TURN_ON_SWING_AXIS = "0F0000"

    REVERSE_MODE_MAP = {v: k for k, v in MODE_MAP.items()}
    REVERSE_FAN_MODE_MAP = {v: k for k, v in FAN_MODE_MAP.items()}

    INFO_RESOURCES = []

    def get_path(self, *keys):
        """Get API path from the nested dictionary structure.

        Args:
            *keys: Variable length list of keys to navigate the API_PATHS dictionary.
                  For example: "temp_settings", "cool" would return the path for
                  cool mode temperature.

        Returns:
            List of path components to use with find_value_by_pn.

        Raises:
            DaikinException: If the path is not found in the API_PATHS dictionary.
        """
        current = self.API_PATHS
        for key in keys:
            if key not in current:
                raise DaikinException(f"Path key {key} not found")
            current = current[key]
        return current

    def __init__(self, device_id, session: Optional[ClientSession] = None) -> None:
        """Initialize the Daikin appliance for firmware 2.8.0."""
        super().__init__(device_id, session)
        self.url = f"{self.base_url}/dsiot/multireq"
        self._last_temp_adjustment = None
        self._cached_target_temp = None  # Cache last target temp when unit is on
        self._pending_target_temp = None  # Store temp to apply when turning on

    @staticmethod
    def hex_to_temp(value: str, divisor=2) -> float:
        """Convert hexadecimal temperature to float."""
        # Handle potential signed temperature values
        temp_raw = int(value[:2], 16)
        # Check if this is a signed value (for negative temps)
        if temp_raw > 127:  # 0x7F - if MSB is set, it's negative
            temp_raw = temp_raw - 256  # Convert from unsigned to signed
        return temp_raw / divisor

    @staticmethod
    def temp_to_hex(temperature: float, divisor=2) -> str:
        """Convert temperature to hexadecimal."""
        return format(int(temperature * divisor), '02x')

    @staticmethod
    def hex_to_int(value: str) -> int:
        """Convert hexadecimal string to integer."""
        return int(value, 16)

    @staticmethod
    def find_value_by_pn(data: dict, fr: str, *keys):
        """Find values in nested response data."""
        data = [x['pc'] for x in data['responses'] if x['fr'] == fr]

        while keys:
            current_key = keys[0]
            keys = keys[1:]
            found = False
            for pcs in data:
                if pcs['pn'] == current_key:
                    if not keys:
                        return pcs['pv']
                    data = pcs['pch']
                    found = True
                    break
            if not found:
                raise DaikinException(f'Key {current_key} not found')
        return None

    def get_swing_state(self, data: dict) -> str:
        """Get the current swing state from response data."""
        mode = self.values.get('mode', invalidate=False)
        if (
            mode is not None
            and mode != 'off'
            and mode in self.API_PATHS["swing_settings"]
        ):
            try:
                vertical = "F" in self.find_value_by_pn(
                    data, *self.get_path("swing_settings", mode, "vertical")
                )
                horizontal = "F" in self.find_value_by_pn(
                    data, *self.get_path("swing_settings", mode, "horizontal")
                )

                if horizontal and vertical:
                    return 'both'
                if horizontal:
                    return 'horizontal'
                if vertical:
                    return 'vertical'
            except DaikinException:
                pass  # Keep default 'off'

        return 'off'  # Default return value

    async def init(self):
        """Initialize the device and fetch initial state."""
        await self.update_status()

    async def update_status(self, resources=None):
        """Update device status."""
        payload = {
            "requests": [
                {"op": 2, "to": "/dsiot/edge/adr_0100.dgc_status?filter=pv,pt,md"},
                {"op": 2, "to": "/dsiot/edge/adr_0200.dgc_status?filter=pv,pt,md"},
                {
                    "op": 2,
                    "to": "/dsiot/edge/adr_0100.i_power.week_power?filter=pv,pt,md",
                },
                {"op": 2, "to": "/dsiot/edge.adp_i"},
            ]
        }

        try:
            response = await self._get_resource("", params=payload)

            if not response or 'responses' not in response:
                raise DaikinException("Invalid response from device")
        except asyncio.TimeoutError as e:
            _LOGGER.error("Timeout communicating with device at %s", self.device_id)
            raise DaikinException(f"Timeout communicating with device at {self.device_id}") from e
        except DaikinException:
            raise  # Re-raise DaikinException as-is
        except Exception as e:
            error_msg = str(e).strip()
            error_type = type(e).__name__
            if not error_msg:
                error_msg = error_type
            _LOGGER.error(
                "Error communicating with device at %s: %s (%s)",
                self.device_id,
                error_msg,
                error_type,
            )
            raise DaikinException(
                f"Error communicating with device at {self.device_id}: {error_msg} ({error_type})"
            ) from e

        # Extract basic info
        try:
            # Get MAC address
            mac = self.find_value_by_pn(response, *self.get_path("mac_address"))
            self.values['mac'] = mac

            # Get model number
            try:
                model_hex = self.find_value_by_pn(response, *self.get_path("model"))
                if model_hex:
                    self.values['model'] = bytes.fromhex(model_hex).decode('ascii', errors='ignore')
                    _LOGGER.info(f"Extracted model: {self.values['model']} from hex: {model_hex}")
                else:
                    self.values['model'] = None
                    _LOGGER.warning("Model hex was empty or None")
            except Exception as e:
                _LOGGER.error(f"Could not parse model number: {e}")
                self.values['model'] = None

            # Get power state
            is_off = self.find_value_by_pn(response, *self.get_path("power")) == "00"

            # Get mode
            mode_value = self.find_value_by_pn(response, *self.get_path("mode"))

            self.values['pow'] = "0" if is_off else "1"
            self.values['mode'] = 'off' if is_off else self.MODE_MAP[mode_value]

            # Get temperatures
            try:
                outdoor_temp_hex = self.find_value_by_pn(response, *self.get_path("outdoor_temp"))
                if outdoor_temp_hex:
                    _LOGGER.debug(f"Outdoor temp raw hex: {outdoor_temp_hex}")
                    # Check if this looks like it's already a decimal value mistakenly treated as hex
                    if len(outdoor_temp_hex) == 4 and outdoor_temp_hex.startswith('20'):
                        # This might be the issue - value like "2080" being read as 8320 decimal
                        # Try treating first 2 chars as signed hex
                        self.values['otemp'] = str(self.hex_to_temp(outdoor_temp_hex[:2], divisor=2))
                    else:
                        self.values['otemp'] = str(self.hex_to_temp(outdoor_temp_hex, divisor=2))
                else:
                    self.values['otemp'] = "--"
            except Exception as e:
                _LOGGER.error(f"Error parsing outdoor temperature: {e}")
                self.values['otemp'] = "--"

            self.values['htemp'] = str(
                self.hex_to_temp(
                    self.find_value_by_pn(response, *self.get_path("indoor_temp")),
                    divisor=1,
                )
            )

            # Get humidity
            try:
                self.values['hhum'] = str(
                    self.hex_to_int(
                        self.find_value_by_pn(
                            response, *self.get_path("indoor_humidity")
                        )
                    )
                )
            except DaikinException:
                self.values['hhum'] = "--"

            # Get target temperature
            if self.values['mode'] in self.API_PATHS["temp_settings"]:
                temp_value = str(
                    self.hex_to_temp(
                        self.find_value_by_pn(
                            response,
                            *self.get_path("temp_settings", self.values['mode']),
                        )
                    )
                )
                self.values['stemp'] = temp_value
                self._cached_target_temp = temp_value  # Cache temp when unit is on
            else:
                # When off, use cached temp if available, otherwise "--"
                self.values['stemp'] = self._cached_target_temp if self._cached_target_temp else "--"

            # Get fan mode
            if self.values['mode'] in self.API_PATHS["fan_settings"]:
                fan_value = self.find_value_by_pn(
                    response, *self.get_path("fan_settings", self.values['mode'])
                )
                self.values['f_rate'] = self.FAN_MODE_MAP.get(fan_value, 'auto')
            else:
                self.values['f_rate'] = 'auto'

            # Get swing mode
            self.values['f_dir'] = self.get_swing_state(response)

            # Get energy data
            try:
                self.values['today_runtime'] = self.find_value_by_pn(
                    response, *self.get_path("energy", "today_runtime")
                )

                energy_data = self.find_value_by_pn(
                    response, *self.get_path("energy", "weekly_data")
                )
                if isinstance(energy_data, list) and len(energy_data) > 0:
                    self.values['datas'] = '/'.join(map(str, energy_data))
            except DaikinException:
                pass

        except DaikinException as e:
            _LOGGER.error("Error extracting values: %s", e)
            raise

    async def _get_resource(self, path: str, params: Optional[Dict] = None):
        """Make the HTTP request to the device."""
        _LOGGER.debug(
            "Calling: %s %s",
            self.url,
            json.dumps(params) if params else "{}",
        )

        try:
            async with self.request_semaphore:
                async with self.session.post(
                    self.url,
                    json=params,
                    headers=self.headers,
                    ssl=self.ssl_context,
                    timeout=5,  # Add a timeout to avoid hanging
                ) as response:
                    response.raise_for_status()
                    return await response.json()
        except (asyncio.TimeoutError, asyncio.CancelledError) as e:
            # Network timeout or cancellation - log as warning not error
            _LOGGER.warning(
                "Network timeout or cancellation communicating with device at %s: %s",
                self.device_id,
                type(e).__name__,
            )
            raise DaikinException(
                f"Network timeout communicating with device at {self.device_id}"
            ) from e
        except Exception as e:
            error_msg = str(e).strip()
            error_type = type(e).__name__
            if not error_msg:
                error_msg = error_type
            _LOGGER.error(
                "Error in _get_resource for %s: %s (%s)",
                self.device_id,
                error_msg,
                error_type,
            )
            raise

    def _validate_response(self, response: Dict):
        """Validate response status codes from device."""
        if not response or 'responses' not in response:
            raise DaikinException("Invalid response format from device")

        for resp in response['responses']:
            rsc = resp.get('rsc')
            if rsc is None:
                continue

            if rsc in (2000, 2004):
                continue  # Success codes
            elif rsc == 4000:
                fr = resp.get('fr', 'unknown')
                raise DaikinException(f"Device rejected request to {fr} (error code: {rsc})")
            else:
                fr = resp.get('fr', 'unknown')
                raise DaikinException(f"Device error for {fr}: code {rsc}")

    async def _set_temperature_with_clipping(self, target_temp: float) -> float:
        """Set temperature with smart clipping to nearest valid value."""
        if self.values['mode'] not in self.API_PATHS["temp_settings"]:
            raise DaikinException(f"Temperature setting not supported in mode: {self.values['mode']}")

        path = self.get_path("temp_settings", self.values['mode'])

        # Try the exact temperature first
        try:
            await self._try_set_temperature(path, target_temp)
            # Update status after successful setting
            await self.update_status()
            return target_temp
        except DaikinException as e:
            if "error code: 4000" not in str(e):
                raise  # Re-raise non-temperature-range errors

        # If exact temp failed, try clipping upward first (toward warmer temps)
        # This handles the common case where requested temp is too cold
        try:
            final_temp = await self._search_valid_temperature(path, target_temp, direction=1)
            await self.update_status()
            return final_temp
        except DaikinException:
            # If that fails, try downward (toward cooler temps)
            final_temp = await self._search_valid_temperature(path, target_temp, direction=-1)
            await self.update_status()
            return final_temp

    async def _try_set_temperature(self, path: List[str], temperature: float):
        """Try to set a specific temperature."""
        requests = []
        temp_hex = self.temp_to_hex(temperature)
        self.add_request(requests, path, temp_hex)

        request_payload = DaikinRequest(requests).serialize()
        _LOGGER.debug("Trying temperature %.1f°C", temperature)
        response = await self._get_resource("", params=request_payload)
        self._validate_response(response)

    async def _search_valid_temperature(self, path: List[str], start_temp: float, direction: int) -> float:
        """Search for a valid temperature in the given direction using optimized binary search."""
        # Reasonable temperature bounds (most AC units support 16-30°C)
        min_temp, max_temp = 16.0, 30.0

        # First, try just a few nearby temperatures (most likely to succeed)
        # This handles the common case where device rounds to nearest 0.5 or 1.0
        quick_tries = [0.5, 1.0, -0.5, -1.0] if direction > 0 else [-0.5, -1.0, 0.5, 1.0]

        for offset in quick_tries:
            test_temp = start_temp + offset
            if min_temp <= test_temp <= max_temp:
                try:
                    await self._try_set_temperature(path, test_temp)
                    return test_temp
                except DaikinException as e:
                    if "error code: 4000" not in str(e):
                        raise
                    continue

        # If quick tries failed, do a linear search in the specified direction
        current_temp = start_temp
        for _ in range(10):  # Reduced from 15 for faster failure
            current_temp += direction * 0.5

            if current_temp < min_temp or current_temp > max_temp:
                break

            try:
                await self._try_set_temperature(path, current_temp)
                return current_temp
            except DaikinException as e:
                if "error code: 4000" not in str(e):
                    raise
                continue

        # If we get here, no valid temperature was found
        raise DaikinException(
            f"No valid temperature found near {start_temp:.1f}°C. "
            f"Device may have limited temperature range in {self.values['mode']} mode."
        )

    async def _update_settings(self, settings):
        """Update settings to set on Daikin device."""
        # Start with current values
        _LOGGER.debug("Updating settings: %s", settings)

        # Handle specific translations for this firmware version
        for key, value in settings.items():
            if key == 'mode' and value == 'off':
                self.values['pow'] = '0'
            elif key == 'mode':
                self.values['pow'] = '1'
                self.values['mode'] = value
            else:
                self.values[key] = value

        # Store pending temp if setting temp while off
        if 'stemp' in settings and self.values.get('pow') == '0':
            self._pending_target_temp = settings['stemp']
            _LOGGER.debug("Stored pending temperature: %s", self._pending_target_temp)

        return self.values

    def add_request(self, requests, path, value):
        """Append DaikinAttribute to requests."""
        requests.append(DaikinAttribute(path[-1], value, path[2:4], path[0]))

    def _handle_power_setting(self, settings, requests):
        """Handle power-related settings."""
        if 'mode' not in settings:
            return

        # Turn off/on
        power_path = self.get_path("power")
        self.add_request(
            requests, power_path, "00" if settings['mode'] == 'off' else "01"
        )

        if settings['mode'] == 'off':
            return

        # Set mode
        mode_value = self.REVERSE_MODE_MAP.get(settings['mode'])
        if mode_value:
            mode_path = self.get_path("mode")
            self.add_request(requests, mode_path, mode_value)

        # Apply pending temperature when turning on
        if self._pending_target_temp and settings['mode'] in self.API_PATHS["temp_settings"]:
            settings['stemp'] = self._pending_target_temp
            _LOGGER.info("Applying pending temperature %s when turning on", self._pending_target_temp)
            self._pending_target_temp = None  # Clear after applying

    def _handle_temperature_setting(self, settings, requests):
        """Handle temperature-related settings."""
        if (
            'stemp' not in settings
            or self.values['mode'] not in self.API_PATHS["temp_settings"]
        ):
            return

        path = self.get_path("temp_settings", self.values['mode'])
        temp_hex = self.temp_to_hex(float(settings['stemp']))
        self.add_request(requests, path, temp_hex)

    def _handle_fan_setting(self, settings, requests):
        """Handle fan-related settings."""
        if (
            'f_rate' not in settings
            or self.values['mode'] not in self.API_PATHS["fan_settings"]
        ):
            return

        path = self.get_path("fan_settings", self.values['mode'])
        fan_value = None

        # Try both formats - the internal one and the user-friendly one
        for key, value in self.FAN_MODE_MAP.items():
            if value == settings['f_rate'] or key == settings['f_rate']:
                fan_value = key
                break

        if fan_value:
            self.add_request(requests, path, fan_value)

    def _handle_swing_setting(self, settings, requests):
        """Handle swing-related settings."""
        if (
            'f_dir' not in settings
            or self.values['mode'] not in self.API_PATHS["swing_settings"]
        ):
            return

        # Set vertical swing
        vertical_path = self.get_path("swing_settings", self.values['mode'], "vertical")
        self.add_request(
            requests,
            vertical_path,
            (
                self.TURN_OFF_SWING_AXIS
                if settings['f_dir'] in ('off', 'horizontal')
                else self.TURN_ON_SWING_AXIS
            ),
        )

        # Set horizontal swing
        horizontal_path = self.get_path(
            "swing_settings", self.values['mode'], "horizontal"
        )
        self.add_request(
            requests,
            horizontal_path,
            (
                self.TURN_OFF_SWING_AXIS
                if settings['f_dir'] in ('off', 'vertical')
                else self.TURN_ON_SWING_AXIS
            ),
        )

    async def set(self, settings):
        """Set settings on Daikin device."""
        await self._update_settings(settings)

        # Handle temperature setting with smart clipping if other settings exist
        has_temp_setting = 'stemp' in settings
        has_other_settings = any(key != 'stemp' for key in settings.keys())

        if has_temp_setting and not has_other_settings:
            # If setting temp while off, just store and return
            if self.values.get('pow') == '0':
                _LOGGER.info("Storing temperature %.1f°C to apply when unit turns on", float(settings['stemp']))
                return  # Exit early, temp already stored in _update_settings

            # Temperature-only setting while on - use smart clipping
            requested_temp = float(settings['stemp'])
            final_temp = await self._set_temperature_with_clipping(requested_temp)
            if final_temp != requested_temp:
                self._last_temp_adjustment = {
                    'requested': requested_temp,
                    'actual': final_temp,
                    'message': f"Temperature adjusted from {requested_temp:.1f}°C to {final_temp:.1f}°C (nearest available)"
                }
                _LOGGER.info(self._last_temp_adjustment['message'])
            else:
                self._last_temp_adjustment = None
            return  # Exit early for temperature-only settings

        # Handle other settings normally
        requests = []
        self._handle_power_setting(settings, requests)
        self._handle_temperature_setting(settings, requests)
        self._handle_fan_setting(settings, requests)
        self._handle_swing_setting(settings, requests)

        if requests:
            request_payload = DaikinRequest(requests).serialize()
            _LOGGER.debug("Sending request: %s", request_payload)
            response = await self._get_resource("", params=request_payload)
            _LOGGER.debug("Response: %s", response)

            # Validate response status codes
            self._validate_response(response)

            # Update status after setting
            await self.update_status()

    # pylint: disable=unused-argument
    async def set_streamer(self, mode):
        """Streamer mode not supported in firmware 2.8.0"""
        _LOGGER.debug("Streamer mode not supported in firmware 2.8.0")

    # pylint: disable=unused-argument
    async def set_holiday(self, mode):
        """Set holiday mode."""
        _LOGGER.debug("Holiday mode not supported in firmware 2.8.0")

    # pylint: disable=unused-argument
    async def set_advanced_mode(self, mode, value):
        """Set advanced mode."""
        _LOGGER.debug("Advanced mode not supported in firmware 2.8.0")

    @property
    def support_away_mode(self) -> bool:
        """Set holiday mode not supported in firmware 2.8.0"""
        return False

    @property
    def support_advanced_modes(self) -> bool:
        """Advanced mode not supported in firmware 2.8.0"""
        return False

    @property
    def support_zone_count(self) -> bool:
        """Zones mode not supported in firmware 2.8.0"""
        return False

    @property
    def last_temperature_adjustment(self) -> Optional[Dict]:
        """Return information about the last temperature adjustment, if any."""
        return self._last_temp_adjustment

    def get_temperature_adjustment_message(self) -> Optional[str]:
        """Return a user-friendly message about the last temperature adjustment."""
        if self._last_temp_adjustment:
            return self._last_temp_adjustment['message']
        return None

    @property
    def inside_temperature(self) -> Optional[float]:
        """Return current indoor temperature (for compatibility with official HA integration)."""
        try:
            return float(self.values.get('htemp', 0))
        except (ValueError, TypeError):
            return None

    @property
    def target_temperature(self) -> Optional[float]:
        """Return target temperature (for compatibility with official HA integration)."""
        # If unit is off but we have cached temp, return it
        if self.values.get('pow') == '0' and self._cached_target_temp:
            try:
                return float(self._cached_target_temp)
            except (ValueError, TypeError):
                pass

        try:
            return float(self.values.get('stemp', 0))
        except (ValueError, TypeError):
            return None

    @property
    def outside_temperature(self) -> Optional[float]:
        """Return outdoor temperature (for compatibility with official HA integration)."""
        try:
            otemp = self.values.get('otemp', '--')
            if otemp == '--':
                return None
            return float(otemp)
        except (ValueError, TypeError):
            return None
