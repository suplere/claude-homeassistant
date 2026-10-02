"""HAv2 – filtrace bazénu: napojení řízení (hav2_pool) na Home Assistant.

Mixin pro aplikaci Hav2 (hav2_app). Publikuje:
  sensor.pool_plan            – cíl a odběhnuté hodiny, očekávané hodiny ze slunce a v NT
  sensor.pool_controller      – stav řízení, doporučení zapnout/vypnout, důvod
  input_text.pool_last_decision + logbook – změny rozhodnutí
Skutečný běh čerpadla (binary_sensor.pool_pump_running, měření Pro EM-50) se porovnává s relé:
nesoulad = venkovní vypínač (VYPNUTO, nebo MANUAL + ZAPNUTO) → logbook + notifikace.
Čerpadlo spíná JEN přes script.hav2_pool_set, a to jen když
input_select.energy_system_mode == "Auto" a input_boolean.pool_control == on.
V režimu „Jen doporučení“ řízení běží nad virtuálním čerpadlem (poslední vlastní povel).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

import hav2_pool as B

PUMP = "switch.bazen_filtrace_rele"  # relé Pro EM-50 → cívka stykače
PUMP_RUNNING = "binary_sensor.pool_pump_running"  # skutečný běh podle měření
PUMP_POWER = "sensor.bazen_cerpadlo_vykon"
MISMATCH_AFTER = timedelta(minutes=3)
NOTIFY = "notify/mobile_app_evzen_iphone"
OVERRIDE_AFTER_S = 90
OVERRIDE_FOR = timedelta(hours=2)


class PoolControl:
    """Předpokládá hass.Hass a fnum/_nt_fn z Hav2."""

    def pool_init(self, tz) -> None:
        self.pool_tz = tz
        self.pool_ctl = B.PoolController()
        self.pool_virtual: Optional[bool] = None
        self.pool_written: Optional[Tuple[bool, datetime]] = None
        self.pool_override_until: Optional[datetime] = None
        self.pool_manual_since: Optional[datetime] = None
        self.pool_published: Optional[tuple] = None
        self.pool_logged: Optional[tuple] = None
        self.pool_solar_hours_left = 0.0
        self.pool_mismatch: str = ""  # "" | "vypnuto vypínačem" | "běží ručně"
        self.pool_mismatch_since: Optional[datetime] = None
        self.pool_mismatch_kind: str = ""
        self.pool_is_nt = lambda ts: ts.hour >= 22 or ts.hour < 6

        self.run_every(self.pool_loop, "now+45", 60)
        self.listen_state(self.pool_on_manual, "input_select.pool_manual")

    # ----------------------------------------------------------- pomocné
    def pool_executing(self) -> bool:
        return (self.get_state("input_select.energy_system_mode") == "Auto"
                and self.get_state("input_boolean.pool_control") == "on")

    def pool_target(self) -> float:
        rec_raw = self.get_state("sensor.pool_hours_recommended")
        try:
            rec: Optional[float] = float(rec_raw)
        except (TypeError, ValueError):
            rec = None
        return B.target_hours(self.fnum("input_number.pool_hours_required", 6), rec,
                              self.get_state("input_boolean.pool_use_recommendation") == "on")

    # ------------------------------------------------------------- plán
    def pool_replan(self, now: datetime, slots, is_nt) -> None:
        """Volá se z plánovače baterie: odhad hodin ze slunce do konce bazénového dne."""
        self.pool_is_nt = is_nt
        day_end = B.pool_day_start(now) + timedelta(days=1)
        start_kw = self.pool_ctl.p.start_surplus_w / 1000
        hours = 0.0
        for s in slots:
            if s.start >= day_end or s.is_nt:
                continue
            surplus_kw = (s.pv_kwh - s.load_kwh - s.grid_only_kwh) / (0.25 * s.fraction)
            if surplus_kw >= start_kw:
                hours += 0.25 * s.fraction
        self.pool_solar_hours_left = round(hours, 2)

    # ------------------------------------------------------------ smyčka
    def pool_loop(self, kwargs: Dict[str, Any]) -> None:
        try:
            self._pool_loop()
        except Exception as err:  # noqa: BLE001
            self.log(f"smyčka filtrace selhala: {err}", level="ERROR")

    def _pool_loop(self) -> None:
        now = datetime.now(self.pool_tz)
        self.pool_ctl.p = B.PoolParams(min_run_min=self.fnum("input_number.pool_min_run_min", 60))
        execute = self.pool_executing()
        state = self.get_state(PUMP)
        available = state in ("on", "off")
        running = state == "on"
        self._pool_manual_expiry(now)
        self._pool_check_real(now, available, running)
        if execute:
            self._pool_detect_override(now, available, running)
        elif self.pool_virtual is not None:
            running = self.pool_virtual

        target = self.pool_target()
        done = self.fnum("sensor.pool_hours_done", 0)
        inp = B.PoolInputs(
            now=now,
            season=self.get_state("input_boolean.pool_season") == "on",
            manual=self.get_state("input_select.pool_manual") or "Auto",
            hours_done=done,
            target_h=target,
            surplus_w=self.fnum("sensor.energy_surplus_smoothed_w", 0),
            is_nt=self.pool_is_nt(now),
            running=running,
            running_since=self._pool_running_since() if execute and running else None,
        )
        cmd = self.pool_ctl.step(inp)
        self.pool_virtual = cmd.on
        overridden = bool(self.pool_override_until and now < self.pool_override_until)
        self._pool_publish(now, cmd, inp, execute, overridden)
        if execute and available and not overridden and cmd.on != (state == "on"):
            self.call_service("script/hav2_pool_set", on=cmd.on, reason=cmd.reason[:200])
            self.pool_written = (cmd.on, now)

    def _pool_running_since(self) -> Optional[datetime]:
        raw = (self.get_state(PUMP, attribute="all") or {}).get("last_changed")
        try:
            return datetime.fromisoformat(str(raw)).astimezone(self.pool_tz)
        except (TypeError, ValueError):
            return None

    def _pool_publish(self, now: datetime, cmd: B.PoolCommand, inp: B.PoolInputs,
                      execute: bool, overridden: bool) -> None:
        nt_h = B.expected_nt_hours(inp.target_h, inp.hours_done, self.pool_solar_hours_left)
        plan_text = (f"{inp.hours_done:.1f}/{inp.target_h:.1f} h, slunce ~{self.pool_solar_hours_left:.1f} h, "
                     f"NT ~{nt_h:.1f} h")
        self.set_state("sensor.pool_plan", state=plan_text, replace=True, attributes={
            "friendly_name": "HAv2 plán filtrace", "icon": "mdi:calendar-clock",
            "target_h": f"{inp.target_h:.1f}",
            "done_h": f"{inp.hours_done:.2f}",
            "solar_hours_left": f"{self.pool_solar_hours_left:.2f}",
            "nt_hours_expected": f"{nt_h:.2f}",
            "day_start": B.pool_day_start(now).isoformat(),
        })
        key = (cmd.on, cmd.state, cmd.reason, execute, overridden, self.pool_mismatch)
        if key != self.pool_published:
            self.pool_published = key
            self.set_state("sensor.pool_controller", state=cmd.state, replace=True, attributes={
                "friendly_name": "HAv2 řízení filtrace", "icon": "mdi:pool",
                "pump_on": "ano" if cmd.on else "ne",
                "reason": cmd.reason,
                "surplus_w": f"{inp.surplus_w:.0f}",
                "solar_starts_today": str(self.pool_ctl.solar_starts),
                "executing": "ano" if execute else "ne",
                "override_until": self.pool_override_until.isoformat() if overridden else "",
                "pump_running_real": "ano" if self.get_state(PUMP_RUNNING) == "on" else "ne",
                "switch_mismatch": self.pool_mismatch,
                "updated": now.isoformat(),
            })
        decision = (cmd.on, cmd.state)
        if decision != self.pool_logged:
            self.pool_logged = decision
            prefix = "" if execute else "[doporučení] "
            text = f"{now:%H:%M} {prefix}{'zapnout' if cmd.on else 'vypnout'} ({cmd.state}) – {cmd.reason}"
            self.call_service("input_text/set_value", entity_id="input_text.pool_last_decision", value=text[:255])
            self.call_service("logbook/log", name="HAv2 bazén", message=text[:500])
            self.log(text)

    # ------------------------------------------- relé × skutečný běh (venkovní vypínače)
    def _pool_check_real(self, now: datetime, available: bool, relay_on: bool) -> None:
        real = self.get_state(PUMP_RUNNING)
        if not available or real not in ("on", "off") or self.get_state(PUMP_POWER) in (None, "unknown", "unavailable"):
            return
        kind = ("vypnuto vypínačem" if relay_on and real == "off"
                else "běží ručně" if not relay_on and real == "on" else "")
        if kind != self.pool_mismatch_kind:
            self.pool_mismatch_kind, self.pool_mismatch_since = kind, now
        if kind and not self.pool_mismatch and now - self.pool_mismatch_since >= MISMATCH_AFTER:
            self.pool_mismatch = kind
            text = ("Relé filtrace je sepnuté, ale čerpadlo neběží – venku je nejspíš VYPNUTO."
                    if relay_on else
                    "Čerpadlo běží, i když ho HAv2 nesepnulo – venku je nejspíš MANUAL + ZAPNUTO.")
            self.call_service("logbook/log", name="HAv2 bazén", message=text)
            self.call_service(NOTIFY, title="Filtrace: " + kind, message=text + " Hodiny se počítají podle skutečného běhu.")
            self.log(text, level="WARNING")
        elif not kind and self.pool_mismatch:
            self.call_service("logbook/log", name="HAv2 bazén",
                              message=f"Filtrace: relé a skutečný běh zase souhlasí (konec „{self.pool_mismatch}“)")
            self.pool_mismatch = ""

    # ------------------------------------------------ ruční zásah / override
    def _pool_detect_override(self, now: datetime, available: bool, running: bool) -> None:
        if not available or not self.pool_written:
            return
        mine, at = self.pool_written
        if (now - at).total_seconds() < OVERRIDE_AFTER_S:
            return
        if running != mine and not (self.pool_override_until and now < self.pool_override_until):
            self.pool_override_until = now + OVERRIDE_FOR
            self.pool_written = None
            self.call_service("input_datetime/set_datetime", entity_id="input_datetime.energy_override_until",
                              timestamp=int(self.pool_override_until.timestamp()))
            self.call_service("logbook/log", name="HAv2 bazén",
                              message=f"Ruční zásah na filtraci – HAv2 nezasahuje do {self.pool_override_until:%H:%M}")

    def pool_on_manual(self, entity, attribute, old, new, kwargs) -> None:
        self.pool_manual_since = datetime.now(self.pool_tz) if new != "Auto" else None
        self.pool_override_until = None
        self.run_in(self.pool_loop, 2)

    def _pool_manual_expiry(self, now: datetime) -> None:
        manual = self.get_state("input_select.pool_manual")
        if manual in (None, "Auto"):
            return
        self.pool_manual_since = self.pool_manual_since or now
        hours = self.fnum("input_number.pool_manual_duration_h", 2)
        if now - self.pool_manual_since >= timedelta(hours=hours):
            self.call_service("input_select/select_option", entity_id="input_select.pool_manual", option="Auto")
            self.call_service("logbook/log", name="HAv2 bazén",
                              message=f"Ruční režim filtrace „{manual}“ skončil po {hours:g} h")
            self.pool_manual_since = None
