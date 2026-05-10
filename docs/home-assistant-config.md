# Home Assistant — dokumentace konfigurace

## Přehled systému

Fotovoltaická elektrárna s baterií, řízená přes GoodWe střídač. Systém optimalizuje spotřebu a přetoky na základě aktuálních spotových cen elektřiny (OTE) a predikce výroby (Solcast).

**Klíčové integrace:**
- **GoodWe** — střídač, baterie, řízení výkonu a režimů
- **Solcast** — predikce výroby FVE (API, aktualizace přes den)
- **Spot electricity** — aktuální hodinové ceny OTE (CZK/kWh)
- **EcoVolter Pro II** — nabíjení EV (`sensor.ecovolter_revcr01c00002056_*`)
- **Shelly Pro 1PM** — spínač a měřič bazénového čerpadla (DIN lišta)
- **Shelly plug** — zásuvka u filtrace (`zasuvka_filtrace`), řízená zásuvka (`zasuvka_rizena`)

---

## Klíčové entity

### Spotřeba

| Entita | Popis |
|--------|-------|
| `sensor.house_consumption` | Celková spotřeba domu (GoodWe, W) |
| `sensor.base_house_consumption` | Spotřeba bez filtrace a EV (W) |
| `sensor.base_nighttime_consumption` | Základní spotřeba v noci 20:00–09:00 (W) |
| `sensor.final_daily_house_consumption` | Denní snapshot spotřeby ve 23:59:55 (kWh) |
| `sensor.base_final_daily_house_consumption` | Denní snapshot mínus filtrace a EV (kWh) |

### Výroba a baterie

| Entita | Popis |
|--------|-------|
| `sensor.pv_power` | Aktuální výkon FVE (W) |
| `sensor.battery_state_of_charge` | Stav nabití baterie (%) |
| `sensor.solcast_pv_forecast_forecast_today` | Predikce výroby dnes (kWh) |
| `sensor.solcast_pv_forecast_forecast_tomorrow` | Predikce výroby zítra (kWh) |
| `sensor.solcast_pv_forecast_forecast_remaining_today` | Zbývající predikce dnes (kWh) |

### Ceny elektřiny

| Entita | Popis |
|--------|-------|
| `sensor.current_spot_electricity_price` | Aktuální spotová cena (CZK/kWh) |
| `sensor.current_spot_electricity_hour_order` | Pořadí hodiny dle ceny (dict) |
| `sensor.spot_most_expensive_electricity_today` | Nejvyšší cena dneška |
| `binary_sensor.spot_electricity_is_cheapest_3_hours_block` | Právě probíhá nejlevnější 3h blok |

### Bazénová filtrace

| Entita | Popis |
|--------|-------|
| `switch.filtrace_switch` | Spínač čerpadla (Shelly Pro 1PM) |
| `sensor.filtrace_vykon` | Reálný změřený výkon čerpadla (W, Shelly Pro 1PM) |
| `sensor.filtrace_energie` | Celková spotřeba energie čerpadla (kWh, Shelly Pro 1PM) |
| `sensor.filtrace_spustena` | Počet hodin filtrace dnes (history_stats) |
| `sensor.bazenova_filtrace_vykon` | Výkon filtrace pro výpočty (W, 0 nebo `filtrace_vykon_w`) |
| `input_number.filtrace_vykon_w` | Nastavitelný příkon čerpadla (výchozí 450 W) |

**Shelly Pro 1PM** (`shellypro1pm_2cbcbba45344`) — montáž na DIN lištu, ovládá obvod čerpadla.
Entita `switch.filtrace_switch` je primární přepínač používaný ve všech automatizacích.

### Zásuvky (Shelly plug)

| Entita | Popis |
|--------|-------|
| `switch.zasuvka_rizena` | Řízená zásuvka — aktuálně bez funkce |
| `sensor.zasuvka_rizena_power` | Výkon řízené zásuvky (W) |
| `sensor.zasuvka_rizena_energy` | Spotřeba řízené zásuvky (kWh) |

**Zásuvka filtrace** (`zasuvka_filtrace`) — Shelly plug fyzicky umístěný u bazénu, aktuálně bez automatizace.  
**Zásuvka řízená** (`zasuvka_rizena`) — Shelly plug připravený k budoucímu použití, aktuálně bez řízení.

### EV — EcoVolter Pro II

| Entita | Popis |
|--------|-------|
| `sensor.ecovolter_revcr01c00002056_actual_power` | Okamžitý výkon nabíjení (kW) |
| `sensor.ecovolter_revcr01c00002056_charged_energy` | Celková energie od uvedení (kWh, total_increasing) |
| `sensor.eco_volter_vykon` | Wrapper kW→W pro grafy (W) |
| `sensor.ecovolter_revcr01c00002056_eco_volter_daily_energy` | Denní spotřeba EV (kWh, GUI utility meter) |

### Průměry (statistics senzory, sensor.yaml)

| Entita | Popis |
|--------|-------|
| `sensor.base_average_daily_consumption` | Průměr denní spotřeby za 7 dní (kWh) |
| `sensor.base_average_consumption_in_last_20_minutes` | Průměr spotřeby za 20 min (W) |
| `sensor.base_average_night_consumption_in_last_3_days` | Průměr noční spotřeby za 3 dny (W) |
| `sensor.average_pv_production_in_last_20_minutes` | Průměr výroby FVE za 20 min (W) |
| `sensor.count_of_100_battery_state_in_a_week` | Počet dosažení 100% SoC za 7 dní |

