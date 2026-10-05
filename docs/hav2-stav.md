# HAv2 – stav a předávka (k 5. 10. 2026)

Zadání: `docs/HAv2_prompt.md` · Návrh (schválený): `docs/hav2-architektura.md` · Záloha v1 a plán mazání: `archive/v1-2026-09-25/README.md`

## 1. Kde jsme

| Fáze | Stav |
|---|---|
| 1 – inventura | hotovo |
| Krok 0 – záloha v1 | hotovo (HA záloha `pre-HAv2-2026-09-25` id `9740260a`, archiv `archive/v1-2026-09-25/`, commit 88482e2) |
| Krok A – statistiky a náklady | hotovo (`packages/hav2_statistics.yaml`, GUI utility metery, Energy dashboard s cenami) |
| 2 – návrh | schváleno 25. 9. 2026 |
| 3 – implementace | hotovo – datová vrstva, baterie, EV, bazén, dashboard |
| 4 – ověření a převzetí řízení | **hotovo** – test Auto od 26. 9. 2026, HAv2 řídí natrvalo, v1 smazáno 3. 10. 2026 |

**Režim:** `input_select.energy_system_mode` = **Auto**, přepínače `input_boolean.energy_battery_control`, `ev_control`,
`pool_control` zapnuté, DoD 80 %. Automatizace, skripty a blueprinty v1 jsou smazané (§A).
Návrat k v1 jen obnovou HA zálohy `465189a7` (před úklidem v1) nebo `81c05fcd` (před testem Auto).

**Hlavní dashboard** = výchozí `lovelace` („Přehled“, cesty `/lovelace/<path>`), zdroj pravdy `dashboards/energie-v2.yaml` (§2).

**Data:** minutový záznam `config/appdaemon/hav2_data/RRRR-MM-DD.jsonl` (AppDaemon, 60 dní, stáhne `make pull`, mimo git)
+ historie HA + PND D+1 (integrace `cez_pnd`, statistiky `cez_pnd:*` od 1. 1. 2026).

## 1b. Pro novou session (stav 4. 10. 2026 odpoledne)

**Pracovní postupy:**
- Kontroly: `config/appdaemon/hav2_data/*.jsonl` (klíče `DATA_STATES`/`DATA_ATTRS` v `hav2_app.py`), PND v
  `sensor.energy_boiler_pnd_daily` (atributy `meas_*`, `residual_*`), měření Shelly (bojler, filtrace, sauna).
- Nahrávání: změněné soubory raději jednotlivě `rsync` na HA (po validaci `tools/run_tests.py`) – celý `make push`
  (`rsync --delete`) přepíše na HA i `.ha_mcp/logs` starší lokální kopií. Lokální `.storage` bývá zastaralý → před
  validací stáhnout jen registr (`rsync homeassistant:/config/.storage/core.entity_registry config/.storage/`);
  celý `make pull` by přepsal neuložené YAML.
- AppDaemon vidí nové entity až po **restartu doplňku** (~4 min, watchdog pošle notifikaci, vypne filtraci, baterii dá
  na auto – předem říct uživateli). Změny `.py`/`hav2.yaml` se načtou samy.
- **Statistiky přes WS/službu:** recorder zapisuje se zpožděním (desítky s až minuty) – kontrolovat až po ustálení
  a další import navazující na součty spouštět až po zápisu předchozího.

### 0c. Kontrola 5. 10. dopoledne (výsledky)
- **PND za 4. 10.:** synchronizace v 6:00 OK (skončila 6:00:13, bez zásahu), `pnd_check` v 6:02 po synchronizaci,
  ranní souhrn 7:40 už s bojlerem za 4. 10. Pojistka `automation.hav2_opakovat_stazeni_pnd` nebyla potřeba (podmínky
  nesplněny) – ověřit při příštím výpadku portálu. Bezchybných dnů přes integraci zatím 1 (3. a 4. 10. ručně) → smazání
  PND app (0/5) nejdřív 7. 10.
- **Oprava rozpadu bojleru (4. 10. 7:55) – potvrzeno:** v hodinách s přetokem 12–16 h HA „ze sítě“ 0,037 kWh, PND nákup −
  GoodWe nákup 0,008 kWh (3. 10.: 0,25 / 0,008). Rozpad 4. 10.: Shelly 4,27 kWh = z přetoku 0,66 + ze sítě 3,61; PND odhad 3,96.
- **Odchylky 4. 10. (`meas_*`):** nákup +0,13 kWh (+2,1 %, stálý offset ~5 Wh/h), prodej −0,17 kWh (−6,6 %; 3. 10. −0,34).
  Zbytek prodeje: ~−0,08 v hodinách s pulzy bojleru 14–16 h, ~−0,04 v 10–11 h (bojler skoro nic) a ~−0,05 rozprostřeno
  → GoodWe počítá export o ~3 % víc než elektroměr, s bojlerem to nesouvisí. Sledovat, nic neměnit.
- **Energy dashboard:** `energy/validate` bez chyb; `sensor.energy_buy_gross_energie_cost` roste se správnou cenou
  (NT 3,51, VT 6,10 Kč/kWh). Porovnání s PND za celý den 5. 10. – 6. 10. (0b/1).
- **Mrazák:** od 4. 10. 16:00 bez poplachu (automatizace běžela jen při startu HA), chod ~64–68 W, ~21–32 Wh/h →
  odhad ~0,6 kWh/den. V sankey nad `min_state` (0,47 kWh v 11 h); karta ukazuje kWh (uživatel 5. 10.) → 0b/4 uzavřeno.
