"""HAv2 – filtrace bazénu: řízení čerpadla (čistý Python, bez AppDaemonu).

Návrh: docs/hav2-architektura.md §5.5.
- bazénový den 06:00–06:00, cíl hodin = požadované nebo doporučené (teplota / ORP)
- přednostně z přetoků FVE: start při přebytku ≥ 600 W po 5 min, min. běh 60 min,
  stop při přebytku < 200 W po 10 min, max. 4 solární starty za den
- zbytek hodin v NT od 22:00 v jednom bloku (do splnění nebo do 06:00)
Přebytek (sensor.energy_surplus_smoothed_w) nezahrnuje čerpadlo ani EV, takže se
při běhu čerpadla nemění. Filtrace má přednost před baterií domu i EV bez termínu:
kWh ze slunce ušetří NT (3,51), baterie jen VT − hodnota baterie (~1,2 Kč).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

STATE_OFF_SEASON = "Mimo sezónu"
STATE_DONE = "Splněno"
STATE_SOLAR = "Solár"
STATE_NT = "NT doplnění"
STATE_WAIT = "Čeká"
STATE_MIN_RUN = "Minimální běh"
STATE_MANUAL = "Ručně"


def pool_day_start(now: datetime) -> datetime:
    start = now.replace(hour=6, minute=0, second=0, microsecond=0)
    return start if now >= start else start - timedelta(days=1)


def target_hours(required: float, recommended: Optional[float], use_recommendation: bool) -> float:
    if use_recommendation and recommended is not None:
        return recommended
    return required


@dataclass
class PoolParams:
    start_surplus_w: float = 600.0
    start_hold_min: float = 5.0
    stop_surplus_w: float = 200.0
    stop_hold_min: float = 10.0
    min_run_min: float = 60.0
    max_solar_starts: int = 4


@dataclass
class PoolInputs:
    now: datetime
    season: bool
    manual: str  # Auto / Zapnout / Vypnout
    hours_done: float
    target_h: float
    surplus_w: float
    is_nt: bool
    running: bool


@dataclass
class PoolCommand:
    on: bool
    state: str
    reason: str


@dataclass
class PoolController:
    p: PoolParams = None  # type: ignore[assignment]
    state: str = STATE_WAIT
    on_since: Optional[datetime] = None
    above_since: Optional[datetime] = None
    below_since: Optional[datetime] = None
    solar_starts: int = 0
    day: Optional[datetime] = None
    last: Optional[PoolCommand] = None

    def __post_init__(self) -> None:
        if self.p is None:
            self.p = PoolParams()

    def _cmd(self, i: PoolInputs, on: bool, state: str, reason: str) -> PoolCommand:
        if on and not i.running:
            self.on_since = i.now
            if state == STATE_SOLAR:
                self.solar_starts += 1
        if not on:
            self.on_since = None
        self.state = state
        self.last = PoolCommand(on, state, reason)
        return self.last

    def step(self, i: PoolInputs) -> PoolCommand:
        p = self.p
        day = pool_day_start(i.now)
        if self.day != day:
            self.day, self.solar_starts = day, 0
        if i.running and self.on_since is None:
            self.on_since = i.now  # běží od dřív (restart, ruční zapnutí)

        if i.manual == "Zapnout":
            return self._cmd(i, True, STATE_MANUAL, "ručně zapnuto")
        if i.manual == "Vypnout":
            return self._cmd(i, False, STATE_MANUAL, "ručně vypnuto")
        if not i.season:
            return self._cmd(i, False, STATE_OFF_SEASON, "sezóna bazénu vypnutá")

        missing = max(0.0, i.target_h - i.hours_done)
        ran_min = (i.now - self.on_since).total_seconds() / 60 if (i.running and self.on_since) else 0.0
        done_txt = f"{i.hours_done:.1f}/{i.target_h:.1f} h"

        if missing <= 0.01:
            if i.running and ran_min < p.min_run_min and not i.is_nt:
                return self._cmd(i, True, STATE_MIN_RUN, f"splněno {done_txt}, dobíhá min. běh")
            return self._cmd(i, False, STATE_DONE, f"splněno {done_txt}")

        # NT: doplnit chybějící hodiny v jednom bloku
        if i.is_nt:
            self.above_since = self.below_since = None
            return self._cmd(i, True, STATE_NT, f"NT doplnění, chybí {missing:.1f} h ({done_txt})")

        # den: jen z přetoků
        if i.surplus_w >= p.start_surplus_w:
            self.above_since = self.above_since or i.now
        else:
            self.above_since = None
        if i.surplus_w < p.stop_surplus_w:
            self.below_since = self.below_since or i.now
        else:
            self.below_since = None

        if i.running:
            if ran_min < p.min_run_min:
                return self._cmd(i, True, STATE_MIN_RUN if i.surplus_w < p.stop_surplus_w else STATE_SOLAR,
                                 f"běží {ran_min:.0f}/{p.min_run_min:.0f} min, přebytek {i.surplus_w:.0f} W")
            if self.below_since and (i.now - self.below_since).total_seconds() / 60 >= p.stop_hold_min:
                return self._cmd(i, False, STATE_WAIT,
                                 f"přebytek {i.surplus_w:.0f} W < {p.stop_surplus_w:.0f} W {p.stop_hold_min:.0f} min → stop ({done_txt})")
            return self._cmd(i, True, STATE_SOLAR, f"ze slunce, přebytek {i.surplus_w:.0f} W ({done_txt})")

        if self.solar_starts >= p.max_solar_starts:
            return self._cmd(i, False, STATE_WAIT, f"limit {p.max_solar_starts} startů, zbytek v NT ({done_txt})")
        if self.above_since:
            held = (i.now - self.above_since).total_seconds() / 60
            if held >= p.start_hold_min:
                self.above_since = None
                return self._cmd(i, True, STATE_SOLAR,
                                 f"přebytek {i.surplus_w:.0f} W {held:.0f} min → start ({done_txt})")
            return self._cmd(i, False, STATE_WAIT,
                             f"přebytek {i.surplus_w:.0f} W, start za {p.start_hold_min - held:.0f} min ({done_txt})")
        return self._cmd(i, False, STATE_WAIT,
                         f"čeká na přebytek ≥ {p.start_surplus_w:.0f} W (teď {i.surplus_w:.0f} W), chybí {missing:.1f} h")


def expected_nt_hours(target_h: float, hours_done: float, solar_hours_left: float) -> float:
    """Kolik hodin zbyde na NT po očekávaném běhu ze slunce (pro plán/dashboard)."""
    return round(max(0.0, target_h - hours_done - solar_hours_left), 2)
