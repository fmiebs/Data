"""
fetch_fundamentals_quarterly.py
---------------------------------
Fetches quarterly fundamentals for all SPX constituents.
Writes to pub_equity.stock_fundamentals_quarterly.

Delta logic per RIC:
  - No existing rows  -> start_date = today - HISTORY_YEARS
  - Existing rows     -> start_date = MAX(period_end_date) + 1 day
  - start_date > today -> skip
"""
import logging
import time
from datetime import date, timedelta

import eikon as ek
import pandas as pd
from sqlalchemy import text, Table, MetaData
from sqlalchemy.dialects.postgresql import insert as pg_insert

from config import eikon_init  # noqa: F401
from config.db import engine
from config.logging_setup import setup_logging
from config.pipeline import run_finish, run_start
from modules.utils import eikon_fetch

SCHEMA        = 'pub_equity'
TABLE         = 'stock_fundamentals_quarterly'
SLEEP_TIME    = 0.5
BATCH_SIZE    = 20
HISTORY_YEARS = 10

logger = setup_logging('fetch_fundamentals_quarterly')

_fq_table = None


def _get_fq_table():
    global _fq_table
    if _fq_table is None:
        _fq_table = Table(TABLE, MetaData(), schema=SCHEMA, autoload_with=engine)
    return _fq_table


def _get_start_date(conn, ric: str, override_start: date | None = None) -> date | None:
    if override_start is not None:
        return override_start
    row = conn.execute(
        text(f"SELECT MAX(period_end_date) FROM {SCHEMA}.{TABLE} WHERE ric = :ric"),
        {'ric': ric}
    ).scalar()
    today = date.today()
    start = (today.replace(year=today.year - HISTORY_YEARS) if row is None
             else row + timedelta(days=1))
    return None if start > today else start


def _fetch_batch(rics: list[str], start_date: date, end_date: date) -> pd.DataFrame | None:
    result = eikon_fetch(
        ek.get_data,
        rics,
        ['TR.BookValuePerShare.periodenddate', 'TR.BookValuePerShare',
         'TR.TotalEquity', 'TR.SharesOutstanding',
         'TR.GICSSector', 'TR.GICSIndustryGroup', 'TR.GICSIndustry',
         'TR.GICSSubIndustry', 'TR.ReportCurrency'],
        {'SDate': start_date.strftime('%Y-%m-%d'),
         'EDate': end_date.strftime('%Y-%m-%d'),
         'Period': 'FQ0', 'Frq': 'FQ'},
    )
    if result is None:
        return None
    data, _ = result

    if data is None or data.empty:
        return None

    date_col = next(
        (c for c in data.columns
         if c is not None and 'periodend' in c.lower().replace(' ', '')),
        None
    )
    if date_col is None:
        logger.warning('No period end date column (cols: %s)', list(data.columns))
        return None

    col_map = {
        'Instrument':               'ric',
        date_col:                   'period_end_date',
        'Book Value Per Share':     'book_value_per_share',
        'Total Equity':             'book_equity',
        'Outstanding Shares':       'shares_outstanding',
        'GICS Sector Name':         'gics_sector',
        'GICS Sector Code':         'gics_sector',
        'GICS Industry Group Name': 'gics_industry_group',
        'GICS Industry Group Code': 'gics_industry_group',
        'GICS Industry Name':       'gics_industry',
        'GICS Industry Code':       'gics_industry',
        'GICS Sub-Industry Name':   'gics_sub_industry',
        'GICS Sub-Industry Code':   'gics_sub_industry',
        'Report Currency':          'reporting_ccy',
    }
    df = data.rename(columns=col_map)

    if 'ric' not in df.columns:
        logger.warning("'Instrument' absent (cols: %s)", list(data.columns))
        return None

    df['period_end_date'] = pd.to_datetime(df['period_end_date']).dt.date
    df = df.dropna(subset=['period_end_date'])

    _month_to_q = {1: 1, 2: 1, 3: 1, 4: 2, 5: 2, 6: 2,
                   7: 3, 8: 3, 9: 3, 10: 4, 11: 4, 12: 4}
    df['fiscal_year']    = [d.year            for d in df['period_end_date']]
    df['fiscal_quarter'] = [_month_to_q[d.month] for d in df['period_end_date']]

    if df.empty:
        return None

    for col in ('book_value_per_share', 'book_equity', 'shares_outstanding',
                'gics_sector', 'gics_industry_group', 'gics_industry',
                'gics_sub_industry', 'reporting_ccy'):
        if col not in df.columns:
            df[col] = None
        else:
            df[col] = df[col].where(df[col].notna(), other=None)

    if df['shares_outstanding'].notna().any():
        df['shares_outstanding'] = df['shares_outstanding'].where(
            df['shares_outstanding'].isna(),
            df['shares_outstanding'].astype('Int64')
        )

    valid_cols = ['ric', 'period_end_date', 'fiscal_year', 'fiscal_quarter',
                  'book_value_per_share', 'book_equity', 'shares_outstanding',
                  'gics_sector', 'gics_industry_group', 'gics_industry',
                  'gics_sub_industry', 'reporting_ccy']
    return df[[c for c in valid_cols if c in df.columns]]


