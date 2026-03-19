"""
Monthly pipeline orchestrator.
Runs all modules sequentially: Constituents → Index Chains → Constituent Chains → Prices
"""
from datetime import datetime


def get_snapshot_date() -> str:
    """Returns the 1st of the current month as snapshot date."""
    today = datetime.now()
    return today.replace(day=1).strftime('%Y-%m-%d')


def main():
    snapshot_date = get_snapshot_date()
    print(f"=== Monthly Pipeline Run | snapshot_date: {snapshot_date} ===\n")

    # Modul 1: Constituents
    print("--- Modul 1: Constituents ---")
    from modules.fetch_constituents import run
    run(snapshot_date)

    # Modul 2: Option Chains (Indizes)
    print("\n--- Modul 2: Option Chains (Indizes) ---")
    from modules.fetch_option_chains_index import run
    run(snapshot_date)

    # Modul 3: Option Chains (Constituents)
    print("\n--- Modul 3: Option Chains (Constituents) ---")
    from modules.fetch_option_chains_constituents import run
    run(snapshot_date)

    # Modul 4: Option Prices
    print("\n--- Modul 4: Option Prices ---")
    from modules.fetch_option_prices import run
    run(snapshot_date)

    print("\n=== Pipeline abgeschlossen ===")


if __name__ == '__main__':
    main()
