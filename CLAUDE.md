# CLAUDE.md – Projekt research_db

## Refactoring-Status
Vollständige Spec unter `docs/instructions/refactoring_spec.md`.
- Session 1: DB-Migration + Infrastruktur — **abgeschlossen**
- Session 2: Module umschreiben (Logging, Retry, Schema-Mapping, mode-Parameter) — **abgeschlossen**
- Session 3: Smoke-Tests (Eikon muss laufen) — **ausstehend**
- DB-Migration + Bereinigung vollständig durchgeführt (März 2026)

## Credentials
- Alle Credentials in `C:\Users\Miebs\PycharmProjects\Config\.env`
- Variablen: `REFINITIV_APP_KEY`, `DB_USER`, `DB_PASS`, `DB_HOST`, `DB_PORT`, `DB_NAME`, `FRED_API_KEY`

## Datenbank
- PostgreSQL, Datenbank: `research_db`
- Schema `pub_config`: `data_sources`, `stock_indices`, `futures_underlyings`, `rates`, `pipeline_runs`, `fx_pairs`, `ric_org_map`
- Schema `pub_equity`: `index_overview`, `index_constituents`, `stock_prices_daily`, `stock_fundamentals_quarterly`, `stock_dividends`, `index_prices_daily`, `computations_index_mcap`
- Schema `pub_options`: `options_chains`, `options_prices`, `computations_options_information`, `computations_svix`
- Schema `pub_rates`: `usd_rates`, `fx_rates`
- Schema `pub_futures`: `futures_chains`, `futures_prices`

Hinweis: `computations_*`-Tabellen enthalten Berechnungsergebnisse (MCap, implizite Vola, SVIX) — keine Pipeline-Tabellen, werden nicht direkt von Fetch-Modulen beschrieben.

## Architektur (nach Refactoring)
- **Tabellennamen**: niemals hardcoden — `from config.schema import get_table, TBL_*`
- **Logging**: `from config.logging_setup import setup_logging`
- **Eikon Retry**: `from modules.utils import eikon_fetch` (wraps ek.get_data)
- **Pipeline-Log**: `from config.pipeline import run_start, run_finish`
- **DB-Engine**: `from config.db import engine` (QueuePool, pool_pre_ping)
- **Migration ausführen**: `python schema/migrate.py` (idempotent)
- **Backfill**: `python run_backfill.py --start 2016-01-01 --end 2026-03-01`

## options_chains — Unified Table (nach Refactoring)
- PK: `(underlying_ric, option_ric)` — kein `snapshot_date` mehr
- `option_ric`: Basisform (kein führendes `/`, kein `^`-Suffix)
- `option_ric_hist`: historischer RIC mit `^`-Suffix (für abgelaufene Optionen)
- `first_seen_date`: snapshot_date des ersten Auftretens
- Eikon live fetch: `'/' + option_ric`; historisch: `option_ric_hist`

## options_prices — hist-RIC (Stand April 2026)
- `option_ric`: **hist-RIC mit `^`-Suffix** — gilt für aktive UND abgelaufene Optionen
- PK: `(option_ric, price_date)`
- Beispiel: `SPXb172669000.U^B26`, `STXE40000L5.EX^L25`, `AXJO5800M6.AX^M16`
- **Warum `^`**: 1-stellige Jahreszahl-RICs (STOXX50E, SSMI, AXJO, NSEI) wiederholen sich alle 10 Jahre — ohne Suffix wäre `AXJO5800M6.AX` in 2016 und 2026 identisch
- **Join-Regel**: immer `options_chains.option_ric_hist = options_prices.option_ric` — NICHT über `option_ric`
- Umstellung durchgeführt April 2026: bestehende SPX-Rows per UPDATE migriert, `fetch_option_prices.py` angepasst

## API-Constraints (Eikon)
- **Ausschließlich `ek.get_data()`** aus dem `eikon`-Paket. Kein `rd`, kein `rdp`, kein `get_timeseries`.
- Eikon Desktop/Workspace muss auf dem Rechner laufen (API-Proxy).
- Rate Limiting: min. 0.5s zwischen Calls, **3600s (1h) Backoff bei 429**, **24 Retries** (entspricht 24h Abdeckung). Erfahrungswert: Sperre dauert ~24h.
- `eikon_fetch()` in `utils.py`: `max_retries=24`, `sleep=3600.0`
- `_do_fetch()` in `build_index_hist_chains.py`: ebenfalls 24 Retries à 1h

## API-Constraints (FRED)
- Paket: `fredapi` (`Fred(api_key=...)`), Key aus `.env` (`FRED_API_KEY`)
- Serien: konfiguriert in `pub_config.rates` (nicht mehr hardcodiert)

## Codestandards
- Idempotenz überall: Vor jedem Schreibvorgang prüfen ob Daten existieren.
- Fehlertoleranz: Einzelfehler loggen, Pipeline nicht abbrechen.
- Module exponieren eine `run(snapshot_date: str)`-Funktion.
- `snapshot_date` ist immer der 1. des aktuellen Monats.

