# CLAUDE.md – Projekt research_db

## Credentials
- Alle Credentials in `C:\Users\Miebs\PycharmProjects\Config\.env`
- Variablen: `REFINITIV_APP_KEY`, `DB_USER`, `DB_PASS`, `DB_HOST`, `DB_PORT`, `DB_NAME`, `FRED_API_KEY`

## Datenbank
- PostgreSQL, Datenbank: `research_db`
- Schema `pub_equity`: `index_overview`, `index_constituents`, `prices_daily`, `fundamentals_quarterly`, `dividends`
- Schema `pub_options`: `options_chains`, `options_prices`
- Schema `pub_rates`: `usd_rates`
- Schema `pub_futures`: `futures_overview`, `futures_chains`, `futures_prices`

## API-Constraints (Eikon)
- **Ausschließlich `ek.get_data()`** aus dem `eikon`-Paket. Kein `rd`, kein `rdp`, kein `get_timeseries`.
- Eikon Desktop/Workspace muss auf dem Rechner laufen (API-Proxy).
- Rate Limiting: min. 0.5s zwischen Calls, 63s Backoff bei 429.

## API-Constraints (FRED)
- Paket: `fredapi` (`Fred(api_key=...)`), Key aus `.env` (`FRED_API_KEY`)
- Serien: DGS1MO, DGS3MO, DGS6MO, DGS1, DGS2, DGS3, DGS5, DGS7, DGS10, DGS20, DGS30

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
9. `modules/fetch_futures_chains.py` — Futures Stammdaten (Eikon)
10. `modules/fetch_futures_prices.py` — Futures Preiszeitreihen (Eikon, delta: 1 Jahr)

## Bekannte Eikon-Eigenheiten (wichtig für zukünftige Module)

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

### `prices_daily`
- PK: `(ric, trade_date)`
- Felder: `close_price`, `open_price`, `high_price`, `low_price`, `volume`
- Kein `shares_outstanding` — liegt in `fundamentals_quarterly`
- Upsert: `on_conflict_do_update`
- Universum: SPX Constituents, 10 Jahre History (ab ~2016-01-01)
- Stand: 1.270.047 Zeilen, 503 RICs, 2010-01-04 – 2026-03-20

### `fundamentals_quarterly`
- PK: `(ric, period_end_date)`
- Felder: `fiscal_year`, `fiscal_quarter`, `book_value_per_share`, `book_equity`, `shares_outstanding`, `gics_sector`, `gics_industry_group`, `gics_industry`, `gics_sub_industry` (alle VARCHAR, Namen nicht Codes), `reporting_ccy`, `gics_backfill`
- `fiscal_quarter`: Kalenderquartal des `period_end_date` (alle 12 Monate abgedeckt — auch nicht-standardisierte Geschäftsjahresenden wie WMT Jan, NVDA Jan, CSCO Jul etc.)
- GICS-Spalten: VARCHAR(64); `gics_backfill=TRUE` für per ffill/bfill gefüllte Zeilen
- 70 RICs ohne GICS-Wert von Eikon: `gics_backfill=FALSE` + `gics_sector IS NULL`
- Stand: 17.158 Zeilen, 461 RICs (inkl. 15 nachgezogene Non-Dec-FY-Titel)

### `dividends`
- PK: `(ric, ex_date, div_type)` — `div_type` ∈ `{'regular', 'special'}`
- Nur Regular Dividends vorhanden (15.193 Zeilen, 415 RICs)
- Non-Payer (AMZN, TSLA, ADBE, AMD, NFLX, NOW, PLTR, UBER, ISRG, BRKb u.a.) haben 0 Einträge — korrekt verifiziert via direktem Eikon-Pull

### `overview_company_data`
- Control-Tabelle: mappt Eikon-Felder auf Zieltabellen/-spalten
- `TR.SharesOutstanding` → `pub_equity.fundamentals_quarterly.shares_outstanding` (aktualisiert)

## Expiry-Klassifikation (pub_options.options_chains)

### Spalte `expiry_type CHAR(1)`
- `W` = Weekly (alles außer 3. Freitag)
- `M` = Monthly (3. Freitag, feiertagsbereinigt)
- `L` = LEAPS (tau > 1.0 zum snapshot_date, nur Equity)
- Logik in `modules/utils.py`: `classify_expiry(expiry_date, snapshot_date, asset_type)`
- `US_FRIDAY_HOLIDAYS` in utils.py jährlich prüfen und ergänzen (nächster Fall: Juneteenth 2032)
- Backfill einmalig ausgeführt: `backfill_expiry_type.py`
- Neue Chain-Pulls (Modul 2+3) setzen `expiry_type` direkt beim Schreiben

### Für Futures: `classify_futures_expiry(expiry_date)` in utils.py
- `Q` = Quartal (Mär/Jun/Sep/Dez), `M` = Serienmonat

## Schema pub_futures

### `futures_overview`
- PK: `underlying_ric`
- Felder: `underlying_name`, `futures_chain_ric` (Eikon-Chain ohne `0#`-Prefix), `active`
- Steuert welche Underlyings in Modul 9+10 gezogen werden
- Vor erstem Run: `INSERT INTO pub_futures.futures_overview VALUES ('.OEX', 'S&P 100', '<chain_ric>', TRUE)`
- Hinweis: OEX-Futures existieren nicht — für Diskontfaktor-Schätzung alternativ SPX-PCP oder USD-Rates verwenden

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
  - `par` (source=FRED): verbatim, discount-Basis (≤6m) bzw. semiannual BEY (≥1y)
  - `zero` (source=bootstrapped): jährlich diskret, `disc(τ) = (1 + r/100)^(−τ/365)`
- Historische Tiefe: 10 Jahre (ab ca. 2016-01-01)
- Inkrementell: startet ab MAX(trade_date)+1, kein Forward-Fill an Nicht-Handelstagen
- Bootstrap-Logik:
  - Short-end (≤6m): Bank-Discount → annual discrete zero
  - Long-end (≥1y): Sequential par-bond bootstrap, log-lineare Interpolation für Zwischentermine
- Automatischer Scheduler: Windows Task Scheduler, täglich 22:00, `-StartWhenAvailable`
  - Batch: `run_usd_rates.bat`, Log: `logs/usd_rates.log`
- Conversion in Application Code (continuous): `r_cont = np.log(1 + r_disc/100)`

## Pipeline-Dokumentation
- Options-Pipeline: `docs/instructions/pipeline_options.md`
- USD Rates Requirements: `Instructions/usd_rates_data_requirements.md`
- Stock-Level SVIX Requirements: `Instructions/svix_stock_data_requirements.md`
- Expiry-Klassifikation: `Instructions/expiry_type_classification.md`
- USD Rates LaTeX-Doku: `docs/pipeline_usd_rates.tex`
- Bestehender Backfill-Code als Referenz: `Constituents_Historical.py`
