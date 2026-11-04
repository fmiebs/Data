"""
fetch_gov_rates.py
------------------
Fetches government benchmark bond yields from Eikon (TR.YIELD field) and
bootstraps annually compounded zero rates. Writes both to pub_rates.rate_curves
with curve_type = 'gov'.

Benchmark conventions by currency:
  EUR: German Bunds (DE{T}T=RR)
  GBP: UK Gilts    (GB{T}T=RR)
  AUD: Commonwealth Government Securities (AU{T}T=RR)
  CHF: Swiss Confederation bonds (CH{T}T=RR)

Field used: TR.YIELD (YTM on a closing-price basis). Unlike FX/OIS where
TR.BidPrice is the rate, for bonds TR.YIELD is the relevant quantity directly.

Note: All RICs in pub_config.gov_curves are currently active=FALSE. Set to
TRUE after verifying RIC names and coverage against live Eikon data.
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

logger = setup_logging('fetch_gov_rates')

SCHEMA        = 'pub_rates'
TABLE         = 'rate_curves'
CURVE_TYPE    = 'gov'
HISTORY_YEARS = 15
SHORT_END_DAYS = 365

_table = None


def _get_table():
    global _table
    if _table is None:
        _table = Table(TABLE, MetaData(), schema=SCHEMA, autoload_with=engine)
    return _table


def _load_config() -> dict[str, list[dict]]:
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT crncy, tenor, tenor_days, eikon_ric
            FROM pub_config.gov_curves
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
            "WHERE crncy = :c AND curve_type = 'gov'"
        ), {'c': crncy}).scalar()
    today = date.today()
    return (today.replace(year=today.year - HISTORY_YEARS) if row is None
            else row + timedelta(days=1))


def _fetch_chunk(rics: list[str], start_date: date, end_date: date) -> pd.DataFrame | None:
    params = {
        'SDate': start_date.strftime('%Y-%m-%d'),
        'EDate': end_date.strftime('%Y-%m-%d'),
        'Frq':   'D',
    }
    # Government bond yields via TR.BIDYIELD + TR.ASKYIELD (mid used, =RR RICs)
    result = eikon_fetch(
        ek.get_data,
        rics,
        ['TR.BIDYIELD.Date', 'TR.BIDYIELD', 'TR.ASKYIELD'],
        params,
    )
    if result is None:
        return None
    data, _ = result
    if data is None or data.empty:
        return None

    inst_col  = data.columns[0]
    date_col  = next((c for c in data.columns if 'DATE' in str(c).upper()), None)
    bid_col   = next((c for c in data.columns if 'BID' in str(c).upper() and 'DATE' not in str(c).upper()), None)
    ask_col   = next((c for c in data.columns if 'ASK' in str(c).upper()), None)

    if date_col is None or bid_col is None:
        logger.warning('Unexpected columns: %s', list(data.columns))
        return None

    data = data.copy()
    data['trade_date'] = pd.to_datetime(data[date_col]).dt.date
    data['bid'] = pd.to_numeric(data[bid_col], errors='coerce')
    data['ask'] = pd.to_numeric(data[ask_col], errors='coerce') if ask_col else data['bid']
    data['yield_pct'] = (data['bid'] + data['ask']) / 2.0
    data = data.rename(columns={inst_col: 'ric'})
    data = data[['ric', 'trade_date', 'yield_pct']].dropna()
    data = data[data['yield_pct'] > -5]  # plausibility filter (CHF hatte neg. yields)
    return data


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


def _gov_to_zero(par_pct: float, tenor_days: int,
                 known: list[tuple[int, float]]) -> float | None:
    par = par_pct / 100.0
    if tenor_days < SHORT_END_DAYS:
        disc = 1.0 / (1.0 + par * tenor_days / 365.0)
        return (disc ** (-365.0 / tenor_days) - 1.0) * 100.0
    else:
        if not known:
            return None
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
        return (d_T ** (-365.0 / tenor_days) - 1.0) * 100.0


def _bootstrap(par_row: dict[str, float], tenors: list[dict]) -> dict[str, float]:
    zeros: dict[str, float] = {}
    known: list[tuple[int, float]] = []
    for t in sorted(tenors, key=lambda x: x['tenor_days']):
        tenor = t['tenor']
        days  = t['tenor_days']
        if tenor not in par_row:
            continue
        z = _gov_to_zero(par_row[tenor], days, known)
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
            'crncy': crncy, 'curve_type': CURVE_TYPE, 'trade_date': trade_date,
            'tenor': tenor, 'tenor_days': tenor_map.get(tenor),
            'rate_type': 'par', 'rate_pct': round(float(par_pct), 6),
            'compounding': 'annual', 'source_system': 'Eikon/Reuters',
        })
    for tenor, zero_pct in zeros.items():
        records.append({
            'crncy': crncy, 'curve_type': CURVE_TYPE, 'trade_date': trade_date,
            'tenor': tenor, 'tenor_days': tenor_map.get(tenor),
            'rate_type': 'zero', 'rate_pct': round(float(zero_pct), 6),
            'compounding': 'annual', 'source_system': 'bootstrapped',
        })
    return records


def run(
    snapshot_date: str | None = None,  # noqa: ARG001
    mode: str = 'live',
    start_date: str | None = None,
    end_date: str | None = None,
) -> None:
    logger.info('fetch_gov_rates | mode=%s', mode)
    run_id = run_start('fetch_gov_rates', mode, snapshot_date)

    config = _load_config()
    if not config:
        logger.info('No active gov curves configured (all active=FALSE) – done.')
        run_finish(run_id, rows_written=0)
        return
    logger.info('Currencies: %s', list(config.keys()))

    end_dt = date.fromisoformat(end_date) if end_date else date.today()
    tbl    = _get_table()
    total_written = 0

    for crncy, tenors in config.items():
        ric_map = {t['eikon_ric']: t['tenor'] for t in tenors}
        all_rics = list(ric_map.keys())

        start_dt = date.fromisoformat(start_date) if (mode == 'backfill' and start_date) \
                   else _get_start_date(crncy)

        if start_dt > end_dt:
            logger.info('  %s already up to date – skip', crncy)
            continue

        logger.info('  %s | %s – %s | %d tenors', crncy, start_dt, end_dt, len(tenors))

        chunk_start   = start_dt
        crncy_written = 0

        while chunk_start <= end_dt:
            chunk_end = min(
                date(chunk_start.year + 1, chunk_start.month, chunk_start.day) - timedelta(days=1),
                end_dt,
            )
            df = _fetch_chunk(all_rics, chunk_start, chunk_end)
            if df is not None and not df.empty:
                df['tenor'] = df['ric'].map(ric_map)
                pivot = df.pivot_table(index='trade_date', columns='tenor',
                                       values='yield_pct', aggfunc='last')
                all_records = []
                for trade_date, row in pivot.iterrows():
                    par_row = {c: float(v) for c, v in row.items() if pd.notna(v)}
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
    logger.info('fetch_gov_rates done. Total rows written: %d', total_written)


if __name__ == '__main__':
    run()
