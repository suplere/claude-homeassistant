"""Unit testy EV plánovače a regulátoru HAv2 (config/appdaemon/apps/hav2/hav2_ev.py)."""

import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "config/appdaemon/apps/hav2"))

from hav2_ev import (  # noqa: E402
    STATE_DONE,
    STATE_MANUAL,
    STATE_PAUSED,
    STATE_PLAN,
    STATE_SOLAR,
    STATE_SUPPORT,
    EvPlanParams,
    EvSlot,
    RegInputs,
    Regulator,
    ev_power_kw,
    max_amps_for,
    plan_ev,
)

TZ = timezone(timedelta(hours=2))
T0 = datetime(2026, 9, 26, 11, 0, tzinfo=TZ)


def inputs(**kw):
    base = dict(now=T0, connected=True, available=True, mode="Solár+NT", manual="Auto",
                manual_current=11, manual_phases="Auto", target_reached=False, surplus_w=5000,
                battery_soc=80, sun_returns=True, plan_slot=None, worst_phase_a=10,
                boiler_heating=False, cur_enabled=True, cur_amps=6, cur_phases=3)
    base.update(kw)
    return RegInputs(**base)


def run(reg, inp, steps, dt=60, **changes):
    """Simuluje smyčku: výstup kroku se stává stavem wallboxu."""
    cmd = None
    for k in range(steps):
        inp = replace(inp, now=inp.now + timedelta(seconds=dt), **changes)
        cmd = reg.step(inp)
        inp = replace(inp, cur_enabled=cmd.enable, cur_amps=cmd.amps, cur_phases=cmd.phases)
    return cmd, inp


# ------------------------------------------------------------ výkon ↔ proud


def test_power_table_and_inverse():
    assert ev_power_kw(1, 6) == 1.27
    assert ev_power_kw(3, 6) == 4.17
    assert max_amps_for(3, 5.2, 6, 11) == 8
    assert max_amps_for(3, 4.0, 6, 11) is None
    assert max_amps_for(1, 2.0, 6, 11) == 10


# ---------------------------------------------------------------- regulátor


def test_ramps_up_one_amp_per_step():
    reg = Regulator(state=STATE_SOLAR)
    cmd, inp = run(reg, inputs(surplus_w=7200), 1)
    assert (cmd.amps, cmd.phases) == (7, 3)
    cmd, _ = run(reg, inp, 1)
    assert cmd.amps == 8


def test_respects_interval_between_steps():
    reg = Regulator(state=STATE_SOLAR)
    c1 = reg.step(inputs(surplus_w=7200))
    c2 = reg.step(inputs(now=T0 + timedelta(seconds=20), surplus_w=7200, cur_amps=c1.amps))
    assert c2.amps == c1.amps


def test_ramps_down_and_holds_in_band():
    reg = Regulator(state=STATE_SOLAR)
    cmd, _ = run(reg, inputs(surplus_w=4700, cur_amps=9), 1)
    assert cmd.amps == 8
    reg = Regulator(state=STATE_SOLAR)
    cmd, _ = run(reg, inputs(surplus_w=4950, cur_amps=8), 1)  # 5,00 − 100 W ≤ 4,95
    assert cmd.amps == 8


def test_switch_to_1f_after_hold_time():
    reg = Regulator(state=STATE_SOLAR)
    cmd, _ = run(reg, inputs(surplus_w=2100), 2)
    assert cmd.phases == 3  # zatím dotuje/čeká
    cmd, _ = run(reg, inputs(surplus_w=2100), 4)
    assert cmd.phases == 1 and cmd.enable
    assert cmd.amps == 10  # 1,98 kW ≤ 2,1


def test_no_1f_when_disabled_pauses_instead():
    reg = Regulator(state=STATE_SOLAR)
    reg.p.allow_1f = False
    cmd, _ = run(reg, inputs(surplus_w=2000, battery_soc=40), 1)
    assert not cmd.enable and cmd.state == STATE_PAUSED


def test_support_from_battery_is_limited():
    reg = Regulator(state=STATE_SOLAR)
    reg.p.allow_1f = False
    cmd, inp = run(reg, inputs(surplus_w=3000), 2)
    assert cmd.state == STATE_SUPPORT and cmd.enable
    # deficit 1,17 kW → 1 kWh za ~51 min, ale limit 10 min přijde dřív
    cmd, _ = run(reg, inp, 10, surplus_w=3000)
    assert cmd.state == STATE_PAUSED and not cmd.enable


def test_no_support_when_sun_not_returning_or_low_soc():
    for kw in (dict(sun_returns=False), dict(battery_soc=55)):
        reg = Regulator(state=STATE_SOLAR)
        reg.p.allow_1f = False
        cmd, _ = run(reg, inputs(surplus_w=3000, **kw), 2)
        assert cmd.state == STATE_PAUSED