## Pipeline-Module (run_monthly.py)
1. `modules/fetch_constituents.py` — Index-Constituents (Eikon); SPX point-in-time via `TR.IndexConstituentRIC`
2. `modules/fetch_option_chains_index.py` — Option Chains Indizes (Eikon); setzt `expiry_type`
3. `modules/fetch_option_chains_constituents.py` — Option Chains Constituents (Eikon); setzt `expiry_type`
4. `modules/fetch_option_prices.py` — Option Prices (Eikon, delta: 1 Jahr zurück)
5. `modules/fetch_usd_rates.py` — USD Zinskurve (FRED CMT, täglich inkrementell)
6. `modules/fetch_prices_daily.py` — Tagespreise OHLCV, SPX Constituents (Eikon, 10 Jahre)
7. `modules/fetch_fundamentals_quarterly.py` — Quartalsfundamentals inkl. shares_outstanding, SPX Constituents (Eikon)
8. `modules/fetch_dividends.py` — Dividenden SPX Constituents (Eikon)
9. `modules/fetch_futures_chains.py` — Futures Stammdaten (Eikon); liest aus `pub_config.futures_underlyings`
10. `modules/fetch_futures_prices.py` — Futures Preiszeitreihen (Eikon, delta: 1 Jahr)
11. `modules/fetch_index_prices_daily.py` — Indexpreise täglich (Lokalwährung, 30 Jahre History)
12. `modules/fetch_fx_rates.py` — FX-Spotkurse (Eikon, Steuerung über `pub_config.fx_pairs`)
13. `modules/fetch_ric_org_map.py` — RIC → Unternehmens-PermID (muss **vor** `compute_index_mcap` laufen)

## Bekannte Eikon-Eigenheiten (wichtig für zukünftige Module)

### PUTCALLIND-Mapping (internationale Indizes)
- Standard: `'C'` / `'P'` (erste Zeichen)
- HKEx (HSI): liefert `'C-EU'` statt `'C'`
- NSE (Nifty 50): liefert `'Call'` / `'Put'` (ausgeschrieben)
- **Regel**: Immer nur das **erste Zeichen** verwenden, case-insensitive
- Implementiert in `_map_option_type()` in `fetch_option_chains_index.py` und `fetch_option_chains_constituents.py`

### Historische Optionspreise: `^`-Suffix (WICHTIG)
- **`^`-Suffix funktioniert für**: Eurex (STOXX50E, SSMI), ASX (AXJO), SPX (CBOE) ✓
- **`^`-Suffix funktioniert NICHT für**: ICE/LIFFE (FTSE/LFE) vor 2024 — Eikon hat nur ~2 Jahre History
- **HSI (Hang Seng)**: kein `^`-Suffix-Support, nur ~2 Jahre History über Basis-RIC, nicht im Chain Builder implementiert
- **Historische RICs niemals mit führendem `/`** — ausschließlich Basisform + `^`-Suffix verwenden
- **`^`-Suffix-Regel**: Immer den **Call-Monatsbuchstaben** verwenden — auch für Put-RICs
  - Dezember Call-Buchstabe = `L` → Suffix `^L25` gilt für Calls **und** Puts
  - Implementiert in `build_option_ric_hist()` in `modules/utils.py` (generisch, nicht SPX-spezifisch)
  - Buchstabe wird aus `expiry_date.month` abgeleitet (A=Jan..L=Dez) — unabhängig vom RIC-Format
- **1-stellige Jahreszahl (STOXX50E, SSMI, AXJO, NSEI)**: RICs wiederholen sich alle 10 Jahre
  - Eikon liefert Daten für den aktuell aktiven Zyklus (z.B. 2026 statt 2016)
  - Historische Put-Daten für abgelaufene Zyklen (z.B. Jan 2016 nach dem 16.01.2026) nicht mehr abrufbar
  - `build_index_hist_chains.py` erkennt 10-Jahres-Rollover und überschreibt alte Einträge **nur vorwärts** (neuere expiry gewinnt)

### RIC-Drift bei historischen Constituents (geprüft Nov. 2026)
- `TR.IndexConstituentRIC` behandelt zwei Arten von RIC-Wechsel unterschiedlich:
  - **Umbenennung / Delisting: rückwirkend** — ein heutiger Abruf von 2015 liefert `SKYD.N` (umbenannt Sept. 2026) und `WBD.OQ^J26` (delistet Okt. 2026) für *alle* Stichtage
  - **Handelsplatzwechsel: point-in-time** — `BKR.N` bis 2021-11-30, `BKR.OQ` ab 2021-12-31, im selben Abruf korrekt getrennt
- Folge: Snapshots aus verschiedenen Abrufzeitpunkten tragen für dieselbe Firma verschiedene RICs. **Mitgliedschafts-Diffs deshalb über `company_name` oder `org_permid` rechnen, nie über `constituent_ric`** — sonst wirken 134 von 137 Monatspaaren verschieden, während es über den Namen 77 % identische sind (Mittel 0,47 Titel, max. 6)
- In `stock_prices_daily` entstehen dadurch zwei Reihen je Firma (`FITB.OQ` bis 2026-04-07, `FITB.N` ab 2026-06-15). Auflösung über `pub_config.ric_org_map.successor_ric`
- `TR.OrganizationID` kommt **numerisch** — ohne Cast landet `'5001437821.0'` in der VARCHAR-Spalte und die Entdopplung je Unternehmen greift nicht mehr. `normalize_permid()` in `modules/utils.py`

### Spaltenname-Fallen
- `TR.SharesOutstanding` → Spalte heißt **`'Outstanding Shares'`** (nicht `'Shares Outstanding'`)
- `TR.GICSSector` → liefert den **Namen** (`'Information Technology'`), nicht den 2-stelligen Code
- GICS-Spalten: `'GICS Sector Name'`, `'GICS Industry Group Name'`, `'GICS Industry Name'`, `'GICS Sub-Industry Name'`
- `TR.BookValuePerShare.periodenddate` → Spalte heißt `'Period End Date'`
- `TR.DivExDate` → `'Dividend Ex Date'` (nicht `'Ex-Dividend Date'`)
- `TR.DivPayDate` → `'Dividend Pay Date'` (nicht `'Pay Date'`)
- `TR.DivUnadjustedGross` → `'Gross Dividend Amount'`
- `TR.SpecialDivExDate/Amount` → Eikon gibt Spalten manchmal in Großbuchstaben zurück (`TR.SPECIALDIVEXDATE`) wenn keine Daten vorhanden — flexibles Matching notwendig

