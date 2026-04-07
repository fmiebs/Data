"""
fetch_option_prices.py
-----------------------
Fetches daily bid/ask prices for options in pub_options.options_chains.

Live mode (default):
  - Scope: expiry_date >= today
  - RIC:   '/' + option_ric   (live Eikon format)
  - Window per option: [MAX(price_date)+1 or today-1y, today]

Backfill mode:
  - Scope: expired options where NOT (bid_complete AND ask_complete)
  - Completeness: option is complete when a price within 30d of expiry exists
  - Per-RIC sdate: MAX(price_date)+1 if any prices exist, else expiry-18m
  - Batch window: min(sdate) across all RICs in the expiry group
  - Always queries both hist RIC (^-suffix) and live RIC (/prefix) per option
  - After each expiry batch: updates bid_complete / ask_complete flags

option_ric in DB is stored as Basisform (no /). Written to options_prices:
  - Active options (live mode): Basisform
  - Expired options (backfill mode): hist-RIC with ^-suffix (option_ric_hist)
"""
import logging
import time
from collections import defaultdict
from datetime import date, datetime, timedelta

import eikon as ek
import pandas as pd
from dateutil.relativedelta import relativedelta
from sqlalchemy import text, Table, MetaData
from sqlalchemy.dialects.postgresql import insert as pg_insert

from config import eikon_init  # noqa: F401
from config.db import engine
from config.logging_setup import setup_logging
from config.pipeline import run_finish, run_start
from modules.utils import eikon_fetch

SCHEMA          = 'pub_options'
TABLE           = 'options_prices'
SLEEP_TIME      = 0.5
BATCH_SIZE      = 50          # live options per Eikon call
HIST_BATCH_SIZE = 25          # expired options per Eikon call (heavier time-series)
HISTORY_MONTHS  = 18          # backfill window for expired options

logger = setup_logging('fetch_option_prices')

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
                    horizon: date) -> date | None:
    row = conn.execute(
        text(f"SELECT MAX(price_date) FROM {SCHEMA}.{TABLE} WHERE option_ric = :ric"),
        {'ric': option_ric}
    ).scalar()

    if row is None:
        snap  = _to_date(snapshot_date)
        start = snap - relativedelta(years=1)
    else:
        start = _to_date(row) + timedelta(days=1)

    return None if start > horizon else start


# ---------------------------------------------------------------------------
# Live fetch (active options, '/' + ric format)
# ---------------------------------------------------------------------------

def _fetch_batch(rics: list[str], start_date: date, end_date: date,
                 base_to_hist: dict[str, str] | None = None) -> pd.DataFrame | None:
    eikon_rics = ['/' + r for r in rics]
    result = eikon_fetch(
        ek.get_data,
        eikon_rics,
        ['TR.BIDPRICE.Date', 'TR.BIDPRICE', 'TR.ASKPRICE'],
        {'SDate': start_date.strftime('%Y-%m-%d'),
         'EDate': end_date.strftime('%Y-%m-%d')},
    )
    if result is None:
        return None
    data, _ = result

    if data is None or data.empty:
        return None

    date_col = next(
        (c for c in data.columns
         if c is not None and c.lower() in ('date', 'dates',
                                            'tr.bidprice date', 'tr.bidprice.date')),
        None
    )
    if date_col is None:
        logger.warning('No date column in live batch (columns: %s)', list(data.columns))
        return None

    instr_col = 'Instrument' if 'Instrument' in data.columns else data.columns[0]
    df = data.rename(columns={
        instr_col:   'option_ric',
        date_col:    'price_date',
        'Bid Price': 'price_bid',
        'Ask Price': 'price_ask',
    })

    if 'option_ric' not in df.columns:
        logger.warning("'Instrument' absent in batch (columns: %s)", list(data.columns))
        return None

    df['option_ric'] = df['option_ric'].astype(str).str.lstrip('/')
    if base_to_hist:
        df['option_ric'] = df['option_ric'].map(lambda r: base_to_hist.get(r, r))
    df['price_date'] = pd.to_datetime(df['price_date']).dt.date
    df = df.dropna(subset=['price_date'])

    for col in ('price_bid', 'price_ask'):
        if col not in df.columns:
            df[col] = None
        else:
            df[col] = df[col].where(df[col].notna(), other=None)

    return df[['option_ric', 'price_date', 'price_bid', 'price_ask']]


# ---------------------------------------------------------------------------
# Historical fetch (expired options, ^-suffix RICs, no '/')
# ---------------------------------------------------------------------------

