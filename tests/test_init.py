"""Verify that init() calls the expected set of endpoints for each Daikin device.

The fork's BRP069 init() is slimmed to 6 essential resources — the basic
state (common/basic_info, aircon/get_sensor_info, aircon/get_control_info)
plus the one-shot support-flag resources added by audit item M2
(common/get_holiday, aircon/get_week_power, aircon/get_year_power) — see
daikin_brp069.py init(). The fixtures below register exactly the routes
that init() consumes; when init() grows resources, add the matching
routes here.
"""

import ssl

from aiohttp import ClientSession
import pytest
import pytest_asyncio

from pydaikin.daikin_airbase import DaikinAirBase
from pydaikin.daikin_brp069 import DaikinBRP069
from pydaikin.daikin_brp072c import DaikinBRP072C

BASIC_INFO_RESPONSE = "ret=OK,type=aircon,reg=eu,dst=1,ver=1_2_54,rev=203DE8C,pow=1,err=0,location=0,name=%4e%6f%74%74%65,icon=3,method=home only,port=30050,id=,pw=,lpw_flag=0,adp_kind=3,pv=3.20,cpv=3,cpv_minor=20,led=1,en_setzone=1,mac=409F38D107AC,adp_mode=run,en_hol=0,ssid1=Pinguino Curioso,radio1=-35,grp_name=,en_grp=0"

SENSOR_INFO_RESPONSE = "ret=OK,htemp=25.0,hhum=-,otemp=21.0,err=0,cmpfreq=40"

CONTROL_INFO_RESPONSE = "ret=OK,pow=1,mode=2,adv=,stemp=M,shum=50,dt1=25.0,dt2=M,dt3=25.0,dt4=25.0,dt5=25.0,dt7=25.0,dh1=AUTO,dh2=50,dh3=0,dh4=0,dh5=0,dh7=AUTO,dhh=50,b_mode=2,b_stemp=M,b_shum=50,alert=255,f_rate=A,f_dir=0,b_f_rate=5,b_f_dir=0,dfr1=5,dfr2=5,dfr3=A,dfr4=5,dfr5=5,dfr6=3,dfr7=5,dfrh=5,dfd1=0,dfd2=0,dfd3=2,dfd4=0,dfd5=0,dfd6=2,dfd7=0,dfdh=0,dmnd_run=0,en_demand=0"

HOLIDAY_RESPONSE = "ret=OK,en_hol=0"

WEEK_POWER_RESPONSE = "ret=OK,today_runtime=38,datas=5700/4000/6100/3900/2200/3400/400"

YEAR_POWER_RESPONSE = (
    "ret=OK,previous_year=7/0/1/0/1/21/57/24/2/0/0/2,this_year=4/0/0/0/1/18/40/53"
)


@pytest_asyncio.fixture
async def client_session():
    client_session = ClientSession()
    yield client_session
    await client_session.close()


def add_brp069_init_routes(aresponses, prefix=""):
    """Register the routes the slimmed init() fetches."""
    aresponses.add(
        path_pattern=f"{prefix}/common/basic_info",
        method_pattern="GET",
        response=BASIC_INFO_RESPONSE,
    )
    aresponses.add(
        path_pattern=f"{prefix}/aircon/get_sensor_info",
        method_pattern="GET",
        response=SENSOR_INFO_RESPONSE,
    )
    aresponses.add(
        path_pattern=f"{prefix}/aircon/get_control_info",
        method_pattern="GET",
        response=CONTROL_INFO_RESPONSE,
    )
    aresponses.add(
        path_pattern=f"{prefix}/common/get_holiday",
        method_pattern="GET",
        response=HOLIDAY_RESPONSE,
    )
    aresponses.add(
        path_pattern=f"{prefix}/aircon/get_week_power",
        method_pattern="GET",
        response=WEEK_POWER_RESPONSE,
    )
    aresponses.add(
        path_pattern=f"{prefix}/aircon/get_year_power",
        method_pattern="GET",
        response=YEAR_POWER_RESPONSE,
    )


