# Instruction: Klassifikation von Options-Fälligkeiten (W / M / L)

> **Version:** 1.2
> **Erstellt:** 2026-03-24
> **Projekt:** research_db (PostgreSQL)

---

## 1. Ziel und Marktlogik

Die Klassifikation folgt der **Marktstruktur**, nicht reiner Kalenderlogik.

| Code | Typ | Gilt für | Definition |
|------|-----|----------|------------|
| `W` | Weekly | Index + Equity | Alle Fälligkeiten, die kein Standard-Monatsverfall sind |
| `M` | Monthly | Index + Equity | 3. Freitag jedes Monats (feiertagsbereinigt) |
| `L` | LEAPS | Equity only | Laufzeit > 12 Monate zum `snapshot_date` (`tau > 1.0`) |

**Hinweise:**
- Für Einzelaktien gibt es kein eigenständiges Quarterly-Produkt. Der 3. Freitag
  im März ist strukturell identisch mit dem 3. Freitag im April — beide sind `M`.
- LEAPS sind als eigenständige Serie gelistet und haben andere Liquiditätsprofile.
  Die Grenze `tau > 1.0` bezieht sich auf das `snapshot_date` (Aufnahmezeitpunkt
  in die DB) und bleibt über die Lebensdauer des Kontrakts konstant.
- Für Index-Optionen (SPX etc.) existieren EOM-Quarterly-Kontrakte (`Q`) als
  eigenes Produkt, die über separate Chain-RICs gezogen werden müssten. Diese
  werden aktuell nicht geladen und daher hier nicht kodiert. Erweiterung auf `Q`
  erfolgt, sobald diese Chains in der Pipeline ergänzt werden.

---

## 2. Schema-Erweiterung

```sql
ALTER TABLE pub_options.options_chains
ADD COLUMN IF NOT EXISTS expiry_type CHAR(1);

COMMENT ON COLUMN pub_options.options_chains.expiry_type IS
'W=Weekly, M=Monthly (3rd Friday), L=LEAPS (tau>1.0 at snapshot, equity only)';
```

---

## 3. Klassifikationslogik

### 3.1 Utility-Modul: modules/utils.py

```python
"""
modules/utils.py
Gemeinsame Hilfsfunktionen fuer die Options-Pipeline.
"""
import datetime

# US Federal Holidays, die auf einen Freitag fallen und den Monatsverfall verschieben.
# Jaehrlich pruefen und ggf. erweitern!
US_FRIDAY_HOLIDAYS: set[datetime.date] = {
    datetime.date(2026, 6, 19),  # Juneteenth 2026 faellt auf Freitag
    # Naechste Juneteenth-Freitage: 2032-06-19
}


def third_friday(year: int, month: int) -> datetime.date:
    """3. Freitag eines Monats."""
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(4 - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=2)


def canonical_monthly_expiry(year: int, month: int) -> datetime.date:
    """
    Tatsaechliches Datum des Standard-Monatsverfalls (3. Freitag).
    Bei US-Feiertag (Freitag): Verschiebung auf Donnerstag davor.
    """
    tf = third_friday(year, month)
    if tf in US_FRIDAY_HOLIDAYS:
        return tf - datetime.timedelta(days=1)
    return tf


def classify_expiry(expiry_date: datetime.date,
                    snapshot_date: datetime.date,
                    asset_type: str) -> str:
    """
    Klassifiziert ein Verfallsdatum.

    Parameters:
        expiry_date:   Fälligkeitsdatum der Option
        snapshot_date: snapshot_date der Chain (= 1. des Monats)
        asset_type:    'index' oder 'equity'

    Returns:
        'M'  Standard-Monatsverfall (3. Freitag, feiertagsbereinigt)
        'L'  LEAPS: tau > 1.0 zum snapshot_date (nur equity)
        'W'  Weekly: alles andere
    """
    # LEAPS: nur fuer Einzelaktien, tau > 1.0 zum snapshot_date
    if asset_type == 'equity':
        tau = (expiry_date - snapshot_date).days / 365.25
        if tau > 1.0:
            return 'L'

    # M: Standard-Monatsverfall (gilt fuer Index und Equity gleich)
    if expiry_date == canonical_monthly_expiry(expiry_date.year, expiry_date.month):
        return 'M'

    return 'W'
```

### 3.2 Beispiele