- **Noc 4./5. 10.:** baterie v NT držela 80 % („drží energii na ranní VT“), dům ~2,6 kWh ze sítě v NT; od 6:00 vlastní
  spotřeba, VT nákup 6–10 h ~0,28 kWh. Plán to volí o ~0,26 Kč levněji (výkup ráno ~2,2–2,7 Kč × ztráta přetoku + opotřebení
  1 Kč/kWh ≈ úspora NT 3,51) – správně podle modelu, těsné rozhodnutí. Bez EV.
- **Profil bojleru (C5) – hotovo:** `boiler_kwh_by_hour` 22 h 2,0 / 23 h 0,6 kWh, odpoledne jen ~0,15 kWh/h z přetoku.
- **Faktura výkupu Yello 9/2026 (106,9 kWh, 2 228,44 Kč/MWh, 238,22 Kč) – ověřeno:** množství = PND 106,90 kWh (denně
  max Δ 0,05); cena = **0,75 × vážený spot OTE** (2 971,74 Kč/MWh z hodinových přetoků PND × `HourlyPrice` OTE × kurz ČNB,
  poměr 0,7499). GoodWe 137,9 kWh – rozdíl = bojler z přetoku. **Koeficient výkupu změněn 0,85 → 0,75** (5. 10.,
  souhlas uživatele): `input_number.energy_sell_coefficient` (YAML `initial`, nastaveno službou), výchozí hodnoty
  v `hav2_planner.Prices`, `hav2_app`, šabloně ceny prodeje a na dashboardu; test arbitráže NT drží 0,85 explicitně.
  Atribut `initial` ukazuje do restartu HA ještě 0,85 (reload ho neobnovil), stav je 0,75. Spotové ceny OTE jdou
  stáhnout jen z HA (z Macu timeout): SOAP `GetDamPricePeriodE` (PT15M, EUR/MWh) na `https://www.ote-cr.cz/services/PublicDataService`.
- **EV září 2026:** EcoVolter 207,7 kWh – NT 70,5 kWh za ~247 Kč (1.–24. 9. ruční záznam 193,26 Kč, HAv2 54,11 Kč),
  den 137,1 kWh (hlavně slunce) ≈ 173 Kč ušlého výkupu (0,75 × spot) → ekonomicky ~425 Kč (~2,05 Kč/kWh; vše ve VT by
  bylo 1 267 Kč). Rozpad podle zdroje jen od 25. 9. (HAv2); ~3,8 kWh 25.–30. 9. bez přiřazeného zdroje.
- Log HA bez chyb HAv2 (jen Chromecasty nedostupné, Apple TV reconnect, HACS: `bar-card` vyřazený z HACS – nepoužívaný, odinstalován 5. 10. včetně resource; použitý byl jen ve starém v1 dashboardu v archivu).

### 0a. Kontrola 4. 10. ráno (výsledky)
- **PND 3. 10.:** plánovaná synchronizace 6:00 **selhala** (timeout portálu ČEZ, `PndTimeoutError`); integrace po timeoutu
  sama neopakuje (retry jen po hlášené údržbě) → entity `cez_elektromer_*` unavailable. Ručně `cez_pnd.fetch_data` v 7:41
  OK: součty navazují (náklady 10 682,37 → 10 718,57 Kč), NT 6,83 / VT 2,00 kWh přesně podle HDO 22–06, `pnd_check`
  v 7:43 po synchronizaci (listener funguje). Ranní souhrn 7:40 ale ještě měl bojler za 2. 10.
  → **Pojistka (4. 10., `packages/hav2_pnd.yaml`, `automation.hav2_opakovat_stazeni_pnd`):** v 6:30, 7:15, 8:30 a 10:00,
  když atribut `date` včerejší spotřeby není včerejšek a synchronizace neběží → `cez_pnd.fetch_data`; v 10:00 při
  neúspěchu notifikace. Ověřit při příštím výpadku portálu (logbook „HAv2 PND“).
- **Odchylky 3. 10. (měřený bojler):** nákup −0,14 kWh (−1,6 %), prodej −0,34 kWh (−4,6 %).
- **Bojler ze sítě v hodinách s přetokem – potvrzeno:** 12–16 h HA „ze sítě“ 0,25 kWh, PND nákup − GoodWe nákup 0,008 kWh;
  prodej PND o 0,26 kWh nižší než GoodWe − „z přetoku“ → HA přesouvá ~0,25 kWh/den z přetoku do sítě (pulzy WATTrouteru
  × okamžitý export L3). **Opraveno 4. 10. 7:55:** `sensor.bojler_z_pretoku_w` = min(průměr 60 s příkonu, průměr 60 s
  výkonu L3 ≥ 0), `_ze_site_w` z průměru příkonu; nový GUI helper Statistika `sensor.sklep_goodwe_sit_l3_vykon_prumer_1_min`
  (štítky hav2/hav2_system). **Ověřit 5. 10.** (PND za 4. 10.): ze sítě v hodinách s přetokem ≈ PND, odchylky ~0.
- **Rozpad bojleru 3. 10.:** Shelly 7,59 kWh = z přetoku 2,27 + ze sítě 5,31 (po korekci ~2,5 / ~5,1); PND odhad 7,26 kWh.
- **Baterie v noci 3./4. 10.:** dobití na konci NT nebylo potřeba – noc spotřebovala méně (~0,2 kWh/h), plán od 1:00 bez
  nabíjení, SOC 6:00 26 %, VT nákup ~0. Bez nabíjení EV v NT.