@pytest.mark.asyncio
async def test_daikinBRP069(aresponses, client_session):
    add_brp069_init_routes(aresponses)

    device = DaikinBRP069('ip', session=client_session)

    await device.init()

    aresponses.assert_all_requests_matched()
    aresponses.assert_no_unused_routes()

    assert device.values.get('mac') == '409F38D107AC'
    assert device.values.get('pow') == '1'
    assert device.inside_temperature == 25.0


@pytest.mark.asyncio
async def test_daikinBRP072C(aresponses, client_session):
    aresponses.add(
        path_pattern="/common/register_terminal",
        method_pattern="GET",
        response="ret=OK",
    )
    add_brp069_init_routes(aresponses)

    device = DaikinBRP072C('ip', session=client_session, key="xxxkeyxxx")

    await device.init()

    aresponses.assert_all_requests_matched()
    aresponses.assert_no_unused_routes()

    assert device.values.get('pow') == '1'


@pytest.mark.asyncio
async def test_daikinAirBase(aresponses, client_session):
    add_brp069_init_routes(aresponses, prefix="/skyfi")

    device = DaikinAirBase('ip', session=client_session)

    await device.init()

    aresponses.assert_all_requests_matched()
    aresponses.assert_no_unused_routes()

    assert device.values.get('pow') == '1'
    # AirBase DEFAULTS applied on top of fetched values
    assert device.values.get('shum') == '50'


# --- M2: init() must fetch the support-flag gating resources -----------------


@pytest.mark.asyncio
async def test_init_detects_away_and_energy_support(monkeypatch):
    """holiday/week-power/year-power fetched at init flip the support flags."""
    device = DaikinBRP069('127.0.0.1', session=object())

    resource_map = {
        'common/basic_info': {'mac': '409F38D107AC'},
        'aircon/get_sensor_info': {'htemp': '25.0', 'otemp': '21.0'},
        'aircon/get_control_info': {'pow': '1', 'mode': '3'},
        'common/get_holiday': {'en_hol': '0'},
        'aircon/get_week_power': {'datas': '100/200/300/400/500/600/700'},
        'aircon/get_year_power': {
            'previous_year': '10/10/10',
            'this_year': '1/2/3',
        },
    }

    async def fake_get_resource(resource, *args, **kwargs):
        return resource_map[resource]

    monkeypatch.setattr(device, '_get_resource', fake_get_resource)

    await device.init()

    assert device.support_away_mode is True
    assert device.support_energy_consumption is True
    assert 'aircon/get_day_power_ex' in device.get_info_resources()


@pytest.mark.asyncio
async def test_init_unsupported_endpoints_keep_flags_false(monkeypatch):
    """Units lacking the endpoints (404/ret!=OK -> {}) keep both flags False."""
    device = DaikinBRP069('127.0.0.1', session=object())

    resource_map = {
        'common/basic_info': {'mac': '409F38D107AC'},
        'aircon/get_sensor_info': {'htemp': '25.0', 'otemp': '21.0'},
        'aircon/get_control_info': {'pow': '1', 'mode': '3'},
        'common/get_holiday': {},
        'aircon/get_week_power': {},
        'aircon/get_year_power': {},
    }

    async def fake_get_resource(resource, *args, **kwargs):
        return resource_map[resource]

    monkeypatch.setattr(device, '_get_resource', fake_get_resource)

    await device.init()

    assert device.support_away_mode is False
    assert device.support_energy_consumption is False
    assert device.get_info_resources() == DaikinBRP069.INFO_RESOURCES


# --- LOW-brp072c-ssl-context -------------------------------------------------


def test_brp072c_caller_ssl_context_not_mutated():
    """A caller-supplied ssl_context is used as-is — never hardened/mutated."""
    ctx = ssl.create_default_context()
    device = DaikinBRP072C('1.2.3.4', session=object(), key='k', ssl_context=ctx)
    assert device.ssl_context is ctx
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_brp072c_default_ssl_context_hardened():
    """Without a caller context, BRP072C builds its own legacy-TLS context."""
    device = DaikinBRP072C('1.2.3.4', session=object(), key='k')
    assert device.ssl_context.verify_mode == ssl.CERT_NONE
    assert device.ssl_context.check_hostname is False
    # SSL_OP_LEGACY_SERVER_CONNECT
    assert device.ssl_context.options & 0x4
