"""BRP069 command-path tests.

Covers audit items:
- M3 (BRP069 half): _update_settings guards invalid/rejected get_control_info;
  set() surfaces device-rejected set_control_info (ret != OK) as
  DaikinException instead of silently 'succeeding'.
- LOW-brp069-abort-contract: every set() return path shares the same shape
  and an abort re-applies device truth to self.values (no pollution from the
  pre-abort settings merge).
- LOW-brp069-setters: set_holiday/set_advanced_mode/set_streamer validate
  input (ValueError), check the rejection marker (DaikinException) and only
  mutate state after device acceptance.
- H3 amendment: a post-set refresh failure must NOT fail the command — the
  set request already succeeded and the coordinator poll reconciles;
  CancelledError still propagates.
"""

import asyncio
import logging

from aiohttp import ClientSession
import pytest
import pytest_asyncio

from pydaikin.daikin_brp069 import DaikinBRP069
from pydaikin.exceptions import DaikinException

from .test_init import CONTROL_INFO_RESPONSE, SENSOR_INFO_RESPONSE

POW_OFF_CONTROL_INFO_RESPONSE = CONTROL_INFO_RESPONSE.replace('pow=1', 'pow=0')


@pytest_asyncio.fixture
async def client_session():
    client_session = ClientSession()
    yield client_session
    await client_session.close()


def add_control_info_route(aresponses, response=CONTROL_INFO_RESPONSE):
    aresponses.add(
        path_pattern="/aircon/get_control_info",
        method_pattern="GET",
        response=response,
    )


# --- M3: rejected/invalid responses surface as DaikinException ---------------


@pytest.mark.asyncio
async def test_set_rejected_raises(aresponses, client_session):
    """A device-rejected set_control_info (ret != OK) raises DaikinException."""
    add_control_info_route(aresponses)
    aresponses.add(
        path_pattern="/aircon/set_control_info",
        method_pattern="GET",
        response="ret=PARAM NG",
    )

    device = DaikinBRP069('ip', session=client_session)
    with pytest.raises(DaikinException, match="rejected set_control_info"):
        await device.set({'mode': 'cool'})

    aresponses.assert_all_requests_matched()
    aresponses.assert_no_unused_routes()


@pytest.mark.asyncio
async def test_set_empty_control_info(aresponses, client_session):
    """A rejected get_control_info raises DaikinException (was KeyError('mode'))."""
    add_control_info_route(aresponses, response="ret=PARAM NG")

    device = DaikinBRP069('ip', session=client_session)
    with pytest.raises(DaikinException, match="invalid/rejected"):
        await device.set({'mode': 'off'})

    aresponses.assert_all_requests_matched()
    aresponses.assert_no_unused_routes()


@pytest.mark.asyncio
async def test_set_success_return_contract(aresponses, client_session):
    """Normal set() returns the full shared return shape."""
    add_control_info_route(aresponses)
    aresponses.add(
        path_pattern="/aircon/set_control_info",
        method_pattern="GET",
        response="ret=OK",
    )
    # Post-set update_status: get_sensor_info was never fetched, and
    # get_control_info was invalidated by the values.get('pow') read in
    # set(), so BOTH are re-fetched.
    aresponses.add(
        path_pattern="/aircon/get_sensor_info",
        method_pattern="GET",
        response=SENSOR_INFO_RESPONSE,
    )
    add_control_info_route(aresponses)

    device = DaikinBRP069('ip', session=client_session)
    result = await device.set({'mode': 'cool'})

    aresponses.assert_all_requests_matched()
    aresponses.assert_no_unused_routes()

    assert result == {
        'detected_power_off': False,
        'detected_power_on': False,
        'current_val': result['current_val'],
        'aborted': False,
    }
    assert result['current_val'].get('pow') == '1'


# --- LOW-brp069-abort-contract ------------------------------------------------


@pytest.mark.asyncio
async def test_set_abort_on_remote_off(aresponses, client_session):
    """expected_pow='1' but device off: abort, full shape, values un-polluted."""
    add_control_info_route(aresponses, response=POW_OFF_CONTROL_INFO_RESPONSE)
    # NO set_control_info route: the command must not be sent.

    device = DaikinBRP069('ip', session=client_session)
    result = await device.set({'mode': 'cool'}, expected_pow='1')

    aresponses.assert_all_requests_matched()
    aresponses.assert_no_unused_routes()

    assert result['aborted'] is True
    assert result['detected_power_off'] is True
    assert result['detected_power_on'] is False
    assert result['current_val'].get('pow') == '0'
    # un-polluted: the pre-abort merge set pow='1'/mode='3'; device truth
    # was re-applied
    assert device.values['pow'] == '0'
    assert device.values['mode'] == '2'


@pytest.mark.asyncio
async def test_set_abort_on_remote_on(aresponses, client_session):
    """expected_pow='0' but device on: mirror abort branch."""
    add_control_info_route(aresponses)  # pow=1

    device = DaikinBRP069('ip', session=client_session)
    result = await device.set({'mode': 'off'}, expected_pow='0')

    aresponses.assert_all_requests_matched()
    aresponses.assert_no_unused_routes()

    assert result['aborted'] is True
    assert result['detected_power_off'] is False
    assert result['detected_power_on'] is True
    assert result['current_val'].get('pow') == '1'
    # un-polluted: the pre-abort merge set pow='0'; device truth re-applied
    assert device.values['pow'] == '1'
    assert device.values['mode'] == '2'


# --- B:H3 amendment: post-set refresh failures don't fail the command --------


