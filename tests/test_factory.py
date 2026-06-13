"""Tests for DaikinFactory: ip/port resolution, discovery lookup semantics,
detection-chain exception handling and owned-session cleanup."""

import asyncio
import socket

from aiohttp.client_exceptions import ClientOSError, ServerDisconnectedError
import pytest

from pydaikin.daikin_airbase import DaikinAirBase
from pydaikin.daikin_brp069 import DaikinBRP069
from pydaikin.daikin_brp084 import DaikinBRP084
from pydaikin.exceptions import DaikinException
from pydaikin.factory import DaikinFactory

# ---------------------------------------------------------------------------
# _extract_ip_port (B:M1) — pure sync fast path, no network I/O
# ---------------------------------------------------------------------------


def test_extract_ip_port_plain_ip():
    assert DaikinFactory._extract_ip_port('192.168.50.47') == ('192.168.50.47', None)


def test_extract_ip_port_with_port():
    assert DaikinFactory._extract_ip_port('192.168.50.47:8080') == (
        '192.168.50.47',
        8080,
    )


def test_extract_ip_port_ipv6_not_mangled_by_port_regex():
    assert DaikinFactory._extract_ip_port('::1') == ('::1', None)


def test_extract_ip_port_hostname_returns_needs_discovery_sentinel():
    assert DaikinFactory._extract_ip_port('livingroom') is None


def test_extract_ip_port_hostname_with_port():
    assert DaikinFactory._extract_ip_port('livingroom:8080') == ('livingroom', 8080)


# ---------------------------------------------------------------------------
# _discovery_lookup (B:M1 amendment) — advertised UDP port maps to None
# ---------------------------------------------------------------------------


def test_discovery_lookup_maps_advertised_udp_port_to_none(monkeypatch):
    monkeypatch.setattr(
        'pydaikin.factory.get_name',
        lambda name: {'ip': '1.2.3.4', 'port': '30050', 'udp_port': '30050'},
    )
    assert DaikinFactory._discovery_lookup('livingroom') == ('1.2.3.4', None)


def test_discovery_lookup_keeps_advertised_http_port(monkeypatch):
    monkeypatch.setattr(
        'pydaikin.factory.get_name', lambda name: {'ip': '1.2.3.4', 'port': '8080'}
    )
    assert DaikinFactory._discovery_lookup('livingroom') == ('1.2.3.4', 8080)


def test_discovery_lookup_entry_without_port(monkeypatch):
    monkeypatch.setattr('pydaikin.factory.get_name', lambda name: {'ip': '1.2.3.4'})
    assert DaikinFactory._discovery_lookup('livingroom') == ('1.2.3.4', None)


def test_discovery_lookup_falls_back_to_dns(monkeypatch):
    monkeypatch.setattr('pydaikin.factory.get_name', lambda name: None)
    monkeypatch.setattr(socket, 'gethostbyname', lambda name: '5.6.7.8')
    assert DaikinFactory._discovery_lookup('livingroom') == ('5.6.7.8', None)


def test_discovery_lookup_total_failure_passes_hostname_through(monkeypatch):
    def raise_oserror(name):
        raise OSError('bind failed')

    def raise_gaierror(name):
        raise socket.gaierror('name does not resolve')

    monkeypatch.setattr('pydaikin.factory.get_name', raise_oserror)
    monkeypatch.setattr(socket, 'gethostbyname', raise_gaierror)
    assert DaikinFactory._discovery_lookup('livingroom') == ('livingroom', None)


# ---------------------------------------------------------------------------
# Factory resolution paths (B:M1)
# ---------------------------------------------------------------------------


def _canned_brp084_update(values_map):
    async def fake_update_status(self, resources=None):
        for key, value in values_map.items():
            self.values[key] = value

    return fake_update_status


@pytest.mark.asyncio
async def test_factory_ip_literal_never_calls_discovery(monkeypatch):
    def fail_lookup(device_id):
        raise AssertionError('discovery must not run for IP literals')

    monkeypatch.setattr(DaikinFactory, '_discovery_lookup', staticmethod(fail_lookup))
    monkeypatch.setattr(
        DaikinBRP084,
        'update_status',
        _canned_brp084_update({'mode': 'cool', 'pow': '1'}),
    )

    device = await DaikinFactory('10.0.0.1', session=object())
    assert isinstance(device, DaikinBRP084)
    assert device.device_ip == '10.0.0.1'


@pytest.mark.asyncio
async def test_factory_hostname_calls_discovery_exactly_once(monkeypatch):
    calls = []

    def fake_lookup(device_id):
        calls.append(device_id)
        return ('1.2.3.4', None)

    monkeypatch.setattr(DaikinFactory, '_discovery_lookup', staticmethod(fake_lookup))
    monkeypatch.setattr(
        DaikinBRP084,
        'update_status',
        _canned_brp084_update({'mode': 'cool', 'pow': '1'}),
    )

    device = await DaikinFactory('livingroom', session=object())
    assert calls == ['livingroom']
    assert device.device_ip == '1.2.3.4'


@pytest.mark.asyncio
async def test_factory_applies_explicit_port_to_brp084_candidate(monkeypatch):
    monkeypatch.setattr(
        DaikinBRP084,
        'update_status',
        _canned_brp084_update({'mode': 'cool', 'pow': '1'}),
    )
    device = await DaikinFactory('10.0.0.1:8080', session=object())
    assert device.base_url == 'http://10.0.0.1:8080'
    assert device.url == 'http://10.0.0.1:8080/dsiot/multireq'


