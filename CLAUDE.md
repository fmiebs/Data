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
- Schema `pub_config`: `data_sources`, `stock_indices`, `futures_underlyings`, `rates`, `pipeline_runs`
- Schema `pub_equity`: `index_overview`, `index_constituents`, `stock_prices_daily`, `stock_fundamentals_quarterly`, `stock_dividends`, `index_prices_daily`
- Schema `pub_options`: `options_chains`, `options_prices`, `computations_options_information`, `computations_svix`
- Schema `pub_rates`: `usd_rates`
- Schema `pub_futures`: `futures_chains`, `futures_prices`

Hinweis: `computations_*`-Tabellen enthalten Berechnungsergebnisse (implizite Vola, SVIX) — keine Pipeline-Tabellen, werden nicht von den Modulen beschrieben.

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
1. `modules/fetch_constituents.py` — Index-Constituents (Eikon)
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
- Felder: `close_price`, `open_price`, `high_price`, `low_price`, `volume`, `crncy`
- Kein `shares_outstanding` — liegt in `stock_fundamentals_quarterly`
- Upsert: `on_conflict_do_update`
- Universum: SPX Constituents, 10 Jahre History (ab ~2016-01-01)
- Stand: 1.270.047 Zeilen, 503 RICs, 2010-01-04 – 2026-03-20

### `stock_fundamentals_quarterly`
- PK: `(ric, period_end_date)`
- Felder: `fiscal_year`, `fiscal_quarter`, `book_value_per_share`, `book_equity`, `shares_outstanding`, `gics_sector`, `gics_industry_group`, `gics_industry`, `gics_sub_industry` (alle VARCHAR, Namen nicht Codes), `reporting_ccy`, `gics_backfill`
- `fiscal_quarter`: Kalenderquartal des `period_end_date`
- GICS-Spalten: VARCHAR(64); `gics_backfill=TRUE` für per ffill/bfill gefüllte Zeilen
- Stand: 17.158 Zeilen, 461 RICs

### `stock_dividends`
- PK: `(ric, ex_date, div_type)` — `div_type` ∈ `{'regular', 'special'}`
- Nur Regular Dividends vorhanden (15.193 Zeilen, 415 RICs)

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

## Pipeline-Dokumentation
- Options-Pipeline: `docs/instructions/pipeline_options.md`
- USD Rates Requirements: `Instructions/usd_rates_data_requirements.md`
- Stock-Level SVIX Requirements: `Instructions/svix_stock_data_requirements.md`
- Expiry-Klassifikation: `Instructions/expiry_type_classification.md`
- USD Rates LaTeX-Doku: `docs/pipeline_usd_rates.tex`
- **Options-Datenakquisition LaTeX-Doku**: `docs/data_acquisition_options.tex` (SPX, STOXX50E, AXJO, SMI — RIC-Struktur, Strike-Kandidaten, Expiry-Konventionen, Preisabruf, Deskriptive Statistiken)
- Bestehender Backfill-Code als Referenz: `Constituents_Historical.py`
