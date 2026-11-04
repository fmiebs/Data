"""
compute_index_mcap.py
---------------------
Berechnet täglich Free-Float Market Cap (ff_mcap) und Gesamt-Market Cap (mcap)
für jeden Index aus Constituent-Daten.

Formel:
  mcap    = SUM(close_price × shares_outstanding)
  ff_mcap = SUM(close_price × shares_outstanding × free_float_pct / 100)

Quellen:
  - pub_equity.index_constituents          (monatlicher Snapshot; letzter verfügbarer ≤ trade_date)
  - pub_equity.stock_prices_daily.market_cap (TR.CompanyMarketCap — NICHT close_price *
    shares_outstanding: Preise sind splitbereinigt, shares_outstanding nicht)
  - pub_equity.stock_prices_daily          (tägliche Preise in Lokalwährung)
  - pub_equity.stock_fundamentals_quarterly (shares_outstanding, free_float_pct; forward-fill)

Ziel: pub_equity.computations_index_mcap

Delta-Logik: MAX(date)+1 oder start_date; verarbeitet in Monats-Chunks.
"""
import logging
from datetime import date, timedelta

from dateutil.relativedelta import relativedelta
from sqlalchemy import text

from config.db import engine
from config.logging_setup import setup_logging
from config.pipeline import run_start, run_finish

logger = setup_logging('compute_index_mcap')

SCHEMA = 'pub_equity'
TABLE  = 'computations_index_mcap'

# SQL: Aggregiert MCap und FF-MCap für einen Monats-Chunk.
# Optimierung: Snapshot-Lookup einmalig je Index (DISTINCT ON), nicht je Preiszeile.
# LATERAL JOIN für Fundamentals-forward-fill läuft nur je (constituent, trade_date).
_COMPUTE_SQL = """
INSERT INTO pub_equity.computations_index_mcap (date, index_ric, crncy, mcap, ff_mcap)
WITH
-- Einmalig: letzter Constituent-Snapshot je Index der <= chunk_start liegt
index_snapshots AS (
    SELECT DISTINCT ON (ic.index_ric)
        ic.index_ric,
        ic.snapshot_date
    FROM pub_equity.index_constituents ic
    JOIN pub_config.stock_indices si
        ON si.index_ric = ic.index_ric
        AND si.fetch_constituent_prices = TRUE
        AND si.active = TRUE
    WHERE ic.snapshot_date <= :start_date
    ORDER BY ic.index_ric, ic.snapshot_date DESC
),
-- Constituents dieses Snapshots
snapshot_constituents AS (
    SELECT isn.index_ric, ic.constituent_ric
    FROM index_snapshots isn
    JOIN pub_equity.index_constituents ic
        ON ic.index_ric     = isn.index_ric
        AND ic.snapshot_date = isn.snapshot_date
),
-- Tages-MCap im Chunk (TR.CompanyMarketCap, splitkorrekt, in mcap_crncy notiert)
prices AS (
    SELECT ric, trade_date, market_cap, mcap_crncy
    FROM pub_equity.stock_prices_daily
    WHERE trade_date BETWEEN :start_date AND :end_date
      AND market_cap IS NOT NULL
),
-- Constituents x MCap; org_permid aus der Map, Free Float via LATERAL
constituent_days AS (
    SELECT
        sc.index_ric,
        p.trade_date,
        sc.constituent_ric,
        p.market_cap,
        COALESCE(org.org_permid, sc.constituent_ric) AS org_key,
        fq.free_float_pct,
        fq.shares_outstanding
    FROM snapshot_constituents sc
    JOIN prices p ON p.ric = sc.constituent_ric
    -- org_permid aus pub_config.ric_org_map (eine Zeile je RIC, vollstaendig).
    -- Frueher aus stock_fundamentals_quarterly: dort fehlen abgeloeste RICs
    -- (FITB.OQ, LUMN.OQ, SKYD.N u.a.), die dadurch als eigenes Unternehmen
    -- gezaehlt wurden statt mit ihrer Nachfolge-Gattung zusammenzufallen.
    LEFT JOIN pub_config.ric_org_map org ON org.ric = sc.constituent_ric
    LEFT JOIN LATERAL (
        -- Free Float: letztes Quartal <= trade_date MIT Wert (nicht einfach das letzte)
        SELECT free_float_pct, shares_outstanding
        FROM pub_equity.stock_fundamentals_quarterly
        WHERE ric              = sc.constituent_ric
          AND period_end_date <= p.trade_date
          AND free_float_pct IS NOT NULL
        ORDER BY period_end_date DESC
        LIMIT 1
    ) fq ON TRUE
),
-- Je Unternehmen einmal: TR.CompanyMarketCap gilt fuer ALLE Gattungen (GOOGL = GOOG).
-- Free Float = nach shares_outstanding gewichtetes Mittel der im Index enthaltenen Gattungen.
with_fundamentals AS (
    SELECT
        index_ric,
        trade_date,
        MAX(market_cap) AS market_cap,
        CASE
            WHEN SUM(COALESCE(shares_outstanding, 1)) FILTER (WHERE free_float_pct IS NOT NULL) > 0
            THEN SUM(free_float_pct * COALESCE(shares_outstanding, 1))
                     FILTER (WHERE free_float_pct IS NOT NULL)
                 / SUM(COALESCE(shares_outstanding, 1)) FILTER (WHERE free_float_pct IS NOT NULL)
            ELSE MAX(free_float_pct)
        END AS free_float_pct
    FROM constituent_days
    GROUP BY index_ric, trade_date, org_key
)
SELECT
    wf.trade_date                                                        AS date,
    wf.index_ric,
    si.currency                                                          AS crncy,
    SUM(wf.market_cap)::NUMERIC(24,2)                                    AS mcap,
    SUM(CASE WHEN wf.free_float_pct IS NOT NULL
             THEN wf.market_cap * wf.free_float_pct / 100.0
             ELSE NULL END)::NUMERIC(24,2)                               AS ff_mcap
FROM with_fundamentals wf
JOIN pub_config.stock_indices si ON si.index_ric = wf.index_ric
GROUP BY wf.trade_date, wf.index_ric, si.currency
ON CONFLICT (date, index_ric) DO UPDATE SET
    crncy   = EXCLUDED.crncy,
    mcap    = EXCLUDED.mcap,
    ff_mcap = EXCLUDED.ff_mcap
"""


