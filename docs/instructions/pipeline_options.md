# CLI Instruction: Options Data Pipeline

> **Version:** 1.0  
> **Erstellt:** 2026-03-19  
> **Projekt:** research_db (PostgreSQL)  
> **Autor:** Felix / Claude  

---

## 1. Überblick

Diese Instruction beschreibt die Implementierung einer monatlichen Pipeline zum Laden von Index-Constituents, Options-Chains und Options-Preisen in die PostgreSQL-Datenbank `research_db`. Die Pipeline besteht aus vier Modulen und einem Orchestrator.

### Laufzeitmodell

Die Pipeline läuft **einmal monatlich**, idealerweise am selben Kalendertag. Alle Snapshots beziehen sich auf den **1. des laufenden Monats** als kanonisches `snapshot_date`.

### Prinzipien

- **Einzige API:** `eikon` Python-Paket, ausschließlich `ek.get_data()`. Keine anderen APIs (kein `rd`, kein `rdp`, kein `get_timeseries`).
- **Idempotenz:** Jedes Modul prüft vor dem Schreiben, ob Daten für den aktuellen Snapshot/Zeitraum bereits existieren. Kein Doppel-Insert.
- **Rate Limiting:** Mindestens 0.5 Sekunden Pause zwischen API-Calls. Bei HTTP 429 oder "limit"-Fehlern: 60 Sekunden warten, dann Retry.
- **Fehlertoleranz:** Einzelfehler bei einem Underlying dürfen die Pipeline nicht abbrechen. Fehler loggen, weiter mit nächstem Underlying.

---

## 2. Projektstruktur

```
C:\Users\Miebs\PycharmProjects\Data\
├── config/
│   ├── db.py                          # DB-Connection, Engine
│   └── eikon_init.py                  # Eikon App Key Setup
├── modules/
│   ├── fetch_constituents.py          # Modul 1: Index-Constituents
│   ├── fetch_option_chains_index.py   # Modul 2: Options-Chains (Indizes)
│   ├── fetch_option_chains_constituents.py  # Modul 3: Options-Chains (Constituents)
│   └── fetch_option_prices.py         # Modul 4: Historische Options-Preise
├── run_monthly.py                     # Orchestrator
└── docs/
    └── instructions/
        └── pipeline_options.md        # Diese Instruction
```

---

## 3. Konfiguration

### 3.1 Credentials

Alle Credentials liegen in `C:\Users\Miebs\PycharmProjects\Config\.env`:

```
REFINITIV_APP_KEY=...
DB_USER=...
DB_PASS=...
DB_HOST=...
DB_PORT=...
DB_NAME=...
```

### 3.2 config/db.py

```python
import os
from sqlalchemy import create_engine
from dotenv import load_dotenv

load_dotenv(r'C:\Users\Miebs\PycharmProjects\Config\.env')

engine = create_engine(
    f'postgresql://{os.getenv("DB_USER")}:{os.getenv("DB_PASS")}'
    f'@{os.getenv("DB_HOST")}:{os.getenv("DB_PORT")}/{os.getenv("DB_NAME")}'
)
```

### 3.3 config/eikon_init.py

```python
import os
import eikon as ek
from dotenv import load_dotenv

load_dotenv(r'C:\Users\Miebs\PycharmProjects\Config\.env')
ek.set_app_key(os.getenv('REFINITIV_APP_KEY'))
```

---

## 4. Datenbankschema

### 4.1 Bestehende Objekte (pub_equity)

**Schema:** `pub_equity` (existiert bereits)

**Tabelle `index_overview`** (existiert bereits, wird erweitert):

```sql
-- Neue Spalte ergänzen:
ALTER TABLE pub_equity.index_overview
ADD COLUMN IF NOT EXISTS option_chain_ric VARCHAR(50);

-- Beispielwerte manuell pflegen:
-- .SPX  → 'SPX*.U'
-- .DJI  → 'DJI*.U'
-- WICHTIG: Wert OHNE '0#'-Prefix speichern, das Prefix wird im Code ergänzt.
```

**Tabelle `index_constituents`** (existiert bereits):
- Felder: `index_ric`, `constituent_ric`, `symbol`, `company_name`, `snapshot_date`

### 4.2 Neue Objekte (pub_options)

