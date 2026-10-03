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

## 1b. Pro novou session (stav 2. 10. 2026 odpoledne)

Kontroly: `make pull` → `config/appdaemon/hav2_data/RRRR-MM-DD.jsonl` (minutový záznam, klíče `DATA_STATES`/`DATA_ATTRS`
v `hav2_app.py`, nově `pool_run`, `pool_w`), PND D+1 v `sensor.energy_boiler_pnd_daily`, měření Shelly (níže).
Nové entity v HA vidí AppDaemon až po **restartu doplňku** (~4 min, watchdog pošle notifikaci – předem říct uživateli;
watchdog při něm vypne filtraci, baterii dá na auto – je to v pořádku). YAML `platform: integration` senzory chtějí restart HA.
Lokální `.storage` je po přejmenování entit zastaralý → před `make push` stáhnout jen registr
(`rsync homeassistant:/config/.storage/core.entity_registry config/.storage/`), celý `make pull` by přepsal neuložené YAML.

### 0. Hned v nové session (checklist)
1. **Prodej 2. 10. 19:00–20:00 s blokací bojleru (B1):** z jsonl / historie – `plan = discharge`, `switch.bojler_blokace_rele`
   on po dobu prodeje a pak off, `sensor.bojler_vykon` ≈ 0 W, `sensor.bojler_z_pretoku_w` 0; PND 2. 10. (D+1 ráno 3. 10.):
   export ≈ GoodWe export. Při úspěchu B1 uzavřít.
   **Výsledek 2. 10.:** discharge 19:00:10–20:00:10, relé on 19:01:02–20:01:02 (živá smyčka až po ~52 s) → bojler
   19:00:18–19:01:06 ~1,1–1,6 kW (~15 Wh), pak 0 W; prodej bez bojleru 3,79 kWh (baterie 5 kWh, SOC 92 → 46 %).
   Opraveno: `_boiler_block()` se volá hned při změně plánu před `battery_apply()`.
   **PND 2. 10. (3. 10.): B1 UZAVŘENO** – bojler v PND jen v 3/12/13/22/23 h (v 19 h nic), odchylka prodeje −1,2 %
   (1. 10.: −9,8 %), nákupu +1,5 %. Bojler 4,38 kWh (22 h 2,29 kWh – dohřál večer nevytopené). Ráno 3. 10. SOC 20 % v 7:30.
2. ~~**Ranní souhrn 3. 10. 7:40**~~ – přišel (potvrdil uživatel 3. 10.).
3. **Přesnost předpovědí:** první řádek `history` v `sensor.energy_forecast_accuracy` za 2. 10. (předpověď FVE 20,4 /
   Solcast 27,3 / spotřeba 9,6 kWh) proti skutečnosti; graf na Baterie & FVE.
4. **Rozpad bojleru za celý 3. 10.** (`sensor.bojler_z_pretoku_energie` / `_ze_site_energie`, sankey „Celkem“ správně od 3. 10.;
   `sensor.energy_sources_total` sbírá statistiky od 2. 10. 9:29).
5. **PND kontrola `meas_*`** poprvé za 3. 10. (PND 4. 10.) – odchylka prodeje by měla být ~0 (dosud −9,8 % kvůli bojleru).
6. **Sobota 4. 10. – test sauny** (uživatel): auto-detekce „Dnes sauna“ po 5 min, přepočet plánu, konec po 20 min bez topení,
   nákup ve VT po sauně, teplota zásuvky Plug E; prodej v 19 h se saunou (test 2. 10.: 4,2 → 1,9 kWh, SOC 20 %, VT nákup 0,7 kWh –
   posoudit, jestli prodávat a pak kupovat ve VT dává smysl).
7. ~~**B2 – úklid v1**~~ – hotovo 3. 10. (viz A6).
8. EV: vynucená aktualizace Kia při připojení funguje (30. 9., 1. 10. – data do 30 s), limit 2×/den, uživatel ponechává.

### A. Hotovo 2. 10.
1. **Smazán `sensor.pool_hours_done_legacy`** i se sčítáním v `sensor.pool_hours_done` a `base_final_daily_house_consumption`.
   Osiřelá entita i zařízení Shelly Pro 1PM (`switch.filtrace_switch`) smazány v UI (2. 10.).
