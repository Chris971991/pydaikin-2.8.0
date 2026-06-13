"""Tests for the DaikinBRP084 (firmware 2.8.0) appliance."""

import asyncio
import json
import logging
import math

from aiohttp import ClientResponseError, ClientSession
import pytest
import pytest_asyncio

from pydaikin.daikin_brp084 import DaikinAttribute, DaikinBRP084, DaikinRequest
from pydaikin.exceptions import DaikinException, DaikinRejectedValueError

ADR_0100 = "/dsiot/edge/adr_0100.dgc_status"
ADR_0200 = "/dsiot/edge/adr_0200.dgc_status"
WEEK_POWER = "/dsiot/edge/adr_0100.i_power.week_power"
ADP_I = "/dsiot/edge.adp_i"

# e_3001 node sets per mode (target temp / fan / swing vertical+horizontal)
COOL_NODES = [
    {"pn": "p_02", "pv": "32"},  # Cool temp (25°C)
    {"pn": "p_09", "pv": "0A00"},  # Cool fan speed (AUTO)
    {"pn": "p_05", "pv": "000000"},  # Vertical swing OFF
    {"pn": "p_06", "pv": "000000"},  # Horizontal swing OFF
]

HOT_NODES = [
    {"pn": "p_03", "pv": "2c"},  # Heat temp (22°C)
    {"pn": "p_0A", "pv": "0A00"},  # Heat fan speed (AUTO)
    {"pn": "p_07", "pv": "000000"},  # Vertical swing OFF
    {"pn": "p_08", "pv": "000000"},  # Horizontal swing OFF
]

AUTO_NODES = [
    {"pn": "p_1D", "pv": "30"},  # Auto temp (24°C)
    {"pn": "p_26", "pv": "0A00"},  # Auto fan speed (AUTO)
    {"pn": "p_20", "pv": "000000"},  # Vertical swing OFF
    {"pn": "p_21", "pv": "000000"},  # Horizontal swing OFF
]

FAN_NODES = [
    {"pn": "p_28", "pv": "0A00"},  # Fan-mode fan speed (AUTO)
    {"pn": "p_24", "pv": "000000"},  # Vertical swing OFF
    {"pn": "p_25", "pv": "000000"},  # Horizontal swing OFF
]


@pytest_asyncio.fixture
async def client_session():
    client_session = ClientSession()
    yield client_session
    await client_session.close()


def make_device():
    """Device with a dummy session: for tests that perform no HTTP."""
    return DaikinBRP084('127.0.0.1', session=object())


def make_status_response(
    power="01",
    mode="0200",
    mode_nodes=COOL_NODES,
    include_humidity=True,
    adr_0200_failed=False,
    adp_i_failed=False,
):
    """Build a full multireq status response body."""
    e3001 = [{"pn": "p_01", "pv": mode}] + list(mode_nodes)
    a00b = [{"pn": "p_01", "pv": "18"}]  # Room temp (24°C)
    if include_humidity:
        a00b.append({"pn": "p_02", "pv": "3c"})  # Humidity (60%)

    adr_0100_entry = {
        "fr": ADR_0100,
        "pc": {
            "pn": "dgc_status",
            "pch": [
                {
                    "pn": "e_1002",
                    "pch": [
                        {"pn": "e_A001", "pch": [{"pn": "p_0D", "pv": "31323334"}]},
                        {"pn": "e_A002", "pch": [{"pn": "p_01", "pv": power}]},
                        {"pn": "e_3001", "pch": e3001},
                        {"pn": "e_A00B", "pch": a00b},
                    ],
                }
            ],
        },
        "rsc": 2000,
    }

    if adr_0200_failed:
        # Failed sub-response: carries fr/rsc but no 'pc' key.
        adr_0200_entry = {"fr": ADR_0200, "rsc": 4004}
    else:
        adr_0200_entry = {
            "fr": ADR_0200,
            "pc": {
                "pn": "dgc_status",
                "pch": [
                    {
                        "pn": "e_1003",
                        "pch": [
                            {
                                "pn": "e_A00D",
                                "pch": [{"pn": "p_01", "pv": "22"}],  # 17°C
                            }
                        ],
                    }
                ],
            },
            "rsc": 2000,
        }

    week_power_entry = {
        "fr": WEEK_POWER,
        "pc": {
            "pn": "week_power",
            "pch": [
                {"pn": "today_runtime", "pv": "120"},
                {"pn": "datas", "pv": [100, 200, 300, 400, 500, 600, 700]},
            ],
        },
        "rsc": 2000,
    }

    if adp_i_failed:
        adp_i_entry = {"fr": ADP_I, "rsc": 4004}
    else:
        adp_i_entry = {
            "fr": ADP_I,
            "pc": {"pn": "adp_i", "pch": [{"pn": "mac", "pv": "112233445566"}]},
            "rsc": 2000,
        }

    return {
        "responses": [adr_0100_entry, adr_0200_entry, week_power_entry, adp_i_entry]
    }


