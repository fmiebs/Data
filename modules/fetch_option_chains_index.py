"""
fetch_option_chains_index.py
----------------------------
Fetches option chains for all indices in pub_config.stock_indices
where fetch_index_option_chain = TRUE. Writes to pub_options.options_chains.

New schema (unified): PK = (underlying_ric, option_ric).
option_ric is stored in Basisform (no leading /, no ^-suffix).
option_ric_hist is computed via build_option_ric_hist() and stored on write.
first_seen_date = snapshot_date of first occurrence.
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

logger = setup_logging('fetch_option_chains_index')


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
    """True if both CALL and PUT options first seen on snapshot_date exist."""
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


def _fetch(index_ric: str, option_chain_ric: str,
           snapshot_date: str) -> pd.DataFrame | None:
    chain_ric = option_chain_ric if option_chain_ric.startswith('0#') else f'0#{option_chain_ric}'
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

    # Strip leading / from option_ric (Basisform)
    df['option_ric'] = df['option_ric'].str.lstrip('/')

    # Compute option_ric_hist and expiry_type
    df['option_ric_hist'] = df.apply(
        lambda r: build_option_ric_hist(r['option_ric'], r['expiry_date']),
        axis=1
    )
    df['expiry_type'] = df['expiry_date'].apply(
        lambda d: classify_expiry(d, snap, 'index', underlying_ric=index_ric)
    )
    df['underlying_ric']  = index_ric
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

    logger.info('fetch_option_chains_index | snapshot_date=%s | mode=%s',
                snapshot_date, mode)
    run_id = run_start('fetch_option_chains_index', mode, snapshot_date)

    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT index_ric, option_chain_ric "
                 "FROM pub_config.stock_indices "
                 "WHERE fetch_index_option_chain = TRUE "
                 "  AND active = TRUE "
                 "  AND option_chain_ric IS NOT NULL")
        ).fetchall()

    logger.info('%d index/es with option_chain_ric', len(rows))

    total_written = 0
    try:
        for index_ric, option_chain_ric in rows:
            with engine.connect() as conn:
                if _has_calls_and_puts(conn, index_ric, snapshot_date):
                    logger.info('SKIP %s – calls+puts already complete for %s',
                                index_ric, snapshot_date)
                    continue

            display_chain = (option_chain_ric if option_chain_ric.startswith('0#')
                             else f'0#{option_chain_ric}')
            logger.info('Processing %s (chain: %s)...', index_ric, display_chain)
            df = _fetch(index_ric, option_chain_ric, snapshot_date)

            if df is not None and not df.empty:
                with engine.connect() as conn:
                    existing = _get_existing_rics(conn, index_ric)
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
                                index_ric, len(df), len(existing))
                else:
                    logger.info('SKIP %s – all rows already in DB', index_ric)
            else:
                logger.warning('WARN %s – nothing to write', index_ric)

            time.sleep(SLEEP_TIME)

        run_finish(run_id, rows_written=total_written)
        logger.info('fetch_option_chains_index done. rows_written=%d', total_written)
    except Exception as e:
        run_finish(run_id, error=str(e))
        raise


if __name__ == '__main__':
    run()
