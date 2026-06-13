"""Tests for the DaikinAirBase appliance: fan rate lists, zones and set()."""

import pytest

from pydaikin.daikin_airbase import DaikinAirBase
from pydaikin.exceptions import DaikinException

from .test_init import CONTROL_INFO_RESPONSE, client_session

assert client_session

# Real AirBase control-info responses include f_auto/f_airside; AirBase.set()
# reads self.values['f_auto'] so mocks must provide it (or rely on the
# setdefault, exercised separately below).
AIRBASE_CONTROL_INFO_RESPONSE = CONTROL_INFO_RESPONSE + ",f_auto=0,f_airside=0"


@pytest.mark.asyncio
async def test_fan_rate_two_step_with_auto(client_session):
    """2-step + fan-auto units must offer Low/High (+auto variants), no Mid."""
    device = DaikinAirBase('ip', session=client_session)
    device.values.update({'frate_steps': '2', 'en_frate_auto': '1'})
    assert device.fan_rate == ['Auto', 'Low', 'High', 'Low/Auto', 'High/Auto']


@pytest.mark.asyncio
async def test_fan_rate_two_step_no_auto(client_session):
    device = DaikinAirBase('ip', session=client_session)
    device.values.update({'frate_steps': '2', 'en_frate_auto': '0'})
    assert device.fan_rate == ['Low', 'High']


@pytest.mark.asyncio
async def test_fan_rate_three_step_no_auto(client_session):
    device = DaikinAirBase('ip', session=client_session)
    device.values.update({'frate_steps': '3', 'en_frate_auto': '0'})
    assert device.fan_rate == ['Low', 'Mid', 'High']


@pytest.mark.asyncio
async def test_fan_rate_default_full_list(client_session):
    device = DaikinAirBase('ip', session=client_session)
    assert device.fan_rate == [
        'Auto',
        'Low',
        'Mid',
        'High',
        'Low/Auto',
        'Mid/Auto',
        'High/Auto',
    ]


@pytest.mark.asyncio
async def test_zones_with_placeholder_stemp(client_session):
    """Fan/dry mode reports stemp 'M' — zones must not raise ValueError."""
    device = DaikinAirBase('ip', session=client_session)
    device.values.update(
        {
            'zone_name': 'Living%20Room;Bedroom',
            'zone_onoff': '1;0',
            'lztemp_c': 'M;M',
            'lztemp_h': 'M;M',
            'mode': '0',  # fan
            'stemp': 'M',  # placeholder target temperature
        }
    )
    assert device.support_zone_temperature is True
    assert device.zones == [('Living Room', '1', 0), ('Bedroom', '0', 0)]


@pytest.mark.asyncio
async def test_zones_with_numeric_temperatures(client_session):
    """Real per-zone temperatures still come through as floats."""
    device = DaikinAirBase('ip', session=client_session)
    device.values.update(
        {
            'zone_name': 'Living%20Room;Bedroom',
            'zone_onoff': '1;0',
            'lztemp_c': '22;24.5',
            'lztemp_h': '20;21',
            'mode': '2',  # cool
            'stemp': '23',
        }
    )
    assert device.zones == [('Living Room', '1', 22.0), ('Bedroom', '0', 24.5)]


@pytest.mark.asyncio
async def test_set_accepts_expected_pow(aresponses, client_session):
    """set() must accept the expected_pow kwarg (API compatibility, unused)."""
    aresponses.add(
        path_pattern="/skyfi/aircon/get_control_info",
        method_pattern="GET",
        response=AIRBASE_CONTROL_INFO_RESPONSE,
    )
    aresponses.add(
        path_pattern="/skyfi/aircon/set_control_info",
        method_pattern="GET",
        response="ret=OK",
    )

    device = DaikinAirBase('ip', session=client_session)
    result = await device.set({'mode': 'cool'}, expected_pow='1')

    aresponses.assert_all_requests_matched()
    aresponses.assert_no_unused_routes()
    assert 'detected_power_off' in result
    assert result['detected_power_off'] is False


@pytest.mark.asyncio
async def test_set_rejected(aresponses, client_session):
    """A device-rejected set_control_info (ret != OK) must raise."""
    aresponses.add(
        path_pattern="/skyfi/aircon/get_control_info",
        method_pattern="GET",
        response=AIRBASE_CONTROL_INFO_RESPONSE,
    )
    aresponses.add(
        path_pattern="/skyfi/aircon/set_control_info",
        method_pattern="GET",
        response="ret=PARAM NG",
    )

    device = DaikinAirBase('ip', session=client_session)
    with pytest.raises(DaikinException):
        await device.set({'mode': 'cool'})

    aresponses.assert_all_requests_matched()


@pytest.mark.asyncio
async def test_set_defaults_f_auto_and_f_airside(aresponses, client_session):
    """Units whose get_control_info omits f_auto/f_airside must not KeyError."""
    aresponses.add(
        path_pattern="/skyfi/aircon/get_control_info",
        method_pattern="GET",
        response=CONTROL_INFO_RESPONSE,  # no f_auto / f_airside
    )

    captured = {}

    def set_handler(request):
        captured['query'] = dict(request.query)
        return aresponses.Response(text="ret=OK")

    aresponses.add(
        path_pattern="/skyfi/aircon/set_control_info",
        method_pattern="GET",
        response=set_handler,
    )

    device = DaikinAirBase('ip', session=client_session)
    await device.set({'mode': 'cool'})

    aresponses.assert_all_requests_matched()
    assert captured['query']['f_auto'] == '0'
    assert captured['query']['f_airside'] == '0'
