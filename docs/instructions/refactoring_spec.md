# Refactoring Specification – research_db Pipeline

Dieses Dokument beschreibt alle strukturellen Änderungen, die in einer einzigen Session
vollständig umgesetzt werden sollen. Vor Beginn der Implementierung CLAUDE.md lesen.

---

## Kernarchitektur: `pub_config.data_sources` als Single Source of Truth

`pub_config.data_sources` ist die Weiterentwicklung von `pub_equity.overview_company_data`.
Sie ist die **einzige Stelle** im gesamten System an der Tabellennamen, Spaltennamen und
Eikon-Felder definiert werden. Alle anderen Stellen (Python-Code, Module) lesen daraus.

**Einzige hardcodierte Konstante im gesamten Python-Code:**
```python
_CONFIG_TABLE = 'pub_config.data_sources'
```

**Schema `pub_config.data_sources`:**
```sql
CREATE TABLE pub_config.data_sources (
    id              SERIAL PRIMARY KEY,
    domain          VARCHAR   NOT NULL,  -- 'equity', 'options', 'rates', 'futures'
    entity_type     VARCHAR   NOT NULL,  -- 'index', 'stock', 'option', 'future', 'rate'
    module          VARCHAR   NOT NULL,  -- 'fetch_prices_daily', 'fetch_option_chains', etc.
    eikon_field     VARCHAR,             -- 'TR.CLOSEPRICE', NULL für berechnete Felder
    eikon_params    JSONB,               -- zusätzliche Eikon-Parameter (Frq, CURN, etc.)
    logical_name    VARCHAR   NOT NULL,  -- stabiler Code-Alias, z.B. 'stock_prices_daily'
    target_schema   VARCHAR   NOT NULL,  -- z.B. 'pub_equity'
    target_table    VARCHAR   NOT NULL,  -- z.B. 'stock_prices_daily' — hier umbenennen
    target_column   VARCHAR   NOT NULL,  -- z.B. 'close_price'
    source_code     CHAR(2)   NOT NULL,  -- 'EK' = Eikon, 'FM' = eigene Berechnung
    active          BOOLEAN   DEFAULT TRUE
);
```

**`config/schema.py` liest beim Import einmalig aus der DB:**
```python
from config.db import engine
from sqlalchemy import text
from functools import lru_cache

_CONFIG_TABLE = 'pub_config.data_sources'

@lru_cache(maxsize=None)
def _load_tables() -> dict[str, str]:
    """Liest alle (logical_name → schema.table) Mappings aus pub_config.data_sources."""
    with engine.connect() as conn:
        rows = conn.execute(text(
            f"SELECT DISTINCT logical_name, target_schema, target_table "
            f"FROM {_CONFIG_TABLE} WHERE logical_name IS NOT NULL AND active = TRUE"
        )).fetchall()
    return {r[0]: f'{r[1]}.{r[2]}' for r in rows}

def get_table(logical_name: str) -> str:
    return _load_tables()[logical_name]

# Convenience-Konstanten (lesen aus dem gecachten Dict, kein Hardcoding):
TBL_STOCK_PRICES        = get_table('stock_prices_daily')
TBL_INDEX_PRICES        = get_table('index_prices_daily')
TBL_OPTIONS_CHAINS      = get_table('options_chains')
TBL_OPTIONS_PRICES      = get_table('options_prices')
TBL_USD_RATES           = get_table('usd_rates')
TBL_FUTURES_CHAINS      = get_table('futures_chains')
TBL_FUTURES_PRICES      = get_table('futures_prices')
TBL_FUNDAMENTALS        = get_table('fundamentals_quarterly')
TBL_DIVIDENDS           = get_table('dividends')
TBL_INDEX_CONSTITUENTS  = get_table('index_constituents')
TBL_CONFIG_INDICES      = get_table('config_indices')
```