def rsc_response(rsc):
    """Build a set-response body with the given rsc status code."""
    return {"responses": [{"fr": ADR_0100, "rsc": rsc}]}


def add_json_route(aresponses, body, repeat=1):
    """Register one POST /dsiot/multireq route answering with a JSON body."""
    aresponses.add(
        path_pattern="/dsiot/multireq",
        method_pattern="POST",
        response=aresponses.Response(
            status=200,
            text=json.dumps(body),
            headers={"Content-Type": "application/json"},
        ),
        repeat=repeat,
    )


def add_recording_route(aresponses, recorded, body, repeat=1):
    """Register a route that records the request payload before answering.

    `body` is either a dict or a callable(payload) -> dict.
    """

    async def handler(request):
        payload = await request.json()
        recorded.append(payload)
        response_body = body(payload) if callable(body) else body
        return aresponses.Response(
            status=200,
            text=json.dumps(response_body),
            headers={"Content-Type": "application/json"},
        )

    aresponses.add(
        path_pattern="/dsiot/multireq",
        method_pattern="POST",
        response=handler,
        repeat=repeat,
    )


def collect_leaves(payload):
    """Flatten a multireq payload into (parent_pn, pn, pv) triples."""
    leaves = []

    def walk(node, parent):
        if "pv" in node:
            leaves.append((parent, node["pn"], node["pv"]))
        for child in node.get("pch", []):
            walk(child, node.get("pn"))

    for request in payload.get("requests", []):
        walk(request["pc"], None)
    return leaves


@pytest.mark.asyncio
async def test_daikin_brp084(aresponses, client_session):
    """Init parses status; a temp-only set() clips and refreshes once."""
    add_json_route(aresponses, make_status_response())  # init
    add_json_route(aresponses, rsc_response(2004))  # temp request accepted
    add_json_route(aresponses, make_status_response())  # trailing refresh

    device = DaikinBRP084('ip', session=client_session)
    await device.init()

    # Check basic properties
    assert device.values.get('mode') == 'cool'
    assert device.values.get('pow') == '1'
    assert device.values.get('stemp') == '25.0'
    assert device.values.get('f_rate') == 'auto'
    assert device.values.get('htemp') == '24.0'
    assert device.values.get('otemp') == '17.0'
    assert device.values.get('f_dir') == 'off'
    assert device.values.get('mac') == '112233445566'

    # Test setting temperature
    await device.set({'stemp': '26.0'})
    assert device.last_temperature_adjustment is None

    aresponses.assert_all_requests_matched()
    aresponses.assert_no_unused_routes()


