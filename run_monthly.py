"""
Monthly pipeline orchestrator.
Runs all modules sequentially with mode='live'.
After fetching chains, validates call/put completeness and retries once if needed.
"""
import logging
from datetime import datetime

from sqlalchemy import text

from config.db import engine
from config.logging_setup import setup_logging

logger = setup_logging('run_monthly', log_file='run_monthly.log')

CHAIN_RATIO_TOLERANCE = 0.25  # flag if |calls/puts - 1| > 25%


def get_snapshot_date() -> str:
    today = datetime.now()
    return today.replace(day=1).strftime('%Y-%m-%d')


def _run_chains(snapshot_date: str) -> None:
    from modules.fetch_option_chains_index import run as run_index
    from modules.fetch_option_chains_constituents import run as run_constituents
    logger.info('--- Modul 2: Option Chains (Indizes) ---')
    run_index(snapshot_date)
    logger.info('--- Modul 3: Option Chains (Constituents) ---')
    run_constituents(snapshot_date)


def _validate_chains(snapshot_date: str) -> list[tuple]:
    """
    Returns rows (underlying_ric, calls, puts) where the chain looks incomplete:
    - calls == 0 or puts == 0
    - |calls/puts - 1| > CHAIN_RATIO_TOLERANCE
    Uses first_seen_date = snapshot_date to identify current month's chains.
    """
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT underlying_ric,
                       SUM(CASE WHEN option_type = 'CALL' THEN 1 ELSE 0 END) AS calls,
                       SUM(CASE WHEN option_type = 'PUT'  THEN 1 ELSE 0 END) AS puts
                FROM pub_options.options_chains
                WHERE first_seen_date = :snap
                GROUP BY underlying_ric
                HAVING SUM(CASE WHEN option_type = 'CALL' THEN 1 ELSE 0 END) = 0
                    OR SUM(CASE WHEN option_type = 'PUT'  THEN 1 ELSE 0 END) = 0
                    OR ABS(
                        1.0 * SUM(CASE WHEN option_type = 'CALL' THEN 1 ELSE 0 END)
                            / NULLIF(SUM(CASE WHEN option_type = 'PUT' THEN 1 ELSE 0 END), 0)
                        - 1
                    ) > :tol
            """),
            {'snap': snapshot_date, 'tol': CHAIN_RATIO_TOLERANCE}
        ).fetchall()
    return rows


def _log_incomplete(incomplete: list[tuple]) -> None:
    for underlying_ric, calls, puts in incomplete:
        if calls == 0:
            missing = 'alle CALLs fehlen'
        elif puts == 0:
            missing = 'alle PUTs fehlen'
        else:
            ratio   = calls / puts if puts else float('inf')
            missing = f'ratio calls/puts={ratio:.2f} außerhalb Toleranz'
        logger.warning('WARN %s: calls=%d, puts=%d – %s',
                       underlying_ric, calls, puts, missing)


def main() -> None:
    snapshot_date = get_snapshot_date()
    logger.info('=== Monthly Pipeline Run | snapshot_date: %s ===', snapshot_date)

    # Modul 1: Constituents
    logger.info('--- Modul 1: Constituents ---')
    from modules.fetch_constituents import run as run_constituents
    run_constituents(snapshot_date)

    # Module 2+3: Option Chains
    _run_chains(snapshot_date)

    # Vollständigkeitsprüfung
    logger.info('--- Chain-Validierung (Call/Put-Ratio) ---')
    incomplete = _validate_chains(snapshot_date)
    if incomplete:
        logger.warning('%d Underlying(s) mit unvollständiger Chain – Retry:',
                       len(incomplete))
        _log_incomplete(incomplete)
        _run_chains(snapshot_date)
        incomplete = _validate_chains(snapshot_date)
        if incomplete:
            logger.warning('nach Retry noch %d unvollständig – fortfahren:',
                           len(incomplete))
            _log_incomplete(incomplete)
        else:
            logger.info('OK – alle Chains nach Retry vollständig.')
    else:
        logger.info('OK – alle Chains vollständig.')

    # Modul 4: Option Prices
    logger.info('--- Modul 4: Option Prices ---')
    from modules.fetch_option_prices import run as run_prices
    run_prices(snapshot_date)

    # Modul 5: USD Risk-Free Rates
    logger.info('--- Modul 5: USD Rates (FRED CMT) ---')
    from modules.fetch_usd_rates import run as run_usd_rates
    run_usd_rates(snapshot_date)

    # Modul 6: Daily Prices (SPX Constituents)
    logger.info('--- Modul 6: Daily Prices (SPX Constituents) ---')
    from modules.fetch_prices_daily import run as run_prices_daily
    run_prices_daily(snapshot_date)

    # Modul 7: Quarterly Fundamentals
    logger.info('--- Modul 7: Quarterly Fundamentals ---')
    from modules.fetch_fundamentals_quarterly import run as run_fundamentals
    run_fundamentals(snapshot_date)

    # Modul 8: Dividends
    logger.info('--- Modul 8: Dividends ---')
    from modules.fetch_dividends import run as run_dividends
    run_dividends(snapshot_date)

    # Modul 9: Futures Chains
    logger.info('--- Modul 9: Futures Chains ---')
    from modules.fetch_futures_chains import run as run_futures_chains
    run_futures_chains(snapshot_date)

    # Modul 10: Futures Prices
    logger.info('--- Modul 10: Futures Prices ---')
    from modules.fetch_futures_prices import run as run_futures_prices
    run_futures_prices(snapshot_date)

    # Modul 11: Index Prices
    logger.info('--- Modul 11: Index Prices ---')
    from modules.fetch_index_prices_daily import run as run_index_prices
    run_index_prices(snapshot_date)

    logger.info('=== Pipeline abgeschlossen ===')


if __name__ == '__main__':
    main()
