"""Pydaikin appliance, represent a Daikin device."""

import logging
import ssl
from uuid import NAMESPACE_OID, uuid3

from .daikin_brp069 import DaikinBRP069

_LOGGER = logging.getLogger(__name__)


class DaikinBRP072C(DaikinBRP069):
    """Daikin class for BRP072Cxx units."""

    def __init__(  # pylint: disable=[too-many-arguments]
        self,
        device_id,
        session=None,
        *,
        key=None,
        uuid=None,
        ssl_context=None,
    ) -> None:
        """Init the pydaikin appliance, representing one Daikin AirBase
        (BRP15B61) device."""
        super().__init__(device_id, session)
        self._key = key
        if uuid is None:
            uuid = uuid3(NAMESPACE_OID, 'pydaikin')
        self._uuid = str(uuid).replace('-', '')
        self.headers = {"X-Daikin-uuid": self._uuid}
        if ssl_context is not None:
            # Caller-supplied context is used as-is: the caller owns its
            # configuration and may share it — never mutate it here.
            # (Callers needing legacy Daikin TLS must apply the hardening
            # below themselves, e.g. options |= 0x4 for
            # SSL_OP_LEGACY_SERVER_CONNECT.)
            self.ssl_context = ssl_context
        else:
            context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
            # SSL_OP_LEGACY_SERVER_CONNECT, https://github.com/python/cpython/issues/89051
            context.options |= 0x4
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            # Lower security level to allow legacy Daikin SSL/TLS configurations
            # Fixes HA 2025.10 SSL WRONG_SIGNATURE_TYPE error
            # See: https://github.com/home-assistant/core/issues/153385
            try:
                context.set_ciphers('DEFAULT:@SECLEVEL=0')
            except ssl.SSLError:
                pass  # Fallback for systems that don't support SECLEVEL=0
            self.ssl_context = context
        self.base_url = f"https://{self.device_ip}"

    async def init(self):
        """Init status."""
        await self._get_resource('common/register_terminal', {"key": self._key})
        await super().init()
