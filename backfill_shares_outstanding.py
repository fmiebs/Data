"""
backfill_shares_outstanding.py
-------------------------------
One-time backfill: fetches TR.SharesOutstanding for all SPX constituents
and updates NULL shares_outstanding in pub_equity.prices_daily.

Run once after the column-mapping bug fix in fetch_prices_daily.py.
"""
import time
from datetime import date

import eikon as ek
import pandas as pd
from sqlalchemy import text

from config.db import engine
from config import eikon_init  # noqa: F401

BATCH_SIZE = 20
SLEEP_TIME = 0.5
HISTORY_YEARS = 10


def _fetch_shares(rics: list[str], start_date: date, end_date: date,
                  _retries: int = 0) -> pd.DataFrame | None:
    try:
        data, err = ek.get_data(
            rics,
            ['TR.SharesOutstanding.Date', 'TR.SharesOutstanding'],
            {'SDate': start_date.strftime('%Y-%m-%d'),
             'EDate': end_date.strftime('%Y-%m-%d')}
        )
    except Exception as e:
        if '429' in str(e) or 'limit' in str(e).lower():
            if _retries >= 5:
                print(f"  ERROR {rics[0]} – max retries, skipping")
                return None
            print(f"  Rate limit – waiting 63s (attempt {_retries + 1}/5)...")
            time.sleep(63)
            return _fetch_shares(rics, start_date, end_date, _retries + 1)
        print(f"  ERROR {rics[0]}: {e}")
        return None

    if data is None or data.empty:
        return None

    date_col = next(
        (c for c in data.columns if c and 'date' in c.lower()),
        None
    )
    shares_col = next(
        (c for c in data.columns if c and 'outstanding' in c.lower()),
        None
    )
    if date_col is None or shares_col is None:
        print(f"  WARN: unexpected columns {list(data.columns)} – skipping")
        return None

    df = data.rename(columns={
        'Instrument': 'ric',
        date_col:     'trade_date',
        shares_col:   'shares_outstanding',
    })

    df['trade_date'] = pd.to_datetime(df['trade_date']).dt.date
    df = df.dropna(subset=['trade_date', 'shares_outstanding'])
    if df.empty:
        return None

    df['shares_outstanding'] = df['shares_outstanding'].astype('Int64')
    return df[['ric', 'trade_date', 'shares_outstanding']]


def main():
    today = date.today()
    start = today.replace(year=today.year - HISTORY_YEARS)

    print(f"backfill_shares_outstanding | range={start} – {today}")

    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT DISTINCT constituent_ric "
                 "FROM pub_equity.index_constituents WHERE index_ric = '.SPX'")
        ).fetchall()

    all_rics = [r[0] for r in rows]
    print(f"  {len(all_rics)} RICs in universe")

    total_updated = 0

    for i in range(0, len(all_rics), BATCH_SIZE):
        batch = all_rics[i:i + BATCH_SIZE]
        df = _fetch_shares(batch, start, today)

        if df is not None and not df.empty:
            # Bulk UPDATE via temp table
            records = df.to_dict(orient='records')
            with engine.begin() as conn:
                conn.execute(text("""
                    CREATE TEMP TABLE _shares_tmp (
                        ric VARCHAR(32),
                        trade_date DATE,
                        shares_outstanding BIGINT
                    ) ON COMMIT DROP
                """))
                conn.execute(
                    text("INSERT INTO _shares_tmp VALUES (:ric, :trade_date, :shares_outstanding)"),
                    records
                )
                result = conn.execute(text("""
                    UPDATE pub_equity.prices_daily pd
                    SET    shares_outstanding = t.shares_outstanding
                    FROM   _shares_tmp t
                    WHERE  pd.ric = t.ric
                      AND  pd.trade_date = t.trade_date
                      AND  pd.shares_outstanding IS NULL
                """))
                updated = result.rowcount
            total_updated += updated
            print(f"  batch {i // BATCH_SIZE + 1}: {updated} rows updated ({batch[0]}…)")
        else:
            print(f"  batch {i // BATCH_SIZE + 1}: no data ({batch[0]}…)")

        time.sleep(SLEEP_TIME)

    print(f"backfill_shares_outstanding done. Total rows updated: {total_updated:,}")


if __name__ == '__main__':
    main()
