"""
config/schema.py
----------------
Single Source of Truth fuer alle Tabellennamen.

Liest (logical_name -> schema.table) einmalig aus pub_config.data_sources
und cached das Ergebnis via lru_cache. Tabellennamen niemals hardcoden —
immer get_table() oder die TBL_*-Konstanten verwenden.

Einzige hardcodierte Konstante im gesamten Python-Code:
    _CONFIG_TABLE = 'pub_config.data_sources'
"""
from functools import lru_cache

from sqlalchemy import text

from config.db import engine

_CONFIG_TABLE = 'pub_config.data_sources'


@lru_cache(maxsize=None)
def _load_tables() -> dict[str, str]:
    """Liest alle (logical_name -> schema.table) Mappings aus pub_config.data_sources."""
    with engine.connect() as conn:
        rows = conn.execute(text(
            f"SELECT DISTINCT logical_name, target_schema, target_table "  # noqa: S608
            f"FROM {_CONFIG_TABLE} WHERE logical_name IS NOT NULL AND active = TRUE"
        )).fetchall()
    return {r[0]: f'{r[1]}.{r[2]}' for r in rows}


def get_table(logical_name: str) -> str:
    """Gibt den vollqualifizierten Tabellennamen (schema.table) zurueck."""
    mapping = _load_tables()
    if logical_name not in mapping:
        raise KeyError(
            f"Unbekannter logical_name '{logical_name}' in {_CONFIG_TABLE}. "
            f"Vorhandene Keys: {sorted(mapping)}"
        )
    return mapping[logical_name]


# ---------------------------------------------------------------------------
# Convenience-Konstanten — einmalig bei erstem Import aufgeloest (lru_cache)
# Zugriff: from config.schema import TBL_STOCK_PRICES  (String)
# ---------------------------------------------------------------------------

TBL_STOCK_PRICES       = get_table('stock_prices_daily')
TBL_INDEX_PRICES       = get_table('index_prices_daily')
TBL_OPTIONS_CHAINS     = get_table('options_chains')
TBL_OPTIONS_PRICES     = get_table('options_prices')
TBL_USD_RATES          = get_table('usd_rates')
TBL_FUTURES_CHAINS     = get_table('futures_chains')
TBL_FUTURES_PRICES     = get_table('futures_prices')
TBL_FUNDAMENTALS       = get_table('fundamentals_quarterly')
TBL_DIVIDENDS          = get_table('stock_dividends')
TBL_INDEX_CONSTITUENTS = get_table('index_constituents')
TBL_CONFIG_INDICES     = get_table('config_indices')