def test_resume_after_stable_surplus():
    reg = Regulator(state=STATE_PAUSED)
    inp = inputs(surplus_w=5300, cur_enabled=False)
    cmd, inp = run(reg, inp, 4)
    assert not cmd.enable
    cmd, _ = run(reg, inp, 2)
    assert cmd.enable and cmd.phases == 3 and cmd.amps == 8


def test_resume_resets_when_surplus_drops():
    reg = Regulator(state=STATE_PAUSED)
    inp = inputs(surplus_w=5300, cur_enabled=False)
    _, inp = run(reg, inp, 4)
    _, inp = run(reg, inp, 1, surplus_w=500)
    cmd, _ = run(reg, inp, 3, surplus_w=5300)
    assert not cmd.enable


def test_boiler_caps_3f_current():
    reg = Regulator(state=STATE_SOLAR)
    cmd, _ = run(reg, inputs(surplus_w=9000, cur_amps=8, boiler_heating=True), 1)
    assert cmd.amps == 8


def test_breaker_cuts_two_amps_after_hold():
    reg = Regulator(state=STATE_PLAN)
    assert reg.breaker(inputs(worst_phase_a=24, cur_amps=11)) is None
    cmd = reg.breaker(inputs(now=T0 + timedelta(seconds=11), worst_phase_a=24, cur_amps=11))
    assert cmd.amps == 9 and cmd.enable
    reg2 = Regulator()
    reg2.breaker(inputs(worst_phase_a=24, cur_amps=7))
    cmd = reg2.breaker(inputs(now=T0 + timedelta(seconds=11), worst_phase_a=24, cur_amps=7))
    assert not cmd.enable


def test_manual_and_plan_override_solar():
    reg = Regulator()
    cmd = reg.step(inputs(manual="Nabíjet teď", manual_current=9, manual_phases="1f", surplus_w=0))
    assert (cmd.enable, cmd.amps, cmd.phases, cmd.state) == (True, 9, 1, STATE_MANUAL)
    cmd = reg.step(inputs(manual="Zastavit"))
    assert not cmd.enable
    cmd = reg.step(inputs(plan_slot="NT", surplus_w=0))
    assert (cmd.enable, cmd.amps, cmd.phases, cmd.state) == (True, 11, 3, STATE_PLAN)
    cmd = reg.step(inputs(target_reached=True))
    assert not cmd.enable and cmd.state == STATE_DONE


def test_back_from_plan_jumps_to_fitting_current():
    reg = Regulator(state=STATE_PLAN)
    cmd = reg.step(inputs(surplus_w=5200, cur_amps=11))
    assert cmd.state == STATE_SOLAR and cmd.amps == 8


# ----------------------------------------------------------------- plánovač


def ev_slots(start, hours, solar_kw_day=0.0):
    out = []
    for k in range(int(hours * 4)):
        ts = start + timedelta(minutes=15 * k)
        nt = ts.hour >= 22 or ts.hour < 6
        sun = solar_kw_day if 9 <= ts.hour < 16 else 0.0
        out.append(EvSlot(ts, nt, sun * 0.25))
    return out


def test_plan_solar_covers_everything():
    now = datetime(2026, 9, 25, 20, 0, tzinfo=TZ)
    plan = plan_ev(ev_slots(now, 28, 4.0), EvPlanParams(needed_kwh=10, mode="Solár+NT"), now)
    assert plan.nt_kwh == 0 and plan.solar_kwh == 10


def test_plan_nt_fills_rest_from_start_of_block():
    now = datetime(2026, 9, 25, 20, 0, tzinfo=TZ)
    slots = ev_slots(now, 28, 1.0)  # 7 h × 1 kW × 0,7 = 4,9 kWh slunce
    plan = plan_ev(slots, EvPlanParams(needed_kwh=20, mode="Solár+NT"), now)
    assert abs(plan.solar_kwh - 4.9) < 0.01
    assert abs(plan.nt_kwh - 15.1) < 0.01
    first = min(plan.grid_slots)
    assert first.hour == 22 and set(plan.grid_slots.values()) == {"NT"}


def test_plan_solar_only_mode_no_grid():
    now = datetime(2026, 9, 25, 20, 0, tzinfo=TZ)
    plan = plan_ev(ev_slots(now, 28, 1.0), EvPlanParams(needed_kwh=20, mode="Solár"), now)
    assert plan.grid_slots == {}


