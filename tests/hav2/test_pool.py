"""Unit testy řízení filtrace HAv2 (config/appdaemon/apps/hav2/hav2_pool.py)."""

import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "config/appdaemon/apps/hav2"))

from hav2_pool import (  # noqa: E402
    STATE_DONE,
    STATE_MANUAL,
    STATE_NT,
    STATE_OFF_SEASON,
    STATE_SOLAR,
    STATE_WAIT,
    PoolController,
    PoolInputs,
    expected_nt_hours,
    pool_day_start,
    target_hours,
)

TZ = timezone(timedelta(hours=2))
T0 = datetime(2026, 9, 26, 11, 0, tzinfo=TZ)


def inputs(**kw):
    base = dict(now=T0, season=True, manual="Auto", hours_done=0.0, target_h=6.0,
                surplus_w=1000, is_nt=False, running=False)
    base.update(kw)
    return PoolInputs(**base)


def run(ctl, inp, minutes, **changes):
    """Simuluje smyčku po minutách: povel se stává stavem čerpadla, běh přičítá hodiny."""
    cmd = None
    for _ in range(minutes):
        inp = replace(inp, now=inp.now + timedelta(minutes=1), **changes)
        cmd = ctl.step(inp)
        inp = replace(inp, running=cmd.on, hours_done=inp.hours_done + (1 / 60 if cmd.on else 0))
    return cmd, inp


def test_day_starts_at_six():
    assert pool_day_start(datetime(2026, 9, 26, 5, 59, tzinfo=TZ)).day == 25
    assert pool_day_start(datetime(2026, 9, 26, 6, 0, tzinfo=TZ)).day == 26


def test_target_hours_choice():
    assert target_hours(6, 8.5, True) == 8.5
    assert target_hours(6, 8.5, False) == 6
    assert target_hours(6, None, True) == 6


def test_starts_after_stable_surplus():
    ctl = PoolController()
    cmd, inp = run(ctl, inputs(), 4)
    assert not cmd.on and cmd.state == STATE_WAIT
    cmd, _ = run(ctl, inp, 2)
    assert cmd.on and cmd.state == STATE_SOLAR


def test_short_dip_does_not_start():
    ctl = PoolController()
    _, inp = run(ctl, inputs(), 3)
    _, inp = run(ctl, inp, 1, surplus_w=300)
    cmd, _ = run(ctl, inp, 3, surplus_w=1000)
    assert not cmd.on


def test_min_run_then_stop_after_low_surplus():
    ctl = PoolController()
    cmd, inp = run(ctl, inputs(), 6)
    assert cmd.on
    cmd, inp = run(ctl, inp, 30, surplus_w=0)
    assert cmd.on  # minimální běh 60 min
    cmd, inp = run(ctl, inp, 35, surplus_w=0)
    assert not cmd.on and cmd.state == STATE_WAIT


def test_keeps_running_with_surplus():
    ctl = PoolController()
    _, inp = run(ctl, inputs(), 6)
    cmd, _ = run(ctl, inp, 120, surplus_w=800)
    assert cmd.on


def test_max_solar_starts():
    ctl = PoolController()
    inp = inputs()
    for _ in range(4):
        _, inp = run(ctl, inp, 6, surplus_w=1000)
        _, inp = run(ctl, inp, 71, surplus_w=0)
    assert ctl.solar_starts == 4
    cmd, _ = run(ctl, inp, 10, surplus_w=1000)
    assert not cmd.on and "limit" in cmd.reason


def test_nt_fills_missing_hours_and_stops_when_done():
    ctl = PoolController()
    inp = inputs(now=datetime(2026, 9, 26, 22, 0, tzinfo=TZ), is_nt=True, surplus_w=-500, hours_done=4.0)
    cmd, inp = run(ctl, inp, 60)
    assert cmd.on and cmd.state == STATE_NT
    cmd, _ = run(ctl, inp, 65)
    assert not cmd.on and cmd.state == STATE_DONE


def test_done_does_not_start():
    ctl = PoolController()
    cmd, _ = run(ctl, inputs(hours_done=6.0), 10)
    assert not cmd.on and cmd.state == STATE_DONE


def test_manual_and_season():
    ctl = PoolController()
    assert ctl.step(inputs(manual="Zapnout", hours_done=9)).on
    assert ctl.step(inputs(manual="Zapnout")).state == STATE_MANUAL
    assert not ctl.step(inputs(manual="Vypnout", is_nt=True)).on
    cmd = ctl.step(inputs(season=False, is_nt=True))
    assert not cmd.on and cmd.state == STATE_OFF_SEASON


def test_solar_start_counter_resets_next_day():
    ctl = PoolController()
    ctl.solar_starts = 4
    ctl.day = pool_day_start(T0)
    ctl.step(inputs(now=T0 + timedelta(days=1)))
    assert ctl.solar_starts == 0


def test_expected_nt_hours():
    assert expected_nt_hours(6, 1, 3) == 2
    assert expected_nt_hours(6, 5, 3) == 0