**Umbenennung einer Tabelle** = ein SQL-UPDATE in `pub_config.data_sources`:
```sql
UPDATE pub_config.data_sources
SET target_table = 'new_table_name'
WHERE logical_name = 'stock_prices_daily';
-- Kein Python-Code muss geändert werden.
```

---

## 1. Equity-Tabellen: Umbenennung und Harmonisierung

### 1a. Umbenennungen und Bereinigungen

Alle Tabellen in `pub_equity` tragen im Namen entweder den Präfix `stock_` oder `index_`.

```sql
-- prices_daily → stock_prices_daily
ALTER TABLE pub_equity.prices_daily           RENAME TO stock_prices_daily;

-- dividends → stock_dividends
ALTER TABLE pub_equity.dividends              RENAME TO stock_dividends;

-- fundamentals_quarterly → stock_fundamentals_quarterly
ALTER TABLE pub_equity.fundamentals_quarterly RENAME TO stock_fundamentals_quarterly;

-- fundamentals_annual droppen (30 Zeilen, alle inhaltlichen Spalten NULL, nicht genutzt)
DROP TABLE pub_equity.fundamentals_annual;

-- Bereits korrekt benannt (kein Handlungsbedarf):
-- index_overview, index_constituents, index_prices_daily, index_dividend_yield
```

### 1b. Spalten harmonisieren zwischen `stock_prices_daily` und `index_prices_daily`

**Ziel:** Beide Tabellen nutzen identische Spaltennamen für OHLCV.

| Bisher `stock_prices_daily` | Bisher `index_prices_daily` | Neu (beide Tabellen) |
|---|---|---|
| `ric` | `index_ric` | `ric` / `index_ric` (unverändert, da verschiedene PKs) |
| `close_price` | `close_local` | `close_price` |
| `open_price` | `open_local` | `open_price` |
| `high_price` | `high_local` | `high_price` |
| `low_price` | `low_local` | `low_price` |
| `volume` | *(nicht vorhanden)* | `volume` (NULL für Indizes) |
| *(nicht vorhanden)* | `close_usd` | **gedroppt** |

```sql
-- index_prices_daily: Spalten umbenennen + close_usd droppen
ALTER TABLE pub_equity.index_prices_daily
    RENAME COLUMN close_local TO close_price;
ALTER TABLE pub_equity.index_prices_daily
    RENAME COLUMN open_local  TO open_price;
ALTER TABLE pub_equity.index_prices_daily
    RENAME COLUMN high_local  TO high_price;
ALTER TABLE pub_equity.index_prices_daily
    RENAME COLUMN low_local   TO low_price;
ALTER TABLE pub_equity.index_prices_daily
    DROP COLUMN close_usd;
```

### 1c. `crncy`-Spalte in beiden Tabellen

```sql
ALTER TABLE pub_equity.stock_prices_daily
    ADD COLUMN crncy VARCHAR(3) NOT NULL DEFAULT 'LOC';
ALTER TABLE pub_equity.index_prices_daily
    ADD COLUMN crncy VARCHAR(3) NOT NULL DEFAULT 'LOC';
```

**Hinweis:** `index_prices_daily` zieht ab sofort nur noch Lokalwährung (kein
`CURN='USD'`-Parameter mehr). `close_usd` entfällt ersatzlos.

**Code-Änderungen:**
- `modules/fetch_index_prices_daily.py` — `CURN='USD'`-Call entfernen, `crncy='LOC'` setzen
- `modules/fetch_prices_daily.py` (umbenannt aus fetch-Modul) — `crncy='LOC'` setzen

---

## 2. `pub_config`-Schema und Steuerungstabellen

### 2a. `pub_config.data_sources` (Single Source of Truth)

Anlegen wie in der Kernarchitektur beschrieben. Migration aus `overview_company_data`:
```sql
CREATE SCHEMA IF NOT EXISTS pub_config;

-- Daten aus overview_company_data übernehmen und anpassen
INSERT INTO pub_config.data_sources
    (domain, entity_type, module, eikon_field, logical_name,
     target_schema, target_table, target_column, source_code)
SELECT
    'equity', 'stock', module, eikon_field, logical_name,
    target_schema, target_table, target_column, 'EK'
FROM pub_equity.overview_company_data;

-- Anschließend overview_company_data droppen:
DROP TABLE pub_equity.overview_company_data;
```

