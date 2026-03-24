"""
fetch_futures_chains.py
-----------------------
Fetches active futures contracts for all underlyings in pub_futures.futures_overview
where active = TRUE. Writes master data to pub_futures.futures_chains.

Idempotent: existing (futures_ric, snapshot_date) pairs are skipped.
"""
import time
from datetime import datetime

import eikon as ek
import pandas as pd
from sqlalchemy import text

from config.db import engine
from config import eikon_init  # noqa: F401
from modules.utils import classify_futures_expiry

SCHEMA = 'pub_futures'
TABLE  = 'futures_chains'
SLEEP_TIME = 0.5


def _get_snapshot_date() -> str:
    today = datetime.now()
    return today.replace(day=1).strftime('%Y-%m-%d')


def _is_present(conn, underlying_ric: str, snapshot_date: str) -> bool:
    count = conn.execute(
        text(f"SELECT COUNT(*) FROM {SCHEMA}.{TABLE} "
             "WHERE underlying_ric = :ric AND snapshot_date = :snap"),
        {"ric": underlying_ric, "snap": snapshot_date}
    ).scalar()
    return count > 0


def _fetch(underlying_ric: str, futures_chain_ric: str,
           snapshot_date: str, _retries: int = 0) -> pd.DataFrame | None:
    chain = f'0#{futures_chain_ric}'
    try:
        data, err = ek.get_data(chain, ['EXPIR_DATE'])
    except Exception as e:
        if '429' in str(e) or 'limit' in str(e).lower():
            if _retries >= 5:
                print(f"  ERROR {chain} – max retries, skipping")
                return None
            print(f"  Rate limit – waiting 63s (attempt {_retries + 1}/5)...")
            time.sleep(63)
            return _fetch(underlying_ric, futures_chain_ric, snapshot_date, _retries + 1)
        print(f"  ERROR {chain}: {e}")
        return None

    if data is None or data.empty:
        print(f"  No data for {chain}")
        return None

    df = data.rename(columns={
        'Instrument': 'futures_ric',
        'Expiry Date': 'expiry_date',
    })

    # Eikon may return column as 'EXPIR_DATE' unchanged
    if 'expiry_date' not in df.columns and 'EXPIR_DATE' in df.columns:
        df = df.rename(columns={'EXPIR_DATE': 'expiry_date'})

    df = df.dropna(subset=['expiry_date'])
    df['expiry_date']   = pd.to_datetime(df['expiry_date']).dt.date
    df['contract_month'] = df['expiry_date'].apply(lambda d: d.strftime('%Y-%m'))
    df['expiry_type']    = df['expiry_date'].apply(classify_futures_expiry)
    df['underlying_ric'] = underlying_ric
    df['snapshot_date']  = snapshot_date

    valid_cols = ['underlying_ric', 'futures_ric', 'snapshot_date',
                  'expiry_date', 'contract_month', 'expiry_type']
    return df[valid_cols]


def run(snapshot_date: str | None = None) -> None:
    if snapshot_date is None:
        snapshot_date = _get_snapshot_date()

    print(f"fetch_futures_chains | snapshot_date={snapshot_date}")

    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT underlying_ric, futures_chain_ric "
                 "FROM pub_futures.futures_overview "
                 "WHERE active = TRUE AND futures_chain_ric IS NOT NULL")
        ).fetchall()

    print(f"  {len(rows)} active underlying(s)")

    for underlying_ric, futures_chain_ric in rows:
        with engine.connect() as conn:
            if _is_present(conn, underlying_ric, snapshot_date):
                print(f"  SKIP {underlying_ric} – already in DB")
                continue

        print(f"  Processing {underlying_ric} (chain: 0#{futures_chain_ric})...")
        df = _fetch(underlying_ric, futures_chain_ric, snapshot_date)

        if df is not None and not df.empty:
            df.to_sql(TABLE, engine, schema=SCHEMA, if_exists='append', index=False)
            print(f"  OK {underlying_ric} – {len(df)} contracts written")
        else:
            print(f"  WARN {underlying_ric} – nothing to write")

        time.sleep(SLEEP_TIME)

    print("fetch_futures_chains done.")


if __name__ == '__main__':
    run()
