# PND (ČEZ Distribuce) – oprava INTERVAL exportu + NT/VT tarify

## Kontext

**Aplikace:** `config/appdaemon/apps/HomeAssistant-CEZDistribuce-PND/pnd.py` – AppDaemon app
nainstalovaná přes HACS (repo `HomeAssistant-CEZDistribuce-PND`). Denně (06:00, event `run_pnd`
z automatizace `automation.run_pnd`, id `1778526312883`) se přihlašuje na portál PND ČEZ
Distribuce a stahuje:
- **DAILY** sekci – report za "Včera" (07 Profil spotřeby, 08 Profil výroby)
- **INTERVAL** sekci – reporty za celé nastavené období (`DataInterval` v `apps.yaml`)

**Problém (od 13.8.2026):** INTERVAL sekce padala s `FileNotFoundError` při přejmenování
`pnd_export.csv`. DAILY sekce fungovala bez problémů.

**Instalace přes HACS** – to znamená, že aktualizace aplikace přes HACS **přepíše** `pnd.py`
zpět na vendor verzi a smaže naše opravy. Proto existuje záloha (viz sekce Zálohy níže).

---

## Zjištěné příčiny (2 nezávislé bugy)

### 1. Nativní Selenium `.click()` nefunguje na záložkách reportů a tlačítku exportu

Ověřeno testem přímo proti reálnému portálu (samostatný skript mimo AppDaemon/HA): kliknutí na
záložku reportu (07/08/17) nebo na tlačítko "Exportovat data" se často "ztratí" – zůstane vybraný
předchozí report, nebo se rozbalovací nabídka nerozevře (`aria-expanded` zůstane `false`). Skript
pak stáhl **pořád to samé CSV** bez ohledu na to, který report "kliknul".

**Oprava:** JS klik (`driver.execute_script("arguments[0].click();", el)`) místo nativního
`.click()`, plus čekání dokud `aria-expanded` skutečně nepřejde na `true` (viz metody
`click_report_link()` a `export_csv_and_rename()` v `pnd.py`).

### 2. Čísla v CSV mají různý formát podle prostředí (locale headless Chrome)

- Na lokálním Macu (test) portál vracel český formát: `"6 733,39"` (mezera = tisíce, čárka = desetiny)
- V produkčním AppDaemon Dockeru portál vrací anglický formát: `"8,228.968"` (čárka = tisíce, tečka = desetiny)

Pandas `.sum()` na neopraveném textovém sloupci dělá **spojování řetězců**, ne součet – u malých
denních hodnot (report 07/08, bez tisícového oddělovače) to náhodou fungovalo, u velkých kumulativních
hodnot (report 17, v tisících kWh) to dávalo nesmyslné výsledky / `NaN`.

**Oprava:** funkce `_normalize_decimal_str()` / `parse_flexible_decimal()` v `pnd.py` – detekuje
podle POZICE poslední čárky/tečky, která je desetinná a která tisícová, funguje pro oba formáty.
`"-"` (placeholder pro ještě neuzavřené dny) se mapuje na `NaN` přes `errors="coerce"`.

### 3. `clamp_data_interval()` se počítal jen jednou při startu aplikace, ne při každém běhu (zjištěno 15.8.2026)

`self.datainterval = clamp_data_interval(...)` byl původně v `initialize()` – u AppDaemon apek se
ale `initialize()` volá **jen při startu/reloadu aplikace**, ne při každém `run_pnd` eventu. Po
posledním nasazení (14.8. večer) AppDaemon aplikaci reloadnul, spočítal "dnešek = 14.8." a tahle
hodnota zůstala v `self.datainterval` navždy zmrzlá – i běh dnes ráno (06:00) i ruční testy pořád
posílaly portálu konec intervalu `"14.08.2026 00:00"`, takže 14.8. (natož další dny) by se **nikdy**
nestáhlo. Ověřeno v logu AppDaemonu (`ha addons logs a0d7b954_appdaemon`): `Data Interval Entered -
'01.01.2026 00:00 - 14.08.2026 00:00'` i hodiny po půlnoci 15.8.

