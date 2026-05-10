# EV Nabíjení - Automatizace

KIA EV6 + EcoVolter Pro II (třífázový). Integrace přes kia_uvo (HACS) + Bluelink Token Generator add-on.

## Cíl

Nabíjet EV minimálním proudem 6A (≈ 4.1 kW třífázově) ve dvou případech:
1. **Záporná cena OTE** — auto jako extra baterie při přetocích ze solárů
2. **Sluneční přebytek** — přetoky do sítě přesahují výkon nabíjení (> 4.1 kW)

Uživatel nakupuje za fixní tarif (neprodává na OTE spotu), takže:
- Export při záporné ceně = platí za každou kWh poslanou do sítě
- Import ze sítě při záporné ceně = stojí plný fixní tarif (nevýhodné)

## Entity EcoVolteru (real-time)

| Účel | Entita |
|---|---|
| Zapnout/vypnout nabíjení | `switch.ecovolter_revcr01c00002056_is_charging_enable` |
| Nastavit proud (A) | `number.ecovolter_revcr01c00002056_target_current` |
| Vozidlo připojeno | `binary_sensor.ecovolter_revcr01c00002056_is_vehicle_connected` |
| Aktuální výkon nabíjení | `sensor.ecovolter_revcr01c00002056_actual_power` (kW) |

## Entity KIA EV6 (kia_uvo — opožděné, aktualizace po skocích)

| Účel | Entita |
|---|---|
| SoC baterie EV (%) | `sensor.ev6_ev_battery_level` |
| Zástrčka zapojena | `binary_sensor.ev6_ev_battery_plug` |
| Právě nabíjí | `binary_sensor.ev6_ev_battery_charge` |
| AC nabíjecí limit (%) | `number.ev6_ac_charging_limit` |
| Výkon nabíjení dle KIA | `sensor.ev6_ev_charging_power` |

**Důležité:** KIA API nevrací real-time data — stav SoC se aktualizuje periodicky nebo
při událostech z KIA serverů. Pro okamžitou detekci výkonu používat EcoVolter
`actual_power`.

## Master kill-switch

`input_boolean.ev_nabijeni_povoleno` — vytvořen v HA GUI (2026-04-25).
Pokud OFF → žádná EV automatizace nic nespustí. Probíhající nabíjení zastav ručně přes EcoVolter entitu.

## Správa GoodWe export limitu při nabíjení EV

**Problém z praxe:** S limitem 0W GoodWe nevyrábí nad spotřebu domu. Když EV přidá
zátěž 4.1 kW, GoodWe tuto zátěž nepokryje ze solárů — místo toho vybíjí baterii.

**Řešení:** Při startu nabíjení za záporné ceny se `number.goodwe_limit_dodavky_do_site`
nastaví na 10000 W, aby se GoodWe rozběhl na plný solární výkon. Po ukončení nabíjení
se limit obnoví na 0.

| Událost | Akce na goodwe_limit |
|---|---|
| EV start při záporné ceně | `= 10000` — GoodWe se rozběhne na plný solár |
| EV stop při záporné ceně | `= 0` — obnoví omezení exportu |
| EV start/stop při přetocích (kladná cena) | beze změny |
| Cena přejde do kladné | `Disable Overflow` blueprint nastaví `= 10000` sám |

`Disable Overflow` blueprint je po dobu záporné ceny v `wait_template` (čeká na price > 0).
Naše změny limitu s ním nekolidují — reaguje jen na přechod ceny, ne na mezistav.

## Proč nelze použít `energy_sell` ani `pv_power` jako solární indikátor

`Disable Overflow` nastaví `goodwe_limit = 0` → GoodWe deratuje MPPT a nevyrábí nad
spotřebu domu → `energy_sell = 0` a `pv_power` taktéž klesá na nízkou hodnotu.
Oba sensory jsou tedy při záporné ceně nespolehlivé jako indikátor solárního potenciálu.

**Správný solární indikátor:** `sensor.solcast_pv_forecast_power_now` — externí Solcast
předpověď, nezávislá na GoodWe export limitu.

## Proč nelze použít `energy_buy` jako stop indikátor

Při nedostatku solárů GoodWe vybíjí baterii — `energy_buy` zůstává nízký i když
solár nestačí a baterie kryje EV.

**Správný stop/snižovací indikátor:** `sensor.fve_battery_discharge_w` — pokud baterie
vydává > 2000 W, solár nestačí a je třeba snížit nebo zastavit nabíjení.

---

## Přehled automatizací

Soubor: `config/automations.yaml`