### GICS-Verfügbarkeit
- Eikon liefert GICS nur für das aktuellste Quartal je RIC; alle anderen Quartale kommen als Leerstring `''`
- Leerstrings müssen vor ffill/bfill durch `NaN` ersetzt werden
- `pd.NA` in `to_dict(orient='records')` wird von psycopg2 als Literal-String `'NaN'` geschrieben — vor DB-Write immer `None` verwenden: `df[col] = df[col].where(df[col].notna(), other=None)`
- Tabelle `fundamentals_quarterly` hat Spalte `gics_backfill BOOLEAN` — TRUE wenn Wert per ffill/bfill aus einem anderen Quartal übernommen wurde

### Historische Index-Constituents (getestet Sept. 2026)
- `ek.get_data('0#.SPX', ..., {'SDate': d})` und `'0#.SPX(YYYYMMDD)'` **ignorieren das Datum** → immer aktuelle Zusammensetzung
- Korrekt: `ek.get_data('.SPX', ['TR.IndexConstituentRIC', 'TR.IndexConstituentName'], {'SDate': d})` → Spalten `'Constituent RIC'`, `'Constituent Name'`; delistete Titel mit `^`-Suffix (z.B. `AET.N^K18`); umbenannte Titel unter aktuellem RIC (FB → `META.OQ`)
- Historie ab ca. **Mitte April 2015** (2015-01-02 und früher: leer)
- Spin-off-Platzhalter (z.B. `MILLROSE PROPERTIES CLS A ORD`) kommen **ohne RIC** — ein leerer RIC lässt jeden Batch mit Fehler 1422 (`instruments - non-empty is required`) scheitern → werden in `fetch_constituents.py` verworfen
- Nur `.SPX` liefert vollständige Historie (`HIST_CONSTITUENT_INDICES`); `.FTSE` lückenhaft (77–95 Titel), `.STOXX50E`/`.AXJO` Fehler 414, `.SSMI` nur aktuell
- **FTSE, AXJO, SSMI, STOXX50E**: Snapshots in `index_constituents` sind die heutige Zusammensetzung rückwärts kopiert (Survivorship Bias) — MCap dieser Indizes vor 2026 entsprechend verzerrt
- Backfill: `run(snapshot_date='YYYY-MM-01', mode='backfill', start_date='2015-05-01', end_date=...)` in `fetch_constituents.py`
- **Snapshot-Datum = MONATSENDE** (seit Sept. 2026): `_to_month_end()` mappt einen übergebenen Monatsersten auf den Vortag. Grund: S&P-Factsheets und Rebalancings beziehen sich auf Monats-/Quartalsenden; ein Snapshot vom 1. kennt die Wechsel vom Vormonatsende nicht.
- **Konsumenten müssen `snapshot_date <= :date` mit DISTINCT ON verwenden, nie Gleichheit.** Stand Nov. 2026: `.SPX` (139 Snapshots, 2015-04-30 bis 2026-10-31) und `.DJI` (119, 2016-12-31 bis 2026-10-31) liegen **ausschließlich auf Monatsenden** — die Monatserste wurden am 03.11.2026 neu gezogen und gelöscht (Backup: `pub_equity.index_constituents_bak_20261103`). `.AXJO`/`.FTSE`/`.SSMI`/`.STOXX50E` liegen weiterhin auf Monatsersten, dort sind je 149 Snapshots ohnehin Pseudo-Historie. Fünf `.DJI`-Snapshots liegen mitten im Monat (2018-06-26, 2019-04-02, 2024-02-26, 2024-11-08, 2026-06-29). Angepasst in `fetch_option_chains_constituents.py`; `compute_index_mcap.py` nutzt bereits `<=`.

### Market Cap: NIE close_price × shares_outstanding (WICHTIG)
- `TR.PriceClose` ist **splitbereinigt**, `TR.SharesOutstanding` ist der **damals gemeldete** Stand
- Produkt der beiden ist vor jedem späteren Split zu niedrig — AMZN 31.12.2021: 85 Mrd. statt 1.691 Mrd. (20:1-Split 2022), ebenso GOOGL/GOOG (20:1), NVDA (10:1 2024), TSLA (3:1 2022)
- Richtig: `TR.CompanyMarketCap` → Spalte `'Company Market Cap'`, gespeichert in `stock_prices_daily.market_cap`
- **`TR.CompanyMarketCap` ist die MCap des UNTERNEHMENS**, nicht der Aktiengattung: GOOGL.OQ und GOOG.OQ liefern denselben Wert → Doppelzählung ohne Entdopplung über `org_permid`
- `TR.OrganizationID` → `'Organization PermID'`, identisch für alle Gattungen; in `stock_fundamentals_quarterly.org_permid`
- Funktioniert auch für delistete RICs (`AET.N^K18`) und liefert LSE-Werte in **GBP, nicht Pence**

### Währungen in stock_prices_daily
- `crncy` = Kurswährung aus `TR.PriceClose.currency` — LSE-Titel: **`'GBp'`** (Pence!), nicht `'GBP'`
- `mcap_crncy` = Währung der Market Cap aus `TR.CompanyMarketCap.currency` — LSE: `'GBP'`
- Eikon liefert beide als Spalte `'Currency'`; Zuordnung über die vorhergehende Wertspalte (`_dedupe_currency_cols()` in `fetch_prices_daily.py`)
- Früher stand in `crncy` konstant `'LOC'`

