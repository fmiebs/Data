# CLAUDE.md – Projekt research_db

## Credentials
- Alle Credentials in `C:\Users\Miebs\PycharmProjects\Config\.env`
- Variablen: `REFINITIV_APP_KEY`, `DB_USER`, `DB_PASS`, `DB_HOST`, `DB_PORT`, `DB_NAME`

## Datenbank
- PostgreSQL, Datenbank: `research_db`
- Schema `pub_equity`: `index_overview`, `index_constituents`
- Schema `pub_options`: `options_chains`, `options_prices`

## API-Constraints
- **Ausschließlich `ek.get_data()`** aus dem `eikon`-Paket. Kein `rd`, kein `rdp`, kein `get_timeseries`.
- Eikon Desktop/Workspace muss auf dem Rechner laufen (API-Proxy).
- Rate Limiting: min. 0.5s zwischen Calls, 63s Backoff bei 429.

## Codestandards
- Idempotenz überall: Vor jedem Schreibvorgang prüfen ob Daten existieren.
- Fehlertoleranz: Einzelfehler loggen, Pipeline nicht abbrechen.
- Module exponieren eine `run(snapshot_date: str)`-Funktion.
- `snapshot_date` ist immer der 1. des aktuellen Monats.

## Pipeline-Dokumentation
- Vollständige Instruction: `docs/instructions/pipeline_options.md`
- Bestehender Backfill-Code als Referenz: `Constituents_Historical.py`
