"""Pydaikin base appliance, represent a Daikin device."""

import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import logging
import random
import socket
from ssl import SSLContext
from typing import Optional
from urllib.parse import unquote

from aiohttp import ClientSession, ClientTimeout
from aiohttp.client_exceptions import (
    ClientOSError,
    ClientResponseError,
    ServerDisconnectedError,
    ServerTimeoutError,
)
from aiohttp.web_exceptions import HTTPForbidden

from .discovery import get_name
from .power import ATTR_COOL, ATTR_HEAT, ATTR_TOTAL, TIME_TODAY, DaikinPowerMixin
from .response import parse_response
from .values import ApplianceValues

_LOGGER = logging.getLogger(__name__)

# Transient network errors worth retrying in-call. HTTPForbidden (auth) and
# CancelledError are deliberately excluded: fail fast / propagate.
RETRYABLE_EXCEPTIONS = (
    ClientOSError,
    ClientResponseError,
    ServerDisconnectedError,
    ServerTimeoutError,
    asyncio.TimeoutError,
)


def _redact(params: dict, headers: dict) -> tuple:
    """Return copies of params/headers with credentials masked for logging."""
    redacted_params = {**params, **{k: '****' for k in ('pass', 'key') if k in params}}
    redacted_headers = {
        **headers,
        **({'X-Daikin-uuid': '****'} if 'X-Daikin-uuid' in headers else {}),
    }
    return redacted_params, redacted_headers