### S&P-Methodik: was für unsere Rechnung gilt (geprüft Sept. 2026)
Quellen: `methodology-sp-us-indices.pdf` (88 S.), `methodology-index-math.pdf` (83 S.), Factsheet `fs-sp-500.pdf`.
- **Indexformel** (Index Math S. 6–7): `Index = Σ(Pᵢ × Qᵢ) / Divisor` mit `Qᵢ = IWFᵢ × Total Sharesᵢ`
- **Shares** (US Indices S. 16): „essentially *basic shares* as defined by FASB in GAAP", float-adjustiert
- **Updates** (S. 17): Share Counts quartalsweise auf den Stand der letzten Filings; die 5-%-Schwelle gilt für **IWF**-Änderungen beim Quartalsreview, **nicht** für Share-Updates
- **Mehrere Gattungen** (S. 10): jede gelistete Gattung separat mit dem **eigenen** FMC; nicht gelistete Gattungen zählen nur in die Company-TMC
- **Berkshire-Ausnahme** (S. 24): „S&P DJI will continue to consolidate the share count for this company under the B share class line" → BRK.B trägt A+B. Unsere Rechnung erfüllt das automatisch, weil `compute_index_mcap` über `org_permid` auf Unternehmensebene aggregiert (31.08.2026: 1.079,3 Mrd. statt 704,8 Mrd. nur Klasse B).
- **Bekannte Abweichung**: Bei Unternehmen mit einer gelisteten Gattung **außerhalb** des Index (z.B. CRWD, CVNA) überschätzt die Unternehmensebene; S&P zählt dort nur die Indexgattung.

### Abgleich mit dem Factsheet (Stand 31.08.2026)
- Indexstand: unser `.SPX`-Close 7.686,14 = Factsheet **exakt**
- Titelzahl 503 = 503; größter Titel 5,343 vs. 5,3474 Bio. (−0,08 %); Top-10-Gewicht float-adj. 37,79 vs. 37,80 %
- Total Market Cap: Gattungsebene 69,49 Bio. vs. 68,828 Bio. (**+0,96 %**) — bestes erreichbares Ergebnis
- **Nicht** die Ursache der Restdifferenz (getestet): quartalsweise vs. aktuellste Shares (69,502 vs. 69,490 — Eikon-Quartalsdaten laufen ohnehin nach, 370+ Titel stehen in beiden Varianten auf 2026-03-31)
- Ursachen der Restdifferenz: Definitionsunterschied Shares (Eikon `TR.SharesOutstanding` vs. S&P basic shares), Einzelfehler in Eikon (APH 17.08.2026 2:1-Split: Kurs bereinigt, Shares nicht), IWF ≠ `TR.FreeFloatPct`
- **Impliziter Divisor als Qualitätsmaß**: `ff_mcap / Indexstand` ≈ 8,63 Mrd.; 95,8 % der Tage ändert er sich < 0,1 %. Sprünge liegen auf Monats-/Quartalsanfängen = Snapshot- und Fundamentals-Wechsel, nicht Marktereignisse.

### SIC / TRBC
- `TR.SICIndustryCode` → `'SIC Industry Code'`, `TR.SICIndustry` → `'SIC Industry Name'`, `TR.TRBCEconomicSector` → `'TRBC Economic Sector Name'`
- Statisch wie GICS (nur erste Zeile je RIC/Abruf). SIC fehlt bei ~24 % der SPX-Titel (Eikon liefert NA, z.B. ADM, AMAT); keine Alternative-Felder gefunden
- Delistete RICs: **kein GICS**, aber SIC + TRBC

### Special Dividends
- `TR.SpecialDivExDate` / `TR.SpecialDivAmount` liefern in der Praxis keine Daten über Eikon get_data() für den SPX-Universum-Fetch — 0 Zeilen in `pub_equity.dividends` mit `div_type='special'`
- Für historische Special Dividends (z.B. Microsoft 2004) wäre manuelle Ergänzung nötig

### Shares Outstanding
- **Gehört in `fundamentals_quarterly`, nicht in `prices_daily`** — ist ein Quartalsfundamental, kein Tagespreis
- Fetch: `TR.SharesOutstanding` mit `Frq='FQ'` als Teil des quarterly Pulls
- Spaltenname im Eikon-Response: `'Outstanding Shares'`
- Non-Payer (AMZN, TSLA, ADBE, AMD, NFLX, NOW, PLTR, UBER, ISRG, BRKb u.a.) haben 0 Dividend-Zeilen — korrekt, kein Datenfehler

## Tabellen-Übersicht: pub_equity

### `index_overview`
- Flags: `get_index_option_chain`, `get_constituents`, `get_constituents_option_chain` (BOOLEAN)
- Steuern welche Module in der Pipeline für welchen Index laufen
- Aktuell: `.SPX` mit `get_constituents=TRUE`, `get_constituents_option_chain=TRUE`

### `stock_prices_daily`
- PK: `(ric, trade_date)`
- Felder: `close_price`, `open_price`, `high_price`, `low_price`, `volume`, `market_cap`, `mcap_crncy`, `crncy`
- Kein `shares_outstanding` — liegt in `stock_fundamentals_quarterly`
- Upsert: `on_conflict_do_update`
- Universum: alle RICs, die jemals in einem Snapshot waren (inkl. delistete `^`-RICs)
- Backfill lädt nur fehlende Zeiträume (RICs mit Historie ≥ start_date: nur ab MAX+1)

