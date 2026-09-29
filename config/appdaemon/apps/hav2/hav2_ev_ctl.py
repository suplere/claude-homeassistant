"""HAv2 – EV: napojení plánovače a regulátoru (hav2_ev) na Home Assistant.

Mixin pro aplikaci Hav2 (hav2_app). Publikuje:
  sensor.ev_plan       – rozdělení potřebné energie (slunce / NT / VT) a síťové sloty
  sensor.ev_regulator  – stav regulátoru, doporučený proud a fáze, důvod
  sensor.ev_target_status – cíl vs. limit nabíjení v autě (OK / Info / Varování)
  input_text.ev_last_decision + logbook – změny rozhodnutí
Do EcoVolteru zapisuje JEN přes script.hav2_ev_set, a to jen když
input_select.energy_system_mode == "Auto" a input_boolean.ev_control == on. Za stejných
podmínek nastavuje limit AC nabíjení v autě (number.ev6_ac_charging_limit) pro jednorázový
cíl nad standardním limitem (docs/hav2-architektura.md §5.3).
V režimu „Jen doporučení“ regulátor běží nad virtuálním wallboxem (poslední vlastní povel),
aby doporučení dávala smysl i když wallbox mezitím řídí staré automatizace v1.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

import hav2_ev as E

EV = "ecovolter_revcr01c00002056"
EV_CHARGING = f"switch.{EV}_is_charging_enable"
EV_CURRENT = f"number.{EV}_target_current"
EV_3F = f"switch.{EV}_is_three_phase_mode_enable"
EV_CONNECTED = f"binary_sensor.{EV}_is_vehicle_connected"
EV_CAR_LIMIT = "number.ev6_ac_charging_limit"
EV_TARGET = "input_number.ev_target_soc"
EV_TARGET_DEFAULT = "input_number.ev_target_soc_default"
EV_CAR_LIMIT_DEFAULT = "input_number.ev_car_limit_default"
EV_HIGH_WINDOW = "input_number.ev_high_soc_window_h"
NOTIFY = "notify/mobile_app_evzen_iphone"

OVERRIDE_AFTER_S = 90  # rozdíl proti vlastnímu povelu starší než tohle = ruční zásah
OVERRIDE_FOR = timedelta(hours=2)
LIMIT_RETRY = timedelta(minutes=10)  # zápis limitu do auta (Kia cloud) – další pokus nejdřív za
LIMIT_TRIES = 3
FORCE_UPDATE_PER_DAY = 2
# „bojler dnes nahřátý“ – pomocník v HA: přežije restart, je vidět v UI a jde ručně změnit
BOILER_LAST_FULL = "input_datetime.energy_boiler_last_full"


class EvControl:
    """Předpokládá hass.Hass (self.get_state, call_service, …) a TZ/fnum z Hav2."""

    def ev_init(self, tz) -> None:
        self.ev_tz = tz
        self.ev_reg = E.Regulator()
        self.ev_plan: Optional[E.EvPlan] = None
        self.ev_reserve_w = 0.0
        self.ev_battery_refill = False
        self.ev_boiler = E.BoilerGate()
        self._ev_boiler_sync(self.get_state(BOILER_LAST_FULL))
        self.ev_boiler_reserve_w = 0.0
        self.ev_boiler_logged: Optional[tuple] = None
        self.ev_virtual: Optional[E.Command] = None
        self.ev_written: Optional[Tuple[E.Command, datetime]] = None
        self.ev_override_until: Optional[datetime] = None
        self.ev_published: Optional[tuple] = None
        self.ev_logged: Optional[tuple] = None
        self.ev_manual_since: Optional[datetime] = None
        self.ev_force_updates: Dict[str, int] = {}
        self.ev_unavailable_since: Optional[datetime] = None
        self.ev_notified: set = set()
        self.ev_limit_tries: Dict[int, Tuple[datetime, int]] = {}
        self.ev_limit_error = ""
        self.ev_deadline_watch: Optional[datetime] = None

        self.run_every(self.ev_loop, "now+40", 5)
        self.listen_state(self.ev_on_connect, EV_CONNECTED)
        self.listen_state(self.ev_on_manual, "input_select.ev_manual")
        self.listen_state(self.ev_on_boiler_full, BOILER_LAST_FULL)

    # ----------------------------------------------------------- pomocné
    def ev_executing(self) -> bool:
        return (self.get_state("input_select.energy_system_mode") == "Auto"
                and self.get_state("input_boolean.ev_control") == "on")

    def ev_params(self) -> E.RegParams:
        return E.RegParams(
            amin=int(self.fnum("input_number.ev_current_min", 6)),
            amax=min(11, int(self.fnum("input_number.ev_current_max", 11))),
            interval_s=int(self.fnum("input_number.ev_regulation_interval_s", 60)),
            allow_1f=self.get_state("input_boolean.ev_allow_1f") == "on",
            support_min_soc=self.fnum("input_number.ev_support_min_soc", 60),
            support_max_min=self.fnum("input_number.ev_support_max_min", 10),
            support_max_kwh=self.fnum("input_number.ev_support_max_kwh", 1.0),
            resume_after_min=self.fnum("input_number.ev_resume_after_min", 5),
        )

    def ev_deadline(self, now: datetime) -> Optional[datetime]:
        raw = self.get_state("input_datetime.ev_deadline")
        try:
            d = datetime.fromisoformat(str(raw)).replace(tzinfo=self.ev_tz)
        except (TypeError, ValueError):
            return None
        return d if d > now else None

    def ev_wallbox(self) -> Tuple[bool, bool, int, int]:
        """(dostupný, nabíjení zapnuto, proud, fáze) – skutečný stav EcoVolteru."""
        sw, cur, ph = (self.get_state(e) for e in (EV_CHARGING, EV_CURRENT, EV_3F))
        available = sw in ("on", "off") and cur not in (None, "unavailable", "unknown")
        try:
            amps = int(float(cur))
        except (TypeError, ValueError):
            amps = 6
        return available, sw == "on", amps, 3 if ph == "on" else 1

    # ---------------------------------------------- cíl a limit v autě
    def ev_limit_plan(self, now: datetime) -> E.LimitPlan:
        try:
            car: Optional[float] = float(self.get_state(EV_CAR_LIMIT))
        except (TypeError, ValueError):
            car = None
        return E.limit_plan(E.LimitInputs(
            now=now, target_soc=self.fnum(EV_TARGET, 80), std_limit=self.fnum(EV_CAR_LIMIT_DEFAULT, 90),
            car_limit=car, deadline=self.ev_deadline(now), window_h=self.fnum(EV_HIGH_WINDOW, 24),
            connected=self.get_state(EV_CONNECTED) == "on"))

    def ev_soc(self) -> Optional[float]:
        try:
            return float(self.get_state("sensor.ev_soc_estimate"))
        except (TypeError, ValueError):
            return None

    def ev_energy_to(self, soc_to: float) -> Optional[float]:
        """kWh do daného SOC (stejně jako sensor.ev_energy_needed_kwh); None = SOC neznámý."""
        soc = self.ev_soc()
        if soc is None:
            return None
        return max(0.0, soc_to - soc) / 100 * self.fnum("input_number.ev_battery_capacity_kwh", 77.4) \
            / (self.fnum("input_number.ev_charge_efficiency", 90) / 100)

    def ev_reached(self, lp: E.LimitPlan, soc_to: Optional[float] = None) -> bool:
        """Dosažen SOC, do kterého teď auto nabije (cíl omezený limitem v autě).
        Na limitu auta auto přestane brát samo → tolerance 1 % proti odhadu SOC."""
        top = lp.top_now if soc_to is None else soc_to
        soc, e = self.ev_soc(), self.ev_energy_to(top)
        if soc is None or e is None:
            try:
                return float(self.get_state("sensor.ev_energy_needed_kwh")) <= 0.05
            except (TypeError, ValueError):
                return False
        self_stop = top >= lp.top_now - 1e-6 and (lp.capped or top >= 100 - 1e-6)
        return e <= 0.05 or (self_stop and soc >= top - 1.0)

    def ev_needed_now(self) -> Optional[float]:
        """kWh, které auto teď opravdu přijme (0 = hotovo); None = SOC neznámý."""
        lp = self.ev_limit_plan(datetime.now(self.ev_tz))
        if self.ev_reached(lp):
            return 0.0
        return self.ev_energy_to(lp.top_now)

    def _ev_limit_act(self, now: datetime, lp: E.LimitPlan) -> None:
        """Zvednutí / vrácení limitu v autě (jen při řízení EV v režimu Auto)."""
        want = lp.want_limit
        if want is None:
            self.ev_limit_tries, self.ev_limit_error = {}, ""
            return
        if not self.ev_executing():
            return
        last, tries = self.ev_limit_tries.get(want, (None, 0))
        if last and now - last < LIMIT_RETRY:
            return
        if tries >= LIMIT_TRIES:
            self.ev_limit_error = f"Limit v autě se nepodařilo nastavit na {want} % ({tries} pokusy přes Kia cloud)."
            key = ("limit", want)
            if key not in self.ev_notified:
                self.ev_notified.add(key)
                self.call_service(NOTIFY, title="HAv2 – limit nabíjení v autě",
                                  message=self.ev_limit_error + " Nastav ho ručně v aplikaci Kia.")
            return
        self.ev_limit_tries = {want: (now, tries + 1)}
        self.call_service("number/set_value", entity_id=EV_CAR_LIMIT, value=want)
        self.run_in(lambda *_: self.call_service("kia_uvo/update"), 180)
        text = f"{now:%H:%M} limit AC nabíjení v autě → {want} % (pokus {tries + 1})"
        self.call_service("logbook/log", name="HAv2 EV", message=text)
        self.log(text)

    def _ev_oneoff_done(self, now: datetime, why: str) -> None:
        """Jednorázový cíl nad standardem splněn / skončil → cíl zpět na standard."""
        target, default = self.fnum(EV_TARGET, 80), self.fnum(EV_TARGET_DEFAULT, 80)
        if target <= default or not self.ev_executing():
            return
        self.call_service("input_number/set_value", entity_id=EV_TARGET, value=default)
        self.call_service("logbook/log", name="HAv2 EV",
                          message=f"{now:%H:%M} jednorázový cíl {target:.0f} % skončil ({why}) → cíl {default:.0f} %")

    def _ev_deadline_check(self, now: datetime) -> None:
        """Termín právě nastal: auto doma pod cílem → notifikace; jednorázový cíl končí."""
        dl = self.ev_deadline(now)
        if dl:
            self.ev_deadline_watch = dl
            return
        watch, self.ev_deadline_watch = self.ev_deadline_watch, None
        if not watch or now < watch:
            return
        target, soc = self.fnum(EV_TARGET, 80), self.ev_soc()
        if self.get_state(EV_CONNECTED) == "on" and soc is not None and soc < target - 1 and self.ev_executing():
            self.call_service(NOTIFY, title="HAv2 – EV pod cílem v termínu",
                              message=f"Termín {watch:%d.%m. %H:%M}: SOC auta {soc:.0f} % < cíl {target:.0f} %.")
        self._ev_oneoff_done(now, f"termín {watch:%d.%m. %H:%M}")

    def _ev_publish_target(self, now: datetime, lp: E.LimitPlan, ev_plan: E.EvPlan) -> None:
        level, parts = lp.level, [lp.message] if lp.message else []
        if lp.want_limit is not None and lp.want_limit > (self.fnum(EV_CAR_LIMIT, 100)) and not self.ev_executing():
            level = "Varování"
            parts.append(f"Řízení EV neběží (režim není Auto nebo je vypnuté) – limit v autě nastav ručně "
                         f"na {lp.want_limit} %.")
        if self.ev_limit_error:
            level = "Varování"
            parts.append(self.ev_limit_error)
        if ev_plan.shortfall_kwh > 0.05:
            level = "Varování"
            parts.append(f"Do termínu se nestihne {ev_plan.shortfall_kwh:.1f} kWh – SOC auta bude pod cílem.")
        self.set_state("sensor.ev_target_status", state=level, replace=True, attributes={
            "friendly_name": "HAv2 EV cíl a limit auta",
            "icon": "mdi:alert" if level == "Varování" else "mdi:battery-arrow-up" if level == "Info"
            else "mdi:check-circle",
            "message": " ".join(parts) or "—",
            "target_soc": f"{self.fnum(EV_TARGET, 80):.0f}",
            "car_limit": self.get_state(EV_CAR_LIMIT) or "?",
            "std_limit": f"{self.fnum(EV_CAR_LIMIT_DEFAULT, 90):.0f}",
            "charge_to_now": f"{lp.top_now:.0f}",
            "high_from": lp.high_from.isoformat(timespec="minutes") if lp.high_from else "—",
            "in_window": "ano" if lp.in_window else "ne",
            "want_limit": str(lp.want_limit) if lp.want_limit is not None else "—",
            "updated": now.isoformat(),
        })

    # ------------------------------------------------------------- plán
    def ev_replan(self, now: datetime, slots, plan, is_nt) -> None:
        """Volá se z plánovače baterie: přebytek pro EV = přetok v plánu baterie."""
        ev_slots = [E.EvSlot(s.start, s.is_nt, r.grid_export_kwh, s.fraction)
                    for s, r in zip(slots, plan.results)]
        self._ev_deadline_check(now)
        lp = self.ev_limit_plan(now)
        if lp.over and self.ev_reached(lp, self.fnum(EV_TARGET, 80)) and not lp.capped:
            self._ev_oneoff_done(now, "nabito")
        self._ev_limit_act(now, lp)
        # potřeba do cíle; nad standardním limitem auta jen v okně před termínem
        high_kwh = 0.0
        e_top = self.ev_energy_to(lp.top_now if not lp.over else self.fnum(EV_TARGET, 80))
        if e_top is not None:
            needed, soc_known = (0.0 if self.ev_reached(lp) and not lp.over else e_top), True
            if lp.over:
                high_kwh = max(0.0, e_top - (self.ev_energy_to(lp.normal_top) or 0.0))
        else:
            needed, soc_known = 0.0, False
        connected = self.get_state(EV_CONNECTED) == "on"
        params = E.EvPlanParams(
            needed_kwh=needed if connected else 0.0,
            mode=self.get_state("input_select.ev_mode") or "Solár+NT",
            deadline=self.ev_deadline(now),
            deadline_hard=self.get_state("input_boolean.ev_deadline_hard") == "on",
            nt_price=self.fnum("input_number.energy_price_nt", 3.51),
            vt_price=self.fnum("input_number.energy_price_vt", 6.1),
            high_kwh=high_kwh if connected else 0.0,
            high_from=lp.high_from,
        )
        # NT za koncem slotů (a před termínem) – naplánuje se, až bude v horizontu
        if params.deadline and slots:
            t = slots[-1].start + timedelta(minutes=15)
            later = later_high = 0.0
            while t < params.deadline:
                if is_nt(t):
                    later += params.grid_kw * 0.25
                    if lp.high_from is None or t >= lp.high_from:
                        later_high += params.grid_kw * 0.25
                t += timedelta(minutes=15)
            params.later_nt_kwh = later
            params.later_nt_high_kwh = later_high
        ev_plan = E.plan_ev(ev_slots, params, now)
        if not connected:
            ev_plan.reason = "auto nepřipojeno"
        elif not soc_known:
            ev_plan.reason = "SOC auta neznámý – jen slunce; " + ev_plan.reason
        self.ev_plan = ev_plan

        # rezerva pro baterii domu (priorita 3): když ji slunce do večera samo nenabije,
        # dostane EV jen přebytek nad průměrný výkon potřebný k nabití do 17:00
        soc = self.fnum("sensor.battery_state_of_charge", 50)
        cap = self.fnum("input_number.battery_capacity", 10)
        end = now.replace(hour=17, minute=0, second=0, microsecond=0)
        fills = soc >= 95 or any(r.soc_pct >= 95 for r in plan.results if now <= r.start < end)
        deadline_danger = ev_plan.shortfall_kwh > 0.05
        hours = max(1.0, (end - now).total_seconds() / 3600)
        # „3f z plné baterie“: plán ji do 17:00 dobije a opravená FVE na zbytek dne pokryje
        # doplnění baterie s rezervou 2 kWh (EV dotované z baterie pak nechybí večer)
        pv_rest = self.fnum("sensor.energy_pv_forecast_corrected", 0)
        self.ev_battery_refill = fills and pv_rest >= (100 - soc) / 100 * cap + 2.0 and now < end
        # rezerva podle skutečného SOC, ne podle plánu baterie: ten spotřebu EV nezná, takže
        # „nabije se do poledne“ platí jen bez auta – s rezervou 0 by EV sebralo celý přebytek
        # a baterie by večer chyběla ve VT (kWh pro EV má hodnotu jen NT)
        self.ev_reserve_w = 0.0 if (soc >= 95 or deadline_danger or now >= end) else \
            round(min(5000.0, (100 - soc) / 100 * cap / hours * 1000), 0)

        self.set_state("sensor.ev_plan", state=ev_plan.reason[:250], replace=True, attributes={
            "friendly_name": "HAv2 plán nabíjení EV", "icon": "mdi:calendar-clock",
            "needed_kwh": f"{ev_plan.needed_kwh:.2f}",
            "solar_kwh": f"{ev_plan.solar_kwh:.2f}",
            "nt_kwh": f"{ev_plan.nt_kwh:.2f}",
            "vt_kwh": f"{ev_plan.vt_kwh:.2f}",
            "shortfall_kwh": f"{ev_plan.shortfall_kwh:.2f}",
            "est_grid_cost": f"{ev_plan.est_cost:.2f}",
            "battery_reserve_w": f"{self.ev_reserve_w:.0f}",
            "horizon_end": ev_plan.horizon_end.isoformat() if ev_plan.horizon_end else "",
            "grid_slots_json": json.dumps([[t.isoformat(timespec="minutes"), k] for t, k in ev_plan.grid_slots.items()],
                                          ensure_ascii=False),
            "generated": now.isoformat(),
        })
        self._ev_publish_target(now, lp, ev_plan)
        key = ("shortfall", ev_plan.horizon_end.isoformat() if ev_plan.horizon_end else "")
        if deadline_danger and self.ev_executing() and key not in self.ev_notified:
            self.ev_notified.add(key)
            self.call_service(NOTIFY, title="HAv2 – EV nestihne termín",
                              message=f"{ev_plan.reason}. Zapni „EV nabít za každou cenu“ pro doplnění ve VT.")

    # ------------------------------------------------------------ vstupy
    def ev_sun_returns(self, now: datetime, lowest_kw: float) -> bool:
        pv = [(t, kw) for t, kw in self._pv_slots() if t + timedelta(minutes=30) > now
              and t < now + timedelta(minutes=60)]
        if not pv:
            return False
        base = self.fnum("sensor.base_house_consumption", 500) / 1000
        return sum(kw for _, kw in pv) / len(pv) - base >= lowest_kw

    def ev_inputs(self, now: datetime, available: bool, enabled: bool, amps: int, phases: int) -> E.RegInputs:
        # filtrace má přednost; v doporučení podle virtuálního čerpadla řízení filtrace
        virtual_pool = getattr(self, "pool_virtual", None)
        pool_w = (self.fnum("input_number.filtrace_vykon_w", 500) if virtual_pool else 0.0) \
            if virtual_pool is not None else self.fnum("sensor.bazenova_filtrace_vykon", 0)
        # cíl omezený limitem v autě (nad limit auto nenabije, i kdyby wallbox pouštěl)
        target_reached = self.ev_reached(self.ev_limit_plan(now))
        connected = self.get_state(EV_CONNECTED) == "on"
        mode = self.get_state("input_select.ev_mode") or "Solár+NT"
        manual = self.get_state("input_select.ev_manual") or "Auto"
        plan_slot = self.ev_plan.slot_now(now) if self.ev_plan else None
        surplus_pool = self.fnum("sensor.energy_surplus_smoothed_w", 0) - pool_w
        # prostor pro bojler (WATrouter): jen když EV nabíjí ze slunce a termín nehrozí
        self.ev_boiler.p.enabled = self.get_state("input_boolean.ev_boiler_priority") == "on"
        eligible = (connected and not target_reached and mode in ("Solár", "Solár+NT") and manual == "Auto"
                    and not plan_slot and not (self.ev_plan and self.ev_plan.shortfall_kwh > 0.05))
        self.ev_boiler_reserve_w = self.ev_boiler.step(E.BoilerInputs(
            now=now, eligible=eligible, surplus_w=surplus_pool,
            battery_soc=self.fnum("sensor.battery_state_of_charge", 0),
            heating=self.get_state("binary_sensor.energy_boiler_heating") == "on",
            grid_export_w=self.fnum("sensor.meter_active_power_total", 0),
            boiler_avg_kwh=self.fnum_attr("sensor.energy_boiler_pnd_daily", "avg_kwh_14d"),
            sell_price=self.fnum("sensor.energy_price_sell_now", 99.0)))
        self._ev_log_boiler(now)
        surplus = surplus_pool - self.ev_reserve_w - self.ev_boiler_reserve_w
        lowest = E.ev_power_kw(1 if self.ev_reg.p.allow_1f else 3, self.ev_reg.p.amin)
        return E.RegInputs(
            now=now,
            connected=connected,
            available=available,
            mode=mode,
            manual=manual,
            manual_current=int(self.fnum("input_number.ev_manual_current", 11)),
            manual_phases=self.get_state("input_select.ev_manual_phases") or "Auto",
            target_reached=target_reached,
            surplus_w=surplus,
            battery_soc=self.fnum("sensor.battery_state_of_charge", 0),
            sun_returns=self.ev_sun_returns(now, lowest),
            plan_slot=plan_slot,
            worst_phase_a=25 - self.fnum("sensor.energy_breaker_headroom_a", 25),
            boiler_heating=self.get_state("binary_sensor.energy_boiler_heating") == "on",
            cur_enabled=enabled, cur_amps=amps, cur_phases=phases,
            sell_price=self.fnum("sensor.energy_price_sell_now", 99.0),
            battery_refill=getattr(self, "ev_battery_refill", False),
            boiler_reserve_w=self.ev_boiler_reserve_w,
        )

    # ------------------------------------------------------------ smyčka
    def ev_loop(self, kwargs: Dict[str, Any]) -> None:
        try:
            self._ev_loop()
        except Exception as err:  # noqa: BLE001
            self.log(f"EV smyčka selhala: {err}", level="ERROR")

    def _ev_loop(self) -> None:
        now = datetime.now(self.ev_tz)
        self.ev_reg.p = self.ev_params()
        execute = self.ev_executing()
        available, enabled, amps, phases = self.ev_wallbox()
        self._ev_manual_expiry(now)
        self._ev_availability(now, available, execute)

        if execute:
            self._ev_detect_override(now, available, enabled, amps, phases)
        elif self.ev_virtual:
            # doporučení: regulátor pracuje nad vlastním posledním povelem
            enabled, amps, phases = self.ev_virtual.enable, self.ev_virtual.amps, self.ev_virtual.phases
        inp = self.ev_inputs(now, available, enabled, amps, phases)

        cmd = self.ev_reg.breaker(inp) or self.ev_reg.step(inp)
        self.ev_virtual = cmd
        overridden = bool(self.ev_override_until and now < self.ev_override_until)
        acting = execute and not overridden and available

        self._ev_publish(now, cmd, inp, execute, overridden)
        if acting and (cmd.enable, cmd.amps, cmd.phases) != (enabled, amps, phases) and inp.connected:
            self.call_service("script/hav2_ev_set", enable=cmd.enable, current=cmd.amps,
                              phases=str(cmd.phases), reason=cmd.reason[:200])
            self.ev_written = (cmd, now)

    def _ev_publish(self, now: datetime, cmd: E.Command, inp: E.RegInputs, execute: bool, overridden: bool) -> None:
        key = (cmd.enable, cmd.amps, cmd.phases, cmd.state, cmd.reason, execute, overridden, self.ev_boiler.reason)
        if key != self.ev_published:
            self.ev_published = key
            self.set_state("sensor.ev_regulator", state=cmd.state, replace=True, attributes={
                "friendly_name": "HAv2 regulátor EV", "icon": "mdi:ev-station",
                "enable": "ano" if cmd.enable else "ne",
                "current_a": str(cmd.amps),
                "phases": f"{cmd.phases}f",
                "power_kw": f"{E.ev_power_kw(cmd.phases, cmd.amps) if cmd.enable else 0:.2f}",
                "reason": cmd.reason,
                "surplus_for_ev_w": f"{inp.surplus_w:.0f}",
                "battery_reserve_w": f"{self.ev_reserve_w:.0f}",
                "boiler_reserve_w": f"{self.ev_boiler_reserve_w:.0f}",
                "boiler_gate": self.ev_boiler.reason,
                "worst_phase_a": f"{inp.worst_phase_a:.1f}",
                "sun_returns": "ano" if inp.sun_returns else "ne",
                "plan_slot": inp.plan_slot or "",
                "executing": "ano" if execute else "ne",
                "override_until": self.ev_override_until.isoformat() if overridden else "",
                "updated": now.isoformat(),
            })
        # do logbooku jen změna povelu, ne každá změna přebytku v důvodu
        decision = (cmd.enable, cmd.amps, cmd.phases, cmd.state)
        if decision != self.ev_logged:
            self.ev_logged = decision
            prefix = "" if execute else "[doporučení] "
            what = f"{cmd.amps} A {cmd.phases}f" if cmd.enable else "stop"
            text = f"{now:%H:%M} {prefix}{cmd.state}: {what} – {cmd.reason}"
            self.call_service("input_text/set_value", entity_id="input_text.ev_last_decision", value=text[:255])
            self.call_service("logbook/log", name="HAv2 EV", message=text[:500])
            self.log(text)

    def _ev_log_boiler(self, now: datetime) -> None:
        """Do logbooku jen začátek/konec rezervy pro bojler a „nahřátý“."""
        state = (self.ev_boiler_reserve_w > 0, self.ev_boiler.done)
        if self.ev_boiler_logged is None or state == self.ev_boiler_logged:
            self.ev_boiler_logged = state
            return
        self.ev_boiler_logged = state
        if self.ev_boiler.done:
            self.call_service("input_datetime/set_datetime", entity_id=BOILER_LAST_FULL,
                              timestamp=int(now.timestamp()))
        text = f"{now:%H:%M} bojler: {self.ev_boiler.reason}"
        self.call_service("logbook/log", name="HAv2 EV", message=text)
        self.log(text)

    def fnum_attr(self, entity: str, attr: str) -> Optional[float]:
        try:
            return float(self.get_state(entity, attribute=attr))
        except (TypeError, ValueError):
            return None

    def ev_on_boiler_full(self, entity, attribute, old, new, kwargs) -> None:
        self._ev_boiler_sync(new)

    def _ev_boiler_sync(self, value: Any) -> None:
        """Pomocník s dnešním datem = bojler dnes nahřátý; jiné datum (ruční změna) = zkusit znovu."""
        today = datetime.now(self.ev_tz).date().isoformat()
        g = self.ev_boiler
        if str(value or "")[:10] == today:
            g.day, g.done, g.reason = today, True, "bojler dnes nahřátý"
        elif g.done and g.day == today:
            g.done = False

    # ------------------------------------------------ ruční zásah / override
    def _ev_detect_override(self, now: datetime, available: bool, enabled: bool, amps: int, phases: int) -> None:
        if not available or self.get_state(EV_CONNECTED) != "on" or not self.ev_written:
            return
        cmd, at = self.ev_written
        if (now - at).total_seconds() < OVERRIDE_AFTER_S:
            return
        mine = (cmd.enable, cmd.amps if cmd.enable else amps, cmd.phases if cmd.enable else phases)
        if (enabled, amps, phases) != mine and not (self.ev_override_until and now < self.ev_override_until):
            self.ev_override_until = now + OVERRIDE_FOR
            self.ev_written = None
            self.call_service("input_datetime/set_datetime", entity_id="input_datetime.energy_override_until",
                              timestamp=int(self.ev_override_until.timestamp()))
            self.call_service("logbook/log", name="HAv2 EV",
                              message=f"Ruční zásah na wallboxu – HAv2 nezasahuje do {self.ev_override_until:%H:%M}")

    def ev_on_manual(self, entity, attribute, old, new, kwargs) -> None:
        self.ev_manual_since = datetime.now(self.ev_tz) if new != "Auto" else None
        self.ev_override_until = None  # volba na dashboardu ruší ruční zásah na wallboxu

    def _ev_manual_expiry(self, now: datetime) -> None:
        manual = self.get_state("input_select.ev_manual")
        if manual in (None, "Auto"):
            return
        until = self.get_state("input_select.ev_manual_until")
        since = self.ev_manual_since or now
        self.ev_manual_since = since
        expired = (
            (until == "Do odpojení" and self.get_state(EV_CONNECTED) == "off")
            or (until == "Do cílového SOC" and self._ev_target_reached())
            or (until == "Na 1 h" and now - since >= timedelta(hours=1))
            or (until == "Na 3 h" and now - since >= timedelta(hours=3))
        )
        if expired:
            self.call_service("input_select/select_option", entity_id="input_select.ev_manual", option="Auto")
            self.call_service("logbook/log", name="HAv2 EV", message=f"Ruční režim „{manual}“ skončil ({until})")
            self.ev_manual_since = None

    def _ev_target_reached(self) -> bool:
        return self.ev_reached(self.ev_limit_plan(datetime.now(self.ev_tz)))

    # ------------------------------------------------ připojení a výpadky
    def ev_on_connect(self, entity, attribute, old, new, kwargs) -> None:
        if new == "on" and old == "off":
            self.ev_reg = E.Regulator(p=self.ev_params())
            self.ev_virtual = None
            self.ev_override_until = None
            day = datetime.now(self.ev_tz).date().isoformat()
            if self.ev_executing() and self.ev_force_updates.get(day, 0) < FORCE_UPDATE_PER_DAY:
                self.ev_force_updates = {day: self.ev_force_updates.get(day, 0) + 1}
                self.call_service("kia_uvo/force_update")
            self.run_in(self.tick, 60, reason="připojeno EV")
        elif new == "off" and old == "on":
            # jednorázový cíl odpojení neruší (krátká jízda před cestou) – končí nabitím nebo termínem
            self.run_in(self.tick, 5, reason="odpojeno EV")

    def _ev_availability(self, now: datetime, available: bool, execute: bool) -> None:
        if available:
            self.ev_unavailable_since = None
            self.ev_notified.discard("unavailable")
            return
        self.ev_unavailable_since = self.ev_unavailable_since or now
        if execute and now - self.ev_unavailable_since > timedelta(minutes=5) and "unavailable" not in self.ev_notified:
            self.ev_notified.add("unavailable")
            self.call_service(NOTIFY, title="HAv2 – EcoVolter nedostupný",
                              message="Wallbox je nedostupný déle než 5 min, regulace EV stojí.")
