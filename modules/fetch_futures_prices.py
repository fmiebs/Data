"""
fetch_futures_prices.py
-----------------------
Fetches daily bid/ask/settle/volume/open-interest for all futures contracts
in pub_futures.futures_chains and writes them to pub_futures.futures_prices.

Delta logic per futures_ric:
  - No existing rows  → start_date = today - 1 year
  - Existing rows     → start_date = MAX(price_date) + 1 day
  - start_date > today → skip
"""
import time
from collections import defaultdict
from datetime import date, timedelta

import eikon as ek
import pandas as pd
from sqlalchemy import text, Table, MetaData
from sqlalchemy.dialects.postgresql import insert as pg_insert

from config.db import engine
from config import eikon_init  # noqa: F401

SCHEMA     = 'pub_futures'
TABLE      = 'futures_prices'
SLEEP_TIME = 0.5
BATCH_SIZE = 50

_prices_table = None


def _get_prices_table():
    global _prices_table
    if _prices_table is None:
        _prices_table = Table(TABLE, MetaData(), schema=SCHEMA, autoload_with=engine)
    return _prices_table


def _get_start_date(conn, futures_ric: str) -> date | None:
    row = conn.execute(
        text(f"SELECT MAX(price_date) FROM {SCHEMA}.{TABLE} "
             "WHERE futures_ric = :ric"),
        {"ric": futures_ric}
    ).scalar()

    today = date.today()
    if row is None:
        start = today.replace(year=today.year - 1)
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
            ['TR.BIDPRICE.Date', 'TR.BIDPRICE', 'TR.ASKPRICE',
             'TR.SettlePrice', 'TR.Volume', 'TR.OpenInterest'],
            {'SDate': start_date.strftime('%Y-%m-%d'),
             'EDate': end_date.strftime('%Y-%m-%d')}
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

    date_col = next(
        (c for c in data.columns
         if c is not None and c.lower() in ('date', 'dates',
                                            'tr.bidprice date', 'tr.bidprice.date')),
        None
    )
    if date_col is None:
        print(f"  WARN: no date column (cols: {list(data.columns)}) – skipping")
        return None

    col_map = {
        'Instrument':    'futures_ric',
        date_col:        'price_date',
        'Bid Price':     'price_bid',
        'Ask Price':     'price_ask',
        'Settlement Price': 'price_settle',
        'Volume':        'volume',
        'Open Interest': 'open_interest',
    }
    df = data.rename(columns=col_map)

    if 'futures_ric' not in df.columns:
        print(f"  WARN: 'Instrument' absent (cols: {list(data.columns)}) – skipping")
        return None

    df['price_date'] = pd.to_datetime(df['price_date']).dt.date
    df = df.dropna(subset=['price_date'])

    for col in ('price_bid', 'price_ask', 'price_settle'):
        if col not in df.columns:
            df[col] = None
        else:
            df[col] = df[col].where(df[col].notna(), other=None)

    for col in ('volume', 'open_interest'):
        if col not in df.columns:
            df[col] = None
        else:
            df[col] = df[col].where(df[col].notna(), other=None)

    valid_cols = ['futures_ric', 'price_date', 'price_bid', 'price_ask',
                  'price_settle', 'volume', 'open_interest']
    return df[valid_cols]


def run(snapshot_date: str | None = None) -> None:
    end_date = date.today()

    print(f"fetch_futures_prices | end_date={end_date}")

    # Universe: all distinct futures_ric from the latest snapshot per underlying
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT DISTINCT fc.futures_ric
                FROM pub_futures.futures_chains fc
                JOIN pub_futures.futures_overview fo
                  ON fo.underlying_ric = fc.underlying_ric
                WHERE fo.active = TRUE
                  AND fc.snapshot_date = (
                      SELECT MAX(snapshot_date)
                      FROM pub_futures.futures_chains fc2
                      WHERE fc2.underlying_ric = fc.underlying_ric
                  )
            """)
        ).fetchall()

    all_rics = [r[0] for r in rows]
    print(f"  {len(all_rics)} futures RICs in scope")

    if not all_rics:
        print("  Nothing to fetch – run fetch_futures_chains first.")
        return

    # Delta logic: group by start_date for efficient batching
    groups: dict[date, list[str]] = defaultdict(list)
    with engine.connect() as conn:
        for ric in all_rics:
            start = _get_start_date(conn, ric)
            if start is not None:
                groups[start].append(ric)

    print(f"  {sum(len(v) for v in groups.values())} RICs to fetch "
          f"across {len(groups)} start-date group(s)")

    tbl = _get_prices_table()
    total_written = 0

    for start_date, rics in sorted(groups.items()):
        print(f"  Group start_date={start_date} | {len(rics)} RICs")
        for i in range(0, len(rics), BATCH_SIZE):
            batch = rics[i:i + BATCH_SIZE]
            df = _fetch_batch(batch, start_date, end_date)

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
                print(f"    batch {i // BATCH_SIZE + 1}: {len(df)} rows written")
            else:
                print(f"    batch {i // BATCH_SIZE + 1}: no data")

            time.sleep(SLEEP_TIME)

    print(f"fetch_futures_prices done. Total rows written: {total_written}")


if __name__ == '__main__':
    run()