### `stock_fundamentals_quarterly`
- PK: `(ric, period_end_date)`
- Felder: `fiscal_year`, `fiscal_quarter`, `book_value_per_share`, `book_equity`, `shares_outstanding`, `free_float_pct`, `gics_sector`, `gics_industry_group`, `gics_industry`, `gics_sub_industry` (alle VARCHAR, Namen nicht Codes), `sic_code`, `sic_industry`, `trbc_economic_sector`, `org_permid`, `reporting_ccy`, `gics_backfill`
- Klassifikationen (GICS/SIC/TRBC) werden nach jedem Lauf je RIC mit dem jüngsten bekannten Wert aufgefüllt (`_fill_classifications`), `gics_backfill=TRUE` für aufgefüllte Zeilen
- `fiscal_quarter`: Kalenderquartal des `period_end_date`
- GICS-Spalten: VARCHAR(64); `gics_backfill=TRUE` für per ffill/bfill gefüllte Zeilen
- SPX-Universum neu geladen Sept. 2026 (ab 2014-01-01)

### `stock_dividends`
- PK: `(ric, ex_date, div_type)` — `div_type` ∈ `{'regular', 'special'}`
- Nur Regular Dividends vorhanden

### `index_prices_daily`
- PK: `(index_ric, trade_date)`
- Felder: `close_price`, `open_price`, `high_price`, `low_price`, `crncy`
- Universum: alle Indizes in `pub_equity.index_overview`
- History: 30 Jahre (ab ~1996)
- Delta-Logik: MAX(trade_date)+1 oder 30 Jahre zurück

## Tabellen-Übersicht: pub_config

### `futures_underlyings`
- PK: `underlying_ric`
- Felder: `underlying_name`, `futures_chain_ric` (Eikon-Chain ohne `0#`-Prefix), `fetch_chains`, `fetch_prices`, `active`
- **Aktuell leer** — vor erstem Futures-Run manuell befüllen:
  ```sql
  INSERT INTO pub_config.futures_underlyings (underlying_ric, underlying_name, futures_chain_ric, active)
  VALUES ('.SPX', 'S&P 500', '<chain_ric>', TRUE);
  ```

## Expiry-Klassifikation (pub_options.options_chains)

### Spalte `expiry_type CHAR(1)`
- `W` = Weekly (alles außer 3. Freitag)
- `M` = Monthly (3. Freitag, feiertagsbereinigt)
- `L` = LEAPS (tau > 1.0 zum snapshot_date, nur Equity)
- Logik in `modules/utils.py`: `classify_expiry(expiry_date, snapshot_date, asset_type)`
- `US_FRIDAY_HOLIDAYS` in utils.py jährlich prüfen und ergänzen (nächster Fall: Juneteenth 2032)
- Neue Chain-Pulls (Modul 2+3) setzen `expiry_type` direkt beim Schreiben

### Exchange-spezifische Monatsverfall-Konventionen (in utils.py)
- US / Eurex / Euronext / ICE / B3: 3. Freitag (feiertagsbereinigt) → `canonical_monthly_expiry()`
- KRX (KOSPI 200, `.KS200`): 2. Donnerstag
- NSE (Nifty 50, `.NSEI`): letzter Dienstag
- HKEx (Hang Seng, `.HSI`): vorletzter Handelstag des Monats
- ASX (ASX 200, `.AXJO`): 3. Donnerstag
- B3 (IBOVESPA, `.BVSP`): Mittwoch nächste zum 15.
- Mapping in `_UNDERLYING_CONVENTION` in `modules/utils.py`; Default = 'us'

### Für Futures: `classify_futures_expiry(expiry_date)` in utils.py
- `Q` = Quartal (Mär/Jun/Sep/Dez), `M` = Serienmonat

## Schema pub_futures

### `futures_chains`
- PK: `(futures_ric, snapshot_date)`
- Felder: `underlying_ric`, `expiry_date`, `contract_month` (YYYY-MM), `expiry_type`

### `futures_prices`
- PK: `(futures_ric, price_date)`
- Felder: `price_bid`, `price_ask`, `price_settle`, `volume`, `open_interest`
- `price_settle` = offizieller Tages-Settlement-Preis — relevant für Diskontfaktor-Schätzung
- Delta-Logik: MAX(price_date)+1 pro futures_ric oder 1 Jahr zurück

## FX Rates (pub_rates.fx_rates + pub_config.fx_pairs)

### pub_config.fx_pairs
- Steuertabelle für alle FX-Paare die gezogen werden
- PK: `(from_crncy, to_crncy)` — immer in der Form `FCY/USD`
- Felder: `eikon_ric VARCHAR` (Reuters-Spot-RIC), `invert BOOLEAN`, `active BOOLEAN`
- `invert=TRUE`: Eikon quotiert als USD-per-FCY (z.B. `CAD=` liefert USD/CAD) → gespeichert wird 1/rate (= CAD/USD)
- Aktuell konfigurierte Paare:

| from_crncy | to_crncy | eikon_ric | invert |
|------------|----------|-----------|--------|
| AUD        | USD      | AUD=      | FALSE  |
| CAD        | USD      | CAD=      | TRUE   |
| CHF        | USD      | CHF=      | TRUE   |
| EUR        | USD      | EUR=      | FALSE  |
| GBP        | USD      | GBP=      | FALSE  |
| HKD        | USD      | HKD=      | TRUE   |
| KRW        | USD      | KRW=      | TRUE   |

### pub_rates.fx_rates
- PK: `(from_crncy, to_crncy, trade_date)`
- Felder: `spot_rate NUMERIC(18,8)` (Midkurs = Mittelwert aus Bid/Ask), `source_system VARCHAR`
- Quelle: Eikon `TR.BidPrice` / `TR.AskPrice` (nicht `TR.PriceClose` — liefert NULLs für FX-RICs)
- Alle Rates als direkte Kursnotierung: 1 FCY = x USD (nach Inversion falls nötig)
- Historische Tiefe: ab 1995-12-29, täglich (Handelstage), ~7.890 Zeilen pro Paar
- Delta-Logik: MAX(trade_date)+1 pro Währungspaar oder Konfigurationsstart
- Modul: `modules/fetch_fx_rates.py`, Steuerung über `pub_config.fx_pairs WHERE active=TRUE`
- Verwendung: Umrechnung der lokalen Index-MCap in USD (computations_index_mcap ist in Lokalwährung)
- **Neue Währungen hinzufügen**: Zeile in `pub_config.fx_pairs` einfügen, nächster Pipeline-Run lädt History automatisch

