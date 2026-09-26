"""HAv2 – bojler (WATrouter) z fakturačních dat PND (čistý Python, bez AppDaemonu).

Bojler je zapojený mimo měření GoodWe, takže ho HA vidí jen jako rozdíl proti PND:
  navíc_h = (nákup PND − nákup HA) + (prodej HA − prodej PND)   [hodinově]
Hodina s navíc > THRESHOLD kWh je ohřev bojleru (šum z rozlišení GoodWe 0,1 kWh je menší).
Zbytek rozdílu je skutečná odchylka měření HA proti PND (kontrola fakturace).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable, Dict, List, Optional, Tuple

THRESHOLD_KWH = 0.25


@dataclass
class BoilerDay:
    day: date
    boiler_kwh: float = 0.0  # celkem
    import_kwh: float = 0.0  # z toho nákup ze sítě
    export_loss_kwh: float = 0.0  # z toho přetok, který jinak šel do sítě
    cost_kc: float = 0.0  # nákup × cena NT/VT (ušlý prodej se nezapočítává)
    hours: List[Tuple[int, float]] = field(default_factory=list)
    pnd_import: float = 0.0
    pnd_export: float = 0.0
    ha_import: float = 0.0
    ha_export: float = 0.0

    @property
    def residual_import_kwh(self) -> float:
        return self.pnd_import - self.ha_import - self.import_kwh

    @property
    def residual_export_kwh(self) -> float:
        return self.pnd_export - (self.ha_export - self.export_loss_kwh)

    def residual_pct(self, which: str) -> Optional[float]:
        base = self.pnd_import if which == "import" else self.pnd_export
        res = self.residual_import_kwh if which == "import" else self.residual_export_kwh
        return round(res / base * 100, 1) if base > 0.05 else None


def boiler_days(
    pnd_imp: Dict[datetime, float],
    pnd_exp: Dict[datetime, float],
    ha_imp: Dict[datetime, float],
    ha_exp: Dict[datetime, float],
    is_nt: Callable[[datetime], bool],
    nt_price: float,
    vt_price: float,
    threshold: float = THRESHOLD_KWH,
) -> List[BoilerDay]:
    """Hodinová data {začátek hodiny (lokální čas): kWh} → vyhodnocení po dnech (jen úplné hodiny ve všech 4 zdrojích)."""
    days: Dict[date, BoilerDay] = {}
    for ts in sorted(pnd_imp):
        if ts not in pnd_exp or ts not in ha_imp or ts not in ha_exp:
            continue
        d = days.setdefault(ts.date(), BoilerDay(ts.date()))
        pi, pe, hi, he = pnd_imp[ts], pnd_exp[ts], ha_imp[ts], ha_exp[ts]
        d.pnd_import += pi
        d.pnd_export += pe
        d.ha_import += hi
        d.ha_export += he
        imp_part = max(0.0, pi - hi)
        exp_part = max(0.0, he - pe)
        if imp_part + exp_part > threshold:
            d.boiler_kwh += imp_part + exp_part
            d.import_kwh += imp_part
            d.export_loss_kwh += exp_part
            d.cost_kc += imp_part * (nt_price if is_nt(ts) else vt_price)
            d.hours.append((ts.hour, round(imp_part + exp_part, 2)))
    return [days[k] for k in sorted(days)]
