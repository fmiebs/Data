"""
fetch_usd_rates.py
------------------
Fetches FRED CMT par yields and bootstraps annually compounded discrete zero rates.
Writes both rate types to pub_rates.usd_rates (PostgreSQL).

Series configuration is read from pub_config.rates (active = TRUE).

Rate conventions:
- Par yields: bank discount basis (1m/3m/6m) or semiannual BEY (1y+), stored verbatim.
- Zero rates: annually compounded discrete, bootstrapped from par yields.
  Discount factor: disc(t) = (1 + r_zero/100)^(-t/365)
"""
import logging
import os
from datetime import date, timedelta

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import Table, MetaData, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from config.db import engine
from config.logging_setup import setup_logging
from config.pipeline import run_finish, run_start

load_dotenv(r'C:\Users\Miebs\PycharmProjects\Config\.env')

logger = setup_logging('fetch_usd_rates')

HISTORY_YEARS = 10
SCHEMA        = 'pub_rates'
TABLE         = 'usd_rates'
CHUNK_SIZE    = 5000
SHORT_END     = {'1m', '3m', '6m'}   # bank discount, 360-day year
LONG_END      = {'1y', '2y', '3y', '5y', '7y', '10y', '20y', '30y'}  # semiannual BEY


# ---------------------------------------------------------------------------
# Config from DB
# ---------------------------------------------------------------------------

def _load_rate_config() -> tuple[dict[str, int], dict[str, str]]:
    """
    Liest tenor_days und FRED series_id aus pub_config.rates (active = TRUE).

    Returns:
        (tenor_days, fred_series) — beide dicts keyed by tenor (z.B. '3m', '10y')
    """
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT tenor, tenor_days, series_id "
            "FROM pub_config.rates WHERE active = TRUE ORDER BY tenor_days"
        )).fetchall()
    tenor_days  = {r[0]: r[1] for r in rows}
    fred_series = {r[0]: r[2] for r in rows}
    return tenor_days, fred_series


# ---------------------------------------------------------------------------
# Incremental date range
# ---------------------------------------------------------------------------

def _get_fetch_start() -> date:
    with engine.connect() as conn:
        try:
            row = conn.execute(
                text(f"SELECT MAX(trade_date) FROM {SCHEMA}.{TABLE}")
            ).scalar()
        except Exception:
            row = None
    if row is not None:
        return row + timedelta(days=1)
    today = date.today()
    return today.replace(year=today.year - HISTORY_YEARS)


# ---------------------------------------------------------------------------
# Bootstrap helpers
# ---------------------------------------------------------------------------

def _interp_zero(target_days: int, known: list[tuple[int, float]]) -> float:
    days_arr = [d for d, _ in known]
    rate_arr = [r for _, r in known]
    if target_days <= days_arr[0]:
        return rate_arr[0]
    if target_days >= days_arr[-1]:
        return rate_arr[-1]
    for i in range(len(days_arr) - 1):
        if days_arr[i] <= target_days <= days_arr[i + 1]:
            w = (np.log(target_days) - np.log(days_arr[i])) / (
                np.log(days_arr[i + 1]) - np.log(days_arr[i])
            )
            return (1.0 - w) * rate_arr[i] + w * rate_arr[i + 1]
    return rate_arr[-1]


def _bill_to_zero(d_pct: float, days: int) -> float:
    d     = d_pct / 100.0
    price = 1.0 - d * days / 360.0
    return ((1.0 / price) ** (365.0 / days) - 1.0) * 100.0


def _bond_to_zero(c_pct: float, tenor_days: int,
                  known: list[tuple[int, float]]) -> float | None:
    c        = c_pct / 100.0
    n_periods = round(tenor_days / 365.0 * 2.0)
    sum_d    = 0.0
    for i in range(1, n_periods):
        t_d  = round(i * 0.5 * 365.0)
        r_i  = _interp_zero(t_d, known) / 100.0
        sum_d += (1.0 + r_i) ** (-t_d / 365.0)
    d_T = (1.0 - (c / 2.0) * sum_d) / (1.0 + c / 2.0)
    if d_T <= 0.0:
        logger.warning('Degenerate discount factor d(T)=%.4f for tenor_days=%d',
                       d_T, tenor_days)
        return None
    return (d_T ** (-365.0 / tenor_days) - 1.0) * 100.0