- **Předpověď 3. 10.:** FVE opravená 25,0 / skutečnost 26,4 kWh (−5 %), Solcast 33,4 (+27 %); spotřeba 14,3 proti 11,5 kWh.

### 0b. Ověřit po změnách 4. 10. odpoledne (sauna Pro 1PM, mrazák, Energy dashboard; commit 84fc43f)
1. **Energy dashboard – síť podle elektroměru** (přepnuto 4. 10. v 16:50, po restartu HA): 4. 10. je neúplný (nákup jen
   od 16:50). **6. 10. (PND za 5. 10.):** denní změna `sensor.energy_buy_gross_energie` ≈ PND nákup a
   `sensor.energy_sell_net_energie` ≈ PND prodej (cíl jako u `meas_*`, odchylka do ~0,2 kWh); spotřeba domu v Energy
   dashboardu ≈ dům + bojler; náklady se počítají (statistika `sensor.energy_buy_gross_energie_cost` existuje a roste).
   `energy/validate` bez chyb (4. 10. 16:58 čisté). Pokud odchylka nákupu > PND, prověřit metodu `left` u Riemannu
   z `energy_buy_gross_w` (součet dvou template senzorů s různým okamžikem aktualizace).
2. **Sauna na Pro 1PM – při prvním použití:** páčka B10 nahoru → `switch.sauna` on a `binary_sensor.sauna_vypinac_b10`
   on, dolů → off; zapnutí/vypnutí z HA funguje i při páčce nahoře; 9,6 A nevypne limit 12 A
   (`binary_sensor.sauna_overcurrent`); `sensor.sauna_device_temperature` během delší sauny (modul v zavřené skříni,
   pozor nad ~70 °C); auto-off 3 h. Zapnutá zásuvka ≠ sauna topí (saunu pouští uživatel ručně) – detekce „Dnes sauna“
   je z výkonu > 1000 W po 5 min. Plus body z checklistu 0/6.
3. **Mrazák:** automatizace `automation.mrazak_hlidani_zasuvky` zatím nikdy nespustila. Volitelně test: ručně vypnout
   `switch.mrazak` → do ~10 s zapnuto + notifikace. Sledovat falešné poplachy „6 h bez odběru“ (studený sklep) a
   „nedostupná 30 min“ (Plug E ve sklepě má ping ~100 ms, RSSI entita vypnutá) – případně prodloužit limity.
   Typický odběr při chodu kompresoru ~64 W; po pár dnech doplnit denní kWh.
4. **Dashboard Energie v2:** mrazák v sankey (Dům → Mrazák + Dům ostatní) se objeví od 5. 10. (dnes pod `min_state`);
   sankey má `unit_prefix: k` a `round: 0` – po přidání mrazáku se karta sama přepnula na Wh, příčina nezjištěna
   (`sensor.mrazak_energy` je v kWh).
5. **Statistiky sauny a mrazáku** smazány 4. 10. ~16:00 (`recorder/clear_statistics`), plní se od té doby – grafy
   kWh/den 14 dní budou do ~18. 10. neúplné. Profil plánovače (`STAT_SAUNA`, medián 14 dní) to neovlivní.
6. ~~Přejmenovat `update.…_beta_firmware` → `update.sauna_beta_firmware_update`~~ – uživatel hotovo 5. 10.
7. Poučení: 4. 10. jsem spustil `make pull` až po úpravách → přepsal lokální YAML (nový `mrazak.yaml` smazal) – viz
   pracovní postupy výše, před úpravami vždy nejdřív stáhnout.

### 0. Hned v nové session (checklist)
1. **PND za 3. 10. (stáhne se 4. 10. v 6:00) – první den jen přes integraci `cez_pnd`:**
   - synchronizace proběhla (`binary_sensor.cez_elektromer_8591_0246_synchronizace_pnd_bezi`, `sensor.cez_elektromer_…_vcerejsi_spotreba`),
     statistiky `cez_pnd:*` navazují bez skoku, NT/VT za 3. 10. sedí s pravidlem NT 22–06;
   - `pnd_check` se spustil po konci synchronizace (`sensor.energy_boiler_pnd_daily`: `date` 2026-10-03, `updated` krátce po 6:00,
     ne až v 7:30), ranní souhrn 7:40 má bojler podle PND;
   - pohled PND na dashboardu ukazuje 3. 10.
2. **Kontrola PND s měřeným bojlerem (`meas_*`) poprvé za celý den 3. 10.** – dlaždice „Odchylka nákupu/prodeje“ (kWh).
   Očekávání: stálá odchylka nákupu ~+0,1 kWh/den (PND o ~5 Wh/h víc než GoodWe, viz §A), prodej ~0.
3. **Bojler „ze sítě“ v hodinách s přetokem (3. 10. slunečno):** po hodinách porovnat `sensor.bojler_ze_site_energie`
   s PND (nákup PND − nákup GoodWe). 2. 10. vycházelo HA o 0,04–0,09 kWh/h víc (artefakt nesouběžných odečtů Shelly ×
   GoodWe, s průměrem 30–60 s sedí). Pokud se potvrdí, vyhladit vstupy `sensor.bojler_z_pretoku_w` (~60 s průměr) –
   logika po fázích (L3) zůstává. Postup analýzy: §A „Fakturace po fázích“.