2. **Noc 1./2. 10. – souběh EV a bojleru: OK.** EV 22:00–23:22 (89,7 → 99 %, auto končí na 99 %), bojler 22:06–22:42
   → EV staženo na 8 A ve 3f, `breaker_headroom_a` min. 7,5 A (22:06), po vypnutí bojleru zpět 11 A. Jednorázový cíl:
   23:31 cíl 80 %, 23:34 limit auta zpět 90 % (bod C2 ověřen).
3. **Bojler v noci (Shelly):** 22:06–22:42 1,33 kWh (PND 22 h: 1,31 kWh ✔), 03:45–04:02 dalších ~0,6 kWh.
   PND 1. 10. celkem 3,63 kWh (2 h 0,54 / 12 h 0,46 / 19 h 1,32 prodej / 22 h 1,31).

4. **Přejmenování Shelly Pro EM-50 (2. 10., se souhlasem uživatele):** zařízení „Bazén (Shelly Pro EM-50)“ (MAC …841fe890fc44)
   a „Bojler (Shelly Pro EM-50)“ (…ece334fd2370), všech 50 entit: `switch.bazen_filtrace_rele`, `sensor.bazen_cerpadlo_vykon`
   / `_energie` / `_ucinik` …, kanál 1 `bazen_kanal2_*`, servisní `bazen_em_*`; `switch.bojler_blokace_rele`, `sensor.bojler_vykon`
   / `_energie` …, `bojler_kanal2_*`, `bojler_em_*`. Historie a statistiky se přenesly. Ve starších záznamech (jsonl, §3b) zůstávají
   stará ID `shellyproem50_<MAC>_energy_meter_0_*`.
5. **Watchdog otestován (2. 10. 11:31, nechtěně):** při restartu AppDaemonu (> 3 min bez heartbeatu) watchdog vypnul filtraci,
   baterii přepnul na auto, limit přetoku 10 000 W, relé bojleru off + notifikace; po startu HAv2 vše převzalo zpět.