**Oprava:** přepočet přesunut do `run_pnd()` (počítá se nově při každém běhu), a jako zdroj "dnešního
data" se místo syrových OS hodin kontejneru (`dt.now()`) používá `self.datetime()` – AppDaemonův
vlastní, na HA navázaný čas (doporučený postup pro AppDaemon apky obecně). Ověřeno: po opravě log
ukazuje správně `'01.01.2026 00:00 - 15.08.2026 00:00'` a `sensor.pnd_data`/`sensor.pnd_tariff_data`
okamžitě doplnily chybějící 14.8.

---

## Vedlejší vylepšení

**`clamp_data_interval()`** – `DataInterval` z `apps.yaml` (natvrdo `"01.01.2026 00:00 - 31.12.2026 00:00"`,
tj. rozsah do budoucna) se při každém běhu ořízne na dnešek. Nejde o hlavní příčinu původního pádu
(ta byla v bodě 1 výše), ale nemá smysl žádat portál o dny, které ještě neexistují, a `apps.yaml` se
tak nemusí ručně upravovat. (Viz bug č. 3 výše – tohle zlepšení mělo vlastní skrytou chybu.)

**Posun plánovaného běhu z 00:30 na 06:00** (automatizace "Run PND", `automations.yaml`, id
`1778526312883`) – zjištěno 15.8.2026: portál ČEZ má u denních dat zpoždění zpracování, které
může být **déle než 24 hodin**. Běh v 00:30 (i ruční re-run v 8:31 téhož dne) opakovaně nezachytil
data za předchozí den, protože je ČEZ ještě neměl uzavřená (stejný jev jako `-` placeholdery u
NT/VT). Posunutím na 06:00 má ČEZ víc času data zpracovat před stažením.

---

## Nová funkce – report "17 Registry za den (+E, -E)"

Stahuje se v INTERVAL sekci jako `range-tariff.csv`, hned po 07/08. Sloupce (`+E`, `-E`, `+E_NT`,
`+E_VT`) jsou **kumulativní stavy měřidla**, ne denní hodnoty (na rozdíl od reportu 07/08) – proto
se denní NT/VT spotřeba počítá jako mezidenní rozdíl (`.diff()`).

Nové senzory:
- `sensor.pnd_tariff_data` – historická řada NT/VT spotřeby po dnech (atributy `pnddate`, `low_tariff`, `high_tariff`)
- `sensor.pnd_total_interval_consumption_low_tariff` / `_high_tariff` – součet NT/VT za celé období
- `sensor.pnd_tariff_low` / `sensor.pnd_tariff_high` – **poslední uzavřený den** (ne nutně včerejší –
  report 17 může mít u posledních dnů `-` placeholder, dokud ČEZ data neuzavře; kód najde poslední
  den s reálnou hodnotou)

Ověřeno reálným během 14.8.2026: NT=30,71 kWh, VT=1,65 kWh za 13.8.2026; součet od 1.1. do 13.8.:
NT=306,64 kWh, VT=1265,54 kWh.

---

## Zálohy

Umístěny v `backups/` (v repu, ale **mimo** `config/` – `make push`/`make pull` používají
`rsync --delete`, takže cokoliv v `config/` co lokálně neexistuje, push smaže; `backups/` je
navíc explicitně v `.rsync-excludes-push`/`-pull`, takže se do syncu vůbec nezapojuje):

| Soubor | Co je to |
|---|---|
| `backups/pnd.py.pre-fix-20260814.py` | Originální HACS verze (před jakýmikoliv úpravami) |
| `backups/pnd.py.fixed-20260814.py` | Aktuální opravená verze – nasazena a ověřena funkční |

**Pokud HACS aktualizuje aplikaci a přepíše `pnd.py`:** zkopírovat `backups/pnd.py.fixed-20260814.py`
zpět do `config/appdaemon/apps/HomeAssistant-CEZDistribuce-PND/pnd.py` a `make push`.

**Pozor:** záloha na živém HA hostu samotná (bez lokální kopie mimo `config/`) nefunguje – první
pokus o zálohu (`pnd.py.bak-*` přímo na HA) smazal následující `make push` právě kvůli `--delete`.

---

## Jak otestovat reálným během

1. `make push` (validace + nahrání na HA)
2. Spustit automatizaci `automation.run_pnd` ručně (Settings → Automations → "Run PND" → Spustit,
   nebo `automation.trigger` s `entity_id: automation.run_pnd`) – nečekat na plánovaný běh v 06:00