4. **Rozpad bojleru za celý 3. 10.** (`sensor.bojler_z_pretoku_energie` / `_ze_site_energie`, sankey na Úsporách).
5. **Smazat AppDaemon PND app** (po 2–3 dnech bezchybného stahování přes integraci, se souhlasem): aplikace z HACS,
   `config/appdaemon/apps/HomeAssistant-CEZDistribuce-PND/`, `apps/pnd/`, blok `pnd:` v `apps.yaml`, automatizace
   `automation.run_pnd` a `automation.run_actions_after_appdaemon_starts` (obě vypnuté), entity `sensor.pnd_*`.
   `init_helper` (událost APPDAEMON_READY) nechat, dokud ho něco používá – ověřit.
6. **Test sauny – odložen, termín neurčen** (uživatel 3. 10.). Až proběhne: auto-detekce „Dnes sauna“ po 5 min, přepočet
   plánu, konec po 20 min bez topení, nákup ve VT po sauně, teplota Shelly Pro 1PM (`sensor.sauna_device_temperature`); prodej v 19 h se saunou (test 2. 10.:
   4,2 → 1,9 kWh, SOC 20 %, VT nákup 0,7 kWh – posoudit, jestli prodávat a pak kupovat ve VT dává smysl).
7. **Přesnost předpovědí** – jen sbírat (`sensor.energy_forecast_accuracy`, atribut `history`) do C8 (~16. 10.).
   2. 10.: FVE 23,7 kWh, opravená předpověď 20,4 (−14 %), surový Solcast 27,3 (+15 %); spotřeba 11,1 proti 9,6 kWh.

### A. Hotovo a zjištěno 1.–3. 10.
1. **Blokování bojleru při prodeji (B1) – uzavřeno.** Relé Pro EM-50 → vstup LT WATTrouteru, plán SSR3 „omezit“ 16–22 h,
   `script.hav2_boiler_block`, `input_boolean.energy_boiler_block_on_sale`, auto-off 2 h v Shelly. Prodej 2. 10. 19–20 h:
   bojler jen první minutu (~15 Wh) – opraveno (`_boiler_block()` hned při změně plánu před `battery_apply()`); PND 2. 10.
   bez bojleru v 19 h, odchylka prodeje −1,2 % (1. 10. bez blokace −9,8 %).
2. **Úklid v1 (B2) – hotovo 3. 10.:** smazány automatizace v1 (19) + osiřelé (`spustit_filtraci`, `get_data_from_dip`,
   `run_pnd_2`), skripty `fve_grid_export_*`, blueprinty `jan-trnka` (7), YAML `input_boolean.time_to_use_overflows`,
   `input_select.season`, `input_number.winter_dod`, šablony Nighttime / Base Nighttime / Final Daily House Consumption,
   EV denní náklady, EV nabíjí ze solárů a 7 statistik v `sensor.yaml`; uživatel v UI smazal staré helpery, šablonu
   `sensor.filtrace`, utility metery Filtrace Daily, EV energie týdně/měsíčně. **Ponecháno (používá HAv2):** Filtrace Sum,
   House Consumption Sum/Daily, ECO Volter Daily Energy, energy_buy/sell (+ _sum, _daily), fve_battery_charge/discharge_w,
   `ev_nt_*` celkem a měsíc, skripty Filtrace ON/OFF (Assist, spínají `switch.bazen_filtrace_rele`).
3. **Hlavní dashboard (3. 10.):** obsah Energie v2 zkopírován do `lovelace` (ověřeno 1:1), dashboard `energie-v2` smazán
   (záloha `archive/energie-v2-dashboard-2026-10-03.json`, starý `lovelace` v `archive/v1-2026-09-25/dashboard/lovelace-2026-10-03.json`).
4. **Fakturace po fázích – ověřeno 3. 10.** (PND × historie výkonu GoodWe L1–L3 po sekundách, 26. 9.–2. 10.):
   v 13 hodinách se souběhem nákupu a prodeje na různých fázích PND prodej 2,11 kWh, model po fázích 2,27, součtový
   model 1,44 (v noci 2. 10. součtově 0, PND 0,05 kWh/h) → **elektroměr účtuje po fázích**, `energy_buy/sell` (Σ fází)
   jsou správně. Bojler na L3 v modelu po fázích sedí s PND (22 h 2,83 / 2,84 kWh), takže logika
   `sensor.bojler_z_pretoku_w` = min(příkon bojleru, export L3) je správná; odchylka při topení z přetoku viz 0/3.
   **Stálá odchylka nákupu:** PND o ~5 Wh/h víc než GoodWe nezávisle na velikosti odběru (23 h bez bojleru, ~0,12 kWh/den,
   ~0,6 Kč/den) – u malých součtů (VT 0,89 kWh) dává 7 %, proto odchylky v kWh.
5. **Odchylky proti PND v kWh (3. 10.):** `sensor.energy_pnd_deviation_import_kwh` / `_export_kwh` (PND − HA, + = PND víc,
   atribut `pct`) na dlaždicích Úspory → Kontrola proti PND; % senzory zůstaly (mají statistiky).