Alle bestehenden und neuen Eikon-Felder aller Module werden als Zeilen eingetragen,
sodass `data_sources` eine vollständige Übersicht aller Datenflüsse darstellt.

### 2b. `pub_config.stock_indices` — Index-Orchestrierung

Ersetzt die Boolean-Flags aus `pub_equity.index_overview`:

```sql
CREATE TABLE pub_config.stock_indices (
    index_ric                      VARCHAR PRIMARY KEY,
    index_name                     VARCHAR,
    currency                       CHAR(3),
    fetch_index_prices             BOOLEAN DEFAULT TRUE,
    fetch_index_option_chain       BOOLEAN DEFAULT FALSE,
    fetch_constituents             BOOLEAN DEFAULT FALSE,
    fetch_constituent_options      BOOLEAN DEFAULT FALSE,
    fetch_constituent_prices       BOOLEAN DEFAULT FALSE,
    fetch_constituent_fundamentals BOOLEAN DEFAULT FALSE,
    fetch_constituent_dividends    BOOLEAN DEFAULT FALSE,
    active                         BOOLEAN DEFAULT TRUE
);

-- Migration:
INSERT INTO pub_config.stock_indices
SELECT index_ric, index_name, NULL,
       get_index_option_chain, get_index_option_chain,
       get_constituents, get_constituents_option_chain,
       get_constituents, get_constituents, get_constituents,
       TRUE
FROM pub_equity.index_overview;

-- Control-Spalten aus index_overview entfernen (Stammdaten bleiben):
ALTER TABLE pub_equity.index_overview
    DROP COLUMN get_index_option_chain,
    DROP COLUMN get_constituents,
    DROP COLUMN get_constituents_option_chain;
```

### 2c. `pub_config.futures_underlyings` — Futures-Orchestrierung

Ersetzt `pub_futures.futures_overview`:

```sql
CREATE TABLE pub_config.futures_underlyings (
    underlying_ric    VARCHAR PRIMARY KEY,
    underlying_name   VARCHAR,
    futures_chain_ric VARCHAR,
    fetch_chains      BOOLEAN DEFAULT TRUE,
    fetch_prices      BOOLEAN DEFAULT TRUE,
    active            BOOLEAN DEFAULT TRUE
);

INSERT INTO pub_config.futures_underlyings
SELECT * FROM pub_futures.futures_overview;

DROP TABLE pub_futures.futures_overview;
```

### 2d. `pub_config.rates` — Rates-Orchestrierung

Ersetzt die hardcodierten FRED-Serien in `modules/fetch_usd_rates.py`:

```sql
CREATE TABLE pub_config.rates (
    series_id     VARCHAR PRIMARY KEY,  -- FRED-Kennung, z.B. 'DGS3MO'
    tenor         VARCHAR NOT NULL,     -- '3m', '1y', '10y', etc.
    tenor_days    INTEGER NOT NULL,     -- Laufzeit in Tagen für Bootstrap (91, 365, etc.)
    rate_type     VARCHAR NOT NULL,     -- 'treasury_cmt'
    source        VARCHAR NOT NULL,     -- 'FRED'
    active        BOOLEAN DEFAULT TRUE
);

INSERT INTO pub_config.rates (series_id, tenor, tenor_days, rate_type, source) VALUES
    ('DGS1MO',  '1m',  30,    'treasury_cmt', 'FRED'),
    ('DGS3MO',  '3m',  91,    'treasury_cmt', 'FRED'),
    ('DGS6MO',  '6m',  182,   'treasury_cmt', 'FRED'),
    ('DGS1',    '1y',  365,   'treasury_cmt', 'FRED'),
    ('DGS2',    '2y',  730,   'treasury_cmt', 'FRED'),
    ('DGS3',    '3y',  1095,  'treasury_cmt', 'FRED'),
    ('DGS5',    '5y',  1825,  'treasury_cmt', 'FRED'),
    ('DGS7',    '7y',  2555,  'treasury_cmt', 'FRED'),
    ('DGS10',   '10y', 3650,  'treasury_cmt', 'FRED'),
    ('DGS20',   '20y', 7300,  'treasury_cmt', 'FRED'),
    ('DGS30',   '30y', 10950, 'treasury_cmt', 'FRED');
```

