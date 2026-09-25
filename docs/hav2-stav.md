# HAv2 – stav a předávka (k 25. 9. 2026)

Zadání: `docs/HAv2_prompt.md` · Návrh (schválený): `docs/hav2-architektura.md` · Záloha v1 a plán mazání: `archive/v1-2026-09-25/README.md`

## 1. Kde jsme

| Fáze | Stav |
|---|---|
| 1 – inventura | hotovo |
| Krok 0 – záloha v1 | hotovo (HA záloha `pre-HAv2-2026-09-25` id `9740260a`, archiv `archive/v1-2026-09-25/`, commit 88482e2) |
| Krok A – statistiky a náklady | hotovo (`packages/hav2_statistics.yaml`, GUI utility metery, Energy dashboard s cenami) |
| 2 – návrh | schváleno 25. 9. 2026 |
| 3 – implementace | **hotovo** – datová vrstva, baterie, EV, bazén, dashboard `energie-v2` |
| 4 – ověření a převzetí řízení | **čeká**: pár dní pozorování v režimu „Jen doporučení“ |

**Aktuální režim:** `input_select.energy_system_mode` = **Jen doporučení**. Přepínače
`input_boolean.energy_battery_control`, `ev_control`, `pool_control` jsou **vypnuté**.
HAv2 nic nezapisuje do střídače, wallboxu ani čerpadla – řízení mají stále staré automatizace v1.

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

Publikuje: `sensor.energy_plan`, `sensor.energy_load_forecast`, `sensor.ev_plan`, `sensor.ev_regulator`,
`sensor.pool_plan`, `sensor.pool_controller`, `input_text.*_last_decision`, `input_datetime.hav2_heartbeat`.

Testy: `source venv/bin/activate && pytest tests/hav2 -q --no-cov` (47 testů).

### Dashboard `energie-v2`
Zdroj pravdy `dashboards/energie-v2.yaml`, nahrání:
```
source venv/bin/activate && set -a && source .env && set +a
python dashboards/push_dashboard.py energie-v2 dashboards/energie-v2.yaml
```
HACS karty: power-flow-card-plus, apexcharts-card, flex-table-card, auto-entities, card-mod, mushroom.

## 3. Úskalí (důležité pro další práci)

- **Fakturace po fázích** – nikdy součtový výkon/čítače; základ `energy_buy/sell` (Σ fází).
- **Bojler (WATrouter, 2,2 kW, L3) je mimo měření GoodWe** – baterie ho nekryje; nucený ohřev denně od 16:09; model `grid_only`.
- **GoodWe EMS:** `conserve` nabíjí i ze sítě – nepoužívat; „drž SOC“ = `battery_standby`.
- **AppDaemon `set_state` zahazuje falsy hodnoty** (0, False, i v seznamech) → čísla a logické hodnoty v atributech jako text, seznamy řádků s textovými hodnotami, nebo JSON řetězec.
- AppDaemon: nová podsložka apps se načte až po restartu doplňku; `log:` jen s definicí v `appdaemon.yaml`; `logbook.log` bez `entity_id`.
- Trigger šablony: `this` nejde ve `variables`.
- Nové YAML platformy (statistics, history_stats, integration) potřebují restart HA.
- Utility metery vždy přes GUI (YAML neprojde validátorem).
- Markdown karty: obsah jako `|` (literal), ne `>` – jinak se rozbijí tabulky.
- Dashboard dlaždice: vždy explicitní `grid_options`, jinak se v sekcích „rozsypou“.
- Štítky: nové HAv2 entity vždy `hav2` + oblast (`hav2_system` / `hav2_baterie` / `hav2_ev` / `hav2_bazen`) – výjimka v CLAUDE.md. Senzory z AppDaemonu štítek mít nemůžou.
- **cez_pnd (HACS) má lokální patch** v HA `/config/custom_components/cez_pnd/http_client.py` (záloha `.orig-1.1.1`) – update z HACS ho přepíše, pak znovu aplikovat nebo počkat na opravu (issue igracek/HACS_CEZD_PND#2).
- Citlivé: `.env` (HA_TOKEN), `config/appdaemon/apps/apps.yaml` (PND heslo), Keychain `cez_pnd_test` – nikdy nevypisovat ani necommitovat.

## 4. Další kroky

1. **Pozorování (pár dní):** stránky Plán a Nastavení → Log rozhodnutí; porovnat doporučení se skutečností
   (baterie přes noc, EV v NT, filtrace ze slunce). Zaznamenat odchylky.
2. **26. 9.:** ověřit `sensor.energy_pnd_deviation_import/export_pct` (první den s PND daty za 25. 9.).
3. **Slunečný den:** experiment předehřevu bojleru (`input_boolean.energy_boiler_preheat`, §5.6) – výsledek z PND 15 min.
4. **Převzetí řízení po oblastech (každá se schválením):**
   1. záloha (HA backup) → smazat staré automatizace oblasti podle `archive/v1-2026-09-25/README.md`
      (jako první `ev_blokovat_vybijeni_baterie` a `predictive_overflow_negative_price`),
   2. baterie: `number.goodwe_maximum_vybiti_v_siti` = 80 % (DoD pro min SOC 20 %),
   3. `energy_system_mode` = Auto + přepínač oblasti on; sledovat první den.
5. **Po převzetí:** smazat helpery „nepotřebujeme“ (archiv README), vypnout AppDaemon PND app po ověření dat z HACS integrace (denní, 15 min, VT/NT).
6. Volitelně: sankey graf na stránce Úspory, Solcast auto-dampening, duplicitní `input_number.battery_capacity` (YAML + storage).

## 5. Známé drobnosti
- `sensor.pool_water_temperature` a `sensor.pool_hours_recommended` mají hodnotu až po ≥ 10 min běhu filtrace.
- `sensor.ev_deadline_slack_h` je nedostupný bez zadaného termínu (záměr; na dashboardu skrytý).
- Solcast API 10/10 denně vyčerpáno – auto-update integrace; při výpadku konzervativní režim (× 0,5).
- EV náklady se počítají od 25. 9. 2026; historie v1 (ruční NT) je v `sensor.ev_nt_*`.
