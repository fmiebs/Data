"""
fetch_ois_rates.py
------------------
Fetches OIS swap par rates from Eikon and bootstraps annually compounded
zero rates. Writes both to pub_rates.rate_curves.

curve_type = 'ois' for all entries.

Rate conventions:
- Par rates: OIS swap fixed-leg rates, quoted as % p.a.
  * Short end (< 1Y): single payment at maturity, treated as simple rate
    disc(T) = 1 / (1 + r_par * tenor_days / 365)
  * Long end (>= 1Y): annual coupon payments
    Bootstrapped analogously to CMT zeros in fetch_usd_rates.py

Currencies: all active rows in pub_config.ois_curves.
History: HISTORY_YEARS (default 15) or explicit backfill range.

RIC patterns (from live Eikon tests):
  USD: USD{T}OIS=ICAP   (e.g. USD1MOIS=ICAP, USD1YOIS=ICAP)
  EUR: EUREON{T}=        (e.g. EUREON1M=, EUREON1Y=)
  GBP: GBP{T}OIS=ICAP   (e.g. GBP1MOIS=ICAP, GBP1YOIS=ICAP)
  AUD: TBD (active=FALSE until confirmed)
  CHF: TBD (active=FALSE until confirmed)
"""
import logging
from datetime import date, timedelta

import eikon as ek
import numpy as np
import pandas as pd
from sqlalchemy import text, Table, MetaData
from sqlalchemy.dialects.postgresql import insert as pg_insert

from config.db import engine
from config import eikon_init  # noqa: F401
from config.logging_setup import setup_logging
from config.pipeline import run_start, run_finish
from modules.utils import eikon_fetch

logger = setup_logging('fetch_ois_rates')

SCHEMA        = 'pub_rates'
TABLE         = 'rate_curves'
CURVE_TYPE    = 'ois'
HISTORY_YEARS = 15
CHUNK_DAYS    = 365        # Eikon-Anfragen in Jahreschunks
SHORT_END_DAYS = 365       # tenor_days < 365: simple-rate Konvention

_table = None