`modules/fetch_usd_rates.py` liest die aktiven Serien beim Start aus `pub_config.rates`.

---

## 3. Unified Options Chain Table

### Ticker-Logik

`option_ric` ist überall die **Basisform**: kein führendes `/`, kein `^`-Suffix.
Beispiel: `SPXb202669000.U`

| Verwendung | Format | Beispiel |
|---|---|---|
| DB-Key (überall) | Basisform | `SPXb202669000.U` |
| Eikon live fetch | `/` voranstellen | `/SPXb202669000.U` |
| Eikon hist fetch | `^`-Suffix anhängen | `SPXb202669000.U^B26` |

`option_ric_hist` wird **beim ersten Schreiben berechnet und gespeichert**
via `build_option_ric_hist()` in `modules/utils.py` (siehe unten).
Live-Chain wird immer vollständig neu gezogen (kein Rebuild aus DB).

**Hilfsfunktion in `modules/utils.py`:**
```python
def build_option_ric_hist(option_ric: str, expiry_date: date) -> str:
    """Basisform → historischer RIC mit ^-Suffix."""
    CALL_LETTERS = list('abcdefghijkl')
    PUT_LETTERS  = list('mnopqrstuvwx')
    letter = option_ric[3].lower()
    if letter in CALL_LETTERS:
        suffix_letter = letter.upper()
    else:
        suffix_letter = CALL_LETTERS[PUT_LETTERS.index(letter)].upper()
    yy = str(expiry_date.year)[-2:]
    return f'{option_ric}^{suffix_letter}{yy}'
```

### Neues Schema

```sql
CREATE TABLE pub_options.options_chains (
    underlying_ric   VARCHAR      NOT NULL,
    option_ric       VARCHAR      NOT NULL,   -- Basisform: kein /, kein ^
    option_ric_hist  VARCHAR,                 -- ^-Suffix-Form; NULL solange aktiv
    expiry_date      DATE         NOT NULL,
    strike           NUMERIC      NOT NULL,
    option_type      VARCHAR(4)   NOT NULL,   -- CALL / PUT
    expiry_type      CHAR(1),                 -- W / M / L
    first_seen_date  DATE         NOT NULL,
    source           CHAR(2)      NOT NULL DEFAULT 'EK',
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (underlying_ric, option_ric)
);
```

### Migration

```sql
-- Schritt 1: Aus options_chains_history (bereits Basisform)
INSERT INTO pub_options.options_chains_new
    (underlying_ric, option_ric, option_ric_hist, expiry_date,
     strike, option_type, expiry_type, first_seen_date, source)
SELECT underlying_ric, option_ric, option_ric_hist, expiry_date,
       strike, option_type, expiry_type, expiry_date, 'EK'
FROM pub_options.options_chains_history
ON CONFLICT DO NOTHING;

-- Schritt 2: Aus altem options_chains (/ entfernen, option_ric_hist per Python berechnen)
-- Python-Skript: liest altes options_chains, berechnet Basisform + hist-RIC, schreibt in neue Tabelle
-- first_seen_date = MIN(snapshot_date) pro option_ric

-- Schritt 3: options_chains_history droppen
DROP TABLE pub_options.options_chains_history;
```

### Migration `options_prices` (Basisform herstellen)