6. **Vylepšení 2. 10. odpoledne:**
   - **Ranní souhrn** `automation.hav2_ranni_souhrn` (`packages/hav2_notify.yaml`) v 7:40 do mobilu: včera (FVE, nákup,
     prodej, netto Kč – snímky `sensor.pv_generation_day`, `energy_net_cost_day` ve 23:59:50), bojler podle PND, plán dne,
     varování; vypínač `input_boolean.energy_morning_summary`. **Ověřit 3. 10. 7:40** (první plný souhrn).
   - **Automatická detekce sauny** (`_sauna_detect` v `hav2_app.py`): sauna > 1 kW ≥ 5 min bez „Dnes sauna“ → zapne
     se samo (začátek = skutečný start); po 20 min bez topení (a když v session běžela) se vypne → plán se přepočítá.
   - **Přesnost předpovědí:** `sensor.energy_forecast_day_morning` (6:00: FVE opravená/Solcast, základní spotřeba),
     `sensor.energy_forecast_accuracy` (23:59:50, atribut `history` 30 dní) → graf na Baterie & FVE. Podklad pro C8 (Solcast).
   - **Kontrola PND s měřeným bojlerem:** atributy `meas_*` v `sensor.energy_boiler_pnd_daily` (PND nákup ≈ GoodWe +
     bojler ze sítě, PND prodej ≈ prodej bez bojleru); dlaždice odchylek je použijí od prvního celého dne (3. 10. → PND 4. 10.).
   - **Úklid v1 (B2)** odložen na rozhodnutí po kontrole prodeje s blokací bojleru.
   - **Přehled → tok energie:** přidán uzel „Bojler“ (`sensor.bojler_vykon`, individual v power-flow-card-plus);
     síť = `sensor.energy_buy_gross_w` (odběr GoodWe + bojler ze sítě) / `sensor.energy_sell_net_w` (prodej bez bojleru),
     dům = `sensor.house_consumption_with_boiler_w` (`hav2_data.yaml`). Karta umí spotřebiče jen jako větev z domu.
   - **3. 10. – Energie v2 jako hlavní dashboard:** přidán pohled **PND** (`/energie-v2/pnd`, před Nastavení): 14 dní
     a 12 měsíců nákup/prodej (`sensor.pnd_data`) a nákup NT/VT (`sensor.pnd_tariff_data`). Ze starého dashboardu
     se nic dalšího nepřebírá (detail EV nechce, Solcast a teplota dávkovače už v2 má). Výchozí dashboard nastavuje
     uživatel v Profilu (i v mobilu); starý `lovelace` (záloha `archive/v1-2026-09-25/dashboard/lovelace-2026-10-03.json`)
     smazat po úklidu v1. HA záloha před úklidem `465189a7`.
     **Provedeno 3. 10.:** obsah Energie v2 zkopírován do hlavního dashboardu `lovelace` („Přehled“, přes
     `lovelace/config/save`, ověřeno 1:1), dashboard `energie-v2` smazán (konfigurace v `archive/energie-v2-dashboard-2026-10-03.json`).
     Cesty pohledů jsou teď `/lovelace/<path>` (prehled, plan, ev, …). Smazáno 17 automatizací v1 přes API;
     zbylé 2 EV automatizace smazal uživatel v UI.
   - **Úklid v1 (B2) HOTOVO 3. 10.:** smazány všechny automatizace v1 (19) + 3 osiřelé (`spustit_filtraci`,
     `get_data_from_dip`, `run_pnd_2`), skripty `fve_grid_export_*` (3), blueprinty `jan-trnka` (7), z YAML
     `input_boolean.time_to_use_overflows`, `input_select.season`, `input_number.winter_dod`, šablony Nighttime /
     Base Nighttime / Final Daily House Consumption, EV denní náklady, EV nabíjí ze solárů a 7 statistik v `sensor.yaml`
     (zůstaly Filtrace spuštěna, Base Average Daily Consumption). Uživatel v UI smazal helpery `filtrace_overrride`,
     `fve_battery_charge_*`, `goodwe_grid_export`, `ev_nt_start_*`, `ev_nabijeni_povoleno`, `ev_nabijeni_manualni_nt`,
     šablonu `sensor.filtrace`, utility metery Filtrace Daily, EV energie týdně/měsíčně a osiřelé entity.
     **Ponecháno (používá HAv2/PND):** Filtrace Sum, House Consumption Sum/Daily, ECO Volter Daily Energy,
     energy_buy/sell (+ _sum, _daily), fve_battery_charge/discharge_w, `ev_nt_*` celkem a měsíc, skripty Filtrace ON/OFF (Assist).
     Návrat k v1 už není možný bez obnovy HA zálohy `465189a7` (nebo `81c05fcd` před testem Auto).
     Sankey 2. 10. bez větve Celkem → Bojler: `energy_sources_total` má statistiky až od 9:29, „Celkem“ je menší
     než součet spotřebičů → na bojler (poslední v pořadí) nezbyde nic. Od 3. 10. v pořádku.

### B. Rozpracované – čeká na uživatele
1. **HOTOVO 2. 10. – blokování bojleru při prodeji** (arch. §5.4): relé Pro EM-50 → LT, plán SSR3 omezit 16–22 vyp+LT, `script.hav2_boiler_block`, `input_boolean.energy_boiler_block_on_sale`. **Ověřit při prvním prodeji:** relé on po dobu `discharge`, bojler 0 W (Shelly), PND export ≈ GoodWe export; po prodeji relé off. Auto-off 2 h v Shelly nastaven (2. 10.). První prodej s blokací: 2. 10. 19:00–20:00.
   Původní popis: **Bojler bere při prodeji z baterie (1. 10.):** prodej 19–20 h 5 kW → WATTrouter poslal do bojleru 1,32 kWh (~1,35 kW,
   celou L3), do sítě jen ~2,9 kWh místo 4,2 → ztráta ~3,6 Kč (výkup 6,24 vs. NT 3,51). Návrh: beznapěťový kontakt I/O
   bojlerového Pro EM-50 (`switch.bojler_blokace_rele`, nepoužitý) → vstup WATTrouteru **LT–GND**, ve WATTconfigu
   časový plán SSR3 typu „blokovat“ 00–24 s podmínkou „Binární vstup“; HAv2 sepne relé při `plan = discharge`
   (případně i jindy). **Uživatel pošle screenshot roletek** v záložce Časové plány (typ plánu u „vynutit“, „Binární
   vstup → žádný“). Bez hardwaru alternativa: plánovač počítá při prodeji ~⅓ přetoku do bojleru za hodnotu NT.
