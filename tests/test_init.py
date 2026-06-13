"""Verify that init() calls the expected set of endpoints for each Daikin device.

The fork's BRP069 init() is deliberately slimmed to the essential resources
(common/basic_info, aircon/get_sensor_info, aircon/get_control_info) for
faster startup — see daikin_brp069.py init(). The fixtures below register
exactly the routes that init() consumes; when init() grows resources
(e.g. holiday/week-power/year-power for away-mode + energy support, audit
item M2), add the matching routes here.
"""

from aiohttp import ClientSession
import pytest
import pytest_asyncio

from pydaikin.daikin_airbase import DaikinAirBase
from pydaikin.daikin_brp069 import DaikinBRP069
from pydaikin.daikin_brp072c import DaikinBRP072C

BASIC_INFO_RESPONSE = "ret=OK,type=aircon,reg=eu,dst=1,ver=1_2_54,rev=203DE8C,pow=1,err=0,location=0,name=%4e%6f%74%74%65,icon=3,method=home only,port=30050,id=,pw=,lpw_flag=0,adp_kind=3,pv=3.20,cpv=3,cpv_minor=20,led=1,en_setzone=1,mac=409F38D107AC,adp_mode=run,en_hol=0,ssid1=Pinguino Curioso,radio1=-35,grp_name=,en_grp=0"

SENSOR_INFO_RESPONSE = "ret=OK,htemp=25.0,hhum=-,otemp=21.0,err=0,cmpfreq=40"

CONTROL_INFO_RESPONSE = "ret=OK,pow=1,mode=2,adv=,stemp=M,shum=50,dt1=25.0,dt2=M,dt3=25.0,dt4=25.0,dt5=25.0,dt7=25.0,dh1=AUTO,dh2=50,dh3=0,dh4=0,dh5=0,dh7=AUTO,dhh=50,b_mode=2,b_stemp=M,b_shum=50,alert=255,f_rate=A,f_dir=0,b_f_rate=5,b_f_dir=0,dfr1=5,dfr2=5,dfr3=A,dfr4=5,dfr5=5,dfr6=3,dfr7=5,dfrh=5,dfd1=0,dfd2=0,dfd3=2,dfd4=0,dfd5=0,dfd6=2,dfd7=0,dfdh=0,dmnd_run=0,en_demand=0"


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