| expiry_date | snapshot_date | asset_type | tau  | Typ | Begründung |
|-------------|---------------|------------|------|-----|------------|
| 2026-03-20  | 2026-03-01    | index      | 0.05 | M   | 3. Freitag März |
| 2026-03-27  | 2026-03-01    | equity     | 0.07 | W   | Kein 3. Freitag |
| 2026-04-17  | 2026-03-01    | equity     | 0.13 | M   | 3. Freitag April |
| 2026-04-24  | 2026-03-01    | equity     | 0.15 | W   | Kein 3. Freitag |
| 2026-06-18  | 2026-03-01    | equity     | 0.30 | M   | Juneteenth-verschoben (19.→18.) |
| 2026-06-19  | 2026-03-01    | equity     | 0.30 | W   | 19. Juni = Feiertag, kein Verfall |
| 2026-12-18  | 2026-03-01    | equity     | 0.80 | M   | 3. Freitag Dez, tau < 1.0 |
| 2027-01-15  | 2026-03-01    | equity     | 0.87 | M   | 3. Freitag Jan 2027, tau < 1.0 |
| 2027-03-19  | 2026-03-01    | equity     | 1.05 | L   | tau > 1.0 → LEAPS |
| 2028-01-21  | 2026-03-01    | equity     | 1.90 | L   | tau > 1.0 → LEAPS |

---

## 4. Backfill: Altbestand klassifizieren

```python
"""
backfill_expiry_type.py
Klassifiziert alle bestehenden Zeilen in pub_options.options_chains.
Idempotent: Zeilen mit expiry_type IS NOT NULL werden uebersprungen.
"""
import datetime
from sqlalchemy import text
from config.db import engine
from modules.utils import classify_expiry

with engine.begin() as con:
    # Alle noch nicht klassifizierten Kombinationen laden
    rows = con.execute(text("""
        SELECT DISTINCT
            oc.expiry_date,
            oc.snapshot_date,
            CASE
                WHEN io.index_ric IS NOT NULL THEN 'index'
                ELSE 'equity'
            END AS asset_type
        FROM pub_options.options_chains oc
        LEFT JOIN pub_equity.index_overview io
          ON io.index_ric = oc.underlying_ric
        WHERE oc.expiry_type IS NULL
        ORDER BY oc.snapshot_date, oc.expiry_date
    """)).fetchall()

    print(f"{len(rows)} distinct (expiry_date, snapshot_date, asset_type) zu klassifizieren...")

    for expiry_date, snapshot_date, asset_type in rows:
        etype = classify_expiry(expiry_date, snapshot_date, asset_type)
        con.execute(text("""
            UPDATE pub_options.options_chains oc
            SET expiry_type = :etype
            WHERE oc.expiry_date    = :expiry_date
              AND oc.snapshot_date  = :snapshot_date
              AND oc.expiry_type IS NULL
              AND (
                  CASE WHEN EXISTS (
                      SELECT 1 FROM pub_equity.index_overview io
                      WHERE io.index_ric = oc.underlying_ric
                  ) THEN 'index' ELSE 'equity' END
              ) = :asset_type
        """), {
            'etype':         etype,
            'expiry_date':   expiry_date,
            'snapshot_date': snapshot_date,
            'asset_type':    asset_type,
        })

    print("Backfill abgeschlossen.")
```

**Kontrolle nach Backfill:**

```sql
SELECT
    CASE WHEN io.index_ric IS NOT NULL THEN 'index' ELSE 'equity' END AS asset_type,
    oc.expiry_type,
    COUNT(DISTINCT oc.expiry_date) AS n_expiries,
    COUNT(*)                       AS n_rics
FROM pub_options.options_chains oc
LEFT JOIN pub_equity.index_overview io ON io.index_ric = oc.underlying_ric
GROUP BY 1, 2
ORDER BY 1, 2;
```

---

## 5. Integration in Modul 2 und 3 (künftige Pulls)

In beiden Chain-Modulen nach dem Expiry-Mapping ergänzen:

```python
from modules.utils import classify_expiry

# asset_type je nach Modul:
# Modul 2 (Index):  asset_type = 'index'
# Modul 3 (Equity): asset_type = 'equity'

df['expiry_type'] = df.apply(
    lambda row: classify_expiry(row['expiry_date'], snapshot_date, asset_type),
    axis=1
)
```

---

## 6. Verwendung in Research (SVIX-Berechnung)

Nach dem Backfill filtert `compute_svix_stock.py` in `_load_options_batch()`
Weeklys automatisch heraus:

```sql
-- Equity SVIX: nur Monats- und LEAPS-Verfälle
AND oc.expiry_type IN ('M', 'L')

-- Index SVIX (compute_svix_spx.py):
AND oc.expiry_type = 'M'
```

---

## 7. Checkliste

- [ ] `ALTER TABLE pub_options.options_chains ADD COLUMN expiry_type CHAR(1)` ausführen
- [ ] `modules/utils.py` anlegen
- [ ] `backfill_expiry_type.py` einmalig ausführen und Kontrolle per SQL
- [ ] `fetch_option_chains_index.py` ergänzen (Modul 2)
- [ ] `fetch_option_chains_constituents.py` ergänzen (Modul 3)
- [ ] `compute_svix_stock.py`: Filter `expiry_type IN ('M', 'L')` in `_load_options_batch`
- [ ] `compute_svix_spx.py`: Filter `expiry_type = 'M'` ergänzen