def _get_table():
    global _table
    if _table is None:
        _table = Table(TABLE, MetaData(), schema=SCHEMA, autoload_with=engine)
    return _table


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _load_config() -> dict[str, list[dict]]:
    """Gibt dict crncy -> [{tenor, tenor_days, eikon_ric}, ...] fuer active=TRUE."""
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT crncy, tenor, tenor_days, eikon_ric
            FROM pub_config.ois_curves
            WHERE active = TRUE
            ORDER BY crncy, tenor_days
        """)).fetchall()
    result: dict[str, list[dict]] = {}
    for r in rows:
        result.setdefault(r[0], []).append({
            'tenor': r[1], 'tenor_days': r[2], 'eikon_ric': r[3]
        })
    return result


def _get_start_date(crncy: str) -> date:
    with engine.connect() as conn:
        row = conn.execute(text(
            f"SELECT MAX(trade_date) FROM {SCHEMA}.{TABLE} "
            "WHERE crncy = :c AND curve_type = 'ois'"
        ), {'c': crncy}).scalar()
    today = date.today()
    start = (today.replace(year=today.year - HISTORY_YEARS) if row is None
             else row + timedelta(days=1))
    return start


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def _fetch_chunk(rics: list[str], start_date: date, end_date: date) -> pd.DataFrame | None:
    params = {
        'SDate': start_date.strftime('%Y-%m-%d'),
        'EDate': end_date.strftime('%Y-%m-%d'),
        'Frq':   'D',
    }
    result = eikon_fetch(
        ek.get_data,
        rics,
        ['TR.BidPrice.Date', 'TR.BidPrice', 'TR.AskPrice'],
        params,
    )
    if result is None:
        return None
    data, _ = result
    if data is None or data.empty:
        return None

    cols = {c: c.upper().replace(' ', '.') if c else 'INSTRUMENT' for c in data.columns}
    data = data.rename(columns=cols)

    date_col = next((c for c in data.columns if 'DATE' in str(c) and 'BID' in str(c)), None) or \
               next((c for c in data.columns if 'DATE' in str(c)), None)
    bid_col  = next((c for c in data.columns if 'BID' in str(c) and 'DATE' not in str(c)), None)
    ask_col  = next((c for c in data.columns if 'ASK' in str(c)), None)
    inst_col = data.columns[0]

    if date_col is None or bid_col is None:
        logger.warning('Unexpected columns: %s', list(data.columns))
        return None

    data['trade_date'] = pd.to_datetime(data[date_col]).dt.date
    data['bid'] = pd.to_numeric(data[bid_col], errors='coerce')
    data['ask'] = pd.to_numeric(data[ask_col], errors='coerce') if ask_col else data['bid']
    data['mid'] = (data['bid'] + data['ask']) / 2.0
    data = data.rename(columns={inst_col: 'ric'})
    data = data[['ric', 'trade_date', 'mid']].dropna()
    data = data[data['mid'] > 0]
    return data


# ---------------------------------------------------------------------------
# Bootstrap
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
                np.log(days_arr[i + 1]) - np.log(days_arr[i]))
            return (1.0 - w) * rate_arr[i] + w * rate_arr[i + 1]
    return rate_arr[-1]


def _ois_to_zero(par_pct: float, tenor_days: int,
                 known: list[tuple[int, float]]) -> float | None:
    par = par_pct / 100.0
    if tenor_days < SHORT_END_DAYS:
        # Einfache Verzinsung (Money-Market-Konvention)
        disc = 1.0 / (1.0 + par * tenor_days / 365.0)
        zero = (disc ** (-365.0 / tenor_days) - 1.0) * 100.0
        return zero
    else:
        if not known:
            return None
        # Jaehrliche Kuponstruktur (analog CMT-Bootstrap)
        n_periods = round(tenor_days / 365.0)
        sum_d = 0.0
        for i in range(1, n_periods):
            t_d = round(i * 365.0)
            r_i = _interp_zero(t_d, known) / 100.0
            sum_d += (1.0 + r_i) ** (-t_d / 365.0)
        d_T = (1.0 - par * sum_d) / (1.0 + par)
        if d_T <= 0.0:
            logger.warning('Degenerate d(T)=%.4f for tenor_days=%d', d_T, tenor_days)
            return None
        zero = (d_T ** (-365.0 / tenor_days) - 1.0) * 100.0
        return zero


def _bootstrap(par_row: dict[str, float],
               tenors: list[dict]) -> dict[str, float]:
    zeros: dict[str, float] = {}
    known: list[tuple[int, float]] = []
    for t in sorted(tenors, key=lambda x: x['tenor_days']):
        tenor = t['tenor']
        days  = t['tenor_days']
        if tenor not in par_row:
            continue
        z = _ois_to_zero(par_row[tenor], days, known)
        if z is not None:
            zeros[tenor] = z
            known.append((days, z))
            known.sort()
    return zeros


def _build_records(trade_date: date, par_row: dict[str, float],
                   tenors: list[dict], crncy: str) -> list[dict]:
    zeros = _bootstrap(par_row, tenors)
    tenor_map = {t['tenor']: t['tenor_days'] for t in tenors}
    records = []

    for tenor, par_pct in par_row.items():
        records.append({
            'crncy':         crncy,
            'curve_type':    CURVE_TYPE,
            'trade_date':    trade_date,
            'tenor':         tenor,
            'tenor_days':    tenor_map.get(tenor),
            'rate_type':     'par',
            'rate_pct':      round(float(par_pct), 6),
            'compounding':   'annual',
            'source_system': 'Eikon/ICAP',
        })

    for tenor, zero_pct in zeros.items():
        records.append({
            'crncy':         crncy,
            'curve_type':    CURVE_TYPE,
            'trade_date':    trade_date,
            'tenor':         tenor,
            'tenor_days':    tenor_map.get(tenor),
            'rate_type':     'zero',
            'rate_pct':      round(float(zero_pct), 6),
            'compounding':   'annual',
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
    logger.info('fetch_ois_rates | mode=%s', mode)
    run_id = run_start('fetch_ois_rates', mode, snapshot_date)

    config = _load_config()
    if not config:
        logger.info('No active OIS curves configured – done.')
        run_finish(run_id, rows_written=0)
        return
    logger.info('Currencies: %s', list(config.keys()))

    end_dt = date.fromisoformat(end_date) if end_date else date.today()
    tbl    = _get_table()
    total_written = 0

    for crncy, tenors in config.items():
        ric_map = {t['eikon_ric']: t['tenor'] for t in tenors}
        all_rics = list(ric_map.keys())

        if mode == 'backfill' and start_date:
            start_dt = date.fromisoformat(start_date)
        else:
            start_dt = _get_start_date(crncy)

        if start_dt > end_dt:
            logger.info('  %s already up to date – skip', crncy)
            continue

        logger.info('  %s | %s – %s | %d tenors', crncy, start_dt, end_dt, len(tenors))

        # Jahreschunks um Eikon-Limits zu vermeiden
        chunk_start = start_dt
        crncy_written = 0

        while chunk_start <= end_dt:
            chunk_end = min(
                date(chunk_start.year + 1, chunk_start.month, chunk_start.day) - timedelta(days=1),
                end_dt
            )

            df = _fetch_chunk(all_rics, chunk_start, chunk_end)
            if df is not None and not df.empty:
                # Pivot: eine Zeile je trade_date, Spalten = tenors
                df['tenor'] = df['ric'].map(ric_map)
                pivot = df.pivot_table(index='trade_date', columns='tenor',
                                       values='mid', aggfunc='last')

                all_records = []
                for trade_date, row in pivot.iterrows():
                    par_row = {col: float(val)
                               for col, val in row.items() if pd.notna(val)}
                    if par_row:
                        all_records.extend(_build_records(trade_date, par_row, tenors, crncy))

                if all_records:
                    stmt = pg_insert(tbl).values(all_records).on_conflict_do_update(
                        index_elements=['crncy', 'curve_type', 'trade_date', 'tenor', 'rate_type'],
                        set_={'rate_pct': pg_insert(tbl).excluded.rate_pct},
                    )
                    with engine.begin() as conn:
                        conn.execute(stmt)
                    crncy_written += len(all_records)

            chunk_start = chunk_end + timedelta(days=1)

        logger.info('  OK %s – %d rows written', crncy, crncy_written)
        total_written += crncy_written

    run_finish(run_id, rows_written=total_written)
    logger.info('fetch_ois_rates done. Total rows written: %d', total_written)


if __name__ == '__main__':
    run()
