"Factory to generate Pydaikin complete objects"

import asyncio
import ipaddress
import logging
import re
import socket
from typing import Optional, Tuple

from aiohttp import ClientError, ClientSession
from aiohttp.web_exceptions import HTTPForbidden

from .daikin_airbase import DaikinAirBase
from .daikin_base import Appliance
from .daikin_brp069 import DaikinBRP069
from .daikin_brp072c import DaikinBRP072C
from .daikin_brp084 import DaikinBRP084
from .daikin_skyfi import DaikinSkyFi
from .discovery import UDP_DST_PORT, get_name
from .exceptions import DaikinException

_LOGGER = logging.getLogger(__name__)

# Exceptions that legitimately escape a detection probe or init():
# - DaikinException: protocol-level failures (already contextualized)
# - ClientError: aiohttp base for connection failures and 5xx
#   (ClientOSError, ClientResponseError, ServerDisconnectedError, ...)
# - HTTPForbidden: 403 probe against the wrong protocol
# - asyncio.TimeoutError: request timeout after retries
# CancelledError is a BaseException and still propagates.
_PROBE_EXCEPTIONS = (DaikinException, ClientError, HTTPForbidden, asyncio.TimeoutError)


class DaikinFactory:  # pylint: disable=too-few-public-methods
    "Factory object generating instantiated instances of Appliance"

    _generated_object: Appliance

    async def __new__(cls, *a, **kw):  # pylint: disable=invalid-overridden-method
        "Return not itself, but the Appliance instanced by __init__"
        instance = super().__new__(cls)
        await instance.__init__(*a, **kw)
        return instance._generated_object

    async def __init__(
        self,
        device_id: str,
        session: Optional[ClientSession] = None,
        password: str = None,
        key: str = None,
        **kwargs,
    ) -> None:
        """Factory to init the corresponding Daikin class."""

        # Resolve IP literals / explicit ports synchronously; only fall back
        # to (blocking) UDP discovery for plain names, off the event loop.
        resolved = self._extract_ip_port(device_id)
        if resolved is None:
            resolved = await asyncio.get_running_loop().run_in_executor(
                None, self._discovery_lookup, device_id
            )
        device_ip, device_port = resolved

        if password is not None:
            self._generated_object = DaikinSkyFi(device_ip, session, password)
            if device_port:
                # An explicit :port in device_id overrides SkyFi's default 2000
                _LOGGER.debug("Using custom port %s for SkyFi", device_port)
                self._generated_object.base_url = f"http://{device_ip}:{device_port}"
        elif key is not None:
            self._generated_object = DaikinBRP072C(
                device_ip,
                session,
                key=key,
                uuid=kwargs.get('uuid'),
                ssl_context=kwargs.get('ssl_context'),
            )
        else:  # special case for BRP069, AirBase, and BRP firmware 2.8.0
            # First try to check if it's firmware 2.8.0
            try:
                _LOGGER.debug("Trying connection to firmware 2.8.0")
                self._generated_object = DaikinBRP084(device_ip, session)

                # If we have a specific port from discovery, set it in the base_url
                if device_port and device_port != 80:
                    _LOGGER.debug("Using custom port %s for BRP084", device_port)
                    self._generated_object.base_url = (
                        f"http://{device_ip}:{device_port}"
                    )
                    self._generated_object.url = (
                        f"{self._generated_object.base_url}/dsiot/multireq"
                    )

                try:
                    await self._generated_object.update_status()
                    # If we get here, it's likely a 2.8.0 device
                    _LOGGER.info("Successfully connected to firmware 2.8.0 device")
                    # Initialize mode to "off" if we couldn't read it
                    if not self._generated_object.values.get("mode", invalidate=False):
                        self._generated_object.values["mode"] = "off"
                        self._generated_object.values["pow"] = "0"
                    return
                except Exception as e:
                    _LOGGER.debug(
                        "Failed to communicate with firmware 2.8.0 endpoint: %s", e
                    )
                    # Use from e to properly chain exceptions
                    raise DaikinException(f"Not a firmware 2.8.0 device: {e}") from e
            except DaikinException as err:
                _LOGGER.debug("Not a firmware 2.8.0 device: %s", err)
                # Close the discarded candidate's owned session (no-op when
                # the caller supplied a session).
                await self._generated_object.close()

            # Try BRP069
            try:
                _LOGGER.debug("Trying connection to BRP069")
                self._generated_object = DaikinBRP069(device_ip, session)

                # If we have a specific port from discovery, set it in the base_url
                if device_port and device_port != 80:
                    _LOGGER.debug("Using custom port %s for BRP069", device_port)
                    self._generated_object.base_url = (
                        f"http://{device_ip}:{device_port}"
                    )

                await self._generated_object.update_status(
                    self._generated_object.HTTP_RESOURCES[:1]
                )
                if not self._generated_object.values:
                    raise DaikinException("Empty Values.")
            except _PROBE_EXCEPTIONS as err:
                _LOGGER.debug("Falling back to AirBase: %s", err)
                await self._generated_object.close()
                self._generated_object = DaikinAirBase(device_ip, session)

                # If we have a specific port from discovery, set it in the base_url
                if device_port and device_port != 80:
                    _LOGGER.debug("Using custom port %s for AirBase", device_port)
                    self._generated_object.base_url = (
                        f"http://{device_ip}:{device_port}"
                    )

                # Pre-populate values before calling init() to avoid "Empty values" error
                try:
                    await self._generated_object.update_status(
                        self._generated_object.HTTP_RESOURCES[:1]
                    )
                    if not self._generated_object.values:
                        raise DaikinException(
                            f"Device at {device_ip} is not responding. "
                            "The device may be offline or unreachable. "
                            "Please check the device network connection and try again."
                        )
                except _PROBE_EXCEPTIONS as airbase_err:
                    # All device types failed - device is likely offline
                    await self._generated_object.close()
                    raise DaikinException(
                        f"Unable to connect to Daikin device at {device_ip}. "
                        f"The device appears to be offline or unreachable. "
                        f"Tried BRP084, BRP069, and AirBase protocols. "
                        f"Last error: {airbase_err}"
                    ) from airbase_err

        try:
            await self._generated_object.init()
        except _PROBE_EXCEPTIONS as e:
            # Re-raise with more context about which device type failed.
            # Catches the original exception types update_status re-raises
            # (timeouts, aiohttp errors) so HA's config flow / setup sees a
            # DaikinException instead of a raw traceback.
            device_type = type(self._generated_object).__name__
            await self._generated_object.close()
            raise DaikinException(
                f"Failed to initialize {device_type} device at {device_ip}: {e}. "
                f"The device may be offline or unreachable."
            ) from e

        if not self._generated_object.values.get("mode"):
            await self._generated_object.close()
            raise DaikinException(
                f"Error creating device, {device_id} is not supported."
            )

        _LOGGER.debug("Daikin generated object: %s", self._generated_object)

    @staticmethod
    def _extract_ip_port(device_id: str) -> Optional[Tuple[str, Optional[int]]]:
        """Extract (host, port) from device_id without any network I/O.

        Order matters: the IP-literal check runs first so bare IPv6
        addresses like '::1' are not mangled by the port regex. Returns
        None as a 'needs discovery' sentinel for plain names.
        """
        try:
            ipaddress.ip_address(device_id)
            return device_id, None
        except ValueError:
            pass

        port_match = re.match(r'^(.+):(\d+)$', device_id)
        if port_match:
            return port_match.group(1), int(port_match.group(2))

        return None

    @staticmethod
    def _discovery_lookup(device_id: str) -> Tuple[str, Optional[int]]:
        """Resolve a device name via UDP discovery (blocking; run in executor).

        The advertised 'port' from basic_info is treated as an HTTP port
        only when it differs from the UDP discovery port: real adapters
        advertise port=30050 (the UDP port itself), which is never a valid
        HTTP port, so it maps to None (default 80).
        """
        try:
            entry = get_name(device_id)
            if entry:
                port = int(entry['port']) if 'port' in entry else None
                if port == UDP_DST_PORT:
                    port = None
                return entry['ip'], port
        except (KeyError, ValueError, TypeError, OSError) as exc:
            _LOGGER.debug("Discovery lookup failed for %s: %s", device_id, exc)

        # Fall back to DNS while still inside the executor, so session=None
        # construction does not block the event loop in discover_ip later.
        try:
            return socket.gethostbyname(device_id), None
        except OSError as exc:
            _LOGGER.debug("DNS lookup failed for %s: %s", device_id, exc)

        return device_id, None
