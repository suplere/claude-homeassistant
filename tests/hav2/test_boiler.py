"""Unit testy vyhodnocení bojleru z PND (config/appdaemon/apps/hav2/hav2_boiler.py)."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "config/appdaemon/apps/hav2"))

from hav2_boiler import boiler_days  # noqa: E402

TZ = timezone(timedelta(hours=2))
D0 = datetime(2026, 9, 25, 0, 0, tzinfo=TZ)


def nt(ts):
    return ts.hour >= 22 or ts.hour < 6


def day(pnd_imp_by_hour, ha_imp_by_hour=None, pnd_exp=None, ha_exp=None):
    hours = [D0 + timedelta(hours=h) for h in range(24)]
    pi = {t: pnd_imp_by_hour.get(t.hour, 0.05) for t in hours}
    hi = {t: (ha_imp_by_hour or {}).get(t.hour, 0.05) for t in hours}
    pe = {t: (pnd_exp or {}).get(t.hour, 0.0) for t in hours}
    he = {t: (ha_exp or {}).get(t.hour, 0.0) for t in hours}
    return boiler_days(pi, pe, hi, he, nt, 3.51, 6.10)


def test_forced_heating_detected_and_priced_in_vt():
    # 25. 9.: 16–19 h bojler ~2,2 kWh/h, GoodWe nevidí
    (d,) = day({16: 1.97, 17: 2.32, 18: 2.21}, {16: 0.0, 17: 0.1, 18: 0.1})
    assert [h for h, _ in d.hours] == [16, 17, 18]
    assert d.boiler_kwh == pytest.approx(1.97 + 2.22 + 2.11)
    assert d.cost_kc == pytest.approx(d.import_kwh * 6.10)
    assert d.residual_import_kwh == pytest.approx(0.0, abs=1e-9)
    assert d.residual_pct("import") == 0.0


def test_noise_below_threshold_is_residual_not_boiler():
    (d,) = day({10: 0.2}, {10: 0.0})
    assert d.boiler_kwh == 0
    assert d.residual_import_kwh == pytest.approx(0.2)


def test_boiler_from_surplus_counts_export_loss():
    # poledne: GoodWe vidí prodej 1,5 kWh, PND jen 0,3 → 1,2 kWh šlo do bojleru
    (d,) = day({}, {}, pnd_exp={12: 0.3}, ha_exp={12: 1.5})
    assert d.export_loss_kwh == pytest.approx(1.2)
    assert d.cost_kc == 0
    assert d.residual_export_kwh == pytest.approx(0.0)


def test_missing_hours_skipped_and_nt_price():
    hours = [D0 + timedelta(hours=h) for h in (3, 4)]
    pi = {hours[0]: 1.0, hours[1]: 1.0}
    hi = {hours[0]: 0.0}  # hodina 4 chybí v HA
    (d,) = boiler_days(pi, {h: 0.0 for h in hours}, hi, {h: 0.0 for h in hours}, nt, 3.51, 6.10)
    assert d.hours == [(3, 1.0)]
    assert d.cost_kc == pytest.approx(3.51)
