"""
run_backfill.py
---------------
Backfill-Pipeline: fuellt historische Daten fuer einen angegebenen Zeitraum.
Idempotent – kann mehrfach ausgefuehrt werden.

Verwendung:
    python run_backfill.py [--start 2016-01-01] [--end 2026-03-01]

Standardwerte: start = 2016-01-01, end = heute
"""
import argparse
import logging
from datetime import date

from config.logging_setup import setup_logging

logger = setup_logging('run_backfill', log_file='run_backfill.log')

DEFAULT_START = '2016-01-01'


def _end_default() -> str:
    return date.today().strftime('%Y-%m-%d')


def main(start: str = DEFAULT_START, end: str | None = None) -> None:
    if end is None:
        end = _end_default()

    logger.info('=== Backfill | start=%s end=%s ===', start, end)

    # Preise (Aktien)
    logger.info('--- Backfill: stock_prices_daily ---')
    from modules.fetch_prices_daily import run as run_prices
    run_prices(mode='backfill', start_date=start, end_date=end)

    # Fundamentals
    logger.info('--- Backfill: stock_fundamentals_quarterly ---')
    from modules.fetch_fundamentals_quarterly import run as run_fq
    run_fq(mode='backfill', start_date=start, end_date=end)

    # Dividenden
    logger.info('--- Backfill: stock_dividends ---')
    from modules.fetch_dividends import run as run_div
    run_div(mode='backfill', start_date=start, end_date=end)

    # Indexpreise
    logger.info('--- Backfill: index_prices_daily ---')
    from modules.fetch_index_prices_daily import run as run_idx
    run_idx(mode='backfill', start_date=start, end_date=end)

    # USD Rates
    logger.info('--- Backfill: usd_rates ---')
    from modules.fetch_usd_rates import run as run_rates
    run_rates(mode='backfill', start_date=start, end_date=end)

    # Options Prices
    logger.info('--- Backfill: options_prices ---')
    from modules.fetch_option_prices import run as run_opt_prices
    run_opt_prices(mode='backfill', start_date=start, end_date=end)

    # Futures Prices
    logger.info('--- Backfill: futures_prices ---')
    from modules.fetch_futures_prices import run as run_fut_prices
    run_fut_prices(mode='backfill', start_date=start, end_date=end)

    logger.info('=== Backfill abgeschlossen ===')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Backfill historical pipeline data.')
    parser.add_argument('--start', default=DEFAULT_START,
                        help=f'Start date YYYY-MM-DD (default: {DEFAULT_START})')
    parser.add_argument('--end', default=None,
                        help='End date YYYY-MM-DD (default: today)')
    args = parser.parse_args()
    main(start=args.start, end=args.end)