6. **Bojler na dashboardu (3. 10.):** WATTrouter při malém přetoku spíná bojler pulzy 4–5 s jednou za minutu (500–1500 W,
   průměr 35–100 W) → okamžitý výkon ukazoval 0 W a `binary_sensor.energy_boiler_heating` (zap. > 1000 W, delay_off 3 min)
   svítil pořád. GUI helpery (štítky hav2/hav2_system): Statistika `average_step` 60 s
   `sensor.sklep_shellyproem50_ece334fd2370_energy_meter_0_bojler_vykon_prumer_1_min` a Práh 300 ± 100 W
   `binary_sensor.sklep_shellyproem50_ece334fd2370_energy_meter_0_bojler_hreje_prumer` → dlaždice „Bojler teď (průměr 1 min)“,
   „Hřeje“ a odznak na Přehledu. `binary_sensor.energy_boiler_heating` beze změny – používá regulace EV (jistič L3, pulz =
   plný proud) a `hav2_ev.BoilerGate`. Dlouhá ID helperů vygeneroval HA podle zařízení; přejmenování jen se souhlasem.
7. **PND jen z integrace `cez_pnd` (3. 10.):** dříve zdvojeno s AppDaemon app „CEZ Distribuce PND“ (Selenium, CSV,
   `sensor.pnd_data` / `pnd_tariff_data`, vlastní opravy přepisované aktualizací HACS). Postup (HA záloha s DB `f5356db0`):
   `cez_pnd.fetch_data` (max 60 dní na volání) 1. 1. – 15. 9.; integrace při zpětném importu posouvá součty následných
   hodin, ale bloky spuštěné rychle za sebou četly výchozí součet před zápisem předchozího → skoky na hranicích; opraveno
   přepočtem všech hodin od 1. 1. (hodnota hodiny = `state`) a zápisem `recorder/import_statistics` se souvislými součty.
   Integrace vynechá hodinu s neúplnou čtvrthodinou (28. 4., 11. 5., 20. 6., 14. 7., 2. 8., 9. 9.) a 2 h při změně času
   29. 3. → doplněno rozdílem k dennímu součtu z app A. Bez historie HDO v recorderu dává integrace vše do VT → NT/VT
   podle **HDO 22–06** (stejné od 1. 1. 2026), ověřeno proti tarifnímu reportu PND **274/274 dní** (max Δ 0,015 kWh).
   `recalculate_costs` + `rebuild_total_costs`: 10 682,37 Kč za 1. 1. – 2. 10. (NT 3,51 / VT 6,09 Kč celý rok).
   Kontrola: nákup 1966,31 / prodej 1559,04 kWh = app A (1966,29 / 1559,03), denně max Δ 0,002 kWh.
   Pohled PND = `statistics-graph` nad `cez_pnd:*` (včera, 14 dní, 12 měsíců, NT/VT, náklady); HAv2 spouští `pnd_check`
   po konci synchronizace (`pnd_sync_entity` v `hav2.yaml`), dál i v 7:30. App A vypnutá (`disable: true` v `apps.yaml`),
   obě její automatizace vypnuté – smazání viz 0/5.
8. **EV bez termínu jen z přetoku (3. 10. večer):** auto připojené ve 22:15 bez požadavku nabilo v NT ~3 kWh (72 → 76 %,
   plán „NT jen na to, co nepokryje slunce do zítřka 18:00“ se každých 15 min přepočítával a NT rostlo; zastavil SOC baterie
   ≤ 60 %). Ve skutečnosti šlo z baterie domu (vybíjela 5,2 kW, SOC 75 → 42 %) – pravidlo §5.2 „EV ze sítě → standby“ nebylo
   naprogramované. Opraveno: (a) bez požadavku jen přetok FVE – `plan_ev` plánuje NT/VT jen se zadaným termínem
   `input_datetime.ev_deadline`; (b) EV ze sítě (Plán, Rychle, Ručně) → baterie `battery_standby` (i místo prodeje);
   (c) plánovač baterie zná variantu „noc auto + dobití na konci NT“ a dobije přesně to, co by se ráno koupilo ve VT
   (noc 3./4. 10.: 0,47 kWh v 5 h místo 0,34 kWh VT). **Ověřit 4. 10. ráno:** dobití v 5 h proběhlo, ve VT nákup ~0.
9. **Dřívější (1.–2. 10.):** souběh EV a bojleru v noci OK (EV staženo na 8 A ve 3f, rezerva jističe min. 7,5 A);
   jednorázový cíl EV nad limitem auta OK; watchdog otestován (restart AppDaemonu); Shelly Pro EM-50 přejmenovány
   (`switch.bazen_filtrace_rele`, `sensor.bazen_cerpadlo_*`, `switch.bojler_blokace_rele`, `sensor.bojler_*`; v jsonl starší
   ID `shellyproem50_<MAC>_energy_meter_0_*`); smazán `sensor.pool_hours_done_legacy` a Shelly Pro 1PM; ranní souhrn
   `automation.hav2_ranni_souhrn` 7:40 (ověřen 3. 10.); auto-detekce sauny `_sauna_detect`; přesnost předpovědí
   (`sensor.energy_forecast_day_morning`, `_accuracy`); tok energie s uzlem Bojler (`energy_buy_gross_w`, `energy_sell_net_w`,
   `house_consumption_with_boiler_w`); sankey s bojlerem (od 3. 10. celý den, `energy_sources_total` od 2. 10. 9:29).

### B. Čeká na uživatele
1. Souhlas se smazáním AppDaemon PND app (0/5).
2. Termín testu sauny (0/6).
3. Volitelně kratší ID nových helperů bojleru (např. `sensor.bojler_vykon_prumer_1min`, `binary_sensor.bojler_hreje`) –
   přejmenování entit jen se souhlasem (CLAUDE.md), pak upravit i dashboard.

