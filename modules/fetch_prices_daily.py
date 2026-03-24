"""
fetch_prices_daily.py
---------------------
Fetches daily OHLCV prices for all SPX constituents from Refinitiv Eikon
and writes them to pub_equity.prices_daily.

Note: shares_outstanding is a quarterly fundamental — see fetch_fundamentals_quarterly.py.

Delta logic per RIC:
  - No existing rows  → start_date = today - 10 years
  - Existing rows     → start_date = MAX(trade_date) + 1 day
  - start_date > today → skip (already up to date)
"""
import time
from datetime import date, timedelta

import eikon as ek
import pandas as pd
from sqlalchemy import text, Table, MetaData
from sqlalchemy.dialects.postgresql import insert as pg_insert

from config.db import engine
from config import eikon_init  # noqa: F401

SCHEMA = 'pub_equity'
TABLE = 'prices_daily'
SLEEP_TIME = 0.5
BATCH_SIZE = 10          # small batches: each RIC has up to 10 years of daily rows
HISTORY_YEARS = 10

_prices_table = None


def _get_prices_table():
    global _prices_table
    if _prices_table is None:
        _prices_table = Table(TABLE, MetaData(), schema=SCHEMA, autoload_with=engine)
    return _prices_table


def _get_start_date(conn, ric: str) -> date | None:
    row = conn.execute(
        text(f"SELECT MAX(trade_date) FROM {SCHEMA}.{TABLE} WHERE ric = :ric"),
        {"ric": ric}
    ).scalar()

    today = date.today()
    if row is None:
        start = today.replace(year=today.year - HISTORY_YEARS)
    else:
        start = row + timedelta(days=1)

    if start > today:
        return None
    return start


def _fetch_batch(rics: list[str], start_date: date, end_date: date,
                 _retries: int = 0) -> pd.DataFrame | None:
    try:
        data, err = ek.get_data(
            rics,
            [
                'TR.PriceClose.Date',
                'TR.PriceClose',
                'TR.PriceOpen',
                'TR.PriceHigh',
                'TR.PriceLow',
                'TR.Volume',
            ],
            {
                'SDate': start_date.strftime('%Y-%m-%d'),
                'EDate': end_date.strftime('%Y-%m-%d'),
                'Frq':   'D',
            }
        )
    except Exception as e:
        if '429' in str(e) or 'limit' in str(e).lower():
            if _retries >= 5:
                print(f"  ERROR batch {rics[0]} – max retries, skipping")
                return None
            print(f"  Rate limit – waiting 63s (attempt {_retries + 1}/5)...")
            time.sleep(63)
            return _fetch_batch(rics, start_date, end_date, _retries + 1)
        print(f"  ERROR batch {rics[0]}: {e}")
        return None

    if data is None or data.empty:
        return None

    # Normalise date column name
    date_col = next(
        (c for c in data.columns
         if c is not None and c.lower() in ('date', 'dates', 'tr.priceclose date',
                                            'tr.priceclose.date')),
        None
    )
    if date_col is None:
        print(f"  WARN: no date column in batch (cols: {list(data.columns)}) – skipping")
        return None

    col_map = {
        'Instrument':       'ric',
        date_col:           'trade_date',
        'Price Close':      'close_price',
        'Price Open':       'open_price',
        'Price High':       'high_price',
        'Price Low':        'low_price',
        'Volume':           'volume',
    }
    df = data.rename(columns=col_map)

    if 'ric' not in df.columns:
        print(f"  WARN: 'Instrument' absent (cols: {list(data.columns)}) – skipping")
        return None

    df['trade_date'] = pd.to_datetime(df['trade_date']).dt.date
    df = df.dropna(subset=['trade_date'])

    for col in ('close_price', 'open_price', 'high_price', 'low_price', 'volume'):
        if col not in df.columns:
            df[col] = None
        else:
            df[col] = df[col].where(df[col].notna(), other=None)

    valid_cols = ['ric', 'trade_date', 'close_price', 'open_price',
                  'high_price', 'low_price', 'volume']
    return df[valid_cols]


def run(snapshot_date: str | None = None) -> None:  # noqa: ARG001
    end_date = date.today()

    print(f"fetch_prices_daily | end_date={end_date} | history={HISTORY_YEARS}y")

    # Universe: all distinct SPX constituent RICs
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT DISTINCT constituent_ric "
                "FROM pub_equity.index_constituents "
                "WHERE index_ric = '.SPX'"
            )
        ).fetchall()

    all_rics = [r[0] for r in rows]
    print(f"  {len(all_rics)} RICs in universe")

    # Delta logic: determine start_date per RIC
    ric_starts: dict[str, date] = {}
    with engine.connect() as conn:
        for ric in all_rics:
            start = _get_start_date(conn, ric)
            if start is not None:
                ric_starts[ric] = start

    rics_to_fetch = list(ric_starts.keys())
    print(f"  {len(rics_to_fetch)} RICs to fetch (rest up to date)")

    tbl = _get_prices_table()
    total_written = 0

    for i in range(0, len(rics_to_fetch), BATCH_SIZE):
        batch_rics = rics_to_fetch[i:i + BATCH_SIZE]
        # Use earliest start_date in batch
        batch_start = min(ric_starts[r] for r in batch_rics)

        df = _fetch_batch(batch_rics, batch_start, end_date)

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
                }
            )
            with engine.begin() as conn:
                conn.execute(stmt)
            total_written += len(df)
            print(f"    batch {i // BATCH_SIZE + 1}: {len(df)} rows written "
                  f"({batch_rics[0]}…)")
        else:
            print(f"    batch {i // BATCH_SIZE + 1}: no data ({batch_rics[0]}…)")

        time.sleep(SLEEP_TIME)

    print(f"fetch_prices_daily done. Total rows written: {total_written}")


if __name__ == '__main__':
    run()
