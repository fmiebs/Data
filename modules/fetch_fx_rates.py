"""
fetch_fx_rates.py
-----------------
Fetches daily FX spot rates for currency pairs needed to convert index market caps to USD.

Source : Eikon ek.get_data(), field TR.PriceClose on FX spot RICs (e.g. EUR=, CHF=).
Config : pub_config.fx_pairs (active=TRUE), filtered to currencies in use by
         pub_config.stock_indices WHERE fetch_index_prices=TRUE AND currency != 'USD'.

Inversion: fx_pairs.invert=TRUE  ->  spot_rate stored as 1/raw_rate
           (converts FCY-per-USD quotes to USD-per-FCY for uniform downstream use).
Delta logic per (from_crncy, to_crncy): MAX(trade_date)+1 or today - HISTORY_YEARS.
"""
import logging
import time
from datetime import date, timedelta

import eikon as ek
import pandas as pd
from sqlalchemy import text, Table, MetaData
from sqlalchemy.dialects.postgresql import insert as pg_insert

from config.db import engine
from config import eikon_init  # noqa: F401
from config.logging_setup import setup_logging
from config.pipeline import run_start, run_finish
from modules.utils import eikon_fetch

logger = setup_logging('fetch_fx_rates')

SCHEMA        = 'pub_rates'
TABLE         = 'fx_rates'
SLEEP_TIME    = 0.5
HISTORY_YEARS = 30

_table = None


def _get_table():
    global _table
    if _table is None:
        _table = Table(TABLE, MetaData(), schema=SCHEMA, autoload_with=engine)
    return _table


def _load_pairs() -> list[dict]:
    """FX-Paare die fuer aktive Indizes mit fetch_index_prices=TRUE benoetigt werden."""
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT fp.from_crncy, fp.to_crncy, fp.eikon_ric, fp.invert
            FROM pub_config.fx_pairs fp
            WHERE fp.active = TRUE
              AND fp.from_crncy IN (
                  SELECT DISTINCT currency
                  FROM pub_config.stock_indices
                  WHERE active = TRUE
                    AND fetch_index_prices = TRUE
                    AND currency IS NOT NULL
                    AND currency != 'USD'
              )
            ORDER BY fp.from_crncy
        """)).fetchall()
    return [
        {'from_crncy': r[0], 'to_crncy': r[1], 'eikon_ric': r[2], 'invert': r[3]}
        for r in rows
    ]


def _get_start_date(from_crncy: str, to_crncy: str) -> date | None:
    with engine.connect() as conn:
        row = conn.execute(text(
            f"SELECT MAX(trade_date) FROM {SCHEMA}.{TABLE} "
            "WHERE from_crncy = :fc AND to_crncy = :tc"
        ), {'fc': from_crncy, 'tc': to_crncy}).scalar()
    today = date.today()
    start = (today.replace(year=today.year - HISTORY_YEARS) if row is None
             else row + timedelta(days=1))
    return None if start > today else start


def _fetch(eikon_ric: str, start_date: date, end_date: date) -> pd.DataFrame | None:
    # TR.BidPrice / TR.AskPrice sind die einzigen FX-Time-Series-Felder die fuer
    # Spot-RICs (EUR=, CHF= etc.) via ek.get_data() zuverlaessig funktionieren.
    # spot_rate = mid = (bid + ask) / 2
    params = {
        'SDate': start_date.strftime('%Y-%m-%d'),
        'EDate': end_date.strftime('%Y-%m-%d'),
        'Frq':   'D',
    }
    result = eikon_fetch(
        ek.get_data,
        [eikon_ric],
        ['TR.BidPrice.Date', 'TR.BidPrice', 'TR.AskPrice'],
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
                                            'tr.bidprice date', 'tr.bidprice.date')),
        None
    )
    bid_col = next(
        (c for c in data.columns if c is not None and 'bid' in c.lower()), None
    )
    ask_col = next(
        (c for c in data.columns if c is not None and 'ask' in c.lower()), None
    )
    if date_col is None or bid_col is None:
        logger.warning('Unexpected columns for %s: %s', eikon_ric, list(data.columns))
        return None

    df = data.rename(columns={date_col: 'trade_date'})
    df['trade_date'] = pd.to_datetime(df['trade_date']).dt.date
    df['bid'] = pd.to_numeric(df[bid_col], errors='coerce')
    if ask_col:
        df['ask'] = pd.to_numeric(df[ask_col], errors='coerce')
        df['spot_rate'] = (df['bid'] + df['ask']) / 2.0
    else:
        df['spot_rate'] = df['bid']
    df = df.dropna(subset=['trade_date', 'spot_rate'])
    df = df[df['spot_rate'] > 0]
    df = df.drop_duplicates(subset=['trade_date'])
    return df[['trade_date', 'spot_rate']]


def run(
    snapshot_date: str | None = None,  # noqa: ARG001
    mode: str = 'live',
    start_date: str | None = None,
    end_date: str | None = None,
) -> None:
    logger.info('fetch_fx_rates | mode=%s', mode)
    run_id = run_start('fetch_fx_rates', mode, snapshot_date)

    pairs = _load_pairs()
    if not pairs:
        logger.info('No active FX pairs needed – done.')
        run_finish(run_id, rows_written=0)
        return
    logger.info('%d FX pairs: %s', len(pairs), [p['from_crncy'] for p in pairs])

    end_dt = date.fromisoformat(end_date) if end_date else date.today()
    tbl = _get_table()
    total_written = 0

    for pair in pairs:
        fc  = pair['from_crncy']
        tc  = pair['to_crncy']
        ric = pair['eikon_ric']
        inv = pair['invert']

        if mode == 'backfill' and start_date:
            start_dt = date.fromisoformat(start_date)
        else:
            start_dt = _get_start_date(fc, tc)

        if start_dt is None or start_dt > end_dt:
            logger.info('  %s/%s already up to date – skip', fc, tc)
            continue

        logger.info('  %s/%s (%s) | %s – %s | invert=%s', fc, tc, ric, start_dt, end_dt, inv)
        df = _fetch(ric, start_dt, end_dt)
        time.sleep(SLEEP_TIME)

        if df is None or df.empty:
            logger.warning('  WARN %s/%s – no data returned', fc, tc)
            continue

        if inv:
            df['spot_rate'] = 1.0 / df['spot_rate']

        df['from_crncy']    = fc
        df['to_crncy']      = tc
        df['source']        = 'EK'
        df['source_system'] = 'Eikon'
        df['spot_rate']     = df['spot_rate'].where(df['spot_rate'].notna(), other=None)

        records = df[['trade_date', 'from_crncy', 'to_crncy',
                      'spot_rate', 'source', 'source_system']].to_dict(orient='records')

        stmt = pg_insert(tbl).values(records).on_conflict_do_update(
            index_elements=['trade_date', 'from_crncy', 'to_crncy'],
            set_={'spot_rate': pg_insert(tbl).excluded.spot_rate}
        )
        with engine.begin() as conn:
            conn.execute(stmt)
        total_written += len(records)
        logger.info('  OK %s/%s – %d rows written', fc, tc, len(records))

    run_finish(run_id, rows_written=total_written)
    logger.info('fetch_fx_rates done. Total rows written: %d', total_written)


if __name__ == '__main__':
    run()
