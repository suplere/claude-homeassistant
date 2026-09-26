"""Unit testy plánovače baterie HAv2 (config/appdaemon/apps/hav2/hav2_planner.py)."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "config/appdaemon/apps/hav2"))

from hav2_planner import (  # noqa: E402
    MODE_AUTO,
    MODE_CHARGE,
    MODE_DEFER,
    MODE_DISCHARGE,
    MODE_STANDBY,
    BatteryParams,
    Prices,
    build_slots,
    plan_battery,
    simulate,
)

TZ = timezone(timedelta(hours=2))


def nt(ts: datetime) -> bool:
    return ts.hour >= 22 or ts.hour < 6


def pv_curve(day: datetime, peak_kw: float):
    """Zjednodušená denní křivka FVE po 30 min (7–19 h, parabola)."""
    out = []
    t = day.replace(hour=0, minute=0)
    for i in range(48):
        ts = t + timedelta(minutes=30 * i)
        h = ts.hour + ts.minute / 60
        kw = max(0.0, peak_kw * (1 - ((h - 13) / 6) ** 2)) if 7 <= h <= 19 else 0.0
        out.append((ts, kw))
    return out


def make_slots(now, peak_today, peak_tomorrow, load_kw=0.45, boiler=None, spot=None):
    pv = pv_curve(now, peak_today) + pv_curve(now + timedelta(days=1), peak_tomorrow)
    spot = spot or {}
    hours = {}
    t = now.replace(minute=0)
    for i in range(60):
        h = t + timedelta(hours=i)
        hours[h] = spot.get(h.hour, 2.0)
    boiler = boiler or {}
    return build_slots(now, pv, lambda h: load_kw, lambda h: boiler.get(h.hour, 0.0), hours, nt)


BATT = BatteryParams(capacity_kwh=10.0, soc_pct=40.0, min_soc_pct=20.0)


def test_sunny_tomorrow_full_battery_no_grid_charge():
    now = datetime(2026, 9, 25, 21, 0, tzinfo=TZ)
    batt = BatteryParams(capacity_kwh=10.0, soc_pct=85.0, min_soc_pct=20.0)
    slots = make_slots(now, 0, 5.0)
    plan = plan_battery(slots, batt, Prices())
    assert plan.grid_charge_kwh == 0


def test_sunny_tomorrow_low_battery_charges_little():
    # ráno 6–9 h FVE nepokryje dům: vyplatí se malé NT nabití / držení, ne plná baterie
    now = datetime(2026, 9, 25, 21, 0, tzinfo=TZ)
    plan = plan_battery(make_slots(now, 0, 5.0), BATT, Prices())
    cloudy = plan_battery(make_slots(now, 0, 0.3), BATT, Prices())
    assert plan.grid_charge_kwh <= 1.5
    assert plan.grid_charge_kwh < cloudy.grid_charge_kwh


def test_holding_beats_discharging_in_nt_before_vt_deficit():
    # vybíjet v NT za 3,51 a ráno kupovat za 6,10 je horší než baterii podržet
    now = datetime(2026, 9, 25, 21, 0, tzinfo=TZ)
    plan = plan_battery(make_slots(now, 0, 5.0), BATT, Prices())
    first_nt = next(i for i, s in enumerate(make_slots(now, 0, 5.0)) if s.is_nt)
    assert plan.actions[first_nt].mode in (MODE_STANDBY, MODE_CHARGE)
    assert plan.candidates["0"] < plan.candidates["auto"]


def test_cloudy_tomorrow_charges_in_nt_at_end_of_block():
    now = datetime(2026, 9, 25, 21, 0, tzinfo=TZ)
    slots = make_slots(now, 0, 0.3)
    plan = plan_battery(slots, BATT, Prices())
    assert plan.grid_charge_kwh > 3
    charge_idx = [i for i, a in enumerate(plan.actions) if a.mode == MODE_CHARGE]
    assert charge_idx, "má se nabíjet"
    assert all(slots[i].is_nt for i in charge_idx)
    # nabíjí se na konci NT (těsně před 06:00), předtím baterie stojí
    assert slots[charge_idx[-1]].start.hour == 5
    first_nt = next(i for i, s in enumerate(slots) if s.is_nt)
    assert plan.actions[first_nt].mode == MODE_STANDBY


def test_grid_charge_not_worth_when_nt_too_expensive():
    now = datetime(2026, 9, 25, 21, 0, tzinfo=TZ)
    slots = make_slots(now, 0, 0.3)
    plan = plan_battery(slots, BATT, Prices(nt=5.5))  # 5.5/0.9 + 1 > 6.1
    assert plan.grid_charge_kwh == 0
    assert "nevyplatí" in plan.reason or plan.now.mode == MODE_AUTO


def test_partial_sun_charges_less_than_cloudy():
    now = datetime(2026, 9, 25, 21, 0, tzinfo=TZ)
    cloudy = plan_battery(make_slots(now, 0, 0.3), BATT, Prices())
    partial = plan_battery(make_slots(now, 0, 1.2), BATT, Prices())
    assert 0 <= partial.grid_charge_kwh < cloudy.grid_charge_kwh


def test_soc_never_below_min():
    now = datetime(2026, 9, 25, 12, 0, tzinfo=TZ)
    batt = BatteryParams(capacity_kwh=10.0, soc_pct=25.0, min_soc_pct=20.0)
    plan = plan_battery(make_slots(now, 0.2, 0.2, load_kw=1.5), batt, Prices())
    assert min(r.soc_pct for r in plan.results) >= 20.0 - 1e-6


def test_sell_at_spot_peak_when_sun_refills():
    now = datetime(2026, 9, 26, 6, 0, tzinfo=TZ)
    batt = BatteryParams(capacity_kwh=10.0, soc_pct=90.0, min_soc_pct=20.0)
    slots = make_slots(now, 6.0, 6.0, spot={7: 9.5})
    plan = plan_battery(slots, batt, Prices())
    sells = [s.start.hour for a, s in zip(plan.actions, slots) if a.mode == MODE_DISCHARGE]
    assert sells and all(h == 7 for h in sells)


def test_no_evening_sell_when_sun_cannot_refill_same_day():
    # 25.9. 18:00, špička v 19 h: baterii už dnes slunce nedobije → neprodávat
    now = datetime(2026, 9, 25, 18, 0, tzinfo=TZ)
    batt = BatteryParams(capacity_kwh=10.0, soc_pct=82.0, min_soc_pct=20.0)
    slots = make_slots(now, 5.0, 5.0, spot={19: 7.3})
    plan = plan_battery(slots, batt, Prices())
    assert all(a.mode != MODE_DISCHARGE for a in plan.actions)


def test_no_sell_below_threshold():
    now = datetime(2026, 9, 26, 6, 0, tzinfo=TZ)
    batt = BatteryParams(capacity_kwh=10.0, soc_pct=90.0, min_soc_pct=20.0)
    slots = make_slots(now, 6.0, 6.0, spot={7: 7.0})
    plan = plan_battery(slots, batt, Prices())
    assert all(a.mode != MODE_DISCHARGE for a in plan.actions)


def test_boiler_is_grid_only_and_does_not_drain_battery():
    # bojler je mimo měření GoodWe: baterie ho nekryje, zvyšuje jen nákup ze sítě
    now = datetime(2026, 9, 26, 12, 0, tzinfo=TZ)
    batt = BatteryParams(capacity_kwh=10.0, soc_pct=60.0, min_soc_pct=20.0)
    base = plan_battery(make_slots(now, 0.5, 0.5), batt, Prices())
    boil = plan_battery(make_slots(now, 0.5, 0.5, boiler={16: 2.2, 17: 1.3}), batt, Prices())
    i17 = next(i for i, r in enumerate(base.results) if r.start.hour == 18)
    assert boil.results[i17].soc_pct == pytest.approx(base.results[i17].soc_pct)
    assert boil.cost > base.cost + 3.5 * 6.0


def test_boiler_uses_pv_export_first():
    now = datetime(2026, 9, 26, 16, 0, tzinfo=TZ)
    batt = BatteryParams(capacity_kwh=10.0, soc_pct=100.0, min_soc_pct=20.0)
    slots = make_slots(now, 6.0, 6.0, load_kw=0.0, boiler={16: 2.0})
    from hav2_planner import Action

    res, _ = simulate(slots[:4], [Action()] * 4, batt, Prices())
    pv_h = sum(s.pv_kwh for s in slots[:4])
    assert sum(r.grid_export_kwh for r in res) == pytest.approx(max(pv_h - 2.0, 0), abs=1e-6)


def test_first_slot_is_partial_and_horizon_ends_tomorrow_midnight():
    now = datetime(2026, 9, 25, 21, 7, tzinfo=TZ)
    slots = make_slots(now, 0, 3.0)
    assert slots[0].start == datetime(2026, 9, 25, 21, 0, tzinfo=TZ)
    assert slots[0].fraction == pytest.approx(8 / 15)
    assert slots[-1].start == datetime(2026, 9, 26, 23, 45, tzinfo=TZ)


def test_simulate_standby_keeps_soc():
    now = datetime(2026, 9, 25, 23, 0, tzinfo=TZ)
    slots = make_slots(now, 0, 0)[:8]
    from hav2_planner import Action

    res, _ = simulate(slots, [Action(MODE_STANDBY)] * len(slots), BATT, Prices())
    assert res[-1].soc_pct == pytest.approx(BATT.soc_pct)
    assert res[0].grid_import_kwh == pytest.approx(slots[0].load_kwh)


def test_hourly_table_rows_are_text_and_aggregated():
    from hav2_planner import hourly_table

    now = datetime(2026, 9, 25, 21, 0, tzinfo=TZ)
    slots = make_slots(now, 0, 0.3)
    plan = plan_battery(slots, BATT, Prices())
    table = hourly_table(plan, slots, Prices())
    assert len(table) == 24
    assert table[0]["t"] == "21:00" and table[3]["t"] == "26.09. 00:00"
    assert all(isinstance(v, str) and v for row in table for v in row.values())
    first_import = sum(r.grid_import_kwh for r in plan.results[:4])
    assert table[0]["nakup"] == f"{first_import:.2f}"
    assert any(row["rezim"] == "nabíjet ze sítě" for row in table)


def test_missing_tomorrow_spot_uses_same_hour_yesterday():
    # před vydáním zítřejších cen: zítřejší poledne = dnešní poledne, ne dnešní večer
    now = datetime(2026, 9, 26, 12, 0, tzinfo=TZ)
    today = {now.replace(hour=h): (0.05 if 10 <= h <= 15 else 4.5) for h in range(24)}
    pv = pv_curve(now, 5.0) + pv_curve(now + timedelta(days=1), 5.0)
    slots = build_slots(now, pv, lambda h: 0.4, lambda h: 0.0, today, nt)
    noon_tomorrow = next(s for s in slots if s.start == datetime(2026, 9, 27, 12, 0, tzinfo=TZ))
    evening_tomorrow = next(s for s in slots if s.start == datetime(2026, 9, 27, 20, 0, tzinfo=TZ))
    assert noon_tomorrow.spot == 0.05
    assert evening_tomorrow.spot == 4.5


NEG_NOON = {**{h: 2.0 for h in range(24)}, **{h: -0.5 for h in range(11, 16)}}


def test_negative_noon_defers_morning_charging_and_still_fills():
    now = datetime(2026, 9, 27, 6, 30, tzinfo=TZ)
    batt = BatteryParams(capacity_kwh=10.0, soc_pct=30.0, min_soc_pct=20.0)
    slots = make_slots(now, 6.0, 6.0, spot=NEG_NOON)
    plan = plan_battery(slots, batt, Prices())
    today = [(r, a) for r, a in zip(plan.results, plan.actions) if r.start.date() == now.date()]
    deferred = [r.start.hour for r, a in today if a.mode == MODE_DEFER]
    assert deferred and max(deferred) < 11
    assert max(r.soc_pct for r, _ in today if r.start.hour < 17) >= 95
    base = plan_battery(slots, batt, Prices(), allow_defer=False)
    assert plan.cost < base.cost
    assert "odložit nabíjení" in {row["rezim"] for row in __import__("hav2_planner").hourly_table(plan, slots, Prices())}


def test_no_defer_without_negative_price_or_when_disabled():
    now = datetime(2026, 9, 27, 6, 30, tzinfo=TZ)
    batt = BatteryParams(capacity_kwh=10.0, soc_pct=30.0, min_soc_pct=20.0)
    plan = plan_battery(make_slots(now, 6.0, 6.0), batt, Prices())
    assert plan.defer_slots == 0
    plan = plan_battery(make_slots(now, 6.0, 6.0, spot=NEG_NOON), batt, Prices(), allow_defer=False)
    assert plan.defer_slots == 0


def test_no_defer_when_weak_sun_could_not_fill_battery():
    now = datetime(2026, 9, 27, 6, 30, tzinfo=TZ)
    batt = BatteryParams(capacity_kwh=10.0, soc_pct=20.0, min_soc_pct=20.0)
    plan = plan_battery(make_slots(now, 1.8, 1.8, spot=NEG_NOON), batt, Prices())
    assert plan.defer_slots == 0


def test_simulate_defer_exports_surplus_but_covers_deficit():
    now = datetime(2026, 9, 27, 10, 0, tzinfo=TZ)
    slots = make_slots(now, 6.0, 0)[:2]
    batt = BatteryParams(capacity_kwh=10.0, soc_pct=50.0, min_soc_pct=20.0)
    res, _ = simulate(slots, [__import__("hav2_planner").Action(MODE_DEFER)] * 2, batt, Prices())
    assert res[-1].soc_pct == 50.0 and res[0].grid_export_kwh > 0
    night = make_slots(datetime(2026, 9, 27, 23, 0, tzinfo=TZ), 0, 0)[:2]
    res, _ = simulate(night, [__import__("hav2_planner").Action(MODE_DEFER)] * 2, batt, Prices())
    assert res[-1].soc_pct < 50.0 and res[0].grid_import_kwh == 0