def _get_start_date() -> date:
    with engine.connect() as conn:
        row = conn.execute(
            text(f"SELECT MAX(date) FROM {SCHEMA}.{TABLE}")
        ).scalar()
    if row is not None:
        return row + timedelta(days=1)
    today = date.today()
    return today.replace(year=today.year - 10)


def run(
    snapshot_date: str | None = None,  # noqa: ARG001
    mode: str = 'live',
    start_date: str | None = None,
    end_date: str | None = None,
) -> None:
    end_dt = date.fromisoformat(end_date) if end_date else date.today()

    if mode == 'backfill' and start_date:
        start_dt = date.fromisoformat(start_date)
    else:
        start_dt = _get_start_date()

    if start_dt > end_dt:
        logger.info('compute_index_mcap already up to date.')
        return

    logger.info('compute_index_mcap | mode=%s | %s – %s', mode, start_dt, end_dt)
    run_id = run_start('compute_index_mcap', mode, snapshot_date)

    total_written = 0
    chunk_start = start_dt

    while chunk_start <= end_dt:
        chunk_end = min(
            (chunk_start + relativedelta(months=1)) - timedelta(days=1),
            end_dt
        )
        with engine.begin() as conn:
            result = conn.execute(
                text(_COMPUTE_SQL),
                {'start_date': chunk_start, 'end_date': chunk_end}
            )
            rows = result.rowcount

        total_written += rows
        logger.info('  %s – %s: %d rows', chunk_start, chunk_end, rows)
        chunk_start = chunk_end + timedelta(days=1)

    run_finish(run_id, rows_written=total_written)
    logger.info('compute_index_mcap done. rows_written=%d', total_written)


if __name__ == '__main__':
    run()