```sql
CREATE SCHEMA IF NOT EXISTS pub_options;

-- options_chains: existiert bereits, ggf. nach pub_options migrieren
-- Erwartetes Schema:
-- CREATE TABLE pub_options.options_chains (
--     underlying_ric  VARCHAR(50)  NOT NULL,
--     option_ric      VARCHAR(50)  NOT NULL,
--     expiry_date     DATE         NOT NULL,
--     strike          NUMERIC      NOT NULL,
--     option_type     VARCHAR(4)   NOT NULL,  -- 'CALL' oder 'PUT'
--     snapshot_date   DATE         NOT NULL
-- );

-- Neue Tabelle:
CREATE TABLE IF NOT EXISTS pub_options.options_prices (
    option_ric  VARCHAR(50)  NOT NULL,
    price_date  DATE         NOT NULL,
    price_bid   NUMERIC,          -- NULL wenn kein Preis verfügbar
    price_ask   NUMERIC,          -- NULL wenn kein Preis verfügbar
    PRIMARY KEY (option_ric, price_date)
);
```

**Hinweis:** Die `options_chains`-Tabelle liegt aktuell möglicherweise noch in `pub_equity`. Die DDL oben zeigt das Zielschema. Migration bestehender Daten ist separat durchzuführen.

---

## 5. Modul 1: fetch_constituents.py

### Zweck
Zieht die aktuelle Zusammensetzung aller Indizes aus `index_overview` und schreibt sie nach `index_constituents`.

### Logik

1. `snapshot_date` = 1. des aktuellen Monats (z.B. `2026-03-01`)
2. Lies alle `index_ric` aus `pub_equity.index_overview`
3. Für jeden Index:
   a. **Idempotenz-Check:** `SELECT COUNT(*) FROM pub_equity.index_constituents WHERE index_ric = :ric AND snapshot_date = :date` → wenn > 0, skip
   b. API-Call: `ek.get_data(f'0#{ric}', ['TR.CommonName', 'TR.TickerSymbol'], {'SDate': snapshot_date})`
   c. Ergebnis-Mapping:
      - `Instrument` → `constituent_ric`
      - `Company Common Name` → `company_name`
      - `Ticker Symbol` → `symbol`
   d. Ergänze `index_ric` und `snapshot_date`
   e. Schreibe nach `pub_equity.index_constituents` via `df.to_sql()`
4. Rate Limiting: `time.sleep(0.5)` zwischen Calls

### Referenz-Code
Basiert auf `Constituents_Historical.py` (siehe Upload), angepasst auf:
- Dynamische Index-Liste statt hartkodiertem RIC
- Snapshot-Date = Monatserster statt historischer Backfill

---

## 6. Modul 2: fetch_option_chains_index.py

### Zweck
Zieht die Options-Chain für jeden Index und schreibt die **gesamte** Chain nach `pub_options.options_chains`.

### Logik

1. `snapshot_date` = 1. des aktuellen Monats
2. Lies alle Zeilen aus `pub_equity.index_overview` wo `option_chain_ric IS NOT NULL`
3. Für jeden Index:
   a. **Idempotenz-Check:** `SELECT COUNT(*) FROM pub_options.options_chains WHERE underlying_ric = :ric AND snapshot_date = :date` → wenn > 0, skip
   b. Konstruiere Chain-RIC: `f'0#{option_chain_ric}'` (z.B. `0#SPX*.U`)
   c. API-Call: `ek.get_data(chain_ric, ['EXPIR_DATE', 'STRIKE_PRC', 'PUTCALLIND'])`
   d. Ergebnis-Mapping:
      - `Instrument` → `option_ric`
      - `EXPIR_DATE` → `expiry_date` (als Date parsen)
      - `STRIKE_PRC` → `strike`
      - `PUTCALLIND` → `option_type` (Mapping: typischerweise 1=CALL, 2=PUT, prüfen!)
   e. Ergänze `underlying_ric` = Index-RIC (z.B. `.SPX`) und `snapshot_date`
   f. Entferne Zeilen mit NaN in kritischen Feldern (erste Zeile ist oft der Underlying selbst)
   g. Schreibe **gesamte** Chain nach `pub_options.options_chains`
4. Rate Limiting: `time.sleep(0.5)` zwischen Calls

### PUTCALLIND-Mapping
Das Feld `PUTCALLIND` kann verschiedene Formate liefern. Im Code robust behandeln:

```python
def map_option_type(val):
    """Maps PUTCALLIND values to standardized CALL/PUT."""
    if val in [1, '1', 'CALL', 'Call', 'C']:
        return 'CALL'
    elif val in [2, '2', 'PUT', 'Put', 'P']:
        return 'PUT'
    return None  # Unbekannt → rausfiltern
```

### Wichtige Hinweise
- Die Chain `0#TICKER*.U` liefert nur **Standard-Expiries** (keine Weeklys). Weekly-Chains hätten eigene RICs (z.B. `0#SPXW*.U`), die hier NICHT abgefragt werden.
- Die erste Zeile im Ergebnis ist häufig der Underlying selbst (kein Strike, kein Expiry) → rausfiltern.