### C. Ověřit, až nastane
1. **Záporný výkup s plnou baterií** (nové 1. 10.): limit přetoku = příkon bojleru + 600 W (`hav2_boiler.NegPriceBoiler`)
   – náběh bojleru ~200 W/min, PND export ~0, po nahřátí limit 0 W a pokus po hodině; logbook „HAv2 přetok“.
2. ~~EV – jednorázový cíl nad limitem auta~~ – ověřeno v noci 1./2. 10. (viz A2).
3. **Filtrace – nesoulad s venkovními vypínači:** první notifikace „vypnuto vypínačem“ / „běží ručně“
   (`switch_mismatch` v `sensor.pool_controller`).
4. **Večerní prodej z baterie:** start/konec na čtvrthodině, ráno ve VT nechybí baterie (1. 10.: prodej 19:00–20:00 OK).
5. ~~Profil bojleru v plánu se přeučuje na noc~~ – hotovo 5. 10. (0c).
6. Nenastalo: 3f z plné baterie, NT nabíjení baterie před zataženým dnem. (Watchdog otestován 2. 10., viz A5.)
7. **EV – kmitání proudu (úprava 2. 10.):** proud se mění až po 2 min trvání (nahoru i dolů); přehrání 26. 9.–1. 10.
   změn 177 → 57. Ověřit při prvním solárním nabíjení s troubou: proud drží, baterie kryje krátké poklesy.
8. **Solcast – automatické tlumení, krok 1 (2. 10.):** zapnuto jen `get_actuals` (odhad skutečné výroby, 1 volání/den,
   limit prognóz 10 → 9), `generation_entities` = `sensor.total_pv_generation`, `auto_dampen` **vypnuto**. Potlačení
   půlhodin s omezeným přetokem: `binary_sensor.solcast_suppress_auto_dampening` (limit přetoku < 10 000 W).
   **Kolem 16. 10.:** porovnat pevnou korekci `hav2.jinja` (měsíc × hodina) s faktory tlumení Solcastu (14 dní dat)
   → buď zapnout `auto_dampen` a `hav2.jinja` zjednodušit (jinak dvojí korekce!), nebo zůstat u pevné korekce.
9. FVE ranní stín: od listopadu přepočítat hodinový tvar (`hav2.jinja`, poměr `sensor.pv_power` / Solcast this_hour).

### D. Nápady / později
- Energy dashboard (od 4. 10.): síť = elektroměr distributora – nákup `sensor.energy_buy_gross_energie` (nový
  Riemann z `energy_buy_gross_w`, tj. GoodWe nákup + bojler ze sítě), prodej `sensor.energy_sell_net_energie` (bez
  přetoku do bojleru) → spotřeba domu vč. bojleru, odpovídá PND. Historie sítě a nákladů v Energy dashboardu proto
  začíná 4. 10. (dřív `energy_buy_daily` / `energy_sell_daily`, statistiky zůstaly). Spotřebiče: EV, Filtrace
  (`filtrace_sum` = Riemann z měřeného výkonu), Sauna, Mrazák, Bojler; baterie se SOC. Na Energie v2 navíc grafy
  kWh/den (Bazén, Úspory), sankey a skládaný graf spotřeby na Úsporách.
- Druhý kanál (IB) obou Pro EM-50 volný.
- **Infrasauna – hotovo 2. 10.:** Shelly Plug E (16 A) = `switch.sauna`, `sensor.sauna_power`, `sensor.sauna_energy`
  (zařízení „Sauna (Shelly Plug E)“, entity přejmenovány z „Zásuvka řízená/Filtrace“ se souhlasem uživatele). Test 2. 10.:
  **2,28 kW, 9,6 A, fáze L2** (s filtrací), termostat za 8 min nespínal. Odečtená z `sensor.base_house_consumption`
  a z profilu plánovače (`STAT_SAUNA`); „Dnes sauna“ (`input_boolean.energy_sauna_today`, `input_datetime.energy_sauna_start`,
  `input_number.energy_sauna_duration_h`, `..._power_kw` = 2,3) přičte zátěž do slotů plánu (`hav2_planner.add_extra_load`,
  baterie ji může krýt) a po konci se sama vypne; atribut `sauna` v `sensor.energy_plan`. Dashboard: Přehled → Sauna,
  sankey a skládaný graf.
- **Sauna na Shelly Pro 1PM – od 4. 10.:** nová podružná skříň SRN 6 vedle bazénového rozvaděče (fáze ze svorky 2
  chrániče F7-25/30 mA, tj. pod proudovým chráničem), B16 → Pro 1PM I → O → pevná zásuvka 16 A u sauny. Jistič B10
  ve skříni = ruční vypínač na SW1 (vstup `switch`, `in_mode: follow` → sauna jde zapnout i bez HA). Pro 1PM
  (192.168.68.121): `initial_state: off`, auto-off 3 h, `current_limit` 12 A, `power_limit` 3000 W. Entity
  přejmenovány na stejná ID (`switch.sauna`, `sensor.sauna_power`, `sensor.sauna_energy`,
  `binary_sensor.sauna_overpowering`, + `binary_sensor.sauna_vypinac_b10`, `sensor.sauna_current`, …), zařízení
  „Sauna (Shelly Pro 1PM)“. Zapnutá zásuvka ≠ sauna běží (saunu je nutné pustit ručně), detekce jede z výkonu.
  Filtrace a sauna nikdy nepoběží zároveň (uživatel).
