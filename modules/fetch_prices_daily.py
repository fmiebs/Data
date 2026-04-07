"""
fetch_prices_daily.py
---------------------
Fetches daily OHLCV prices for all SPX constituents.
Writes to pub_equity.stock_prices_daily.

Delta logic per RIC:
  - No existing rows  -> start_date = today - HISTORY_YEARS
  - Existing rows     -> start_date = MAX(trade_date) + 1 day
  - start_date > today -> skip
"""
import logging
import time
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

SCHEMA        = 'pub_equity'
TABLE         = 'stock_prices_daily'
SLEEP_TIME    = 0.5
BATCH_SIZE    = 10
HISTORY_YEARS = 10

logger = setup_logging('fetch_prices_daily')

_prices_table = None


def _get_prices_table():
    global _prices_table
    if _prices_table is None:
        _prices_table = Table(TABLE, MetaData(), schema=SCHEMA, autoload_with=engine)
    return _prices_table


def _get_start_date(conn, ric: str, override_start: date | None = None) -> date | None:
    if override_start is not None:
        return override_start
    row = conn.execute(
        text(f"SELECT MAX(trade_date) FROM {SCHEMA}.{TABLE} WHERE ric = :ric"),
        {'ric': ric}
    ).scalar()
    today = date.today()
    start = (today.replace(year=today.year - HISTORY_YEARS) if row is None
             else row + timedelta(days=1))
    return None if start > today else start


def _fetch_batch(rics: list[str], start_date: date, end_date: date) -> pd.DataFrame | None:
    result = eikon_fetch(
        ek.get_data,
        rics,
        ['TR.PriceClose.Date', 'TR.PriceClose', 'TR.PriceOpen',
         'TR.PriceHigh', 'TR.PriceLow', 'TR.Volume'],
        {'SDate': start_date.strftime('%Y-%m-%d'),
         'EDate': end_date.strftime('%Y-%m-%d'),
         'Frq':   'D'},
    )
    if result is None:
        return None
    data, _ = result

    if data is None or data.empty:
        return None

    date_col = next(
        (c for c in data.columns
         if c is not None and c.lower() in ('date', 'dates',
                                            'tr.priceclose date', 'tr.priceclose.date')),
        None
    )
    if date_col is None:
        logger.warning('No date column in batch (cols: %s)', list(data.columns))
        return None

    df = data.rename(columns={
        'Instrument':  'ric',
        date_col:      'trade_date',
        'Price Close': 'close_price',
        'Price Open':  'open_price',
        'Price High':  'high_price',
        'Price Low':   'low_price',
        'Volume':      'volume',
    })

    if 'ric' not in df.columns:
        logger.warning("'Instrument' absent (cols: %s)", list(data.columns))
        return None

    df['trade_date'] = pd.to_datetime(df['trade_date']).dt.date
    df = df.dropna(subset=['trade_date'])

    for col in ('close_price', 'open_price', 'high_price', 'low_price', 'volume'):
        if col not in df.columns:
            df[col] = None
        else:
            df[col] = df[col].where(df[col].notna(), other=None)

    df['crncy'] = 'LOC'
    return df[['ric', 'trade_date', 'close_price', 'open_price',
               'high_price', 'low_price', 'volume', 'crncy']]


def run(
    snapshot_date: str | None = None,  # noqa: ARG001
    mode: str = 'live',
    start_date: str | None = None,
    end_date: str | None = None,
) -> None:
    end = date.fromisoformat(end_date) if end_date else date.today()
    override = date.fromisoformat(start_date) if (mode == 'backfill' and start_date) else None

    logger.info('fetch_prices_daily | mode=%s | end_date=%s', mode, end)
    run_id = run_start('fetch_prices_daily', mode, snapshot_date)

    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT DISTINCT constituent_ric "
                 "FROM pub_equity.index_constituents WHERE index_ric = '.SPX'")
        ).fetchall()

    all_rics = [r[0] for r in rows]
    logger.info('%d RICs in universe', len(all_rics))

    ric_starts: dict[str, date] = {}
    with engine.connect() as conn:
        for ric in all_rics:
            s = _get_start_date(conn, ric, override)
            if s is not None:
                ric_starts[ric] = s

    rics_to_fetch = list(ric_starts.keys())
    logger.info('%d RICs to fetch', len(rics_to_fetch))

    tbl           = _get_prices_table()
    total_written = 0

    try:
        for i in range(0, len(rics_to_fetch), BATCH_SIZE):
            batch_rics  = rics_to_fetch[i:i + BATCH_SIZE]
            batch_start = min(ric_starts[r] for r in batch_rics)

            df = _fetch_batch(batch_rics, batch_start, end)

            if df is not None and not df.empty:
                df = df.drop_duplicates(subset=['ric', 'trade_date'])
                records = df.to_dict(orient='records')
                stmt = pg_insert(tbl).values(records).on_conflict_do_update(
                    index_elements=['ric', 'trade_date'],
                    set_={
                        'close_price': pg_insert(tbl).excluded.close_price,
                        'open_price':  pg_insert(tbl).excluded.open_price,
                        'high_price':  pg_insert(tbl).excluded.high_price,
                        'low_price':   pg_insert(tbl).excluded.low_price,
                        'volume':      pg_insert(tbl).excluded.volume,
                        'crncy':       pg_insert(tbl).excluded.crncy,
                    }
                )
                with engine.begin() as conn:
                    conn.execute(stmt)
                total_written += len(df)
                logger.info('batch %d: %d rows written (%s...)',
                            i // BATCH_SIZE + 1, len(df), batch_rics[0])
            else:
                logger.info('batch %d: no data (%s...)',
                            i // BATCH_SIZE + 1, batch_rics[0])

            time.sleep(SLEEP_TIME)

        run_finish(run_id, rows_written=total_written)
        logger.info('fetch_prices_daily done. rows_written=%d', total_written)
    except Exception as e:
        run_finish(run_id, error=str(e))
        raise


if __name__ == '__main__':
    run()