@pytest.mark.asyncio
async def test_add_request_method(aresponses, client_session):
    """Test the request-building handlers and the full set() flow."""
    add_json_route(aresponses, make_status_response())  # init
    add_json_route(aresponses, rsc_response(2004))  # power/mode/fan/swing multireq
    add_json_route(aresponses, rsc_response(2004))  # temp request
    add_json_route(aresponses, make_status_response())  # trailing refresh

    device = DaikinBRP084('ip', session=client_session)
    await device.init()

    # Test power setting
    requests = []
    device._handle_power_setting({'mode': 'off'}, requests)
    assert len(requests) == 1
    assert requests[0].name == "p_01"
    assert requests[0].value == "00"

    # Test power on and mode setting
    requests = []
    device._handle_power_setting({'mode': 'cool'}, requests)
    assert len(requests) == 2
    assert requests[0].name == "p_01"
    assert requests[0].value == "01"  # Power on
    assert requests[1].name == "p_01"
    assert requests[1].value == "0200"  # Cool mode

    # Test fan setting (handlers take the target mode explicitly)
    requests = []
    device._handle_fan_setting({'f_rate': 'auto'}, requests, 'cool')
    assert len(requests) == 1
    assert requests[0].name == "p_09"  # Cool mode fan parameter
    assert requests[0].value == "0A00"  # Auto fan speed

    # Test swing setting
    requests = []
    device._handle_swing_setting({'f_dir': 'both'}, requests, 'cool')
    assert len(requests) == 2
    assert requests[0].name == "p_05"  # Vertical swing parameter for cool mode
    assert requests[0].value == device.TURN_ON_SWING_AXIS
    assert requests[1].name == "p_06"  # Horizontal swing parameter for cool mode
    assert requests[1].value == device.TURN_ON_SWING_AXIS

    # Test the full set method with multiple settings
    await device.set(
        {'mode': 'cool', 'stemp': '26.0', 'f_rate': 'auto', 'f_dir': 'both'}
    )

    aresponses.assert_all_requests_matched()
    aresponses.assert_no_unused_routes()


@pytest.mark.asyncio
async def test_add_request_direct(client_session):
    """Test the add_request method directly."""
    device = DaikinBRP084('ip', session=client_session)

    # Initialize requests list
    requests = []

    # Test adding a power request
    power_path = device.get_path("power")
    device.add_request(requests, power_path, "01")  # Power on

    assert len(requests) == 1
    assert requests[0].name == "p_01"
    assert requests[0].value == "01"
    assert requests[0].path == ["e_1002", "e_A002"]
    assert requests[0].to == "/dsiot/edge/adr_0100.dgc_status"

    # Test adding a mode request
    mode_path = device.get_path("mode")
    device.add_request(requests, mode_path, "0200")  # Cool mode

    assert len(requests) == 2
    assert requests[1].name == "p_01"
    assert requests[1].value == "0200"
    assert requests[1].path == ["e_1002", "e_3001"]
    assert requests[1].to == "/dsiot/edge/adr_0100.dgc_status"

    # Test adding a temperature request
    temp_path = device.get_path("temp_settings", "cool")
    device.add_request(requests, temp_path, "32")  # 25°C

    assert len(requests) == 3
    assert requests[2].name == "p_02"
    assert requests[2].value == "32"
    assert requests[2].path == ["e_1002", "e_3001"]
    assert requests[2].to == "/dsiot/edge/adr_0100.dgc_status"


# --- H1: case-insensitive fan/swing handlers ---


def test_handle_fan_setting_case_insensitive(caplog):
    """f_rate accepts any case, passes raw codes through, warns on unknown."""
    device = make_device()

    requests = []
    device._handle_fan_setting({'f_rate': 'Auto'}, requests, 'cool')
    assert [r.value for r in requests] == ['0A00']

    requests = []
    device._handle_fan_setting({'f_rate': 'Quiet'}, requests, 'cool')
    assert [r.value for r in requests] == ['0B00']

    requests = []
    device._handle_fan_setting({'f_rate': '0A00'}, requests, 'cool')
    assert [r.value for r in requests] == ['0A00']

    requests = []
    with caplog.at_level(logging.WARNING):
        device._handle_fan_setting({'f_rate': 'bogus'}, requests, 'cool')
    assert requests == []
    assert 'Unsupported f_rate' in caplog.text


def test_handle_swing_setting_case_insensitive():
    """f_dir accepts any case; 'Off' turns both axes off, '3D' both on."""
    device = make_device()

    requests = []
    device._handle_swing_setting({'f_dir': 'Off'}, requests, 'cool')
    assert [r.value for r in requests] == [device.TURN_OFF_SWING_AXIS] * 2

    requests = []
    device._handle_swing_setting({'f_dir': '3D'}, requests, 'cool')
    assert [r.value for r in requests] == [device.TURN_ON_SWING_AXIS] * 2

    requests = []
    device._handle_swing_setting({'f_dir': 'Horizontal'}, requests, 'cool')
    assert requests[0].name == 'p_05'  # vertical
    assert requests[0].value == device.TURN_OFF_SWING_AXIS
    assert requests[1].name == 'p_06'  # horizontal
    assert requests[1].value == device.TURN_ON_SWING_AXIS


# --- H2: canonical 'hot' mode name, 'heat' accepted as alias ---