## RIC → Unternehmen (pub_config.ric_org_map)

- PK: `ric`; Felder: `org_permid`, `company_name`, `crncy`, `successor_ric`, `first_price`, `last_price`, `source`, `updated_at`
- Universum: **alle** je in `stock_prices_daily` oder `index_constituents` aufgetretenen RICs (Stand Nov. 2026: 1.160 RICs, 1.128 Unternehmen) — auch delistete und solche, die kein aktiver Snapshot mehr referenziert
- Modul: `modules/fetch_ric_org_map.py`, Eikon `TR.OrganizationID` + `TR.CommonName`; `crncy`/`first_price`/`last_price` kommen aus `stock_prices_daily` (kein Eikon-Call)
- `mode='live'` holt nur RICs ohne PermID, `mode='refresh'` alle
- **`org_permid` ist ein UNTERNEHMENS-Schlüssel**, kein Instrumentenschlüssel: er gruppiert auch Aktiengattungen (`GOOGL.OQ`/`GOOG.OQ`, `FOX.OQ`/`FOXA.OQ`) und Zweitnotierungen in anderer Währung (`NEM.AX`/`NEM.N`, `RMD.AX`/`RMD.N`). Für unternehmensweite Aggregation richtig, als Join-Key für **Preisreihen falsch**.
- `successor_ric` ist die Instrumentenebene (RIC-Wechsel wie `FITB.OQ` → `FITB.N`) und wird **kuratiert**, nie automatisch gesetzt — die PermID allein kann Umbenennung nicht von Zweitgattung unterscheiden
- `compute_index_mcap` bezieht `org_permid` aus dieser Tabelle (vorher aus `stock_fundamentals_quarterly`, wo abgelöste RICs fehlen und dadurch als eigenes Unternehmen zählten)
- `pub_equity.index_constituents` hat zusätzlich eine Spalte `org_permid`, die `fetch_constituents` beim Schreiben setzt (nur für neue Snapshots, Altbestand ist NULL)

## Index Market Cap (pub_equity.computations_index_mcap)

- PK: `(date, index_ric)`
- Felder: `crncy CHAR(3)`, `mcap NUMERIC(24,2)`, `ff_mcap NUMERIC(24,2)`
- Berechnung: bottom-up aus `stock_prices_daily.market_cap` (= `TR.CompanyMarketCap`), entdoppelt je `org_permid`
  - `mcap    = SUM(market_cap)` je Unternehmen einmal (GOOGL+GOOG zählen einmal)
  - `ff_mcap = SUM(market_cap × free_float_pct / 100)`, free_float_pct = nach `shares_outstanding` gewichtetes Mittel der im Index enthaltenen Gattungen
  - **Nicht** `close_price × shares_outstanding` — siehe Abschnitt „Market Cap" oben
- Währung: **Lokalwährung des Index** (GBP für FTSE, AUD für AXJO, CHF für SSMI, EUR für STOXX50E, USD für SPX)
- **LSE-Stocks (`.L`-Suffix)**: `close_price` ist in Pence (`crncy='GBp'`), `market_cap` aber in GBP → keine Division mehr nötig
- Snapshot-Logik: letzter `index_constituents`-Snapshot ≤ chunk_start (DISTINCT ON, einmalig je Index pro Chunk)
- Fundamentals forward-fill: LATERAL JOIN auf `stock_fundamentals_quarterly` — letztes `period_end_date ≤ trade_date`
- Modul: `modules/compute_index_mcap.py`, verarbeitet in Monats-Chunks
- Steuerung: läuft für alle Indizes mit `fetch_constituent_prices=TRUE AND active=TRUE` in `pub_config.stock_indices`
- Historische Tiefe (Stand April 2026):

| Index     | Währung | Ø MCap (2024–) | Ø FF-MCap (2024–) | History ab  |
|-----------|---------|-----------------|-------------------|-------------|
| .SPX      | USD     | ~52 Bio.        | ~48 Bio.          | 2015-05-01 (point-in-time Constituents) |
| .FTSE     | GBP     | ~2.1 Bio.       | ~1.9 Bio.         | 2014-01-01  |
| .AXJO     | AUD     | ~2.6 Bio.       | ~2.2 Bio.         | 2014-01-01  |
| .SSMI     | CHF     | ~1.3 Bio.       | ~1.2 Bio.         | 2014-01-01  |
| .STOXX50E | EUR     | ~4.3 Bio.       | ~3.2 Bio.         | 2014-01-01  |

Abgleich mit S&P-Factsheet (.SPX, Stand 31.08.2026): S&P weist unter „Index Characteristics" die **Total Market Cap der Constituents** aus (nicht float-adjustiert): 68,828 Bio. USD, 503 Titel, Median 44,73 Mrd., groesster Titel 5,3474 Bio.
Unsere Werte zum selben Tag: Gattungsebene (Preis x splitkorrigierte NOSH) 69,226 Bio. (+0,58 %), groesster Titel 5,343 Bio. (-0,08 %), Median 46,41 Mrd. (+3,7 %); Unternehmensebene (SUM CompanyMarketCap, entdoppelt) 70,011 Bio. (+1,72 %); ff_mcap 66,333 Bio. (-3,62 %).
**Vergleichsregel**: S&P Total Market Cap entspricht der Gattungsebene, NICHT ff_mcap. Die im Factsheet genannten Gewichte (groesster Titel 8,1 %, Top 10 37,8 %) sind dagegen float-adjustiert (unsere ungewichteten Werte: 7,7 % / 38,1 %).

