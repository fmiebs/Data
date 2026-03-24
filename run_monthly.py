"""
Monthly pipeline orchestrator.
Runs all modules sequentially: Constituents → Index Chains → Constituent Chains → Prices
After fetching chains, validates call/put completeness and retries once if needed.
"""
from datetime import datetime

from sqlalchemy import text

from config.db import engine

CHAIN_RATIO_TOLERANCE = 0.25  # flag if |calls/puts - 1| > 25%


def get_snapshot_date() -> str:
    today = datetime.now()
    return today.replace(day=1).strftime('%Y-%m-%d')


def _run_chains(snapshot_date: str) -> None:
    from modules.fetch_option_chains_index import run as run_index
    from modules.fetch_option_chains_constituents import run as run_constituents
    print("\n--- Modul 2: Option Chains (Indizes) ---")
    run_index(snapshot_date)
    print("\n--- Modul 3: Option Chains (Constituents) ---")
    run_constituents(snapshot_date)


def _validate_chains(snapshot_date: str) -> list[tuple]:
    """
    Returns rows (underlying_ric, calls, puts) where the chain looks incomplete:
    - calls == 0 or puts == 0
    - |calls/puts - 1| > CHAIN_RATIO_TOLERANCE
    """
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT underlying_ric,
                       SUM(CASE WHEN option_type = 'CALL' THEN 1 ELSE 0 END) AS calls,
                       SUM(CASE WHEN option_type = 'PUT'  THEN 1 ELSE 0 END) AS puts
                FROM pub_options.options_chains
                WHERE snapshot_date = :snap
                GROUP BY underlying_ric
                HAVING SUM(CASE WHEN option_type = 'CALL' THEN 1 ELSE 0 END) = 0
                    OR SUM(CASE WHEN option_type = 'PUT'  THEN 1 ELSE 0 END) = 0
                    OR ABS(
                        1.0 * SUM(CASE WHEN option_type = 'CALL' THEN 1 ELSE 0 END)
                            / NULLIF(SUM(CASE WHEN option_type = 'PUT' THEN 1 ELSE 0 END), 0)
                        - 1
                    ) > :tol
            """),
            {"snap": snapshot_date, "tol": CHAIN_RATIO_TOLERANCE}
        ).fetchall()
    return rows


def _log_incomplete(incomplete: list[tuple]) -> None:
    for underlying_ric, calls, puts in incomplete:
        if calls == 0:
            missing = "alle CALLs fehlen"
        elif puts == 0:
            missing = "alle PUTs fehlen"
        else:
            ratio = calls / puts if puts else float('inf')
            missing = f"ratio calls/puts={ratio:.2f} außerhalb Toleranz"
        print(f"    WARN {underlying_ric}: calls={calls}, puts={puts} – {missing}")


def main():
    snapshot_date = get_snapshot_date()
    print(f"=== Monthly Pipeline Run | snapshot_date: {snapshot_date} ===\n")

    # Modul 1: Constituents
    print("--- Modul 1: Constituents ---")
    from modules.fetch_constituents import run as run_constituents
    run_constituents(snapshot_date)

    # Module 2+3: Option Chains
    _run_chains(snapshot_date)

    # Vollständigkeitsprüfung
    print("\n--- Chain-Validierung (Call/Put-Ratio) ---")
    incomplete = _validate_chains(snapshot_date)
    if incomplete:
        print(f"  {len(incomplete)} Underlying(s) mit unvollständiger Chain – Retry:")
        _log_incomplete(incomplete)
        _run_chains(snapshot_date)
        incomplete = _validate_chains(snapshot_date)
        if incomplete:
            print(f"  WARN: nach Retry noch {len(incomplete)} unvollständig – fortfahren mit Preisen:")
            _log_incomplete(incomplete)
        else:
            print("  OK – alle Chains nach Retry vollständig.")
    else:
        print("  OK – alle Chains vollständig.")

    # Modul 4: Option Prices
    print("\n--- Modul 4: Option Prices ---")
    from modules.fetch_option_prices import run as run_prices
    run_prices(snapshot_date)

    # Modul 5: USD Risk-Free Rates
    print("\n--- Modul 5: USD Rates (FRED CMT) ---")
    from modules.fetch_usd_rates import run as run_usd_rates
    run_usd_rates(snapshot_date)

    # Modul 6: Daily Prices + Shares Outstanding (SPX constituents)
    print("\n--- Modul 6: Daily Prices (SPX Constituents) ---")
    from modules.fetch_prices_daily import run as run_prices_daily
    run_prices_daily(snapshot_date)

    # Modul 7: Quarterly Fundamentals (SPX constituents)
    print("\n--- Modul 7: Quarterly Fundamentals (SPX Constituents) ---")
    from modules.fetch_fundamentals_quarterly import run as run_fundamentals
    run_fundamentals(snapshot_date)

    # Modul 8: Dividends – regular + special (SPX constituents)
    print("\n--- Modul 8: Dividends (SPX Constituents) ---")
    from modules.fetch_dividends import run as run_dividends
    run_dividends(snapshot_date)

    # Modul 9: Futures Chains (aktive Kontrakte je Underlying)
    print("\n--- Modul 9: Futures Chains ---")
    from modules.fetch_futures_chains import run as run_futures_chains
    run_futures_chains(snapshot_date)

    # Modul 10: Futures Prices (Bid/Ask/Settle/Volume/OI, delta: 1 Jahr)
    print("\n--- Modul 10: Futures Prices ---")
    from modules.fetch_futures_prices import run as run_futures_prices
    run_futures_prices(snapshot_date)

    print("\n=== Pipeline abgeschlossen ===")


if __name__ == '__main__':
    main()
