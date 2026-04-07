"""
fetch_futures_chains.py
-----------------------
Fetches active futures contracts for all underlyings in pub_config.futures_underlyings
where active = TRUE. Writes to pub_futures.futures_chains.

Idempotent: existing (underlying_ric, snapshot_date) pairs are skipped.
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
from modules.utils import classify_futures_expiry, eikon_fetch

SCHEMA     = 'pub_futures'
TABLE      = 'futures_chains'
SLEEP_TIME = 0.5

logger = setup_logging('fetch_futures_chains')


def _get_snapshot_date() -> str:
    today = datetime.now()
    return today.replace(day=1).strftime('%Y-%m-%d')


def _is_present(conn, underlying_ric: str, snapshot_date: str) -> bool:
    count = conn.execute(
        text(f"SELECT COUNT(*) FROM {SCHEMA}.{TABLE} "
             "WHERE underlying_ric = :ric AND snapshot_date = :snap"),
        {'ric': underlying_ric, 'snap': snapshot_date}
    ).scalar()
    return count > 0


def _fetch(underlying_ric: str, futures_chain_ric: str,
           snapshot_date: str) -> pd.DataFrame | None:
    chain  = f'0#{futures_chain_ric}'
    result = eikon_fetch(ek.get_data, chain, ['EXPIR_DATE'])
    if result is None:
        return None
    data, _ = result

    if data is None or data.empty:
        logger.warning('No data for %s', chain)
        return None

    df = data.rename(columns={'Instrument': 'futures_ric', 'Expiry Date': 'expiry_date'})
    if 'expiry_date' not in df.columns and 'EXPIR_DATE' in df.columns:
        df = df.rename(columns={'EXPIR_DATE': 'expiry_date'})

    df = df.dropna(subset=['expiry_date'])
    df['expiry_date']    = pd.to_datetime(df['expiry_date']).dt.date
    df['contract_month'] = df['expiry_date'].apply(lambda d: d.strftime('%Y-%m'))
    df['expiry_type']    = df['expiry_date'].apply(classify_futures_expiry)
    df['underlying_ric'] = underlying_ric
    df['snapshot_date']  = snapshot_date

    return df[['underlying_ric', 'futures_ric', 'snapshot_date',
               'expiry_date', 'contract_month', 'expiry_type']]


def run(
    snapshot_date: str | None = None,
    mode: str = 'live',
    start_date: str | None = None,  # noqa: ARG001
    end_date: str | None = None,    # noqa: ARG001
) -> None:
    if snapshot_date is None:
        snapshot_date = _get_snapshot_date()

    logger.info('fetch_futures_chains | snapshot_date=%s | mode=%s', snapshot_date, mode)
    run_id = run_start('fetch_futures_chains', mode, snapshot_date)

    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT underlying_ric, futures_chain_ric "
                 "FROM pub_config.futures_underlyings "
                 "WHERE active = TRUE AND futures_chain_ric IS NOT NULL")
        ).fetchall()

    logger.info('%d active underlying(s)', len(rows))

    total_written = 0
    try:
        for underlying_ric, futures_chain_ric in rows:
            with engine.connect() as conn:
                if _is_present(conn, underlying_ric, snapshot_date):
                    logger.info('SKIP %s – already in DB', underlying_ric)
                    continue

            logger.info('Processing %s (chain: 0#%s)...', underlying_ric, futures_chain_ric)
            df = _fetch(underlying_ric, futures_chain_ric, snapshot_date)

            if df is not None and not df.empty:
                df.to_sql(TABLE, engine, schema=SCHEMA, if_exists='append', index=False)
                total_written += len(df)
                logger.info('OK %s – %d contracts written', underlying_ric, len(df))
            else:
                logger.warning('WARN %s – nothing to write', underlying_ric)

            time.sleep(SLEEP_TIME)

        run_finish(run_id, rows_written=total_written)
        logger.info('fetch_futures_chains done. rows_written=%d', total_written)
    except Exception as e:
        run_finish(run_id, error=str(e))
        raise


if __name__ == '__main__':
    run()
