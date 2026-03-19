import time
from datetime import datetime

import eikon as ek
import pandas as pd
from sqlalchemy import text

from config.db import engine
from config import eikon_init  # noqa: F401 – sets Eikon app key on import


SCHEMA = 'pub_options'
TABLE = 'options_chains'
SLEEP_TIME = 0.5


def _get_snapshot_date() -> str:
    today = datetime.now()
    return today.replace(day=1).strftime('%Y-%m-%d')


def _check_exists(conn, underlying_ric: str, snapshot_date: str) -> bool:
    query = text(
        f"SELECT COUNT(*) FROM {SCHEMA}.{TABLE} "
        "WHERE underlying_ric = :ric AND snapshot_date = :date"
    )
    count = conn.execute(query, {"ric": underlying_ric, "date": snapshot_date}).scalar()
    return count > 0


def _map_option_type(val) -> str | None:
    if val in [1, '1', 'CALL', 'Call', 'C']:
        return 'CALL'
    elif val in [2, '2', 'PUT', 'Put', 'P']:
        return 'PUT'
    return None


def _extract_ticker(constituent_ric: str) -> str:
    """'AAPL.O' → 'AAPL', '.SPX' → 'SPX'"""
    parts = constituent_ric.split('.')
    return parts[1] if parts[0] == '' else parts[0]


def _fetch(constituent_ric: str, snapshot_date: str) -> pd.DataFrame | None:
    ticker = _extract_ticker(constituent_ric)
    chain_ric = f'0#{ticker}*.U'

    try:
        data, err = ek.get_data(
            chain_ric,
            ['EXPIR_DATE', 'STRIKE_PRC', 'PUTCALLIND']
        )
    except Exception as e:
        if '429' in str(e) or 'limit' in str(e).lower():
            print(f"  Rate limit hit for {chain_ric} – waiting 63s...")
            time.sleep(63)
            return _fetch(constituent_ric, snapshot_date)
        print(f"  ERROR fetching {chain_ric}: {e}")
        return None

    if data is None or data.empty:
        print(f"  No data for {chain_ric} (no chain or access denied)")
        return None

    df = data.rename(columns={
        'Instrument': 'option_ric',
        'EXPIR_DATE': 'expiry_date',
        'STRIKE_PRC': 'strike',
        'PUTCALLIND': 'option_type',
    })

    df = df.dropna(subset=['strike', 'expiry_date'])
    df['option_type'] = df['option_type'].apply(_map_option_type)
    df = df.dropna(subset=['option_type'])

    df['expiry_date']    = pd.to_datetime(df['expiry_date']).dt.date
    df['underlying_ric'] = constituent_ric
    df['snapshot_date']  = snapshot_date

    valid_columns = ['underlying_ric', 'option_ric', 'expiry_date', 'strike', 'option_type', 'snapshot_date']
    return df[valid_columns]


def run(snapshot_date: str | None = None) -> None:
    if snapshot_date is None:
        snapshot_date = _get_snapshot_date()

    print(f"fetch_option_chains_constituents | snapshot_date={snapshot_date}")

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT DISTINCT constituent_ric "
                "FROM pub_equity.index_constituents "
                "WHERE snapshot_date = :date"
            ),
            {"date": snapshot_date}
        ).fetchall()

    constituents = [r[0] for r in rows]
    print(f"  {len(constituents)} unique constituent(s) found")

    for ric in constituents:
        with engine.connect() as conn:
            if _check_exists(conn, ric, snapshot_date):
                print(f"  SKIP {ric} – already in DB")
                continue

        print(f"  Processing {ric}...")
        df = _fetch(ric, snapshot_date)

        if df is not None and not df.empty:
            df.to_sql(TABLE, engine, schema=SCHEMA, if_exists='append', index=False)
            print(f"  OK {ric} – {len(df)} rows written")
        else:
            print(f"  WARN {ric} – nothing to write")

        time.sleep(SLEEP_TIME)

    print("fetch_option_chains_constituents done.")


if __name__ == '__main__':
    run()
