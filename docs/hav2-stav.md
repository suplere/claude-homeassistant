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

## 1b. Pro novou session (stav 1. 10. 2026 večer)

Kontroly: `make pull` → `config/appdaemon/hav2_data/RRRR-MM-DD.jsonl` (minutový záznam, klíče `DATA_STATES`/`DATA_ATTRS`
v `hav2_app.py`, nově `pool_run`, `pool_w`), PND D+1 v `sensor.energy_boiler_pnd_daily`, měření Shelly (níže).
Nové entity v HA vidí AppDaemon až po **restartu doplňku** (~4 min, watchdog pošle notifikaci – předem říct uživateli).

### A. Hned 2. 10. ráno
1. **Po 06:00 smazat přechodný `sensor.pool_hours_done_legacy`** (history_stats v `hav2_data.yaml`) + jeho sčítání
   v `sensor.pool_hours_done` (template) a v `base_final_daily_house_consumption` (`configuration.yaml`). Pak uživatel
   smaže zařízení Shelly Pro 1PM v UI (dřív ne – legacy čte jeho historii).
2. **Noc 1./2. 10. – souběh EV a bojleru:** EV v NT 22–23 h (cíl 100 %, termín 2. 10. 6:00, ~5,9 kWh) + nucený ohřev
   bojleru od 22:00. Ověřit z jsonl/historie: `breaker_headroom_a` nikdy < 2 A, strop EV 8 A ve 3f při `boiler_on`,
   že EV do 6:00 nabilo, a jednorázový cíl nad limitem auta (bod C2).
3. **Bojler v noci podle měření:** `sensor.shellyproem50_ece334fd2370_energy_meter_0_energie` – kolik vzal 22–06 h
   (večer 19–20 h už dostal 1,32 kWh z prodeje baterie). Porovnat s PND D+1 za 1. 10.

### B. Rozpracované – čeká na uživatele
1. **Bojler bere při prodeji z baterie (1. 10.):** prodej 19–20 h 5 kW → WATTrouter poslal do bojleru 1,32 kWh (~1,35 kW,
   celou L3), do sítě jen ~2,9 kWh místo 4,2 → ztráta ~3,6 Kč (výkup 6,24 vs. NT 3,51). Návrh: beznapěťový kontakt I/O
   bojlerového Pro EM-50 (`switch.shellyproem50_ece334fd2370`, nepoužitý) → vstup WATTrouteru **LT–GND**, ve WATTconfigu
   časový plán SSR3 typu „blokovat“ 00–24 s podmínkou „Binární vstup“; HAv2 sepne relé při `plan = discharge`
   (případně i jindy). **Uživatel pošle screenshot roletek** v záložce Časové plány (typ plánu u „vynutit“, „Binární
   vstup → žádný“). Bez hardwaru alternativa: plánovač počítá při prodeji ~⅓ přetoku do bojleru za hodnotu NT.
2. **Rozhodnutí po testu Auto (uživatel):** převzít řízení natrvalo (záloha → smazat 16 vypnutých automatizací v1 podle
   `archive/v1-2026-09-25/README.md`; pozor – v1 automatizace už odkazují na nový spínač filtrace; statistiky
   `sensor.ev_nt_*` ponechat), nebo návrat.

### C. Ověřit, až nastane
1. **Záporný výkup s plnou baterií** (nové 1. 10.): limit přetoku = příkon bojleru + 600 W (`hav2_boiler.NegPriceBoiler`)
   – náběh bojleru ~200 W/min, PND export ~0, po nahřátí limit 0 W a pokus po hodině; logbook „HAv2 přetok“.
2. **EV – jednorázový cíl nad limitem auta (29. 9.):** `sensor.ev_target_status`, zápis `number.ev6_ac_charging_limit`
   (Kia cloud), po nabití návrat limitu 90 % a cíle 80 %. Probíhá teď (cíl 100 %, termín 2. 10. 6:00).
