"""
fetch_constituents.py
---------------------
Fetches index constituents for all indices in pub_config.stock_indices
where fetch_constituents = TRUE. Writes to pub_equity.index_constituents.

Two fetch methods:
  - Indices in HIST_CONSTITUENT_INDICES: TR.IndexConstituentRIC on the index RIC
    with SDate -> point-in-time composition (incl. delisted RICs with '^' suffix).
  - All others: '0#<index>' chain. Eikon ignores SDate here and always returns the
    current composition, so only the current snapshot is meaningful.

Snapshot-Datum: Constituents werden zum MONATSENDE gezogen (nicht zum Monatsersten).
Grund: S&P-Factsheets und Rebalancings beziehen sich auf Monats-/Quartalsenden; ein
Snapshot vom Monatsersten kennt die Aufnahmen/Abgaenge des Vormonats-Endes nicht.
Ein uebergebenes snapshot_date vom 1. wird auf den Vortag (= Monatsende) gemappt.
Konsumenten muessen den letzten Snapshot <= Datum verwenden, nicht Gleichheit.

Modes:
  - live:     current snapshot_date only
  - backfill: monthly snapshots from start_date to end_date, but only for indices
              in HIST_CONSTITUENT_INDICES (others: current snapshot only)

Idempotent: existing (index_ric, snapshot_date) pairs are skipped.
"""
import logging
import time
from datetime import date, datetime, timedelta

import eikon as ek
import pandas as pd
from dateutil.relativedelta import relativedelta
from sqlalchemy import text

from config import eikon_init  # noqa: F401
from config.db import engine
from config.logging_setup import setup_logging
from config.pipeline import run_finish, run_start
from modules.utils import eikon_fetch, normalize_permid

SCHEMA     = 'pub_equity'
TABLE      = 'index_constituents'
SLEEP_TIME = 0.5

# Indices for which TR.IndexConstituentRIC returns a complete point-in-time history
# (tested 2026-09: .FTSE incomplete, .STOXX50E/.AXJO error 414, .SSMI current only)
HIST_CONSTITUENT_INDICES = {'.SPX', '.DJI'}  # .DJI ab Dez. 2016 verfuegbar

logger = setup_logging('fetch_constituents')


def _get_snapshot_date() -> str:
    today = datetime.now()
    return today.replace(day=1).strftime('%Y-%m-%d')


def _check_exists(conn, index_ric: str, snapshot_date: str) -> bool:
    count = conn.execute(
        text(f"SELECT COUNT(*) FROM {SCHEMA}.{TABLE} "
             "WHERE index_ric = :ric AND snapshot_date = :date"),
        {'ric': index_ric, 'date': snapshot_date}
    ).scalar()
    return count > 0


def _fetch_chain(index_ric: str, snapshot_date: str) -> pd.DataFrame | None:
    result = eikon_fetch(
        ek.get_data,
        f'0#{index_ric}',
        ['TR.CommonName', 'TR.TickerSymbol', 'TR.OrganizationID'],
        {'SDate': snapshot_date},
    )
    if result is None:
        return None
    data, _ = result

    if data is None or data.empty:
        logger.warning('No data returned for %s', index_ric)
        return None

    return data.rename(columns={
        'Instrument':          'constituent_ric',
        'Company Common Name': 'company_name',
        'Ticker Symbol':       'symbol',
        'Organization PermID': 'org_permid',
    })


def _fetch_hist(index_ric: str, snapshot_date: str) -> pd.DataFrame | None:
    result = eikon_fetch(
        ek.get_data,
        index_ric,
        ['TR.IndexConstituentRIC', 'TR.IndexConstituentName'],
        {'SDate': snapshot_date},
    )
    if result is None:
        return None
    data, _ = result

    if data is None or data.empty or 'Constituent RIC' not in data.columns:
        logger.warning('No constituent data returned for %s @ %s', index_ric, snapshot_date)
        return None

    df = data.rename(columns={
        'Constituent RIC':  'constituent_ric',
        'Constituent Name': 'company_name',
    }).dropna(subset=['constituent_ric'])
    # Spin-off-Platzhalter (z.B. 'MILLROSE PROPERTIES CLS A ORD') kommen ohne RIC —
    # ein leerer RIC laesst jeden Folge-Batch mit Fehler 1422 scheitern
    empty = df['constituent_ric'].astype(str).str.strip() == ''
    if empty.any():
        logger.warning('%s @ %s: %d constituent(s) without RIC dropped: %s', index_ric,
                       snapshot_date, empty.sum(), df.loc[empty, 'company_name'].tolist())
        df = df[~empty]
    if df.empty:
        return None

    # Ticker + PermID separat (für delistete RICs liefert Eikon beim Ticker '')
    time.sleep(SLEEP_TIME)
    result = eikon_fetch(ek.get_data, df['constituent_ric'].tolist(),
                         ['TR.TickerSymbol', 'TR.OrganizationID'])
    extra = result[0] if (result is not None and result[0] is not None) else None
    if extra is not None and 'Ticker Symbol' in extra.columns:
        tickers = extra.set_index('Instrument')['Ticker Symbol']
        df['symbol'] = df['constituent_ric'].map(tickers[~tickers.index.duplicated()])
    else:
        df['symbol'] = None
    if extra is not None and 'Organization PermID' in extra.columns:
        permids = extra.set_index('Instrument')['Organization PermID']
        df['org_permid'] = df['constituent_ric'].map(permids[~permids.index.duplicated()])
    else:
        df['org_permid'] = None
    return df


