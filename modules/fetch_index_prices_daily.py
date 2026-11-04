"""
fetch_index_prices_daily.py
---------------------------
Fetches daily OHLC prices (local currency) and market cap for all indices in
pub_config.stock_indices WHERE fetch_index_prices=TRUE.

Writes to:
  - pub_equity.index_prices_daily  (OHLC)
  - pub_equity.index_mcap          (market cap in local currency)

Delta logic per index_ric (separate per table):
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

SCHEMA_EQUITY = 'pub_equity'
TABLE_PRICES  = 'index_prices_daily'
TABLE_MCAP    = 'index_mcap'
SLEEP_TIME    = 0.5
HISTORY_YEARS = 30

_tbl_prices = None
_tbl_mcap   = None


def _get_tbl_prices():
    global _tbl_prices
    if _tbl_prices is None:
        _tbl_prices = Table(TABLE_PRICES, MetaData(), schema=SCHEMA_EQUITY, autoload_with=engine)
    return _tbl_prices


def _get_tbl_mcap():
    global _tbl_mcap
    if _tbl_mcap is None:
        _tbl_mcap = Table(TABLE_MCAP, MetaData(), schema=SCHEMA_EQUITY, autoload_with=engine)
    return _tbl_mcap


def _get_start_date(table: str, id_col: str, index_ric: str) -> date | None:
    with engine.connect() as conn:
        row = conn.execute(
            text(f"SELECT MAX(trade_date) FROM {SCHEMA_EQUITY}.{table} WHERE {id_col} = :ric"),
            {'ric': index_ric}
        ).scalar()
    today = date.today()
    start = (today.replace(year=today.year - HISTORY_YEARS) if row is None
             else row + timedelta(days=1))
    return None if start > today else start


def _fetch_prices(rics: list[str], start_date: date, end_date: date) -> pd.DataFrame | None:
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
        'Instrument':  'index_ric',
        date_col:      'trade_date',
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


def _fetch_mcap(ric: str, start_date: date, end_date: date) -> pd.DataFrame | None:
    params = {
        'SDate': start_date.strftime('%Y-%m-%d'),
        'EDate': end_date.strftime('%Y-%m-%d'),
        'Frq':   'D',
    }
    result = eikon_fetch(
        ek.get_data,
        [ric],
        ['TR.IndexMarketCap.Date', 'TR.IndexMarketCap'],
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
                                            'tr.indexmarketcap date', 'tr.indexmarketcap.date',
                                            'tr.indexmarketcap date', 'index market cap date')),
        None
    )
    mcap_col = next(
        (c for c in data.columns
         if c is not None and (
             'market cap' in c.lower()
             or c.lower() in ('tr.indexmarketcap', 'index market cap')
         )),
        None
    )
    if date_col is None or mcap_col is None:
        print(f'  WARN {ric}: unexpected mcap columns {list(data.columns)} – skipping')
        return None

    df = data.rename(columns={date_col: 'trade_date', mcap_col: 'mcap_local'})
    df['trade_date'] = pd.to_datetime(df['trade_date']).dt.date
    df = df.dropna(subset=['trade_date', 'mcap_local'])
    df['mcap_local'] = df['mcap_local'].where(df['mcap_local'].notna(), other=None)
    df['index_ric']  = ric
    return df[['index_ric', 'trade_date', 'mcap_local']]


def run(
    snapshot_date: str | None = None,  # noqa: ARG001
    mode: str = 'live',
    start_date: str | None = None,
    end_date: str | None = None,
) -> None:
    end_dt = date.fromisoformat(end_date) if end_date else date.today()
    print(f'fetch_index_prices_daily | mode={mode} | end_date={end_dt} | history={HISTORY_YEARS}y')

    with engine.connect() as conn:
        rows = conn.execute(text(
            'SELECT index_ric, currency FROM pub_config.stock_indices '
            'WHERE active = TRUE AND fetch_index_prices = TRUE'
        )).fetchall()
    all_rics     = [r[0] for r in rows]
    crncy_map    = {r[0]: r[1] for r in rows}
    print(f'  {len(all_rics)} index RICs: {all_rics}')

    tbl_prices = _get_tbl_prices()
    tbl_mcap   = _get_tbl_mcap()
    total_prices_written = 0
    total_mcap_written   = 0

    for ric in all_rics:
        # ---- Prices ----
        if mode == 'backfill' and start_date:
            price_start = date.fromisoformat(start_date)
        else:
            price_start = _get_start_date(TABLE_PRICES, 'index_ric', ric)

        if price_start is not None and price_start <= end_dt:
            df_prices = _fetch_prices([ric], price_start, end_dt)
            time.sleep(SLEEP_TIME)

            if df_prices is None or df_prices.empty:
                print(f'  WARN {ric} prices – no data')
            else:
                df_prices = df_prices.drop_duplicates(subset=['index_ric', 'trade_date'])
                stmt = pg_insert(tbl_prices).values(df_prices.to_dict(orient='records')).on_conflict_do_update(
                    index_elements=['index_ric', 'trade_date'],
                    set_={
                        'close_price': pg_insert(tbl_prices).excluded.close_price,
                        'open_price':  pg_insert(tbl_prices).excluded.open_price,
                        'high_price':  pg_insert(tbl_prices).excluded.high_price,
                        'low_price':   pg_insert(tbl_prices).excluded.low_price,
                        'crncy':       pg_insert(tbl_prices).excluded.crncy,
                    }
                )
                with engine.begin() as conn:
                    conn.execute(stmt)
                total_prices_written += len(df_prices)
                print(f'  OK {ric} prices – {len(df_prices)} rows')
        else:
            print(f'  {ric} prices already up to date – skip')

        # ---- Market Cap ----
        if mode == 'backfill' and start_date:
            mcap_start = date.fromisoformat(start_date)
        else:
            mcap_start = _get_start_date(TABLE_MCAP, 'index_ric', ric)

        if mcap_start is not None and mcap_start <= end_dt:
            df_mcap = _fetch_mcap(ric, mcap_start, end_dt)
            time.sleep(SLEEP_TIME)

            if df_mcap is None or df_mcap.empty:
                print(f'  WARN {ric} mcap – no data')
            else:
                df_mcap = df_mcap.drop_duplicates(subset=['index_ric', 'trade_date'])
                df_mcap['crncy'] = crncy_map.get(ric, 'LOC') or 'LOC'
                stmt = pg_insert(tbl_mcap).values(df_mcap.to_dict(orient='records')).on_conflict_do_update(
                    index_elements=['index_ric', 'trade_date'],
                    set_={
                        'mcap_local': pg_insert(tbl_mcap).excluded.mcap_local,
                        'crncy':      pg_insert(tbl_mcap).excluded.crncy,
                    }
                )
                with engine.begin() as conn:
                    conn.execute(stmt)
                total_mcap_written += len(df_mcap)
                print(f'  OK {ric} mcap – {len(df_mcap)} rows')
        else:
            print(f'  {ric} mcap already up to date – skip')

    print(f'fetch_index_prices_daily done. '
          f'prices={total_prices_written} rows, mcap={total_mcap_written} rows')


if __name__ == '__main__':
    run()