3. Sledovat log AppDaemonu (add-on log) nebo počkat na `binary_sensor.pnd_running` → `off`
4. Zkontrolovat `sensor.pnd_script_status` (očekáváno `Stopped` / `Finished`) a nové senzory výše

---

## Dashboard – zjištěné chyby a oprava

Dashboard "PND" je spravovaný přes HA UI (Lovelace `.storage`), takže podle pravidel projektu
se needituje přímo v repu – finální YAML k ručnímu vložení do editoru dashboardu (Nastavení
dashboardu → ⋮ → "Upravit v YAML") je uložený jako referenční kopie v **`docs/pnd-dashboard.yaml`**.

### Bug 1 – karta "PND Včerejší stav" ukazovala špatné dny

Karta používala `sensor.pnd_consumption`/`sensor.pnd_production` (jednorázové hodnoty "za
včerejšek") s `apexcharts-card group_by: func: last, duration: 1d`. Toto řadí sloupce podle
**času zápisu stavu do HA** (recorder), ne podle dne, ke kterému data reálně patří. Ověřeno
přes `/api/history` – konkrétní den se dokázal "přesunout" na jiný sloupec, když:
- ČEZ portál později revidoval hodnotu (viz `-` placeholdery u NT/VT výše) a stav se v HA
  přepsal v jiný den, než ke kterému se váže
- skript se spustil mimo pravidelného plánu (např. ruční test)

Důkaz: reálná data (`sensor.pnd_data`) ukazovala spotřebu 11.8.=13,0 kWh a 12.8.=15,5 kWh, graf
ale u obou dnů ukazoval téměř nulu.

**Oprava:** karta přestavěna na stejný zdroj jako "Posledních 10 dní" – `sensor.pnd_data` s
`data_generator`, který mapuje hodnoty na jejich skutečné datum (`pnddate` atribut), ne na čas
zápisu do recorderu. Číselný souhrn v hlavičce zachován přes `header.show_states` +
`colorize_states` (místo defaultní legendy vázané na `entity.state`).

### Bug 2 – měsíční graf měl "vynulované" některé měsíce (březen/květen/červenec)

`group_by: { func: sum, duration: '1month' }` v apexcharts-card evidentně nepočítá se skutečnou
délkou kalendářního měsíce, ale s nějakou pevnou aproximací (~30 dní). Chyba se kumuluje – ověřeno
výpočtem, že "kotva" (2. den měsíce) postupně driftuje těsně k hranici bucketu:

| Měsíc | dní od startu / 30.44 |
|---|---|
| Únor | 1,05 (OK, uprostřed bucketu) |
| **Březen** | **1,97 (těsně na hranici → špatně přiřazeno)** |
| Duben | 2,99 (OK) |
| **Květen** | **3,98 (těsně na hranici → špatně přiřazeno)** |
| Červen | 4,99 (OK) |
| **Červenec** | **5,98 (těsně na hranici → špatně přiřazeno)** |

Týdenní graf (`duration: 7d`) tímto netrpí – 7 dní je vždy přesně 7 dní, žádná kumulativní odchylka.
Ověřeno nezávislým přepočtem přes pandas (`groupby(to_period('M'))`) – týdenní i "poslední neúplný
měsíc" (srpen) seděly na desetinu kWh přesně, ale plné měsíce březen/květen/červenec byly v grafu
skoro nulové.

**Oprava:** `group_by` na měsíčních sériích úplně odstraněn, agregace po kalendářních měsících se
počítá ručně v `data_generator` (JS `getFullYear()+'-'+getMonth()` jako klíč, `Object.values(...).sort(...)`
na konci) – nezávisí na žádné aproximaci délky měsíce.

### NT/VT karty

4 nové karty (Včerejší stav / Posledních 10 dní / Týdenní / Měsíční agregace) analogické ke
spotřebě/výrobě, ale zdroj `sensor.pnd_tariff_data` (`low_tariff`/`high_tariff`). Na rozdíl od
spotřeby/výroby (kde je spotřeba `invert: true`, tj. graf je "divergentní" nahoru/dolů) jsou NT a VT
**obě kladné a naskládané na sobě** (bez `invert`) – NT+VT dohromady = celková denní/týdenní/měsíční
spotřeba, protože jde o rozklad jedné veličiny, ne o dva opačné toky energie. Barvy: NT modrá
(`#2196F3`), VT oranžová (`#FF9800`), odlišné od červené/zelené u spotřeby/výroby.