- **Plug E → mrazák ve sklepě (4. 10.):** `switch.mrazak`, `sensor.mrazak_power`, `sensor.mrazak_energy`, zařízení
  „Měřená zásuvka mrazák (Shelly Plug E)“; statistiky sauny z testu 2. 10. přešly přejmenováním k mrazáku. Mrazák
  patří do základní spotřeby, nic se neodečítá. V Plug E ověřit: stav po výpadku = zapnuto, žádný auto-off.

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
| `hav2.yaml` | konfigurace app (samostatně – `apps.yaml` je gitignored kvůli heslům; ID statistik PND a `pnd_sync_entity`) |
| `hav2_app.py` | app `Hav2`: profil spotřeby, plán baterie každých 15 min + při změně vstupů, heartbeat |
| `hav2_planner.py` | plánovač baterie (čistý Python) + `hourly_table` pro dashboard |
| `hav2_ev.py` / `hav2_ev_ctl.py` | EV plán + regulátor proudu (smyčka 5 s) / napojení na HA |
| `hav2_pool.py` / `hav2_pool_ctl.py` | řízení filtrace (smyčka 60 s) / napojení na HA |
| `hav2_boiler.py` | bojler a zbytková odchylka z PND (volá `Hav2.pnd_check` v 07:30 a po konci synchronizace integrace `cez_pnd`) |

Publikuje: `sensor.energy_plan`, `sensor.energy_export_control`, `sensor.energy_load_forecast`, `sensor.ev_plan`, `sensor.ev_regulator`,
`sensor.pool_plan`, `sensor.pool_controller`, `sensor.energy_boiler_pnd_daily`, `input_text.*_last_decision`, `input_datetime.hav2_heartbeat`.

Testy: `source venv/bin/activate && pytest tests/hav2 -q --no-cov` (103 testů).

### Hlavní dashboard (`lovelace`)
Zdroj pravdy `dashboards/energie-v2.yaml` (název souboru historický), nahrání do výchozího dashboardu:
```
source venv/bin/activate && set -a && source .env && set +a
python dashboards/push_dashboard.py lovelace dashboards/energie-v2.yaml
```
HACS karty: power-flow-card-plus, apexcharts-card, flex-table-card, auto-entities, card-mod, mushroom, sankey-chart.
- **EV → Aktuální session** (jen s připojeným autem): energie, cena, průměr, SOC → cíl, délka a rozpad podle zdroje
  (atributy `solar`/`battery`/`grid_nt`/`grid_vt` senzoru `sensor.ev_session_energy`, od 2. 10., nulují se při připojení).
- **Úspory → Toky energie dnes:** sankey (`custom:sankey-chart`, `time_period_from: now/d`) – zdroje GoodWe → `sensor.energy_sources_total`
  (FVE + nákup NT/VT po fázích + vybíjení baterie) → dům / EV / filtrace / nabíjení baterie / přetok, „Ostatní / ztráty“ = zbytek;
  bojler rozdělený (od 2. 10.): z „Celkem“ část přetoku (`sensor.bojler_z_pretoku_w` = min(příkon bojleru, export L3),
  kWh `sensor.bojler_z_pretoku_energie`), zbytek „Síť (bojler)“ (`sensor.bojler_ze_site_w` / `_energie`); „Prodej do sítě“ =
  `sensor.energy_sell_net_w` / `_energie` (export GoodWe bez bojleru ≈ PND). Pod ním skládaný graf kWh/den 14 dní.
- **Doplněno 2. 10.:** Přehled → „Dnes“ (FVE, spotřeba, EV, nákup, prodej, netto Kč) a „Světla a zásuvka“ (jako starý
  dashboard + `switch.zasuvka_rizena`); EV → „Kia EV6“ (SOC z auta, dojezd, zámek a předtopení jen přes detail, port,
  tlačítko force refresh s potvrzením, mapa); Baterie & FVE → předpověď 7 dní (atribut `week` senzoru
  `sensor.energy_pv_forecast_corrected`: Solcast p50 / × měsíční faktor / p10 × faktor); Nastavení → Zdraví → diagnostika
  (teploty střídače, baterie, EcoVolteru, napětí fází).

## 3. Úskalí (důležité pro další práci)

- **Fakturace po fázích** (ověřeno proti PND 3. 10., §A4) – nikdy součtový výkon/čítače; základ `energy_buy/sell` (Σ fází).
- **Bojler (WATrouter, 2,2 kW, L3) je mimo měření GoodWe** – baterie ho nekryje; nucený ohřev **od 29. 9. 22:00–06:00** (WATTrouter, dříve 16–19 h ve VT); z přetoku jen plynule podle přetoku L3; model `grid_only` (profil z PND za 8 dní – přesune se do noci sám za ~4–5 dní); příkon měří od 1. 10. Shelly Pro EM-50 (`sensor.bojler_vykon`, `sensor.energy_boiler_power_w`). Skutečnou denní spotřebu dává `sensor.energy_boiler_pnd_daily` (PND − GoodWe, D+1; průměr ~4,3 kWh/den). `binary_sensor.energy_boiler_heating` je od 1. 10. podle Shelly (zap. > 1000 W, vyp. < 300 W, delay_off 3 min; napěťový index jen záloha) – pro regulaci EV; pro zobrazení průměr 1 min a práh (§A6). Při malém přetoku spíná WATTrouter pulzy 4–5 s.
- **Senzory `energy_pnd_deviation_*` jsou bez bojleru** (zbytek rozdílu HA↔PND, počítá AppDaemon; od 3. 10. s měřeným bojlerem `meas_*`). Na dashboardu v kWh (`_kwh`), % je u malých součtů zavádějící.
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
- **PND = jen integrace cez_pnd (od 3. 10.):** statistiky `cez_pnd:*_consumption` / `_consumption_nt` / `_consumption_vt` / `_production` / `_cost_*`
  od 1. 1. 2026. NT/VT určuje podle historie `binary_sensor.cez_hdo_hightariffactive_dum` v recorderu (~10 dní) – **při zpětném
  `fetch_data` starším než historie dá vše do VT** → přepočítat NT/VT podle 22–06 a pak `recalculate_costs` + `rebuild_total_costs`
  (postup §A7). Hodiny s neúplnou čtvrthodinou v PND integrace vynechá.