### GoodWe řízení

| Entita | Popis |
|--------|-------|
| `select.goodwe_provozni_rezim_stridace` | Provozní režim (General / Eco / Off-grid) |
| `number.goodwe_vykon_v_ekonomickem_rezimu` | Výkon v Eco režimu (W) |
| `number.goodwe_maximum_vybiti_v_siti` | Maximální vybití baterie — DoD (%) |
| `number.goodwe_stav_nabiti_baterie_ekonomickem_rezimu` | Cílový SoC v Eco režimu (%) |
| `number.goodwe_limit_dodavky_do_site` | Limit dodávky do sítě (W nebo %) |

---

## Helpery

### input_number

| Entita | Popis | Výchozí |
|--------|-------|---------|
| `input_number.battery_capacity` | Kapacita baterie (kWh) | — |
| `input_number.winter_dod` | Zimní DoD hodnota (%) | — |
| `input_number.filtrace_vykon_w` | Příkon bazénového čerpadla (W) | 450 W |

### input_boolean

| Entita | Popis |
|--------|-------|
| `input_boolean.time_to_use_overflows` | Manuální/automatický příznak pro zapnutí filtrace při přetocích |

### input_select

| Entita | Popis |
|--------|-------|
| `input_select.season` | Aktuální sezóna (Summer / Spring-Autumn / Winter) |

---

## Automatizace

### FVE: solcast updates
Aktualizuje predikci Solcast přes API. Spouští se v pevných časech od 06:30 do 18:30 (10× denně).

### Přetoky nastavit
Při záporné spotové ceně: zapne export do sítě (script), omezí výkon FVE na 200 W, notifikace.
Při kladné ceně: obnoví plný export.
Trigger: změna `sensor.current_spot_electricity_hour_order`.

### Ovládání filtrace - přetoky
Každých 5 minut (09:00–18:00) kontroluje přetoky na fázi L2.
- Přetoky > 400 W nebo `time_to_use_overflows = on` → filtrace zapnuta
- Jinak → filtrace vypnuta

### Get data from DIP
Každý den ve 00:45 spustí event `run_pnd` (načítání dat z distribuce).

### Prediktivni pretoky pri zaporne cene
Vlastní automatizace (bez blueprintu). Při záporné ceně přebírá řízení od blueprintu *Time to Use Overflow*.
Rozhodování na základě:
- `battery_soc >= 80` nebo predikovaný přebytek > 1 kWh
- Sezóna: duben–září
- Zbývající hodiny do večera (20:00)

Triggery: záporná/kladná cena, SoC > 80 %, každých 15 minut.

---

## Blueprinty (jan-trnka)

| Blueprint | Automatizace | Popis |
|-----------|-------------|-------|
| `time_to_use_overflow` | Time to Use Overflow | Zapíná filtraci při přetocích dle průměrné spotřeby/výroby a ceny |
| `auto_set_dod` | Auto Set DoD | Automaticky nastavuje DoD dle predikce výroby, sezóny a průměrné spotřeby |
| `battery_fully_charge` | Fully Charge Battery Once a Week | Jednou týdně plně nabije baterii (pokud nebyla na 100 %) |
| `discharge_battery_to_grid` | Discharge Battery to the Grid | Vybíjí baterii do sítě při vysoké ceně a dostatečné predikci výroby |
| `eco_discharge_when_low_price` | Eco Discharge When Low Price at Noon | Eco vybíjení při nízké ceně kolem poledne |
| `turn_off_eco_discharge_when_peak` | Turn Off Eco Discharge Mode When Peak | Vypíná Eco vybíjení při špičkové spotřebě |
| `disable_overflow` | Disable Overflow | Omezuje/povoluje dodávku do sítě dle spotové ceny |

---

## Skripty

| Skript | Popis |
|--------|-------|
| `script.fve_grid_export_status` | Načte stav grid export parametru z GoodWe |
| `script.fve_grid_export_off` (`1717590811347`) | Zakáže export do sítě (grid_export = 0) |
| `script.fve_grid_export_on` (`1717590841020`) | Povolí export do sítě (grid_export = 1) |
| `script.filtrace_on` | Zapne bazénové čerpadlo |
| `script.filtrace_off` | Vypne bazénové čerpadlo |

---

## Logika výpočtu spotřeby

```
house_consumption (GoodWe)
  − bazenova_filtrace_vykon (0 nebo filtrace_vykon_w)
  − eco_volter_vykon (kW→W wrapper)
= base_house_consumption
```

Denní snapshot (23:59:55):
```
house_consumption_daily
  − filtrace_spustena [h] × filtrace_vykon_w [W] / 1000
  − eco_volter_daily_energy [kWh]
= base_final_daily_house_consumption
```

Průměry pak vychází z `base_final_daily_house_consumption` (7denní mean) a `base_nighttime_consumption` (3denní mean).

---

## Poznámky

- **Utility metery** vždy vytvářet přes GUI (Settings → Helpers), nikdy přes YAML — YAML utility_meter entity nejsou v `core.entity_registry` a reference validator je odmítne.
- `input_number.filtrace_vykon_w` je jediné místo pro změnu příkonu čerpadla — propaguje se automaticky do všech výpočtů.
- Blueprinty od jan-trnka jsou v `config/blueprints/automation/jan-trnka/`.