def test_plan_deadline_hard_adds_latest_vt():
    now = datetime(2026, 9, 25, 20, 0, tzinfo=TZ)
    deadline = datetime(2026, 9, 26, 8, 0, tzinfo=TZ)
    plan = plan_ev(ev_slots(now, 28), EvPlanParams(needed_kwh=60, mode="Solár+NT",
                                                  deadline=deadline, deadline_hard=True), now)
    assert plan.nt_kwh == 56.0  # 8 h × 7 kW
    vt = [t for t, k in plan.grid_slots.items() if k == "VT"]
    assert vt and max(vt) == datetime(2026, 9, 26, 7, 45, tzinfo=TZ)
    assert plan.shortfall_kwh == 0


def test_plan_deadline_soft_reports_shortfall():
    now = datetime(2026, 9, 25, 20, 0, tzinfo=TZ)
    deadline = datetime(2026, 9, 26, 8, 0, tzinfo=TZ)
    plan = plan_ev(ev_slots(now, 28), EvPlanParams(needed_kwh=60, mode="Solár+NT", deadline=deadline), now)
    assert plan.vt_kwh == 0 and abs(plan.shortfall_kwh - 4.0) < 0.01
    assert "CHYBÍ" in plan.reason


def test_plan_slot_now_lookup():
    now = datetime(2026, 9, 25, 22, 7, tzinfo=TZ)
    plan = plan_ev(ev_slots(now.replace(minute=0), 10), EvPlanParams(needed_kwh=5, mode="Solár+NT"), now)
    assert plan.slot_now(now) == "NT"


# ------------------------------------------------------- 3f z plné baterie


def test_full_battery_cheap_export_keeps_3f_with_support_without_limit():
    # 26. 9. 13:00: přebytek 3,5 kW, baterie 99 %, výkup 0,04 Kč → 3f 6 A, rozdíl z baterie
    reg = Regulator(state=STATE_SOLAR)
    inp = inputs(surplus_w=3500, battery_soc=99, sell_price=0.04, battery_refill=True)
    cmd, _ = run(reg, inp, 30)  # 30 min – déle než limit epizody 10 min
    assert (cmd.enable, cmd.amps, cmd.phases, cmd.state) == (True, 6, 3, STATE_SUPPORT)
    assert "plné baterie" in cmd.reason


def test_no_boost_when_export_is_valuable_switches_to_1f():
    reg = Regulator(state=STATE_SOLAR)
    inp = inputs(surplus_w=3500, battery_soc=99, sell_price=3.0, battery_refill=True)
    cmd, _ = run(reg, inp, 6)
    assert cmd.phases == 1 and cmd.enable


def test_no_boost_when_battery_would_not_refill():
    reg = Regulator(state=STATE_SOLAR)
    inp = inputs(surplus_w=3500, battery_soc=99, sell_price=0.04, battery_refill=False)
    cmd, _ = run(reg, inp, 6)
    assert cmd.phases == 1


def test_boost_deficit_too_large_falls_back():
    # přebytek 2,5 kW → rozdíl 1,67 kW > 1 kW: ne 3f z baterie
    reg = Regulator(state=STATE_SOLAR)
    inp = inputs(surplus_w=2500, battery_soc=99, sell_price=0.04, battery_refill=True)
    cmd, _ = run(reg, inp, 6)
    assert cmd.phases == 1


def test_boost_resume_starts_3f():
    reg = Regulator(state=STATE_PAUSED)
    inp = inputs(surplus_w=3400, cur_enabled=False, battery_soc=95, sell_price=0.1, battery_refill=True)
    cmd, _ = run(reg, inp, 6)
    assert cmd.enable and cmd.phases == 3 and cmd.amps == 6


# ------------------------------------------------ NT až v poslední noci před termínem


def test_plan_uses_latest_nt_block_before_deadline():
    now = datetime(2026, 9, 26, 14, 0, tzinfo=TZ)
    deadline = datetime(2026, 9, 28, 6, 0, tzinfo=TZ)  # dvě noci v horizontu
    plan = plan_ev(ev_slots(now, 40, 1.0), EvPlanParams(needed_kwh=20, mode="Solár+NT", deadline=deadline), now)
    nights = {t.date() if t.hour >= 22 else (t - timedelta(days=1)).date() for t in plan.grid_slots}
    assert nights == {datetime(2026, 9, 27).date()}
    assert min(plan.grid_slots).hour == 22


def test_plan_defers_nt_beyond_forecast_horizon():
    # sloty do neděle 24:00, termín úterý 06:00 → NT pondělní noci se naplánuje později
    now = datetime(2026, 9, 26, 14, 0, tzinfo=TZ)
    deadline = datetime(2026, 9, 29, 6, 0, tzinfo=TZ)
    plan = plan_ev(ev_slots(now, 34, 1.0), EvPlanParams(needed_kwh=33, mode="Solár+NT", deadline=deadline,
                                                        later_nt_kwh=56.0), now)
    assert plan.grid_slots == {}
    assert plan.nt_kwh > 0 and plan.shortfall_kwh == 0
    assert "později" in plan.reason