class Appliance(DaikinPowerMixin):
    # pylint: disable=too-many-public-methods,too-many-instance-attributes
    """Daikin main appliance class."""

    base_url: str
    session: Optional[ClientSession]
    ssl_context: Optional[SSLContext] = None

    TRANSLATIONS = {}

    VALUES_TRANSLATION = {}

    VALUES_SUMMARY = []

    INFO_RESOURCES = []

    MAX_CONCURRENT_REQUESTS = 4

    @classmethod
    def daikin_to_human(cls, dimension, value):
        """Return converted values from Daikin to Human."""
        return cls.TRANSLATIONS.get(dimension, {}).get(value, str(value))

    @classmethod
    def human_to_daikin(cls, dimension, value):
        """Return converted values from Human to Daikin (case-insensitive on value).

        On miss the ORIGINAL value is returned unchanged so daikin-native
        codes (e.g. 'A') pass through exactly as before.
        """
        translations_rev = {
            dim: {v.lower(): k for k, v in item.items()}
            for dim, item in cls.TRANSLATIONS.items()
        }
        lookup = value.lower() if isinstance(value, str) else value
        return translations_rev.get(dimension, {}).get(lookup, value)

    @classmethod
    def daikin_values(cls, dimension):
        """Return sorted list of translated values."""
        return sorted(list(cls.TRANSLATIONS.get(dimension, {}).values()))

    @staticmethod
    def parse_response(response_body):
        """Parse response from Daikin.
        Subclassed by submodules with own implementation"""
        return parse_response(response_body)

    @staticmethod
    def translate_mac(value):
        """Return translated MAC address."""
        return ':'.join(value[i : i + 2] for i in range(0, len(value), 2))

    @staticmethod
    def discover_ip(device_id):
        """Return translated name to ip address."""
        try:
            socket.inet_aton(device_id)
            device_ip = device_id  # id is an IP
        except socket.error:
            device_ip = None

        if device_ip is None:
            # id is a common name, try discovery
            device_name = get_name(device_id)
            if device_name is None:
                # try DNS
                try:
                    device_ip = socket.gethostbyname(device_id)
                except socket.gaierror as exc:
                    raise ValueError(f"no device found for {device_id}") from exc
            else:
                device_ip = device_name['ip']
        return device_ip

    def __init__(self, device_id, session: Optional[ClientSession] = None) -> None:
        """Init the pydaikin appliance, representing one Daikin device."""
        self.values = ApplianceValues()
        self._owned_session = session is None
        self.session = session if session is not None else ClientSession()
        self.headers: dict = {}
        self._energy_consumption_history = defaultdict(list)
        self._last_rejected_ret: dict = {}
        if session:
            self.device_ip = device_id
        else:
            self.device_ip = self.discover_ip(device_id)

        self.base_url = f"http://{self.device_ip}"

        self.request_semaphore = asyncio.Semaphore(value=self.MAX_CONCURRENT_REQUESTS)

    async def close(self):
        """Close the underlying session if this appliance created it.

        Library users that construct an appliance without passing a session
        should use `async with` or call close() explicitly; sessions passed
        in (e.g. by Home Assistant) are left untouched.
        """
        if self._owned_session and self.session is not None and not self.session.closed:
            await self.session.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        await self.close()

    def __getitem__(self, name):
        """Return values from self.value."""
        if name in self.values:
            return self.values[name]
        raise AttributeError("No such attribute: " + name)

    async def init(self):
        """Init status."""
        # Re-defined in all sub-classes
        raise NotImplementedError

    async def _retry_request(
        self, attempt_coro_factory, *, attempts: int = 2, description: str = ""
    ):
        """Run a request coroutine, retrying RETRYABLE_EXCEPTIONS with jitter.

        Shared by Appliance._get_resource and DaikinBRP084._get_resource.
        Budget note: worst case PER REQUEST is attempts x 20s HTTP timeout
        (+ ~1s jitter). A full BRP069 set() issues up to 3 serialized
        requests (state fetch + set + post-set refresh), so the command-
        critical requests (fetch + set, attempts=2 each) stay under the
        HA integration's 60s wait_for except when the device genuinely
        fails twice per request. Polls use attempts=1 — the coordinator's
        10s cadence is the retry loop.
        """
        attempts = max(1, attempts)
        last_exc = None
        for attempt in range(attempts):
            try:
                return await attempt_coro_factory()
            except (
                RETRYABLE_EXCEPTIONS
            ) as exc:  # pylint: disable=catching-non-exception
                last_exc = exc
                if attempt + 1 < attempts:
                    _LOGGER.debug(
                        "Retrying %s after %r (attempt %d/%d)",
                        description,
                        exc,
                        attempt + 1,
                        attempts,
                    )
                    await asyncio.sleep(random.uniform(0.2, 1.2))
        raise last_exc

    async def _get_resource(
        self, path: str, params: Optional[dict] = None, *, attempts: int = 2
    ):
        """Make the http request."""
        if params is None:
            params = {}

        if _LOGGER.isEnabledFor(logging.DEBUG):
            log_params, log_headers = _redact(params, self.headers)
            _LOGGER.debug(
                "Calling: %s/%s %s [%s]",
                self.base_url,
                path,
                log_params,
                log_headers,
            )

        return await self._retry_request(
            lambda: self._get_resource_once(path, params),
            attempts=attempts,
            description=f"{self.base_url}/{path}",
        )

    async def _get_resource_once(self, path: str, params: dict):
        """Single attempt of the http request."""
        # cannot manage session on outer async with or this will close the session
        # passed to pydaikin (homeassistant for instance)
        try:
            async with self.request_semaphore:
                # Set a generous timeout for slow/old devices (20 seconds total)
                # Some older Daikin units can take 15+ seconds to respond
                timeout = ClientTimeout(total=20)
                _LOGGER.debug("HTTP REQUEST START: %s/%s", self.base_url, path)
                async with self.session.get(
                    f'{self.base_url}/{path}',
                    params=params,
                    headers=self.headers,
                    ssl=self.ssl_context,
                    timeout=timeout,
                ) as response:
                    _LOGGER.debug(
                        "HTTP RESPONSE: %s/%s status=%s",
                        self.base_url,
                        path,
                        response.status,
                    )
                    if response.status == 403:
                        raise HTTPForbidden(
                            reason=f"HTTP 403 Forbidden for {response.url}"
                        )
                    # Airbase returns a 404 response on invalid urls but requires fallback
                    if response.status == 404:
                        _LOGGER.debug("HTTP 404 Not Found for %s", response.url)
                        return (
                            {}
                        )  # return an empty dict to indicate successful connection but bad data
                    if response.status != 200:
                        _LOGGER.debug(
                            "Unexpected HTTP status code %s for %s",
                            response.status,
                            response.url,
                        )
                    response.raise_for_status()
                    result = self.parse_response(await response.text())
                    _LOGGER.debug("HTTP REQUEST COMPLETE: %s/%s", self.base_url, path)
                    return result
        except asyncio.CancelledError:
            _LOGGER.warning(
                "HTTP REQUEST CANCELLED: %s/%s - Task was cancelled externally",
                self.base_url,
                path,
            )
            raise
        except asyncio.TimeoutError:
            _LOGGER.warning(
                "HTTP REQUEST TIMEOUT: %s/%s - Request timed out after 20s",
                self.base_url,
                path,
            )
            raise
        except Exception as e:
            _LOGGER.warning(
                "HTTP REQUEST ERROR: %s/%s - %s: %s",
                self.base_url,
                path,
                type(e).__name__,
                e,
            )
            raise

    async def update_status(self, resources=None):
        """Update status from resources.

        Applies every successfully fetched resource, then raises the first
        failure so callers see a failed poll. The raise is load-bearing:
        it feeds HA entity availability and arms the coordinator-recovery
        reconnect grace in the integration.
        """
        if resources is None:
            resources = self.get_info_resources()
        resources = [
            resource
            for resource in resources
            if self.values.should_resource_be_updated(resource)
        ]
        _LOGGER.debug("Updating %s", resources)

        # gather(return_exceptions=True) rather than TaskGroup: TaskGroup
        # cancels siblings on first failure, losing their results and hiding
        # total outages (cancelled tasks are absent from eg.exceptions).
        results = await asyncio.gather(
            *(self._get_resource(resource, attempts=1) for resource in resources),
            return_exceptions=True,
        )

        failures = []
        for resource, result in zip(resources, results):
            if isinstance(result, BaseException):
                failures.append((resource, result))
                continue
            if 'ret' in result:
                # Device explicitly rejected this resource (ret != OK).
                # Skip the merge so the marker never enters values — the
                # factory's protocol probing relies on values staying empty
                # when every resource is rejected. Warn once per distinct
                # ret value, debug thereafter (a persistently-rejected
                # resource is re-polled every cycle).
                if self._last_rejected_ret.get(resource) != result['ret']:
                    self._last_rejected_ret[resource] = result['ret']
                    _LOGGER.warning(
                        "Resource %s rejected by device: ret=%s",
                        resource,
                        result['ret'],
                    )
                else:
                    _LOGGER.debug(
                        "Resource %s still rejected by device: ret=%s",
                        resource,
                        result['ret'],
                    )
                continue
            self._last_rejected_ret.pop(resource, None)
            self.values.update_by_resource(resource, result)

        if failures:
            for resource, exc in failures:
                _LOGGER.warning(
                    "Failed to update resource %s for %s: %r",
                    resource,
                    self.device_ip,
                    exc,
                )
            if len(failures) == len(resources):
                _LOGGER.error(
                    "All %d resource requests failed for device %s",
                    len(resources),
                    self.device_ip,
                )
            for _, exc in failures:
                if isinstance(exc, asyncio.CancelledError):
                    raise exc
            raise failures[0][1]

        self._register_energy_consumption_history()

    def get_info_resources(self):
        """Returns info_resources"""
        return self.INFO_RESOURCES

    def show_values(self, only_summary=False):
        """Print values."""
        if only_summary:
            keys = self.VALUES_SUMMARY
        else:
            keys = sorted(self.values.keys())

        for key in keys:
            if key in self.values:
                k, val = self.represent(key)
                print(f"{k : >20}: {val}")

    def log_sensors(self, file):
        """Log sensors to a file."""
        data = [
            ('datetime', datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')),
            ('in_temp', self.inside_temperature),
        ]
        if self.support_outside_temperature:
            data.append(('out_temp', self.outside_temperature))
        if self.support_compressor_frequency:
            data.append(('cmp_freq', self.compressor_frequency))
        if self.support_filter_dirty:
            data.append(('en_filter_sign', self.filter_dirty))
        if self.support_energy_consumption:
            data.append(
                ('total_today', self.energy_consumption(ATTR_TOTAL, TIME_TODAY))
            )
            data.append(('cool_today', self.energy_consumption(ATTR_COOL, TIME_TODAY)))
            data.append(('heat_today', self.energy_consumption(ATTR_HEAT, TIME_TODAY)))
            data.append(('total_power', self.current_total_power_consumption))
            data.append(('cool_energy', self.last_hour_cool_energy_consumption))
            data.append(('heat_energy', self.last_hour_heat_energy_consumption))
        if file.tell() == 0:
            file.write(','.join(k for k, _ in data))
            file.write('\n')
        file.write(','.join(str(v) for _, v in data))
        file.write('\n')
        file.flush()

    def show_sensors(self):
        """Print sensors."""

        def fmt(label, value, spec='.0f', unit=''):
            if value is None:
                return f'{label}=n/a'
            return f'{label}={value:{spec}}{unit}'

        data = [
            datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
            fmt('in_temp', self.inside_temperature, unit='°C'),
        ]
        if self.support_outside_temperature:
            data.append(fmt('out_temp', self.outside_temperature, unit='°C'))
        if self.support_compressor_frequency:
            data.append(fmt('cmp_freq', self.compressor_frequency, unit='Hz'))
        if self.support_filter_dirty:
            data.append(fmt('en_filter_sign', self.filter_dirty))
        if self.support_energy_consumption:
            data.append(
                fmt(
                    'total_today',
                    self.energy_consumption(ATTR_TOTAL, TIME_TODAY),
                    '.01f',
                    'kWh',
                )
            )
            data.append(
                fmt(
                    'cool_today',
                    self.energy_consumption(ATTR_COOL, TIME_TODAY),
                    '.01f',
                    'kWh',
                )
            )
            data.append(
                fmt(
                    'heat_today',
                    self.energy_consumption(ATTR_HEAT, TIME_TODAY),
                    '.01f',
                    'kWh',
                )
            )
            data.append(
                fmt('total_power', self.current_total_power_consumption, '.02f', 'kW')
            )
            data.append(
                fmt('cool_energy', self.last_hour_cool_energy_consumption, '.01f', 'kW')
            )
            data.append(
                fmt('heat_energy', self.last_hour_heat_energy_consumption, '.01f', 'kW')
            )
        print('  '.join(data))

    def represent(self, key):
        """Return translated value from key."""
        k = self.VALUES_TRANSLATION.get(key, key)

        # adapt the value
        val = self.values.get(key)

        if key == 'mode' and self.values.get('pow') == '0':
            val = 'off'
        elif key == 'mac':
            val = self.translate_mac(val)
            val = unquote(self.values[key]).split(';')
        else:
            val = self.daikin_to_human(key, val)

        _LOGGER.log(logging.NOTSET, 'Represent: %s, %s, %s', key, k, val)
        return (k, val)

    def _parse_number(self, dimension) -> Optional[float]:
        """Parse float number."""
        try:
            return float(self.values.get(dimension))
        except (TypeError, ValueError):
            return None

    @property
    def mac(self) -> str:
        """Return device's MAC address."""
        return self.values.get('mac', self.device_ip)

    @property
    def support_away_mode(self) -> bool:
        """Return True if the device support away_mode."""
        return 'en_hol' in self.values

    @property
    def support_fan_rate(self) -> bool:
        """Return True if the device support setting fan_rate."""
        return 'f_rate' in self.values

    @property
    def support_swing_mode(self) -> bool:
        """Return True if the device support setting swing_mode."""
        return 'f_dir' in self.values

    @property
    def support_outside_temperature(self) -> bool:
        """Return True if the device is not an AirBase unit."""
        return "otemp" in self.values

    @property
    def support_humidity(self) -> bool:
        """Return True if the device has humidity sensor."""
        return self.humidity is not None

    @property
    def support_advanced_modes(self) -> bool:
        """Return True if the device supports advanced modes."""
        return 'adv' in self.values

    @property
    def support_compressor_frequency(self) -> bool:
        """Return True if the device supports compressor frequency."""
        return 'cmpfreq' in self.values

    @property
    def support_filter_dirty(self) -> bool:
        """Return True if the device supports dirty filter notification and it is turned on."""
        value = self._parse_number('en_filter_sign')
        return (
            value is not None and 'filter_sign_info' in self.values and int(value) == 1
        )

    @property
    def support_zone_count(self) -> bool:
        """Return True if the device supports count of active zones."""
        return 'en_zone' in self.values

    @property
    def support_energy_consumption(self) -> bool:
        """Return True if the device supports energy consumption monitoring."""
        return super().support_energy_consumption

    @property
    def outside_temperature(self) -> Optional[float]:
        """Return current outside temperature."""
        return self._parse_number('otemp')

    @property
    def inside_temperature(self) -> Optional[float]:
        """Return current inside temperature."""
        return self._parse_number('htemp')

    @property
    def target_temperature(self) -> Optional[float]:
        """Return current target temperature."""
        return self._parse_number('stemp')

    @property
    def compressor_frequency(self) -> Optional[float]:
        """Return current compressor frequency."""
        return self._parse_number('cmpfreq')

    @property
    def filter_dirty(self) -> Optional[float]:
        """Return current status of the filter."""
        return self._parse_number('filter_sign_info')

    @property
    def zone_count(self) -> Optional[float]:
        """Return number of enabled zones."""
        return self._parse_number('en_zone')

    @property
    def humidity(self) -> Optional[float]:
        """Return current humidity."""
        return self._parse_number('hhum')

    @property
    def target_humidity(self) -> Optional[float]:
        """Return target humidity."""
        return self._parse_number('shum')

    @property
    def current_total_power_consumption(self):
        """Return the current total (heating+cooling, all devices) power consumption in kW."""
        # We tolerate a 50% delay in consumption measure
        return self.current_power_consumption(
            mode=ATTR_TOTAL, exp_diff_time_margin_factor=0.5
        )

    @property
    def last_hour_cool_energy_consumption(self):
        """Return the last hour cool power consumption of a given mode in kW."""
        # We tolerate a 5 minutes delay in consumption measure
        return self.current_power_consumption(
            mode=ATTR_COOL,
            exp_diff_time_value=timedelta(minutes=60),
            exp_diff_time_margin_factor=timedelta(minutes=5),
        )

    @property
    def last_hour_heat_energy_consumption(self):
        """Return the last hour heat power consumption of a given mode in kW."""
        # We tolerate a 5 minutes margin in consumption measure
        return self.current_power_consumption(
            mode=ATTR_HEAT,
            exp_diff_time_value=timedelta(minutes=60),
            exp_diff_time_margin_factor=timedelta(minutes=5),
        )

    @property
    def today_cool_energy_consumption(self):
        """Return today's cooling energy consumption in kWh."""
        return self.energy_consumption(
            mode=ATTR_COOL,
            time=TIME_TODAY,
        )

    @property
    def today_heat_energy_consumption(self):
        """Return today's heating energy consumption in kWh."""
        return self.energy_consumption(
            mode=ATTR_HEAT,
            time=TIME_TODAY,
        )

    @property
    def today_total_energy_consumption(self):
        """Return today's total (all devices) energy consumption in kWh."""
        return self.energy_consumption(
            mode=ATTR_TOTAL,
            time=TIME_TODAY,
        )

    @property
    def today_energy_consumption(self):
        """Return today's energy consumption in kWh."""
        return (self.today_cool_energy_consumption or 0) + (
            self.today_heat_energy_consumption or 0
        )

    @property
    def fan_rate(self) -> list:
        """Return list of supported fan rates."""
        return list(map(str.title, self.TRANSLATIONS.get('f_rate', {}).values()))

    @property
    def swing_modes(self) -> list:
        """Return list of supported swing modes."""
        return list(map(str.title, self.TRANSLATIONS.get('f_dir', {}).values()))

    async def set(self, settings, expected_pow=None):
        """Set settings on Daikin device.

        Args:
            settings: dict of settings to apply
            expected_pow: If provided ('0' or '1'), abort command if device pow doesn't match.
                         Used by climate.py to detect physical remote override.
        """
        raise NotImplementedError

    async def set_holiday(self, mode):
        """Set holiday mode."""
        raise NotImplementedError

    async def set_advanced_mode(self, mode, value):
        """Enable or disable advanced modes."""
        raise NotImplementedError

    async def set_streamer(self, mode):
        """Enable or disable the streamer."""
        raise NotImplementedError

    @property
    def zones(self):
        """Return list of zones."""
        return

    async def set_zone(self, zone_id, key, value):
        """Set zone status."""
        raise NotImplementedError
