"""Sync tests for DaikinPowerMixin guards (B:LOW-power-mixin).

Deliberately synchronous: they exercise pure computation in power.py and
must run regardless of async test configuration.
"""

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from pydaikin.power import ATTR_TOTAL, DaikinPowerMixin, EnergyConsumptionState


def _mixin_with_history(states):
    """Build a bare mixin with history populated oldest -> newest."""
    mixin = DaikinPowerMixin()
    mixin._energy_consumption_history = defaultdict(list)
    for state in states:
        # _register_energy_consumption_history inserts newest at index 0
        mixin._energy_consumption_history[ATTR_TOTAL].insert(0, state)
    return mixin


def _state(dt, first_state, today, yesterday=0.0):
    return EnergyConsumptionState(
        datetime=dt, first_state=first_state, today=today, yesterday=yesterday
    )


def test_current_power_consumption_min_power_none_no_typeerror():
    # 4 states so the energy_to_log subtraction (the max(est_power,
    # min_power) line) is actually reached with est_power > 0.
    now = datetime.now(timezone.utc)
    states = [
        _state(now - timedelta(minutes=40), True, 0.1),
        _state(now - timedelta(minutes=30), False, 0.2),
        _state(now - timedelta(minutes=20), False, 0.4),
        _state(now - timedelta(minutes=10), False, 0.7),
    ]
    mixin = _mixin_with_history(states)

    result = mixin.current_power_consumption(min_power=None)

    assert isinstance(result, (int, float))
    assert result >= 0


def test_current_power_consumption_zero_interval_no_zerodivision():
    # Two consecutive history states sharing a timestamp (possible under
    # freezegun or coarse clocks) must not divide by zero.
    now = datetime.now(timezone.utc)
    states = [
        _state(now, False, 0.1),
        _state(now, False, 0.2),
    ]
    mixin = _mixin_with_history(states)

    result = mixin.current_power_consumption()

    assert result == 0


def test_current_power_consumption_default_min_power_still_floors():
    # Regression guard: default min_power=0.1 semantics are preserved.
    now = datetime.now(timezone.utc)
    states = [
        _state(now - timedelta(minutes=40), True, 0.1),
        _state(now - timedelta(minutes=30), False, 0.101),
        _state(now - timedelta(minutes=20), False, 0.102),
    ]
    mixin = _mixin_with_history(states)

    result = mixin.current_power_consumption()

    assert result == 0 or result >= 0.1


def test_current_power_consumption_empty_history_returns_zero():
    mixin = _mixin_with_history([])
    assert mixin.current_power_consumption(min_power=None) == 0