@pytest.mark.asyncio
async def test_brp084_heat_mode(aresponses, client_session):
    """An actively heating BRP084 reports mode 'hot'; set accepts 'heat'."""
    add_json_route(aresponses, make_status_response(mode='0100', mode_nodes=HOT_NODES))
    recorded = []
    add_recording_route(aresponses, recorded, rsc_response(2004))
    add_json_route(aresponses, make_status_response(mode='0100', mode_nodes=HOT_NODES))

    device = DaikinBRP084('ip', session=client_session)
    await device.init()

    assert device.values['mode'] == 'hot'
    assert device.values['stemp'] == '22.0'  # parsed via the renamed key

    requests = []
    device._handle_power_setting({'mode': 'hot'}, requests)
    assert requests[1].name == 'p_01'
    assert requests[1].value == '0100'

    # The legacy 'heat' alias maps to 'hot' and sends mode code 0100
    await device.set({'mode': 'heat'})
    leaves = collect_leaves(recorded[0])
    assert ('e_A002', 'p_01', '01') in leaves  # power on
    assert ('e_3001', 'p_01', '0100') in leaves  # heat mode code

    aresponses.assert_all_requests_matched()
    aresponses.assert_no_unused_routes()


# --- M6: unmapped firmware mode code must not crash the poll ---


@pytest.mark.asyncio
async def test_unknown_mode_code(aresponses, client_session, caplog):
    """An unmapped mode code degrades to 'auto' with a warning."""
    add_json_route(aresponses, make_status_response(mode='0700', mode_nodes=AUTO_NODES))

    device = DaikinBRP084('ip', session=client_session)
    with caplog.at_level(logging.WARNING):
        await device.init()

    assert device.values['mode'] == 'auto'
    assert device.values['stemp'] == '24.0'
    assert 'Unknown BRP084 mode code' in caplog.text


# --- M7: set() restructure ---


@pytest.mark.asyncio
async def test_set_mode_and_out_of_range_temp(aresponses, client_session):
    """Temp is sent separately after the mode multireq and clipped on 4000."""
    add_json_route(aresponses, make_status_response())  # init in cool
    recorded = []
    add_recording_route(aresponses, recorded, rsc_response(2004))  # power+mode
    add_recording_route(aresponses, recorded, rsc_response(4000))  # temp rejected
    add_recording_route(aresponses, recorded, rsc_response(2004))  # temp retry ok
    add_json_route(aresponses, make_status_response(mode='0100', mode_nodes=HOT_NODES))

    device = DaikinBRP084('ip', session=client_session)
    await device.init()

    result = await device.set({'mode': 'hot', 'stemp': '22.0'})
    assert result == {'detected_power_off': False, 'current_val': None}

    multireq_leaves = collect_leaves(recorded[0])
    assert ('e_A002', 'p_01', '01') in multireq_leaves  # power on
    assert ('e_3001', 'p_01', '0100') in multireq_leaves  # mode request present
    assert not [leaf for leaf in multireq_leaves if leaf[1] == 'p_03']  # no temp

    assert collect_leaves(recorded[1]) == [('e_3001', 'p_03', '2c')]  # 22.0
    assert collect_leaves(recorded[2]) == [('e_3001', 'p_03', '2d')]  # 22.5

    assert device.last_temperature_adjustment['requested'] == 22.0
    assert device.last_temperature_adjustment['actual'] == 22.5

    aresponses.assert_all_requests_matched()
    aresponses.assert_no_unused_routes()


@pytest.mark.asyncio
async def test_set_failure_does_not_pollute_values(aresponses, client_session):
    """A rejected set() leaves self.values untouched (no optimistic mutation)."""
    add_json_route(aresponses, make_status_response(mode='0100', mode_nodes=HOT_NODES))
    add_json_route(aresponses, rsc_response(4000))  # multireq rejected

    device = DaikinBRP084('ip', session=client_session)
    await device.init()
    assert device.values['mode'] == 'hot'

    with pytest.raises(DaikinException):
        await device.set({'mode': 'cool'})

    assert device.values['mode'] == 'hot'
    assert device.values['pow'] == '1'

    aresponses.assert_all_requests_matched()
    aresponses.assert_no_unused_routes()


