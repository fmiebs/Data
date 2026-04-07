"""
fetch_constituents.py
---------------------
Fetches index constituents for all indices in pub_config.stock_indices
where fetch_constituents = TRUE. Writes to pub_equity.index_constituents.

Idempotent: existing (index_ric, snapshot_date) pairs are skipped.
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
from modules.utils import eikon_fetch

SCHEMA     = 'pub_equity'
TABLE      = 'index_constituents'
SLEEP_TIME = 0.5

logger = setup_logging('fetch_constituents')


def _get_snapshot_date() -> str:
    today = datetime.now()
    return today.replace(day=1).strftime('%Y-%m-%d')


def _check_exists(conn, index_ric: str, snapshot_date: str) -> bool:
    count = conn.execute(
        text(f"SELECT COUNT(*) FROM {SCHEMA}.{TABLE} "
             "WHERE index_ric = :ric AND snapshot_date = :date"),
        {'ric': index_ric, 'date': snapshot_date}
    ).scalar()
    return count > 0


def _fetch(index_ric: str, snapshot_date: str) -> pd.DataFrame | None:
    result = eikon_fetch(
        ek.get_data,
        f'0#{index_ric}',
        ['TR.CommonName', 'TR.TickerSymbol'],
        {'SDate': snapshot_date},
    )
    if result is None:
        return None
    data, _ = result

    if data is None or data.empty:
        logger.warning('No data returned for %s', index_ric)
        return None

    df = data.rename(columns={
        'Instrument':          'constituent_ric',
        'Company Common Name': 'company_name',
        'Ticker Symbol':       'symbol',
    })
    df['index_ric']     = index_ric
    df['snapshot_date'] = snapshot_date

    valid_cols = ['index_ric', 'constituent_ric', 'symbol', 'company_name', 'snapshot_date']
    return df[valid_cols].dropna(subset=['constituent_ric'])


def run(
    snapshot_date: str | None = None,
    mode: str = 'live',
    start_date: str | None = None,  # noqa: ARG001 – not used for snapshot modules
    end_date: str | None = None,    # noqa: ARG001
) -> None:
    if snapshot_date is None:
        snapshot_date = _get_snapshot_date()

    logger.info('fetch_constituents | snapshot_date=%s | mode=%s', snapshot_date, mode)
    run_id = run_start('fetch_constituents', mode, snapshot_date)

    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT index_ric FROM pub_config.stock_indices "
                 "WHERE fetch_constituents = TRUE AND active = TRUE")
        ).fetchall()

    index_rics = [r[0] for r in rows]
    logger.info('%d index/es found: %s', len(index_rics), index_rics)

    total_written = 0
    try:
        for ric in index_rics:
            with engine.connect() as conn:
                if _check_exists(conn, ric, snapshot_date):
                    logger.info('SKIP %s – already in DB', ric)
                    continue

            logger.info('Processing %s ...', ric)
            df = _fetch(ric, snapshot_date)

            if df is not None and not df.empty:
                df.to_sql(TABLE, engine, schema=SCHEMA, if_exists='append', index=False)
                total_written += len(df)
                logger.info('OK %s – %d rows written', ric, len(df))
            else:
                logger.warning('WARN %s – nothing to write', ric)

            time.sleep(SLEEP_TIME)

        run_finish(run_id, rows_written=total_written)
        logger.info('fetch_constituents done. rows_written=%d', total_written)
    except Exception as e:
        run_finish(run_id, error=str(e))
        raise


if __name__ == '__main__':
    run()
