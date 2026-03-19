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


def _fetch(index_ric: str, option_chain_ric: str, snapshot_date: str) -> pd.DataFrame | None:
    chain_ric = f'0#{option_chain_ric}'
    try:
        data, err = ek.get_data(
            chain_ric,
            ['EXPIR_DATE', 'STRIKE_PRC', 'PUTCALLIND']
        )
    except Exception as e:
        if '429' in str(e) or 'limit' in str(e).lower():
            print(f"  Rate limit hit for {chain_ric} – waiting 63s...")
            time.sleep(63)
            return _fetch(index_ric, option_chain_ric, snapshot_date)
        print(f"  ERROR fetching {chain_ric}: {e}")
        return None

    if data is None or data.empty:
        print(f"  No data returned for {chain_ric}")
        return None

    df = data.rename(columns={
        'Instrument':  'option_ric',
        'EXPIR_DATE':  'expiry_date',
        'STRIKE_PRC':  'strike',
        'PUTCALLIND':  'option_type',
    })

    # Drop first row if it is the underlying itself (no strike / no expiry)
    df = df.dropna(subset=['strike', 'expiry_date'])

    df['option_type'] = df['option_type'].apply(_map_option_type)
    df = df.dropna(subset=['option_type'])

    df['expiry_date']    = pd.to_datetime(df['expiry_date']).dt.date
    df['underlying_ric'] = index_ric
    df['snapshot_date']  = snapshot_date

    valid_columns = ['underlying_ric', 'option_ric', 'expiry_date', 'strike', 'option_type', 'snapshot_date']
    df = df[valid_columns]

    return df


def run(snapshot_date: str | None = None) -> None:
    if snapshot_date is None:
        snapshot_date = _get_snapshot_date()

    print(f"fetch_option_chains_index | snapshot_date={snapshot_date}")

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT index_ric, option_chain_ric "
                "FROM pub_equity.index_overview "
                "WHERE option_chain_ric IS NOT NULL"
            )
        ).fetchall()

    print(f"  {len(rows)} index/es with option_chain_ric")

    for index_ric, option_chain_ric in rows:
        with engine.connect() as conn:
            if _check_exists(conn, index_ric, snapshot_date):
                print(f"  SKIP {index_ric} – already in DB")
                continue

        print(f"  Processing {index_ric} (chain: 0#{option_chain_ric})...")
        df = _fetch(index_ric, option_chain_ric, snapshot_date)

        if df is not None and not df.empty:
            df.to_sql(TABLE, engine, schema=SCHEMA, if_exists='append', index=False)
            print(f"  OK {index_ric} – {len(df)} rows written")
        else:
            print(f"  WARN {index_ric} – nothing to write")

        time.sleep(SLEEP_TIME)

    print("fetch_option_chains_index done.")


if __name__ == '__main__':
    run()
