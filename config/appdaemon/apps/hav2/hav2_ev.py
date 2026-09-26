"""HAv2 – EV: plánovač energie a regulátor proudu (čistý Python, bez AppDaemonu).

Návrh: docs/hav2-architektura.md §5.3 (plánovač), §5.4 (regulační smyčka), §7 (jistič).
- plan_ev(): kolik energie nabít ze slunce / v NT / ve VT do termínu a ve kterých slotech
- Regulator.step(): jeden krok regulace (±1 A, 1f↔3f s hysterezí, dotování z baterie,
  pozastavení a obnovení) + ochrana hlavního jističe
Regulátor jen rozhoduje, co by se mělo nastavit (Command). Zápis dělá AppDaemon přes
script.hav2_ev_set, a to jen v režimu Auto se zapnutým řízením EV.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

# ------------------------------------------------------------ výkon ↔ proud
# změřeno 2026-09-25 (EcoVolter + Kia EV6); auto bere pod setpointem nelineárně
POWER_1F = {6: 1.27, 7: 1.40, 8: 1.55, 9: 1.77, 10: 1.98, 11: 2.15}
POWER_3F = {6: 4.17, 7: 4.40, 8: 5.00, 9: 5.65, 10: 6.30, 11: 7.00}

STATE_IDLE = "Nečinné"
STATE_SOLAR = "Solár"
STATE_SUPPORT = "Dotuje"
STATE_PAUSED = "Pozastaveno"
STATE_PLAN = "Plán"
STATE_FAST = "Rychle"
STATE_MANUAL = "Ručně"
STATE_OFF = "Vypnuto"
STATE_DONE = "Nabito"


def ev_power_kw(phases: int, amps: int) -> float:
    table = POWER_3F if phases == 3 else POWER_1F
    amps = int(amps)
    if amps in table:
        return table[amps]
    # mimo tabulku lineárně (230 V × fáze × účiník ~0,92)
    return round(amps * 0.23 * phases * 0.92, 2)


def max_amps_for(phases: int, kw: float, amin: int, amax: int) -> Optional[int]:
    """Nejvyšší proud, jehož výkon se vejde do kw; None, když ani amin."""
    best = None
    for a in range(amin, amax + 1):
        if ev_power_kw(phases, a) <= kw + 1e-9:
            best = a
    return best


# ------------------------------------------------------------------ plánovač


@dataclass
class EvSlot:
    start: datetime
    is_nt: bool
    solar_kwh: float  # očekávaný přebytek pro EV v tomto slotu (po domě a baterii)
    fraction: float = 1.0


@dataclass
class EvPlanParams:
    needed_kwh: float
    mode: str  # Solár / Solár+NT / Rychle / Vypnuto
    deadline: Optional[datetime] = None
    deadline_hard: bool = False
    grid_kw: float = 7.0  # plánovaný výkon ze sítě (3f 11 A)
    solar_confidence: float = 0.7  # jak velkou část předpovězeného přebytku počítat
    nt_price: float = 3.51
    vt_price: float = 6.10


@dataclass
class EvPlan:
    needed_kwh: float
    solar_kwh: float
    nt_kwh: float
    vt_kwh: float
    shortfall_kwh: float
    grid_slots: Dict[datetime, str]  # začátek slotu → "NT"/"VT"
    horizon_end: Optional[datetime]
    reason: str
    est_cost: float = 0.0

    def slot_now(self, now: datetime) -> Optional[str]:
        start = now.replace(minute=now.minute - now.minute % 15, second=0, microsecond=0)
        return self.grid_slots.get(start)


def plan_ev(slots: Sequence[EvSlot], p: EvPlanParams, now: datetime) -> EvPlan:
    """Rozdělí potřebnou energii: slunce → NT → (VT jen při „za každou cenu“).

    Bez termínu je horizont do zítřka 18:00: v NT se nabije jen to, co do té doby
    nepokryje předpovězený přebytek (odjezdy jsou nepravidelné, termín zadává uživatel).
    NT se plánuje od začátku bloku (baterie domu se nabíjí na jeho konci), VT co nejpozději.
    """
    need = max(0.0, p.needed_kwh)
    empty = EvPlan(need, 0.0, 0.0, 0.0, 0.0, {}, None, "")
    if p.mode == "Vypnuto":
        empty.reason = "režim Vypnuto"
        return empty
    if need <= 0.05:
        empty.reason = "cílový SOC dosažen"
        return empty
    if p.mode == "Rychle":
        empty.reason = "režim Rychle: hned, max. proud"
        return empty

    if p.deadline and p.deadline > now:
        horizon = p.deadline
    else:
        horizon = (now + timedelta(days=1)).replace(hour=18, minute=0, second=0, microsecond=0)
    window = [s for s in slots if s.start < horizon]

    solar = min(need, sum(s.solar_kwh for s in window) * p.solar_confidence)
    rest = need - solar
    grid: Dict[datetime, str] = {}
    nt_kwh = vt_kwh = 0.0
    if p.mode == "Solár+NT" and rest > 0.05:
        for s in window:
            if rest <= 0.05:
                break
            if s.is_nt:
                e = min(rest, p.grid_kw * 0.25 * s.fraction)
                grid[s.start] = "NT"
                nt_kwh += e
                rest -= e
    if rest > 0.05 and p.deadline_hard and p.deadline and p.deadline > now:
        for s in reversed(window):
            if rest <= 0.05:
                break
            if not s.is_nt and s.start not in grid:
                e = min(rest, p.grid_kw * 0.25 * s.fraction)
                grid[s.start] = "VT"
                vt_kwh += e
                rest -= e
    shortfall = max(0.0, rest) if (p.deadline and p.deadline > now) else 0.0

    parts = [f"potřeba {need:.1f} kWh", f"slunce ~{solar:.1f}"]
    if nt_kwh:
        parts.append(f"NT {nt_kwh:.1f}")
    if vt_kwh:
        parts.append(f"VT {vt_kwh:.1f}")
    if p.deadline and p.deadline > now:
        parts.append(f"do {p.deadline:%d.%m. %H:%M}")
        if shortfall > 0.05:
            parts.append(f"CHYBÍ {shortfall:.1f} kWh")
    elif p.mode == "Solár+NT":
        parts.append("bez termínu (NT jen na to, co nepokryje slunce do zítřka 18:00)")
    else:
        parts.append("jen slunce")
    cost = nt_kwh * p.nt_price + vt_kwh * p.vt_price
    return EvPlan(need, round(solar, 2), round(nt_kwh, 2), round(vt_kwh, 2), round(shortfall, 2),
                  dict(sorted(grid.items())), horizon, ", ".join(parts), round(cost, 2))


# ----------------------------------------------------------------- regulátor


@dataclass
class RegParams:
    amin: int = 6
    amax: int = 11
    interval_s: int = 60
    allow_1f: bool = True
    support_min_soc: float = 60.0
    support_max_min: float = 10.0
    support_max_kwh: float = 1.0
    resume_after_min: float = 5.0
    up_margin_w: float = 150.0
    down_margin_w: float = 100.0
    to3f_w: float = 4600.0
    to1f_w: float = 3900.0
    phase_hold_s: float = 180.0
    phase_switch_min_s: float = 600.0
    breaker_limit_a: float = 23.0
    breaker_hold_s: float = 10.0
    boiler_3f_max_a: int = 8
    # 3f z plné baterie: když je baterie skoro plná, výkup skoro nulový a slunce ji do večera
    # dobije, je lepší nabíjet 3f na minimum a malý rozdíl krýt z baterie, než posílat
    # přebytek do sítě (kWh pro EV by se jinak v noci koupila za NT)
    boost_soc: float = 90.0
    boost_max_sell: float = 1.0  # Kč/kWh (spot × koeficient)
    boost_max_deficit_w: float = 1000.0


@dataclass
class RegInputs:
    now: datetime
    connected: bool
    available: bool  # EcoVolter dosažitelný
    mode: str  # ev_mode
    manual: str  # ev_manual: Auto / Nabíjet teď / Zastavit
    manual_current: int
    manual_phases: str  # Auto / 1f / 3f
    target_reached: bool
    surplus_w: float  # vyhlazený přebytek pro EV (už bez filtrace a rezervy baterie)
    battery_soc: float
    sun_returns: bool  # Solcast: přebytek se do 30–60 min vrátí
    plan_slot: Optional[str]  # "NT"/"VT" když je teď plánovaný síťový slot
    worst_phase_a: float  # nejvíc zatížená fáze ze sítě vč. bojleru
    boiler_heating: bool
    cur_enabled: bool
    cur_amps: int
    cur_phases: int
    sell_price: float = 99.0  # výkupní cena teď (Kč/kWh); výchozí = „drahý“ → bez 3f z baterie
    battery_refill: bool = False  # předpověď FVE do večera baterii dobije


@dataclass
class Command:
    enable: bool
    amps: int
    phases: int
    state: str
    reason: str


@dataclass
class Regulator:
    p: RegParams = field(default_factory=RegParams)
    state: str = STATE_IDLE
    last_step: Optional[datetime] = None
    last_phase_switch: Optional[datetime] = None
    phase_cond_since: Optional[datetime] = None
    phase_cond_dir: int = 0
    resume_since: Optional[datetime] = None
    support_since: Optional[datetime] = None
    support_kwh: float = 0.0
    breaker_since: Optional[datetime] = None
    last_cmd: Optional[Command] = None

    # ----------------------------------------------------------- pomocné
    def _cap(self, i: RegInputs, phases: int, amps: int) -> int:
        """Strop proudu: rozsah + bojler na L3 (EV 3f + 9,8 A bojleru pod jističem)."""
        amps = max(self.p.amin, min(self.p.amax, amps))
        if phases == 3 and i.boiler_heating:
            amps = min(amps, self.p.boiler_3f_max_a)
        return amps

    def _cmd(self, enable: bool, amps: int, phases: int, state: str, reason: str) -> Command:
        self.state = state
        self.last_cmd = Command(enable, int(amps), int(phases), state, reason)
        return self.last_cmd

    def _hold(self, i: RegInputs, state: str, reason: str) -> Command:
        return self._cmd(i.cur_enabled, i.cur_amps, i.cur_phases, state, reason)

    def _reset_episode(self) -> None:
        self.support_since = None
        self.support_kwh = 0.0

    # ------------------------------------------------------ ochrana jističe
    def breaker(self, i: RegInputs) -> Optional[Command]:
        """Volat často (5 s). Fáze > limit po dobu hold → −2 A, na minimu stop."""
        if not (i.cur_enabled and i.connected) or i.worst_phase_a <= self.p.breaker_limit_a:
            self.breaker_since = None
            return None
        if self.breaker_since is None:
            self.breaker_since = i.now
            return None
        if (i.now - self.breaker_since).total_seconds() < self.p.breaker_hold_s:
            return None
        self.breaker_since = i.now
        if i.cur_amps - 2 >= self.p.amin:
            return self._cmd(True, i.cur_amps - 2, i.cur_phases, self.state,
                             f"jistič: fáze {i.worst_phase_a:.1f} A > {self.p.breaker_limit_a:.0f} A, −2 A")
        return self._cmd(False, self.p.amin, i.cur_phases, STATE_PAUSED,
                         f"jistič: fáze {i.worst_phase_a:.1f} A i při minimu, stop")

    # --------------------------------------------------------------- krok
    def step(self, i: RegInputs) -> Command:
        p = self.p
        if not i.available:
            return self._hold(i, self.state, "EcoVolter nedostupný – beze změny")
        if not i.connected:
            self._reset_episode()
            self.resume_since = None
            return self._cmd(False, p.amin, i.cur_phases, STATE_IDLE, "auto nepřipojeno")

        # ruční ovládání má přednost
        if i.manual == "Zastavit":
            return self._cmd(False, i.cur_amps, i.cur_phases, STATE_MANUAL, "ručně zastaveno")
        if i.manual == "Nabíjet teď":
            ph = 1 if i.manual_phases == "1f" else 3
            return self._cmd(True, self._cap(i, ph, i.manual_current), ph, STATE_MANUAL,
                             f"ručně nabíjet {i.manual_current} A {ph}f")

        if i.mode == "Vypnuto":
            return self._cmd(False, i.cur_amps, i.cur_phases, STATE_OFF, "režim Vypnuto")
        if i.target_reached:
            return self._cmd(False, i.cur_amps, i.cur_phases, STATE_DONE, "cílový SOC dosažen")
        if i.mode == "Rychle":
            return self._cmd(True, self._cap(i, 3, p.amax), 3, STATE_FAST, "režim Rychle")
        if i.plan_slot:
            self._reset_episode()
            return self._cmd(True, self._cap(i, 3, p.amax), 3, STATE_PLAN,
                             f"plánovaný slot {i.plan_slot} (termín / NT)")

        # solární regulace – krok jen jednou za interval
        in_loop = self.state in (STATE_SOLAR, STATE_SUPPORT, STATE_PAUSED)
        if in_loop and self.last_step and (i.now - self.last_step).total_seconds() < p.interval_s - 1:
            return self.last_cmd or self._hold(i, self.state, "čeká na další krok")
        dt_s = (i.now - self.last_step).total_seconds() if self.last_step else 0.0
        self.last_step = i.now
        return self._solar(i, dt_s)

    def _solar(self, i: RegInputs, dt_s: float) -> Command:
        p = self.p
        avail_kw = i.surplus_w / 1000
        min_1f = ev_power_kw(1, p.amin)
        min_3f = ev_power_kw(3, p.amin)
        boost = (i.battery_soc >= p.boost_soc and i.sell_price < p.boost_max_sell and i.battery_refill)
        # s „3f z baterie“ stačí na 3f minimum o boost_max_deficit méně
        min_3f_eff = min_3f - (p.boost_max_deficit_w / 1000 if boost else 0.0)
        lowest = min(min_1f if p.allow_1f else min_3f, min_3f_eff)

        # --- nabíjení neběží: čekáme na stabilní přebytek
        if not i.cur_enabled:
            if avail_kw >= lowest:
                self.resume_since = self.resume_since or i.now
                waited = (i.now - self.resume_since).total_seconds() / 60
                if waited >= p.resume_after_min:
                    ph = 3 if (avail_kw >= min_3f_eff or not p.allow_1f) else 1
                    amps = max_amps_for(ph, avail_kw, p.amin, p.amax) or p.amin
                    self.resume_since = None
                    self._reset_episode()
                    return self._cmd(True, self._cap(i, ph, amps), ph, STATE_SOLAR,
                                     f"přebytek {i.surplus_w:.0f} W stabilní {waited:.0f} min → start {amps} A {ph}f")
                return self._cmd(False, i.cur_amps, i.cur_phases, STATE_PAUSED,
                                 f"přebytek {i.surplus_w:.0f} W, čekám {p.resume_after_min - waited:.0f} min")
            self.resume_since = None
            return self._cmd(False, i.cur_amps, i.cur_phases, STATE_PAUSED,
                             f"přebytek {i.surplus_w:.0f} W < {lowest * 1000:.0f} W")

        ph, amps = i.cur_phases, i.cur_amps
        if self.state not in (STATE_SOLAR, STATE_SUPPORT):
            # návrat z plánu / Rychle / ručního režimu: rovnou na proud podle přebytku
            ph = 3 if (avail_kw >= min_3f_eff or not p.allow_1f) else ph
            fit = max_amps_for(ph, avail_kw, p.amin, p.amax) or (p.amin if ph == 3 and boost else None)
            if fit is not None:
                self._reset_episode()
                return self._cmd(True, self._cap(i, ph, fit), ph, STATE_SOLAR,
                                 f"přechod na solární regulaci: {fit} A {ph}f")
            amps = p.amin

        # --- volba fází s hysterezí (max. 1× za 10 min)
        want = 0
        to3f_w = min(p.to3f_w, min_3f_eff * 1000 + p.up_margin_w) if boost else p.to3f_w
        to1f_w = min(p.to1f_w, min_3f_eff * 1000) if boost else p.to1f_w
        if ph == 1 and avail_kw * 1000 > to3f_w:
            want = 3
        elif ph == 3 and p.allow_1f and avail_kw * 1000 < to1f_w:
            want = 1
        if want:
            if self.phase_cond_dir != want:
                self.phase_cond_dir, self.phase_cond_since = want, i.now
            held = (i.now - self.phase_cond_since).total_seconds()
            recent = self.last_phase_switch and (i.now - self.last_phase_switch).total_seconds() < p.phase_switch_min_s
            if held >= p.phase_hold_s and not recent:
                self.last_phase_switch = i.now
                self.phase_cond_dir, self.phase_cond_since = 0, None
                amps = max_amps_for(want, avail_kw, p.amin, p.amax) or p.amin
                self._reset_episode()
                return self._cmd(True, self._cap(i, want, amps), want, STATE_SOLAR,
                                 f"přebytek {i.surplus_w:.0f} W {held / 60:.0f} min → přepnout na {want}f, {amps} A")
        else:
            self.phase_cond_dir, self.phase_cond_since = 0, None

        # --- ±1 A
        now_kw = ev_power_kw(ph, amps)
        if amps < p.amax and avail_kw * 1000 > ev_power_kw(ph, amps + 1) * 1000 + p.up_margin_w:
            self._reset_episode()
            return self._cmd(True, self._cap(i, ph, amps + 1), ph, STATE_SOLAR,
                             f"přebytek {i.surplus_w:.0f} W → {amps + 1} A {ph}f")
        if avail_kw * 1000 >= now_kw * 1000 - p.down_margin_w:
            self._reset_episode()
            return self._cmd(True, self._cap(i, ph, amps), ph, STATE_SOLAR,
                             f"přebytek {i.surplus_w:.0f} W, drží {amps} A {ph}f")
        if amps > p.amin:
            return self._cmd(True, self._cap(i, ph, amps - 1), ph, STATE_SOLAR,
                             f"přebytek {i.surplus_w:.0f} W → {amps - 1} A {ph}f")

        # --- na minimu a přebytek nestačí → dotování z baterie, nebo pauza
        deficit_kw = max(0.0, now_kw - avail_kw)
        if boost and ph == 3 and deficit_kw * 1000 <= p.boost_max_deficit_w:
            # plná baterie, výkup ~0, slunce ji dobije → bez limitu epizody
            self._reset_episode()
            return self._cmd(True, amps, ph, STATE_SUPPORT,
                             f"3f z plné baterie: dotuje {deficit_kw * 1000:.0f} W (SOC {i.battery_soc:.0f} %, "
                             f"výkup {i.sell_price:.2f} Kč)")
        if self.support_since is None:
            self.support_since = i.now
        else:
            self.support_kwh += deficit_kw * dt_s / 3600
        mins = (i.now - self.support_since).total_seconds() / 60
        ok = (i.battery_soc > p.support_min_soc and i.sun_returns
              and mins < p.support_max_min and self.support_kwh < p.support_max_kwh)
        if ok:
            return self._cmd(True, amps, ph, STATE_SUPPORT,
                             f"dotuje z baterie {deficit_kw * 1000:.0f} W ({mins:.0f} min, {self.support_kwh:.2f} kWh)")
        why = ("SOC baterie ≤ %.0f %%" % p.support_min_soc if i.battery_soc <= p.support_min_soc
               else "Solcast nevidí návrat přebytku" if not i.sun_returns
               else f"limit dotování ({mins:.0f} min / {self.support_kwh:.2f} kWh)")
        self._reset_episode()
        self.resume_since = None
        return self._cmd(False, amps, ph, STATE_PAUSED, f"pozastaveno: {why}")
