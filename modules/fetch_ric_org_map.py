"""
fetch_ric_org_map.py
--------------------
Fuellt pub_config.ric_org_map: eine Zeile je RIC mit der Unternehmens-PermID.

Universum: alle RICs, die je in pub_equity.stock_prices_daily oder
pub_equity.index_constituents aufgetreten sind — auch delistete (^-Suffix) und
solche, die kein aktiver Snapshot mehr referenziert. Genau diese fehlen in
stock_fundamentals_quarterly und waren bisher ohne PermID.

WICHTIG — Bedeutung von org_permid:
    org_permid (TR.OrganizationID) ist ein UNTERNEHMENS-Schluessel. Er gruppiert
    deshalb auch Aktiengattungen (FOX.OQ/FOXA.OQ, NWSA.OQ/NWS.OQ) und
    Zweitnotierungen in anderer Waehrung (NEM.AX/NEM.N, RMD.AX/RMD.N).
    Fuer unternehmensweite Aggregation (Index-MCap) ist das richtig.
    Als Join-Key fuer Preisreihen ist er FALSCH — dafuer ist successor_ric da,
    das auf Instrumentenebene arbeitet und kuratiert wird (nie automatisch).

crncy, first_price, last_price kommen aus stock_prices_daily (kein Eikon-Call).
successor_ric wird von diesem Modul NIE ueberschrieben.

Modes:
  - live:    nur RICs ohne Eintrag oder ohne org_permid
  - refresh: alle RICs neu abfragen

Idempotent: Upsert je RIC.
"""
import logging
import time

import eikon as ek
import pandas as pd
from sqlalchemy import text

from config import eikon_init  # noqa: F401
from config.db import engine
from config.logging_setup import setup_logging
from config.pipeline import run_finish, run_start
from modules.utils import eikon_fetch, normalize_permid

SCHEMA     = 'pub_config'
TABLE      = 'ric_org_map'
SLEEP_TIME = 0.5
BATCH_SIZE = 10

logger = setup_logging('fetch_ric_org_map')


def _universe(conn, mode: str) -> list[str]:
    """Alle je aufgetretenen RICs; in mode='live' nur die ohne PermID."""
    missing_only = (
        f"WHERE NOT EXISTS (SELECT 1 FROM {SCHEMA}.{TABLE} m "
        f"WHERE m.ric = a.ric AND m.org_permid IS NOT NULL)"
        if mode == 'live' else ''
    )
    sql = f"""
        WITH alle AS (
            SELECT DISTINCT ric             AS ric FROM pub_equity.stock_prices_daily
            UNION
            SELECT DISTINCT constituent_ric AS ric FROM pub_equity.index_constituents
        )
        SELECT a.ric FROM alle a
        {missing_only}
        ORDER BY a.ric
    """
    return [r[0] for r in conn.execute(text(sql))]


def _price_meta(conn) -> dict[str, tuple]:
    """crncy (haeufigster echter Wert), erster und letzter Kurstag je RIC.

    Altzeilen tragen in crncy konstant 'LOC' — die werden verworfen und durch
    mcap_crncy ersetzt. Bei 9 von 1160 RICs bleibt beides leer (alte US-RICs,
    deren Reihe vor der crncy-Umstellung endete).
    """
    rows = conn.execute(text("""
        SELECT p.ric,
               COALESCE(
                 (SELECT p2.crncy FROM pub_equity.stock_prices_daily p2
                  WHERE p2.ric = p.ric AND p2.crncy IS NOT NULL AND p2.crncy <> 'LOC'
                  GROUP BY p2.crncy ORDER BY COUNT(*) DESC LIMIT 1),
                 (SELECT p3.mcap_crncy FROM pub_equity.stock_prices_daily p3
                  WHERE p3.ric = p.ric AND p3.mcap_crncy IS NOT NULL AND p3.mcap_crncy <> 'LOC'
                  GROUP BY p3.mcap_crncy ORDER BY COUNT(*) DESC LIMIT 1)
               ) AS crncy,
               MIN(p.trade_date), MAX(p.trade_date)
        FROM pub_equity.stock_prices_daily p
        GROUP BY p.ric
    """)).fetchall()
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