---

## 7. Modul 3: fetch_option_chains_constituents.py

### Zweck
Zieht die Options-Chain für jeden Constituent des aktuellen Snapshots.

### Logik

1. `snapshot_date` = 1. des aktuellen Monats
2. Lies alle `constituent_ric` aus `pub_equity.index_constituents WHERE snapshot_date = :date`
   - Deduplizieren! Ein Constituent kann in mehreren Indizes vorkommen.
3. Für jeden Constituent:
   a. **Idempotenz-Check:** `SELECT COUNT(*) FROM pub_options.options_chains WHERE underlying_ric = :ric AND snapshot_date = :date` → wenn > 0, skip
   b. Konstruiere Chain-RIC aus Constituent-RIC:
      - Extrahiere Ticker vor dem Punkt: `AAPL.O` → `AAPL`
      - Chain-RIC: `f'0#{ticker}*.U'` → `0#AAPL*.U`
   c. API-Call: `ek.get_data(chain_ric, ['EXPIR_DATE', 'STRIKE_PRC', 'PUTCALLIND'])`
   d. Mapping identisch zu Modul 2
   e. `underlying_ric` = Constituent-RIC (z.B. `AAPL.O`)
   f. Schreibe **gesamte** Chain nach `pub_options.options_chains`
4. Rate Limiting: `time.sleep(0.5)` zwischen Calls

### Ticker-Extraktion

```python
def extract_ticker(constituent_ric: str) -> str:
    """Extracts ticker from RIC. 'AAPL.O' → 'AAPL', '.SPX' → 'SPX'"""
    ticker = constituent_ric.split('.')[0]
    if ticker == '':  # Fall: '.SPX' → split gibt ['', 'SPX']
        ticker = constituent_ric.split('.')[1]
    return ticker
```

### Fehlerbehandlung
Nicht jeder Constituent hat notwendigerweise eine Options-Chain (z.B. kleinere Titel oder nicht-US-Venues). Bei leeren Ergebnissen oder Access Denied: loggen und weiter.

---

## 8. Modul 4: fetch_option_prices.py

### Zweck
Zieht historische Bid/Ask-Preise für alle Options mit Fälligkeit innerhalb der nächsten 18 Monate.

### Logik

1. `snapshot_date` = 1. des aktuellen Monats
2. `cutoff_date` = `snapshot_date + 18 Monate`
3. Lies alle relevanten Options-RICs:
   ```sql
   SELECT DISTINCT option_ric
   FROM pub_options.options_chains
   WHERE expiry_date <= :cutoff_date
     AND snapshot_date = :snapshot_date
   ```
4. Für jede `option_ric`:
   a. **Delta-Logik:**
      ```sql
      SELECT MAX(price_date) FROM pub_options.options_prices WHERE option_ric = :ric
      ```
      - Kein Ergebnis → `start_date = snapshot_date - 1 Jahr`
      - Ergebnis vorhanden → `start_date = MAX(price_date) + 1 Tag`
      - Wenn `start_date > heute` → skip (schon aktuell)
   b. `end_date` = heute
   c. API-Call:
      ```python
      ek.get_data(
          option_ric,
          ['TR.BIDPRICE.Date', 'TR.BIDPRICE', 'TR.ASKPRICE'],
          {'SDate': start_date, 'EDate': end_date}
      )
      ```
   d. Ergebnis-Mapping:
      - `Date` → `price_date`
      - `TR.BIDPRICE` / `Bid Price` → `price_bid`
      - `TR.ASKPRICE` / `Ask Price` → `price_ask`
   e. NaN-Handling: Bid/Ask können NULL sein → als `None` in DB schreiben (PostgreSQL NULL). Die Zeile wird trotzdem eingefügt, damit die Delta-Logik weiß, dass der Tag abgefragt wurde.
   f. Ergänze `option_ric` und schreibe nach `pub_options.options_prices`
5. Rate Limiting: `time.sleep(0.5)` zwischen Calls

### Hinweise zu historischen Feldern
- **Nur `TR.BIDPRICE` und `TR.ASKPRICE`** liefern historische Daten. Die Real-Time-Felder `CF_BID`/`CF_ASK` geben nur den aktuellen Snapshot zurück.
- Das Datum kommt als eigene Spalte mit, wenn man `TR.BIDPRICE.Date` als Feld mit angibt.
- Expired Options können unter Umständen nicht aufgelöst werden (anderer RIC nach Archivierung). Diese werden übersprungen und in einem separaten Schritt behandelt.

### Batching-Strategie
Bei hunderten bis tausenden Options-RICs sollte der Call nicht einzeln pro RIC laufen. `ek.get_data()` akzeptiert eine Liste von Instrumenten:

