"""
fetch_futures_prices.py
-----------------------
Fetches daily bid/ask/settle/volume/open-interest for all futures contracts
in pub_futures.futures_chains. Writes to pub_futures.futures_prices.

Delta logic per futures_ric:
  - No existing rows  -> start_date = today - 1 year
  - Existing rows     -> start_date = MAX(price_date) + 1 day
  - start_date > today -> skip
"""
import logging
import time
from collections import defaultdict
from datetime import date, timedelta

import eikon as ek
import pandas as pd
from sqlalchemy import text, Table, MetaData
from sqlalchemy.dialects.postgresql import insert as pg_insert

from config import eikon_init  # noqa: F401
from config.db import engine
from config.logging_setup import setup_logging
from config.pipeline import run_finish, run_start
from modules.utils import eikon_fetch

SCHEMA     = 'pub_futures'
TABLE      = 'futures_prices'
SLEEP_TIME = 0.5
BATCH_SIZE = 50

logger = setup_logging('fetch_futures_prices')

_prices_table = None


def _get_prices_table():
    global _prices_table
    if _prices_table is None:
        _prices_table = Table(TABLE, MetaData(), schema=SCHEMA, autoload_with=engine)
    return _prices_table


def _get_start_date(conn, futures_ric: str,
                    override_start: date | None = None) -> date | None:
    if override_start is not None:
        return override_start
    row = conn.execute(
        text(f"SELECT MAX(price_date) FROM {SCHEMA}.{TABLE} WHERE futures_ric = :ric"),
        {'ric': futures_ric}
    ).scalar()
    today = date.today()
    start = (today.replace(year=today.year - 1) if row is None
             else row + timedelta(days=1))
    return None if start > today else start


def _fetch_batch(rics: list[str], start_date: date, end_date: date) -> pd.DataFrame | None:
    result = eikon_fetch(
        ek.get_data,
        rics,
        ['TR.BIDPRICE.Date', 'TR.BIDPRICE', 'TR.ASKPRICE',
         'TR.SettlePrice', 'TR.Volume', 'TR.OpenInterest'],
        {'SDate': start_date.strftime('%Y-%m-%d'),
         'EDate': end_date.strftime('%Y-%m-%d')},
    )
    if result is None:
        return None
    data, _ = result

    if data is None or data.empty:
        return None

    date_col = next(
        (c for c in data.columns
         if c is not None and c.lower() in ('date', 'dates',
                                            'tr.bidprice date', 'tr.bidprice.date')),
        None
    )
    if date_col is None:
        logger.warning('No date column (cols: %s)', list(data.columns))
        return None

    df = data.rename(columns={
        'Instrument':       'futures_ric',
        date_col:           'price_date',
        'Bid Price':        'price_bid',
        'Ask Price':        'price_ask',
        'Settlement Price': 'price_settle',
        'Volume':           'volume',
        'Open Interest':    'open_interest',
    })

    if 'futures_ric' not in df.columns:
        logger.warning("'Instrument' absent (cols: %s)", list(data.columns))
        return None

    df['price_date'] = pd.to_datetime(df['price_date']).dt.date
    df = df.dropna(subset=['price_date'])

    for col in ('price_bid', 'price_ask', 'price_settle', 'volume', 'open_interest'):
        if col not in df.columns:
            df[col] = None
        else:
            df[col] = df[col].where(df[col].notna(), other=None)

    return df[['futures_ric', 'price_date', 'price_bid', 'price_ask',
               'price_settle', 'volume', 'open_interest']]


def run(
    snapshot_date: str | None = None,  # noqa: ARG001
    mode: str = 'live',
    start_date: str | None = None,
    end_date: str | None = None,
) -> None:
    end      = date.fromisoformat(end_date) if end_date else date.today()
    override = date.fromisoformat(start_date) if (mode == 'backfill' and start_date) else None

    logger.info('fetch_futures_prices | mode=%s | end_date=%s', mode, end)
    run_id = run_start('fetch_futures_prices', mode, snapshot_date)

    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT DISTINCT fc.futures_ric
                FROM pub_futures.futures_chains fc
                JOIN pub_config.futures_underlyings fu
                  ON fu.underlying_ric = fc.underlying_ric
                WHERE fu.active = TRUE
                  AND fc.snapshot_date = (
                      SELECT MAX(snapshot_date)
                      FROM pub_futures.futures_chains fc2
                      WHERE fc2.underlying_ric = fc.underlying_ric
                  )
            """)
        ).fetchall()

    all_rics = [r[0] for r in rows]
    logger.info('%d futures RICs in scope', len(all_rics))

    if not all_rics:
        logger.warning('Nothing to fetch – run fetch_futures_chains first.')
        run_finish(run_id, rows_written=0)
        return

    groups: dict[date, list[str]] = defaultdict(list)
    with engine.connect() as conn:
        for ric in all_rics:
            s = _get_start_date(conn, ric, override)
            if s is not None:
                groups[s].append(ric)

    logger.info('%d RICs to fetch across %d start-date group(s)',
                sum(len(v) for v in groups.values()), len(groups))

    tbl           = _get_prices_table()
    total_written = 0

    try:
        for s_date, rics in sorted(groups.items()):
            logger.info('Group start_date=%s | %d RICs', s_date, len(rics))
            for i in range(0, len(rics), BATCH_SIZE):
                batch = rics[i:i + BATCH_SIZE]
                df    = _fetch_batch(batch, s_date, end)

                if df is not None and not df.empty:
                    df = df.drop_duplicates(subset=['futures_ric', 'price_date'])
                    records = df.to_dict(orient='records')
                    stmt = pg_insert(tbl).values(records).on_conflict_do_update(
                        index_elements=['futures_ric', 'price_date'],
                        set_={
                            'price_bid':     pg_insert(tbl).excluded.price_bid,
                            'price_ask':     pg_insert(tbl).excluded.price_ask,
                            'price_settle':  pg_insert(tbl).excluded.price_settle,
                            'volume':        pg_insert(tbl).excluded.volume,
                            'open_interest': pg_insert(tbl).excluded.open_interest,
                        }
                    )
                    with engine.begin() as conn:
                        conn.execute(stmt)
                    total_written += len(df)
                    logger.info('  batch %d: %d rows written', i // BATCH_SIZE + 1, len(df))
                else:
                    logger.info('  batch %d: no data', i // BATCH_SIZE + 1)

                time.sleep(SLEEP_TIME)

        run_finish(run_id, rows_written=total_written)
        logger.info('fetch_futures_prices done. rows_written=%d', total_written)
    except Exception as e:
        run_finish(run_id, error=str(e))
        raise


if __name__ == '__main__':
    run()
