"""
fetch_option_chains_constituents.py
------------------------------------
Fetches option chains for all SPX constituents of indices where
pub_config.stock_indices.fetch_constituent_options = TRUE.
Writes to pub_options.options_chains (unified table, Basisform).
"""
import logging
import time
from datetime import datetime

import eikon as ek
import pandas as pd
from sqlalchemy import text

from config import eikon_init  # noqa: F401
from config.db import engine
from config.logging_setup import setup_logging
from config.pipeline import run_finish, run_start
from modules.utils import build_option_ric_hist, classify_expiry, eikon_fetch

SCHEMA     = 'pub_options'
TABLE      = 'options_chains'
SLEEP_TIME = 0.5

logger = setup_logging('fetch_option_chains_constituents')


def _get_snapshot_date() -> str:
    today = datetime.now()
    return today.replace(day=1).strftime('%Y-%m-%d')


def _get_existing_rics(conn, underlying_ric: str) -> set:
    rows = conn.execute(
        text(f"SELECT option_ric FROM {SCHEMA}.{TABLE} WHERE underlying_ric = :ric"),
        {'ric': underlying_ric}
    ).fetchall()
    return {r[0] for r in rows}


def _has_calls_and_puts(conn, underlying_ric: str, snapshot_date: str) -> bool:
    row = conn.execute(
        text(f"SELECT COUNT(DISTINCT option_type) FROM {SCHEMA}.{TABLE} "
             "WHERE underlying_ric = :ric AND first_seen_date = :date"),
        {'ric': underlying_ric, 'date': snapshot_date}
    ).scalar()
    return row >= 2


def _map_option_type(val) -> str | None:
    if pd.isna(val):
        return None
    first = str(val).strip()[0].upper()
    if first == 'C':
        return 'CALL'
    if first == 'P':
        return 'PUT'
    return None


def _extract_ticker(constituent_ric: str) -> str:
    """'AAPL.O' -> 'AAPL', '.SPX' -> 'SPX'"""
    parts = constituent_ric.split('.')
    return parts[1] if parts[0] == '' else parts[0]


def _fetch(constituent_ric: str, snapshot_date: str) -> pd.DataFrame | None:
    ticker    = _extract_ticker(constituent_ric)
    chain_ric = f'0#{ticker}*.U'

    result = eikon_fetch(ek.get_data, chain_ric, ['EXPIR_DATE', 'STRIKE_PRC', 'PUTCALLIND'])
    if result is None:
        return None
    data, _ = result

    if data is None or data.empty:
        logger.warning('No data for %s', chain_ric)
        return None

    df = data.rename(columns={
        'Instrument': 'option_ric',
        'EXPIR_DATE': 'expiry_date',
        'STRIKE_PRC': 'strike',
        'PUTCALLIND': 'option_type',
    })
    df = df.dropna(subset=['strike', 'expiry_date'])
    df['option_type'] = df['option_type'].apply(_map_option_type)
    df = df.dropna(subset=['option_type'])

    df['expiry_date'] = pd.to_datetime(df['expiry_date']).dt.date
    snap             = pd.to_datetime(snapshot_date).date()

    # Strip leading / (Basisform)
    df['option_ric'] = df['option_ric'].str.lstrip('/')

    df['option_ric_hist'] = df.apply(
        lambda r: build_option_ric_hist(r['option_ric'], r['expiry_date']),
        axis=1
    )
    df['expiry_type'] = df['expiry_date'].apply(
        lambda d: classify_expiry(d, snap, 'equity')
    )
    df['underlying_ric']  = constituent_ric
    df['first_seen_date'] = snapshot_date
    df['source']          = 'EK'

    return df[['underlying_ric', 'option_ric', 'option_ric_hist', 'expiry_date',
               'strike', 'option_type', 'expiry_type', 'first_seen_date', 'source']]


def run(
    snapshot_date: str | None = None,
    mode: str = 'live',
    start_date: str | None = None,  # noqa: ARG001
    end_date: str | None = None,    # noqa: ARG001
) -> None:
    if snapshot_date is None:
        snapshot_date = _get_snapshot_date()

    logger.info('fetch_option_chains_constituents | snapshot_date=%s | mode=%s',
                snapshot_date, mode)
    run_id = run_start('fetch_option_chains_constituents', mode, snapshot_date)

    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT DISTINCT ic.constituent_ric
                FROM pub_equity.index_constituents ic
                JOIN pub_config.stock_indices si ON si.index_ric = ic.index_ric
                WHERE ic.snapshot_date = :date
                  AND si.fetch_constituent_options = TRUE
                  AND si.active = TRUE
            """),
            {'date': snapshot_date}
        ).fetchall()

    constituents = [r[0] for r in rows]
    logger.info('%d unique constituent(s) found', len(constituents))

    total_written = 0
    try:
        for ric in constituents:
            with engine.connect() as conn:
                if _has_calls_and_puts(conn, ric, snapshot_date):
                    logger.info('SKIP %s – calls+puts already complete', ric)
                    continue

            logger.info('Processing %s...', ric)
            df = _fetch(ric, snapshot_date)

            if df is not None and not df.empty:
                with engine.connect() as conn:
                    existing = _get_existing_rics(conn, ric)
                df = df[~df['option_ric'].isin(existing)]
                if not df.empty:
                    records = df.to_dict(orient='records')
                    with engine.begin() as conn:
                        conn.execute(text(f"""
                            INSERT INTO {SCHEMA}.{TABLE}
                                (underlying_ric, option_ric, option_ric_hist, expiry_date,
                                 strike, option_type, expiry_type, first_seen_date, source)
                            VALUES
                                (:underlying_ric, :option_ric, :option_ric_hist, :expiry_date,
                                 :strike, :option_type, :expiry_type, :first_seen_date, :source)
                            ON CONFLICT (underlying_ric, option_ric) DO NOTHING
                        """), records)
                    total_written += len(df)
                    logger.info('OK %s – %d rows written (%d already existed)',
                                ric, len(df), len(existing))
                else:
                    logger.info('SKIP %s – all rows already in DB', ric)
            else:
                logger.warning('WARN %s – nothing to write', ric)

            time.sleep(SLEEP_TIME)

        run_finish(run_id, rows_written=total_written)
        logger.info('fetch_option_chains_constituents done. rows_written=%d', total_written)
    except Exception as e:
        run_finish(run_id, error=str(e))
        raise


if __name__ == '__main__':
    run()
