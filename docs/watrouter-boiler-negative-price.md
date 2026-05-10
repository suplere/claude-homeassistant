# WATrouter bojler – optimalizace při záporné ceně

## Kontext

**Hardware:**
- WATrouter ECO – bez síťového připojení, nelze integrovat do HA
- Bojler Dražice 148L, 2,2 kW, jednofázový (230V)
- GoodWe střídač s baterií

**Problém:**
Při záporné spot ceně automatizace "Přetoky nastavit" nastaví `grid_export_limit = 200W`.
GoodWe se throttluje → WATrouter nevidí přetoky → bojler se neohřeje → solární energie se zahodí.

**Cíl:**
- Nulový/minimální export do sítě při záporné ceně (aby uživatel neplatil)
- WATrouter má stále dostatek "signálu" (přetoků) aby ohříval bojler

---

## Řešení: Adaptivní export limit

Zvýšit limit na 2500W (nad kapacitu bojleru 2,2kW) → WATrouter vidí přetoky → hřeje bojler.
Monitorovací automatizace detekuje přes `sensor.energy_sell`, kdy je bojler plný, a limit sníží.

### Logika

```
Záporná cena → limit 2500W → WATrouter hřeje bojler (~2,2 kW)
     ↓
Export > 300W po 10 min + baterie > 90%
→ bojler i baterie plné → limit na 0W + notifikace
     ↓
Každou hodinu "Přetoky nastavit" obnoví limit na 2500W → WATrouter zkusí znovu
     ↓
Pokud bojler stále plný → monitoring znovu sníží na 0W
```

Přijatelná ztráta: ~10 min × 2,2 kW × 3 Kč/kWh = **~1,1 Kč/hodinu** (vs. hodiny ztraceného výkonu dnes)

---

## Co je potřeba udělat

### Změna 1 – Upravit automatizaci "Přetoky nastavit"

**Soubor:** `config/automations.yaml`, id `1717828775243`

Změnit `value: 200` → `value: 2500` a aktualizovat text notifikace:

```yaml
# PŘED:
        parameter: grid_export_limit
        value: 200
      action: goodwe.set_parameter
    - action: notify.mobile_app_evzen_iphone
      data:
        title: Omezený výkon FVE
        message: Jsou záporné ceny, nastaven omezený výkon elektrárny.

# PO:
        parameter: grid_export_limit
        value: 2500
      action: goodwe.set_parameter
    - action: notify.mobile_app_evzen_iphone
      data:
        title: Omezený výkon FVE
        message: Jsou záporné ceny, export omezen na 2500W (WATrouter hřeje bojler).
```

---

### Změna 2 – Přidat novou automatizaci

**Soubor:** `config/automations.yaml` – přidat na konec

```yaml
- id: boiler_negative_price_monitor
  alias: Bojler - monitorování záporné ceny
  description: >
    Při záporné ceně hlídá export do sítě jako proxy pro stav bojleru.
    Pokud je export > 300W déle než 10 min a baterie > 90% → bojler i baterie
    jsou plné → omezí GoodWe na 0W. "Přetoky nastavit" obnoví limit každou hodinu.
  mode: restart
  triggers:
    - trigger: numeric_state
      entity_id: sensor.energy_sell
      above: 300
      for:
        minutes: 10
  conditions:
    - condition: numeric_state
      entity_id: sensor.current_spot_electricity_price
      below: 0
    - condition: numeric_state
      entity_id: sensor.battery_state_of_charge
      above: 90
  actions:
    - action: notify.mobile_app_evzen_iphone
      data:
        title: Bojler plný - omezuji export
        message: >
          Export {{ states('sensor.energy_sell') | int }}W po dobu 10 min,
          baterie {{ states('sensor.battery_state_of_charge') | int }}%.
          Omezuji GoodWe na 0W. Limit se obnoví při příští změně hodiny.
    - data:
        device_id: ebb0a36fdc7eb73781df6cf51cf4448f
        parameter: grid_export_limit
        value: 0
      action: goodwe.set_parameter
```

---

## Testy (provést za slunečného dne)

### Test 1 – Ověřit sensor.energy_sell
**Cíl:** Potvrdit, že sensor ukazuje kladné hodnoty při exportu do sítě.

1. Za slunečného rána (přetoky jdou do sítě)
2. Otevřít Developer Tools → States v HA
3. Najít `sensor.energy_sell` – měl by ukazovat kladné Watty při exportu
4. Pokud je hodnota záporná nebo opačná → upravit podmínku v automatizaci na `below: -300`

### Test 2 – Ruční simulace záporné ceny
**Cíl:** Ověřit, že GoodWe přijme nový limit 2500W a WATrouter začne hřát bojler.

1. Za slunečného dne (alespoň 2500W přetoků)
2. Ručně spustit akci:
   ```
   goodwe.set_parameter
   device_id: ebb0a36fdc7eb73781df6cf51cf4448f
   parameter: grid_export_limit
   value: 2500
   ```