def _fetch_batch_hist(hist_rics: list[str], hist_to_base: dict[str, str],
                      start_date: date, end_date: date,
                      retries: int = 0) -> pd.DataFrame | None:
    # Query both hist RICs (^-suffix) and live RICs (/prefix) simultaneously.
    # option_ric in result is always the hist-form (^-suffix) for disambiguation
    # across 10-year RIC cycles (1-digit year indices).
    base_rics    = list(hist_to_base.values())
    base_to_hist = {v: k for k, v in hist_to_base.items()}
    live_rics    = ['/' + r for r in base_rics]
    live_to_base = {'/' + r: r for r in base_rics}
    combined     = hist_rics + live_rics

    try:
        data, _ = ek.get_data(
            combined,
            ['TR.BIDPRICE.Date', 'TR.BIDPRICE', 'TR.ASKPRICE'],
            {'SDate': start_date.strftime('%Y-%m-%d'),
             'EDate': end_date.strftime('%Y-%m-%d')}
        )
    except Exception as e:
        if ('429' in str(e) or 'limit' in str(e).lower()) and retries < 5:
            logger.warning('Rate limit – waiting 1h (retry %d/5)', retries + 1)
            time.sleep(3600)
            return _fetch_batch_hist(hist_rics, hist_to_base, start_date, end_date,
                                     retries + 1)
        logger.error('_fetch_batch_hist error: %s', e)
        return None

    if data is None or data.empty:
        return None

    date_col = next(
        (c for c in data.columns
         if c is not None and c.lower() in ('date', 'dates',
                                            'tr.bidprice date', 'tr.bidprice.date')),
        None
    )
    if date_col is None:
        return None

    instr_col = 'Instrument' if 'Instrument' in data.columns else data.columns[0]
    df = data.rename(columns={
        instr_col:   'raw_ric',
        date_col:    'price_date',
        'Bid Price': 'price_bid',
        'Ask Price': 'price_ask',
    })

    def _to_hist(raw: str) -> str | None:
        if raw in hist_to_base:
            return raw  # already hist form
        if raw in live_to_base:
            base = live_to_base[raw]
            return base_to_hist.get(base)
        # fallback: strip / prefix, check if base is known
        stripped = raw.lstrip('/').split('^')[0]
        return base_to_hist.get(stripped)

    df['option_ric'] = df['raw_ric'].map(_to_hist)
    df = df.dropna(subset=['option_ric'])
    df = df.drop(columns=['raw_ric'])

    df['price_date'] = pd.to_datetime(df['price_date']).dt.date
    df = df.dropna(subset=['price_date'])

    for col in ('price_bid', 'price_ask'):
        if col not in df.columns:
            df[col] = None
        else:
            df[col] = df[col].where(df[col].notna(), other=None)

    df = df[df['price_bid'].notna() | df['price_ask'].notna()]
    return df[['option_ric', 'price_date', 'price_bid', 'price_ask']]


# ---------------------------------------------------------------------------
# Write helper
# ---------------------------------------------------------------------------

def _write(df: pd.DataFrame) -> int:
    df = df.drop_duplicates(subset=['option_ric', 'price_date'])
    records = df.to_dict(orient='records')
    tbl  = _get_prices_table()
    stmt = pg_insert(tbl).values(records).on_conflict_do_update(
        index_elements=['option_ric', 'price_date'],
        set_={
            'price_bid': text(
                'COALESCE(EXCLUDED.price_bid, options_prices.price_bid)'
            ),
            'price_ask': text(
                'COALESCE(EXCLUDED.price_ask, options_prices.price_ask)'
            ),
        }
    )
    with engine.begin() as conn:
        result = conn.execute(stmt)
    return result.rowcount


# ---------------------------------------------------------------------------
# Bulk delta-check helper (avoid N individual queries)
# ---------------------------------------------------------------------------

def _load_max_price_dates(option_rics: list[str]) -> dict[str, date]:
    """Returns {option_ric: max_price_date} for rics that have any prices."""
    if not option_rics:
        return {}
    with engine.connect() as conn:
        rows = conn.execute(text(
            f"SELECT option_ric, MAX(price_date) "
            f"FROM {SCHEMA}.{TABLE} "
            f"WHERE option_ric = ANY(:rics) "
            f"GROUP BY option_ric"
        ), {'rics': option_rics}).fetchall()
    return {r[0]: _to_date(r[1]) for r in rows}


