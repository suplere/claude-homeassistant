"""HAv2 – EV: napojení plánovače a regulátoru (hav2_ev) na Home Assistant.

Mixin pro aplikaci Hav2 (hav2_app). Publikuje:
  sensor.ev_plan       – rozdělení potřebné energie (slunce / NT / VT) a síťové sloty
  sensor.ev_regulator  – stav regulátoru, doporučený proud a fáze, důvod
  input_text.ev_last_decision + logbook – změny rozhodnutí
Do EcoVolteru zapisuje JEN přes script.hav2_ev_set, a to jen když
input_select.energy_system_mode == "Auto" a input_boolean.ev_control == on.
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

OVERRIDE_AFTER_S = 90  # rozdíl proti vlastnímu povelu starší než tohle = ruční zásah
OVERRIDE_FOR = timedelta(hours=2)
FORCE_UPDATE_PER_DAY = 2


class EvControl:
    """Předpokládá hass.Hass (self.get_state, call_service, …) a TZ/fnum z Hav2."""

    def ev_init(self, tz) -> None:
        self.ev_tz = tz
        self.ev_reg = E.Regulator()
        self.ev_plan: Optional[E.EvPlan] = None
        self.ev_reserve_w = 0.0
        self.ev_virtual: Optional[E.Command] = None
        self.ev_written: Optional[Tuple[E.Command, datetime]] = None
        self.ev_override_until: Optional[datetime] = None
        self.ev_published: Optional[tuple] = None
        self.ev_logged: Optional[tuple] = None
        self.ev_manual_since: Optional[datetime] = None
        self.ev_force_updates: Dict[str, int] = {}
        self.ev_unavailable_since: Optional[datetime] = None
        self.ev_notified: set = set()

        self.run_every(self.ev_loop, "now+40", 5)
        self.listen_state(self.ev_on_connect, EV_CONNECTED)
        self.listen_state(self.ev_on_manual, "input_select.ev_manual")

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

    # ------------------------------------------------------------- plán
    def ev_replan(self, now: datetime, slots, plan, is_nt) -> None:
        """Volá se z plánovače baterie: přebytek pro EV = přetok v plánu baterie."""
        ev_slots = [E.EvSlot(s.start, s.is_nt, r.grid_export_kwh, s.fraction)
                    for s, r in zip(slots, plan.results)]
        needed_raw = self.get_state("sensor.ev_energy_needed_kwh")
        try:
            needed = float(needed_raw)
            soc_known = True
        except (TypeError, ValueError):
            needed, soc_known = 0.0, False
        connected = self.get_state(EV_CONNECTED) == "on"
        params = E.EvPlanParams(
            needed_kwh=needed if connected else 0.0,
            mode=self.get_state("input_select.ev_mode") or "Solár+NT",
            deadline=self.ev_deadline(now),
            deadline_hard=self.get_state("input_boolean.ev_deadline_hard") == "on",
            nt_price=self.fnum("input_number.energy_price_nt", 3.51),
            vt_price=self.fnum("input_number.energy_price_vt", 6.1),
        )
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
        self.ev_reserve_w = 0.0 if (fills or deadline_danger or now >= end) else \
            round(min(5000.0, (100 - soc) / 100 * cap / hours * 1000), 0)

        self.set_state("sensor.ev_plan", state=ev_plan.reason[:250], attributes={
            "friendly_name": "HAv2 plán nabíjení EV", "icon": "mdi:calendar-clock",
            "needed_kwh": f"{ev_plan.needed_kwh:.2f}",
            "solar_kwh": f"{ev_plan.solar_kwh:.2f}",
            "nt_kwh": f"{ev_plan.nt_kwh:.2f}",
            "vt_kwh": f"{ev_plan.vt_kwh:.2f}",
            "shortfall_kwh": f"{ev_plan.shortfall_kwh:.2f}",
            "est_grid_cost": f"{ev_plan.est_cost:.2f}",
            "battery_reserve_w": f"{self.ev_reserve_w:.0f}",
            "horizon_end": ev_plan.horizon_end.isoformat() if ev_plan.horizon_end else "",
            "grid_slots_json": json.dumps([[t.strftime("%d.%m %H:%M"), k] for t, k in ev_plan.grid_slots.items()],
                                          ensure_ascii=False),
            "generated": now.isoformat(),
        })
        key = ("shortfall", ev_plan.horizon_end.isoformat() if ev_plan.horizon_end else "")
        if deadline_danger and self.ev_executing() and key not in self.ev_notified:
            self.ev_notified.add(key)
            self.call_service("notify/mobile_app_evzen_iphone", title="HAv2 – EV nestihne termín",
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
        pool_w = self.fnum("sensor.bazenova_filtrace_vykon", 0)
        surplus = self.fnum("sensor.energy_surplus_smoothed_w", 0) - pool_w - self.ev_reserve_w
        needed = self.get_state("sensor.ev_energy_needed_kwh")
        try:
            target_reached = float(needed) <= 0.05
        except (TypeError, ValueError):
            target_reached = False
        lowest = E.ev_power_kw(1 if self.ev_reg.p.allow_1f else 3, self.ev_reg.p.amin)
        return E.RegInputs(
            now=now,
            connected=self.get_state(EV_CONNECTED) == "on",
            available=available,
            mode=self.get_state("input_select.ev_mode") or "Solár+NT",
            manual=self.get_state("input_select.ev_manual") or "Auto",
            manual_current=int(self.fnum("input_number.ev_manual_current", 11)),
            manual_phases=self.get_state("input_select.ev_manual_phases") or "Auto",
            target_reached=target_reached,
            surplus_w=surplus,
            battery_soc=self.fnum("sensor.battery_state_of_charge", 0),
            sun_returns=self.ev_sun_returns(now, lowest),
            plan_slot=self.ev_plan.slot_now(now) if self.ev_plan else None,
            worst_phase_a=25 - self.fnum("sensor.energy_breaker_headroom_a", 25),
            boiler_heating=self.get_state("binary_sensor.energy_boiler_heating") == "on",
            cur_enabled=enabled, cur_amps=amps, cur_phases=phases,
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
        key = (cmd.enable, cmd.amps, cmd.phases, cmd.state, cmd.reason, execute, overridden)
        if key != self.ev_published:
            self.ev_published = key
            self.set_state("sensor.ev_regulator", state=cmd.state, attributes={
                "friendly_name": "HAv2 regulátor EV", "icon": "mdi:ev-station",
                "enable": "ano" if cmd.enable else "ne",
                "current_a": str(cmd.amps),
                "phases": f"{cmd.phases}f",
                "power_kw": f"{E.ev_power_kw(cmd.phases, cmd.amps) if cmd.enable else 0:.2f}",
                "reason": cmd.reason,
                "surplus_for_ev_w": f"{inp.surplus_w:.0f}",
                "battery_reserve_w": f"{self.ev_reserve_w:.0f}",
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
        try:
            return float(self.get_state("sensor.ev_energy_needed_kwh")) <= 0.05
        except (TypeError, ValueError):
            return False

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
            self.run_in(self.tick, 5, reason="odpojeno EV")

    def _ev_availability(self, now: datetime, available: bool, execute: bool) -> None:
        if available:
            self.ev_unavailable_since = None
            self.ev_notified.discard("unavailable")
            return
        self.ev_unavailable_since = self.ev_unavailable_since or now
        if execute and now - self.ev_unavailable_since > timedelta(minutes=5) and "unavailable" not in self.ev_notified:
            self.ev_notified.add("unavailable")
            self.call_service("notify/mobile_app_evzen_iphone", title="HAv2 – EcoVolter nedostupný",
                              message="Wallbox je nedostupný déle než 5 min, regulace EV stojí.")