_norm_permid = normalize_permid   # gemeinsame Implementierung in modules/utils.py


def _fetch_batch(rics: list[str]) -> pd.DataFrame | None:
    result = eikon_fetch(ek.get_data, rics, ['TR.OrganizationID', 'TR.CommonName'])
    if result is None or result[0] is None or result[0].empty:
        return None
    data = result[0]

    if 'Instrument' not in data.columns:
        logger.warning("'Instrument' fehlt (cols: %s)", list(data.columns))
        return None

    permid_col = next((c for c in data.columns if c and 'permid' in str(c).lower()), None)
    name_col   = next((c for c in data.columns if c and 'common name' in str(c).lower()), None)
    if permid_col is None:
        logger.warning('keine PermID-Spalte (cols: %s)', list(data.columns))
        return None

    df = pd.DataFrame({
        'ric':        list(data['Instrument']),
        'org_permid': [_norm_permid(v) for v in data[permid_col]],
    })
    if name_col:
        names = data[name_col].astype(object)
        df['company_name'] = list(
            names.where(names.notna() & (names.astype(str).str.strip() != ''), other=None))
    else:
        df['company_name'] = None
    return df.drop_duplicates(subset=['ric'])


def _upsert(records: list[dict]) -> int:
    """Upsert je RIC. successor_ric bleibt unberuehrt."""
    with engine.begin() as conn:
        conn.execute(text(f"""
            INSERT INTO {SCHEMA}.{TABLE}
                (ric, org_permid, company_name, crncy, first_price, last_price,
                 source, updated_at)
            VALUES (:ric, :org_permid, :company_name, :crncy, :first_price,
                    :last_price, 'EK', now())
            ON CONFLICT (ric) DO UPDATE SET
                org_permid   = COALESCE(EXCLUDED.org_permid,   {TABLE}.org_permid),
                company_name = COALESCE(EXCLUDED.company_name, {TABLE}.company_name),
                crncy        = COALESCE(EXCLUDED.crncy,        {TABLE}.crncy),
                first_price  = EXCLUDED.first_price,
                last_price   = EXCLUDED.last_price,
                updated_at   = now()
        """), records)
    return len(records)


def run(
    snapshot_date: str | None = None,
    mode: str = 'live',
    start_date: str | None = None,  # noqa: ARG001
    end_date: str | None = None,    # noqa: ARG001
) -> None:
    logger.info('fetch_ric_org_map | mode=%s', mode)
    run_id = run_start('fetch_ric_org_map', mode, snapshot_date)

    with engine.connect() as conn:
        rics = _universe(conn, mode)
        meta = _price_meta(conn)

    logger.info('%d RICs zu holen (Kursmetadaten fuer %d RICs vorhanden)',
                len(rics), len(meta))

    total = 0
    try:
        for i in range(0, len(rics), BATCH_SIZE):
            batch = rics[i:i + BATCH_SIZE]
            df    = _fetch_batch(batch)

            if df is None or df.empty:
                logger.warning('batch %d: keine Daten (%s...)',
                               i // BATCH_SIZE + 1, batch[0])
                time.sleep(SLEEP_TIME)
                continue

            records = []
            for row in df.itertuples():
                crncy, first_price, last_price = meta.get(row.ric, (None, None, None))
                records.append({
                    'ric':          row.ric,
                    'org_permid':   row.org_permid,
                    'company_name': row.company_name,
                    'crncy':        crncy,
                    'first_price':  first_price,
                    'last_price':   last_price,
                })
            total += _upsert(records)

            n_ok = sum(1 for r in records if r['org_permid'])
            logger.info('batch %d: %d RICs, %d mit PermID (%s...)',
                        i // BATCH_SIZE + 1, len(records), n_ok, batch[0])
            time.sleep(SLEEP_TIME)

        run_finish(run_id, rows_written=total)
        logger.info('fetch_ric_org_map done. rows_written=%d', total)
    except Exception as e:
        run_finish(run_id, error=str(e))
        raise


if __name__ == '__main__':
    run()