@pytest.mark.asyncio
async def test_temp_only_unsupported_mode_warns(aresponses, client_session, caplog):
    """A temp-only set in fan mode warns and sends nothing (no exception)."""
    add_json_route(aresponses, make_status_response(mode='0000', mode_nodes=FAN_NODES))

    device = DaikinBRP084('ip', session=client_session)
    await device.init()
    assert device.values['mode'] == 'fan'

    with caplog.at_level(logging.WARNING):
        result = await device.set({'stemp': '24'})

    assert result == {'detected_power_off': False, 'current_val': None}
    assert 'not supported in mode' in caplog.text
    # Only the init request went out: no set request, no refresh.
    aresponses.assert_all_requests_matched()
    aresponses.assert_no_unused_routes()


# --- M8: temperature clipping ---


@pytest.mark.asyncio
async def test_clipping_no_duplicates_and_cap(aresponses, client_session):
    """Clipping tries at most 8 deduplicated candidates then raises."""
    add_json_route(aresponses, make_status_response())  # init in cool
    recorded = []
    add_recording_route(aresponses, recorded, rsc_response(4000), repeat=math.inf)

    device = DaikinBRP084('ip', session=client_session)
    await device.init()

    with pytest.raises(DaikinException) as excinfo:
        await device.set({'stemp': '21.0'})

    assert not isinstance(excinfo.value, DaikinRejectedValueError)
    assert 'No valid temperature found' in str(excinfo.value)
    assert len(recorded) == 8  # hard cap
    pvs = [collect_leaves(payload)[0][2] for payload in recorded]
    assert len(set(pvs)) == len(pvs)  # no duplicate temperature requests


@pytest.mark.asyncio
async def test_clipping_clamps_out_of_range(aresponses, client_session):
    """An out-of-range request is clamped FIRST: 12°C tries 16°C immediately."""
    add_json_route(aresponses, make_status_response())  # init in cool
    recorded = []

    def body(payload):
        pv = collect_leaves(payload)[0][2]
        return rsc_response(2004) if pv == '20' else rsc_response(4000)

    add_recording_route(aresponses, recorded, body)
    add_json_route(aresponses, make_status_response())  # trailing refresh

    device = DaikinBRP084('ip', session=client_session)
    await device.init()

    await device.set({'stemp': '12.0'})

    assert len(recorded) == 1
    assert collect_leaves(recorded[0]) == [('e_3001', 'p_02', '20')]  # 16.0 first
    assert device.last_temperature_adjustment['requested'] == 12.0
    assert device.last_temperature_adjustment['actual'] == 16.0

    aresponses.assert_all_requests_matched()
    aresponses.assert_no_unused_routes()


@pytest.mark.asyncio
async def test_clipping_non_4000_propagates(aresponses, client_session):
    """A non-4000 device error stops clipping after exactly one attempt."""
    add_json_route(aresponses, make_status_response())  # init in cool
    recorded = []
    add_recording_route(aresponses, recorded, rsc_response(4101))

    device = DaikinBRP084('ip', session=client_session)
    await device.init()

    with pytest.raises(DaikinException):
        await device.set({'stemp': '21.0'})

    assert len(recorded) == 1

    aresponses.assert_all_requests_matched()
    aresponses.assert_no_unused_routes()


# --- LOW-brp084-temp-only-off: auto power-on via last_active_mode ---


@pytest.mark.asyncio
async def test_temp_only_while_off_powers_on(aresponses, client_session):
    """Temp-only set while off powers on using the latched active mode."""
    add_json_route(aresponses, make_status_response(power='01'))  # init: ON, cool
    add_json_route(aresponses, make_status_response(power='00'))  # poll: now OFF
    recorded = []
    add_recording_route(aresponses, recorded, rsc_response(2004))  # power-on
    add_recording_route(aresponses, recorded, rsc_response(2004))  # temp
    add_json_route(aresponses, make_status_response(power='01'))  # refresh

    device = DaikinBRP084('ip', session=client_session)
    await device.init()
    assert device.values['last_active_mode'] == 'cool'  # latched while ON

    await device.update_status()
    assert device.values['mode'] == 'off'
    assert device.values['last_active_mode'] == 'cool'  # latch kept while OFF

    await device.set({'stemp': '24.0'})

    # First multireq: power-on only, no bundled temperature.
    assert collect_leaves(recorded[0]) == [('e_A002', 'p_01', '01')]
    # Temp request goes to the cool path (latched mode), 24.0°C.
    assert collect_leaves(recorded[1]) == [('e_3001', 'p_02', '30')]

    aresponses.assert_all_requests_matched()
    aresponses.assert_no_unused_routes()