def run(
    snapshot_date: str | None = None,  # noqa: ARG001
    mode: str = 'live',
    start_date: str | None = None,
    end_date: str | None = None,
) -> None:
    end      = date.fromisoformat(end_date) if end_date else date.today()
    override = date.fromisoformat(start_date) if (mode == 'backfill' and start_date) else None

    logger.info('fetch_fundamentals_quarterly | mode=%s | end_date=%s', mode, end)
    run_id = run_start('fetch_fundamentals_quarterly', mode, snapshot_date)

    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT DISTINCT constituent_ric "
                 "FROM pub_equity.index_constituents WHERE index_ric = '.SPX'")
        ).fetchall()

    all_rics = [r[0] for r in rows]
    logger.info('%d RICs in universe', len(all_rics))

    ric_starts: dict[str, date] = {}
    with engine.connect() as conn:
        for ric in all_rics:
            s = _get_start_date(conn, ric, override)
            if s is not None:
                ric_starts[ric] = s

    rics_to_fetch = list(ric_starts.keys())
    logger.info('%d RICs to fetch', len(rics_to_fetch))

    tbl           = _get_fq_table()
    total_written = 0

    try:
        for i in range(0, len(rics_to_fetch), BATCH_SIZE):
            batch_rics  = rics_to_fetch[i:i + BATCH_SIZE]
            batch_start = min(ric_starts[r] for r in batch_rics)

            df = _fetch_batch(batch_rics, batch_start, end)

            if df is not None and not df.empty:
                df      = df.drop_duplicates(subset=['ric', 'period_end_date'])
                records = df.to_dict(orient='records')
                update_cols = {c for c in df.columns if c not in ('ric', 'period_end_date')}
                stmt = pg_insert(tbl).values(records).on_conflict_do_update(
                    index_elements=['ric', 'period_end_date'],
                    set_={c: pg_insert(tbl).excluded[c] for c in update_cols}
                )
                with engine.begin() as conn:
                    conn.execute(stmt)
                total_written += len(df)
                logger.info('batch %d: %d rows written (%s...)',
                            i // BATCH_SIZE + 1, len(df), batch_rics[0])
            else:
                logger.info('batch %d: no data (%s...)',
                            i // BATCH_SIZE + 1, batch_rics[0])

            time.sleep(SLEEP_TIME)

        run_finish(run_id, rows_written=total_written)
        logger.info('fetch_fundamentals_quarterly done. rows_written=%d', total_written)
    except Exception as e:
        run_finish(run_id, error=str(e))
        raise


if __name__ == '__main__':
    run()