| ID | Alias | Účel |
|---|---|---|
| `kia_ev6_force_update_on_charge` | KIA EV6: force update | Vyžádá čerstvý SoC z KIA API při startu nabíjení |
| `ev_nabijeni_zaporne_ceny_pretoky` | EV nabíjení - záporné ceny nebo přetoky | Hlavní řídící automatizace (start/stop) |
| `ev_zastavit_velky_import` | EV zastavit - příliš velký import | Záložní ochrana při importu ze sítě |
| `ev_zastavit_plna_baterie` | EV zastavit - auto přestalo nabíjet | Úklid po skončení nabíjení |
| `ev_regulace_proudu` | EV regulace proudu při záporné ceně | Dynamická regulace 6–10A podle přetoků a vybíjení baterie |

---

## Automatizace 1: `kia_ev6_force_update_on_charge`

**Účel:** Vyžádat čerstvý SoC z KIA API v momentě kdy EcoVolter potvrdí reálný odběr.
KIA server sám od sebe neví, že auto začalo nabíjet — je třeba aktivně vyžádat aktualizaci.

**Trigger:**
- `actual_power > 0.3 kW` po dobu 1 minuty

**Podmínka:**
- `this.attributes.last_triggered == none` nebo uplynulo > 3600 s od posledního spuštění
  — ochrana proti opakovanému volání při krátkých pauzách nabíjení

**Akce:**
- `kia_uvo.force_update`

**Mode:** `single`

---

## Automatizace 2: `ev_nabijeni_zaporne_ceny_pretoky`

Hlavní automatizace. Reaguje na čtyři triggery, každý spustí jinou větev `choose`.

**Mode:** `queued`, max: 5

**Triggery:**

| ID | Trigger |
|---|---|
| `pretoky_start` | `energy_sell > 4100 W` po dobu 2 min |
| `cena_zaporna` | `spot_price < 0` |
| `auto_pripojeno` | `is_vehicle_connected → on` |
| `cena_kladna` | `spot_price > 0` |

**Proměnné:**

| Proměnná | Výraz |
|---|---|
| `ma_nabijeni` | EcoVolter switch = on |
| `je_cena_zaporna` | spot_price < 0 |
| `je_pretoky_velke` | energy_sell > 4100 |
| `je_vozidlo_pripojeno` | is_vehicle_connected = on |
| `ev6_soc` | ev6_ev_battery_level (int, default 0) |
| `ev6_limit` | ev6_ac_charging_limit (int, default 80) |
| `ev6_soc_ok` | ev6_battery_level není unavailable/unknown |
| `je_povoleno` | ev_nabijeni_povoleno = on |
| `je_solar_dost` | solcast_pv_forecast_power_now > 1000 W |

**Globální podmínka:** `je_povoleno` — master kill-switch

---

### Větev A: `pretoky_start`

**Podmínky:** `not ma_nabijeni` AND `je_vozidlo_pripojeno` AND `(not ev6_soc_ok OR ev6_soc < ev6_limit)`

```
1. Nastav proud na 6A
2. Zapni nabíjení
3. Čekej max 90s na actual_power > 0.3 kW
   → timeout → Vypni + notify "Auto nepřijímá nabíjení (přetoky)"
   → OK      → notify "Přetoky >4.1 kW. Nabíjení EV 6A aktivní"
```

*goodwe_limit se nemění — při přetocích (kladná cena) GoodWe exportuje normálně.*

---

### Větev B: `cena_zaporna`

**Podmínky:** `not ma_nabijeni` AND `je_vozidlo_pripojeno` AND `je_solar_dost` AND `(not ev6_soc_ok OR ev6_soc < ev6_limit)`

```
1. goodwe_limit = 10000  → GoodWe se rozběhne na plný solár
2. Nastav proud na 6A
3. Zapni nabíjení
4. Čekej max 90s na actual_power > 0.3 kW
   → timeout → Vypni + goodwe_limit = 0 + notify "Auto nepřijímá nabíjení"
   → OK      → Čekej 45s (GoodWe se ustálí)
               → energy_buy > 1500 W NEBO fve_battery_discharge_w > 3000 W?
                  ANO → Vypni + goodwe_limit = 0 + notify "Nedostatek solárů"
                  NE  → notify "Záporná cena + solár. Nabíjení EV 6A aktivní"
```

---

### Větev C: `auto_pripojeno`

**Podmínky:** `(not ev6_soc_ok OR ev6_soc < ev6_limit)` AND (`je_pretoky_velke` OR `je_cena_zaporna AND je_solar_dost`)

```
1. Pokud je_cena_zaporna → goodwe_limit = 10000
2. Nastav proud na 6A
3. Zapni nabíjení
4. Čekej max 90s na actual_power > 0.3 kW
   → timeout → Vypni
               Pokud je_cena_zaporna → goodwe_limit = 0
               notify "Auto nepřijímá nabíjení (připojení vozu)"
   → OK      → notify "Auto připojeno, podmínky splněny. Nabíjení EV 6A aktivní"
```

