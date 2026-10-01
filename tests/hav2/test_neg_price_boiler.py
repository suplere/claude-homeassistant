"""Unit testy limitu přetoku při záporném výkupu podle příkonu bojleru (hav2_boiler.NegPriceBoiler)."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "config/appdaemon/apps/hav2"))

from hav2_boiler import NEG_MARGIN_W, NegPriceBoiler  # noqa: E402

TZ = timezone(timedelta(hours=2))
T0 = datetime(2026, 10, 5, 12, 0, tzinfo=TZ)


def m(minutes):
    return T0 + timedelta(minutes=minutes)


def test_offers_margin_when_boiler_idle():
    b = NegPriceBoiler()
    limit, _ = b.limit(T0, 0, 0)
    assert limit == NEG_MARGIN_W


def test_follows_boiler_power_rounded_up():
    b = NegPriceBoiler()
    limit, _ = b.limit(T0, 1234, 600)
    assert limit == 1900  # 1234 + 600 → nahoru na 100 W


def test_small_change_keeps_current_limit():
    b = NegPriceBoiler()
    limit, _ = b.limit(T0, 1250, 1900)
    assert limit == 1900


def test_capped():
    b = NegPriceBoiler()
    limit, _ = b.limit(T0, 2900, 2000)
    assert limit == 3000


def test_full_after_5_min_without_draw_then_retry_after_hour():
    b = NegPriceBoiler()
    assert b.limit(m(0), 0, 0)[0] == NEG_MARGIN_W
    assert b.limit(m(4), 0, 600)[0] == NEG_MARGIN_W
    limit, text = b.limit(m(5), 0, 600)
    assert limit == 0 and "termostat" in text
    assert b.limit(m(30), 0, 0)[0] == 0  # nahřátý – nezkouší
    assert b.limit(m(66), 0, 0)[0] == NEG_MARGIN_W  # po hodině znovu nabídne


def test_draw_resets_probing():
    b = NegPriceBoiler()
    b.limit(m(0), 0, 0)
    b.limit(m(4), 500, 600)  # začal brát
    assert b.limit(m(8), 0, 1100)[0] == NEG_MARGIN_W  # nový odpočet od 8. min
    assert b.limit(m(12), 0, 600)[0] == NEG_MARGIN_W
    assert b.limit(m(13), 0, 600)[0] == 0