```python
# Batch-Aufruf für bis zu 50 RICs gleichzeitig (konservatives Limit)
BATCH_SIZE = 50
for i in range(0, len(rics), BATCH_SIZE):
    batch = rics[i:i+BATCH_SIZE]
    df, err = ek.get_data(batch, ['TR.BIDPRICE.Date', 'TR.BIDPRICE', 'TR.ASKPRICE'],
                          {'SDate': start_date, 'EDate': end_date})
```

**Achtung:** Batching funktioniert nur, wenn alle RICs im Batch denselben Datumsbereich haben. Da die Delta-Logik pro RIC unterschiedliche Start-Dates liefern kann, müssen RICs mit gleichem Start-Date gruppiert werden.

---

## 9. Orchestrator: run_monthly.py

### Aufbau

```python
"""
Monthly pipeline orchestrator.
Runs all modules sequentially: Constituents → Index Chains → Constituent Chains → Prices
"""
import sys
from datetime import datetime

def get_snapshot_date() -> str:
    """Returns the 1st of the current month as snapshot date."""
    today = datetime.now()
    return today.replace(day=1).strftime('%Y-%m-%d')

def main():
    snapshot_date = get_snapshot_date()
    print(f"=== Monthly Pipeline Run | snapshot_date: {snapshot_date} ===\n")

    # Modul 1: Constituents
    print("--- Modul 1: Constituents ---")
    from modules.fetch_constituents import run
    run(snapshot_date)

    # Modul 2: Option Chains (Indizes)
    print("\n--- Modul 2: Option Chains (Indizes) ---")
    from modules.fetch_option_chains_index import run
    run(snapshot_date)

    # Modul 3: Option Chains (Constituents)
    print("\n--- Modul 3: Option Chains (Constituents) ---")
    from modules.fetch_option_chains_constituents import run
    run(snapshot_date)

    # Modul 4: Option Prices
    print("\n--- Modul 4: Option Prices ---")
    from modules.fetch_option_prices import run
    run(snapshot_date)

    print("\n=== Pipeline abgeschlossen ===")

if __name__ == '__main__':
    main()
```

### Jedes Modul exponiert eine `run(snapshot_date: str)`-Funktion
Das ist die einzige Schnittstelle zum Orchestrator. Module können auch standalone ausgeführt werden (mit Default `snapshot_date = 1. des aktuellen Monats`).

---

## 10. Eikon API-Referenz (Quick Reference)

### Chain-RICs

| Typ | Beispiel Underlying | Chain-RIC | Anmerkung |
|-----|---------------------|-----------|-----------|
| Index-Option | `.SPX` | `0#SPX*.U` | Aus `index_overview.option_chain_ric` |
| Index-Option (Weekly) | `.SPX` | `0#SPXW*.U` | **NICHT ZIEHEN** |
| Equity-Option | `AAPL.O` | `0#AAPL*.U` | Ticker vor dem Punkt |

### Felder

| Kontext | Felder | Anmerkung |
|---------|--------|-----------|
| Chain-Abfrage | `EXPIR_DATE`, `STRIKE_PRC`, `PUTCALLIND` | Real-Time-Felder, funktionieren auf Chain-RICs |
| Historische Preise | `TR.BIDPRICE`, `TR.ASKPRICE`, `TR.BIDPRICE.Date` | TR-Felder, funktionieren auf einzelne Option-RICs |
| Constituents | `TR.CommonName`, `TR.TickerSymbol` | Mit `SDate`-Parameter |

### Einschränkungen

- **TR-Felder funktionieren NICHT auf Chain-RICs** (`0#...`). Erst RICs extrahieren, dann TR-Felder auf Einzelinstrumenten abfragen.
- **Chain-Abfragen liefern nur aktive Optionen.** Expired Options sind nicht enthalten.
- **Historische Preise für expired Options** erfordern die Rekonstruktion des archivierten RIC (wird separat behandelt).

---

## 11. Checkliste vor der Implementierung

- [ ] `option_chain_ric` in `pub_equity.index_overview` ergänzen und für alle relevanten Indizes befüllen
- [ ] Schema `pub_options` anlegen
- [ ] Tabelle `pub_options.options_prices` anlegen
- [ ] Tabelle `options_chains` nach `pub_options` migrieren (falls noch in `pub_equity`)
- [ ] Eikon Desktop / Workspace muss auf dem Rechner laufen (API-Proxy)
- [ ] `.env`-Datei enthält alle DB- und Eikon-Credentials
- [ ] Testlauf mit einem einzelnen Index (z.B. `.DJI`) bevor Full Run
