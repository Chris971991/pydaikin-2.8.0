import asyncio

import pytest

from pydaikin.daikin_airbase import DaikinAirBase
from pydaikin.daikin_base import _redact
from pydaikin.daikin_brp069 import DaikinBRP069
from pydaikin.daikin_skyfi import DaikinSkyFi
from pydaikin.response import parse_response


@pytest.mark.parametrize(
    'body,values',
    [
        (
            # Rejection marker (M3): ret != OK preserves {'ret': ...} so
            # callers can distinguish explicit rejection from empty-OK
            'ret=KO,type=aircon,reg=eu,dst=1',
            {'ret': 'KO'},
        ),
        (
            'ret=PARAM NG,adv=',
            {'ret': 'PARAM NG'},
        ),
        (
            'ret=OK',
            dict(),
        ),
        (
            'ret=OK,type=aircon,reg=eu,dst=1,ver=1_14_68,rev=C3FF8A6,pow=1',
            dict(
                type='aircon',
                reg='eu',
                dst='1',
                ver='1_14_68',
                rev='C3FF8A6',
                pow='1',
            ),
        ),
        (
            'ret=OK,ssid1=Loading 2,4G...,radio1=-33,ssid=DaikinAP47108,grp_name=,en_grp=0',
            dict(
                ssid1='Loading 2,4G...',
                radio1='-33',
                ssid='DaikinAP47108',
                grp_name='',
                en_grp='0',
            ),
        ),
        (
            # '=' inside a value is preserved (old regex mis-keyed this as
            # key 'Loadi'); the comma-glue keeps the ',4G...' tail
            'ret=OK,ssid1=Loadi=ng 2,4G...,radio1=-33,ssid=DaikinAP47108,grp_name=,en_grp=0',
            dict(
                ssid1='Loadi=ng 2,4G...',
                radio1='-33',
                ssid='DaikinAP47108',
                grp_name='',
                en_grp='0',
            ),
        ),
        (
            # '=' in value at end of body (old regex dropped the pair)
            'ret=OK,ssid=abc==,mode=3',
            dict(ssid='abc==', mode='3'),
        ),
        (
            # non-\w key chars survive (old regex truncated 'key-x' to 'x')
            'ret=OK,key-x=1',
            {'key-x': '1'},
        ),
        (
            # trailing comma glues onto the previous value (intentional
            # difference from the old regex, consistent with mid-string
            # comma-in-value handling)
            'ret=OK,a=1,',
            dict(a='1,'),
        ),
    ],
)
def test_parse_response(body: str, values: dict):
    assert parse_response(body) == values


def test_parse_response_missing_ret_raises():
    with pytest.raises(ValueError):
        parse_response('pow=1,mode=3')


def test_human_to_daikin_case_insensitive():
    # Title-case input from HA entity option lists must translate
    assert DaikinBRP069.human_to_daikin('f_rate', 'Auto') == 'A'
    assert DaikinBRP069.human_to_daikin('f_rate', 'Silence') == 'B'
    assert DaikinBRP069.human_to_daikin('f_dir', 'Off') == '0'
    assert DaikinBRP069.human_to_daikin('f_dir', '3D') == '3'
    # Daikin-native codes and unknown values pass through ORIGINAL, unchanged
    assert DaikinBRP069.human_to_daikin('f_rate', 'A') == 'A'
    assert DaikinBRP069.human_to_daikin('f_rate', 'unknown') == 'unknown'
    assert DaikinAirBase.human_to_daikin('f_rate', 'Low/Auto') == '1a'
    assert DaikinSkyFi.human_to_daikin('f_rate', 'High') == '3'


def test_redact_masks_credentials():
    params = {'key': 'secret', 'pass': 'hunter2', 'mode': '3'}
    headers = {'X-Daikin-uuid': 'abc-123', 'Accept': '*/*'}
    red_params, red_headers = _redact(params, headers)
    assert red_params == {'key': '****', 'pass': '****', 'mode': '3'}
    assert red_headers == {'X-Daikin-uuid': '****', 'Accept': '*/*'}
    # originals untouched
    assert params['key'] == 'secret'
    assert headers['X-Daikin-uuid'] == 'abc-123'


def _make_device():
    """Device with a dummy session (no HTTP happens in these tests)."""
    return DaikinBRP069('127.0.0.1', session=object())


@pytest.mark.asyncio
async def test_update_status_all_succeed(monkeypatch):
    device = _make_device()

    async def fake_get_resource(resource, *args, **kwargs):
        return {
            'aircon/get_sensor_info': {'htemp': '22'},
            'aircon/get_control_info': {'pow': '1', 'mode': '3'},
        }[resource]

    monkeypatch.setattr(device, '_get_resource', fake_get_resource)
    await device.update_status()
    assert device.values['htemp'] == '22'
    assert device.values['pow'] == '1'


@pytest.mark.asyncio
async def test_update_status_partial_failure_applies_success_then_raises(
    monkeypatch, caplog
):
    device = _make_device()

    async def fake_get_resource(resource, *args, **kwargs):
        if resource == 'aircon/get_control_info':
            raise asyncio.TimeoutError()
        return {'htemp': '22'}

    monkeypatch.setattr(device, '_get_resource', fake_get_resource)
    with pytest.raises(asyncio.TimeoutError):
        await device.update_status()
    # the successful resource was applied before raising
    assert device.values['htemp'] == '22'
    assert 'pow' not in device.values
    assert 'aircon/get_control_info' in caplog.text


@pytest.mark.asyncio
async def test_update_status_all_fail(monkeypatch, caplog):
    device = _make_device()

    async def fake_get_resource(resource, *args, **kwargs):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(device, '_get_resource', fake_get_resource)
    with pytest.raises(asyncio.TimeoutError):
        await device.update_status()
    assert len(device.values) == 0
    assert 'All 2 resource requests failed' in caplog.text


