"""
fetch_fx_forwards.py
--------------------
Fetches daily FX outright forward rates for currency pairs with option data.

Source: Eikon ek.get_data() — spot RIC (e.g. EUR=) + forward-point RICs
        (EUR1M=, EUR2M=, ..., EUR2Y=). These RICs deliver forward SWAP POINTS
        (pips, unit = 0.0001). Outright forward = spot_mid + points_mid / 10_000.

Storage convention: forward_rate stored as 1 FCY = x USD, identical to
        fx_rates.spot_rate. For pairs with invert=TRUE in pub_config.fx_pairs
        (e.g. CHF, where Eikon quotes CHF-per-USD), the raw outright is inverted.

Currencies: all active pairs in pub_config.fx_pairs (used by indices with
        option data). Tenors: 1M 2M 3M 6M 9M 1Y 18M 2Y.

Delta logic: MAX(trade_date) per currency + 1 day, or today - HISTORY_YEARS.
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

logger = setup_logging('fetch_fx_forwards')

SCHEMA        = 'pub_rates'
TABLE         = 'fx_forwards'
SLEEP_TIME    = 0.5
HISTORY_YEARS = 15

# Tenors stored in DB → Eikon RIC suffix
TENORS: dict[str, str] = {
    '1M':  '1M',
    '2M':  '2M',
    '3M':  '3M',
    '6M':  '6M',
    '9M':  '9M',
    '1Y':  '1Y',
    '18M': '18M',
    '2Y':  '2Y',
}

_table = None


def _get_table():
    global _table
    if _table is None:
        _table = Table(TABLE, MetaData(), schema=SCHEMA, autoload_with=engine)
    return _table


def _load_pairs() -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT from_crncy, to_crncy, eikon_ric, invert
            FROM pub_config.fx_pairs
            WHERE active = TRUE
            ORDER BY from_crncy
        """)).fetchall()
    return [
        {'from_crncy': r[0], 'to_crncy': r[1], 'spot_ric': r[2], 'invert': r[3]}
        for r in rows
    ]


def _get_start_date(from_crncy: str) -> date:
    with engine.connect() as conn:
        row = conn.execute(text(
            f"SELECT MAX(trade_date) FROM {SCHEMA}.{TABLE} "
            "WHERE from_crncy = :fc"
        ), {'fc': from_crncy}).scalar()
    today = date.today()
    start = (today.replace(year=today.year - HISTORY_YEARS) if row is None
             else row + timedelta(days=1))
    return start


def _build_rics(spot_ric: str) -> tuple[str, list[tuple[str, str]]]:
    """Returns (spot_ric, [(tenor, fwd_ric), ...])."""
    prefix = spot_ric.rstrip('=')
    fwd_rics = [(tenor, f'{prefix}{suffix}=') for tenor, suffix in TENORS.items()]
    return spot_ric, fwd_rics


def _fetch(spot_ric: str, fwd_rics: list[tuple[str, str]],
           start_date: date, end_date: date) -> pd.DataFrame | None:
    all_rics = [spot_ric] + [r for _, r in fwd_rics]
    params = {
        'SDate': start_date.strftime('%Y-%m-%d'),
        'EDate': end_date.strftime('%Y-%m-%d'),
        'Frq':   'D',
    }
    result = eikon_fetch(
        ek.get_data,
        all_rics,
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
        None,
    )
    bid_col = next((c for c in data.columns if c is not None and 'bid' in c.lower()), None)
    ask_col = next((c for c in data.columns if c is not None and 'ask' in c.lower()), None)

    if date_col is None or bid_col is None:
        logger.warning('Unexpected columns: %s', list(data.columns))
        return None

    data = data.copy()
    data['trade_date'] = pd.to_datetime(data[date_col]).dt.date
    data['bid'] = pd.to_numeric(data[bid_col], errors='coerce')
    data['ask'] = pd.to_numeric(data[ask_col], errors='coerce') if ask_col else data['bid']
    data['mid'] = (data['bid'] + data['ask']) / 2.0
    return data[['Instrument', 'trade_date', 'mid']].dropna()


