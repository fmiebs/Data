"""
fetch_dividends.py
------------------
Fetches regular and special cash dividends for all SPX constituents.
Writes to pub_equity.stock_dividends.

Delta logic per RIC and div_type:
  - No existing rows  -> start_date = today - HISTORY_YEARS
  - Existing rows     -> start_date = MAX(ex_date) + 1 day
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
TABLE         = 'stock_dividends'
SLEEP_TIME    = 0.5
BATCH_SIZE    = 20
HISTORY_YEARS = 10

logger = setup_logging('fetch_dividends')

_div_table = None


def _get_div_table():
    global _div_table
    if _div_table is None:
        _div_table = Table(TABLE, MetaData(), schema=SCHEMA, autoload_with=engine)
    return _div_table


def _get_start_date(conn, ric: str, div_type: str,
                    override_start: date | None = None) -> date | None:
    if override_start is not None:
        return override_start
    row = conn.execute(
        text(f"SELECT MAX(ex_date) FROM {SCHEMA}.{TABLE} "
             "WHERE ric = :ric AND div_type = :div_type"),
        {'ric': ric, 'div_type': div_type}
    ).scalar()
    today = date.today()
    start = (today.replace(year=today.year - HISTORY_YEARS) if row is None
             else row + timedelta(days=1))
    return None if start > today else start


def _fetch_regular(rics: list[str], start_date: date, end_date: date) -> pd.DataFrame | None:
    result = eikon_fetch(
        ek.get_data,
        rics,
        ['TR.DivExDate', 'TR.DivUnadjustedGross', 'TR.DivPayDate', 'TR.DivCurrency'],
        {'SDate': start_date.strftime('%Y-%m-%d'), 'EDate': end_date.strftime('%Y-%m-%d')},
    )
    if result is None:
        return None
    data, _ = result

    if data is None or data.empty:
        return None

    col_map = {
        'Instrument':            'ric',
        'Dividend Ex Date':      'ex_date',
        'Ex-Dividend Date':      'ex_date',
        'Gross Dividend Amount': 'amount',
        'Dividend Pay Date':     'pay_date',
        'Pay Date':              'pay_date',
        'Dividend Currency':     'currency',
    }
    df = data.rename(columns=col_map)

    if 'ric' not in df.columns or 'ex_date' not in df.columns:
        logger.warning('Expected columns missing (cols: %s)', list(data.columns))
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

    return df[['ric', 'ex_date', 'div_type', 'amount', 'currency', 'pay_date']]


def _fetch_special(rics: list[str], start_date: date, end_date: date) -> pd.DataFrame | None:
    result = eikon_fetch(
        ek.get_data,
        rics,
        ['TR.SpecialDivExDate', 'TR.SpecialDivAmount', 'TR.SpecialDivCurrency'],
        {'SDate': start_date.strftime('%Y-%m-%d'), 'EDate': end_date.strftime('%Y-%m-%d')},
    )
    if result is None:
        return None
    data, _ = result

    if data is None or data.empty:
        return None

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

    return df[['ric', 'ex_date', 'div_type', 'amount', 'currency', 'pay_date']]


def _write_records(df: pd.DataFrame) -> int:
    tbl     = _get_div_table()
    df      = df.drop_duplicates(subset=['ric', 'ex_date', 'div_type'])
    records = df.to_dict(orient='records')
    stmt    = pg_insert(tbl).values(records).on_conflict_do_nothing(
        index_elements=['ric', 'ex_date', 'div_type']
    )
    with engine.begin() as conn:
        conn.execute(stmt)
    return len(df)


def run(
    snapshot_date: str | None = None,  # noqa: ARG001
    mode: str = 'live',
    start_date: str | None = None,
    end_date: str | None = None,
) -> None:
    end      = date.fromisoformat(end_date) if end_date else date.today()
    override = date.fromisoformat(start_date) if (mode == 'backfill' and start_date) else None

    logger.info('fetch_dividends | mode=%s | end_date=%s', mode, end)
    run_id = run_start('fetch_dividends', mode, snapshot_date)

    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT DISTINCT constituent_ric "
                 "FROM pub_equity.index_constituents WHERE index_ric = '.SPX'")
        ).fetchall()

    all_rics = [r[0] for r in rows]
    logger.info('%d RICs in universe', len(all_rics))

    regular_starts: dict[str, date] = {}
    special_starts: dict[str, date] = {}

    with engine.connect() as conn:
        for ric in all_rics:
            s_reg = _get_start_date(conn, ric, 'regular', override)
            if s_reg is not None:
                regular_starts[ric] = s_reg
            s_spe = _get_start_date(conn, ric, 'special', override)
            if s_spe is not None:
                special_starts[ric] = s_spe

    logger.info('%d RICs (regular), %d RICs (special)',
                len(regular_starts), len(special_starts))

    total_written = 0
    try:
        for i in range(0, len(regular_starts), BATCH_SIZE):
            batch_rics  = list(regular_starts.keys())[i:i + BATCH_SIZE]
            batch_start = min(regular_starts[r] for r in batch_rics)
            df = _fetch_regular(batch_rics, batch_start, end)
            if df is not None and not df.empty:
                n = _write_records(df)
                total_written += n
                logger.info('regular batch %d: %d rows (%s...)',
                            i // BATCH_SIZE + 1, n, batch_rics[0])
            else:
                logger.info('regular batch %d: no data (%s...)',
                            i // BATCH_SIZE + 1, batch_rics[0])
            time.sleep(SLEEP_TIME)

        for i in range(0, len(special_starts), BATCH_SIZE):
            batch_rics  = list(special_starts.keys())[i:i + BATCH_SIZE]
            batch_start = min(special_starts[r] for r in batch_rics)
            df = _fetch_special(batch_rics, batch_start, end)
            if df is not None and not df.empty:
                n = _write_records(df)
                total_written += n
                logger.info('special batch %d: %d rows (%s...)',
                            i // BATCH_SIZE + 1, n, batch_rics[0])
            else:
                logger.info('special batch %d: no data (%s...)',
                            i // BATCH_SIZE + 1, batch_rics[0])
            time.sleep(SLEEP_TIME)

        run_finish(run_id, rows_written=total_written)
        logger.info('fetch_dividends done. rows_written=%d', total_written)
    except Exception as e:
        run_finish(run_id, error=str(e))
        raise


if __name__ == '__main__':
    run()