@pytest.mark.asyncio
async def test_temp_only_while_off_no_latch_falls_back_auto(aresponses, client_session):
    """Without a latched mode (never seen ON), auto power-on uses 'auto'."""
    add_json_route(aresponses, make_status_response(power='00'))  # init: OFF
    recorded = []
    add_recording_route(aresponses, recorded, rsc_response(2004))  # power-on
    add_recording_route(aresponses, recorded, rsc_response(2004))  # temp
    add_json_route(
        aresponses, make_status_response(power='01', mode='0300', mode_nodes=AUTO_NODES)
    )

    device = DaikinBRP084('ip', session=client_session)
    await device.init()
    # Conservative latch: the device was never seen ON, so no latch exists.
    assert 'last_active_mode' not in device.values

    await device.set({'stemp': '24.0'})

    assert collect_leaves(recorded[0]) == [('e_A002', 'p_01', '01')]
    assert collect_leaves(recorded[1]) == [('e_3001', 'p_1D', '30')]  # auto path

    aresponses.assert_all_requests_matched()
    aresponses.assert_no_unused_routes()


@pytest.mark.asyncio
async def test_fan_only_while_off_powers_on(aresponses, client_session):
    """Fan-only set while off powers on and applies the fan rate."""
    add_json_route(aresponses, make_status_response(power='01'))  # init: ON, cool
    add_json_route(aresponses, make_status_response(power='00'))  # poll: now OFF
    recorded = []
    add_recording_route(aresponses, recorded, rsc_response(2004))
    add_json_route(aresponses, make_status_response(power='01'))  # refresh

    device = DaikinBRP084('ip', session=client_session)
    await device.init()
    await device.update_status()

    await device.set({'f_rate': '3'})

    leaves = collect_leaves(recorded[0])
    assert ('e_A002', 'p_01', '01') in leaves  # power on
    assert ('e_3001', 'p_09', '0500') in leaves  # fan rate 3 on the cool path
    assert len(leaves) == 2

    aresponses.assert_all_requests_matched()
    aresponses.assert_no_unused_routes()


# --- M5: failed sub-responses raise DaikinException, never KeyError ---


@pytest.mark.asyncio
async def test_failed_subresponse_raises_daikin_exception(aresponses, client_session):
    """A required key inside a failed sub-response raises DaikinException."""
    add_json_route(aresponses, make_status_response(adp_i_failed=True))

    device = DaikinBRP084('ip', session=client_session)
    try:
        with pytest.raises(DaikinException):
            await device.update_status()
    except KeyError:
        pytest.fail("update_status raised KeyError instead of DaikinException")


@pytest.mark.asyncio
async def test_optional_value_degrades_gracefully(aresponses, client_session):
    """Failed OPTIONAL sub-responses degrade to placeholders; poll completes."""
    add_json_route(
        aresponses,
        make_status_response(adr_0200_failed=True, include_humidity=False),
    )

    device = DaikinBRP084('ip', session=client_session)
    await device.update_status()

    assert device.values['otemp'] == '--'
    assert device.values['hhum'] == '--'
    # Required keys all parsed fine.
    assert device.values['mac'] == '112233445566'
    assert device.values['pow'] == '1'
    assert device.values['mode'] == 'cool'
    assert device.values['htemp'] == '24.0'


# --- LOW-brp084-serialize ---


def test_serialize_interleaved_paths():
    """Attributes graft under the MATCHED tree node, not the last one."""
    to = "/dsiot/edge/adr_0100.dgc_status"
    request = DaikinRequest(
        [
            DaikinAttribute('p_01', '01', ['e_1002', 'e_A002'], to),
            DaikinAttribute('p_01', '0200', ['e_1002', 'e_3001'], to),
            DaikinAttribute('p_0D', '00', ['e_1002', 'e_A002'], to),
        ]
    )
    payload = request.serialize()

    e1002_nodes = payload['requests'][0]['pc']['pch']
    assert len(e1002_nodes) == 1
    assert e1002_nodes[0]['pn'] == 'e_1002'

    children = {child['pn']: child for child in e1002_nodes[0]['pch']}
    a002_pns = [c['pn'] for c in children['e_A002']['pch']]
    e3001_pns = [c['pn'] for c in children['e_3001']['pch']]
    assert sorted(a002_pns) == ['p_01', 'p_0D']  # p_0D under e_A002, not e_3001
    assert e3001_pns == ['p_01']