def _update_complete_flags(option_ric_hists: list[str]) -> None:
    """Update bid_complete / ask_complete on options_chains.

    Joins via option_ric_hist because expired prices are stored as hist-RIC.
    Complete = at least one price within 30 calendar days before expiry_date.
    """
    if not option_ric_hists:
        return
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE pub_options.options_chains oc "
            "SET bid_complete = EXISTS("
            "    SELECT 1 FROM pub_options.options_prices op "
            "    WHERE op.option_ric = oc.option_ric_hist "
            "      AND op.price_bid  IS NOT NULL "
            "      AND op.price_date >= oc.expiry_date - 30"
            "), "
            "ask_complete = EXISTS("
            "    SELECT 1 FROM pub_options.options_prices op "
            "    WHERE op.option_ric = oc.option_ric_hist "
            "      AND op.price_ask  IS NOT NULL "
            "      AND op.price_date >= oc.expiry_date - 30"
            ") "
            "WHERE oc.option_ric_hist = ANY(:rics)"
        ), {'rics': option_ric_hists})


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------

def run(
    snapshot_date: str | None = None,
    mode: str = 'live',
    start_date: str | None = None,
    end_date: str | None = None,
    underlyings: list[str] | None = None,
) -> None:
    """
    Parameters
    ----------
    snapshot_date : str, optional
        YYYY-MM-DD of first day of current month. Defaults to today's month.
    mode : str
        'live' (default) or 'backfill'.
    start_date : str, optional
        Backfill only: override start date for live options (YYYY-MM-DD).
    end_date : str, optional
        Override end date (YYYY-MM-DD). Defaults to today.
    underlyings : list[str], optional
        Backfill only: limit expired-option processing to these underlying RICs.
        None = all expired options.
    """
    if snapshot_date is None:
        snapshot_date = _get_snapshot_date()

    today: date   = date.today()
    horizon: date = _to_date(end_date) if end_date else today

    logger.info('fetch_option_prices | snapshot_date=%s | mode=%s | end_date=%s | '
                'underlyings=%s', snapshot_date, mode, horizon, underlyings)
    run_id = run_start('fetch_option_prices', mode, snapshot_date)

    total_written = 0

    try:
        # ----------------------------------------------------------------
        # EXPIRED options  (backfill mode only)
        # Skip RICs where bid_complete AND ask_complete are already TRUE.
        # Per-RIC sdate: MAX(price_date)+1 if any prices exist, else expiry-18m.
        # Batch window per expiry group: min(sdate) across all RICs in group.
        # Always queries both hist RIC (^-suffix) and live RIC (/prefix).
        # Updates completeness flags after each expiry batch.
        # ----------------------------------------------------------------
        if mode == 'backfill':
            logger.info('Backfill: fetching expired options via hist RICs...')

            und_filter = (
                "AND underlying_ric = ANY(:underlyings) " if underlyings else ""
            )
            with engine.connect() as conn:
                exp_rows = conn.execute(text(
                    "SELECT oc.option_ric, oc.option_ric_hist, oc.expiry_date, "
                    "       MAX(op.price_date) AS max_price_date "
                    "FROM pub_options.options_chains oc "
                    "LEFT JOIN pub_options.options_prices op "
                    "    ON op.option_ric = oc.option_ric_hist "
                    f"WHERE oc.expiry_date < CURRENT_DATE "
                    f"  AND oc.option_ric_hist IS NOT NULL "
                    f"  AND NOT (COALESCE(oc.bid_complete, FALSE) "
                    f"           AND COALESCE(oc.ask_complete, FALSE)) "
                    f"  {und_filter}"
                    "GROUP BY oc.option_ric, oc.option_ric_hist, oc.expiry_date "
                    "ORDER BY oc.expiry_date, oc.option_ric"
                ), ({'underlyings': underlyings} if underlyings else {})).fetchall()

            logger.info('%d expired option_rics in scope (%d with no prices, %d resuming)',
                        len(exp_rows),
                        sum(1 for r in exp_rows if r[3] is None),
                        sum(1 for r in exp_rows if r[3] is not None))

            # Group by expiry_date; compute per-RIC sdate
            # tuple: (option_ric, option_ric_hist, sdate)
            exp_by_expiry: dict[date, list[tuple[str, str, date]]] = defaultdict(list)
            for option_ric, option_ric_hist, expiry, max_pd in exp_rows:
                expiry_d   = _to_date(expiry)
                full_start = expiry_d - relativedelta(months=HISTORY_MONTHS)
                if max_pd is not None:
                    sdate = _to_date(max_pd) + timedelta(days=1)
                    if sdate > expiry_d:
                        # already covered past expiry — just refresh flags
                        _update_complete_flags([option_ric])
                        continue
                else:
                    sdate = full_start
                exp_by_expiry[expiry_d].append((option_ric, option_ric_hist, sdate))

            n_exp = sum(len(v) for v in exp_by_expiry.values())
            logger.info('%d expired options to fetch across %d expiry dates',
                        n_exp, len(exp_by_expiry))

            retry_queue: list[tuple] = []  # (batch_hist, batch_h2b, min_sdate, expiry_d)

            for expiry_d in sorted(exp_by_expiry.keys()):
                triples      = exp_by_expiry[expiry_d]
                hist_to_base = {hist: base for base, hist, _ in triples}
                hist_rics    = list(hist_to_base.keys())
                all_base     = [base for base, _, _ in triples]
                # Use earliest sdate in group — captures all missing windows
                min_sdate    = min(sdate for _, _, sdate in triples)
                batch_written = 0

                for i in range(0, len(hist_rics), HIST_BATCH_SIZE):
                    batch_hist = hist_rics[i:i + HIST_BATCH_SIZE]
                    batch_h2b  = {h: hist_to_base[h] for h in batch_hist}
                    df = _fetch_batch_hist(batch_hist, batch_h2b, min_sdate, expiry_d)
                    if df is not None and not df.empty:
                        batch_written += _write(df)
                    elif df is None:
                        retry_queue.append((batch_hist, batch_h2b, min_sdate, expiry_d))
                    time.sleep(SLEEP_TIME)

                total_written += batch_written
                logger.info('  expired %s | %d opts | %d rows written',
                            expiry_d, len(hist_rics), batch_written)

                # Refresh completeness flags for every RIC in this batch
                all_hist = [hist for _, hist, _ in triples]
                _update_complete_flags(all_hist)

            # ---- Retry failed batches ----
            if retry_queue:
                logger.info('Retrying %d failed batch(es)...', len(retry_queue))
                for batch_hist, batch_h2b, min_sdate, expiry_d in retry_queue:
                    df = _fetch_batch_hist(batch_hist, batch_h2b, min_sdate, expiry_d)
                    if df is not None and not df.empty:
                        written = _write(df)
                        total_written += written
                        logger.info('  retry OK: expiry=%s %d rows written',
                                    expiry_d, written)
                        _update_complete_flags(list(batch_h2b.keys()))
                    elif df is None:
                        logger.warning('  retry failed again: expiry=%s %d RICs – skipping',
                                       expiry_d, len(batch_hist))
                    time.sleep(SLEEP_TIME)

        # ----------------------------------------------------------------
        # LIVE / active options
        # ----------------------------------------------------------------
        live_und_filter = "AND underlying_ric = ANY(:underlyings) " if underlyings else ""
        live_params     = {'underlyings': underlyings} if underlyings else {}
        with engine.connect() as conn:
            if mode == 'backfill' and start_date:
                live_rows = conn.execute(
                    text("SELECT DISTINCT option_ric, option_ric_hist "
                         "FROM pub_options.options_chains "
                         f"WHERE TRUE {live_und_filter}"),
                    live_params
                ).fetchall()
            else:
                live_rows = conn.execute(
                    text("SELECT DISTINCT option_ric, option_ric_hist "
                         "FROM pub_options.options_chains "
                         f"WHERE expiry_date >= CURRENT_DATE {live_und_filter}"),
                    live_params
                ).fetchall()

        # Map base RIC → hist RIC; fall back to base if option_ric_hist is NULL
        base_to_hist_live = {r[0]: (r[1] if r[1] else r[0]) for r in live_rows}
        all_rics = list(base_to_hist_live.keys())
        logger.info('%d live option RICs in scope', len(all_rics))

        groups: dict[date, list[str]] = defaultdict(list)
        with engine.connect() as conn:
            for base_ric in all_rics:
                hist_ric = base_to_hist_live[base_ric]
                if mode == 'backfill' and start_date:
                    s = _to_date(start_date)
                else:
                    s = _get_start_date(conn, hist_ric, snapshot_date, horizon)
                if s is not None:
                    groups[s].append(base_ric)

        logger.info('%d live RICs to fetch across %d start-date group(s)',
                    sum(len(v) for v in groups.values()), len(groups))

        for s_date, rics in sorted(groups.items()):
            logger.info('Group start_date=%s | %d RICs', s_date, len(rics))
            batch_base_to_hist = {r: base_to_hist_live[r] for r in rics}
            for i in range(0, len(rics), BATCH_SIZE):
                batch = rics[i:i + BATCH_SIZE]
                df    = _fetch_batch(batch, s_date, horizon, batch_base_to_hist)

                if df is not None and not df.empty:
                    total_written += _write(df)
                    logger.info('  batch %d: %d rows written',
                                i // BATCH_SIZE + 1, len(df))
                else:
                    logger.info('  batch %d: no data', i // BATCH_SIZE + 1)

                time.sleep(SLEEP_TIME)

        run_finish(run_id, rows_written=total_written)
        logger.info('fetch_option_prices done. rows_written=%d', total_written)

    except Exception as e:
        run_finish(run_id, error=str(e))
        raise


if __name__ == '__main__':
    run()