---

### Větev D: `cena_kladna`

**Podmínky:** `ma_nabijeni` AND `not je_pretoky_velke`

```
1. Vypni nabíjení
2. notify "Cena kladná a přetoky <4.1 kW. Nabíjení EV zastaveno"
```

*goodwe_limit se nemění — Disable Overflow blueprint ho nastaví na 10000 sám.*

---

## Automatizace 3: `ev_zastavit_velky_import`

**Účel:** Záložní ochrana při importu ze sítě (doplněk k regulátoru proudu).

**Trigger:** `energy_buy > 1500 W` po dobu 2 minut

**Podmínky:** EcoVolter switch = on AND `ev_nabijeni_povoleno` = on

**Akce:**
```
1. Vypni nabíjení
2. Pokud spot_price < 0 → goodwe_limit = 0
3. notify "Import > 1.5 kW po dobu 2 min"
```

**Mode:** `single`

---

## Automatizace 4: `ev_zastavit_plna_baterie`

**Účel:** Detekuje že auto přestalo přijímat proud a uklidí goodwe_limit.

**Trigger:** `actual_power < 0.3 kW` po dobu 5 minut

**Podmínky:** EcoVolter switch = on AND `ev_nabijeni_povoleno` = on

**Akce:**
```
1. Vypni nabíjení (EcoVolter switch OFF)
2. Pokud spot_price < 0 → goodwe_limit = 0
3. notify "Auto přestalo přijímat proud. Nabíjení ukončeno"
```

**Mode:** `single`

*5 minut záměrně — ochrana před krátkými pauzami při přepnutí fáze.*

---

## Automatizace 5: `ev_regulace_proudu`

**Účel:** Každé 2 minuty dynamicky upravuje nabíjecí proud 6–10A aby EV pohltilo
solární přebytek a baterie se zbytečně nevybíjela.

Maximální proud 10A odpovídá výkonu panelů (7 kW ÷ 230 V × 3 = 10.1 A).

**Trigger:** `time_pattern` každé 2 minuty

**Podmínky:**
- EcoVolter switch = on
- `ev_nabijeni_povoleno` = on
- `spot_price < 0`

**Proměnné:**
- `aktualni_proud` — aktuálně nastavený proud (A)
- `energy_sell` — aktuální export do sítě (W)
- `battery_discharge` — aktuální vybíjení baterie (W)

**Logika (choose — první splněná větev):**

| Podmínka | Akce |
|---|---|
| `energy_sell > 500 W` a proud < 10A | Zvýš proud o 1A (max 10A) |
| `battery_discharge > 2000 W` a proud > 6A | Sniž proud o 1A (min 6A) |
| `battery_discharge > 2000 W` a proud = 6A | Stop + goodwe_limit = 0 + notify |

*`energy_sell` je zde spolehlivý — goodwe_limit = 10000 je nastaveno, GoodWe exportuje.*

**Mode:** `single`

---

## Přehled scénářů

| Scénář | Výsledek |
|---|---|
| Záporná cena, Solcast > 1 kW, auto připojeno | Větev B: goodwe=10000, zapne 6A, probe, regulace |
| Záporná cena, noc (Solcast = 0 W) | Nezapne — `je_solar_dost` = false |
| Záporná cena, zataženo (Solcast < 1 kW) | Nezapne — `je_solar_dost` = false |
| Záporná cena, solár nestačí po spuštění | Probe zastaví (battery_discharge > 3000 W) |
| Přetoky > 4.1 kW, kladná cena | Větev A: zapne 6A, probe |
| Auto připojeno za záporné ceny + solár | Větev C: zapne jako větev B |
| Auto připojeno za přetoků | Větev C: zapne jako větev A |
| Cena přejde do kladné, přetoky < 4.1 kW | Větev D: zastaví |
| Cena přejde do kladné, přetoky > 4.1 kW | Větev D nespustí → nabíjení pokračuje |
| Přebytek solárů při nabíjení | Regulátor zvýší proud (až 10A) |
| Baterie začne krýt EV (discharge > 2 kW) | Regulátor sníží proud, nebo zastaví při 6A |
| Auto samo přestalo nabíjet | `ev_zastavit_plna_baterie`: po 5 min → stop + goodwe=0 |
| `ev_nabijeni_povoleno` = OFF | Žádná automatizace nic nespustí |

## Helper

`input_boolean.ev_nabijeni_povoleno` — vytvořen v HA GUI (2026-04-25).
Nelze spravovat přes YAML (input helpers nejdou do entity_registry přes YAML).
