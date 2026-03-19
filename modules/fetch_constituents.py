import time
from datetime import datetime

import eikon as ek
import pandas as pd
from sqlalchemy import text

from config.db import engine
from config import eikon_init  # noqa: F401 – sets Eikon app key on import


SCHEMA = 'pub_equity'
TABLE = 'index_constituents'
SLEEP_TIME = 0.5


def _get_snapshot_date() -> str:
    today = datetime.now()
    return today.replace(day=1).strftime('%Y-%m-%d')


def _check_exists(conn, index_ric: str, snapshot_date: str) -> bool:
    query = text(
        f"SELECT COUNT(*) FROM {SCHEMA}.{TABLE} "
        "WHERE index_ric = :ric AND snapshot_date = :date"
    )
    count = conn.execute(query, {"ric": index_ric, "date": snapshot_date}).scalar()
    return count > 0


def _fetch(index_ric: str, snapshot_date: str) -> pd.DataFrame | None:
    try:
        data, err = ek.get_data(
            f'0#{index_ric}',
            ['TR.CommonName', 'TR.TickerSymbol'],
            {'SDate': snapshot_date}
        )
    except Exception as e:
        if '429' in str(e) or 'limit' in str(e).lower():
            print(f"  Rate limit hit for {index_ric} – waiting 63s...")
            time.sleep(63)
            return _fetch(index_ric, snapshot_date)
        print(f"  ERROR fetching {index_ric}: {e}")
        return None

    if data is None or data.empty:
        print(f"  No data returned for {index_ric}")
        return None

    df = data.rename(columns={
        'Instrument':           'constituent_ric',
        'Company Common Name':  'company_name',
        'Ticker Symbol':        'symbol',
    })
    df['index_ric']     = index_ric
    df['snapshot_date'] = snapshot_date

    valid_columns = ['index_ric', 'constituent_ric', 'symbol', 'company_name', 'snapshot_date']
    df = df[valid_columns].dropna(subset=['constituent_ric'])

    return df


def run(snapshot_date: str | None = None) -> None:
    if snapshot_date is None:
        snapshot_date = _get_snapshot_date()

    print(f"fetch_constituents | snapshot_date={snapshot_date}")

    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT index_ric FROM pub_equity.index_overview")
        ).fetchall()

    index_rics = [r[0] for r in rows]
    print(f"  {len(index_rics)} index/es found: {index_rics}")

    for ric in index_rics:
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

    print("fetch_constituents done.")


if __name__ == '__main__':
    run()
