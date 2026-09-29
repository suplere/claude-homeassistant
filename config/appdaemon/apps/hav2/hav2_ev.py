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
    # NT energie dostupná až za koncem slotů (předpověď FVE sahá do zítřka 24:00), ale před
    # termínem – ta se naplánuje, až bude vidět; mezitím může nabíjet slunce dalších dní
    later_nt_kwh: float = 0.0
    # část needed_kwh nad standardním limitem auta: jen ve slotech od high_from (okno před
    # termínem, kdy HAv2 zvedne limit v autě); later_nt_high_kwh = NT za koncem slotů v okně
    high_kwh: float = 0.0
    high_from: Optional[datetime] = None
    later_nt_high_kwh: float = 0.0


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
    NT se plánuje v posledním NT bloku před termínem (dřív může nabíjet slunce), v bloku
    od začátku (baterie domu se nabíjí na jeho konci), VT co nejpozději.
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
    high = min(need, max(0.0, p.high_kwh))

    def late(s: EvSlot) -> bool:
        return p.high_from is None or s.start >= p.high_from

    # slunce: část nad limitem jen ze slotů v okně, zbytek z čehokoli
    sun_all = sum(s.solar_kwh for s in window) * p.solar_confidence
    sun_high = min(high, sum(s.solar_kwh for s in window if late(s)) * p.solar_confidence)
    sun_normal = min(need - high, sun_all - sun_high)
    solar = sun_high + sun_normal
    rest_high, rest_normal = high - sun_high, need - high - sun_normal
    grid: Dict[datetime, str] = {}
    nt_kwh = vt_kwh = later_kwh = 0.0

    def fill(s: EvSlot) -> float:
        """Přidělí slotu energii (nejdřív část nad limitem, je-li slot v okně)."""
        nonlocal rest_high, rest_normal
        cap = p.grid_kw * 0.25 * s.fraction
        e_high = min(rest_high, cap) if late(s) and rest_high > 0.05 else 0.0
        e_normal = min(rest_normal, cap - e_high) if rest_normal > 0.05 else 0.0
        rest_high -= e_high
        rest_normal -= e_normal
        return e_high + e_normal

    if p.mode == "Solár+NT" and rest_high + rest_normal > 0.05:
        later_high = min(rest_high, max(0.0, p.later_nt_high_kwh))
        later_normal = min(rest_normal, max(0.0, p.later_nt_kwh - later_high))
        rest_high -= later_high
        rest_normal -= later_normal
        later_kwh = later_high + later_normal
        blocks: List[List[EvSlot]] = []
        for s in window:
            if s.is_nt:
                if blocks and blocks[-1][-1].start + timedelta(minutes=15) == s.start:
                    blocks[-1].append(s)
                else:
                    blocks.append([s])
        for block in reversed(blocks):
            for s in block:
                if rest_high + rest_normal <= 0.05:
                    break
                e = fill(s)
                if e > 0:
                    grid[s.start] = "NT"
                    nt_kwh += e
    if rest_high + rest_normal > 0.05 and p.deadline_hard and p.deadline and p.deadline > now:
        for s in reversed(window):
            if rest_high + rest_normal <= 0.05:
                break
            if not s.is_nt and s.start not in grid:
                e = fill(s)
                if e > 0:
                    grid[s.start] = "VT"
                    vt_kwh += e
    rest = rest_high + rest_normal
    shortfall = max(0.0, rest) if (p.deadline and p.deadline > now) else 0.0

    parts = [f"potřeba {need:.1f} kWh", f"slunce ~{solar:.1f}"]
    if high > 0.05 and p.high_from:
        parts.append(f"z toho nad limit auta {high:.1f} až od {p.high_from:%d.%m. %H:%M}")
    if nt_kwh:
        parts.append(f"NT {nt_kwh:.1f}")
    if later_kwh:
        parts.append(f"NT později {later_kwh:.1f} (poslední noc před termínem)")
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
    cost = (nt_kwh + later_kwh) * p.nt_price + vt_kwh * p.vt_price
    return EvPlan(need, round(solar, 2), round(nt_kwh + later_kwh, 2), round(vt_kwh, 2), round(shortfall, 2),
                  dict(sorted(grid.items())), horizon, ", ".join(parts), round(cost, 2))


# ------------------------------------------------- limit nabíjení v autě (Kia AC)
# Cíl nad standardním limitem auta = jednorázový požadavek (např. 100 % na cestu). Energie
# nad limit se nabíjí až v okně před termínem (dlouho stát na 100 % baterii škodí); na začátku
# okna HAv2 zvedne limit v autě, po nabití / odjezdu / termínu ho vrátí a cíl vrátí na standard.


