"""
fetch_dividends.py
------------------
Fetches regular and special cash dividends for all SPX constituents
from Refinitiv Eikon and writes them to pub_equity.dividends.

Two separate pulls per RIC:
  1. Regular dividends:  TR.DivExDate + TR.DivUnadjustedGross (+ TR.DivPayDate)
  2. Special dividends:  TR.SpecialDivExDate + TR.SpecialDivAmount

Delta logic per RIC and div_type:
  - No existing rows  → start_date = today - 10 years
  - Existing rows     → start_date = MAX(ex_date) + 1 day
  - start_date > today → skip

Upsert on PK (ric, ex_date, div_type) — on conflict do nothing
(dividend amounts are historical facts; we don't overwrite).
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
TABLE = 'dividends'
SLEEP_TIME = 0.5
BATCH_SIZE = 20
HISTORY_YEARS = 10

_div_table = None


def _get_div_table():
    global _div_table
    if _div_table is None:
        _div_table = Table(TABLE, MetaData(), schema=SCHEMA, autoload_with=engine)
    return _div_table


def _get_start_date(conn, ric: str, div_type: str) -> date | None:
    row = conn.execute(
        text(
            f"SELECT MAX(ex_date) FROM {SCHEMA}.{TABLE} "
            "WHERE ric = :ric AND div_type = :div_type"
        ),
        {"ric": ric, "div_type": div_type}
    ).scalar()

    today = date.today()
    if row is None:
        start = today.replace(year=today.year - HISTORY_YEARS)
    else:
        start = row + timedelta(days=1)

    if start > today:
        return None
    return start


def _fetch_regular(rics: list[str], start_date: date, end_date: date,
                   _retries: int = 0) -> pd.DataFrame | None:
    """Fetch regular cash dividends."""
    try:
        data, err = ek.get_data(
            rics,
            [
                'TR.DivExDate',
                'TR.DivUnadjustedGross',
                'TR.DivPayDate',
                'TR.DivCurrency',
            ],
            {
                'SDate': start_date.strftime('%Y-%m-%d'),
                'EDate': end_date.strftime('%Y-%m-%d'),
            }
        )
    except Exception as e:
        if '429' in str(e) or 'limit' in str(e).lower():
            if _retries >= 5:
                print(f"  ERROR regular batch {rics[0]} – max retries, skipping")
                return None
            print(f"  Rate limit – waiting 63s (attempt {_retries + 1}/5)...")
            time.sleep(63)
            return _fetch_regular(rics, start_date, end_date, _retries + 1)
        print(f"  ERROR regular batch {rics[0]}: {e}")
        return None

    if data is None or data.empty:
        return None

    col_map = {
        'Instrument':               'ric',
        'Dividend Ex Date':         'ex_date',
        'Ex-Dividend Date':         'ex_date',   # fallback alias
        'Gross Dividend Amount':    'amount',
        'Dividend Pay Date':        'pay_date',
        'Pay Date':                 'pay_date',  # fallback alias
        'Dividend Currency':        'currency',
    }
    df = data.rename(columns=col_map)

    if 'ric' not in df.columns or 'ex_date' not in df.columns:
        print(f"  WARN: expected columns missing (cols: {list(data.columns)}) – skipping")
        return None

    df['ex_date'] = pd.to_datetime(df['ex_date'], errors='coerce').dt.date
    df = df.dropna(subset=['ex_date', 'amount'])
    if df.empty:
        return None

    df['div_type'] = 'regular'

    if 'pay_date' in df.columns:
        df['pay_date'] = pd.to_datetime(df['pay_date'], errors='coerce').dt.date
        df['pay_date'] = df['pay_date'].where(df['pay_date'].notna(), other=None)
    else:
        df['pay_date'] = None

    if 'currency' not in df.columns:
        df['currency'] = None
    else:
        df['currency'] = df['currency'].where(df['currency'].notna(), other=None)

    valid_cols = ['ric', 'ex_date', 'div_type', 'amount', 'currency', 'pay_date']
    return df[valid_cols]


def _fetch_special(rics: list[str], start_date: date, end_date: date,
                   _retries: int = 0) -> pd.DataFrame | None:
    """Fetch special (non-recurring) cash dividends."""
    try:
        data, err = ek.get_data(
            rics,
            [
                'TR.SpecialDivExDate',
                'TR.SpecialDivAmount',
                'TR.SpecialDivCurrency',
            ],
            {
                'SDate': start_date.strftime('%Y-%m-%d'),
                'EDate': end_date.strftime('%Y-%m-%d'),
            }
        )
    except Exception as e:
        if '429' in str(e) or 'limit' in str(e).lower():
            if _retries >= 5:
                print(f"  ERROR special batch {rics[0]} – max retries, skipping")
                return None
            print(f"  Rate limit – waiting 63s (attempt {_retries + 1}/5)...")
            time.sleep(63)
            return _fetch_special(rics, start_date, end_date, _retries + 1)
        print(f"  ERROR special batch {rics[0]}: {e}")
        return None

    if data is None or data.empty:
        return None

    # Eikon may return uppercase field names (e.g. TR.SPECIALDIVEXDATE) when data is sparse
    def _find_col(df_cols, *patterns) -> str | None:
        for pat in patterns:
            for c in df_cols:
                if c and pat.lower() in str(c).lower().replace('.', '').replace('_', ''):
                    return c
        return None

    inst_col   = _find_col(data.columns, 'instrument')
    exdate_col = _find_col(data.columns, 'specialdivexdate', 'exdate')
    amount_col = _find_col(data.columns, 'specialdivamount', 'amount')
    ccy_col    = _find_col(data.columns, 'specialdivcurrency', 'currency')

    rename = {}
    if inst_col:   rename[inst_col]   = 'ric'
    if exdate_col: rename[exdate_col] = 'ex_date'
    if amount_col: rename[amount_col] = 'amount'
    if ccy_col:    rename[ccy_col]    = 'currency'

    df = data.rename(columns=rename)

    if 'ric' not in df.columns or 'ex_date' not in df.columns:
        return None

    df['ex_date'] = pd.to_datetime(df['ex_date'], errors='coerce').dt.date
    df = df.dropna(subset=['ex_date', 'amount'])
    if df.empty:
        return None

    df['div_type'] = 'special'
    df['pay_date'] = None

    if 'currency' not in df.columns:
        df['currency'] = None
    else:
        df['currency'] = df['currency'].where(df['currency'].notna(), other=None)

    valid_cols = ['ric', 'ex_date', 'div_type', 'amount', 'currency', 'pay_date']
    return df[valid_cols]


def _write_records(df: pd.DataFrame) -> int:
    tbl = _get_div_table()
    df = df.drop_duplicates(subset=['ric', 'ex_date', 'div_type'])
    records = df.to_dict(orient='records')
    stmt = pg_insert(tbl).values(records).on_conflict_do_nothing(
        index_elements=['ric', 'ex_date', 'div_type']
    )
    with engine.begin() as conn:
        conn.execute(stmt)
    return len(df)


def run(snapshot_date: str | None = None) -> None:  # noqa: ARG001
    end_date = date.today()

    print(f"fetch_dividends | end_date={end_date} | history={HISTORY_YEARS}y")

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

    # Determine start dates for regular and special separately
    regular_starts: dict[str, date] = {}
    special_starts: dict[str, date] = {}

    with engine.connect() as conn:
        for ric in all_rics:
            s_reg = _get_start_date(conn, ric, 'regular')
            if s_reg is not None:
                regular_starts[ric] = s_reg
            s_spe = _get_start_date(conn, ric, 'special')
            if s_spe is not None:
                special_starts[ric] = s_spe

    print(f"  {len(regular_starts)} RICs to fetch (regular), "
          f"{len(special_starts)} RICs to fetch (special)")

    total_written = 0

    # --- Regular dividends ---
    rics_reg = list(regular_starts.keys())
    for i in range(0, len(rics_reg), BATCH_SIZE):
        batch_rics = rics_reg[i:i + BATCH_SIZE]
        batch_start = min(regular_starts[r] for r in batch_rics)

        df = _fetch_regular(batch_rics, batch_start, end_date)
        if df is not None and not df.empty:
            n = _write_records(df)
            total_written += n
            print(f"    regular batch {i // BATCH_SIZE + 1}: {n} rows ({batch_rics[0]}…)")
        else:
            print(f"    regular batch {i // BATCH_SIZE + 1}: no data ({batch_rics[0]}…)")

        time.sleep(SLEEP_TIME)

    # --- Special dividends ---
    rics_spe = list(special_starts.keys())
    for i in range(0, len(rics_spe), BATCH_SIZE):
        batch_rics = rics_spe[i:i + BATCH_SIZE]
        batch_start = min(special_starts[r] for r in batch_rics)

        df = _fetch_special(batch_rics, batch_start, end_date)
        if df is not None and not df.empty:
            n = _write_records(df)
            total_written += n
            print(f"    special batch {i // BATCH_SIZE + 1}: {n} rows ({batch_rics[0]}…)")
        else:
            print(f"    special batch {i // BATCH_SIZE + 1}: no data ({batch_rics[0]}…)")

        time.sleep(SLEEP_TIME)

    print(f"fetch_dividends done. Total rows written: {total_written}")


if __name__ == '__main__':
    run()
