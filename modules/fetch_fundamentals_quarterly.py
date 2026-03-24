"""
fetch_fundamentals_quarterly.py
--------------------------------
Fetches quarterly fundamentals (book equity, GICS) for all SPX constituents
from Refinitiv Eikon and writes them to pub_equity.fundamentals_quarterly.

Fields fetched (all quarterly):
  TR.BookValuePerShare, TR.TotalEquity,
  TR.GICSSector, TR.GICSIndustryGroup, TR.GICSIndustry, TR.GICSSubIndustry

Delta logic per RIC:
  - No existing rows  → start_date = today - 10 years
  - Existing rows     → start_date = MAX(period_end_date) + 1 day
  - start_date > today → skip
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
TABLE = 'fundamentals_quarterly'
SLEEP_TIME = 0.5
BATCH_SIZE = 20
HISTORY_YEARS = 10

_fq_table = None


def _get_fq_table():
    global _fq_table
    if _fq_table is None:
        _fq_table = Table(TABLE, MetaData(), schema=SCHEMA, autoload_with=engine)
    return _fq_table


def _get_start_date(conn, ric: str) -> date | None:
    row = conn.execute(
        text(f"SELECT MAX(period_end_date) FROM {SCHEMA}.{TABLE} WHERE ric = :ric"),
        {"ric": ric}
    ).scalar()

    today = date.today()
    if row is None:
        start = today.replace(year=today.year - HISTORY_YEARS)
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
            [
                'TR.BookValuePerShare.periodenddate',
                'TR.BookValuePerShare',
                'TR.TotalEquity',
                'TR.SharesOutstanding',
                'TR.GICSSector',
                'TR.GICSIndustryGroup',
                'TR.GICSIndustry',
                'TR.GICSSubIndustry',
                'TR.ReportCurrency',
            ],
            {
                'SDate': start_date.strftime('%Y-%m-%d'),
                'EDate': end_date.strftime('%Y-%m-%d'),
                'Period': 'FQ0',
                'Frq':    'FQ',
            }
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

    # Identify the period end date column (Eikon returns 'Period End Date')
    date_col = next(
        (c for c in data.columns
         if c is not None and 'periodend' in c.lower().replace(' ', '')),
        None
    )
    if date_col is None:
        print(f"  WARN: no period end date column (cols: {list(data.columns)}) – skipping")
        return None

    col_map = {
        'Instrument':                  'ric',
        date_col:                      'period_end_date',
        'Book Value Per Share':        'book_value_per_share',
        'Total Equity':                'book_equity',
        'Outstanding Shares':          'shares_outstanding',
        'GICS Sector Name':            'gics_sector',
        'GICS Sector Code':            'gics_sector',        # fallback alias
        'GICS Industry Group Name':    'gics_industry_group',
        'GICS Industry Group Code':    'gics_industry_group', # fallback alias
        'GICS Industry Name':          'gics_industry',
        'GICS Industry Code':          'gics_industry',       # fallback alias
        'GICS Sub-Industry Name':      'gics_sub_industry',
        'GICS Sub-Industry Code':      'gics_sub_industry',   # fallback alias
        'Report Currency':             'reporting_ccy',
    }
    df = data.rename(columns=col_map)

    if 'ric' not in df.columns:
        print(f"  WARN: 'Instrument' absent (cols: {list(data.columns)}) – skipping")
        return None

    df['period_end_date'] = pd.to_datetime(df['period_end_date']).dt.date
    df = df.dropna(subset=['period_end_date'])

    # Derive fiscal_year and fiscal_quarter from period_end_date.
    # Calendar-quarter approximation covers all fiscal year conventions
    # (e.g. Jan/Feb/Mar → Q1, Apr/May/Jun → Q2, ...).
    # Companies with non-Dec fiscal year ends (WMT, NVDA, CSCO, ...) are
    # assigned the calendar quarter of their period_end_date.
    _month_to_q = {
        1: 1, 2: 1, 3: 1,
        4: 2, 5: 2, 6: 2,
        7: 3, 8: 3, 9: 3,
        10: 4, 11: 4, 12: 4,
    }
    df['fiscal_year'] = [d.year for d in df['period_end_date']]
    df['fiscal_quarter'] = [_month_to_q[d.month] for d in df['period_end_date']]
    if df.empty:
        return None

    # Ensure optional columns exist
    for col in ('book_value_per_share', 'book_equity', 'shares_outstanding',
                'gics_sector', 'gics_industry_group', 'gics_industry',
                'gics_sub_industry', 'reporting_ccy'):
        if col not in df.columns:
            df[col] = None
        else:
            df[col] = df[col].where(df[col].notna(), other=None)

    # shares_outstanding: ensure integer (Eikon returns float)
    if df['shares_outstanding'].notna().any():
        df['shares_outstanding'] = df['shares_outstanding'].where(
            df['shares_outstanding'].isna(),
            df['shares_outstanding'].astype('Int64')
        )

    valid_cols = [
        'ric', 'period_end_date', 'fiscal_year', 'fiscal_quarter',
        'book_value_per_share', 'book_equity', 'shares_outstanding',
        'gics_sector', 'gics_industry_group', 'gics_industry',
        'gics_sub_industry', 'reporting_ccy',
    ]
    return df[[c for c in valid_cols if c in df.columns]]


def run(snapshot_date: str | None = None) -> None:  # noqa: ARG001
    end_date = date.today()

    print(f"fetch_fundamentals_quarterly | end_date={end_date} | history={HISTORY_YEARS}y")

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

    ric_starts: dict[str, date] = {}
    with engine.connect() as conn:
        for ric in all_rics:
            start = _get_start_date(conn, ric)
            if start is not None:
                ric_starts[ric] = start

    rics_to_fetch = list(ric_starts.keys())
    print(f"  {len(rics_to_fetch)} RICs to fetch")

    tbl = _get_fq_table()
    total_written = 0

    for i in range(0, len(rics_to_fetch), BATCH_SIZE):
        batch_rics = rics_to_fetch[i:i + BATCH_SIZE]
        batch_start = min(ric_starts[r] for r in batch_rics)

        df = _fetch_batch(batch_rics, batch_start, end_date)

        if df is not None and not df.empty:
            df = df.drop_duplicates(subset=['ric', 'period_end_date'])
            records = df.to_dict(orient='records')

            update_cols = {c for c in df.columns if c not in ('ric', 'period_end_date')}
            stmt = pg_insert(tbl).values(records).on_conflict_do_update(
                index_elements=['ric', 'period_end_date'],
                set_={c: pg_insert(tbl).excluded[c] for c in update_cols}
            )
            with engine.begin() as conn:
                conn.execute(stmt)
            total_written += len(df)
            print(f"    batch {i // BATCH_SIZE + 1}: {len(df)} rows written "
                  f"({batch_rics[0]}…)")
        else:
            print(f"    batch {i // BATCH_SIZE + 1}: no data ({batch_rics[0]}…)")

        time.sleep(SLEEP_TIME)

    print(f"fetch_fundamentals_quarterly done. Total rows written: {total_written}")


if __name__ == '__main__':
    run()
