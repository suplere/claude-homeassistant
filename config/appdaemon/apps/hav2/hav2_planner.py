"""HAv2 – plánovač baterie (čistý Python, bez závislosti na AppDaemonu).

Návrh: docs/hav2-architektura.md §5.2 a §6.
- sloty po 15 min od teď do zítřka 24:00
- simulace SOC s modelem GoodWe EMS režimů (auto / standby / charge / discharge)
- volba nejmenšího nabití ze sítě v NT, které minimalizuje náklady
- prodej z baterie ve spotové špičce, jen když se to vyplatí
Náklady: nákup × NT/VT − prodej × spot × koeficient + opotřebení × vybitá energie.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

SLOT = timedelta(minutes=15)
SLOT_H = 0.25

MODE_AUTO = "auto"
MODE_STANDBY = "standby"
MODE_CHARGE = "charge"
MODE_DISCHARGE = "discharge"


@dataclass(frozen=True)
class BatteryParams:
    capacity_kwh: float
    soc_pct: float
    min_soc_pct: float = 20.0
    max_soc_pct: float = 100.0
    efficiency: float = 0.90  # celý cyklus (nabití + vybití)
    max_charge_kw: float = 5.0
    max_discharge_kw: float = 5.0
    max_grid_charge_kw: float = 3.0
    wear_cost: float = 1.0  # Kč za vybitou kWh


@dataclass(frozen=True)
class Prices:
    vt: float = 6.10
    nt: float = 3.51
    sell_coef: float = 0.85
    sell_min_spot: float = 7.2
    export_block_below: float = 0.0  # pod touto výkupní cenou se přetok neplatí (limit 0 W)


@dataclass
class Slot:
    start: datetime
    pv_kwh: float
    load_kwh: float
    is_nt: bool
    spot: float  # Kč/kWh
    fraction: float = 1.0  # první slot může být jen částečný
    # zátěž mimo měření GoodWe (bojler přes WATrouter): baterie ji nevidí ani nekryje,
    # bere se přímo ze sítě – nejdřív sníží přetok, zbytek je nákup
    grid_only_kwh: float = 0.0

    def buy_price(self, prices: Prices) -> float:
        return prices.nt if self.is_nt else prices.vt


@dataclass
class Action:
    mode: str = MODE_AUTO
    power_kw: float = 0.0


@dataclass
class SlotResult:
    start: datetime
    mode: str
    power_kw: float
    soc_pct: float  # na konci slotu
    grid_import_kwh: float
    grid_export_kwh: float
    battery_in_kwh: float
    battery_out_kwh: float
    cost: float


@dataclass
class Plan:
    actions: List[Action]
    results: List[SlotResult]
    cost: float
    grid_charge_kwh: float
    sell_slots: int
    reason: str = ""
    hits_min_soc_in_vt: bool = False
    candidates: Dict[str, float] = field(default_factory=dict)

    @property
    def now(self) -> Action:
        return self.actions[0]


# ----------------------------------------------------------------- sloty


def floor_slot(ts: datetime) -> datetime:
    return ts.replace(minute=ts.minute - ts.minute % 15, second=0, microsecond=0)


def build_slots(
    now: datetime,
    pv_30min: Sequence[Tuple[datetime, float]],
    load_kwh_by_hour: Callable[[datetime], float],
    boiler_kwh_by_hour: Callable[[datetime], float],
    spot_by_hour: Dict[datetime, float],
    is_nt: Callable[[datetime], bool],
    horizon_end: Optional[datetime] = None,
) -> List[Slot]:
    """Sestaví 15min sloty od teď do konce horizontu (výchozí zítra 24:00).

    pv_30min: [(začátek, průměrný výkon kW)] po 30 min (opravená předpověď).
    load_kwh_by_hour / boiler_kwh_by_hour: energie za hodinu začínající v daném čase.
    spot_by_hour: {začátek hodiny: Kč/kWh}; chybějící hodina → poslední známá cena.
    """
    start = floor_slot(now)
    if horizon_end is None:
        horizon_end = (start + timedelta(days=2)).replace(hour=0, minute=0)
    pv_map: Dict[datetime, float] = {}
    for ts, kw in pv_30min:
        pv_map[ts] = kw
        pv_map[ts + SLOT] = kw
    last_spot = next(iter(spot_by_hour.values()), 0.0) if spot_by_hour else 0.0
    slots: List[Slot] = []
    t = start
    while t < horizon_end:
        hour = t.replace(minute=0)
        spot = spot_by_hour.get(hour, last_spot)
        last_spot = spot
        frac = 1.0
        if t == start:
            frac = max(0.0, min(1.0, (t + SLOT - now).total_seconds() / SLOT.total_seconds()))
        pv = pv_map.get(t, 0.0) * SLOT_H * frac
        load = load_kwh_by_hour(hour) / 4 * frac
        boiler = boiler_kwh_by_hour(hour) / 4 * frac
        slots.append(Slot(t, pv, load, is_nt(t), spot, frac, boiler))
        t += SLOT
    return slots


# ------------------------------------------------------------- simulace


def simulate(
    slots: Sequence[Slot],
    actions: Sequence[Action],
    batt: BatteryParams,
    prices: Prices,
) -> Tuple[List[SlotResult], float]:
    eta = math.sqrt(batt.efficiency)  # účinnost jedné cesty
    cap = batt.capacity_kwh
    soc = cap * batt.soc_pct / 100
    soc_min = cap * batt.min_soc_pct / 100
    soc_max = cap * batt.max_soc_pct / 100
    total = 0.0
    out: List[SlotResult] = []
    for slot, act in zip(slots, actions):
        dt = SLOT_H * slot.fraction
        net = slot.pv_kwh - slot.load_kwh  # + přebytek, − deficit
        b_in = b_out = 0.0  # energie na svorkách baterie (AC strana)
        if act.mode == MODE_AUTO:
            if net >= 0:
                b_in = min(net, batt.max_charge_kw * dt, max(0.0, soc_max - soc) / eta)
            else:
                b_out = min(-net, batt.max_discharge_kw * dt, max(0.0, soc - soc_min) * eta)
        elif act.mode == MODE_CHARGE:
            b_in = min(act.power_kw * dt, batt.max_charge_kw * dt, max(0.0, soc_max - soc) / eta)
        elif act.mode == MODE_DISCHARGE:
            b_out = min(act.power_kw * dt, batt.max_discharge_kw * dt, max(0.0, soc - soc_min) * eta)
        # MODE_STANDBY: baterie stojí
        soc += b_in * eta - b_out / eta
        grid = slot.load_kwh + slot.grid_only_kwh + b_in - slot.pv_kwh - b_out  # + nákup, − prodej
        imp = max(grid, 0.0)
        exp = max(-grid, 0.0)
        sell = slot.spot * prices.sell_coef
        revenue = 0.0 if sell < prices.export_block_below else exp * sell
        cost = imp * slot.buy_price(prices) - revenue + b_out * batt.wear_cost
        total += cost
        out.append(SlotResult(slot.start, act.mode, act.power_kw, round(soc / cap * 100, 1),
                              imp, exp, b_in, b_out, cost))
    # hodnota energie, která v baterii zbude (náhrada za NT cenu)
    total -= max(0.0, soc - soc_min) * prices.nt
    return out, total


# -------------------------------------------------------------- plánovač


def _first_nt_block(slots: Sequence[Slot]) -> List[int]:
    """Indexy slotů prvního NT bloku v horizontu (včetně probíhajícího)."""
    idx: List[int] = []
    for i, s in enumerate(slots):
        if s.is_nt:
            idx.append(i)
        elif idx:
            break
    return idx


def _actions_for_charge(slots: Sequence[Slot], nt_idx: List[int], energy_kwh: Optional[float],
                        batt: BatteryParams) -> List[Action]:
    """Varianty chování v prvním NT bloku.

    energy_kwh None → auto (baterie smí v NT vybíjet do domu),
    0 → baterie v NT stojí (drží energii na VT),
    > 0 → stojí a na konci NT se nabije `energy_kwh` (AC) ze sítě.
    """
    actions = [Action() for _ in slots]
    if energy_kwh is None or not nt_idx:
        return actions
    for i in nt_idx:
        actions[i] = Action(MODE_STANDBY, 0.0)
    remaining = energy_kwh
    for i in reversed(nt_idx):
        if remaining <= 1e-6:
            break
        per_slot = batt.max_grid_charge_kw * SLOT_H * slots[i].fraction
        if per_slot <= 0:
            continue
        take = min(per_slot, remaining)
        actions[i] = Action(MODE_CHARGE, round(take / (SLOT_H * slots[i].fraction), 2))
        remaining -= take
    return actions


def plan_battery(slots: Sequence[Slot], batt: BatteryParams, prices: Prices,
                 step_kwh: float = 0.5) -> Plan:
    if not slots:
        raise ValueError("no slots")
    nt_idx = _first_nt_block(slots)
    eta = math.sqrt(batt.efficiency)
    room = max(0.0, (batt.max_soc_pct - batt.soc_pct) / 100 * batt.capacity_kwh) / eta
    grid_cap = sum(batt.max_grid_charge_kw * SLOT_H * slots[i].fraction for i in nt_idx)
    max_e = min(room, grid_cap)

    # vyplatí se nabíjení v NT vůbec? NT/účinnost + opotřebení < VT
    worth = prices.nt / batt.efficiency + batt.wear_cost < prices.vt
    candidates: Dict[str, float] = {}
    best: Optional[Tuple[Optional[float], float, List[Action], List[SlotResult]]] = None
    # pořadí = preference při téměř stejné ceně: auto → držet → menší nabití
    steps: List[Optional[float]] = [None]
    if nt_idx:
        steps.append(0.0)
    if worth and nt_idx:
        e = step_kwh
        while e < max_e - 1e-6:
            steps.append(round(e, 3))
            e += step_kwh
        if max_e > 0:
            steps.append(round(max_e, 3))
    for e in steps:
        acts = _actions_for_charge(slots, nt_idx, e, batt)
        res, cost = simulate(slots, acts, batt, prices)
        candidates["auto" if e is None else f"{e:g}"] = round(cost, 2)
        if best is None or cost < best[1] - 0.05:
            best = (e, cost, acts, res)
    assert best is not None
    e_best, cost_best, acts, res = best
    e_best = e_best or 0.0

    # prodej ve spotové špičce: jen VT sloty nad prahem, jen když to sníží náklady
    sell_slots = 0
    peak = sorted(
        (i for i, s in enumerate(slots)
         if not s.is_nt and s.spot >= prices.sell_min_spot and acts[i].mode == MODE_AUTO),
        key=lambda i: -slots[i].spot,
    )
    for i in peak:
        trial = list(acts)
        trial[i] = Action(MODE_DISCHARGE, batt.max_discharge_kw)
        tres, tcost = simulate(slots, trial, batt, prices)
        # zadání: prodávat jen když baterii týž den prokazatelně dobije slunce
        # (simulace po prodeji dosáhne ≥ 95 % SOC ještě před 18:00 téhož dne)
        day = slots[i].start.date()
        refilled = any(
            r.soc_pct >= 95.0 and r.start.date() == day and r.start.hour < 18
            for r in tres[i + 1:]
        )
        if refilled and tcost < cost_best - 0.05:
            acts, res, cost_best = trial, tres, tcost
            sell_slots += 1

    min_soc = batt.min_soc_pct + 0.05
    hits_min = any(r.soc_pct <= min_soc for r, s in zip(res, slots) if not s.is_nt)
    plan = Plan(acts, res, round(cost_best, 2), round(e_best, 2), sell_slots,
                hits_min_soc_in_vt=hits_min, candidates=candidates)
    plan.reason = explain(plan, slots, batt, prices, worth)
    return plan


def explain(plan: Plan, slots: Sequence[Slot], batt: BatteryParams, prices: Prices,
            worth: bool) -> str:
    now = plan.now
    s0 = slots[0]
    pv_tomorrow = sum(s.pv_kwh for s in slots if s.start.date() > s0.start.date())
    if now.mode == MODE_CHARGE:
        return (f"NT nabíjení {now.power_kw:.1f} kW, celkem {plan.grid_charge_kwh:.1f} kWh "
                f"(FVE zítra {pv_tomorrow:.1f} kWh nestačí)")
    if now.mode == MODE_STANDBY:
        if plan.grid_charge_kwh > 0:
            return (f"NT: baterie drží energii na VT, nabití {plan.grid_charge_kwh:.1f} kWh "
                    f"na konci NT")
        return "NT: baterie drží energii na ranní VT (dům jede ze sítě za NT)"
    if now.mode == MODE_DISCHARGE:
        return f"spotová špička {s0.spot:.2f} Kč/kWh ≥ {prices.sell_min_spot:.2f}: prodej z baterie"
    parts = ["vlastní spotřeba"]
    if s0.is_nt:
        if not worth:
            parts.append("NT nabíjení se nevyplatí (NT/účinnost + opotřebení ≥ VT)")
        elif plan.grid_charge_kwh == 0:
            parts.append(f"NT bez nabíjení, FVE zítra {pv_tomorrow:.1f} kWh baterii dobije")
    elif plan.grid_charge_kwh > 0:
        parts.append(f"v NT plánováno nabití {plan.grid_charge_kwh:.1f} kWh")
    if plan.sell_slots:
        parts.append(f"prodej ve špičce {plan.sell_slots}× 15 min")
    if plan.hits_min_soc_in_vt:
        parts.append("pozor: ve VT dojde na min. SOC")
    return ", ".join(parts)


def compact(plan: Plan, slots: Sequence[Slot], every: int = 2) -> List[list]:
    """Zhuštěný plán pro atributy HA (každý `every`-tý slot): [čas, režim, kW, SOC, import, export]."""
    out = []
    for i, (r, s) in enumerate(zip(plan.results, slots)):
        if i % every:
            continue
        out.append([r.start.isoformat(timespec="minutes"), r.mode, round(r.power_kw, 2), r.soc_pct,
                    round(r.grid_import_kwh, 3), round(r.grid_export_kwh, 3)])
    return out
