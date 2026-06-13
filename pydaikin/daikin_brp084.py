"""Pydaikin appliance, represent a Daikin BRP device with firmware 2.8.0."""

import asyncio
from dataclasses import dataclass, field
import logging
from typing import Any, Dict, List, Optional

from aiohttp import ClientSession

from .daikin_base import Appliance
from .exceptions import DaikinException, DaikinRejectedValueError

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
                    index = len(entry) - 1
                # Descend into the MATCHED node, not the last node, so
                # interleaved paths graft values under the right branch.
                entry = entry[index]['pch']
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
            "hot": _E_1002_E_3001_BASE + ["p_03"],
            "auto": _E_1002_E_3001_BASE + ["p_1D"],
        },
        # Fan settings organized by mode
        "fan_settings": {
            "auto": _E_1002_E_3001_BASE + ["p_26"],
            "cool": _E_1002_E_3001_BASE + ["p_09"],
            "hot": _E_1002_E_3001_BASE + ["p_0A"],
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
            "hot": {
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
            # Canonical name is 'hot' (same dialect as BRP069/AirBase/SkyFi
            # and HA core's DAIKIN_TO_HA_STATE); 'heat' is accepted as an
            # alias in set().
            '0100': 'hot',
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
        '0100': 'hot',
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
        """Convert temperature to hexadecimal (two's complement for negatives)."""
        value = round(temperature * divisor)
        if value < 0:
            value += 256
        return format(value, '02x')

    @staticmethod
    def hex_to_int(value: str) -> int:
        """Convert hexadecimal string to integer."""
        return int(value, 16)

    @staticmethod
    def find_value_by_pn(data: dict, fr: str, *keys):
        """Find values in nested response data."""
        if data is None:
            raise DaikinException("Response data is None")
        if 'responses' not in data:
            raise DaikinException("No 'responses' key in data")

        # Failed sub-responses (rsc 4000-series) carry 'fr'/'rsc' but no 'pc';
        # filter them (and entries lacking 'fr') so a failed sub-response
        # surfaces as DaikinException via the not-found path, never KeyError.
        data = [x['pc'] for x in data['responses'] if x.get('fr') == fr and 'pc' in x]

        while keys:
            current_key = keys[0]
            keys = keys[1:]
            found = False
            for pcs in data:
                if pcs.get('pn') == current_key:
                    if not keys:
                        return pcs['pv']
                    data = pcs.get('pch', [])
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
        # pylint: disable=too-many-branches,too-many-statements
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
            # attempts=1: polls never retry in-call; the coordinator's 10s
            # cadence is the retry loop.
            response = await self._get_resource("", params=payload, attempts=1)

            if not response or 'responses' not in response:
                raise DaikinException("Invalid response from device")
        except DaikinException:
            # Re-raise DaikinException as-is (includes timeouts, which
            # _get_resource already translates to DaikinException).
            raise
        except Exception as e:
            error_msg = str(e).strip()
            error_type = type(e).__name__
            if not error_msg:
                error_msg = error_type
            _LOGGER.error(
                "Error communicating with device at %s: %s (%s)",
                self.device_ip,
                error_msg,
                error_type,
            )
            raise DaikinException(
                f"Error communicating with device at {self.device_ip}: {error_msg} ({error_type})"
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
                    self.values['model'] = bytes.fromhex(model_hex).decode(
                        'ascii', errors='ignore'
                    )
                    _LOGGER.info(
                        "Extracted model: %s from hex: %s",
                        self.values['model'],
                        model_hex,
                    )
                else:
                    self.values['model'] = None
                    _LOGGER.warning("Model hex was empty or None")
            # Graceful degradation: model is informational only, any parse
            # failure must not abort the status update.
            except Exception as e:  # pylint: disable=broad-exception-caught
                _LOGGER.error("Could not parse model number: %s", e)
                self.values['model'] = None

            # Get power state
            is_off = self.find_value_by_pn(response, *self.get_path("power")) == "00"

            # Get mode
            mode_value = self.find_value_by_pn(response, *self.get_path("mode"))
            mode = self.MODE_MAP.get(mode_value)
            if mode is None:
                _LOGGER.warning(
                    "Unknown BRP084 mode code %r; treating as 'auto'", mode_value
                )
                mode = 'auto'

            self.values['pow'] = "0" if is_off else "1"
            self.values['mode'] = 'off' if is_off else mode
            if not is_off:
                # Latch the underlying mode ONLY when the device confirmed
                # it is ON; while off the previously latched value is kept
                # (set() falls back to 'auto' when no latch exists). Used by
                # the auto power-on path for operational settings while off.
                self.values['last_active_mode'] = mode

            # Get temperatures
            try:
                outdoor_temp_hex = self.find_value_by_pn(
                    response, *self.get_path("outdoor_temp")
                )
                if outdoor_temp_hex:
                    _LOGGER.debug("Outdoor temp raw hex: %s", outdoor_temp_hex)
                    # hex_to_temp only reads the first byte, so longer values
                    # (e.g. '2080') need no special-casing.
                    self.values['otemp'] = str(
                        self.hex_to_temp(outdoor_temp_hex, divisor=2)
                    )
                else:
                    self.values['otemp'] = "--"
            # Graceful degradation: fall back to the "--" placeholder, any
            # parse failure must not abort the status update.
            except Exception as e:  # pylint: disable=broad-exception-caught
                _LOGGER.error("Error parsing outdoor temperature: %s", e)
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
                self.values['stemp'] = str(
                    self.hex_to_temp(
                        self.find_value_by_pn(
                            response,
                            *self.get_path("temp_settings", self.values['mode']),
                        )
                    )
                )
            else:
                self.values['stemp'] = "--"

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

    async def _get_resource(
        self, path: str, params: Optional[Dict] = None, *, attempts: int = 2
    ):
        """Make the HTTP request to the device, retrying transient errors.

        The retry loop (shared Appliance._retry_request) wraps the raw POST;
        only the FINAL exception is translated below, so retryable exception
        types stay visible to the retry filter. update_status passes
        attempts=1 (the coordinator's 10s cadence is the retry loop); command
        paths keep the default attempts=2.
        """
        # %s formatting is lazy: no serialization cost above DEBUG level.
        _LOGGER.debug("Calling: %s %s", self.url, params)

        try:
            return await self._retry_request(
                lambda: self._post_request(params),
                attempts=attempts,
                description=self.url,
            )
        except asyncio.CancelledError:
            # Task was cancelled (e.g., by blueprint restart) - don't log as error
            _LOGGER.debug(
                "Request cancelled for device at %s (likely automation restart)",
                self.device_ip,
            )
            raise  # Re-raise to propagate cancellation
        except asyncio.TimeoutError as e:
            # Message must keep containing the word 'timeout': the HA
            # integration's log-level predicate matches on that substring.
            _LOGGER.warning(
                "Network timeout communicating with device at %s",
                self.device_ip,
            )
            raise DaikinException(
                f"Network timeout communicating with device at {self.device_ip}"
            ) from e
        except Exception as e:
            error_msg = str(e).strip()
            error_type = type(e).__name__
            if not error_msg:
                error_msg = error_type
            _LOGGER.error(
                "Error in _get_resource for %s: %s (%s)",
                self.device_ip,
                error_msg,
                error_type,
            )
            raise

    async def _post_request(self, params: Optional[Dict]):
        """Single attempt of the multireq POST."""
        async with self.request_semaphore:
            async with self.session.post(
                self.url,
                json=params,
                headers=self.headers,
                ssl=self.ssl_context,
                timeout=20,  # Match base class timeout for slow/congested networks
            ) as response:
                response.raise_for_status()
                json_data = await response.json()
                if json_data is None:
                    raise DaikinException("Device returned null/empty JSON response")
                return json_data

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

            if rsc == 4000:
                fr = resp.get('fr', 'unknown')
                raise DaikinRejectedValueError(
                    f"Device rejected request to {fr} (error code: {rsc})"
                )

            fr = resp.get('fr', 'unknown')
            raise DaikinException(f"Device error for {fr}: code {rsc}")

    async def _set_temperature_with_clipping(
        self, target_temp: float, mode: str
    ) -> float:
        """Set temperature with smart clipping to the nearest valid value.

        Tries the clamped target first (most rejections are out-of-range
        requests), then walks outward in 0.5°C steps with an upward bias,
        deduplicated and hard-capped at 8 requests. The caller (set()) does
        the single trailing status refresh; no refresh happens here.
        """
        path = self.get_path("temp_settings", mode)
        min_temp, max_temp = 16.0, 30.0
        max_attempts = 8

        # Clamp first: a 12°C request tries 16°C immediately.
        base = min(max(round(target_temp * 2) / 2, min_temp), max_temp)
        candidates = [base]
        offset = 0.5
        while offset <= 3.0:
            for sign in (1, -1):
                candidate = base + sign * offset
                if min_temp <= candidate <= max_temp:
                    candidates.append(candidate)
            offset += 0.5

        tried = set()
        attempts = 0
        for temp in candidates:
            if temp in tried or attempts >= max_attempts:
                continue
            tried.add(temp)
            attempts += 1
            try:
                await self._try_set_temperature(path, temp)
                return temp
            except DaikinRejectedValueError:
                # rsc 4000: value out of range for this device; try the next
                # candidate. Any other error propagates immediately.
                continue

        raise DaikinException(
            f"No valid temperature found near {target_temp:.1f}°C "
            f"after {attempts} attempts in {mode} mode."
        )

    async def _try_set_temperature(self, path: List[str], temperature: float):
        """Try to set a specific temperature."""
        requests = []
        temp_hex = self.temp_to_hex(temperature)
        self.add_request(requests, path, temp_hex)

        request_payload = DaikinRequest(requests).serialize()
        _LOGGER.debug("Trying temperature %.1f°C", temperature)
        response = await self._get_resource("", params=request_payload)
        self._validate_response(response)

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

    def _handle_fan_setting(self, settings, requests, mode):
        """Handle fan-related settings (case-insensitive on f_rate)."""
        if 'f_rate' not in settings or mode not in self.API_PATHS["fan_settings"]:
            return

        path = self.get_path("fan_settings", mode)

        f_rate = str(settings['f_rate']).lower()
        fan_value = self.REVERSE_FAN_MODE_MAP.get(f_rate)
        if fan_value is None and f_rate.upper() in self.FAN_MODE_MAP:
            # Pass-through of raw daikin codes like '0A00'.
            fan_value = f_rate.upper()
        if fan_value is None:
            _LOGGER.warning(
                "Unsupported f_rate value %r; fan command skipped",
                settings['f_rate'],
            )
            return

        self.add_request(requests, path, fan_value)

    def _handle_swing_setting(self, settings, requests, mode):
        """Handle swing-related settings (case-insensitive on f_dir)."""
        if 'f_dir' not in settings or mode not in self.API_PATHS["swing_settings"]:
            return

        f_dir = str(settings['f_dir']).lower()

        # Set vertical swing
        vertical_path = self.get_path("swing_settings", mode, "vertical")
        self.add_request(
            requests,
            vertical_path,
            (
                self.TURN_OFF_SWING_AXIS
                if f_dir in ('off', 'horizontal')
                else self.TURN_ON_SWING_AXIS
            ),
        )

        # Set horizontal swing
        horizontal_path = self.get_path("swing_settings", mode, "horizontal")
        self.add_request(
            requests,
            horizontal_path,
            (
                self.TURN_OFF_SWING_AXIS
                if f_dir in ('off', 'vertical')
                else self.TURN_ON_SWING_AXIS
            ),
        )

    async def set(self, settings, expected_pow=None):
        # pylint: disable=too-many-branches
        """Set settings on Daikin device.

        Args:
            settings: dict of settings to apply
            expected_pow: Ignored for BRP084 - included for API compatibility with BRP069.
                         BRP084 doesn't fetch current state before setting, so it can't
                         detect physical remote override at command time.

        Returns:
            dict with 'detected_power_off' (bool) - always False for BRP084
            since it doesn't fetch current state before setting.
        """
        # Note: expected_pow is ignored for BRP084 because this device type
        # doesn't fetch current state before sending commands, so we can't
        # detect if the physical remote turned off the AC. Physical remote
        # override detection will rely on poll-based detection instead.

        # Normalize a COPY of the caller's settings (never mutate the input):
        # lowercase the string dimensions, and accept the legacy 'heat' alias
        # for this class's canonical 'hot' mode name.
        settings = dict(settings)
        for key in ('mode', 'f_rate', 'f_dir'):
            value = settings.get(key)
            if isinstance(value, str):
                settings[key] = value.lower()
        if settings.get('mode') == 'heat':
            settings['mode'] = 'hot'

        # Mode used for path lookups: the new mode if changing (and not
        # 'off'), else the current device mode.
        if 'mode' in settings and settings['mode'] != 'off':
            target_mode = settings['mode']
        else:
            target_mode = self.values.get('mode', invalidate=False)

        requests = []

        # Auto power-on (BRP069 contract): operational settings sent while
        # the unit is off power it on, using the last mode the device was
        # actually seen running in ('auto' when it was never seen on).
        if (
            'mode' not in settings
            and self.values.get('pow') == '0'
            and any(k in settings for k in ('stemp', 'f_rate', 'f_dir'))
        ):
            target_mode = self.values.get('last_active_mode', 'auto')
            self.add_request(requests, self.get_path("power"), "01")
            _LOGGER.debug(
                "Auto power-on: operational settings while off (mode %s)",
                target_mode,
            )

        self._handle_power_setting(settings, requests)
        self._handle_fan_setting(settings, requests, target_mode)
        self._handle_swing_setting(settings, requests, target_mode)

        if requests:
            request_payload = DaikinRequest(requests).serialize()
            _LOGGER.debug("Sending request: %s", request_payload)
            response = await self._get_resource("", params=request_payload)
            _LOGGER.debug("Response: %s", response)

            # Validate response status codes
            self._validate_response(response)

        # Temperature goes through clipping in its OWN request, AFTER the
        # power/mode multireq: a rejected temperature must never abort a
        # power-on, and the device is already in the target mode when the
        # new mode's temperature path is written.
        temp_applied = False
        if 'stemp' in settings:
            if target_mode in self.API_PATHS["temp_settings"]:
                requested_temp = float(settings['stemp'])
                final_temp = await self._set_temperature_with_clipping(
                    requested_temp, target_mode
                )
                temp_applied = True
                if final_temp != requested_temp:
                    self._last_temp_adjustment = {
                        'requested': requested_temp,
                        'actual': final_temp,
                        'message': (
                            f"Temperature adjusted from {requested_temp:.1f}°C "
                            f"to {final_temp:.1f}°C (nearest available)"
                        ),
                    }
                    _LOGGER.info(self._last_temp_adjustment['message'])
                else:
                    self._last_temp_adjustment = None
            else:
                _LOGGER.warning(
                    "Ignoring stemp=%s: not supported in mode %r",
                    settings['stemp'],
                    target_mode,
                )

        if requests or temp_applied:
            # Single trailing status refresh: device truth lands in
            # self.values here — set() never mutates values optimistically.
            await self.update_status()

        # v2.31.0: Power state verification - WARNING ONLY, do not raise.
        # Per project guidance (CLAUDE.md): "DO NOT check expected_pow in pydaikin -
        # Too many race conditions, use coordinator polling only".
        # BRP072C/BRP084 devices may not reflect the new pow value in the post-set
        # update_status() poll due to slow firmware processing. Raising here would
        # propagate to climate.py:381, clear optimistic state, and cause cascading
        # false-override events. Physical remote override detection happens via
        # _handle_coordinator_update() which polls every 10s and reconciles.
        if 'mode' in settings:
            expected_pow = '0' if settings['mode'] == 'off' else '1'
            actual_pow = self.values.get('pow')
            if actual_pow != expected_pow:
                _LOGGER.warning(
                    "Power state not yet reflected after set(): expected pow=%s, got pow=%s. "
                    "Device may be slow to apply; coordinator poll will reconcile.",
                    expected_pow,
                    actual_pow,
                )

        return {'detected_power_off': False, 'current_val': None}

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

    # inside_temperature / target_temperature / outside_temperature are
    # inherited from Appliance (_parse_number): missing keys and '--'
    # placeholders yield None (not a bogus 0.0), which HA maps to 'unknown'.
