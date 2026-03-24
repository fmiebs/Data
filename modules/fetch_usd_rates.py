"""
fetch_usd_rates.py
------------------
Fetches FRED CMT par yields and bootstraps annually compounded discrete zero rates.
Writes both rate types to pub_rates.usd_rates (PostgreSQL).

Rate conventions (per data requirements spec):
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

load_dotenv(r'C:\Users\Miebs\PycharmProjects\Config\.env')

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TENOR_DAYS: dict[str, int] = {
    '1m':  30,   '3m':  91,   '6m':  182,
    '1y':  365,  '2y':  730,  '3y':  1095,
    '5y':  1825, '7y':  2555, '10y': 3650,
    '20y': 7300, '30y': 10950,
}
FRED_SERIES: dict[str, str] = {
    '1m':  'DGS1MO', '3m':  'DGS3MO', '6m':  'DGS6MO',
    '1y':  'DGS1',   '2y':  'DGS2',   '3y':  'DGS3',
    '5y':  'DGS5',   '7y':  'DGS7',   '10y': 'DGS10',
    '20y': 'DGS20',  '30y': 'DGS30',
}
SHORT_END = ['1m', '3m', '6m']                                 # bank discount, 360-day year
LONG_END  = ['1y', '2y', '3y', '5y', '7y', '10y', '20y', '30y']  # semiannual BEY
HISTORY_YEARS = 10
SCHEMA = 'pub_rates'
TABLE  = 'usd_rates'
CHUNK_SIZE = 5000

_DDL = f"""
CREATE SCHEMA IF NOT EXISTS {SCHEMA};

CREATE TABLE IF NOT EXISTS {SCHEMA}.{TABLE} (
    trade_date   DATE         NOT NULL,
    tenor        VARCHAR(8)   NOT NULL,
    tenor_days   SMALLINT     NOT NULL,
    rate_type    VARCHAR(8)   NOT NULL,
    rate_pct     NUMERIC(8,4),
    compounding  VARCHAR(16)  NOT NULL,
    source       VARCHAR(32)  NOT NULL,
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (trade_date, tenor, rate_type)
);