- **Umrechnung in USD**: `mcap_usd = mcap × spot_rate` via `pub_rates.fx_rates` (Join auf `from_crncy = crncy AND to_crncy = 'USD' AND trade_date = date`)
- Idempotent: ON CONFLICT DO UPDATE — Neuberechnung für einen Zeitraum mit `DELETE` + `run(mode='backfill', start_date=..., end_date=...)` oder direkt mit backfill (upsert überschreibt)

## USD Rates Pipeline (pub_rates.usd_rates)
- Quelle: FRED Constant Maturity Treasury (CMT) Rates
- Tenors: 1m, 3m, 6m, 1y, 2y, 3y, 5y, 7y, 10y, 20y, 30y
- Zwei Rate-Typen pro Datum und Tenor:
  - `par` (`source_system='FRED'`): verbatim, discount-Basis (≤6m) bzw. semiannual BEY (≥1y)
  - `zero` (`source_system='bootstrapped'`): jährlich diskret, `disc(τ) = (1 + r/100)^(−τ/365)`
- **Schema-Hinweis**: Spalte `source_system VARCHAR NOT NULL` muss explizit gesetzt werden (`'FRED'` / `'bootstrapped'`). Spalte `source CHAR(2)` = Datenursprung (`'EK'`/`'FM'`). Beide Felder haben kein DEFAULT.
- Historische Tiefe: 10 Jahre (ab ca. 2016-01-01)
- Inkrementell: startet ab MAX(trade_date)+1, kein Forward-Fill an Nicht-Handelstagen
- Bootstrap-Logik:
  - Short-end (≤6m): Bank-Discount → annual discrete zero
  - Long-end (≥1y): Sequential par-bond bootstrap, log-lineare Interpolation für Zwischentermine
- Automatischer Scheduler: Windows Task Scheduler, täglich 22:00, `-StartWhenAvailable`
  - Batch: `run_usd_rates.bat`, Log: `logs/usd_rates.log`
- Conversion in Application Code (continuous): `r_cont = np.log(1 + r_disc/100)`

## Schema pub_options: Historische Chains

### SPX Option RIC Format
- **Live-RIC**: `/SPX[month_letter][DD][YY][strike*10_5stellen].U`
  - Beispiel: `/SPXd172668500.U` = CALL Apr 17 2026, Strike 6850
  - Call-Buchstaben: a=Jan, b=Feb, c=Mar, d=Apr, e=May, f=Jun, g=Jul, h=Aug, i=Sep, j=Oct, k=Nov, l=Dec
  - Put-Buchstaben: m=Jan, n=Feb, o=Mar, p=Apr, q=May, r=Jun, s=Jul, t=Aug, u=Sep, v=Oct, w=Nov, x=Dec
  - DD = Verfalltag (2-stellig), YY = Jahr (2-stellig), Strike×10 auf 5 Stellen padded
- **Historischer RIC** (abgelaufene Optionen): Suffix `^[BUCHSTABE][YY]` anhängen
  - Beispiel: `SPXb202669000.U^B26` = SPX Feb 20 2026 CALL Strike 6900
  - Suffix-Buchstabe = Monatsbuchstabe der Call-Reihe (Großbuchstabe)
  - **WICHTIG**: Historische RICs verwenden kein führendes `/`
  - Statische Felder (EXPIR_DATE, STRIKE_PRC) liefern für historische RICs keinen Wert —
    nur Zeitreihendaten (TR.BIDPRICE mit SDate/EDate) funktionieren
- **Datenverfügbarkeit**: Eikon liefert historische SPX-Options-Daten ab ca. **Oktober 2015**
  (frühere Daten nicht verfügbar in diesem RIC-Format)

### Good Friday / Karfreitag-Korrektur
- Wenn Good Friday auf den 3. Freitag April fällt, verschiebt sich der Verfall auf Donnerstag
- Betroffen: April 2019 (→ 04-18), April 2022 (→ 04-14), April 2025 (→ 04-17)
- Fix: `US_FRIDAY_HOLIDAYS` in `modules/utils.py` enthält jetzt alle relevanten Good-Friday-Daten
- Gleiche Logik gilt für zukünftige Good-Friday-Treffer: 2030-04-19, 2033-04-15

### Aktueller Stand (Stand: April 2026)
- **SPX**: 93.513 Einträge (inkl. LEAPS), Jan 2016 – Dez 2031. `fetch_index_options_hist_backfill = FALSE`
- **AXJO**: Jan 2016 – Mär 2026, lückenlos (123 Monate). Prices in `options_prices` als hist-RIC. `fetch_index_options_hist_backfill = FALSE`
- **SSMI**: Jan 2016 – Mär 2026, lückenlos (123 Monate). Prices als hist-RIC. `fetch_index_options_hist_backfill = FALSE`
- **STOXX50E**: Jan 2016 – Mär 2026, lückenlos (123 Monate). Prices als hist-RIC. `fetch_index_options_hist_backfill = FALSE`
- **FTSE**: Nur ab Jan 2024 (Eikon hat für LFE-Optionen keine längere History). Prices als hist-RIC. `fetch_index_options_hist_backfill = FALSE`
- Alle aktiven Indizes vollständig nachgezogen — kein Index hat `fetch_index_options_hist_backfill = TRUE`

