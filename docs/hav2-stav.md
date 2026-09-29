# HAv2 – stav a předávka (k 29. 9. 2026)

Zadání: `docs/HAv2_prompt.md` · Návrh (schválený): `docs/hav2-architektura.md` · Záloha v1 a plán mazání: `archive/v1-2026-09-25/README.md`

## 1. Kde jsme

| Fáze | Stav |
|---|---|
| 1 – inventura | hotovo |
| Krok 0 – záloha v1 | hotovo (HA záloha `pre-HAv2-2026-09-25` id `9740260a`, archiv `archive/v1-2026-09-25/`, commit 88482e2) |
| Krok A – statistiky a náklady | hotovo (`packages/hav2_statistics.yaml`, GUI utility metery, Energy dashboard s cenami) |
| 2 – návrh | schváleno 25. 9. 2026 |
| 3 – implementace | **hotovo** – datová vrstva, baterie, EV, bazén, dashboard `energie-v2` |
| 4 – ověření a převzetí řízení | **test Auto běží od 26. 9. 2026 13:58** (víkend + svátek 28. 9.), všechny oblasti |

**Aktuální režim (test):** `input_select.energy_system_mode` = **Auto**, přepínače
`input_boolean.energy_battery_control`, `ev_control`, `pool_control` **zapnuté**. DoD 80 %.
Staré automatizace v1 jsou **vypnuté, ne smazané** (16 ks: TTUO, Auto Set DoD, Fully Charge Once a Week,
Discharge to Grid, Eco Discharge ×2, Disable Overflow, Prediktivní přetoky, 7× `ev_*`, Ovládání filtrace)
a `input_boolean.time_to_use_overflows` = off. HA záloha před testem `81c05fcd`.
**Návrat:** režim „Jen doporučení“ + zapnout uvedené automatizace a TTUO (limit přetoku se vrátí sám).
Po testu rozhodnout: smazat v1 (podle archivu) nebo vrátit.

**Data z testu:** minutový záznam `config/appdaemon/hav2_data/RRRR-MM-DD.jsonl` (AppDaemon, 60 dní,
stáhne `make pull`, mimo git) + historie HA + PND D+1.

## 1b. Sledovat v příští session (stav 29. 9. 2026)

Test Auto běží dál (volno do 30. 9.). Kontroly: `make pull` → `config/appdaemon/hav2_data/RRRR-MM-DD.jsonl`
(minutový záznam, klíče viz `DATA_STATES`/`DATA_ATTRS` v `hav2_app.py`), PND D+1 v `sensor.energy_boiler_pnd_daily`
(`hours_json`, `export_loss_kwh`; hodinově, PND export/import po hodinách = statistiky `cez_pnd:859182400703740246_production/consumption`).

1. **Bojler – energetická dávka (nasazeno 29. 9. 08:47, commit a3e2b2d):** na prvním slunečném dni s plnou baterií
   a připojeným EV sledovat `boiler_gate` / `boiler_reserve_w` (jsonl, `sensor.ev_regulator`) a druhý den PND:
   dostal bojler v poledne víc než 0,74 kWh (28. 9.)? Zkrátil se ohřev ze sítě 16–19 h? Nešel přetok do sítě
   zbytečně (bojler plný, rezerva drží)? Doladit `BoilerParams.budget_share` (0,6), případně okno 10:00–15:30.
2. **Večerní prodej z baterie (od 28. 9., commit 75fb3b3 + 7d07dce):** 28. 9. prodáno 5,2 kWh (18:01–18:16, 19:04–20:03),
   PND export sedí, tržba ~34,8 Kč, čistě ~18,6 Kč. Při další špičce ≥ 7,2 Kč ověřit start/konec přesně na čtvrthodině
   (přepočet plánu v :00:10/:15:10/…) a že ráno ve VT nechybí baterie.
3. **EV:** cíl 80 %, termín 31. 10. 12:00 → nabíjí jen ze slunce, NT až poslední noc 30./31. 10. (zkontrolovat, že
   plán „NT později“ drží). Kmitání proudu ±1 A/min (trouba) – zatím neřešeno.
