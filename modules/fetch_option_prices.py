import time
from collections import defaultdict
from datetime import datetime, date, timedelta
from dateutil.relativedelta import relativedelta

import eikon as ek
import pandas as pd
from sqlalchemy import text, Table, MetaData
from sqlalchemy.dialects.postgresql import insert as pg_insert

from config.db import engine
from config import eikon_init  # noqa: F401 – sets Eikon app key on import


SCHEMA = 'pub_options'
TABLE = 'options_prices'
SLEEP_TIME = 0.5
BATCH_SIZE = 50

_prices_table = None

def _get_prices_table():
    global _prices_table
    if _prices_table is None:
        _prices_table = Table(TABLE, MetaData(), schema=SCHEMA, autoload_with=engine)
    return _prices_table


def _get_snapshot_date() -> str:
    today = datetime.now()
    return today.replace(day=1).strftime('%Y-%m-%d')


def _to_date(val) -> date:
    if isinstance(val, date):
        return val
    return pd.to_datetime(val).date()


def _get_start_date(conn, option_ric: str, snapshot_date: str,
                    end_date: date | None = None) -> date | None:
    """
    Delta logic:
    - No existing prices → start_date = snapshot_date - 1 year
    - Existing prices    → start_date = MAX(price_date) + 1 day
    - start_date > end_date → None (already up to date, skip)
    """
    horizon = end_date if end_date is not None else date.today()

    row = conn.execute(
        text(
            f"SELECT MAX(price_date) FROM {SCHEMA}.{TABLE} "
            "WHERE option_ric = :ric"
        ),
        {"ric": option_ric}
    ).scalar()

    if row is None:
        snap = _to_date(snapshot_date)
        start = snap - relativedelta(years=1)
    else:
        start = _to_date(row) + timedelta(days=1)

    if start > horizon:
        return None
    return start


def _fetch_batch(rics: list[str], start_date: date, end_date: date, _retries: int = 0) -> pd.DataFrame | None:
    try:
        data, err = ek.get_data(
            rics,
            ['TR.BIDPRICE.Date', 'TR.BIDPRICE', 'TR.ASKPRICE'],
            {'SDate': start_date.strftime('%Y-%m-%d'),
             'EDate': end_date.strftime('%Y-%m-%d')}
        )
    except Exception as e:
        if '429' in str(e) or 'limit' in str(e).lower():
            if _retries >= 5:
                print(f"  ERROR batch starting {rics[0]} – max retries reached, skipping")
                return None
            print(f"  Rate limit hit – waiting 63s (attempt {_retries + 1}/5)...")
            time.sleep(63)
            return _fetch_batch(rics, start_date, end_date, _retries + 1)
        print(f"  ERROR fetching batch starting {rics[0]}: {e}")
        return None

    if data is None or data.empty:
        return None

    # Eikon can return the date column under different names depending on the batch
    date_col = next(
        (c for c in data.columns if c is not None and c.lower() in ('date', 'dates', 'tr.bidprice date', 'tr.bidprice.date')),
        None
    )
    if date_col is None:
        print(f"  WARN: no date column found in batch (columns: {list(data.columns)}) – skipping")
        return None

    df = data.rename(columns={
        'Instrument': 'option_ric',
        date_col:     'price_date',
        'Bid Price':  'price_bid',
        'Ask Price':  'price_ask',
    })

    if 'option_ric' not in df.columns:
        print(f"  WARN: 'Instrument' column absent in batch (columns: {list(data.columns)}) – skipping")
        return None

    df['price_date'] = pd.to_datetime(df['price_date']).dt.date
    df = df.dropna(subset=['price_date'])

    # Keep bid/ask as-is – NaN → None (PostgreSQL NULL) to preserve delta coverage
    # Guard against batches where Eikon returns no bid/ask columns
    if 'price_bid' not in df.columns:
        print(f"  WARN: 'Bid Price' column absent in batch – setting price_bid to NULL")
        df['price_bid'] = None
    else:
        df['price_bid'] = df['price_bid'].where(df['price_bid'].notna(), other=None)
    if 'price_ask' not in df.columns:
        print(f"  WARN: 'Ask Price' column absent in batch – setting price_ask to NULL")
        df['price_ask'] = None
    else:
        df['price_ask'] = df['price_ask'].where(df['price_ask'].notna(), other=None)

    valid_columns = ['option_ric', 'price_date', 'price_bid', 'price_ask']
    return df[valid_columns]


def run(snapshot_date: str | None = None,
        end_date: str | date | None = None) -> None:
    if snapshot_date is None:
        snapshot_date = _get_snapshot_date()

    horizon: date = _to_date(end_date) if end_date is not None else date.today()

    cutoff_date = (_to_date(snapshot_date) + relativedelta(months=18)).strftime('%Y-%m-%d')

    print(f"fetch_option_prices | snapshot_date={snapshot_date} | cutoff={cutoff_date} | end_date={horizon}")

    # 1. Load relevant option RICs
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT DISTINCT option_ric "
                "FROM pub_options.options_chains "
                "WHERE expiry_date <= :cutoff AND snapshot_date = :snap"
            ),
            {"cutoff": cutoff_date, "snap": snapshot_date}
        ).fetchall()

    all_rics = [r[0] for r in rows]
    print(f"  {len(all_rics)} option RICs in scope")

    # 2. Delta logic: determine start_date per RIC, group by start_date for batching
    groups: dict[date, list[str]] = defaultdict(list)

    with engine.connect() as conn:
        for ric in all_rics:
            start = _get_start_date(conn, ric, snapshot_date, end_date=horizon)
            if start is None:
                continue
            groups[start].append(ric)

    print(f"  {sum(len(v) for v in groups.values())} RICs to fetch across {len(groups)} start-date group(s)")

    # 3. Fetch & write per start-date group, batched
    for start_date, rics in sorted(groups.items()):
        print(f"  Group start_date={start_date} | {len(rics)} RICs")
        for i in range(0, len(rics), BATCH_SIZE):
            batch = rics[i:i + BATCH_SIZE]
            df = _fetch_batch(batch, start_date, horizon)

            if df is not None and not df.empty:
                # Deduplicate within the batch itself before writing
                df = df.drop_duplicates(subset=['option_ric', 'price_date'])
                records = df.to_dict(orient='records')
                stmt = pg_insert(
                    _get_prices_table()
                ).values(records).on_conflict_do_nothing(
                    index_elements=['option_ric', 'price_date']
                )
                with engine.begin() as conn:
                    conn.execute(stmt)
                print(f"    batch {i // BATCH_SIZE + 1}: {len(df)} rows written")
            else:
                print(f"    batch {i // BATCH_SIZE + 1}: no data")

            time.sleep(SLEEP_TIME)

    print("fetch_option_prices done.")


if __name__ == '__main__':
    run()
