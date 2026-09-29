"""Unit testy EV plánovače a regulátoru HAv2 (config/appdaemon/apps/hav2/hav2_ev.py)."""

import sys

import pytest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "config/appdaemon/apps/hav2"))

from hav2_ev import (  # noqa: E402
    BoilerGate,
    BoilerInputs,
    STATE_DONE,
    STATE_MANUAL,
    STATE_PAUSED,
    STATE_PLAN,
    STATE_SOLAR,
    STATE_SUPPORT,
    EvPlanParams,
    EvSlot,
    LimitInputs,
    limit_plan,
    limit_step,
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


# ------------------------------------------------------- prostor pro bojler

T_NOON = datetime(2026, 9, 28, 11, 0, tzinfo=TZ)


def binp(**kw):
    base = dict(now=T_NOON, eligible=True, surplus_w=3500, battery_soc=100, heating=False, grid_export_w=0)
    base.update(kw)
    return BoilerInputs(**base)


def brun(gate, inp, steps, dt=5, **changes):
    r = 0.0
    for _ in range(steps):
        inp = replace(inp, now=inp.now + timedelta(seconds=dt), **changes)
        r = gate.step(inp)
    return r, inp


def test_boiler_reserve_after_hold_and_off_below_threshold():
    g = BoilerGate()
    r, inp = brun(g, binp(), 30)  # 150 s < 180 s
    assert r == 0
    r, inp = brun(g, inp, 10)
    assert r == 2500
    r, inp = brun(g, inp, 1, surplus_w=2400)  # hystereze: nad off_w drží
    assert r == 2500
    r, inp = brun(g, inp, 1, surplus_w=2200)
    assert r == 0


def test_boiler_reserve_blocked_outside_window_low_soc_or_not_eligible():
    for kw in (dict(now=T_NOON.replace(hour=9)), dict(now=T_NOON.replace(hour=15, minute=40)),
               dict(battery_soc=95), dict(eligible=False)):
        g = BoilerGate()
        r, _ = brun(g, binp(**kw), 60)
        assert r == 0, kw


def test_boiler_heating_means_no_double_reserve():
    g = BoilerGate()
    r, inp = brun(g, binp(), 40)
    assert r == 2500
    r, inp = brun(g, inp, 1, heating=True)
    assert r == 0 and g.reason.startswith("bojler hřeje")


def test_boiler_done_when_energy_budget_delivered():
    # dávka = 60 % z průměru 4,25 kWh = 2,55 kWh ≈ 70 min při 2,2 kW a přetoku ≥ 2,3 kW
    g = BoilerGate()
    r, inp = brun(g, binp(boiler_avg_kwh=4.25, sell_price=1.5), 40)
    assert r == 2500
    r, inp = brun(g, inp, 60 * 12 * 1, grid_export_w=2500)  # 60 min → 2,2 kWh
    assert r == 2500 and not g.done
    r, inp = brun(g, inp, 12 * 12)  # +12 min → 2,64 kWh
    assert r == 0 and g.done
    r, inp = brun(g, inp, 60, grid_export_w=0)  # do konce dne už ne
    assert r == 0
    r, _ = brun(g, replace(inp, now=inp.now + timedelta(days=1)), 40)  # další den znovu
    assert r == 2500


def test_boiler_budget_counts_only_minutes_with_export_and_heating():
    g = BoilerGate()
    r, inp = brun(g, binp(boiler_avg_kwh=4.25, sell_price=1.5), 40)
    for _ in range(6):  # trouba / nabíjení baterie: přetok jen polovinu času → 60 min = 1,1 kWh
        r, inp = brun(g, inp, 60, grid_export_w=2500)
        r, inp = brun(g, inp, 60, grid_export_w=500)
    assert r == 2500 and not g.done
    # detektor hlásí ohřev: rezerva 0 (přebytek bojler už odečítá), energie se počítá dál
    r, inp = brun(g, inp, 12 * 45, heating=True, grid_export_w=2500)
    assert r == 0 and g.done


def test_boiler_budget_halved_at_low_or_negative_price():
    g = BoilerGate()
    assert g.budget_kwh(binp(boiler_avg_kwh=4.0, sell_price=1.5)) == pytest.approx(2.4)
    assert g.budget_kwh(binp(boiler_avg_kwh=4.0, sell_price=-0.2)) == pytest.approx(1.2)
    assert g.budget_kwh(binp(boiler_avg_kwh=None, sell_price=1.5)) == pytest.approx(2.5)


def test_boiler_disabled_switch():
    g = BoilerGate()
    g.p.enabled = False
    r, _ = brun(g, binp(), 60)
    assert r == 0


def test_no_battery_support_when_deficit_is_only_boiler_reserve():
    reg = Regulator()
    # přebytek po rezervě 500 W (bez rezervy 3 000 W), EV na minimu 1f
    inp = inputs(surplus_w=500, boiler_reserve_w=2500, battery_soc=98, cur_amps=6, cur_phases=1,
                 sell_price=0.5, battery_refill=True)
    cmd, _ = run(reg, inp, 1)
    assert not cmd.enable and cmd.state == STATE_PAUSED and "bojler" in cmd.reason
    # skutečný mrak (chybí víc než rezerva) → dotování jako dřív
    reg = Regulator()
    cmd, _ = run(reg, replace(inp, surplus_w=500, boiler_reserve_w=300), 1)
    assert cmd.state == STATE_SUPPORT


# ------------------------------------------------ vyúčtování EV v NT


def test_billing_rows_six_months_sum_v1_and_hav2():
    from hav2_ev import billing_rows, month_starts
    now = datetime(2026, 9, 28, 16, 0, tzinfo=TZ)
    assert month_starts(now, 6) == [(2026, 9), (2026, 8), (2026, 7), (2026, 6), (2026, 5), (2026, 4)]
    assert month_starts(datetime(2026, 2, 1, tzinfo=TZ), 3) == [(2026, 2), (2026, 1), (2025, 12)]
    changes = {"v1_kwh": {(2026, 8): 45.56, (2026, 9): 55.06}, "v1_kc": {(2026, 8): 159.9, (2026, 9): 193.26},
               "kwh": {(2026, 9): 15.5}, "kc": {(2026, 9): 54.4}}
    rows = billing_rows(changes, now)
    assert rows[0] == ("září 2026", 70.56, 247.66)
    assert rows[1] == ("srpen 2026", 45.56, 159.9)
    assert rows[5] == ("duben 2026", 0.0, 0.0)


# ------------------------------------------- jednorázový cíl nad limitem auta


def test_plan_high_part_only_in_window_before_deadline():
    # po 14:00, termín čt 12:00, okno 24 h → nad limit jen od st 12:00 (NT st/čt noc)
    now = datetime(2026, 9, 28, 14, 0, tzinfo=TZ)
    deadline = datetime(2026, 10, 1, 12, 0, tzinfo=TZ)
    high_from = deadline - timedelta(hours=24)
    slots = ev_slots(now, 82, 0.0)  # bez slunce, sloty až do čt 24:00
    plan = plan_ev(slots, EvPlanParams(needed_kwh=20, mode="Solár+NT", deadline=deadline,
                                       high_kwh=9, high_from=high_from), now)
    assert plan.shortfall_kwh == 0 and abs(plan.nt_kwh - 20) < 0.01
    # vše v poslední noci (st/čt), protože NT se plánuje v posledním bloku
    assert min(plan.grid_slots) >= high_from
    assert "nad limit auta 9.0" in plan.reason


def test_plan_high_part_not_in_earlier_night():
    # poslední NT blok nestačí na obojí → normální část do dřívější noci, nad limit nikdy před oknem
    now = datetime(2026, 9, 28, 14, 0, tzinfo=TZ)
    deadline = datetime(2026, 10, 1, 12, 0, tzinfo=TZ)
    high_from = deadline - timedelta(hours=24)
    slots = ev_slots(now, 82, 0.0)
    plan = plan_ev(slots, EvPlanParams(needed_kwh=70, mode="Solár+NT", deadline=deadline,
                                       high_kwh=10, high_from=high_from), now)
    early = [t for t in plan.grid_slots if t < high_from]
    late = [t for t in plan.grid_slots if t >= high_from]
    assert early and len(late) == 32  # poslední noc celá (8 h), zbytek dřív
    assert plan.shortfall_kwh == 0


def test_plan_high_part_solar_only_from_window():
    # slunce jen před oknem → nad limit ze slunce nic, jde do NT v okně
    now = datetime(2026, 9, 28, 8, 0, tzinfo=TZ)
    deadline = datetime(2026, 9, 29, 12, 0, tzinfo=TZ)
    high_from = datetime(2026, 9, 28, 20, 0, tzinfo=TZ)
    slots = ev_slots(now, 28, 4.0)  # 9–16 h slunce 4 kW (28. 9.) + 29. 9. 9–12 h
    plan = plan_ev(slots, EvPlanParams(needed_kwh=15, mode="Solár+NT", deadline=deadline,
                                       high_kwh=15, high_from=high_from), now)
    # v okně je slunce jen 29. 9. 9–12 h: 3 h × 4 kW × 0,7 = 8,4 kWh
    assert abs(plan.solar_kwh - 8.4) < 0.01
    assert abs(plan.nt_kwh - 6.6) < 0.01


def test_plan_without_high_unchanged():
    now = datetime(2026, 9, 25, 20, 0, tzinfo=TZ)
    a = plan_ev(ev_slots(now, 28, 1.0), EvPlanParams(needed_kwh=20, mode="Solár+NT"), now)
    b = plan_ev(ev_slots(now, 28, 1.0), EvPlanParams(needed_kwh=20, mode="Solár+NT", high_kwh=0,
                                                   high_from=now), now)
    assert a.grid_slots == b.grid_slots and a.nt_kwh == b.nt_kwh


def test_limit_step():
    assert limit_step(95) == 100 and limit_step(90) == 90 and limit_step(81) == 90 and limit_step(30) == 50


def lim(**kw):
    base = dict(now=datetime(2026, 9, 28, 10, 0, tzinfo=TZ), target_soc=80, std_limit=90, car_limit=90,
                deadline=None, window_h=24, connected=True)
    base.update(kw)
    return limit_plan(LimitInputs(**base))


def test_limit_normal_target_nothing_to_do():
    lp = lim()
    assert (lp.level, lp.want_limit, lp.top_now, lp.over) == ("OK", None, 80, False)


def test_limit_over_before_window_waits():
    dl = datetime(2026, 10, 2, 12, 0, tzinfo=TZ)
    lp = lim(target_soc=100, deadline=dl)
    assert lp.over and not lp.in_window and lp.want_limit is None
    assert lp.high_from == dl - timedelta(hours=24) and lp.normal_top == 90 and lp.top_now == 90
    assert lp.level == "Info" and "01.10. 12:00" in lp.message


def test_limit_over_in_window_raises_when_connected():
    dl = datetime(2026, 9, 29, 8, 0, tzinfo=TZ)
    assert lim(target_soc=100, deadline=dl).want_limit == 100
    assert lim(target_soc=95, deadline=dl).want_limit == 100
    assert lim(target_soc=100, deadline=dl, connected=False).want_limit is None
    assert lim(target_soc=100, deadline=dl, car_limit=100).want_limit is None  # už zvednuto


def test_limit_over_without_deadline_raises_now():
    assert lim(target_soc=100).want_limit == 100


def test_limit_restored_after_oneoff():
    # cíl zpět na standard, v autě zůstalo 100 → vrátit na 90
    assert lim(car_limit=100).want_limit == 90
    # jednorázový cíl ještě před oknem, limit zvednutý ručně dřív → vrátit (100 % až v okně)
    dl = datetime(2026, 10, 2, 12, 0, tzinfo=TZ)
    assert lim(target_soc=100, deadline=dl, car_limit=100).want_limit == 90


def test_limit_car_below_target_warns_only():
    lp = lim(target_soc=85, car_limit=80)
    assert lp.level == "Varování" and lp.want_limit is None and lp.top_now == 80 and lp.capped


def test_limit_unknown_car_limit_never_writes():
    assert lim(car_limit=None).want_limit is None
    assert lim(target_soc=100, car_limit=None).want_limit is None
