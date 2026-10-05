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
import hav2_ev as E
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
STAT_BOILER = "sensor.bojler_energie"  # měření bojleru od 1. 10. 2026
STAT_SELL_NET = "sensor.energy_sell_net_energie"  # prodej bez bojleru (od 2. 10. 2026)
STAT_BOILER_GRID = "sensor.bojler_ze_site_energie"  # bojler ze sítě (od 2. 10. 2026)
STAT_SAUNA = "sensor.sauna_energy"  # Shelly Pro 1PM od 4. 10. 2026 (v profilu ne, plánuje se přes „Dnes sauna“)
SAUNA_TODAY = "input_boolean.energy_sauna_today"
SAUNA_ON_W = 1000  # nad tímto výkonem sauna topí (2,3 kW; termostat spíná celým výkonem)
BOILER_POWER = "sensor.energy_boiler_power_w"  # atribut source == "měření" → Shelly, jinak odhad

EXPORT_LIMIT = "number.goodwe_limit_dodavky_do_site"
EXPORT_NORMAL_W = 10000  # běžný limit přetoku (jako v1 „Disable Overflow“)
# minutový záznam dat pro ladění (config/appdaemon/hav2_data/RRRR-MM-DD.jsonl, stahuje make pull)
DATA_DIR = Path(__file__).resolve().parents[2] / "hav2_data"
DATA_KEEP_DAYS = 60
# vyúčtování EV ze sítě v NT: HAv2 čítače + ruční NT z v1 (do 25. 9. 2026)
BILLING_MONTHS = 6
BILLING_STATS = {
    "kwh": "sensor.ev_energy_grid_nt_total",
    "kc": "sensor.ev_charging_cost_grid_nt_total",
    "v1_kwh": "sensor.ev_nt_energie_celkem",
    "v1_kc": "sensor.ev_nt_naklady_celkem",
}
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
    "ev_target": "input_number.ev_target_soc",
    "ev_car_limit": "number.ev6_ac_charging_limit",
    "ev_target_status": "sensor.ev_target_status",
    "pool_on": "switch.bazen_filtrace_rele",
    "pool_run": "binary_sensor.pool_pump_running",
    "pool_w": "sensor.bazen_cerpadlo_vykon",
    "pool_done_h": "sensor.pool_hours_done",
    "sauna_w": "sensor.sauna_power",
    "sauna_plan": "input_boolean.energy_sauna_today",
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
    "boiler_reserve_w": ("sensor.ev_regulator", "boiler_reserve_w"),
    "boiler_gate": ("sensor.ev_regulator", "boiler_gate"),
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
        self.neg_boiler = BO.NegPriceBoiler()
        self.boiler_block_sent: Optional[bool] = None
        self.ev_grid_hold = False  # baterie drží, když EV nabíjí ze sítě
        self.sauna_on_since: Optional[datetime] = None
        self.sauna_off_since: Optional[datetime] = None
        self.sauna_seen = False  # sauna v této session „Dnes sauna“ opravdu běžela

        self.run_every(self.heartbeat, "now", 60)
        self.run_daily(self.refresh_profile, "00:05:00")
        self.run_in(self.refresh_profile, 5)
        # přepočet na hranicích 15min slotů (+10 s), aby povel slotu (např. prodej) začal a skončil včas;
        # hned po startu jeden přepočet navíc
        self.run_in(self.tick, 30)
        now = datetime.now(TZ)
        first = now.replace(second=10, microsecond=0) + timedelta(minutes=15 - now.minute % 15)
        self.run_every(self.tick, first, 15 * 60)
        for ent in self.args.get("replan_on", []):
            self.listen_state(self.on_input_change, ent)
        self.ev_init(TZ)
        # bojler a kontrola měření proti PND (data D+1, stahují se ráno)
        self.run_in(self.pnd_check, 20)
        self.run_daily(self.pnd_check, "07:30:00")
        # po dokončení synchronizace integrace ČEZ PND (statistiky cez_pnd:*); recorder zapisuje se zpožděním
        self.listen_state(lambda *a, **k: self.run_in(self.pnd_check, 120),
                          self.args.get("pnd_sync_entity", "binary_sensor.cez_elektromer_8591_0246_synchronizace_pnd_bezi"),
                          new="off")
        self.pool_init(TZ)
        # odložené nabíjení (pojistka proti nákupu) a omezení přetoku při záporném výkupu
        self.run_every(self.live_loop, "now+45", 60)
        # vyúčtování: EV ze sítě v NT po měsících (tabulka na dashboardu EV náklady)
        self.run_every(self.ev_billing, "now+90", 3600)
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

    # ------------------------------------------------ vyúčtování EV v NT
    def ev_billing(self, kwargs: Dict[str, Any]) -> None:
        try:
            self._ev_billing()
        except Exception as err:  # noqa: BLE001
            self.log(f"vyúčtování EV NT selhalo: {err}", level="WARNING")

    def _ev_billing(self) -> None:
        now = datetime.now(TZ)
        oldest = E.month_starts(now, BILLING_MONTHS)[-1]
        start = datetime(oldest[0], oldest[1], 1, tzinfo=TZ)
        stats = self._get_statistics(list(BILLING_STATS.values()), 0, period="month", start=start)
        changes: Dict[str, Dict[Tuple[int, int], float]] = {}
        for key, sid in BILLING_STATS.items():
            for row in stats.get(sid, []) or []:
                t = self._ts(row.get("start"))
                changes.setdefault(key, {})[(t.year, t.month)] = float(row.get("change") or 0)
        rows = E.billing_rows(changes, now, BILLING_MONTHS)
        def fmt(x: float, d: int) -> str:
            return f"{x:,.{d}f}".replace(",", " ").replace(".", ",") if x > 0.005 else "–"

        def row(label: str, kwh: float, kc: float) -> Dict[str, str]:
            # hodnoty jako text: AppDaemon set_state zahazuje falsy hodnoty (0) v seznamech
            return {"mesic": label, "kwh": fmt(kwh, 1), "kc": fmt(kc, 0),
                    "cena": fmt(kc / kwh, 2) if kwh > 0.05 else "–"}

        total_kwh, total_kc = sum(r[1] for r in rows), sum(r[2] for r in rows)
        table = [row(m, k, c) for m, k, c in rows] + [row(f"Celkem {BILLING_MONTHS} měsíců", total_kwh, total_kc)]
        self.set_state("sensor.ev_nt_billing", state=f"{rows[0][2]:.2f}", replace=True, attributes={
            "friendly_name": "EV nabíjení ze sítě v NT – tento měsíc", "icon": "mdi:file-document-outline",
            "unit_of_measurement": "Kč", "device_class": "monetary",
            "table": table,
            "total_kwh": fmt(total_kwh, 1),
            "total_kc": fmt(total_kc, 0),
            "updated": now.isoformat(),
        })

    # ------------------------------------------------------ profil spotřeby
    def _get_statistics(self, ids: List[str], days: int, period: str = "hour",
                        start: Optional[datetime] = None) -> Dict[str, List[dict]]:
        if start is None:
            start = (datetime.now(TZ) - timedelta(days=days)).replace(minute=0, second=0, microsecond=0)
        kwargs = dict(start_time=start.isoformat(), statistic_ids=ids, period=period,
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
            stats = self._get_statistics([STAT_HOUSE, STAT_EV, STAT_POOL, STAT_SAUNA], 14)
            house, ev, pool, sauna = (self._hourly(stats.get(i, []))
                                      for i in (STAT_HOUSE, STAT_EV, STAT_POOL, STAT_SAUNA))
            buckets: Dict[Tuple[bool, int], List[float]] = {}
            for ts, kwh in house.items():
                base = max(0.0, kwh - ev.get(ts, 0.0) - pool.get(ts, 0.0) - sauna.get(ts, 0.0))
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

        # bojler po hodinách za 8 dní: měření Shelly (od 1. 10. 2026), pro starší hodiny
        # co fakturuje PND navíc proti GoodWe (nákup navíc + prodej méně)
        self.boiler_profile = {}
        try:
            ids = [STAT_BOILER] + ([self.pnd_consumption, self.pnd_production, STAT_BUY, STAT_SELL]
                                   if self.pnd_consumption and self.pnd_production else [])
            stats = self._get_statistics(ids, 8)
            meas = self._hourly(stats.get(STAT_BOILER, []))
            by_hour: Dict[int, List[float]] = {}
            for ts, kwh in meas.items():
                by_hour.setdefault(ts.hour, []).append(max(0.0, kwh))
            if len(ids) > 1:
                pi, pe, hi, he = (self._hourly(stats.get(i, [])) for i in ids[1:])
                for ts in pi:
                    if ts not in meas and ts in hi and ts in pe and ts in he:
                        unseen = (pi[ts] - hi[ts]) + (he[ts] - pe[ts])
                        by_hour.setdefault(ts.hour, []).append(max(0.0, unseen))
            self.boiler_profile = {h: round(statistics.median(v), 3) for h, v in by_hour.items()
                                   if len(v) >= 3 and statistics.median(v) > 0.1}
            self.boiler_profile_source = f"měření {len(meas)} h + PND"
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
                           "boiler_source": getattr(self, "boiler_profile_source", "PND"),
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
            meas = self._pnd_measured_check(d.day, pi, pe, hi)
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
                # kontrola s měřením bojleru (Shelly): PND nákup ≈ GoodWe + bojler ze sítě, PND prodej ≈ prodej bez bojleru
                **meas,
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

    def _pnd_measured_check(self, day, pi, pe, hi) -> Dict[str, str]:
        """Odchylka PND proti HA s měřeným bojlerem za den `day`; „–“ dokud nejsou data celého dne."""
        out = {"meas_import_pct": "–", "meas_export_pct": "–", "meas_import_kwh": "–", "meas_export_kwh": "–"}
        try:
            stats = self._get_statistics([STAT_SELL_NET, STAT_BOILER_GRID], 3)
            sn, bg = (self._hourly(stats.get(i, [])) for i in (STAT_SELL_NET, STAT_BOILER_GRID))
            hours = [ts for ts in pi if ts.date() == day]
            if sum(1 for ts in hours if ts in sn and ts in bg) < 23:
                return out
            pnd_i = sum(pi[ts] for ts in hours)
            pnd_e = sum(pe.get(ts, 0.0) for ts in hours)
            exp_i = sum(hi.get(ts, 0.0) + bg.get(ts, 0.0) for ts in hours)
            exp_e = sum(sn.get(ts, 0.0) for ts in hours)
            out["meas_import_kwh"] = f"{pnd_i - exp_i:.2f}"
            out["meas_export_kwh"] = f"{pnd_e - exp_e:.2f}"
            if pnd_i > 0.5:
                out["meas_import_pct"] = f"{(pnd_i - exp_i) / pnd_i * 100:.1f}"
            if pnd_e > 0.5:
                out["meas_export_pct"] = f"{(pnd_e - exp_e) / pnd_e * 100:.1f}"
        except Exception as err:  # noqa: BLE001
            self.log(f"kontrola PND s měřením bojleru: {err}", level="WARNING")
        return out

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
                    end = datetime.fromisoformat(s["end"]).astimezone(TZ)
                    # „23:59:59“ = do půlnoci; „06:00:00“ = do 06:00 (slot 06:00 už je VT)
                    if end.second == 59:
                        end += timedelta(seconds=1)
                    intervals.append((datetime.fromisoformat(s["start"]).astimezone(TZ), end))
            except (KeyError, TypeError, ValueError):
                continue
        horizon = datetime.now(TZ) + timedelta(days=2)
        last_end = max(e for _, e in intervals) if intervals else None
        covered = bool(intervals) and last_end >= horizon.replace(hour=0, minute=0)

        def is_nt(ts: datetime) -> bool:
            # rozpis HDO sahá jen pár dní dopředu; za jeho koncem (NT před vzdáleným termínem EV)
            # záloha 22–06, jinak by plán EV „NT později“ neviděl žádnou noc
            if covered and ts < last_end:
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
        sauna = self._sauna_window(now)
        if sauna:
            slots = P.add_extra_load(slots, *sauna)
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
            sell_coef=self.fnum("input_number.energy_sell_coefficient", 0.75),
            sell_min_spot=self.fnum("input_number.energy_sell_min_spot", 7.2),
            export_block_below=self.fnum("input_number.energy_export_block_below", 0.0),
        )
        # EV, které ještě potřebuje energii, spotřebuje polední přetok samo → odložení
        # nabíjení baterie by ji mohlo nechat večer nenabitou (plánovač EV nezná)
        allow_defer = not self.ev_wants_energy()
        # plné nabití jednou za N dní; den před termínem se naplánuje do nejbližší NT
        full_every = self.fnum("input_number.energy_battery_full_every_days", 7)
        try:
            last_full = datetime.fromisoformat(str(self.get_state("input_datetime.energy_battery_last_full")))
            days_since_full = (now - last_full.replace(tzinfo=TZ)).total_seconds() / 86400
        except (TypeError, ValueError):
            days_since_full = 99.0
        force_full = days_since_full >= full_every - 1
        plan = P.plan_battery(slots, batt, prices, allow_defer=allow_defer, force_full=force_full)
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

        # replace=True: atributy se nahradí celé (jinak AppDaemon slučuje se starými)
        self.set_state("sensor.energy_plan", state=act.mode, replace=True, attributes={
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
            "sauna": (f"{sauna[0]:%H:%M}–{sauna[1]:%H:%M}, {sauna[2]:.1f} kW" if sauna else "ne"),
            "candidates": plan.candidates,
            "defer_slots": str(plan.defer_slots),
            "days_since_full": f"{days_since_full:.1f}",
            "full_charge_due": "ano" if force_full else "ne",
            "full_charge_planned": "ano" if plan.full_charge else "ne",
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
            self._boiler_block()  # relé dřív, než baterie začne prodávat (2. 10.: živá smyčka až po 52 s)
            self.battery_apply()

    def _sauna_window(self, now: datetime) -> Optional[Tuple[datetime, datetime, float]]:
        """„Dnes sauna“: (začátek, konec, kW) dnes; po konci se přepínač sám vypne."""
        if self.get_state(SAUNA_TODAY) != "on":
            return None
        try:
            hh, mm = (int(x) for x in str(self.get_state("input_datetime.energy_sauna_start")).split(":")[:2])
        except (TypeError, ValueError):
            return None
        start = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        end = start + timedelta(hours=self.fnum("input_number.energy_sauna_duration_h", 1.5))
        if now >= end:
            self.call_service("input_boolean/turn_off", entity_id=SAUNA_TODAY)
            self.call_service("logbook/log", name="HAv2 sauna", message=f"sauna {start:%H:%M}–{end:%H:%M} skončila")
            return None
        return start, end, self.fnum("input_number.energy_sauna_power_kw", 2.3)

    # ------------------------------------------------- výkon plánu baterie
    def battery_apply(self) -> None:
        """Zapíše aktuální akci plánu (idempotentní – skript zapisuje jen při rozdílu)."""
        act = self.plan_act
        if act is None:
            return
        mode = act.mode
        reason = self.plan_reason
        if mode == P.MODE_DEFER:
            mode = "standby" if self.defer_live == "standby" else "auto"
        # EV nabíjené ze sítě (plán NT/VT, Rychle, ručně) má jít ze sítě, ne z baterie domu
        # (3. 10. 2026: „NT“ nabíjení EV 22:15–23:00 vybilo baterii 75 → 42 %)
        if mode in ("auto", P.MODE_DISCHARGE) and self.ev_grid_hold:
            mode, reason = "standby", "EV nabíjí ze sítě – baterie drží (do EV nevybíjet)"
        self.call_service("script/hav2_battery_set", mode=mode,
                          power_w=int(round(act.power_kw * 1000)), reason=reason[:200])

    def battery_executing(self) -> bool:
        return (self.get_state("input_select.energy_system_mode") == "Auto"
                and self.get_state("input_boolean.energy_battery_control") == "on")

    def ev_wants_energy(self) -> bool:
        if self.get_state(EV_CONNECTED) != "on" or self.get_state("input_select.ev_mode") == "Vypnuto":
            return False
        need = self.ev_needed_now()  # omezeno limitem nabíjení v autě
        return True if need is None else need > 0.05  # SOC auta neznámý → počítat s tím, že nabíjí

    def _ev_grid_hold_check(self) -> None:
        """EV nabíjí ze sítě (plánovaný slot, Rychle, ručně „Nabíjet teď“) → baterie domu drží,
        do EV se nevybíjí (uživatel 3. 10. 2026). Nabíjení ze slunce s dotováním beze změny."""
        cmd = getattr(self, "ev_virtual", None)
        grid_states = (E.STATE_PLAN, E.STATE_FAST, E.STATE_MANUAL)
        hold = bool(cmd and cmd.enable and cmd.state in grid_states and self.get_state(EV_CONNECTED) == "on")
        if hold != self.ev_grid_hold:
            self.ev_grid_hold = hold
            self.log(f"baterie: {'držet' if hold else 'konec držení'} (EV {cmd.state if cmd else '–'})")
            if self.battery_executing():
                self.battery_apply()

    def live_loop(self, kwargs: Dict[str, Any]) -> None:
        try:
            self._ev_grid_hold_check()
            self._defer_guard()
            self._export_control()
            self._boiler_block()
        except Exception as err:  # noqa: BLE001
            self.log(f"živá smyčka baterie selhala: {err}", level="ERROR")
        try:
            self._sauna_detect()
        except Exception as err:  # noqa: BLE001
            self.log(f"detekce sauny selhala: {err}", level="WARNING")
        try:
            self._record_data()
        except Exception as err:  # noqa: BLE001 – záznam nesmí ovlivnit řízení
            self.log(f"záznam dat selhal: {err}", level="WARNING")

    def _sauna_detect(self) -> None:
        """Sauna běží ≥ 5 min bez „Dnes sauna“ → zapnout (začátek = skutečný start); po vypnutí
        sauny na ≥ 20 min (termostat po nahřátí spíná; a když v session běžela) „Dnes sauna“ vypnout → plán se hned přepočítá."""
        now = datetime.now(TZ)
        running = self.fnum("sensor.sauna_power", 0) > SAUNA_ON_W
        if running:
            self.sauna_on_since = self.sauna_on_since or now
            self.sauna_off_since = None
        else:
            self.sauna_off_since = self.sauna_off_since or now
            self.sauna_on_since = None
        planned = self.get_state(SAUNA_TODAY) == "on"
        if running and planned:
            self.sauna_seen = True
        if running and not planned and now - self.sauna_on_since >= timedelta(minutes=5):
            start = self.sauna_on_since
            self.call_service("input_datetime/set_datetime", entity_id="input_datetime.energy_sauna_start",
                              time=start.strftime("%H:%M:00"))
            self.call_service("input_boolean/turn_on", entity_id=SAUNA_TODAY)
            self.sauna_seen = True
            text = f"{now:%H:%M} sauna běží od {start:%H:%M} → „Dnes sauna“ zapnuto automaticky"
            self.call_service("logbook/log", name="HAv2 sauna", message=text)
            self.log(text)
        elif planned and self.sauna_seen and not running and now - self.sauna_off_since >= timedelta(minutes=20):
            self.call_service("input_boolean/turn_off", entity_id=SAUNA_TODAY)
            self.sauna_seen = False
            text = f"{now:%H:%M} sauna vypnutá → „Dnes sauna“ ukončeno"
            self.call_service("logbook/log", name="HAv2 sauna", message=text)
            self.log(text)
        if not planned and not running:
            self.sauna_seen = False

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

    def _boiler_block(self) -> None:
        """Při prodeji z baterie zablokovat bojler (relé Shelly → vstup LT WATTrouteru, plán SSR3 16–22 h).

        Jinak WATTrouter vidí přetok L3 a pošle ho do bojleru (1. 10. 2026: 1,32 kWh z 4,2 kWh prodeje).
        Zapnutí posílá každou minutu (obnoví relé i po jeho automatickém vypnutí v Shelly), vypnutí
        jen při změně; skript hav2_boiler_block sám hlídá režim Auto a zapisuje jen při rozdílu.
        """
        want = bool(self.battery_executing() and self.plan_act and self.plan_act.mode == P.MODE_DISCHARGE)
        if want or want != self.boiler_block_sent:
            reason = "prodej z baterie" if want else "konec prodeje z baterie"
            self.call_service("script/hav2_boiler_block", block=want, reason=reason)
            if want != self.boiler_block_sent:
                self.log(f"bojler: {'blokovat' if want else 'odblokovat'} ({reason})")
            self.boiler_block_sent = want

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
        S měřením bojleru (Shelly) limit = příkon bojleru + rezerva (hav2_boiler.NegPriceBoiler):
        bojler je před měřením GoodWe, takže hřeje z přetoku, který do sítě neteče.
        """
        now = datetime.now(TZ)
        sell = self.fnum("sensor.energy_price_sell_now", 99.0)
        below = self.fnum("input_number.energy_export_block_below", 0.0)
        soc = self.fnum("sensor.battery_state_of_charge", 0)
        pool_on = (self.pool_virtual if getattr(self, "pool_virtual", None) is not None and not self.pool_executing()
                   else self.get_state(PUMP) == "on")
        pool_missing = (self.get_state("input_boolean.pool_season") == "on"
                        and self.pool_target() - self.fnum("sensor.pool_hours_done", 0) > 0.05)
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
        # záporný výkup: přetok jen tolik, kolik vezme bojler (je před měřením GoodWe) – jen se Shelly
        boiler_measured = (self.get_state(BOILER_POWER, attribute="source") or "") == "měření"
        if block and boiler_measured:
            limit, btxt = self.neg_boiler.limit(now, self.fnum(BOILER_POWER, 0), self.fnum(EXPORT_LIMIT, 0))
            reason += f"; {btxt}"
        else:
            self.neg_boiler.reset()
        key = (block, execute)
        if key != self.export_published:
            self.export_published = key
            prefix = "" if execute else "[doporučení] "
            self.call_service("logbook/log", name="HAv2 přetok", message=f"{prefix}{reason}"[:500])
            self.log(f"{prefix}{reason}")
        self.set_state("sensor.energy_export_control", state="omezeno" if block else "povoleno", replace=True, attributes={
            "friendly_name": "HAv2 omezení přetoku", "icon": "mdi:transmission-tower-export",
            "limit_w": str(limit), "reason": reason, "executing": "ano" if execute else "ne",
            "updated": now.isoformat(),
        })
        if execute and self.fnum(EXPORT_LIMIT, -1) != limit:
            self.call_service("script/hav2_export_set", limit_w=limit, reason=reason[:200])