@pytest.mark.asyncio
async def test_update_status_skips_rejected(monkeypatch, caplog):
    """A rejected resource (ret != OK) is skipped, not merged, not fatal.

    Also pins the factory-probe contract: a fully-rejected probe leaves
    device.values empty (factory protocol detection depends on it).
    """
    device = _make_device()

    async def fake_get_resource(resource, *args, **kwargs):
        if resource == 'aircon/get_control_info':
            return {'ret': 'PARAM NG'}
        return {'htemp': '22'}

    monkeypatch.setattr(device, '_get_resource', fake_get_resource)
    await device.update_status()  # must NOT raise
    assert 'ret' not in device.values
    assert device.values['htemp'] == '22'
    assert 'rejected by device' in caplog.text

    # fully-rejected probe -> values stay empty
    device2 = _make_device()

    async def all_rejected(resource, *args, **kwargs):
        return {'ret': 'PARAM NG'}

    monkeypatch.setattr(device2, '_get_resource', all_rejected)
    await device2.update_status()
    assert len(device2.values) == 0


@pytest.mark.asyncio
async def test_update_status_rejected_warns_once(monkeypatch, caplog):
    """Persistent rejection logs WARNING on first occurrence, DEBUG after."""
    import logging

    device = _make_device()

    async def fake_get_resource(resource, *args, **kwargs):
        if resource == 'aircon/get_control_info':
            return {'ret': 'PARAM NG'}
        return {'htemp': '22'}

    monkeypatch.setattr(device, '_get_resource', fake_get_resource)
    with caplog.at_level(logging.WARNING):
        await device.update_status()
        first_warnings = [
            r for r in caplog.records if 'rejected by device' in r.message
        ]
        assert len(first_warnings) == 1
        caplog.clear()
        # force re-poll of the same resource
        device.values._last_update_by_resource.clear()
        await device.update_status()
        repeat_warnings = [
            r for r in caplog.records if 'rejected by device' in r.message
        ]
        assert len(repeat_warnings) == 0


@pytest.mark.asyncio
async def test_get_resource_retries_then_succeeds():
    """_retry_request retries RETRYABLE_EXCEPTIONS then returns the result."""
    from aiohttp.client_exceptions import ServerDisconnectedError

    device = _make_device()
    calls = []

    async def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise ServerDisconnectedError()
        return {'pow': '1'}

    result = await device._retry_request(flaky, attempts=2, description='test')
    assert result == {'pow': '1'}
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_get_resource_attempts_1_fails_fast():
    device = _make_device()
    calls = []

    async def failing():
        calls.append(1)
        raise asyncio.TimeoutError()

    with pytest.raises(asyncio.TimeoutError):
        await device._retry_request(failing, attempts=1, description='test')
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_retry_request_non_retryable_propagates_immediately():
    device = _make_device()
    calls = []

    async def forbidden():
        calls.append(1)
        from aiohttp.web_exceptions import HTTPForbidden

        raise HTTPForbidden(reason='403')

    from aiohttp.web_exceptions import HTTPForbidden

    with pytest.raises(HTTPForbidden):
        await device._retry_request(forbidden, attempts=2, description='test')
    assert len(calls) == 1


def test_values_invalidation_contract():
    """Pin the load-bearing invalidate-on-get behavior (do not 'fix' it)."""
    from pydaikin.values import ApplianceValues

    values = ApplianceValues()
    values.update_by_resource('r', {'pow': '1'})
    assert values.should_resource_be_updated('r') is False
    # passive read does not invalidate
    assert values.get('pow', invalidate=False) == '1'
    assert values.should_resource_be_updated('r') is False
    # default read invalidates -> resource refreshes next poll
    assert values.get('pow') == '1'
    assert values.should_resource_be_updated('r') is True


def test_discover_ip_returns_resolved_ip(monkeypatch):
    from pydaikin import daikin_base
    from pydaikin.daikin_base import Appliance

    # IP input: fast path, discovery never consulted
    monkeypatch.setattr(
        daikin_base,
        'get_name',
        lambda _: pytest.fail('get_name must not be called for IP input'),
    )
    assert Appliance.discover_ip('192.168.1.2') == '192.168.1.2'

    # discovery hit: resolved IP returned (was: device_id returned, bug)
    monkeypatch.setattr(
        daikin_base, 'get_name', lambda _: {'ip': '1.2.3.4', 'name': 'x', 'mac': 'M'}
    )
    assert Appliance.discover_ip('livingroom') == '1.2.3.4'

    # DNS fallback
    monkeypatch.setattr(daikin_base, 'get_name', lambda _: None)
    monkeypatch.setattr(daikin_base.socket, 'gethostbyname', lambda _: '5.6.7.8')
    assert Appliance.discover_ip('livingroom') == '5.6.7.8'


def test_support_filter_dirty_non_numeric():
    device = _make_device()
    device.values.update_by_resource(
        'r', {'en_filter_sign': '--', 'filter_sign_info': '1'}
    )
    assert device.support_filter_dirty is False
    device.values['en_filter_sign'] = '1'
    device.values['filter_sign_info'] = '0'
    assert device.support_filter_dirty is True


def test_represent_missing_pow_no_crash():
    device = _make_device()
    device.values.update_by_resource('r', {'mode': '3'})
    k, val = device.represent('mode')  # must not KeyError
    assert k == 'mode'
    device.values['pow'] = '0'
    assert device.represent('mode') == ('mode', 'off')


def test_show_sensors_empty_values(capsys):
    device = _make_device()
    device.show_sensors()  # must not raise on missing temperatures
    assert 'in_temp=n/a' in capsys.readouterr().out