### Historische Chains rekonstruieren
- **Index-Optionen** (resumable): `build_index_hist_chains.py --index <ric> [--start YYYY-MM]`
  - Schreibt **sowohl** `options_chains` (Stammdaten) **als auch** `options_prices` (hist-RIC mit `^`) in einem Pass
  - Steuerung über `pub_config.stock_indices.fetch_index_options_hist_backfill = TRUE`
  - Unterstützte Indizes in `INDEX_CONFIG`: `.SPX`, `.STOXX50E`, `.DJI`, `.OEX`, `.SSMI`, `.AXJO`, `XIU.TO`, `DXJ`, `.NSEI`, `.FTSE`
  - Laufzeit: ~15-30s pro Monat (ohne Rate Limit Wartezeiten)
  - `--start YYYY-MM` für Resume nach Rate-Limit-Unterbrechung
  - Skips bereits bekannte RICs via `get_known_rics()` am Start (idempotent)
  - **Strike-Kandidaten**: `strike_step=5`, `range_pct=0.60` (±60% um ATM) für alle Indizes
  - **Rate Limiting**: 24 Retries à 3600s (24h Abdeckung); bei permanentem Fehler: INCOMPLETE-Meldung mit Resume-Kommando
  - Logging: `Done (all batches successful)` nur wenn null permanente Fehler; sonst `INCOMPLETE — N batch(es) permanently failed`
- **Alle aktiven Indizes auf einmal**: `python run_chain_builder_all.py > logs/chain_builder_all.log 2>&1`
  - Liest `fetch_index_options_hist_backfill = TRUE` aus `pub_config.stock_indices`
  - **Protokoll bei Rate Limits**: Nur einen Index gleichzeitig auf TRUE setzen; fertige Indizes auf FALSE setzen vor Restart; niemals mehrere Indizes gleichzeitig aktiv (konkurrieren um API-Quota)

## Student-DB (FACM-Server)

- Zweiter Postgres-Server, Zugang über `.env`-Präfix `DB_FACM_` → `get_engine('DB_FACM_')` aus `config/db.py`
- Datenbank `student_db` auf `139.6.183.100:5432`; **kein TLS** (`SHOW ssl` → `off`), deshalb kein `DB_FACM_SSLMODE` gesetzt — `require` würde die Verbindung abbrechen, nicht absichern
- Modul: `modules/sync_student_db.py`, idempotent (`TRUNCATE` + Neuladen je Tabelle), Übertragung per `COPY`-Strom in Jahres-Chunks. **Nicht** in `run_monthly.py` verdrahtet — schiebt Daten auf einen externen Server, läuft nur auf Zuruf
- Gespiegelt werden **identische Schema- und Tabellennamen**, damit jede Abfrage unverändert gegen beide Datenbanken läuft:

| Tabelle | Filter | Zeilen |
|---------|--------|--------|
| `pub_equity.index_constituents` | `index_ric = '.SPX'` | 70.082 |
| `pub_equity.stock_prices_daily` | SPX-RICs **plus deren Vorgänger-RICs** | 2.124.654 |
| `pub_config.ric_org_map` | vollständig | 1.160 |

- **Vorgänger-RICs müssen mit**: nach dem Neuziehen der Historie trägt `index_constituents` nur die aktuelle Schreibweise (`FITB.N`), die alte Kursreihe liegt unter `FITB.OQ`. Ohne sie wäre die Reihe abgeschnitten.
- Views auf dem Ziel (von `sync_student_db` angelegt):
  - `pub_config.v_ric_canonical` — RIC → kanonischer RIC über `successor_ric`
  - `pub_equity.v_stock_prices_canonical` — Kurse auf den kanonischen RIC umgeschlüsselt, `source_ric` zeigt die Herkunft. Löst RIC-Wechsel auf (FITB: 97 → 1.208 Kurse im 5-Jahres-Fenster)
  - `pub_config.mv_primary_ric` — **materialisiert**, je `org_permid` die Gattung mit dem höheren Medianvolumen (GOOGL vor GOOG, FOXA vor FOX, NWSA vor NWS). Nach jedem Sync `REFRESH MATERIALIZED VIEW`
  - `pub_equity.v_stock_prices_company` — **für Studierende**: genau ein Kurs je Unternehmen und Handelstag, 503 Gattungen → 500 Unternehmen
- Rechte: Gruppenrolle `student_ro` (NOLOGIN) mit `CONNECT`/`USAGE`/`SELECT` und Default-Privileges. Einzelaccounts separat anlegen und `student_ro` granten — niemals `postgres` weitergeben
- **Warum dedupliziert**: zwei Gattungen desselben Unternehmens sind fast perfekt korreliert (GOOGL/GOOG ρ=0,9974) und machen die Kovarianzmatrix nahezu singulär. Konditionszahl über 487 SPX-Titel: 87.690 mit beiden Gattungen, 4.563 dedupliziert. Das GMV-Portfolio baut sonst einen ±30-%-Spread-Trade zwischen GOOGL und GOOG. Deduplizierung ist dafür notwendig, aber nicht hinreichend (86 Eigenwerte < 0,1 bei 487 Titeln / 1.254 Beobachtungen → Shrinkage oder Faktormodell nötig)

## Pipeline-Dokumentation
- Refactoring-Spec: `docs/instructions/refactoring_spec.md`
- USD Rates LaTeX-Doku: `docs/pipeline_usd_rates.tex`
- **Options-Datenakquisition LaTeX-Doku**: `docs/data_acquisition_options.tex` (SPX, STOXX50E, AXJO, SMI — RIC-Struktur, Strike-Kandidaten, Expiry-Konventionen, Preisabruf, Deskriptive Statistiken)
- Bestehender Backfill-Code als Referenz: `Constituents_Historical.py`
- Unterrichtsmaterial (DJI-Beispieldaten): `docs/teaching/`

Hinweis: die frueheren Dateien unter `Instructions/` (pipeline_options, usd_rates_data_requirements, svix_stock_data_requirements, expiry_type_classification) wurden entfernt; ihre Inhalte stehen in den jeweiligen Abschnitten dieser Datei.