3. **Filtrace – nesoulad s venkovními vypínači:** první notifikace „vypnuto vypínačem“ / „běží ručně“
   (`switch_mismatch` v `sensor.pool_controller`).
4. **Večerní prodej z baterie:** start/konec na čtvrthodině, ráno ve VT nechybí baterie (1. 10.: prodej 19:00–20:00 OK).
5. **Profil bojleru v plánu** se přeučuje z 16–18 h na noc (medián 8 dní, měření Shelly + PND) – do ~5. 10. plán
   nadhodnocuje večerní nákup; atribut `boiler_source` v `sensor.energy_load_forecast`.
6. Nenastalo: 3f z plné baterie, NT nabíjení baterie před zataženým dnem, test watchdogu (jen se souhlasem).
7. FVE ranní stín: od listopadu přepočítat hodinový tvar (`hav2.jinja`, poměr `sensor.pv_power` / Solcast this_hour).

### D. Nápady / později
- Kmitání proudu EV ±1 A/min (trouba).
- Energy dashboard: bojler záměrně ne (je mimo měření GoodWe → nesmyslná spotřeba domu); grafy kWh/den na Energie v2
  (Bazén, Úspory).
- Druhý kanál (IB) obou Pro EM-50 volný.

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

Testy: `source venv/bin/activate && pytest tests/hav2 -q --no-cov` (92 testů).

### Dashboard `energie-v2`
Zdroj pravdy `dashboards/energie-v2.yaml`, nahrání:
```
source venv/bin/activate && set -a && source .env && set +a
python dashboards/push_dashboard.py energie-v2 dashboards/energie-v2.yaml
```
HACS karty: power-flow-card-plus, apexcharts-card, flex-table-card, auto-entities, card-mod, mushroom.

## 3. Úskalí (důležité pro další práci)

- **Fakturace po fázích** – nikdy součtový výkon/čítače; základ `energy_buy/sell` (Σ fází).
- **Bojler (WATrouter, 2,2 kW, L3) je mimo měření GoodWe** – baterie ho nekryje; nucený ohřev **od 29. 9. 22:00–06:00** (WATTrouter, dříve 16–19 h ve VT); z přetoku jen plynule podle přetoku L3; model `grid_only` (profil z PND za 8 dní – přesune se do noci sám za ~4–5 dní); odhad příkonu `sensor.energy_boiler_power_w`. Skutečnou denní spotřebu dává `sensor.energy_boiler_pnd_daily` (PND − GoodWe, D+1; průměr 4,2 kWh / 22 Kč/den). Detektor `binary_sensor.energy_boiler_heating` (napětí L3) je jen přibližný (denní součty ±1 kWh); prahy: 22–06 h od 1,0 V, 12–20 h od 2,5 V, vypnout pod 0,5 V (okna = plán WATTrouteru, při změně upravit); při EV 1f index L2 − L3.
- **Senzory `energy_pnd_deviation_*` jsou bez bojleru** (zbytek rozdílu HA↔PND, počítá AppDaemon). Odchylka prodeje v % je při malém prodeji zkreslená rozlišením GoodWe 0,1 kWh.
- Validátor referencí zná i entity z AppDaemonu (hledá `set_state("…")` v `config/appdaemon/apps`). Testy validátoru: `PYTHONPATH=. pytest -o addopts="" tests/test_reference_validator.py` (ve venv chybí coverage).
- **GoodWe EMS:** `conserve` nabíjí i ze sítě – nepoužívat; „drž SOC“ = `battery_standby`.
- **AppDaemon `set_state` zahazuje falsy hodnoty** (0, False, i v seznamech) → čísla a logické hodnoty v atributech jako text, seznamy řádků s textovými hodnotami, nebo JSON řetězec.
- AppDaemon **nevidí entity vzniklé v HA po svém startu** (1. 10.: `sensor.pool_hours_done` = None i po reloadu app) → restart doplňku. Start doplňku trvá ~4 min (instaluje chromium) → watchdogy HAv2 pošlou notifikaci a filtraci mimo NT vypnou; předem upozornit uživatele.
- AppDaemon: nová podsložka apps se načte až po restartu doplňku; `log:` jen s definicí v `appdaemon.yaml`; `logbook.log` bez `entity_id`.
- Trigger šablony: `this` nejde ve `variables`.
- Nové YAML platformy (statistics, history_stats, integration) potřebují restart HA.
- Utility metery vždy přes GUI (YAML neprojde validátorem).
- Markdown karty: obsah jako `|` (literal), ne `>` – jinak se rozbijí tabulky.
- Dashboard dlaždice: vždy explicitní `grid_options`, jinak se v sekcích „rozsypou“.
- Štítky: nové HAv2 entity vždy `hav2` + oblast (`hav2_system` / `hav2_baterie` / `hav2_ev` / `hav2_bazen`) – výjimka v CLAUDE.md. Senzory z AppDaemonu štítek mít nemůžou.
- **cez_pnd (HACS) v1.1.2** (nainstalováno 27. 9. 22:26) funguje **bez lokálního patche** – opravuje účty bez `idDeviceSet`;
  ověřeno 28. 9.: stav OK, 96 záznamů/den, denní spotřeba i výroba sedí s AppDaemon PND app. Starý patch pro 1.1.1 už neplatí.
  **30. 9. aktualizováno na v1.1.8** (nové: nákladové statistiky – u nás vypnuté, kontrola EAN/ELM jen v config flow/reauth –
  při novém přihlášení může chtít potvrzení vazby). Po restartu ověřeno: entry `loaded`, status OK, 96 záznamů, ID statistik
  `cez_pnd:…_consumption/production` beze změny, VT+NT = celková spotřeba.
