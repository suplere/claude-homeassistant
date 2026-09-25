"""HAv2 – AppDaemon aplikace: plánovač baterie a EV (docs/hav2-architektura.md §2, §5.2–5.4, §6).

EV část (plán + regulátor proudu) je v mixinu hav2_ev_ctl.EvControl.

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
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import appdaemon.plugins.hass.hassapi as hass

import hav2_planner as P
from hav2_ev_ctl import EvControl

TZ = ZoneInfo("Europe/Prague")

# zdroje pro profil spotřeby (dlouhodobé statistiky, hodinová změna)
STAT_HOUSE = "sensor.house_consumption_sum"
STAT_EV = "sensor.ecovolter_revcr01c00002056_total_charged_energy"
STAT_POOL = "sensor.filtrace_sum"
STAT_BUY = "sensor.energy_buy_sum"
STAT_SELL = "sensor.energy_sell_sum"


class Hav2(EvControl, hass.Hass):
    def initialize(self) -> None:
        self.pnd_consumption = self.args.get("pnd_consumption_stat")
        self.pnd_production = self.args.get("pnd_production_stat")
        self.base_profile: Dict[Tuple[bool, int], float] = {}
        self.boiler_profile: Dict[int, float] = {}
        self.profile_source = "none"
        self.last_decision: Optional[Tuple[str, float]] = None
        self._pending = None
        self.last_success: Optional[datetime] = None

        self.run_every(self.heartbeat, "now", 60)
        self.run_daily(self.refresh_profile, "00:05:00")
        self.run_in(self.refresh_profile, 5)
        self.run_every(self.tick, "now+30", 15 * 60)
        for ent in self.args.get("replan_on", []):
            self.listen_state(self.on_input_change, ent)
        self.ev_init(TZ)
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
        plan = P.plan_battery(slots, batt, prices)
        try:
            self.ev_replan(now, slots, plan, is_nt)
        except Exception as err:  # noqa: BLE001 – chyba EV nesmí shodit plán baterie
            self.log(f"plán EV selhal: {err}", level="ERROR")
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
            "cost_today_rest": round(cost_today, 2),
            "cost_tomorrow": round(cost_tomorrow, 2),
            "soc_now": batt.soc_pct,
            "soc_min_plan": min(r.soc_pct for r in plan.results),
            "pv_rest_today_kwh": round(sum(s.pv_kwh for s in slots if s.start.date() == today), 2),
            "pv_tomorrow_kwh": round(sum(s.pv_kwh for s in slots if s.start.date() > today), 2),
            "load_tomorrow_kwh": round(sum(s.load_kwh for s in slots if s.start.date() > today), 2),
            "forecast_ok": "ano" if forecast_ok else "ne",
            "nt_source": nt_source,
            "profile_source": self.profile_source,
            "candidates": plan.candidates,
            "slots_json": json.dumps(P.compact(plan, slots, every=2), ensure_ascii=False),
            "slots_columns": "čas, režim, kW, SOC %, nákup kWh, prodej kWh (po 30 min)",
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
        if execute:
            # idempotentní: skript zapisuje do střídače jen při rozdílu proti aktuálnímu stavu
            self.call_service("script/hav2_battery_set", mode=act.mode,
                              power_w=int(round(act.power_kw * 1000)), reason=plan.reason[:200])
