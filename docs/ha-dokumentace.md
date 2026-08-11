# Home Assistant — kompletní dokumentace konfigurace

> Generováno z `config/automations.yaml`, `config/sensor.yaml`, `config/configuration.yaml`, `config/scripts.yaml`  
> Aktuální k: 2026-07-13

---

## Obsah

1. [Přehled systému](#1-přehled-systému)
2. [Integrace a hardware](#2-integrace-a-hardware)
3. [Senzory](#3-senzory)
   - [Spotřeba domu](#31-spotřeba-domu)
   - [FVE a baterie](#32-fve-a-baterie)
   - [Noční spotřeba](#33-noční-spotřeba)
   - [Statistické průměry](#34-statistické-průměry)
   - [Denní snapshoty](#35-denní-snapshoty)
   - [Ostatní senzory](#36-ostatní-senzory)
4. [Binární senzory](#4-binární-senzory)
5. [Pomocné entity (helpers)](#5-pomocné-entity-helpers)
6. [Skripty](#6-skripty)
7. [Automatizace](#7-automatizace)
   - [FVE a správa přetoků](#71-fve-a-správa-přetoků)
   - [Filtrace bazénu](#72-filtrace-bazénu)
   - [Nabíjení EV (KIA EV6)](#73-nabíjení-ev-kia-ev6)
   - [Baterie — GoodWe blueprinty](#74-baterie--goodwe-blueprinty)
   - [AppDaemon / PND](#75-appdaemon--pnd)
8. [Závislosti a cross-reference entit](#8-závislosti-a-cross-reference-entit)

---

## 1. Přehled systému

Systém řídí fotovoltaickou elektrárnu s baterií (GoodWe), nabíjení elektromobilu (KIA EV6 přes EcoVolter Pro II) a bazénovou filtraci (Shelly Pro 1PM). Klíčový princip je **ekonomická optimalizace** podle spot ceny elektřiny OTE:

- Při **záporné ceně** → maximalizovat vlastní spotřebu (EV, baterie, filtrace), minimalizovat přetoky do sítě
- Při **kladné ceně** → priorita je plnit baterii ze solárů; EV nabíjet jen ze solárního přebytku (přetoků >4,1 kW)
- Blueprinty od `jan-trnka` řídí pokročilé scénáře vybíjení baterie do sítě při vysokých cenách

### Klíčové entity (přehled)

| Entita | Typ | Účel |
|--------|-----|------|
| `sensor.energy_buy` | template sensor | Import ze sítě (W), sumace fází |
| `sensor.energy_sell` | template sensor | Export do sítě (W), sumace fází |
| `sensor.base_house_consumption` | template sensor | Spotřeba domu bez filtrace a EV |
| `sensor.solcast_pv_forecast_power_now` | Solcast integrace | Reálný solární výkon nezávislý na GoodWe export limitu — indikátor slunce pro EV i filtraci |
| `sensor.current_spot_electricity_price` | integrace | Aktuální cena OTE (Kč/kWh) |
| `sensor.battery_state_of_charge` | GoodWe | Stav baterie (%) |
| `sensor.fve_battery_discharge_w` | template sensor | Výkon vybíjení baterie (W) |
| `number.goodwe_limit_dodavky_do_site` | GoodWe | Limit exportu do sítě (W) |
| `switch.ecovolter_revcr01c00002056_is_charging_enable` | EcoVolter | Zapnutí/vypnutí nabíjení EV |
| `input_boolean.ev_nabijeni_povoleno` | helper | Master kill-switch nabíjení EV |
| `input_boolean.time_to_use_overflows` | helper | Signál pro využití přetoků |

---

## 2. Integrace a hardware

| Integrace | Hardware / Služba | Klíčové entity |
|-----------|-------------------|----------------|
| **GoodWe** | Střídač + baterie | `sensor.battery_state_of_charge`, `sensor.pv_power`, `number.goodwe_*`, `select.goodwe_*` |
| **Solcast** | Předpověď výroby FVE | `sensor.solcast_pv_forecast_forecast_today`, `sensor.solcast_pv_forecast_forecast_remaining_today`, `sensor.solcast_pv_forecast_power_now` |
| **Spot electricity** | Ceny OTE (ČR) | `sensor.current_spot_electricity_price`, `sensor.current_spot_electricity_hour_order`, `sensor.spot_most_expensive_electricity_today`, `binary_sensor.spot_electricity_is_cheapest_3_hours_block` |
| **EcoVolter Pro II** | Nástěnná nabíječka EV | `switch.ecovolter_revcr01c00002056_is_charging_enable`, `number.ecovolter_*_target_current`, `sensor.ecovolter_*_actual_power`, `binary_sensor.ecovolter_*_is_vehicle_connected` |
| **Kia UVO** | KIA EV6 (cloud API) | `sensor.ev6_ev_battery_level`, `number.ev6_ac_charging_limit` |
| **Shelly Pro 1PM** | Bazénová filtrace | `switch.filtrace_switch` |
| **AppDaemon** | PND (Predictive Node Dispatcher) | `binary_sensor.pnd_running`, event `run_pnd` |

---

## 3. Senzory

### 3.1 Spotřeba domu

#### `sensor.base_house_consumption`
- **Typ:** template sensor (configuration.yaml)
- **Jednotka:** W
- **Vzorec:** `max(house_consumption − bazenova_filtrace_vykon − eco_volter_vykon, 0)`
- **Účel:** "Čistá" spotřeba domu bez proměnných spotřebičů (EV a filtrace jsou odečteny). Používají ho blueprinty pro výpočet skutečné základní zátěže.
- **Závisí na:** `sensor.house_consumption`, `sensor.bazenova_filtrace_vykon`, `sensor.eco_volter_vykon`
- **Používá se v:** Automations `discharge_battery_to_grid`, `turn_off_eco_discharge_when_peak`, `eco_discharge_when_low_price`, sensor `base_average_*`

#### `sensor.bazenova_filtrace_vykon`
- **Typ:** template sensor (configuration.yaml)
- **Jednotka:** W
- **Vzorec:** IF `switch.filtrace_switch` = 'on' → `input_number.filtrace_vykon_w` (default 450) ELSE 0
- **Účel:** Odhadovaný příkon filtrace (není měřený — EcoVolter má svůj senzor, ale Shelly Pro 1PM měří). Používá se v odečtech.
- **Závisí na:** `switch.filtrace_switch`, `input_number.filtrace_vykon_w`

#### `sensor.eco_volter_vykon`
- **Typ:** template sensor (configuration.yaml)
- **Jednotka:** W
- **Vzorec:** `round(float(sensor.ecovolter_revcr01c00002056_actual_power, 0) * 1000, 0)`
- **Účel:** EcoVolter reportuje výkon v kW → konverze na W pro odečty ze spotřeby domu.
- **Závisí na:** `sensor.ecovolter_revcr01c00002056_actual_power`

#### `sensor.ev_denni_naklady`
- **Typ:** template sensor (configuration.yaml)
- **Jednotka:** Kč
- **Vzorec:** `round(float(ecovolter_daily_energy, 0) * float(current_spot_electricity_price, 3), 1)`
- **Účel:** Odhad denních nákladů na nabíjení EV v Kč (kWh × spotová cena).

### 3.2 FVE a baterie

#### `sensor.fve_battery_discharge_w`
- **Typ:** template sensor (GUI — template integration)
- **Jednotka:** W
- **Vzorec:** IF `sensor.battery_power` > 0 → `sensor.battery_power` ELSE 0
- **Účel:** Výkon vybíjení baterie. `battery_power` je kladné při vybíjení, záporné při nabíjení. Tento senzor vrací pouze kladné hodnoty (vybíjení), jinak 0.
- **Závisí na:** `sensor.battery_power` (GoodWe)
- **Používá se v:**
  - `ev_zastavit_velky_import` — trigger >2000 W → zastavit EV
  - `ev_nabijeni_zaporne_ceny_pretoky` — ochrana baterie při záporné ceně
  - `ev_regulace_proudu` — snižování nabíjecího proudu při vybíjení baterie

### 3.3 Noční spotřeba

#### `sensor.nighttime_consumption`
- **Typ:** template sensor (configuration.yaml)
- **Jednotka:** W
- **Vzorec:** IF (hour ≥ 20 OR hour < 9) → `sensor.house_consumption` ELSE 0
- **Účel:** Celková spotřeba domu v nočních hodinách; přes den vrací 0. Slouží pro statistický průměr noční spotřeby.

#### `sensor.base_nighttime_consumption`
- **Typ:** template sensor (configuration.yaml)
- **Jednotka:** W
- **Vzorec:** IF (hour ≥ 20 OR hour < 9) → `sensor.base_house_consumption` ELSE 0
- **Účel:** Noční spotřeba bez filtrace a EV.

### 3.4 Statistické průměry

Všechny statistické senzory jsou definovány v `sensor.yaml` (platform: `statistics` nebo `history_stats`).

| Entita | Zdroj | Charakteristika | Okno | Účel |
|--------|-------|-----------------|------|------|
| `sensor.average_pv_production_in_last_20_minutes` | `sensor.pv_power` | mean | 20 min | Klouzavý průměr výroby FVE; blueprinty ho používají pro detekci přetoků |
| `sensor.base_average_consumption_in_last_20_minutes` | `sensor.base_house_consumption` | mean | 20 min | Klouzavý průměr základní spotřeby; blueprint Time to Use Overflow |
| `sensor.base_average_daily_consumption` | `sensor.base_final_daily_house_consumption` | mean | 7 dní | 7denní průměr denní základní spotřeby; prediktivní automatizace + blueprinty |
| `sensor.base_average_night_consumption_in_last_3_days` | `sensor.base_nighttime_consumption` | mean | 85 h | Průměr noční základní spotřeby; blueprint Discharge Battery to Grid |
| `sensor.count_of_100_battery_state_in_a_week` | `sensor.battery_state_of_charge` | count (=100) | 7 dní | Počet dosažení 100% SoC za týden; blueprint Fully Charge Battery Once a Week |
| `sensor.filtrace_spustena` | `switch.filtrace_switch` (on) | time | dnes | Počet hodin filtrace za dnešní den; použito v denním snapshotu |

### 3.5 Denní snapshoty

Zachycují hodnoty každý den v 23:59:55 (time-triggered template senzory).

#### `sensor.final_daily_house_consumption`
- **Jednotka:** kWh
- **Vzorec:** `round(float(sensor.house_consumption_daily, 0), 1)`
- **Účel:** Celková denní spotřeba domu (včetně filtrace a EV). Slouží jako zdroj pro 7denní průměr `sensor.average_daily_consumption`.

#### `sensor.base_final_daily_house_consumption`
- **Jednotka:** kWh
- **Vzorec:** `max(house_consumption_daily − (filtrace_spustena_h × filtrace_vykon_w / 1000) − ecovolter_daily_energy, 0)`
- **Účel:** Denní základní spotřeba bez filtrace a EV. Zdroj pro `sensor.base_average_daily_consumption` používaný blueprinty.

### 3.6 Ostatní senzory

#### `sensor.nighttime_consumption` (viz 3.3)

#### `sensor.average_consumption_in_last_20_minutes`
- Zdroj: `sensor.house_consumption` (celková spotřeba vč. EV a filtrace), mean 20 min.
- Starší/alternativní průměr celkové spotřeby; blueprinty používají raději `base_*` variantu.

---

## 4. Binární senzory

#### `binary_sensor.ev_nabiji_ze_solaru`
- **Typ:** template binary sensor (configuration.yaml)
- **Vzorec:** `binary_sensor.ecovolter_*_is_charging` = 'on' AND `sensor.energy_sell` > 500
- **Ikona:** `mdi:solar-power` / `mdi:power-plug`
- **Účel:** Indikátor, zda EV aktuálně nabíjí primárně ze solárního přebytku.

#### `binary_sensor.pnd_running`
- **Typ:** template binary sensor (configuration.yaml)
- **Stav:** hard-coded 'off' (skutečný stav spravuje AppDaemon externě)
- **Účel:** Signalizuje, zda běží PND (AppDaemon Predictive Node Dispatcher).

---

## 5. Pomocné entity (helpers)

### input_number

| Entita | Název | Rozsah | Default | Účel |
|--------|-------|--------|---------|------|
| `input_number.battery_capacity` | Battery Capacity | 0–30 kWh | — | Kapacita baterie; blueprinty ji používají pro výpočet SoC headroom |
| `input_number.winter_dod` | Winter DoD Value | 0–100 % | — | Minimální SoC baterie v zimě; blueprint Auto Set DoD |
| `input_number.filtrace_vykon_w` | Výkon filtrace bazénu | 0–2000 W | 450 | Odhadovaný příkon filtrace pro odečty spotřeby |

### input_boolean

| Entita | Název | Účel |
|--------|-------|------|
| `input_boolean.time_to_use_overflows` | Time to Use Overflows | Signál pro automatizaci filtrace: spusť filtraci při přetocích. Přepínán automatizacemi `prediktivni_pretoky` a blueprintem `Disable Overflow`. |
| `input_boolean.ev_nabijeni_povoleno` | EV nabíjení povoleno | **Master kill-switch** pro veškeré automatické nabíjení EV. Při 'off' žádná automatizace EV nespustí nabíjení. Vytvořen v GUI 2026-04-25. |

### input_select

| Entita | Název | Možnosti | Účel |
|--------|-------|----------|------|
| `input_select.season` | Season | Summer / Spring/Autumn / Winter | Ruční přepnutí sezóny; používá blueprint Auto Set DoD |

---

## 6. Skripty

### `script.fve_grid_export_status` — FVE: grid export STATUS
- Přečte aktuální hodnotu parametru `grid_export` z GoodWe střídače a uloží ji do `input_text.goodwe_grid_export`.
- Voláno automaticky po každé změně (ON/OFF skripty níže).

### `script.1717590811347` — FVE: grid export OFF
- Nastaví `grid_export = 0` na GoodWe (zakáže export do sítě).
- Poté zavolá `script.fve_grid_export_status` pro refresh.
- Voláno z automatizace `Přetoky nastavit` při záporné ceně.

### `script.1717590841020` — FVE: grid export ON
- Nastaví `grid_export = 1` na GoodWe (povolí export do sítě).
- Poté zavolá `script.fve_grid_export_status` pro refresh.
- Voláno z automatizace `Přetoky nastavit` při kladné ceně.

### `script.filtrace_on` — Filtrace ON
- Zapne Shelly Pro 1PM (bazénová filtrace) přes `switch.turn_on`.

### `script.filtrace_off` — Filtrace OFF
- Vypne Shelly Pro 1PM (bazénová filtrace) přes `switch.turn_off`.

---

## 7. Automatizace

### 7.1 FVE a správa přetoků

---

#### `Přetoky nastavit` (id: `1717828775243`)
**Trigger:** Změna hodnoty `sensor.current_spot_electricity_hour_order` (= každá celá hodina, kdy se mění cena)

**Logika:**
- Záporná cena → zavolá `FVE: grid export OFF`, nastaví `goodwe_limit_dodavky_do_site = 200`, notifikace
- Kladná cena → zavolá `FVE: grid export ON`, nastaví `goodwe_limit_dodavky_do_site = 10000`

**Proč:** Při záporné ceně je export do sítě ztrátový (platíme za každou kWh navíc). Limit 200 W ponechá GoodWe schopný regulovat frekvenční odchylky, ale fakticky neexportuje.

> ⚠️ **Pozn.:** Tato automatizace konfliktuje se scénářem WATrouter bojleru — viz [watrouter-boiler-negative-price.md](watrouter-boiler-negative-price.md)

**Závisí na:** `sensor.current_spot_electricity_hour_order`, `sensor.current_spot_electricity_price`  
**Ovládá:** `number.goodwe_limit_dodavky_do_site`, skripty `fve_grid_export_*`

---

#### `Prediktivni pretoky pri zaporne cene` (id: `predictive_overflow_negative_price`)
**Trigger (4 triggery s id):**
- `price_negative` — cena klesne pod 0
- `price_positive` — cena stoupne nad 0
- `battery_rising` — SoC baterie přesáhne 80 %
- `periodic` — každých 15 minut

**Proměnné (každé spuštění):**
```
predicted_overflow = solcast_remaining − remaining_battery_capacity − remaining_consumption
should_enable_overflow = je léto (IV–IX) AND je den AND (SoC ≥ 80 OR predicted_overflow > 1 kWh)
```

**Logika (3 větve):**

| Větev | Trigger | Akce |
|-------|---------|------|
| A | `price_negative` | Zastaví blueprint `time_to_use_overflow`; pokud `should_enable_overflow` → zapne přetoky, jinak vypne + notifikace |
| B | `battery_rising` nebo `periodic` (za záporné ceny) | Aktualizuje `time_to_use_overflows` podle `should_enable_overflow` (bez notifikace) |
| C | `price_positive` | Vypne přetoky, reaktivuje blueprint `time_to_use_overflow`, notifikace |

**Proč existuje:** Při záporné ceně musí rozhodnutí o přetocích dělat tato prediktivní logika místo blueprintu (blueprint by přetoky vypnul, protože by je nevyhodnotil jako výhodné při záporné ceně).

**Závisí na:** `sensor.current_spot_electricity_price`, `sensor.battery_state_of_charge`, `sensor.solcast_pv_forecast_forecast_remaining_today`, `sensor.base_average_daily_consumption`, `input_number.battery_capacity`  
**Ovládá:** `input_boolean.time_to_use_overflows`, `automation.time_to_use_overflow`

---

#### `FVE: solcast updates` (id: `1717674886307`)
**Trigger:** Čas — 10× denně (06:30, 08:00–15:30 po hodině, 18:30)  
**Akce:** Volá `solcast_solar.update_forecasts`  
**Účel:** Udržuje Solcast předpověď aktuální pro prediktivní automatizace.

---

### 7.2 Filtrace bazénu

---

#### `Ovládání filtrace - přetoky` (id: `1718351641005`)
**Trigger:** Každých 5 minut (`time_pattern: /5`) + změna stavu `switch.ecovolter_revcr01c00002056_is_charging_enable` (on i off)

**Podmínky:** žádné (dřívější pevné okno 09:00–17:30 bylo zrušeno — nahradilo ho reálné solární posouzení, viz níže)

**Proměnné (každé spuštění):**
```
je_cena_zaporna          = current_spot_electricity_price < 0
solar_now                = sensor.solcast_pv_forecast_power_now (W) POKUD je_cena_zaporna,
                           JINAK sensor.pv_power (W) — reálný výkon panelů
base_spotreba            = sensor.base_house_consumption (W)
volny_vykon              = solar_now − base_spotreba
ev_nabiji                = switch.ecovolter_*_is_charging_enable = 'on'
ev_vykon                 = sensor.eco_volter_vykon (W) pokud ev_nabiji, jinak 0
volny_vykon_pro_filtraci = volny_vykon − ev_vykon
filtrace_vykon           = input_number.filtrace_vykon_w (výchozí 450 W)
battery_soc              = sensor.battery_state_of_charge
baterie_plna             = battery_soc ≥ 95 %
filtrace_bezi            = switch.filtrace_switch = 'on'
dost_slunce_zapnout      = volny_vykon_pro_filtraci > filtrace_vykon + 150 W
dost_slunce_udrzet       = volny_vykon_pro_filtraci > filtrace_vykon − 150 W
energy_buy_now           = sensor.energy_buy (W)
battery_discharge_now    = sensor.fve_battery_discharge_w (W)
system_v_preteceni       = energy_buy_now > 1500 W NEBO battery_discharge_now > 2000 W
muze_bezet               = baterie_plna AND NOT system_v_preteceni
```

**Logika:** Zapnout filtraci, pokud `muze_bezet` (tj. baterie je nabitá A systém není v přetížení) AND platí alespoň jedno:
- filtrace neběží AND `dost_slunce_zapnout`
- filtrace běží AND `dost_slunce_udrzet` (hystereze ±150 W proti trhanému přepínání kolem prahu při každém 5min cyklu)
- `input_boolean.time_to_use_overflows` = 'on'

Jinak filtraci vypnout.

**Proč `baterie_plna` jako povinná podmínka, ne jen jedna z alternativ (opraveno po incidentu 2026-07-14):** Původně byl `baterie_plna` jen jednou z OR-větví — mohl tedy chybět a filtrace přesto naskočila jen na základě `dost_slunce_zapnout`, i když baterie byla teprve na 37 % a reálně se právě dobíjela ze solárního přebytku. To je špatně: dobíjení baterie má mít přednost před filtrací. `baterie_plna` (SoC ≥ 95 %) je teď nutná podmínka (`muze_bezet`) pro jakékoliv zapnutí — dokud baterie není prakticky plná, přebytek jde vždy nejdřív do ní, filtrace nesmí běžet, i kdyby solární výkon byl vysoký.

**Proč `system_v_preteceni` veto (přidáno po incidentu 2026-07-13):** Tato automatizace se spouští i při změně stavu EV switche na 'off'. Když `ev_zastavit_velky_import` nebo `ev_regulace_proudu` zastaví EV kvůli reálnému přetížení (vysoký import/vybíjení baterie), přepnutí EV switche na 'off' okamžitě znovu spustí tuto automatizaci — a bez vlastní znalosti probíhajícího přetížení by `volny_vykon_pro_filtraci` (počítané jen z optimistické Solcast předpovědi) mohlo vyjít kladně a filtraci hned zase zapnout, čímž by zmařilo smysl toho, že EV automatizace filtraci těsně předtím přednostně vypnula. `system_v_preteceni` používá stejné reálné senzory a prahy (1500 W / 2000 W) jako EV-stop automatizace, takže filtrace se nezapne/nezůstane zapnutá, dokud přetížení skutečně neopadne.

**Proč `pv_power` při kladné ceně a `solcast_pv_forecast_power_now` při záporné (opraveno po incidentu 2026-07-13):** Původně se používala jen Solcast předpověď natvrdo, i za kladné ceny — to způsobilo, že se filtrace zapnula, i když reálný výkon panelů byl jen ~400 W a přetoky do sítě ~2 W (Solcast predikce byla optimističtější než realita). Za kladné ceny `grid_export_limit` netlumí výrobu, takže `sensor.pv_power` je přesný a je třeba dát přednost reálné hodnotě před predikcí. Teprve za záporné ceny automatizace `Přetoky nastavit` sníží `grid_export_limit` na 200 W, GoodWe kvůli tomu deratuje MPPT a `pv_power`/`energy_sell` pak ukazují nízké hodnoty i když reálně dost svítí — tehdy je nutné použít Solcast předpověď jako proxy (stejný důvod, proč ji používá i EV nabíjení, viz [ev-charging-automation.md](ev-charging-automation.md)), i za cenu menší přesnosti.

**Proč odečet `ev_vykon`:** Nabíjení EV má prioritu před filtrací. Co EV reálně odebírá, se odečte od volného výkonu dřív, než se rozhoduje o filtraci — pokud EV nenabíjí (není naplánováno nebo je vozidlo nabité), filtrace dostane celý solární přebytek.

**Proč zrušení okna 09:00–17:30:** `solar_now` je mimo denní dobu přirozeně ~0, takže sám funguje jako noční/večerní vypínač. Předchozí verze počítala `predikce_ok` z celodenní Solcast předpovědi a stavu baterie (bez ohledu na okamžitou výrobu), což v létě umožňovalo běh až do ~20:00 — reálně pozorovaných ~11 h denně bez ohledu na aktuální slunce.

**Závisí na:** `sensor.current_spot_electricity_price`, `sensor.pv_power`, `sensor.solcast_pv_forecast_power_now`, `sensor.base_house_consumption`, `switch.ecovolter_revcr01c00002056_is_charging_enable`, `sensor.eco_volter_vykon`, `input_number.filtrace_vykon_w`, `sensor.battery_state_of_charge`, `sensor.energy_buy`, `sensor.fve_battery_discharge_w`, `input_boolean.time_to_use_overflows`  
**Ovládá:** `switch.filtrace_switch`

---

#### `Time to Use Overflow` (id: `1772571943115`) — Blueprint
**Blueprint:** `jan-trnka/time_to_use_overflow.yaml`  
**Účel:** Při přebytku výroby nad spotřebou (zohledňuje cenu) nastaví `input_boolean.time_to_use_overflows` na 'on' → spustí filtraci.  
**Vstupy:** `sensor.base_average_consumption_in_last_20_minutes`, `sensor.average_pv_production_in_last_20_minutes`, `sensor.current_spot_electricity_price`, max_price: 0.7 Kč/kWh, `sensor.battery_state_of_charge`

> **Pozn.:** Při záporné ceně je tento blueprint deaktivován automatizací `prediktivni_pretoky` a řízení přebírá prediktivní logika.

---

#### `Disable Overflow` (id: `1772991037309`) — Blueprint
**Blueprint:** `jan-trnka/disable_overflow.yaml`  
**Účel:** Při kladné ceně (nebo obecně mimo podmínky) vypíná `time_to_use_overflows` a resetuje `goodwe_limit_dodavky_do_site = 10000`.

---

### 7.3 Nabíjení EV (KIA EV6)

Veškeré nabíjení EV je blokováno, pokud `input_boolean.ev_nabijeni_povoleno` = 'off'.

---

#### `EV nabíjení - záporné ceny nebo přetoky` (id: `ev_nabijeni_zaporne_ceny_pretoky`)
**Mode:** queued, max: 5

**Triggery:**
| id | Trigger |
|----|---------|
| `cena_zaporna` | Cena OTE klesne pod 0 |
| `cena_kladna` | Cena OTE stoupne nad 0 |
| `pretoky_start` | `sensor.energy_sell` > 4100 W po dobu 2 min |
| `auto_pripojeno` | Připojení EV (`is_vehicle_connected` → 'on') |

**Proměnné (každé spuštění):**
```
je_solar_dost    = solcast_pv_forecast_power_now > 1000 W
je_cena_zaporna  = current_spot_electricity_price < 0
je_pretoky_velke = energy_sell > 4100 W
ev6_soc          = ev6_ev_battery_level (%)
ev6_limit        = ev6_ac_charging_limit (%)
ev6_soc_ok       = SoC není unavailable/unknown
```

**Logika (4 větve):**

| Větev | Podmínka spuštění | Akce |
|-------|-------------------|------|
| `pretoky_start` | Nenabíjí + EV připojeno + SoC < limit | Nastaví proud 6A → zapne nabíjení → čeká 90s na actual_power > 0.3 kW |
| `cena_zaporna` | Nenabíjí + EV připojeno + dost solárů + SoC < limit | Nastaví goodwe_limit = 10000 → proud 6A → zapne → čeká; po startu ověří import/baterii (stop-check) |
| `auto_pripojeno` | SoC < limit + (přetoky NEBO záporná cena se solarary) | Stejně jako výše, větev podle ceny |
| `cena_kladna` | Nabíjí + přetoky < 4100 W | Vypne nabíjení + notifikace |

**Proč `solcast_pv_forecast_power_now` jako sluneční indikátor:**  
Při záporné ceně je `goodwe_limit_dodavky_do_site` nastaven na 0 (Přetoky nastavit), takže `energy_sell` = 0 a `pv_power` může být utlumená. Solcast předpověď ukazuje skutečný potenciál výroby nezávisle na GoodWe limitu.

**Závisí na:** `sensor.current_spot_electricity_price`, `sensor.energy_sell`, `binary_sensor.ecovolter_*_is_vehicle_connected`, `sensor.ev6_ev_battery_level`, `number.ev6_ac_charging_limit`, `sensor.solcast_pv_forecast_power_now`, `sensor.energy_buy`, `sensor.fve_battery_discharge_w`, `input_boolean.ev_nabijeni_povoleno`  
**Ovládá:** `switch.ecovolter_*_is_charging_enable`, `number.ecovolter_*_target_current`, `number.goodwe_limit_dodavky_do_site`

---

#### `EV zastavit - příliš velký import` (id: `ev_zastavit_velky_import`)
**Triggery:**
- `sit_import`: `sensor.energy_buy` > 1500 W po dobu 2 min
- `baterie_vybijeni`: `sensor.fve_battery_discharge_w` > 2000 W po dobu 2 min

**Podmínky:** EV nabíjí (`is_charging_enable` = 'on') + nabíjení povoleno

**Akce:**
1. Pokud `switch.filtrace_switch` = 'on' → vypne filtraci + notifikace, počká 1 min, znovu zkontroluje `energy_buy > 1500 W` NEBO `fve_battery_discharge_w > 2000 W`
   - Problém vyřešen (nebo filtrace neběžela) → NE → konec, nabíjení EV pokračuje
   - Stále nad limitem → pokračuje krokem 2
2. Vypne nabíjení EV
3. Při záporné ceně: nastaví `goodwe_limit_dodavky_do_site = 0`
4. Notifikace s textem podle triggeru (import ze sítě vs. vybíjení baterie)

**Proč 2 triggery:** EV může být zdrojem problémů i když není síťový import (při záporné ceně je limit = 10000, baterie vybíjí). Proto je sledováno i vybíjení baterie.

**Proč nejdřív filtrace:** Filtrace (~450 W, `switch.filtrace_switch`) má nižší prioritu než EV nabíjení — je to nejsnazší zátěž k odlehčení bez přerušení probíhající nabíjecí session (restart nabíjení má vlastní 90s probe). Stejnou logiku má i `ev_regulace_proudu` (viz níže).

---

#### `EV zastavit - auto přestalo nabíjet` (id: `ev_zastavit_plna_baterie`)
**Trigger:** `sensor.ecovolter_*_actual_power` < 0,3 kW po dobu 5 min  
**Podmínky:** EV nabíjí + nabíjení povoleno

**Akce:**
1. Vypne nabíjení
2. Při záporné ceně: resetuje `goodwe_limit = 0`
3. Notifikace "Auto přestalo přijímat proud"

**Proč:** KIA EV6 přestane přijímat proud při dosažení nastaveného limitu SoC (nebo při plném stavu). EcoVolter nezasílá notifikaci — auto přestane odebírat, ale switch zůstane zapnutý. Tato automatizace to detekuje po 5 minutách.

---

#### `EV regulace proudu při záporné ceně` (id: `ev_regulace_proudu`)
**Trigger:** Každé 2 minuty  
**Podmínky:** Nabíjení běží + povoleno + záporná cena

**Logika (3 větve):**
| Větev | Podmínka | Akce |
|-------|----------|------|
| 1 | `energy_sell` > 500 W + proud < 10A | Zvýší proud o 1A (max 10A) |
| 2 | `fve_battery_discharge_w` > 2000 W + proud > 6A | Sníží proud o 1A (min 6A) |
| 3 | `fve_battery_discharge_w` > 2000 W + proud ≤ 6A | Pokud `switch.filtrace_switch` = 'on' → vypne filtraci + notifikace, počká 1 min, znovu zkontroluje `fve_battery_discharge_w > 2000 W`; pokud stále ano (nebo filtrace neběžela) → zastaví nabíjení + goodwe_limit = 0 + notifikace |

**Účel:** Dynamicky přizpůsobuje nabíjecí proud 6–10A tak, aby EV pohltilo solární přebytek bez zatěžování baterie. Při záporné ceně chceme co nejvíce exportovat do EV (ne do sítě).

---

#### `KIA EV6: force update při začátku nabíjení` (id: `kia_ev6_force_update_on_charge`)
**Trigger:** `sensor.ecovolter_*_actual_power` > 0,3 kW po dobu 1 min  
**Podmínka:** Automation nebyla spuštěna v posledních 60 minutách (throttling)  
**Akce:** `kia_uvo.force_update` pro KIA EV6

**Proč:** KIA cloud API je pomalé a SoC se aktualizuje jen při explicitním dotazu. Po startu nabíjení vyžádáme čerstvá data pro správné rozhodování.

---

#### `EV - blokovat vybíjení baterie při plánovaném nabíjení` (id: `ev_blokovat_vybijeni_baterie`)
**Mode:** queued, max: 3

**Triggery:**
- Změna stavu `input_boolean.ev_nabijeni_povoleno`
- Změna `sensor.ev6_ev_battery_level`
- Změna `number.ev6_ac_charging_limit`

**Proměnné (každé spuštění):**
```
treba_blokovat = ev_nabijeni_povoleno = 'on'
               AND ev6_ev_battery_level není unavailable/unknown
               AND ev6_ev_battery_level < ev6_ac_charging_limit
uz_blokovano   = automation.discharge_battery_to_the_grid = 'off'
```

**Logika (2 větve):**

| Větev | Podmínka | Akce |
|-------|----------|------|
| Zablokovat | `treba_blokovat` AND NOT `uz_blokovano` | Vypne 3 blueprintové automatizace vybíjení + notifikace |
| Obnovit | NOT `treba_blokovat` AND `uz_blokovano` | Zapne 3 blueprintové automatizace vybíjení + notifikace |

**Ovládané automatizace (turn_off / turn_on):**
- `automation.discharge_battery_to_the_grid`
- `automation.eco_discharge_when_low_price_at_noon`
- `automation.turn_off_eco_discharge_mode_when_peak`

**Proč:** Blueprinty `jan-trnka` řídí vybíjení domácí baterie do sítě při výhodné ceně. Pokud je ale naplánováno nabíjení EV a EV baterie je pod limitem, je prioritou nabít EV — vybíjet domácí baterii "zadarmo" do sítě by bylo kontraproduktivní. Automatizace tyto blueprinty dočasně zakáže a po nabití EV je obnoví.

**Závisí na:** `input_boolean.ev_nabijeni_povoleno`, `sensor.ev6_ev_battery_level`, `number.ev6_ac_charging_limit`  
**Ovládá:** `automation.discharge_battery_to_the_grid`, `automation.eco_discharge_when_low_price_at_noon`, `automation.turn_off_eco_discharge_mode_when_peak`

---

### 7.4 Baterie — GoodWe blueprinty

Všechny blueprinty jsou od `jan-trnka`. Řídí GoodWe střídač přes `select.goodwe_provozni_rezim_stridace` (ECO/Normal/Off-Grid) a `number.goodwe_vykon_v_ekonomickem_rezimu`.

| Blueprint | id | Účel |
|-----------|----|------|
| **Auto Set DoD** | `1772629832725` | Automaticky nastavuje hloubku vybití (DoD) podle předpovědi výroby na dnešek a průměrné spotřeby. Přizpůsobuje se sezóně. |
| **Fully Charge Battery Once a Week** | `1772632237910` | Jednou týdně plně nabije baterii (ve 02:00), pokud nebyla nabitá 100% za posledních 7 dní. Prodlouží životnost baterií. |
| **Discharge Battery to the Grid** | `1772656770792` | Při vysoké ceně OTE vybíjí baterii do sítě (přepne do ECO režimu). Rozhoduje podle prognózy na zítřek a noční spotřeby. ⚠️ Blokováno při plánovaném nabíjení EV. |
| **Eco Discharge When Low Price at Noon** | `1772657599353` | Při nízké ceně v poledne (levný 3h blok) vypne ECO vybíjení a nechá baterii nabíjet ze solárů. ⚠️ Blokováno při plánovaném nabíjení EV. |
| **Turn Off Eco Discharge Mode When Peak** | `1772657930649` | Vypne ECO vybíjení při špičce spotřeby (dům bere víc než FVE + výkon ECO režimu). ⚠️ Blokováno při plánovaném nabíjení EV. |

> **⚠️ Tři blueprinty vybíjení jsou automaticky blokovány** automatizací `ev_blokovat_vybijeni_baterie`, pokud je `input_boolean.ev_nabijeni_povoleno` = on a `sensor.ev6_ev_battery_level` < `number.ev6_ac_charging_limit`. Po nabití EV jsou obnoveny.

---

### 7.5 AppDaemon / PND

#### `Run PND` (id: `1778526312883`)
**Trigger:** Každý den v 00:30  
**Akce:** Vyvolá event `run_pnd`  
**Účel:** Denní spuštění PND (Predictive Node Dispatcher) v AppDaemonu pro noční výpočty a plánování.

#### `Run actions after AppDaemon starts` (id: `1778526595548`)
**Trigger:** Event `APPDAEMON_READY`  
**Akce:** Čeká 15 s → vyvolá event `run_pnd`  
**Účel:** Po restartu AppDaemonu (nebo HA) spustí PND po zaručení inicializace závislostí.

---

## 8. Závislosti a cross-reference entit

### Klíčové entity — kde se používají

| Entita | Kde se nastavuje / mění | Kde se čte |
|--------|------------------------|------------|
| `number.goodwe_limit_dodavky_do_site` | `Přetoky nastavit`, `ev_nabijeni_*`, `ev_zastavit_*`, `ev_regulace_proudu`, `Disable Overflow` blueprint | `ev_regulace_proudu` |
| `input_boolean.time_to_use_overflows` | `prediktivni_pretoky`, `Time to Use Overflow` blueprint, `Disable Overflow` blueprint | `Ovládání filtrace - přetoky` |
| `input_boolean.ev_nabijeni_povoleno` | Manuálně (GUI) | `ev_nabijeni_*` (všechny 4 EV automatizace), `ev_blokovat_vybijeni_baterie` |
| `switch.ecovolter_*_is_charging_enable` | `ev_nabijeni_*`, `ev_zastavit_*`, `ev_zastavit_plna_baterie`, `ev_regulace_proudu` | `ev_zastavit_*` (podmínka), `ev_regulace_proudu` (podmínka), `Ovládání filtrace - přetoky` (trigger + proměnná `ev_nabiji`) |
| `sensor.fve_battery_discharge_w` | GoodWe → template sensor | `ev_zastavit_velky_import` (trigger + recheck po vypnutí filtrace, 2000 W), `ev_nabijeni_*` (stop-check 3000 W), `ev_regulace_proudu` (2000 W, recheck po vypnutí filtrace ve větvi 3) |
| `sensor.energy_sell` | GoodWe (fáze L1+L2+L3, kladné hodnoty) | `ev_nabijeni_*` (trigger 4100 W, regulace 500 W), `binary_sensor.ev_nabiji_ze_solaru` |
| `sensor.energy_buy` | GoodWe (fáze L1+L2+L3, záporné hodnoty) | `ev_zastavit_velky_import` (trigger + recheck po vypnutí filtrace, 1500 W), `ev_nabijeni_*` (stop-check) |
| `switch.filtrace_switch` | `Ovládání filtrace - přetoky`, `ev_zastavit_velky_import` (vypne přednostně před EV), `ev_regulace_proudu` (větev 3, vypne přednostně před EV) | `Ovládání filtrace - přetoky` (stav), `ev_zastavit_velky_import`/`ev_regulace_proudu` (podmínka `is on`), `sensor.bazenova_filtrace_vykon`, `sensor.filtrace_spustena` |
| `sensor.current_spot_electricity_price` | Spot integrace | `Přetoky nastavit`, `prediktivni_pretoky`, `ev_nabijeni_*`, `ev_zastavit_*`, `ev_regulace_proudu`, 5 blueprintů |
| `sensor.battery_state_of_charge` | GoodWe | `prediktivni_pretoky`, 5 blueprintů, `Ovládání filtrace - přetoky` |
| `sensor.base_house_consumption` | template (house − filtrace − EV) | `base_average_*` statistiky, blueprinty, `Ovládání filtrace - přetoky` |
| `sensor.solcast_pv_forecast_power_now` | Solcast integrace | `ev_nabijeni_*` (`je_solar_dost`), `Ovládání filtrace - přetoky` (`solar_now`) |
| `sensor.eco_volter_vykon` | template (ecovolter_actual_power × 1000) | `sensor.base_house_consumption`, `Ovládání filtrace - přetoky` (`ev_vykon`) |
| `input_number.filtrace_vykon_w` | Manuálně (GUI) | `sensor.bazenova_filtrace_vykon`, `sensor.base_final_daily_house_consumption`, `Ovládání filtrace - přetoky` (`filtrace_vykon`) |
| `sensor.ev6_ev_battery_level` | Kia UVO cloud | `ev_nabijeni_*`, `ev_blokovat_vybijeni_baterie` |
| `number.ev6_ac_charging_limit` | Kia UVO cloud | `ev_nabijeni_*`, `ev_blokovat_vybijeni_baterie` |
| `automation.discharge_battery_to_the_grid` | `ev_blokovat_vybijeni_baterie` (turn_off/on) | — |
| `automation.eco_discharge_when_low_price_at_noon` | `ev_blokovat_vybijeni_baterie` (turn_off/on) | — |
| `automation.turn_off_eco_discharge_mode_when_peak` | `ev_blokovat_vybijeni_baterie` (turn_off/on) | — |

### Sekvenční závislosti senzorů

```
sensor.battery_power (GoodWe)
  └── sensor.fve_battery_discharge_w (template: >0 → hodnota, jinak 0)

sensor.house_consumption (GoodWe)
  ├── sensor.bazenova_filtrace_vykon (template: switch.filtrace_switch × filtrace_vykon_w)
  ├── sensor.eco_volter_vykon (template: ecovolter_actual_power × 1000)
  └── sensor.base_house_consumption (template: house − filtrace − EV)
        ├── sensor.base_nighttime_consumption (template: base × noční hodiny)
        │     └── sensor.base_average_night_consumption_in_last_3_days (statistics)
        ├── sensor.base_average_consumption_in_last_20_minutes (statistics)
        └── sensor.base_final_daily_house_consumption (time-triggered snapshot)
              └── sensor.base_average_daily_consumption (statistics)

sensor.pv_power (GoodWe)
  └── sensor.average_pv_production_in_last_20_minutes (statistics)

sensor.house_consumption (GoodWe)
  └── sensor.nighttime_consumption (template: house × noční hodiny)
        └── sensor.average_night_consumption_in_last_3_days (statistics)
  └── sensor.final_daily_house_consumption (time-triggered snapshot)

switch.filtrace_switch
  └── sensor.filtrace_spustena (history_stats: počet hodin dnes)

sensor.battery_state_of_charge (GoodWe)
  └── sensor.count_of_100_battery_state_in_a_week (history_stats)
```

---

*Dokumentace pokrývá pouze YAML-spravovatelné části konfigurace. Entity z `.storage/` (utility metery, GUI helpery) jsou spravovány přes Home Assistant UI — viz poznámka v CLAUDE.md.*