def _fetch(index_ric: str, snapshot_date: str) -> pd.DataFrame | None:
    if index_ric in HIST_CONSTITUENT_INDICES:
        df = _fetch_hist(index_ric, snapshot_date)
    else:
        df = _fetch_chain(index_ric, snapshot_date)
    if df is None:
        return None

    df['index_ric']     = index_ric
    df['snapshot_date'] = snapshot_date
    for col in ('symbol', 'company_name', 'org_permid'):
        if col not in df.columns:
            df[col] = None
        df[col] = df[col].where(df[col].notna() & (df[col] != ''), other=None)

    # Eikon liefert die PermID numerisch -> '5001437821.0' wuerde die
    # Entdopplung je Unternehmen aushebeln
    df['org_permid'] = [normalize_permid(v) for v in df['org_permid']]

    valid_cols = ['index_ric', 'constituent_ric', 'symbol', 'company_name',
                  'snapshot_date', 'org_permid']
    return df[valid_cols].drop_duplicates(subset=['constituent_ric'])


MIN_OVERLAP = 0.8  # Mindest-Uebereinstimmung mit dem letzten Snapshot


def _plausible(conn, index_ric: str, snapshot_date: str, rics: list[str]) -> bool:
    """Schutz gegen Venue-Wechsel der 0#-Chain.

    Eikon liefert fuer '0#.STOXX50E' je nach Abruf andere Handelsplaetze desselben
    Unternehmens (ALVG.F statt ALVG.DE, ABI.BR1 statt ABI.BR). Solche Snapshots haben
    kaum Kursabdeckung und verfaelschen jede MCap-Rechnung. Deshalb: Abgleich mit dem
    letzten vorhandenen Snapshot; bei zu geringer Ueberschneidung nicht schreiben.
    """
    prev_date = conn.execute(
        text(f"SELECT MAX(snapshot_date) FROM {SCHEMA}.{TABLE} "
             "WHERE index_ric = :ric AND snapshot_date < :date"),
        {'ric': index_ric, 'date': snapshot_date}
    ).scalar()
    if prev_date is None:
        return True
    prev = {r[0] for r in conn.execute(
        text(f"SELECT constituent_ric FROM {SCHEMA}.{TABLE} "
             "WHERE index_ric = :ric AND snapshot_date = :date"),
        {'ric': index_ric, 'date': prev_date})}
    if not prev:
        return True
    overlap = len(prev & set(rics)) / len(prev)
    if overlap < MIN_OVERLAP:
        logger.error('%s @ %s: nur %.0f%% Ueberschneidung mit Snapshot %s '
                     '(vermutlich andere Handelsplatz-RICs) - nicht geschrieben. Beispiele neu: %s',
                     index_ric, snapshot_date, 100 * overlap, prev_date,
                     sorted(set(rics) - prev)[:5])
        return False
    return True


def _to_month_end(snapshot_date: str) -> str:
    """Monatserster -> Vortag (= Ende des Vormonats). Andere Daten bleiben unveraendert."""
    d = date.fromisoformat(snapshot_date)
    return (d - timedelta(days=1)).strftime('%Y-%m-%d') if d.day == 1 else snapshot_date


def _month_ends(start_date: str, end_date: str) -> list[str]:
    """Liste aller Monatsenden von start_date bis end_date (jeweils letzter Kalendertag).

    Nie in die Zukunft: Eikon liefert fuer ein Datum > heute einfach die aktuelle
    Zusammensetzung, was einen falsch datierten Snapshot erzeugen wuerde.
    """
    d   = date.fromisoformat(start_date).replace(day=1)
    end = min(date.fromisoformat(end_date), date.today())
    out = []
    while d <= end:
        me = d + relativedelta(months=1) - timedelta(days=1)
        if me <= end:
            out.append(me.strftime('%Y-%m-%d'))
        d += relativedelta(months=1)
    return out


def run(
    snapshot_date: str | None = None,
    mode: str = 'live',
    start_date: str | None = None,
    end_date: str | None = None,
) -> None:
    if snapshot_date is None:
        snapshot_date = _get_snapshot_date()

    logger.info('fetch_constituents | snapshot_date=%s | mode=%s', snapshot_date, mode)
    run_id = run_start('fetch_constituents', mode, snapshot_date)

    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT index_ric FROM pub_config.stock_indices "
                 "WHERE fetch_constituents = TRUE AND active = TRUE")
        ).fetchall()

    index_rics = [r[0] for r in rows]
    logger.info('%d index/es found: %s', len(index_rics), index_rics)

    snap_default = min(_to_month_end(snapshot_date), date.today().isoformat())
    jobs: list[tuple[str, str]] = []
    for ric in index_rics:
        if mode == 'backfill' and start_date and ric in HIST_CONSTITUENT_INDICES:
            dates = _month_ends(start_date, end_date or snap_default)
        else:
            dates = [snap_default]
        jobs.extend((ric, d) for d in dates)

    total_written = 0
    try:
        for ric, snap in jobs:
            with engine.connect() as conn:
                if _check_exists(conn, ric, snap):
                    logger.info('SKIP %s @ %s – already in DB', ric, snap)
                    continue

            logger.info('Processing %s @ %s ...', ric, snap)
            df = _fetch(ric, snap)

            if df is not None and not df.empty:
                with engine.connect() as conn:
                    if not _plausible(conn, ric, snap, df['constituent_ric'].tolist()):
                        continue
                df.to_sql(TABLE, engine, schema=SCHEMA, if_exists='append', index=False)
                total_written += len(df)
                logger.info('OK %s @ %s – %d rows written', ric, snap, len(df))
            else:
                logger.warning('WARN %s @ %s – nothing to write', ric, snap)

            time.sleep(SLEEP_TIME)

        run_finish(run_id, rows_written=total_written)
        logger.info('fetch_constituents done. rows_written=%d', total_written)
    except Exception as e:
        run_finish(run_id, error=str(e))
        raise


if __name__ == '__main__':
    run()