2. **Rozhodnutí po testu Auto (uživatel):** převzít řízení natrvalo (záloha → smazat 16 vypnutých automatizací v1 podle
   `archive/v1-2026-09-25/README.md`; pozor – v1 automatizace už odkazují na nový spínač filtrace; statistiky
   `sensor.ev_nt_*` ponechat), nebo návrat.
   Po smazání Pro 1PM (2. 10.) opraveno: skripty `filtrace_on`/`filtrace_off` spínají `switch.bazen_filtrace_rele`
   (zpřístupněné v Assist), GUI šablona `sensor.filtrace` čte `sensor.bazenova_filtrace_vykon`. Při úklidu v1 rozhodnout,
   zda je ponechat (archiv je vede jako „smazat“ / „nepotřebujeme“).

### C. Ověřit, až nastane
1. **Záporný výkup s plnou baterií** (nové 1. 10.): limit přetoku = příkon bojleru + 600 W (`hav2_boiler.NegPriceBoiler`)
   – náběh bojleru ~200 W/min, PND export ~0, po nahřátí limit 0 W a pokus po hodině; logbook „HAv2 přetok“.
2. ~~EV – jednorázový cíl nad limitem auta~~ – ověřeno v noci 1./2. 10. (viz A2).
3. **Filtrace – nesoulad s venkovními vypínači:** první notifikace „vypnuto vypínačem“ / „běží ručně“
   (`switch_mismatch` v `sensor.pool_controller`).
4. **Večerní prodej z baterie:** start/konec na čtvrthodině, ráno ve VT nechybí baterie (1. 10.: prodej 19:00–20:00 OK).
5. **Profil bojleru v plánu** se přeučuje z 16–18 h na noc (medián 8 dní, měření Shelly + PND) – do ~5. 10. plán
   nadhodnocuje večerní nákup; atribut `boiler_source` v `sensor.energy_load_forecast`.
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
- Energy dashboard: bojler záměrně ne (je mimo měření GoodWe → nesmyslná spotřeba domu); místo toho na Energie v2
  grafy kWh/den (Bazén, Úspory) a od 2. 10. sankey + skládaný graf spotřeby na Úsporách.
- Druhý kanál (IB) obou Pro EM-50 volný.
- **Infrasauna – hotovo 2. 10.:** Shelly Plug E (16 A) = `switch.sauna`, `sensor.sauna_power`, `sensor.sauna_energy`
  (zařízení „Sauna (Shelly Plug E)“, entity přejmenovány z „Zásuvka řízená/Filtrace“ se souhlasem uživatele). Test 2. 10.:
  **2,28 kW, 9,6 A, fáze L2** (s filtrací), termostat za 8 min nespínal. Odečtená z `sensor.base_house_consumption`
  a z profilu plánovače (`STAT_SAUNA`); „Dnes sauna“ (`input_boolean.energy_sauna_today`, `input_datetime.energy_sauna_start`,
  `input_number.energy_sauna_duration_h`, `..._power_kw` = 2,3) přičte zátěž do slotů plánu (`hav2_planner.add_extra_load`,
  baterie ji může krýt) a po konci se sama vypne; atribut `sauna` v `sensor.energy_plan`. Dashboard: Přehled → Sauna,
  sankey a skládaný graf. Později možná Shelly Pro 1PM v rozvaděči bazénu (L2, B16, chybí místo na liště – elektrikář);
  pak nové entity přejmenovat na stejná `sauna_*` ID.

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
2. **Převzetí řízení** (se schválením): záloha HA → smazat automatizace v1 (jako první `ev_blokovat_vybijeni_baterie`,
   `predictive_overflow_negative_price`) → baterie DoD 80 % → Auto zůstává.
3. **Po převzetí:** smazat helpery „nepotřebujeme“ (archiv README). AppDaemon PND app vypnout až po přepojení
   `hav2_boiler.py` na data HACS integrace a ověření VT/NT.
4. ~~Volitelně: stránka EV „Aktuální session“, sankey na Úsporách~~ – hotovo 2. 10. (viz §2 Dashboard).

## 5. Známé drobnosti
- `sensor.pool_water_temperature` a `sensor.pool_hours_recommended` mají hodnotu až po ≥ 10 min běhu filtrace.
- `sensor.ev_deadline_slack_h` je nedostupný bez zadaného termínu (záměr; na dashboardu skrytý).
- Solcast API 10/10 denně vyčerpáno – auto-update integrace; při výpadku konzervativní režim (× 0,5).
- EV náklady se počítají od 25. 9. 2026; historie v1 (ruční NT) je v `sensor.ev_nt_*`.