CREATE INDEX IF NOT EXISTS idx_usd_rates_date ON {SCHEMA}.{TABLE} (trade_date);
"""

# ---------------------------------------------------------------------------
# Schema / table setup
# ---------------------------------------------------------------------------

def _ensure_schema_and_table() -> None:
    with engine.begin() as conn:
        conn.execute(text(_DDL))


# ---------------------------------------------------------------------------
# Incremental date range
# ---------------------------------------------------------------------------

def _get_fetch_start() -> date:
    """Returns first date to fetch: day after max known trade_date, or 10y back."""
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
    """Log-linear interpolation of zero rates; flat extrapolation at edges.

    known: sorted list of (days, r_zero_pct) pairs.
    """
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
    return rate_arr[-1]  # fallback


def _bill_to_zero(d_pct: float, days: int) -> float:
    """Convert bank-discount T-bill rate to annually compounded zero rate (%).

    d_pct: annualised bank discount rate in percent
    days:  tenor in calendar days
    """
    d = d_pct / 100.0
    price = 1.0 - d * days / 360.0
    r_zero = (1.0 / price) ** (365.0 / days) - 1.0
    return r_zero * 100.0


def _bond_to_zero(
    c_pct: float,
    tenor_days: int,
    known: list[tuple[int, float]],
) -> float | None:
    """Bootstrap annually compounded zero rate from a par semiannual bond.

    c_pct:      annual par yield in percent (coupon rate = c_pct / 2 per period)
    tenor_days: maturity in calendar days
    known:      sorted list of (days, r_zero_pct) already bootstrapped

    Par bond condition (price = face = 100):
        1 = (c/2) * sum_{i=1}^{n-1} d(t_i)  +  (1 + c/2) * d(T)
    Solving for d(T):
        d(T) = (1 - (c/2) * sum_d) / (1 + c/2)
    """
    c = c_pct / 100.0
    n_periods = round(tenor_days / 365.0 * 2.0)  # number of semiannual periods

    sum_d = 0.0
    for i in range(1, n_periods):
        t_d = round(i * 0.5 * 365.0)
        r_i = _interp_zero(t_d, known) / 100.0
        d_i = (1.0 + r_i) ** (-t_d / 365.0)
        sum_d += d_i

    d_T = (1.0 - (c / 2.0) * sum_d) / (1.0 + c / 2.0)
    if d_T <= 0.0:
        logger.warning("Degenerate discount factor d(T)=%.4f for tenor_days=%d", d_T, tenor_days)
        return None

    r_zero = d_T ** (-365.0 / tenor_days) - 1.0
    return r_zero * 100.0


def _bootstrap_zeros(par_row: dict[str, float]) -> dict[str, float]:
    """Bootstrap zero rates for a single trade date.

    par_row: {tenor: rate_pct} — only tenors with valid (non-NaN) par yields.
    Returns: {tenor: zero_rate_pct}
    """
    zeros: dict[str, float] = {}
    known: list[tuple[int, float]] = []  # sorted (days, r_zero_pct)

    # Short end — direct conversion from bank discount
    for tenor in SHORT_END:
        if tenor not in par_row:
            continue
        days = TENOR_DAYS[tenor]
        z = _bill_to_zero(par_row[tenor], days)
        zeros[tenor] = z
        known.append((days, z))

    # Long end — sequential bootstrap
    for tenor in LONG_END:
        if tenor not in par_row:
            continue
        if not known:
            continue  # cannot bootstrap without any prior zeros
        days = TENOR_DAYS[tenor]
        z = _bond_to_zero(par_row[tenor], days, known)
        if z is not None:
            zeros[tenor] = z
            known.append((days, z))
            known.sort()

    return zeros


# ---------------------------------------------------------------------------
# Record builder
# ---------------------------------------------------------------------------

def _build_records(trade_date: date, par_row: dict[str, float]) -> list[dict]:
    """Build DB records (par + zero) for a single trade date."""
    records: list[dict] = []
    zeros = _bootstrap_zeros(par_row)

    for tenor, days in TENOR_DAYS.items():
        if tenor in par_row:
            comp = 'discount' if tenor in SHORT_END else 'semiannual'
            records.append({
                'trade_date':  trade_date,
                'tenor':       tenor,
                'tenor_days':  days,
                'rate_type':   'par',
                'rate_pct':    round(float(par_row[tenor]), 4),
                'compounding': comp,
                'source':      'FRED',
            })

        if tenor in zeros:
            records.append({
                'trade_date':  trade_date,
                'tenor':       tenor,
                'tenor_days':  days,
                'rate_type':   'zero',
                'rate_pct':    round(float(zeros[tenor]), 4),
                'compounding': 'annual',
                'source':      'bootstrapped',
            })

    return records


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run(snapshot_date: str | None = None) -> None:  # noqa: ARG001 – snapshot_date unused but required by pipeline contract
    """Fetch FRED CMT par yields and bootstrap zero rates into pub_rates.usd_rates.

    snapshot_date is accepted for pipeline consistency; rates are always updated
    to the most recent available FRED observation.
    """
    _ensure_schema_and_table()

    fred_key = os.getenv('FRED_API_KEY')
    if not fred_key:
        raise RuntimeError(
            "FRED_API_KEY not found. Add it to C:\\Users\\Miebs\\PycharmProjects\\Config\\.env"
        )

    from fredapi import Fred  # imported here to keep startup fast when key is missing
    fred = Fred(api_key=fred_key)

    start_date = _get_fetch_start()
    end_date   = date.today()

    if start_date > end_date:
        print("fetch_usd_rates: already up to date.")
        return

    print(f"fetch_usd_rates | range={start_date} - {end_date}")

    # Pull all 11 FRED series
    raw_series: dict[str, pd.Series] = {}
    for tenor, fred_id in FRED_SERIES.items():
        try:
            s = fred.get_series(
                fred_id,
                observation_start=start_date.strftime('%Y-%m-%d'),
                observation_end=end_date.strftime('%Y-%m-%d'),
            )
            raw_series[tenor] = s
            print(f"  {fred_id}: {len(s)} observations")
        except Exception as exc:
            logger.warning("Could not fetch %s: %s", fred_id, exc)
            print(f"  WARN: {fred_id} fetch failed – {exc}")

    if not raw_series:
        print("  ERROR: no FRED series fetched – aborting.")
        return

    # Align into DataFrame (index = date, columns = tenors), drop all-NaN rows
    par_df = pd.DataFrame(raw_series)
    par_df.index = pd.to_datetime(par_df.index).date
    par_df = par_df.sort_index().dropna(how='all')
    print(f"  {len(par_df)} trading days to process")

    # Build records
    all_records: list[dict] = []
    for trade_date, row in par_df.iterrows():
        par_row = {
            t: float(v)
            for t, v in row.items()
            if t in TENOR_DAYS and pd.notna(v)
        }
        all_records.extend(_build_records(trade_date, par_row))

    if not all_records:
        print("  No records to write.")
        return

    # Write in chunks (upsert – idempotent)
    tbl = Table(TABLE, MetaData(), schema=SCHEMA, autoload_with=engine)
    written = 0
    for i in range(0, len(all_records), CHUNK_SIZE):
        chunk = all_records[i:i + CHUNK_SIZE]
        stmt = pg_insert(tbl).values(chunk).on_conflict_do_nothing(
            index_elements=['trade_date', 'tenor', 'rate_type']
        )
        with engine.begin() as conn:
            conn.execute(stmt)
        written += len(chunk)

    print(f"  {written} rows written to {SCHEMA}.{TABLE}")
    print("fetch_usd_rates done.")


if __name__ == '__main__':
    run()