4. **Filtrace:** oprava posledního krátkého běhu (min. 15 min, jen do splnění) a doby běhu po restartu – ověřeno 28. 9.
5. **Nenastalo, počkat:** záporné ceny bez zájmu EV (odložení nabíjení, limit přetoku 0 W), 3f z plné baterie,
   NT nabíjení baterie před zataženým dnem, test watchdogu (zastavit AppDaemon ~4 min – jen se souhlasem).
6. **Rozhodnutí po testu (uživatel):** převzít řízení natrvalo (záloha → smazat 16 vypnutých automatizací v1 podle
   `archive/v1-2026-09-25/README.md`, statistiky `sensor.ev_nt_*` ponechat – tabulka vyúčtování je používá), nebo návrat.
7. **Hardware (návrh uživateli):** měření okruhu bojleru – Shelly EM Gen3 / Pro EM-50 s klešťovým snímačem (+ druhý
   snímač na L3 za elektroměrem = skutečný přetok, podle kterého spíná WATTrouter). Uživatel zvažuje; do té doby dávka (C).
   Dále: přes USB a WATTconfig ECO opsat nastavení WATTrouteru (režim regulace součet/fáze, relé bojleru, časové plány,
   CombiWATT) – nucený ohřev 16:09 je nejspíš časový plán „Vynutit“.

## 2. Mapa systému

### Home Assistant (YAML, `make push`)
| Soubor | Obsah |
|---|---|
| `config/packages/hav2_statistics.yaml` | ceny, náklady, EV náklady podle zdroje, denní snímky, odchylka proti PND |
| `config/packages/hav2_helpers.yaml` | všechny ovládací helpery (bez `initial`) |
| `config/packages/hav2_data.yaml` | přebytek, fázové rezervy, jistič, bojler (napěťový index), opravená předpověď FVE, EV SOC/potřeba, teplota vody, EV souhrny nákladů |
| `config/packages/hav2_battery.yaml` | `script.hav2_battery_set` + watchdog |
| `config/packages/hav2_ev.yaml` | `script.hav2_ev_set` + watchdog + `input_boolean.ev_control` |
| `config/packages/hav2_pool.yaml` | `script.hav2_pool_set` + watchdog + `input_boolean.pool_control` |
| `config/custom_templates/hav2.jinja` | korekce Solcastu (měsíc, hodina, konzervativní faktor) |

Výkonné skripty zapisují **jen** při `energy_system_mode = Auto` a zapnutém přepínači oblasti,
jinak jen zapíšou do logbooku „Nezapsáno“.

### AppDaemon (`config/appdaemon/apps/hav2/`, `make push`, reload automaticky)
| Soubor | Obsah |
|---|---|
| `hav2.yaml` | konfigurace app (samostatně – `apps.yaml` je gitignored kvůli heslům PND) |
| `hav2_app.py` | app `Hav2`: profil spotřeby, plán baterie každých 15 min + při změně vstupů, heartbeat |
| `hav2_planner.py` | plánovač baterie (čistý Python) + `hourly_table` pro dashboard |
| `hav2_ev.py` / `hav2_ev_ctl.py` | EV plán + regulátor proudu (smyčka 5 s) / napojení na HA |
| `hav2_pool.py` / `hav2_pool_ctl.py` | řízení filtrace (smyčka 60 s) / napojení na HA |
| `hav2_boiler.py` | bojler a zbytková odchylka z PND (volá `Hav2.pnd_check` v 07:30 a po stažení PND) |

Publikuje: `sensor.energy_plan`, `sensor.energy_export_control`, `sensor.energy_load_forecast`, `sensor.ev_plan`, `sensor.ev_regulator`,
`sensor.pool_plan`, `sensor.pool_controller`, `sensor.energy_boiler_pnd_daily`, `input_text.*_last_decision`, `input_datetime.hav2_heartbeat`.

