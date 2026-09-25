# Archiv v1: řízení energie před nasazením HAv2 (2026-09-25)

Záloha stávajícího řešení před výstavbou nového systému (zadání `docs/HAv2_prompt.md`).
Vše, co HAv2 smaže nebo nahradí, je tady uložené v plné definici.

## Zálohy
| Co | Kde |
|---|---|
| Plná HA záloha (config + addony, bez DB) | HA → Nastavení → Systém → Zálohy: `pre-HAv2-2026-09-25` (id `9740260a`) |
| Lokální tarball celé `config/` | `backups/ha_config_20260925_144225.tar.gz` |
| YAML (automatizace, skripty, configuration, sensor) | `yaml/` |
| Dashboard `lovelace` (views Home, PND, FVE, EV) + resources | `dashboard/lovelace.json`, `dashboard/lovelace_resources.json` |
| Blueprinty jan-trnka | `blueprints/jan-trnka/` |
| UI helpery input_* (storage) | `helpers/input_*.json` |
| UI helpery template / integration / utility_meter / statistics / history_stats | `helpers/ui_helpers_config_entries.json` |
| Aktuální stavy helperů, automatizací a energetických senzorů | `states_snapshot.json` |

## Plánované k odstranění (po oblastech, v okamžiku převzetí řízení v2)

### FVE / baterie
| id | alias | typ | nahradí v2 |
|---|---|---|---|
| 1717828775243 | Přetoky nastavit | automatizace | řízení limitu přetoku `energy_*` |
| 1772571943115 | Time to Use Overflow | blueprint jan-trnka | plánovač baterie |
| 1772629832725 | Auto Set DoD | blueprint | plánovač baterie (min SOC 20 %) |
| 1772632237910 | Fully Charge Battery Once a Week | blueprint | plánovač (týdenní balanční nabití, pokud bude potřeba) |
| 1772656770792 | Discharge Battery to the Grid | blueprint | pravidlo prodeje ve špičce spotu |
| 1772657599353 | Eco Discharge When Low Price at Noon | blueprint (nefunkční, používá unavailable senzor) | plánovač |
| 1772657930649 | Turn Off Eco Discharge Mode When Peak | blueprint | plánovač |
| 1772991037309 | Disable Overflow | blueprint | řízení limitu přetoku |
| predictive_overflow_negative_price | Prediktivni pretoky pri zaporne cene | automatizace | řízení při záporných cenách |
| fve_grid_export_status, 1717590811347, 1717590841020 | FVE: grid export STATUS/OFF/ON | skripty | řízení limitu přetoku |
| 1717674886307 | FVE: solcast updates | automatizace (vypnutá) | auto-update Solcastu |

### EV
| id | alias |
|---|---|
| ev_blokovat_vybijeni_baterie | EV - blokovat vybíjení baterie (mazat **první**, zapíná jiné automatizace) |
| ev_nabijeni_zaporne_ceny_pretoky | EV nabíjení - záporné ceny nebo přetoky |
| ev_zastavit_velky_import | EV zastavit - příliš velký import |
| ev_zastavit_plna_baterie | EV zastavit - auto přestalo nabíjet |
| ev_regulace_proudu | EV regulace proudu při nabíjení |
| ev_nabijeni_nt_manualni_start / _stop | EV nabíjení NT - manuální start/stop |
| kia_ev6_force_update_on_charge | KIA EV6: force update |

### Bazén
| id | alias |
|---|---|
| 1718351641005 | Ovládání filtrace - přetoky |
| filtrace_on, filtrace_off | skripty Filtrace ON/OFF |

### Helpery a senzory označené „nepotřebujeme“
`sensor.nighttime_consumption`, `sensor.average_daily_consumption`, `sensor.average_consumption_in_last_20_minutes`, `binary_sensor.ev_nabiji_ze_solaru`, `sensor.filtrace` (duplikát), `input_boolean.fve_battery_charge_enable`, `input_datetime.fve_battery_charge_from/_to`, `input_boolean.time_to_use_overflows`, `input_select.season`, `input_text.goodwe_grid_export`. NT-session helpery (`input_number.ev_nt_*`) se smažou až po nahrazení novým výpočtem nákladů EV. Jejich poslední hodnoty jsou v `states_snapshot.json`.

**Zůstávají:** PND automatizace (1778526312883, 1778526595548), `energy_buy/sell*`, `house_consumption_*` a další „převzít“ z plánu.

## Obnova
1. Celé řešení: obnovit HA zálohu `pre-HAv2-2026-09-25` (restartuje HA).
2. Jednotlivě:
   - YAML zkopírovat z `yaml/` do `config/`, pak `make push` a reload automatizací a skriptů.
   - Blueprinty zkopírovat do `config/blueprints/automation/jan-trnka/`.
   - UI helpery znovu vytvořit v UI podle `helpers/*.json`.
   - Dashboard: Raw configuration editor, vložit `views` z `dashboard/lovelace.json`.