def _bootstrap_zeros(par_row: dict[str, float],
                     tenor_days: dict[str, int]) -> dict[str, float]:
    zeros: dict[str, float]       = {}
    known: list[tuple[int, float]] = []

    for tenor in [t for t in tenor_days if t in SHORT_END]:
        if tenor not in par_row:
            continue
        z = _bill_to_zero(par_row[tenor], tenor_days[tenor])
        zeros[tenor] = z
        known.append((tenor_days[tenor], z))

    for tenor in [t for t in tenor_days if t in LONG_END]:
        if tenor not in par_row or not known:
            continue
        z = _bond_to_zero(par_row[tenor], tenor_days[tenor], known)
        if z is not None:
            zeros[tenor] = z
            known.append((tenor_days[tenor], z))
            known.sort()

    return zeros


def _build_records(trade_date: date, par_row: dict[str, float],
                   tenor_days: dict[str, int]) -> list[dict]:
    records: list[dict] = []
    zeros = _bootstrap_zeros(par_row, tenor_days)

    for tenor, days in tenor_days.items():
        if tenor in par_row:
            comp = 'discount' if tenor in SHORT_END else 'semiannual'
            records.append({
                'trade_date':   trade_date,
                'tenor':        tenor,
                'tenor_days':   days,
                'rate_type':    'par',
                'rate_pct':     round(float(par_row[tenor]), 4),
                'compounding':  comp,
                'source':       'EK',
                'source_system': 'FRED',
            })
        if tenor in zeros:
            records.append({
                'trade_date':   trade_date,
                'tenor':        tenor,
                'tenor_days':   days,
                'rate_type':    'zero',
                'rate_pct':     round(float(zeros[tenor]), 4),
                'compounding':  'annual',
                'source':       'FM',
                'source_system': 'bootstrapped',
            })

    return records


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(
    snapshot_date: str | None = None,  # noqa: ARG001
    mode: str = 'live',
    start_date: str | None = None,
    end_date: str | None = None,
) -> None:
    logger.info('fetch_usd_rates | mode=%s', mode)
    run_id = run_start('fetch_usd_rates', mode, snapshot_date)

    fred_key = os.getenv('FRED_API_KEY')
    if not fred_key:
        err = 'FRED_API_KEY not found in .env'
        run_finish(run_id, error=err)
        raise RuntimeError(err)

    from fredapi import Fred
    fred = Fred(api_key=fred_key)

    tenor_days, fred_series = _load_rate_config()

    if mode == 'backfill' and start_date and end_date:
        fetch_start = date.fromisoformat(start_date)
        fetch_end   = date.fromisoformat(end_date)
    else:
        fetch_start = _get_fetch_start()
        fetch_end   = date.today()

    if fetch_start > fetch_end:
        logger.info('fetch_usd_rates: already up to date.')
        run_finish(run_id, rows_written=0)
        return

    logger.info('range=%s - %s', fetch_start, fetch_end)

    raw_series: dict[str, pd.Series] = {}
    for tenor, fred_id in fred_series.items():
        try:
            s = fred.get_series(
                fred_id,
                observation_start=fetch_start.strftime('%Y-%m-%d'),
                observation_end=fetch_end.strftime('%Y-%m-%d'),
            )
            raw_series[tenor] = s
            logger.info('  %s: %d observations', fred_id, len(s))
        except Exception as exc:
            logger.warning('Could not fetch %s: %s', fred_id, exc)

    if not raw_series:
        err = 'No FRED series fetched'
        run_finish(run_id, error=err)
        logger.error(err)
        return

    par_df = pd.DataFrame(raw_series)
    par_df.index = pd.to_datetime(par_df.index).date
    par_df = par_df.sort_index().dropna(how='all')
    logger.info('%d trading days to process', len(par_df))

    all_records: list[dict] = []
    for trade_date, row in par_df.iterrows():
        par_row = {
            t: float(v)
            for t, v in row.items()
            if t in tenor_days and pd.notna(v)
        }
        all_records.extend(_build_records(trade_date, par_row, tenor_days))

    if not all_records:
        logger.info('No records to write.')
        run_finish(run_id, rows_written=0)
        return

    tbl     = Table(TABLE, MetaData(), schema=SCHEMA, autoload_with=engine)
    written = 0
    for i in range(0, len(all_records), CHUNK_SIZE):
        chunk = all_records[i:i + CHUNK_SIZE]
        stmt  = pg_insert(tbl).values(chunk).on_conflict_do_nothing(
            index_elements=['trade_date', 'tenor', 'rate_type']
        )
        with engine.begin() as conn:
            conn.execute(stmt)
        written += len(chunk)

    run_finish(run_id, rows_written=written)
    logger.info('%d rows written to %s.%s', written, SCHEMA, TABLE)
    logger.info('fetch_usd_rates done.')


if __name__ == '__main__':
    run()