```sql
-- Duplikate entfernen (/-Prefix wo Basisform schon existiert):
DELETE FROM pub_options.options_prices op1
WHERE op1.option_ric LIKE '/%'
  AND EXISTS (
      SELECT 1 FROM pub_options.options_prices op2
      WHERE op2.option_ric = LTRIM(op1.option_ric, '/')
        AND op2.price_date  = op1.price_date
  );

-- / entfernen:
UPDATE pub_options.options_prices
SET option_ric = LTRIM(option_ric, '/')
WHERE option_ric LIKE '/%';
```

### Code-Änderungen
- `fetch_option_chains_index.py` — strippt `/`, berechnet `option_ric_hist`, setzt `first_seen_date`
- `fetch_option_chains_constituents.py` — wie oben
- `fetch_option_prices.py` — live fetch via `'/' + option_ric`, Join auf Basisform
- `build_spx_hist_chains_fast.py` — nach Refactoring obsolet

---

## 4. Source + Timestamp in allen Tabellen

Jede Tabelle bekommt (soweit noch nicht vorhanden):

```sql
source     CHAR(2)      NOT NULL DEFAULT 'EK',
created_at TIMESTAMPTZ  NOT NULL DEFAULT now()
```

| `source` | Bedeutung |
|---|---|
| `EK` | Daten direkt aus Eikon |
| `FM` | Eigene Berechnungen (SVIX, europäische Optionen, bootstrapped Zero Rates) |

**Migration alle bestehenden Tabellen:**
```sql
ALTER TABLE pub_equity.stock_prices_daily   ADD COLUMN source CHAR(2) DEFAULT 'EK', ADD COLUMN created_at TIMESTAMPTZ DEFAULT now();
ALTER TABLE pub_equity.index_prices_daily   ADD COLUMN source CHAR(2) DEFAULT 'EK', ADD COLUMN created_at TIMESTAMPTZ DEFAULT now();
ALTER TABLE pub_equity.fundamentals_quarterly ADD COLUMN source CHAR(2) DEFAULT 'EK', ADD COLUMN created_at TIMESTAMPTZ DEFAULT now();
ALTER TABLE pub_equity.dividends            ADD COLUMN source CHAR(2) DEFAULT 'EK', ADD COLUMN created_at TIMESTAMPTZ DEFAULT now();
ALTER TABLE pub_equity.index_constituents   ADD COLUMN source CHAR(2) DEFAULT 'EK', ADD COLUMN created_at TIMESTAMPTZ DEFAULT now();
ALTER TABLE pub_options.options_prices      ADD COLUMN source CHAR(2) DEFAULT 'EK', ADD COLUMN created_at TIMESTAMPTZ DEFAULT now();
ALTER TABLE pub_rates.usd_rates             ADD COLUMN created_at TIMESTAMPTZ DEFAULT now();
-- pub_rates.usd_rates hat bereits source-Spalte ('par'/'bootstrapped') → umbenennen:
-- ALTER TABLE pub_rates.usd_rates RENAME COLUMN source TO rate_type;
-- ALTER TABLE pub_rates.usd_rates ADD COLUMN source CHAR(2) DEFAULT 'EK';
-- UPDATE pub_rates.usd_rates SET source = 'FM' WHERE rate_type = 'zero';
ALTER TABLE pub_futures.futures_chains      ADD COLUMN source CHAR(2) DEFAULT 'EK', ADD COLUMN created_at TIMESTAMPTZ DEFAULT now();
ALTER TABLE pub_futures.futures_prices      ADD COLUMN source CHAR(2) DEFAULT 'EK', ADD COLUMN created_at TIMESTAMPTZ DEFAULT now();
```

---

## 5. Live-Pipeline + Historisches Backfill in allen Modulen

Einheitliche Signatur für alle Module:

```python
def run(
    snapshot_date: str | None = None,  # None → 1. des aktuellen Monats
    mode: str = 'live',                # 'live' | 'backfill'
    start_date: str | None = None,     # nur für mode='backfill'
    end_date:   str | None = None,     # nur für mode='backfill'
) -> None:
```