# ---------------------------------------------------------------------------
# Detection-chain exception handling (B:LOW-factory-excepts)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_factory_all_protocols_fail_raises_friendly_daikin_exception(
    monkeypatch,
):
    async def brp084_fail(self, resources=None):
        raise DaikinException('rejected')

    async def brp069_timeout(self, resources=None):
        raise asyncio.TimeoutError()

    async def airbase_oserror(self, resources=None):
        raise ClientOSError('connection reset')

    monkeypatch.setattr(DaikinBRP084, 'update_status', brp084_fail)
    monkeypatch.setattr(DaikinBRP069, 'update_status', brp069_timeout)
    monkeypatch.setattr(DaikinAirBase, 'update_status', airbase_oserror)

    with pytest.raises(DaikinException, match='Unable to connect to Daikin device'):
        await DaikinFactory('127.0.0.1', session=object())


@pytest.mark.asyncio
async def test_factory_falls_back_to_airbase_on_server_disconnect(monkeypatch):
    async def brp084_fail(self, resources=None):
        raise DaikinException('not 2.8.0')

    async def brp069_disconnect(self, resources=None):
        raise ServerDisconnectedError()

    async def airbase_ok(self, resources=None):
        self.values['mode'] = '2'
        self.values['pow'] = '1'

    monkeypatch.setattr(DaikinBRP084, 'update_status', brp084_fail)
    monkeypatch.setattr(DaikinBRP069, 'update_status', brp069_disconnect)
    monkeypatch.setattr(DaikinAirBase, 'update_status', airbase_ok)

    device = await DaikinFactory('127.0.0.1', session=object())
    assert isinstance(device, DaikinAirBase)


@pytest.mark.asyncio
async def test_factory_init_failure_wrapped_in_daikin_exception(monkeypatch):
    # Amendment: BRP084 probe fails, BRP069 probe succeeds, then init()
    # raises an ORIGINAL exception type (ServerDisconnectedError) — the
    # init wrapper must re-wrap it into the contextual DaikinException.
    async def brp084_fail(self, resources=None):
        raise DaikinException('not 2.8.0')

    async def brp069_probe_ok(self, resources=None):
        self.values['mode'] = '3'
        self.values['pow'] = '1'

    async def brp069_init_disconnect(self):
        raise ServerDisconnectedError()

    monkeypatch.setattr(DaikinBRP084, 'update_status', brp084_fail)
    monkeypatch.setattr(DaikinBRP069, 'update_status', brp069_probe_ok)
    monkeypatch.setattr(DaikinBRP069, 'init', brp069_init_disconnect)

    with pytest.raises(DaikinException, match='Failed to initialize DaikinBRP069'):
        await DaikinFactory('127.0.0.1', session=object())


# ---------------------------------------------------------------------------
# Owned-session cleanup on discard paths (B:LOW-session-leak, factory half)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_factory_closes_discarded_candidate_sessions(monkeypatch):
    created = []
    orig_init = DaikinBRP084.__init__

    def spy_init(self, *args, **kwargs):
        orig_init(self, *args, **kwargs)
        created.append(self)

    monkeypatch.setattr(DaikinBRP084, '__init__', spy_init)

    async def brp084_fail(self, resources=None):
        raise DaikinException('not 2.8.0')

    async def brp069_ok(self, resources=None):
        self.values['mode'] = '3'
        self.values['pow'] = '1'

    monkeypatch.setattr(DaikinBRP084, 'update_status', brp084_fail)
    monkeypatch.setattr(DaikinBRP069, 'update_status', brp069_ok)

    # session=None: every candidate owns its own ClientSession
    device = await DaikinFactory('127.0.0.1')
    try:
        assert isinstance(device, DaikinBRP069)
        assert len(created) == 1
        assert created[0].session.closed is True  # discarded BRP084 closed
        assert device.session.closed is False  # returned device stays usable
    finally:
        await device.close()
    assert device.session.closed is True


@pytest.mark.asyncio
async def test_factory_init_failure_closes_owned_session(monkeypatch):
    # Amendment: the init()-failure handler must also close the
    # successfully-detected object's owned session before re-raising.
    created = []
    orig_init = DaikinBRP069.__init__

    def spy_init(self, *args, **kwargs):
        orig_init(self, *args, **kwargs)
        created.append(self)

    monkeypatch.setattr(DaikinBRP069, '__init__', spy_init)

    async def brp084_fail(self, resources=None):
        raise DaikinException('not 2.8.0')

    async def brp069_probe_ok(self, resources=None):
        self.values['mode'] = '3'
        self.values['pow'] = '1'

    async def brp069_init_fail(self):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(DaikinBRP084, 'update_status', brp084_fail)
    monkeypatch.setattr(DaikinBRP069, 'update_status', brp069_probe_ok)
    monkeypatch.setattr(DaikinBRP069, 'init', brp069_init_fail)

    with pytest.raises(DaikinException, match='Failed to initialize DaikinBRP069'):
        await DaikinFactory('127.0.0.1')

    assert created
    assert all(candidate.session.closed for candidate in created)
