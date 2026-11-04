"""
fetch_prices_daily.py
---------------------
Fetches daily OHLCV prices for all constituents of indices where
fetch_constituent_prices=TRUE in pub_config.stock_indices.
Writes to pub_equity.stock_prices_daily.

Delta logic per RIC:
  - No existing rows  -> start_date = today - HISTORY_YEARS
  - Existing rows     -> start_date = MAX(trade_date) + 1 day
  - start_date > today -> skip

Backfill (start_date gesetzt): RICs, deren Historie bereits bis start_date
zurueckreicht, werden nur ab MAX(trade_date)+1 ergaenzt; alle anderen ab start_date.

market_cap kommt aus TR.CompanyMarketCap. NICHT close_price * shares_outstanding
verwenden: Preise sind splitbereinigt, shares_outstanding nicht -> MCap vor jedem
Split zu niedrig (AMZN 2021: 85 Mrd. statt 1.691 Mrd.).

Waehrungen: crncy = Kurswaehrung (LSE: 'GBp' = Pence!), mcap_crncy = Waehrung der
MCap (LSE: 'GBP'). Eikon liefert beide als Spalte 'Currency' — Zuordnung ueber die
jeweils vorhergehende Wertspalte (_dedupe_currency_cols).
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
TABLE         = 'stock_prices_daily'
SLEEP_TIME    = 0.5
BATCH_SIZE    = 10
HISTORY_YEARS = 10

logger = setup_logging('fetch_prices_daily')

_prices_table = None


def _get_prices_table():
    global _prices_table
    if _prices_table is None:
        _prices_table = Table(TABLE, MetaData(), schema=SCHEMA, autoload_with=engine)
    return _prices_table


def _get_start_date(conn, ric: str, override_start: date | None = None) -> date | None:
    first, last = conn.execute(
        text(f"SELECT MIN(trade_date), MAX(trade_date) FROM {SCHEMA}.{TABLE} WHERE ric = :ric"),
        {'ric': ric}
    ).fetchone()
    today = date.today()
    if override_start is not None and (first is None or first > override_start):
        return override_start
    start = (today.replace(year=today.year - HISTORY_YEARS) if last is None
             else last + timedelta(days=1))
    return None if start > today else start


def _dedupe_currency_cols(columns: list) -> list:
    """'Currency' kommt je Wertfeld einmal — an die vorhergehende Wertspalte binden."""
    out, prev = [], None
    for c in columns:
        if c == 'Currency' and prev is not None:
            out.append(f'{prev} Currency')
        else:
            out.append(c)
            prev = c
    return out


def _fetch_batch(rics: list[str], start_date: date, end_date: date) -> pd.DataFrame | None:
    result = eikon_fetch(
        ek.get_data,
        rics,
        ['TR.PriceClose.Date', 'TR.PriceClose', 'TR.PriceClose.currency',
         'TR.PriceOpen', 'TR.PriceHigh', 'TR.PriceLow', 'TR.Volume',
         'TR.CompanyMarketCap', 'TR.CompanyMarketCap.currency'],
        {'SDate': start_date.strftime('%Y-%m-%d'),
         'EDate': end_date.strftime('%Y-%m-%d'),
         'Frq':   'D'},
    )
    if result is None:
        return None
    data, _ = result

    if data is None or data.empty:
        return None

    date_col = next(
        (c for c in data.columns
         if c is not None and c.lower() in ('date', 'dates',
                                            'tr.priceclose date', 'tr.priceclose.date')),
        None
    )
    if date_col is None:
        logger.warning('No date column in batch (cols: %s)', list(data.columns))
        return None

    data.columns = _dedupe_currency_cols(list(data.columns))

    df = data.rename(columns={
        'Instrument':  'ric',
        date_col:      'trade_date',
        'Price Close': 'close_price',
        'Price Open':  'open_price',
        'Price High':  'high_price',
        'Price Low':   'low_price',
        'Volume':      'volume',
        'Price Close Currency':        'crncy',
        'Company Market Cap':          'market_cap',
        'Company Market Cap Currency': 'mcap_crncy',
    })

    if 'ric' not in df.columns:
        logger.warning("'Instrument' absent (cols: %s)", list(data.columns))
        return None

    df['trade_date'] = pd.to_datetime(df['trade_date']).dt.date
    df = df.dropna(subset=['trade_date'])

    for col in ('close_price', 'open_price', 'high_price', 'low_price', 'volume',
                'market_cap', 'crncy', 'mcap_crncy'):
        if col not in df.columns:
            df[col] = None
        else:
            df[col] = df[col].astype(object).where(
                df[col].notna() & (df[col].astype(str).str.strip() != ''), other=None)

    # crncy ist NOT NULL — 'LOC' als Fallback wenn Eikon keine Waehrung liefert
    df['crncy'] = [c if c else 'LOC' for c in df['crncy']]
    return df[['ric', 'trade_date', 'close_price', 'open_price',
               'high_price', 'low_price', 'volume', 'crncy',
               'market_cap', 'mcap_crncy']]


def run(
    snapshot_date: str | None = None,  # noqa: ARG001
    mode: str = 'live',
    start_date: str | None = None,
    end_date: str | None = None,
) -> None:
    end = date.fromisoformat(end_date) if end_date else date.today()
    override = date.fromisoformat(start_date) if (mode == 'backfill' and start_date) else None

    logger.info('fetch_prices_daily | mode=%s | end_date=%s', mode, end)
    run_id = run_start('fetch_prices_daily', mode, snapshot_date)

    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT DISTINCT ic.constituent_ric
            FROM pub_equity.index_constituents ic
            JOIN pub_config.stock_indices si ON si.index_ric = ic.index_ric
            WHERE si.fetch_constituent_prices = TRUE
              AND si.active = TRUE
        """)).fetchall()

    all_rics = [r[0] for r in rows]
    logger.info('%d RICs in universe', len(all_rics))

    ric_starts: dict[str, date] = {}
    with engine.connect() as conn:
        for ric in all_rics:
            s = _get_start_date(conn, ric, override)
            if s is not None:
                ric_starts[ric] = s

    # nach Startdatum sortieren, damit ein Batch nicht die Historie anderer RICs mitzieht
    rics_to_fetch = sorted(ric_starts, key=ric_starts.get)
    logger.info('%d RICs to fetch', len(rics_to_fetch))

    tbl           = _get_prices_table()
    total_written = 0

    try:
        for i in range(0, len(rics_to_fetch), BATCH_SIZE):
            batch_rics  = rics_to_fetch[i:i + BATCH_SIZE]
            batch_start = min(ric_starts[r] for r in batch_rics)

            df = _fetch_batch(batch_rics, batch_start, end)

            if df is not None and not df.empty:
                df = df.drop_duplicates(subset=['ric', 'trade_date'])
                records = df.to_dict(orient='records')
                stmt = pg_insert(tbl).values(records).on_conflict_do_update(
                    index_elements=['ric', 'trade_date'],
                    set_={
                        'close_price': pg_insert(tbl).excluded.close_price,
                        'open_price':  pg_insert(tbl).excluded.open_price,
                        'high_price':  pg_insert(tbl).excluded.high_price,
                        'low_price':   pg_insert(tbl).excluded.low_price,
                        'volume':      pg_insert(tbl).excluded.volume,
                        'crncy':       pg_insert(tbl).excluded.crncy,
                        'market_cap':  pg_insert(tbl).excluded.market_cap,
                        'mcap_crncy':  pg_insert(tbl).excluded.mcap_crncy,
                    }
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
        logger.info('fetch_prices_daily done. rows_written=%d', total_written)
    except Exception as e:
        run_finish(run_id, error=str(e))
        raise


if __name__ == '__main__':
    run()