def _compute_forwards(df: pd.DataFrame, spot_ric: str,
                      fwd_rics: list[tuple[str, str]],
                      invert: bool) -> list[dict]:
    spot_df = df[df['Instrument'] == spot_ric].set_index('trade_date')['mid']
    spot_df = spot_df[~spot_df.index.duplicated(keep='last')]
    if spot_df.empty:
        return []

    records = []
    for tenor, fwd_ric in fwd_rics:
        pts_df = df[df['Instrument'] == fwd_ric].set_index('trade_date')['mid']
        pts_df = pts_df[~pts_df.index.duplicated(keep='last')]
        if pts_df.empty:
            logger.warning('  No data for %s', fwd_ric)
            continue

        # align on common dates
        common = spot_df.index.intersection(pts_df.index)
        if len(common) == 0:
            continue

        for dt in common:
            spot_mid = spot_df[dt]
            pts_mid  = pts_df[dt]
            if pd.isna(spot_mid) or pd.isna(pts_mid):
                continue

            raw_outright = spot_mid + pts_mid / 10_000.0

            if invert:
                if raw_outright <= 0:
                    continue
                forward_rate = 1.0 / raw_outright
            else:
                forward_rate = raw_outright

            if forward_rate <= 0:
                continue

            records.append({
                'from_crncy':    None,   # filled below
                'to_crncy':      'USD',
                'trade_date':    dt,
                'tenor':         tenor,
                'forward_rate':  round(float(forward_rate), 8),
                'source_system': 'Eikon',
            })

    return records


def run(
    snapshot_date: str | None = None,  # noqa: ARG001
    mode: str = 'live',
    start_date: str | None = None,
    end_date: str | None = None,
) -> None:
    logger.info('fetch_fx_forwards | mode=%s', mode)
    run_id = run_start('fetch_fx_forwards', mode, snapshot_date)

    pairs = _load_pairs()
    if not pairs:
        logger.info('No active FX pairs – done.')
        run_finish(run_id, rows_written=0)
        return
    logger.info('%d FX pairs: %s', len(pairs), [p['from_crncy'] for p in pairs])

    end_dt   = date.fromisoformat(end_date) if end_date else date.today()
    tbl      = _get_table()
    total_written = 0

    for pair in pairs:
        fc      = pair['from_crncy']
        tc      = pair['to_crncy']
        inv     = pair['invert']
        spot_ric, fwd_rics = _build_rics(pair['spot_ric'])

        if mode == 'backfill' and start_date:
            start_dt = date.fromisoformat(start_date)
        else:
            start_dt = _get_start_date(fc)

        if start_dt > end_dt:
            logger.info('  %s/%s already up to date – skip', fc, tc)
            continue

        logger.info('  %s/%s | %s – %s | invert=%s | %d tenors',
                    fc, tc, start_dt, end_dt, inv, len(fwd_rics))

        df = _fetch(spot_ric, fwd_rics, start_dt, end_dt)
        time.sleep(SLEEP_TIME)

        if df is None or df.empty:
            logger.warning('  WARN %s/%s – no data returned', fc, tc)
            continue

        records = _compute_forwards(df, spot_ric, fwd_rics, inv)
        for r in records:
            r['from_crncy'] = fc

        if not records:
            logger.warning('  WARN %s/%s – no valid records after computation', fc, tc)
            continue

        stmt = pg_insert(tbl).values(records).on_conflict_do_update(
            index_elements=['from_crncy', 'to_crncy', 'trade_date', 'tenor'],
            set_={'forward_rate': pg_insert(tbl).excluded.forward_rate},
        )
        with engine.begin() as conn:
            conn.execute(stmt)

        n = len(records)
        total_written += n
        logger.info('  OK %s/%s – %d rows written', fc, tc, n)

    run_finish(run_id, rows_written=total_written)
    logger.info('fetch_fx_forwards done. Total rows written: %d', total_written)


if __name__ == '__main__':
    run()