# --- LOW-brp084-temp-to-hex ---


def test_temp_to_hex():
    """temp_to_hex rounds (not truncates) and two's-complements negatives."""
    assert DaikinBRP084.temp_to_hex(25.0) == '32'
    assert DaikinBRP084.temp_to_hex(21.3) == '2b'  # rounds to 21.5, not 21.0
    assert DaikinBRP084.temp_to_hex(-5.0) == 'f6'  # two's complement, not '-a'
    assert DaikinBRP084.hex_to_temp(DaikinBRP084.temp_to_hex(-5.0)) == -5.0
    assert DaikinBRP084.hex_to_temp(DaikinBRP084.temp_to_hex(22.5)) == 22.5


# --- LOW-brp084-temp-properties ---


def test_temperature_properties_missing_values():
    """Temperature properties return None (not 0.0) for missing/placeholder."""
    device = make_device()

    assert device.inside_temperature is None
    assert device.target_temperature is None
    assert device.outside_temperature is None

    device.values['htemp'] = '24.0'
    device.values['stemp'] = '--'
    device.values['otemp'] = '--'
    assert device.inside_temperature == 24.0
    assert device.target_temperature is None
    assert device.outside_temperature is None

    device.values['stemp'] = '22.5'
    assert device.target_temperature == 22.5


# --- LOW-brp084-infra: bounded retry + timeout translation ---


@pytest.mark.asyncio
async def test_get_resource_retries_then_succeeds(aresponses, client_session):
    """A transient 500 is retried; the second attempt's body is returned."""
    aresponses.add(
        path_pattern="/dsiot/multireq",
        method_pattern="POST",
        response=aresponses.Response(status=500),
    )
    add_json_route(aresponses, make_status_response())

    device = DaikinBRP084('ip', session=client_session)
    result = await device._get_resource('', params={"requests": []})

    assert 'responses' in result
    assert len(aresponses.history) == 2
    aresponses.assert_all_requests_matched()
    aresponses.assert_no_unused_routes()


@pytest.mark.asyncio
async def test_get_resource_attempts_1_fails_fast(aresponses, client_session):
    """attempts=1 propagates the original error after exactly one request."""
    aresponses.add(
        path_pattern="/dsiot/multireq",
        method_pattern="POST",
        response=aresponses.Response(status=500),
        repeat=math.inf,
    )

    device = DaikinBRP084('ip', session=client_session)
    with pytest.raises(ClientResponseError):
        await device._get_resource('', params={"requests": []}, attempts=1)

    assert len(aresponses.history) == 1


@pytest.mark.asyncio
async def test_update_status_single_attempt(aresponses, client_session):
    """Polls never retry in-call: one failed request fails the poll."""
    aresponses.add(
        path_pattern="/dsiot/multireq",
        method_pattern="POST",
        response=aresponses.Response(status=500),
        repeat=math.inf,
    )

    device = DaikinBRP084('ip', session=client_session)
    with pytest.raises(DaikinException):
        await device.update_status()

    assert len(aresponses.history) == 1


@pytest.mark.asyncio
async def test_timeout_translated_after_final_attempt(client_session):
    """TimeoutError is retried, then the FINAL one is translated to a
    DaikinException whose message contains the word 'timeout' (the HA
    integration's log-level predicate depends on that substring)."""
    device = DaikinBRP084('ip', session=client_session)

    calls = []

    async def fake_post(params):
        calls.append(params)
        raise asyncio.TimeoutError

    device._post_request = fake_post

    with pytest.raises(DaikinException) as excinfo:
        await device._get_resource('', params={}, attempts=2)

    # Translation happens OUTSIDE the retry loop: both attempts ran.
    assert len(calls) == 2
    message = str(excinfo.value)
    assert message == f"Network timeout communicating with device at {device.device_ip}"
    assert 'timeout' in message
