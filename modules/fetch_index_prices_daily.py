"""
fetch_index_prices_daily.py
---------------------------
Fetches daily OHLC prices (local currency) for all indices in
pub_config.stock_indices. Writes to pub_equity.index_prices_daily.

Delta logic per index_ric:
  - No existing rows  -> start_date = today - HISTORY_YEARS
  - Existing rows     -> start_date = MAX(trade_date) + 1 day
  - start_date > today -> skip
"""
import time
from datetime import date, timedelta

import eikon as ek
import pandas as pd
from sqlalchemy import text, Table, MetaData
from sqlalchemy.dialects.postgresql import insert as pg_insert

from config.db import engine
from config import eikon_init  # noqa: F401
from modules.utils import eikon_fetch

SCHEMA        = 'pub_equity'
TABLE         = 'index_prices_daily'
SLEEP_TIME    = 0.5
HISTORY_YEARS = 30

_table = None


def _get_table():
    global _table
    if _table is None:
        _table = Table(TABLE, MetaData(), schema=SCHEMA, autoload_with=engine)
    return _table


def _get_start_date(conn, index_ric: str) -> date | None:
    row = conn.execute(
        text(f"SELECT MAX(trade_date) FROM {SCHEMA}.{TABLE} WHERE index_ric = :ric"),
        {'ric': index_ric}
    ).scalar()
    today = date.today()
    start = (today.replace(year=today.year - HISTORY_YEARS) if row is None
             else row + timedelta(days=1))
    return None if start > today else start


def _fetch(rics: list[str], start_date: date, end_date: date) -> pd.DataFrame | None:
    params = {
        'SDate': start_date.strftime('%Y-%m-%d'),
        'EDate': end_date.strftime('%Y-%m-%d'),
        'Frq':   'D',
    }
    result = eikon_fetch(
        ek.get_data,
        rics,
        ['TR.PriceClose.Date', 'TR.PriceClose', 'TR.PriceOpen',
         'TR.PriceHigh', 'TR.PriceLow'],
        params,
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
        print(f'  WARN: no date column (cols: {list(data.columns)}) – skipping')
        return None

    df = data.rename(columns={
        'Instrument': 'index_ric',
        date_col:     'trade_date',
        'Price Close': 'close_price',
        'Price Open':  'open_price',
        'Price High':  'high_price',
        'Price Low':   'low_price',
    })

    if 'index_ric' not in df.columns:
        return None

    df['trade_date'] = pd.to_datetime(df['trade_date']).dt.date
    df = df.dropna(subset=['trade_date'])

    for col in ('close_price', 'open_price', 'high_price', 'low_price'):
        if col not in df.columns:
            df[col] = None
        else:
            df[col] = df[col].where(df[col].notna(), other=None)

    df['crncy'] = 'LOC'
    return df[['index_ric', 'trade_date', 'close_price', 'open_price',
               'high_price', 'low_price', 'crncy']]


def run(snapshot_date: str | None = None) -> None:  # noqa: ARG001
    end_date = date.today()
    print(f'fetch_index_prices_daily | end_date={end_date} | history={HISTORY_YEARS}y')

    with engine.connect() as conn:
        all_rics = [
            r[0] for r in conn.execute(
                text('SELECT index_ric FROM pub_config.stock_indices WHERE active = TRUE AND fetch_index_prices = TRUE')
            ).fetchall()
        ]
    print(f'  {len(all_rics)} index RICs: {all_rics}')

    ric_starts: dict[str, date] = {}
    with engine.connect() as conn:
        for ric in all_rics:
            start = _get_start_date(conn, ric)
            if start is not None:
                ric_starts[ric] = start

    rics_to_fetch = list(ric_starts.keys())
    print(f'  {len(rics_to_fetch)} RICs to fetch')

    tbl = _get_table()
    total_written = 0

    for ric in rics_to_fetch:
        df = _fetch([ric], ric_starts[ric], end_date)
        time.sleep(SLEEP_TIME)

        if df is None or df.empty:
            print(f'  WARN {ric} – no data')
            continue

        df = df.drop_duplicates(subset=['index_ric', 'trade_date'])
        records = df.to_dict(orient='records')

        stmt = pg_insert(tbl).values(records).on_conflict_do_update(
            index_elements=['index_ric', 'trade_date'],
            set_={
                'close_price': pg_insert(tbl).excluded.close_price,
                'open_price':  pg_insert(tbl).excluded.open_price,
                'high_price':  pg_insert(tbl).excluded.high_price,
                'low_price':   pg_insert(tbl).excluded.low_price,
                'crncy':       pg_insert(tbl).excluded.crncy,
            }
        )
        with engine.begin() as conn:
            conn.execute(stmt)
        total_written += len(df)
        print(f'  OK {ric} – {len(df)} rows written')

    print(f'fetch_index_prices_daily done. Total rows written: {total_written}')


if __name__ == '__main__':
    run()