- `mode='live'`: Delta-Logik ab MAX(date)+1
- `mode='backfill'`: verarbeitet `[start_date, end_date]` vollständig, idempotent

**`run_monthly.py`** — unverändert, ruft alle Module mit `mode='live'` auf.

**Neues `run_backfill.py`:**
```python
fetch_prices_daily.run(mode='backfill', start_date='2016-01-01', end_date='2026-03-01')
```

---

## 6. Connection Pooling

```python
# config/db.py
engine = create_engine(
    DATABASE_URL,
    poolclass=QueuePool,
    pool_size=5,
    max_overflow=10,
    pool_timeout=30,
    pool_recycle=1800,
    pool_pre_ping=True,   # auto-reconnect
)
```

---

## 7. Zentrales Logging

```python
# config/logging_setup.py
def setup_logging(name: str = 'pipeline', log_file: str | None = None) -> logging.Logger:
    fmt = '%(asctime)s | %(levelname)-8s | %(name)s | %(message)s'
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=[
        logging.StreamHandler(sys.stdout),
        *([ logging.FileHandler(Path('logs') / log_file) ] if log_file else [])
    ])
    return logging.getLogger(name)
```

Alle `print()` → `logger.info/warning/error()`.

---

## 8. Zentrale Retry-Logik

```python
# modules/utils.py
def eikon_fetch(fn, *args, max_retries: int = 5, sleep: float = 63.0, **kwargs):
    for attempt in range(max_retries + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if ('429' in str(e) or 'limit' in str(e).lower()) and attempt < max_retries:
                logger.warning(f'Rate limit – waiting {sleep}s ({attempt+1}/{max_retries})')
                time.sleep(sleep)
            else:
                logger.error(f'Eikon fetch failed after {attempt} retries: {e}')
                return None
```

---

## 9. DDL-Management

`schema/ddl.sql` — alle `CREATE TABLE IF NOT EXISTS` in Abhängigkeitsreihenfolge.
`schema/migrate.py` — führt `ddl.sql` aus, sicher wiederholbar.

---

## 10. Pipeline-Run-Log (`pub_config.pipeline_runs`)

Jeder Modulaufruf schreibt einen Eintrag — vollständige Ausführungshistorie für
Debugging, Monitoring und Nachvollziehbarkeit.

```sql
CREATE TABLE pub_config.pipeline_runs (
    id            SERIAL PRIMARY KEY,
    module        VARCHAR      NOT NULL,   -- 'fetch_option_chains_index', etc.
    mode          VARCHAR      NOT NULL,   -- 'live' | 'backfill'
    started_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ,
    rows_written  INTEGER,
    status        VARCHAR      NOT NULL,   -- 'running' | 'success' | 'error'
    error_msg     TEXT,
    snapshot_date DATE
);
```

Jedes Modul schreibt beim Start `status='running'`, beim Ende `status='success'`
oder `status='error'` mit optionaler Fehlermeldung. Damit ist jederzeit nachvollziehbar
wann welches Modul zuletzt erfolgreich lief und wie viele Zeilen geschrieben wurden.

---

## 11. Datenbankindizes

Für alle großen Tabellen explizite Indizes anlegen (über PK hinaus):

```sql
-- options_prices: häufige Filterung nach Datum
CREATE INDEX IF NOT EXISTS idx_options_prices_price_date
    ON pub_options.options_prices (price_date);

-- options_chains: häufige Filterung nach expiry_date und underlying
CREATE INDEX IF NOT EXISTS idx_options_chains_expiry
    ON pub_options.options_chains (underlying_ric, expiry_date);

-- stock_prices_daily: Filterung nach Datum
CREATE INDEX IF NOT EXISTS idx_stock_prices_trade_date
    ON pub_equity.stock_prices_daily (trade_date);

-- index_prices_daily: Filterung nach Datum
CREATE INDEX IF NOT EXISTS idx_index_prices_trade_date
    ON pub_equity.index_prices_daily (trade_date);

-- usd_rates: Filterung nach Datum und Tenor
CREATE INDEX IF NOT EXISTS idx_usd_rates_trade_date
    ON pub_rates.usd_rates (trade_date);
```