3. Sledovat `sensor.energy_sell` – měl by klesnout (WATrouter absorbuje)
4. Zkontrolovat fyzicky, zda WATrouter svítí (indikátor ohřevu)

### Test 3 – Zjistit na které fázi je bojler (DŮLEŽITÉ)
**Cíl:** V ČR se export účtuje po fázích, nikoliv celkově. Pokud WATrouter hřeje bojler
na L1, ale přetoky jsou na L2+L3, nic to neřeší a platíš za export na L2/L3 stejně.

1. Za slunečného dne, WATrouter aktivní (hřeje bojler)
2. V HA Developer Tools → States sledovat současně:
   - `sensor.active_power_l1`
   - `sensor.active_power_l2`
   - `sensor.active_power_l3`
3. Zjistit konvenci: kladná = export nebo import? (porovnat se `sensor.energy_sell`)
4. Identifikovat fázi bojleru: když WATrouter zapne ohřev, na které fázi vzroste spotřeba
   (nebo klesne export)?
5. **Výsledek zapsat sem** – na základě toho rozhodnout:
   - Pokud bojler na stejné fázi jako přetoky → stávající logika funguje
   - Pokud na jiné fázi → zvážit přepojení, nebo monitoring upravit
     na fázově-specifický sensor místo `sensor.energy_sell`

**Proč to vadí:** Příklad – solar vyrábí 3 kW na L1, bojler bere 2,2 kW na L3.
Na L1 exportuješ 3 kW (platíš), na L3 importuješ 2,2 kW (platíš) → dvojitá ztráta.
WATrouter by měl být fyzicky zapojen na stejnou fázi jako největší přetoky.

### Test 4 – Celý průběh při záporné ceně
**Cíl:** Ověřit end-to-end chování.

1. Počkat na zápornou spot cenu (nebo simulovat snížením podmínky na `below: 0.5`)
2. Sledovat notifikace na iPhone
3. Sledovat `sensor.energy_sell` a `sensor.battery_state_of_charge` v historii
4. Ověřit, že monitoring zasáhne ve správný moment

---

## Stav senzorů – zjištěno

### sensor.energy_sell / energy_buy – per-phase billing je řešen správně

Oba sensory jsou **template** (ne přímý GoodWe výstup). Formule:
```
energy_sell = active_power_l1 (>0) + active_power_l2 (>0) + active_power_l3 (>0)
energy_buy  = |active_power_l1 (<0)| + |active_power_l2 (<0)| + |active_power_l3 (<0)|
```
Klíčové: nekompenzuje cross-phase. L1 export se nesčítá s L2 importem → **per-phase billing
ČR je správně vyřešen**.

### Dvě sady fázových senzorů – neověřeno

| Senzor | Platforma | Zdroj |
|--------|-----------|-------|
| `sensor.active_power_l1/l2/l3` | goodwe | GoodWe invertor (vlastní měření) |
| `sensor.meter_active_power_l1/l2/l3` | goodwe | **Externí měřič** (Hager ECP280T nebo podobný) přes RS485 |

`energy_sell/buy` template aktuálně používá `active_power_*` (invertorová měření).
`meter_active_power_*` (měřič na přívodu ze sítě) jsou v HA dostupné, ale v žádné
template zatím nepoužity. Znaménková konvence `meter_*` senzorů není ověřena.

### Test 0 – Ověřit která sada senzorů je přesnější (provést za slunečného dne)

**Cíl:** Zjistit zda `active_power_l1/l2/l3` nebo `meter_active_power_l1/l2/l3` lépe
odpovídají skutečnému toku ze/do sítě, a jaká je znaménková konvence `meter_*` senzorů.

1. Za slunečného dne kdy jsou přetoky (invertor exportuje)
2. V HA Developer Tools → States sledovat současně:
   - `sensor.active_power_l1`, `sensor.active_power_l2`, `sensor.active_power_l3`
   - `sensor.meter_active_power_l1`, `sensor.meter_active_power_l2`, `sensor.meter_active_power_l3`
   - `sensor.energy_sell` (výsledný template)
3. Ověřit:
   - Souhlasí celkový `energy_sell` s tím, co vidíš na elektroměru/GoodWe SEMS?
   - Jsou hodnoty `active_power_*` a `meter_active_power_*` shodné nebo jiné?
   - Jaké znaménko má `meter_active_power_l1` při exportu (+ nebo -)?
4. Pokud se liší → `energy_sell/buy` template možná přepsat aby používal `meter_active_power_*`

---

## Otevřené otázky

- **Baterie threshold 90%** – potvrdit, že je správné číslo (nebo upravit)
- **sensor.energy_sell znaménko** – ověřit v Testu 1 (očekáváme kladné při exportu)
- **active_power vs meter_active_power** – ověřit v Testu 0, která sada je správná
- **Fáze bojleru** – zjistit z Testu 3, případně zvážit přepojení WATrouteru
- **WATrouter signalizace** – zjistit, zda WATrouter ECO má LED indikátor, který lze vizuálně ověřit
