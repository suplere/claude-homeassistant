"""HAv2 – AppDaemon aplikace: plánovač baterie a EV (docs/hav2-architektura.md §2, §5.2–5.4, §6).

EV část (plán + regulátor proudu) je v mixinu hav2_ev_ctl.EvControl,
filtrace bazénu v mixinu hav2_pool_ctl.PoolControl.

Čte data z HA, počítá plán (hav2_planner) a publikuje ho:
  sensor.energy_plan            – doporučený režim teď + plán po 30 min v atributech
  sensor.energy_load_forecast   – profil spotřeby domu a bojleru (ze statistik)
  input_datetime.hav2_heartbeat – živost aplikace (watchdog v HA)
  input_text.energy_last_decision + logbook – rozhodnutí a důvod

Do střídače zapisuje JEN přes script.hav2_battery_set, a to jen když
input_select.energy_system_mode == "Auto" a input_boolean.energy_battery_control == on.
Jinak jde o režim „Jen doporučení“ – plán se počítá a loguje, nic se neprovádí.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import appdaemon.plugins.hass.hassapi as hass

import hav2_boiler as BO
import hav2_planner as P
from hav2_ev_ctl import EV_CONNECTED, EvControl
from hav2_pool_ctl import PUMP, PoolControl

TZ = ZoneInfo("Europe/Prague")

# zdroje pro profil spotřeby (dlouhodobé statistiky, hodinová změna)
STAT_HOUSE = "sensor.house_consumption_sum"
STAT_EV = "sensor.ecovolter_revcr01c00002056_total_charged_energy"
STAT_POOL = "sensor.filtrace_sum"
STAT_BUY = "sensor.energy_buy_sum"
STAT_SELL = "sensor.energy_sell_sum"

EXPORT_LIMIT = "number.goodwe_limit_dodavky_do_site"
EXPORT_NORMAL_W = 10000  # běžný limit přetoku (jako v1 „Disable Overflow“)
# minutový záznam dat pro ladění (config/appdaemon/hav2_data/RRRR-MM-DD.jsonl, stahuje make pull)
DATA_DIR = Path(__file__).resolve().parents[2] / "hav2_data"
DATA_KEEP_DAYS = 60
DATA_STATES = {
    "pv_w": "sensor.pv_power",
    "house_w": "sensor.house_consumption",
    "base_w": "sensor.base_house_consumption",
    "grid_w": "sensor.meter_active_power_total",  # + přetok, − nákup (GoodWe)
    "grid_l1_w": "sensor.meter_active_power_l1",
    "grid_l2_w": "sensor.meter_active_power_l2",
    "grid_l3_w": "sensor.meter_active_power_l3",
    "batt_w": "sensor.battery_power",  # + vybíjení
    "soc": "sensor.battery_state_of_charge",
    "ems": "select.goodwe_ems_mode",
    "ems_w": "number.goodwe_ems_power_limit",
    "export_limit_w": "number.goodwe_limit_dodavky_do_site",
    "surplus_w": "sensor.energy_surplus_smoothed_w",
    "breaker_headroom_a": "sensor.energy_breaker_headroom_a",
    "boiler_idx": "sensor.energy_boiler_voltage_index",
    "boiler_on": "binary_sensor.energy_boiler_heating",
    "spot": "sensor.current_spot_electricity_price",
    "sell": "sensor.energy_price_sell_now",
    "nt": "binary_sensor.cez_hdo_lowtariffactive_dum",
    "ev_connected": "binary_sensor.ecovolter_revcr01c00002056_is_vehicle_connected",
    "ev_enabled": "switch.ecovolter_revcr01c00002056_is_charging_enable",
    "ev_amps": "number.ecovolter_revcr01c00002056_target_current",
    "ev_3f": "switch.ecovolter_revcr01c00002056_is_three_phase_mode_enable",
    "ev_w": "sensor.eco_volter_vykon",
    "ev_soc": "sensor.ev_soc_estimate",
    "ev_need_kwh": "sensor.ev_energy_needed_kwh",
    "pool_on": "switch.filtrace_switch",
    "pool_done_h": "sensor.pool_hours_done_today",
    "system_mode": "input_select.energy_system_mode",
    "plan": "sensor.energy_plan",
    "ev_reg": "sensor.ev_regulator",
    "pool_ctl": "sensor.pool_controller",
    "export_ctl": "sensor.energy_export_control",
}
DATA_ATTRS = {
    "plan_reason": ("sensor.energy_plan", "reason"),
    "plan_kw": ("sensor.energy_plan", "power_kw"),
    "ev_reason": ("sensor.ev_regulator", "reason"),
    "ev_plan": ("sensor.ev_plan", "state"),
    "ev_reserve_w": ("sensor.ev_plan", "battery_reserve_w"),
    "pool_reason": ("sensor.pool_controller", "reason"),
    "export_reason": ("sensor.energy_export_control", "reason"),
}
DEFER_HYST_W = 300  # odložené nabíjení: nákup > 300 W → auto, přetok > 300 W → zpět standby


class Hav2(EvControl, PoolControl, hass.Hass):
    def initialize(self) -> None:
        self.pnd_consumption = self.args.get("pnd_consumption_stat")
        self.pnd_production = self.args.get("pnd_production_stat")
        self.base_profile: Dict[Tuple[bool, int], float] = {}
        self.boiler_profile: Dict[int, float] = {}
        self.profile_source = "none"
        self.last_decision: Optional[Tuple[str, float]] = None
        self._pending = None
        self.last_success: Optional[datetime] = None
        self.plan_act: Optional[P.Action] = None
        self.plan_reason = ""
        self.defer_live = "standby"
        self.export_blocked = False
        self.export_published = None

        self.run_every(self.heartbeat, "now", 60)
        self.run_daily(self.refresh_profile, "00:05:00")
        self.run_in(self.refresh_profile, 5)
        self.run_every(self.tick, "now+30", 15 * 60)
        for ent in self.args.get("replan_on", []):
            self.listen_state(self.on_input_change, ent)
        self.ev_init(TZ)
        # bojler a kontrola měření proti PND (data D+1, stahují se ráno)
        self.run_in(self.pnd_check, 20)
        self.run_daily(self.pnd_check, "07:30:00")
        self.listen_state(lambda *a, **k: self.run_in(self.pnd_check, 120), "sensor.pnd_data")
        self.pool_init(TZ)
        # odložené nabíjení (pojistka proti nákupu) a omezení přetoku při záporném výkupu
        self.run_every(self.live_loop, "now+45", 60)
        self.log("HAv2 plánovač spuštěn")

    # --------------------------------------------------------------- utility
    def fnum(self, entity: str, default: float) -> float:
        try:
            return float(self.get_state(entity))
        except (TypeError, ValueError):
            return default

    def heartbeat(self, kwargs: Dict[str, Any]) -> None:
        """Živost pro watchdog v HA: jen když poslední plánování uspělo do 20 min."""
        if self.last_success and datetime.now(TZ) - self.last_success < timedelta(minutes=20):
            self.call_service("input_datetime/set_datetime", entity_id="input_datetime.hav2_heartbeat",
                              timestamp=int(datetime.now(TZ).timestamp()))

    def on_input_change(self, entity, attribute, old, new, kwargs) -> None:
        if old == new:
            return
        # debounce: více změn během 30 s = jeden přepočet
        if self._pending and self.timer_running(self._pending):
            self.cancel_timer(self._pending)
        self._pending = self.run_in(self.tick, 30, reason=f"změna {entity}")

    # ------------------------------------------------------ profil spotřeby
    def _get_statistics(self, ids: List[str], days: int) -> Dict[str, List[dict]]:
        start = (datetime.now(TZ) - timedelta(days=days)).replace(minute=0, second=0, microsecond=0)
        kwargs = dict(start_time=start.isoformat(), statistic_ids=ids, period="hour",
                      types=["change"], units={"energy": "kWh"})
        resp = None
        for flag in ({"return_response": True}, {"return_result": True}):
            try:
                resp = self.call_service("recorder/get_statistics", **kwargs, **flag)
                if resp:
                    break
            except Exception as err:  # noqa: BLE001 – různé verze AppDaemonu
                self.log(f"get_statistics ({list(flag)[0]}) selhalo: {err}", level="DEBUG")
        found = self._find_key(resp, "statistics")
        return found if isinstance(found, dict) else {}

    def _find_key(self, obj: Any, key: str) -> Any:
        if isinstance(obj, dict):
            if key in obj:
                return obj[key]
            for v in obj.values():
                r = self._find_key(v, key)
                if r is not None:
                    return r
        return None

    @staticmethod
    def _ts(value: Any) -> datetime:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, TZ)
        return datetime.fromisoformat(str(value)).astimezone(TZ)

    def _hourly(self, rows: List[dict]) -> Dict[datetime, float]:
        return {self._ts(r["start"]): float(r.get("change") or 0.0) for r in rows or []}

    def refresh_profile(self, kwargs: Dict[str, Any]) -> None:
        try:
            stats = self._get_statistics([STAT_HOUSE, STAT_EV, STAT_POOL], 14)
            house, ev, pool = (self._hourly(stats.get(i, [])) for i in (STAT_HOUSE, STAT_EV, STAT_POOL))
            buckets: Dict[Tuple[bool, int], List[float]] = {}
            for ts, kwh in house.items():
                base = max(0.0, kwh - ev.get(ts, 0.0) - pool.get(ts, 0.0))
                buckets.setdefault((ts.weekday() >= 5, ts.hour), []).append(base)
            if len(house) >= 24 * 3:
                self.base_profile = {k: statistics.median(v) for k, v in buckets.items()}
                self.profile_source = f"statistiky {len(house)} h"
            else:
                raise ValueError(f"málo dat ({len(house)} h)")
        except Exception as err:  # noqa: BLE001
            avg = self.fnum("sensor.base_average_daily_consumption", 12.0)
            self.base_profile = {(w, h): avg / 24 for w in (False, True) for h in range(24)}
            self.profile_source = f"fallback průměr {avg:.1f} kWh/den ({err})"
            self.log(f"profil spotřeby: {self.profile_source}", level="WARNING")

        # bojler = co fakturuje PND navíc proti GoodWe (nákup navíc + prodej méně)
        self.boiler_profile = {}
        if self.pnd_consumption and self.pnd_production:
            try:
                stats = self._get_statistics([self.pnd_consumption, self.pnd_production, STAT_BUY, STAT_SELL], 8)
                pi, pe, hi, he = (self._hourly(stats.get(i, [])) for i in
                                  (self.pnd_consumption, self.pnd_production, STAT_BUY, STAT_SELL))
                by_hour: Dict[int, List[float]] = {}
                for ts in pi:
                    if ts in hi and ts in pe and ts in he:
                        unseen = (pi[ts] - hi[ts]) + (he[ts] - pe[ts])
                        by_hour.setdefault(ts.hour, []).append(max(0.0, unseen))
                self.boiler_profile = {h: round(statistics.median(v), 3) for h, v in by_hour.items()
                                       if len(v) >= 3 and statistics.median(v) > 0.1}
            except Exception as err:  # noqa: BLE001
                self.log(f"profil bojleru nedostupný: {err}", level="WARNING")

        self.set_state("sensor.energy_load_forecast",
                       state=round(sum(self.base_profile.get((False, h), 0) for h in range(24))
                                   + sum(self.boiler_profile.values()), 2),
                       attributes={
                           "unit_of_measurement": "kWh", "friendly_name": "Předpověď spotřeby domu (den)",
                           "icon": "mdi:home-lightning-bolt-outline",
                           "weekday_kwh_by_hour": [round(self.base_profile.get((False, h), 0), 3) for h in range(24)],
                           "weekend_kwh_by_hour": [round(self.base_profile.get((True, h), 0), 3) for h in range(24)],
                           "boiler_kwh_by_hour": self.boiler_profile,
                           "source": self.profile_source,
                       })
        self.log(f"profil spotřeby obnoven: {self.profile_source}, bojler {sum(self.boiler_profile.values()):.2f} kWh/den")

    # ------------------------------------------------ bojler a kontrola PND
    def pnd_check(self, kwargs: Dict[str, Any]) -> None:
        """Bojler za poslední den z PND (PND − GoodWe) a zbytková odchylka měření HA."""
        if not (self.pnd_consumption and self.pnd_production):
            return
        try:
            ids = [self.pnd_consumption, self.pnd_production, STAT_BUY, STAT_SELL]
            stats = self._get_statistics(ids, 40)
            pi, pe, hi, he = (self._hourly(stats.get(i, [])) for i in ids)
            days = BO.boiler_days(pi, pe, hi, he, lambda ts: ts.hour >= 22 or ts.hour < 6,
                                  self.fnum("input_number.energy_price_nt", 3.51),
                                  self.fnum("input_number.energy_price_vt", 6.1))
            full = [d for d in days if sum(1 for ts in pi if ts.date() == d.day and ts in hi) >= 23]
            if not full:
                raise ValueError("žádný úplný den v PND")
            d = full[-1]
            ri, re_ = d.residual_pct("import"), d.residual_pct("export")
            self.set_state("sensor.energy_boiler_pnd_daily", state=round(d.boiler_kwh, 2), attributes={
                "friendly_name": "Bojler podle PND (poslední den)", "icon": "mdi:water-boiler",
                "unit_of_measurement": "kWh", "device_class": "energy", "state_class": "measurement",
                "date": d.day.isoformat(),
                "cost_kc": f"{d.cost_kc:.2f}",
                "import_kwh": f"{d.import_kwh:.2f}",
                "export_loss_kwh": f"{d.export_loss_kwh:.2f}",
                "hours_json": json.dumps(d.hours),
                "pnd_import_kwh": f"{d.pnd_import:.2f}",
                "ha_import_kwh": f"{d.ha_import:.2f}",
                "pnd_export_kwh": f"{d.pnd_export:.2f}",
                "ha_export_kwh": f"{d.ha_export:.2f}",
                "residual_import_kwh": f"{d.residual_import_kwh:.2f}",
                "residual_export_kwh": f"{d.residual_export_kwh:.2f}",
                # čísla jako text (AppDaemon zahazuje nuly); „–“ když nelze spočítat
                "residual_import_pct": f"{ri}" if ri is not None else "–",
                "residual_export_pct": f"{re_}" if re_ is not None else "–",
                "days_json": json.dumps([[x.day.isoformat(), round(x.boiler_kwh, 2), round(x.cost_kc, 2)]
                                         for x in full[-14:]]),
                "month_kwh": f"{sum(x.boiler_kwh for x in full if x.day.month == d.day.month and x.day.year == d.day.year):.2f}",
                "month_cost_kc": f"{sum(x.cost_kc for x in full if x.day.month == d.day.month and x.day.year == d.day.year):.2f}",
                "month_days": str(sum(1 for x in full if x.day.month == d.day.month and x.day.year == d.day.year)),
                "avg_kwh_14d": f"{sum(x.boiler_kwh for x in full[-14:]) / len(full[-14:]):.2f}",
                "avg_cost_14d": f"{sum(x.cost_kc for x in full[-14:]) / len(full[-14:]):.2f}",
                "updated": datetime.now(TZ).isoformat(),
            })
            self.log(f"bojler {d.day}: {d.boiler_kwh:.2f} kWh, {d.cost_kc:.1f} Kč; odchylka nákupu {ri} %, prodeje {re_} %")
        except Exception as err:  # noqa: BLE001
            self.log(f"kontrola PND selhala: {err}", level="WARNING")

    # --------------------------------------------------------------- vstupy
    def _pv_slots(self) -> List[Tuple[datetime, float]]:
        raw = self.get_state("sensor.energy_pv_forecast_corrected", attribute="slots") or []
        out = []
        for item in raw:
            try:
                out.append((datetime.fromisoformat(item[0]).astimezone(TZ), float(item[1])))
            except (TypeError, ValueError, IndexError):
                continue
        return out

    def _spot(self) -> Dict[datetime, float]:
        attrs = self.get_state("sensor.current_spot_electricity_price", attribute="all") or {}
        out = {}
        for k, v in (attrs.get("attributes") or {}).items():
            try:
                out[datetime.fromisoformat(k).astimezone(TZ)] = float(v)
            except (TypeError, ValueError):
                continue
        return out

    def _nt_fn(self):
        sched = self.get_state("sensor.cez_hdo_schedule_dum", attribute="schedule") or []
        intervals = []
        for s in sched:
            try:
                if s.get("tariff") == "NT":
                    intervals.append((datetime.fromisoformat(s["start"]).astimezone(TZ),
                                      datetime.fromisoformat(s["end"]).astimezone(TZ) + timedelta(seconds=1)))
            except (KeyError, TypeError, ValueError):
                continue
        horizon = datetime.now(TZ) + timedelta(days=2)
        covered = bool(intervals) and max(e for _, e in intervals) >= horizon.replace(hour=0, minute=0)

        def is_nt(ts: datetime) -> bool:
            if covered:
                return any(a <= ts < b for a, b in intervals)
            return ts.hour >= 22 or ts.hour < 6  # záloha: NT 22–06

        return is_nt, ("hdo" if covered else "fallback 22-06")

    # ------------------------------------------------------------- plánování
    def tick(self, kwargs: Dict[str, Any]) -> None:
        try:
            self._plan(kwargs.get("reason", "pravidelně"))
            self.last_success = datetime.now(TZ)
        except Exception as err:  # noqa: BLE001
            self.log(f"plánování selhalo: {err}", level="ERROR")
            self.set_state("sensor.energy_plan", state="error",
                           attributes={"friendly_name": "HAv2 plán baterie", "error": str(err),
                                       "generated": datetime.now(TZ).isoformat()})

    def _plan(self, reason: str) -> None:
        now = datetime.now(TZ)
        pv = self._pv_slots()
        forecast_ok = self.get_state("binary_sensor.energy_forecast_valid") == "on" and bool(pv)
        if not forecast_ok:
            pv = [(t, kw * 0.5) for t, kw in pv]  # konzervativně
        is_nt, nt_source = self._nt_fn()

        def load(h: datetime) -> float:
            return self.base_profile.get((h.weekday() >= 5, h.hour), 0.5)

        def boiler(h: datetime) -> float:
            return self.boiler_profile.get(h.hour, 0.0)

        slots = P.build_slots(now, pv, load, boiler, self._spot(), is_nt)
        batt = P.BatteryParams(
            capacity_kwh=self.fnum("input_number.battery_capacity", 10.0),
            soc_pct=self.fnum("sensor.battery_state_of_charge", 50.0),
            min_soc_pct=self.fnum("input_number.energy_battery_min_soc", 20.0),
            efficiency=self.fnum("input_number.energy_battery_efficiency", 90.0) / 100,
            max_grid_charge_kw=self.fnum("input_number.energy_battery_max_grid_charge_w", 3000) / 1000,
            wear_cost=self.fnum("input_number.energy_battery_wear_cost", 1.0),
        )
        prices = P.Prices(
            vt=self.fnum("input_number.energy_price_vt", 6.1),
            nt=self.fnum("input_number.energy_price_nt", 3.51),
            sell_coef=self.fnum("input_number.energy_sell_coefficient", 0.85),
            sell_min_spot=self.fnum("input_number.energy_sell_min_spot", 7.2),
            export_block_below=self.fnum("input_number.energy_export_block_below", 0.0),
        )
        # EV, které ještě potřebuje energii, spotřebuje polední přetok samo → odložení
        # nabíjení baterie by ji mohlo nechat večer nenabitou (plánovač EV nezná)
        allow_defer = not self.ev_wants_energy()
        plan = P.plan_battery(slots, batt, prices, allow_defer=allow_defer)
        try:
            self.ev_replan(now, slots, plan, is_nt)
        except Exception as err:  # noqa: BLE001 – chyba EV nesmí shodit plán baterie
            self.log(f"plán EV selhal: {err}", level="ERROR")
        try:
            self.pool_replan(now, slots, is_nt)
        except Exception as err:  # noqa: BLE001
            self.log(f"plán filtrace selhal: {err}", level="ERROR")
        act = plan.now
        mode = self.get_state("input_select.energy_system_mode")
        control = self.get_state("input_boolean.energy_battery_control") == "on"
        execute = mode == "Auto" and control
        today = now.date()
        cost_today = sum(r.cost for r in plan.results if r.start.date() == today)
        cost_tomorrow = sum(r.cost for r in plan.results if r.start.date() > today)

        self.set_state("sensor.energy_plan", state=act.mode, attributes={
            "friendly_name": "HAv2 plán baterie", "icon": "mdi:calendar-clock",
            "power_kw": f"{act.power_kw:.2f}",
            "reason": plan.reason,
            # AppDaemon zahazuje „nepravdivé“ hodnoty (0, False) i uvnitř seznamů →
            # logické hodnoty jako text, čísla jako řetězec, sloty jako JSON řetězec
            "executing": "ano" if execute else "ne",
            "system_mode": mode,
            "grid_charge_kwh": f"{plan.grid_charge_kwh:.2f}",
            "sell_slots": str(plan.sell_slots),
            "hits_min_soc_in_vt": "ano" if plan.hits_min_soc_in_vt else "ne",
            "cost_today_rest": f"{cost_today:.2f}",
            "cost_tomorrow": f"{cost_tomorrow:.2f}",
            "soc_now": f"{batt.soc_pct:.0f}",
            "soc_min_plan": f"{min(r.soc_pct for r in plan.results):.1f}",
            "pv_rest_today_kwh": f"{sum(s.pv_kwh for s in slots if s.start.date() == today):.2f}",
            "pv_tomorrow_kwh": f"{sum(s.pv_kwh for s in slots if s.start.date() > today):.2f}",
            "load_tomorrow_kwh": f"{sum(s.load_kwh for s in slots if s.start.date() > today):.2f}",
            "forecast_ok": "ano" if forecast_ok else "ne",
            "nt_source": nt_source,
            "profile_source": self.profile_source,
            "candidates": plan.candidates,
            "defer_slots": str(plan.defer_slots),
            "defer_allowed": "ano" if allow_defer else "ne (EV potřebuje energii)",
            "slots_json": json.dumps(P.compact(plan, slots, every=2), ensure_ascii=False),
            "slots_columns": "čas, režim, kW, SOC %, nákup kWh, prodej kWh (po 30 min)",
            # hodinová tabulka pro flex-table-card (seznam řádků, hodnoty jako text)
            "table": P.hourly_table(plan, slots, prices),
            "trigger": reason,
            "generated": now.isoformat(),
        })

        decision = (act.mode, act.power_kw)
        if decision != self.last_decision:
            self.last_decision = decision
            prefix = "" if execute else "[doporučení] "
            text = f"{now:%H:%M} {prefix}{act.mode}"
            text += f" {act.power_kw:.1f} kW" if act.power_kw else ""
            text += f": {plan.reason}"
            self.call_service("input_text/set_value", entity_id="input_text.energy_last_decision",
                              value=text[:255])
            self.call_service("logbook/log", name="HAv2 baterie", message=text[:500])
            self.log(text)
        self.plan_act, self.plan_reason = act, plan.reason
        if act.mode != P.MODE_DEFER:
            self.defer_live = "standby"
        if execute:
            self.battery_apply()

    # ------------------------------------------------- výkon plánu baterie
    def battery_apply(self) -> None:
        """Zapíše aktuální akci plánu (idempotentní – skript zapisuje jen při rozdílu)."""
        act = self.plan_act
        if act is None:
            return
        mode = act.mode
        if mode == P.MODE_DEFER:
            mode = "standby" if self.defer_live == "standby" else "auto"
        self.call_service("script/hav2_battery_set", mode=mode,
                          power_w=int(round(act.power_kw * 1000)), reason=self.plan_reason[:200])

    def battery_executing(self) -> bool:
        return (self.get_state("input_select.energy_system_mode") == "Auto"
                and self.get_state("input_boolean.energy_battery_control") == "on")

    def ev_wants_energy(self) -> bool:
        if self.get_state(EV_CONNECTED) != "on" or self.get_state("input_select.ev_mode") == "Vypnuto":
            return False
        try:
            return float(self.get_state("sensor.ev_energy_needed_kwh")) > 0.05
        except (TypeError, ValueError):
            return True  # SOC auta neznámý → počítat s tím, že nabíjí

    def live_loop(self, kwargs: Dict[str, Any]) -> None:
        try:
            self._defer_guard()
            self._export_control()
        except Exception as err:  # noqa: BLE001
            self.log(f"živá smyčka baterie selhala: {err}", level="ERROR")
        try:
            self._record_data()
        except Exception as err:  # noqa: BLE001 – záznam nesmí ovlivnit řízení
            self.log(f"záznam dat selhal: {err}", level="WARNING")

    def _record_data(self) -> None:
        now = datetime.now(TZ)
        row: Dict[str, Any] = {"t": now.isoformat(timespec="seconds"), "defer_live": self.defer_live}
        for key, ent in DATA_STATES.items():
            row[key] = self.get_state(ent)
        for key, (ent, attr) in DATA_ATTRS.items():
            row[key] = self.get_state(ent) if attr == "state" else self.get_state(ent, attribute=attr)
        DATA_DIR.mkdir(exist_ok=True)
        with open(DATA_DIR / f"{now:%Y-%m-%d}.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        if now.hour == 0 and now.minute == 0:
            for old in DATA_DIR.glob("*.jsonl"):
                if old.stem < f"{now - timedelta(days=DATA_KEEP_DAYS):%Y-%m-%d}":
                    old.unlink()

    def _defer_guard(self) -> None:
        """Odložené nabíjení = battery_standby; když dům začne nakupovat, vrátit auto (a zpět)."""
        if not self.plan_act or self.plan_act.mode != P.MODE_DEFER:
            return
        grid = self.fnum("sensor.meter_active_power_total", 0)  # + přetok, − nákup
        prev = self.defer_live
        if self.defer_live == "standby" and grid < -DEFER_HYST_W:
            self.defer_live = "auto"
        elif self.defer_live == "auto" and grid > DEFER_HYST_W:
            self.defer_live = "standby"
        if self.defer_live != prev:
            self.log(f"odložené nabíjení: síť {grid:.0f} W → {self.defer_live}")
            if self.battery_executing():
                self.battery_apply()

    def _export_control(self) -> None:
        """Záporný výkup: limit přetoku 0 W, až když EV, bazén ani baterie nic nepřijmou.

        Při limitu 0 GoodWe omezí FVE na spotřebu domu, takže přebytek pro EV a filtraci
        (FVE − dům) klesne k nule – proto se omezuje jen tehdy, když řízené spotřebiče nic
        nechtějí, a hned se uvolní, když něco začne chtít (připojení auta, chybějící hodiny).
        """
        now = datetime.now(TZ)
        sell = self.fnum("sensor.energy_price_sell_now", 99.0)
        below = self.fnum("input_number.energy_export_block_below", 0.0)
        soc = self.fnum("sensor.battery_state_of_charge", 0)
        pool_on = (self.pool_virtual if getattr(self, "pool_virtual", None) is not None and not self.pool_executing()
                   else self.get_state(PUMP) == "on")
        pool_missing = (self.get_state("input_boolean.pool_season") == "on"
                        and self.pool_target() - self.fnum("sensor.pool_hours_done_today", 0) > 0.05)
        ev_idle = not self.ev_wants_energy() and self.fnum("sensor.eco_volter_vykon", 0) < 100
        # (splněno, text když splněno, text když ne)
        checks = [
            (sell < below, f"výkup {sell:.2f} < {below:.2f} Kč/kWh", f"výkup {sell:.2f} ≥ {below:.2f} Kč/kWh"),
            (soc >= (95 if self.export_blocked else 97), f"baterie {soc:.0f} %", f"baterie jen {soc:.0f} %"),
            (ev_idle, "EV nic nechce", "EV chce nabíjet"),
            (not pool_on and not pool_missing, "filtrace hotová",
             "filtrace běží" if pool_on else "filtraci chybí hodiny"),
            (self.fnum("sensor.pv_power", 0) > 200, "FVE vyrábí", "FVE nevyrábí"),
        ]
        block = all(ok for ok, _, _ in checks)
        reason = ("přetok omezen na 0 W: " + ", ".join(t for _, t, _ in checks)) if block else \
            ("přetok povolen: " + ", ".join(f for ok, _, f in checks if not ok))
        self.export_blocked = block
        execute = self.battery_executing()
        limit = 0 if block else EXPORT_NORMAL_W
        key = (block, execute)
        if key != self.export_published:
            self.export_published = key
            prefix = "" if execute else "[doporučení] "
            self.call_service("logbook/log", name="HAv2 přetok", message=f"{prefix}{reason}"[:500])
            self.log(f"{prefix}{reason}")
        self.set_state("sensor.energy_export_control", state="omezeno" if block else "povoleno", attributes={
            "friendly_name": "HAv2 omezení přetoku", "icon": "mdi:transmission-tower-export",
            "limit_w": str(limit), "reason": reason, "executing": "ano" if execute else "ne",
            "updated": now.isoformat(),
        })
        if execute and self.fnum(EXPORT_LIMIT, -1) != limit:
            self.call_service("script/hav2_export_set", limit_w=limit, reason=reason[:200])