---

## 12. Datenbankindizes für `options_prices` — Partitionierung prüfen

`pub_options.options_prices` hat >10 Mio. Zeilen und wächst weiter.
Bei Bedarf (Abfrageperformance) kann die Tabelle nach Jahr partitioniert werden:

```sql
-- Optionale Range-Partitionierung nach price_date (nur wenn Performance-Problem auftritt)
-- PARTITION BY RANGE (price_date)
-- Partitionen: 2016, 2017, ..., 2026
```

Entscheidung nach erstem SVIX-Compute-Lauf — erst dann ist klar ob Partitionierung nötig.

---

## Implementierungsreihenfolge

### Aufteilung in zwei Sessions (je ~80-100k Token)

**Session 1 — Infrastruktur & DB-Migration:**
Schritte 1–6 unten. Endet mit vollständig migrierter DB und verifizierten Daten.
Kein Eikon nötig.

**Session 2 — Module umschreiben:**
Schritte 7–9 unten. Setzt Session 1 als abgeschlossen voraus.
Eikon für abschließende Smoke-Tests empfohlen.

---

### Schritte

1. `config/db.py` — Connection Pooling
2. `config/logging_setup.py` — Logging-Setup
3. `modules/utils.py` — `eikon_fetch()` + `build_option_ric_hist()` ergänzen
4. DB-Migration:
   a. `pub_config`-Schema anlegen
   b. `pub_config.data_sources` anlegen + aus `overview_company_data` befüllen
   c. `pub_config.stock_indices` + `pub_config.futures_underlyings` + `pub_config.rates` anlegen + befüllen
   d. `pub_config.pipeline_runs` anlegen
   e. Equity-Tabellen: umbenennen, Spalten harmonisieren, `crncy` + `source` + `created_at`
   f. `pub_options.options_chains` Unified Table + Migration
   g. `options_prices` Basisform-Migration (Duplikate → Strip)
   h. `source` + `created_at` in alle übrigen Tabellen
   i. Datenbankindizes anlegen
5. `config/schema.py` — liest aus `pub_config.data_sources`
6. `schema/ddl.sql` — Gesamtschema dokumentieren
7. Alle Module umschreiben:
   - Logging, Retry, Schema-Mapping via `config/schema.py`
   - `mode`-Parameter
   - Tabellennamen nur noch via `get_table()` / `TBL_*`-Konstanten
   - Pipeline-Run-Log schreiben
8. `run_monthly.py` + `run_backfill.py` aktualisieren
9. CLAUDE.md aktualisieren

---

## Hinweise

### PEP 8 — gilt für den gesamten Python-Code
- snake_case für Variablen, Funktionen, Module
- UPPER_SNAKE_CASE für Konstanten (z.B. `BATCH_SIZE`, `SLEEP_TIME`)
- CamelCase nur für Klassen
- Imports: stdlib → third-party → local, je Gruppe alphabetisch
- Zeilenlänge: max. 100 Zeichen
- Keine `import *`
- Type hints für alle Funktionssignaturen
- Leerzeilen: 2 zwischen Top-Level-Definitionen, 1 innerhalb von Klassen

### Allgemein
- Alle Migrationen sind additiv — keine Daten gehen verloren
- `build_spx_hist_chains_fast.py` + `build_spx_hist_prices.py` werden nach
  dem Refactoring obsolet (Backfill-Mode in reguläre Module überführt)
- Eikon muss während der Implementierung NICHT laufen
- `pub_rates.usd_rates` hat bereits eine `source`-Spalte mit Werten `'par'`/`'bootstrapped'`
  → umbenennen in `rate_type`, neue `source CHAR(2)` ('EK'/'FM') ergänzen