Testy: `source venv/bin/activate && pytest tests/hav2 -q --no-cov` (65 testů).

### Dashboard `energie-v2`
Zdroj pravdy `dashboards/energie-v2.yaml`, nahrání:
```
source venv/bin/activate && set -a && source .env && set +a
python dashboards/push_dashboard.py energie-v2 dashboards/energie-v2.yaml
```
HACS karty: power-flow-card-plus, apexcharts-card, flex-table-card, auto-entities, card-mod, mushroom.

## 3. Úskalí (důležité pro další práci)

- **Fakturace po fázích** – nikdy součtový výkon/čítače; základ `energy_buy/sell` (Σ fází).
- **Bojler (WATrouter, 2,2 kW, L3) je mimo měření GoodWe** – baterie ho nekryje; nucený ohřev denně od 16:09; model `grid_only`. Skutečnou denní spotřebu dává `sensor.energy_boiler_pnd_daily` (PND − GoodWe, D+1; průměr 4,2 kWh / 22 Kč/den). Detektor `binary_sensor.energy_boiler_heating` (napětí L3) je jen přibližný (denní součty ±1 kWh); prahy: 16–19 h od 1,0 V, jinak od 2,5 V, vypnout pod 0,5 V; při EV 1f index L2 − L3.
- **Senzory `energy_pnd_deviation_*` jsou bez bojleru** (zbytek rozdílu HA↔PND, počítá AppDaemon). Odchylka prodeje v % je při malém prodeji zkreslená rozlišením GoodWe 0,1 kWh.
- Validátor referencí zná i entity z AppDaemonu (hledá `set_state("…")` v `config/appdaemon/apps`). Testy validátoru: `PYTHONPATH=. pytest -o addopts="" tests/test_reference_validator.py` (ve venv chybí coverage).
- **GoodWe EMS:** `conserve` nabíjí i ze sítě – nepoužívat; „drž SOC“ = `battery_standby`.
- **AppDaemon `set_state` zahazuje falsy hodnoty** (0, False, i v seznamech) → čísla a logické hodnoty v atributech jako text, seznamy řádků s textovými hodnotami, nebo JSON řetězec.
- AppDaemon: nová podsložka apps se načte až po restartu doplňku; `log:` jen s definicí v `appdaemon.yaml`; `logbook.log` bez `entity_id`.
- Trigger šablony: `this` nejde ve `variables`.
- Nové YAML platformy (statistics, history_stats, integration) potřebují restart HA.
- Utility metery vždy přes GUI (YAML neprojde validátorem).
- Markdown karty: obsah jako `|` (literal), ne `>` – jinak se rozbijí tabulky.
- Dashboard dlaždice: vždy explicitní `grid_options`, jinak se v sekcích „rozsypou“.
- Štítky: nové HAv2 entity vždy `hav2` + oblast (`hav2_system` / `hav2_baterie` / `hav2_ev` / `hav2_bazen`) – výjimka v CLAUDE.md. Senzory z AppDaemonu štítek mít nemůžou.
- **cez_pnd (HACS) v1.1.2** (nainstalováno 27. 9. 22:26) funguje **bez lokálního patche** – opravuje účty bez `idDeviceSet`;
  ověřeno 28. 9.: stav OK, 96 záznamů/den, denní spotřeba i výroba sedí s AppDaemon PND app. Starý patch pro 1.1.1 už neplatí.
- Citlivé: `.env` (HA_TOKEN), `config/appdaemon/apps/apps.yaml` (PND heslo) – nikdy nevypisovat ani necommitovat. (Testovací údaje v Keychain `cez_pnd_test` smazány 25. 9. 2026.)

## 3b. Zjištění z testu Auto (26.–29. 9. 2026)