def limit_step(soc: float) -> int:
    """Limit auta jde nastavit jen po 10 % (50–100)."""
    return int(min(100, max(50, -(-soc // 10) * 10)))


@dataclass
class LimitInputs:
    now: datetime
    target_soc: float  # input_number.ev_target_soc
    std_limit: float  # standardní limit auta (input_number.ev_car_limit_default)
    car_limit: Optional[float]  # skutečný limit v autě (number.ev6_ac_charging_limit)
    deadline: Optional[datetime]  # budoucí termín, jinak None
    window_h: float = 24.0
    connected: bool = False


@dataclass
class LimitPlan:
    over: bool  # cíl nad standardním limitem
    normal_top: float  # SOC, do kterého se plánuje kdykoli
    high_from: Optional[datetime]  # od kdy se smí nabíjet nad standardní limit
    in_window: bool
    top_now: float  # SOC, do kterého auto nabije teď (cíl omezený skutečným limitem)
    capped: bool  # top_now < cíl kvůli limitu v autě
    want_limit: Optional[int]  # limit, který má HAv2 v autě nastavit (None = nechat)
    level: str  # OK / Info / Varování
    message: str


def limit_plan(i: LimitInputs) -> LimitPlan:
    t, std = i.target_soc, i.std_limit
    car = i.car_limit if i.car_limit is not None else 100.0
    over = t > std + 1e-6
    high_from = (i.deadline - timedelta(hours=i.window_h) if i.deadline else i.now) if over else None
    in_window = over and i.now >= high_from
    top_now = min(t, car)
    capped = top_now < t - 1e-6
    want: Optional[int] = None
    if i.car_limit is None:
        pass  # limit v autě neznámý (Kia nedostupná) → nic nezapisovat
    elif in_window and i.connected and capped:
        want = limit_step(t)  # zvednout (jen s připojeným autem, jinak se nenabíjí tak jako tak)
    elif not in_window and car > std + 1e-6:
        want = limit_step(std)  # vrátit standard (po jednorázovém nabití nebo ruční změně v autě)

    if over:
        normal_top = std
        lim = limit_step(t)
        if in_window:
            level = "Info"
            msg = (f"Cíl {t:.0f} % je nad standardním limitem auta {std:.0f} %: limit se zvedá na {lim} %, "
                   f"po nabití se vrátí na {std:.0f} % a cíl na standard.")
            if not i.connected and capped:
                msg += " Zvedne se po připojení auta."
        else:
            level = "Info"
            msg = (f"Cíl {t:.0f} % je nad standardním limitem auta {std:.0f} %: do {std:.0f} % nabíjí kdykoli, "
                   f"nad {std:.0f} % až od {high_from:%d.%m. %H:%M} ({i.window_h:.0f} h před termínem) – "
                   f"limit auta se tehdy zvedne na {lim} %.")
    else:
        normal_top = top_now
        level, msg = "OK", ""
        if capped:
            level = "Varování"
            msg = (f"Limit nabíjení v autě {car:.0f} % je pod cílem {t:.0f} % – nabije se jen do {car:.0f} %. "
                   f"Zvyš limit v autě nebo standardní limit v HAv2.")
    return LimitPlan(over, normal_top, high_from, in_window, top_now, capped, want, level, msg)


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
    boiler_reserve_w: float = 0.0  # přebytek nechaný bojleru (už odečtený ze surplus_w)


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
        if i.boiler_reserve_w > 0 and deficit_kw * 1000 <= i.boiler_reserve_w:
            # chybí jen kvůli rezervě pro bojler → nedotovat z baterie, přetok nechat bojleru
            self._reset_episode()
            self.resume_since = None
            return self._cmd(False, amps, ph, STATE_PAUSED,
                             f"pozastaveno: přebytek nechán bojleru ({i.boiler_reserve_w:.0f} W)")
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


# ------------------------------------------------------- prostor pro bojler
# WATrouter (bojler 2,2 kW mimo měření GoodWe) spíná celým výkonem, až když přetok zřetelně
# převýší ~2,2 kW. kWh v bojleru ušetří VT (nucený ohřev od 16:09), kWh v EV jen NT →
# při plné baterii a velkém přebytku EV nechá bojleru 2,5 kW (docs/hav2-architektura.md §5.4).


@dataclass
class BoilerParams:
    enabled: bool = True
    start_h: float = 10.0  # okno (místní čas); začátek jde později posunout třeba na 12:00
    end_h: float = 15.5
    min_soc: float = 97.0  # baterie plná → přebytek jde do sítě, WATrouter ho uvidí
    on_w: float = 2600.0  # zapnout, když přebytek (bez filtrace) ≥ on_w po dobu on_hold_s
    off_w: float = 2300.0  # vypnout pod off_w (pod tím by bojler stejně nesepnul)
    on_hold_s: float = 180.0
    reserve_w: float = 2500.0
    done_export_w: float = 2300.0  # přetok GoodWe, při kterém WATrouter (relé 2,2 kW) může hřát
    boiler_kw: float = 2.2
    # Kdy je bojler „nahřátý“: napěťový detektor v poledne spolehlivě nefunguje (28. 9.), proto
    # energetický rozpočet – odhad dodané energie (2,2 kW, kdykoli rezerva platí a GoodWe exportuje
    # ≥ done_export_w) proti dávce = podíl průměrné denní spotřeby bojleru z PND
    budget_share: float = 0.6
    budget_default_kwh: float = 2.5  # když PND průměr chybí
    low_sell_kc: float = 0.5  # výkup pod tímto (i záporný) → plný bojler by stál víc → poloviční dávka
    low_sell_factor: float = 0.5


@dataclass
class BoilerInputs:
    now: datetime
    eligible: bool  # EV připojené, chce nabíjet ze slunce, termín nehrozí
    surplus_w: float  # vyhlazený přebytek bez filtrace (před rezervou)
    battery_soc: float
    heating: bool  # binary_sensor.energy_boiler_heating
    grid_export_w: float  # GoodWe přetok teď (+ = do sítě)
    boiler_avg_kwh: Optional[float] = None  # průměr denní spotřeby bojleru z PND
    sell_price: float = 99.0  # výkup teď (Kč/kWh)


@dataclass
class BoilerGate:
    """Rezerva přebytku pro bojler, kterou EV nechá ležet. Volat v každé smyčce (5 s)."""
    p: BoilerParams = field(default_factory=BoilerParams)
    active: bool = False
    on_since: Optional[datetime] = None
    day: Optional[str] = None
    done: bool = False
    delivered_kwh: float = 0.0  # odhad energie dodané do bojleru dnes (z přetoku při rezervě)
    last: Optional[datetime] = None
    reason: str = ""

    def step(self, i: BoilerInputs) -> float:
        p = self.p
        dt = (i.now - self.last).total_seconds() if self.last else 0.0
        self.last = i.now
        day = i.now.date().isoformat()
        if day != self.day:
            self.day, self.done, self.delivered_kwh = day, False, 0.0
        hour = i.now.hour + i.now.minute / 60
        blocked = (
            "vypnuto" if not p.enabled
            else "bojler dnes nahřátý" if self.done
            else "mimo okno" if not (p.start_h <= hour < p.end_h)
            else "EV nenabíjí ze slunce" if not i.eligible
            else f"baterie {i.battery_soc:.0f} % < {p.min_soc:.0f} %" if i.battery_soc < p.min_soc
            else ""
        )
        if blocked:
            self.active, self.on_since = False, None
            self.reason = blocked
            return 0.0
        budget = self.budget_kwh(i)
        if i.heating:
            # přebytek už bojler odečítá (−2 250 W) → rezerva by se počítala dvakrát
            if self._account(i, dt, budget):
                return 0.0
            self.reason = f"bojler hřeje ({self.delivered_kwh:.1f}/{budget:.1f} kWh)"
            return 0.0
        if self.active and i.surplus_w < p.off_w:
            self.active, self.on_since = False, None
        elif not self.active:
            if i.surplus_w >= p.on_w:
                self.on_since = self.on_since or i.now
                if (i.now - self.on_since).total_seconds() >= p.on_hold_s:
                    self.active = True
            else:
                self.on_since = None
        if not self.active:
            self.reason = f"přebytek < {p.on_w:.0f} W – EV bere vše"
            return 0.0
        if self._account(i, dt, budget):
            return 0.0
        self.reason = f"nechává {p.reserve_w:.0f} W bojleru ({self.delivered_kwh:.1f}/{budget:.1f} kWh)"
        return p.reserve_w

    def budget_kwh(self, i: BoilerInputs) -> float:
        p = self.p
        avg = i.boiler_avg_kwh if i.boiler_avg_kwh and i.boiler_avg_kwh > 0 else None
        budget = avg * p.budget_share if avg else p.budget_default_kwh
        if i.sell_price < p.low_sell_kc:
            budget *= p.low_sell_factor
        return budget

    def _account(self, i: BoilerInputs, dt: float, budget: float) -> bool:
        """Připočte odhad energie do bojleru; True = dávka splněna (bojler dnes „nahřátý“)."""
        if i.grid_export_w >= self.p.done_export_w:
            self.delivered_kwh += self.p.boiler_kw * dt / 3600
        if self.delivered_kwh >= budget:
            self.done, self.active = True, False
            self.reason = "bojler dnes nahřátý"
            return True
        return False


# ------------------------------------------------ vyúčtování: EV ze sítě v NT po měsících
MONTHS_CS = ["leden", "únor", "březen", "duben", "květen", "červen",
             "červenec", "srpen", "září", "říjen", "listopad", "prosinec"]


def month_starts(now: datetime, months: int) -> List[Tuple[int, int]]:
    """(rok, měsíc) od aktuálního měsíce zpět."""
    y, m = now.year, now.month
    out = []
    for _ in range(months):
        out.append((y, m))
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return out


def billing_rows(changes: Dict[str, Dict[Tuple[int, int], float]], now: datetime,
                 months: int = 6) -> List[Tuple[str, float, float]]:
    """Řádky (měsíc, kWh, Kč) – součet HAv2 (kwh/kc) a ručního NT z v1 (v1_kwh/v1_kc)."""
    rows = []
    for ym in month_starts(now, months):
        kwh = sum(changes.get(k, {}).get(ym, 0.0) for k in ("kwh", "v1_kwh"))
        kc = sum(changes.get(k, {}).get(ym, 0.0) for k in ("kc", "v1_kc"))
        rows.append((f"{MONTHS_CS[ym[1] - 1]} {ym[0]}", round(kwh, 2), round(kc, 2)))
    return rows