@pytest.mark.asyncio
async def test_set_post_refresh_failure_returns_result(
    aresponses, client_session, monkeypatch, caplog
):
    """A refresh-only failure logs a warning and returns the normal result."""
    add_control_info_route(aresponses)
    aresponses.add(
        path_pattern="/aircon/set_control_info",
        method_pattern="GET",
        response="ret=OK",
    )

    device = DaikinBRP069('ip', session=client_session)

    async def failing_update_status(*args, **kwargs):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(device, 'update_status', failing_update_status)

    with caplog.at_level(logging.WARNING):
        result = await device.set({'mode': 'cool'})

    assert result['aborted'] is False
    assert result['detected_power_off'] is False
    assert 'post-set status refresh failed' in caplog.text

    aresponses.assert_all_requests_matched()
    aresponses.assert_no_unused_routes()


@pytest.mark.asyncio
async def test_set_post_refresh_cancelled_propagates(
    aresponses, client_session, monkeypatch
):
    """CancelledError from the post-set refresh is never swallowed."""
    add_control_info_route(aresponses)
    aresponses.add(
        path_pattern="/aircon/set_control_info",
        method_pattern="GET",
        response="ret=OK",
    )

    device = DaikinBRP069('ip', session=client_session)

    async def cancelled_update_status(*args, **kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(device, 'update_status', cancelled_update_status)

    with pytest.raises(asyncio.CancelledError):
        await device.set({'mode': 'cool'})


# --- LOW-brp069-setters --------------------------------------------------------


@pytest.mark.asyncio
async def test_set_holiday_invalid_raises(aresponses, client_session):
    """Unmapped holiday mode raises ValueError without any HTTP request."""
    device = DaikinBRP069('ip', session=client_session)
    with pytest.raises(ValueError, match="Invalid holiday mode"):
        await device.set_holiday('banana')
    # no routes registered: any request would have failed the test
    aresponses.assert_no_unused_routes()


@pytest.mark.asyncio
async def test_set_holiday_rejected_no_mutation(aresponses, client_session):
    """Device rejection raises DaikinException and en_hol is NOT mutated."""
    aresponses.add(
        path_pattern="/common/set_holiday",
        method_pattern="GET",
        response="ret=PARAM NG",
    )

    device = DaikinBRP069('ip', session=client_session)
    with pytest.raises(DaikinException, match="rejected set_holiday"):
        await device.set_holiday('on')

    assert 'en_hol' not in device.values
    aresponses.assert_all_requests_matched()


@pytest.mark.asyncio
async def test_set_holiday_success_mutates_after_accept(aresponses, client_session):
    aresponses.add(
        path_pattern="/common/set_holiday",
        method_pattern="GET",
        response="ret=OK",
    )

    device = DaikinBRP069('ip', session=client_session)
    await device.set_holiday('on')

    assert device.values['en_hol'] == '1'
    aresponses.assert_all_requests_matched()


@pytest.mark.asyncio
async def test_set_advanced_mode_invalid_kind_raises(aresponses, client_session):
    """Unmapped preset kind raises ValueError (amendment: kind validated too)."""
    device = DaikinBRP069('ip', session=client_session)
    with pytest.raises(ValueError, match="Invalid advanced mode kind"):
        await device.set_advanced_mode('banana', 'on')
    aresponses.assert_no_unused_routes()


@pytest.mark.asyncio
async def test_set_advanced_mode_invalid_value_raises(aresponses, client_session):
    device = DaikinBRP069('ip', session=client_session)
    with pytest.raises(ValueError, match="Invalid advanced mode value"):
        await device.set_advanced_mode('powerful', 'banana')
    aresponses.assert_no_unused_routes()


@pytest.mark.asyncio
async def test_set_advanced_mode_success(aresponses, client_session):
    aresponses.add(
        path_pattern="/aircon/set_special_mode",
        method_pattern="GET",
        response="ret=OK,adv=2",
    )

    device = DaikinBRP069('ip', session=client_session)
    await device.set_advanced_mode('powerful', 'on')

    assert device.values['adv'] == '2'
    assert 'ret' not in device.values
    aresponses.assert_all_requests_matched()


@pytest.mark.asyncio
async def test_set_advanced_mode_rejected_no_mutation(aresponses, client_session):
    """Rejection marker is never merged into values."""
    aresponses.add(
        path_pattern="/aircon/set_special_mode",
        method_pattern="GET",
        response="ret=ADV NG",
    )

    device = DaikinBRP069('ip', session=client_session)
    with pytest.raises(DaikinException, match="rejected set_special_mode"):
        await device.set_advanced_mode('powerful', 'on')

    assert 'adv' not in device.values
    assert 'ret' not in device.values
    aresponses.assert_all_requests_matched()


@pytest.mark.asyncio
async def test_set_streamer_invalid_raises(aresponses, client_session):
    device = DaikinBRP069('ip', session=client_session)
    with pytest.raises(ValueError, match="Invalid streamer mode"):
        await device.set_streamer('banana')
    aresponses.assert_no_unused_routes()


@pytest.mark.asyncio
async def test_set_streamer_success(aresponses, client_session):
    aresponses.add(
        path_pattern="/aircon/set_special_mode",
        method_pattern="GET",
        response="ret=OK,adv=13",
    )

    device = DaikinBRP069('ip', session=client_session)
    await device.set_streamer('on')

    assert device.values['adv'] == '13'
    assert 'ret' not in device.values
    aresponses.assert_all_requests_matched()