- Citlivé: `.env` (HA_TOKEN), `config/appdaemon/apps/apps.yaml` (PND heslo) – nikdy nevypisovat ani necommitovat. (Testovací údaje v Keychain `cez_pnd_test` smazány 25. 9. 2026.)

## 3b. Zjištění z testu Auto (26.–29. 9. 2026)

- **WATTrouter ECO** (manuál: WATTrouter ECO WRE 01/06/14): bojler spíná přes **SSR** (3× Carlo Gavazzi RG na výstupech S1–S3, relé R1/R2 nezapojená; jistič B16/3; v bojleru zapojená jen šedá L3, L1/L2 zaizolované Wago; foto 29. 9.) – režim výstupu ověřit ve WATTconfigu; podle chování sepne, až přebytek na jeho
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
- **29. 9.–1. 10. hotovo:** WATTrouter ECO opsán (SSR1–3 plynulá regulace, režim „každá fáze samostatně“, bojler jen SSR3 = L3,
  relé R1/R2 nezapojená); nucený ohřev přesunut z 16–19 h (VT, relikt starého HDO) na **22–06 h** (30. 9.: 3,8 kWh / 13,2 Kč místo ~22 Kč);
  hodiny regulátoru synchronizovány. Priorita bojleru v poledne vypnuta (`input_boolean.ev_boiler_priority` off – bojler bere jen ~⅓ přetoku).
  **Shelly Pro EM-50 bojler** (`..._ece334fd2370`, napájení B16/3 sv. 6 = L3, snímač na šedém vodiči nad SSR3; test 2,3 kW, účiník 1,00;
  při pulzní regulaci účiník ~0,45, činný výkon platí). **Shelly Pro EM-50 filtrace** (`..._841fe890fc44`) místo Pro 1PM – relé spíná
  cívku stykače, snímač na fázi ze stykače k čerpadlu, 530 W, cos φ 0,92. Garáž na přehledu Energie v2 (s potvrzením).
- PND v HA jen **hodinově** (15min data integrace hned agreguje). cez_pnd v1.1.8 (od 30. 9.) funguje bez lokálního patche.

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