- **WATTrouter ECO** (manuál: WATTrouter ECO WRE 01/06/14): bojler na **reléovém výstupu** – sepne, až přebytek na jeho
  měřicím modulu (za elektroměrem distributora, před bojlerem) převýší příkon ~2,2 kW, a **vypne, když se zapne jiný
  spotřebič** (28. 9. 14:33 ho vypnul start EV). Bojler je mezi elektroměrem a měřením GoodWe → GoodWe vidí přetok i s bojlerem.
  Prodej z baterie večer WATTrouter nespustil (bojler nechtěl hřát); PND export ≈ GoodWe export.
- **Napěťový detektor bojleru** (`binary_sensor.energy_boiler_heating`) je v poledne a při exportu nespolehlivý (skoky napětí
  ±1,3–3,7 V z okolní sítě jsou stejně velké jako sepnutí bojleru); spolehlivý jen večer bez exportu. Proto „nahřátý“
  podle energetické dávky (architektura §5.4).
- **Hodnota kWh:** bojler v poledne ušetří VT 6,10 Kč, EV jen NT 3,51 Kč; plný bojler + rezerva = ztráta NT − výkup
  (při záporném výkupu ještě víc) → rezerva se vyplatí jen s rozumnou jistotou, že bojler bere.
- **Baterie:** přes noc bez NT nabíjení, správně (FVE další den dobije). Večerní prodej ve špičce od 28. 9.
- **EV:** NT nabíjení 28./29. 9. 22:00–00:18, 3f 11 A ~6,8 kW, 15,45 kWh / 54 Kč, baterie domu v `battery_standby`,
  jistič min. rezerva 13,2 A. Tabulka vyúčtování NT (`sensor.ev_nt_billing`, stránka EV náklady) sčítá HAv2 + v1.
- **Opraveno během testu (26.–29. 9.):** NT plán EV v poslední NT před termínem; rezerva baterie před EV; plné nabití
  baterie 1× za 30 dní; doporučení hodin filtrace; příznak bojleru v `input_datetime.energy_boiler_last_full`
  (přežije restart); EV při rezervě bojleru nedotuje z baterie; krátký poslední běh filtrace; NT končí v 06:00 (ne 06:00:01);
  přepočet plánu na hranicích čtvrthodin; atributy AppDaemon senzorů `replace=True`; přesnost `energy_buy_sum`/`sell_sum`
  na 3 desetinná místa (uživatel v GUI 28. 9.).
- PND v HA jen **hodinově** (15min data integrace hned agreguje). cez_pnd v1.1.2 funguje bez lokálního patche.

## 4. Další kroky

Aktuální seznam sledování a rozhodnutí je v **§1b**. Dlouhodobě:
1. **Bojler:** řada `sensor.energy_boiler_pnd_daily` (denně, z toho z přetoku) → roční přínos měření (Shelly) nebo přepojení
   bojleru za měření GoodWe (elektrikář, odhad 5–8 tis. Kč/rok).
2. **Převzetí řízení** (se schválením): záloha HA → smazat automatizace v1 (jako první `ev_blokovat_vybijeni_baterie`,
   `predictive_overflow_negative_price`) → baterie DoD 80 % → Auto zůstává.
3. **Po převzetí:** smazat helpery „nepotřebujeme“ (archiv README). AppDaemon PND app vypnout až po přepojení
   `hav2_boiler.py` na data HACS integrace a ověření VT/NT.
4. Volitelně: rozvržení stránky EV (sekce „Aktuální session“ k ručnímu ovládání), sankey na Úsporách, Solcast
   auto-dampening, duplicitní `input_number.battery_capacity`, zbytečná zpráva „bojler: mimo okno“ o půlnoci.

## 5. Známé drobnosti
- `sensor.pool_water_temperature` a `sensor.pool_hours_recommended` mají hodnotu až po ≥ 10 min běhu filtrace.
- `sensor.ev_deadline_slack_h` je nedostupný bez zadaného termínu (záměr; na dashboardu skrytý).
- Solcast API 10/10 denně vyčerpáno – auto-update integrace; při výpadku konzervativní režim (× 0,5).
- EV náklady se počítají od 25. 9. 2026; historie v1 (ruční NT) je v `sensor.ev_nt_*`.