- **cez_pnd (HACS) v1.1.2** (nainstalováno 27. 9. 22:26) funguje **bez lokálního patche** – opravuje účty bez `idDeviceSet`;
  ověřeno 28. 9.: stav OK, 96 záznamů/den, denní spotřeba i výroba sedí s AppDaemon PND app. Starý patch pro 1.1.1 už neplatí.
  **30. 9. aktualizováno na v1.1.8** (nové: nákladové statistiky – u nás vypnuté, kontrola EAN/ELM jen v config flow/reauth –
  při novém přihlášení může chtít potvrzení vazby). Po restartu ověřeno: entry `loaded`, status OK, 96 záznamů, ID statistik
  `cez_pnd:…_consumption/production` beze změny, VT+NT = celková spotřeba.
- Citlivé: `.env` (HA_TOKEN), `config/appdaemon/apps/apps.yaml` (heslo PND app A) – nikdy nevypisovat ani necommitovat. (Testovací údaje v Keychain `cez_pnd_test` smazány 25. 9. 2026.)

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
- **Test tlumení předpovědi Solcast (od 2. 10. 2026, vyhodnotit ~16. 10.):** Solcast nadhodnocuje (o 20–34 %) a nevidí
  místní stín (komín ráno 7–9 h, odpoledne). Dosud to řeší jen pevná korekce HAv2 v `custom_templates/hav2.jinja`
  (měsíční faktor × hodinový tvar dne, naměřeno 19.–28. 9.), kterou je nutné ručně přepočítat s každou změnou výšky
  slunce. Integrace Solcast (HACS) umí **automatické tlumení**: z 14 dní porovná skutečnou výrobu s vlastním odhadem
  skutečné výroby („estimated actuals“, ze satelitu) a pro slunečné půlhodiny, kdy výroba soustavně nedosahuje odhadu,
  spočítá tlumicí faktor, který se pak průběžně mění se sluncem. **Krok 1 (2. 10.):** zapnuto jen stahování odhadu
  skutečné výroby (1 API volání/den po půlnoci → limit prognóz 10 → 9), zdroj výroby `sensor.total_pv_generation`,
  tlumení **vypnuté** – plánování se nemění. Půlhodiny, kdy HAv2 uměle omezuje přetok (záporný výkup, `NegPriceBoiler`),
  vyřazuje `binary_sensor.solcast_suppress_auto_dampening` (limit přetoku < 10 000 W), jinak by je tlumení bralo jako
  stín. **Vyhodnocení:** na jasných dnech po půlhodinách porovnat skutečnou výrobu, odhad skutečné výroby Solcastu,
  surovou prognózu p50 a korekci HAv2 (`sensor.energy_pv_forecast_corrected`, slotová data v plánu). Faktory tlumení
  integrace počítá jen se zapnutým `auto_dampen` (atribut `dampening_factor` v `detailedForecast`, senzor Accuracy) –
  pro porovnání lze tlumení zapnout krátce a `solcast_solar.force_update_estimates` (nespotřebuje volání), pak hned
  vypnout, aby se korekce nesčítala. **Rozhodnutí:** pokud tlumení vystihne stín aspoň stejně dobře jako `hav2.jinja`,
  zapnout `auto_dampen` natrvalo a v `hav2.jinja` ponechat jen měsíční faktor (bez hodinového tvaru) a ten přeměřit;
  jinak tlumení nechat vypnuté, odhad skutečné výroby použít pro ruční přepočet tvaru dne (C9) a limit vrátit na 10.

## 4. Další kroky

Aktuální seznam sledování a rozhodnutí je v **§1b**. Dlouhodobě:
1. **Bojler:** řada `sensor.energy_boiler_pnd_daily` (denně, z toho z přetoku) → roční přínos měření (Shelly) nebo přepojení
   bojleru za měření GoodWe (elektrikář, odhad 5–8 tis. Kč/rok).
2. ~~Převzetí řízení~~ – hotovo, v1 smazáno 3. 10. (§A2).
3. ~~AppDaemon PND app vypnout~~ – vypnuta 3. 10. (§A7); smazat po pár dnech (§1b 0/5).
4. ~~Volitelně: stránka EV „Aktuální session“, sankey na Úsporách~~ – hotovo 2. 10. (viz §2 Dashboard).

## 5. Známé drobnosti
- `sensor.pool_water_temperature` a `sensor.pool_hours_recommended` mají hodnotu až po ≥ 10 min běhu filtrace.
- `sensor.ev_deadline_slack_h` je nedostupný bez zadaného termínu (záměr; na dashboardu skrytý).
- Solcast API 10/10 denně vyčerpáno – auto-update integrace; při výpadku konzervativní režim (× 0,5).
- EV náklady se počítají od 25. 9. 2026; historie v1 (ruční NT) je v `sensor.ev_nt_*`.
