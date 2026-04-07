"""
schema/migrate.py
-----------------
Session-1-Migration: Infrastruktur & DB-Umstrukturierung.

Ausfuehren:  python -m schema.migrate
             oder:  python schema/migrate.py

Idempotent: kann mehrfach ausgefuehrt werden.
"""
import logging
import sys
from datetime import date
from pathlib import Path

from sqlalchemy import text

# Projektpfad sicherstellen
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.db import engine  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger('migrate')


def _exec(conn, sql: str, label: str = '') -> None:
    """Fuehrt SQL aus. Benutzt Savepoints damit ein Fehler nicht die ganze Transaktion abbricht."""
    try:
        conn.execute(text('SAVEPOINT _m'))
        conn.execute(text(sql))
        conn.execute(text('RELEASE SAVEPOINT _m'))
        if label:
            log.info(f'  OK  {label}')
    except Exception as e:
        try:
            conn.execute(text('ROLLBACK TO SAVEPOINT _m'))
        except Exception:
            pass
        log.warning(f'  SKIP {label}: {e}')


# ---------------------------------------------------------------------------
# 4a  pub_config Schema
# ---------------------------------------------------------------------------

def step_4a(conn) -> None:
    log.info('=== 4a: pub_config Schema ===')
    _exec(conn, 'CREATE SCHEMA IF NOT EXISTS pub_config', 'CREATE SCHEMA pub_config')


# ---------------------------------------------------------------------------
# 4b  pub_config.data_sources
# ---------------------------------------------------------------------------

def step_4b(conn) -> None:
    log.info('=== 4b: pub_config.data_sources ===')

    _exec(conn, """
        CREATE TABLE IF NOT EXISTS pub_config.data_sources (
            id            SERIAL PRIMARY KEY,
            domain        VARCHAR   NOT NULL,
            entity_type   VARCHAR   NOT NULL,
            module        VARCHAR   NOT NULL,
            eikon_field   VARCHAR,
            eikon_params  JSONB,
            logical_name  VARCHAR   NOT NULL,
            target_schema VARCHAR   NOT NULL,
            target_table  VARCHAR   NOT NULL,
            target_column VARCHAR   NOT NULL,
            source_code   CHAR(2)   NOT NULL DEFAULT 'EK',
            active        BOOLEAN   DEFAULT TRUE
        )
    """, 'CREATE TABLE pub_config.data_sources')

    # Daten aus overview_company_data migrieren (falls noch nicht geschehen)
    exists = conn.execute(text(
        "SELECT to_regclass('pub_equity.overview_company_data')"
    )).scalar()

    if exists:
        already = conn.execute(text(
            "SELECT COUNT(*) FROM pub_config.data_sources"
        )).scalar()
        if already == 0:
            # overview_company_data hat andere Spaltenstruktur als data_sources
            # (kein module/logical_name/target_schema) — Migration ueberspringen.
            # Die relevanten Zeilen werden unten per hardcoded INSERT eingetragen.
            log.info('  overview_company_data migration skipped (incompatible schema)')
        else:
            log.info('  data_sources already populated – skipping')

        _exec(conn, 'DROP TABLE IF EXISTS pub_equity.overview_company_data',
              'DROP overview_company_data')
    else:
        log.info('  overview_company_data not found – skipping migration')

    # Kernzeilen eintragen (alle Eikon-Felder aller Module)
    # Nur einfuegen falls logical_name noch nicht existiert
    rows = [
        # domain, entity_type, module, eikon_field, logical_name, target_schema, target_table, target_column, source_code
        ('equity', 'index',  'fetch_index_prices_daily',       'TR.PriceClose',             'index_prices_daily',        'pub_equity',   'index_prices_daily',        'close_price',         'EK'),
        ('equity', 'index',  'fetch_index_prices_daily',       'TR.PriceOpen',              'index_prices_daily',        'pub_equity',   'index_prices_daily',        'open_price',          'EK'),
        ('equity', 'index',  'fetch_index_prices_daily',       'TR.PriceHigh',              'index_prices_daily',        'pub_equity',   'index_prices_daily',        'high_price',          'EK'),
        ('equity', 'index',  'fetch_index_prices_daily',       'TR.PriceLow',               'index_prices_daily',        'pub_equity',   'index_prices_daily',        'low_price',           'EK'),
        ('equity', 'stock',  'fetch_prices_daily',             'TR.PriceClose',             'stock_prices_daily',        'pub_equity',   'stock_prices_daily',        'close_price',         'EK'),
        ('equity', 'stock',  'fetch_prices_daily',             'TR.PriceOpen',              'stock_prices_daily',        'pub_equity',   'stock_prices_daily',        'open_price',          'EK'),
        ('equity', 'stock',  'fetch_prices_daily',             'TR.PriceHigh',              'stock_prices_daily',        'pub_equity',   'stock_prices_daily',        'high_price',          'EK'),
        ('equity', 'stock',  'fetch_prices_daily',             'TR.PriceLow',               'stock_prices_daily',        'pub_equity',   'stock_prices_daily',        'low_price',           'EK'),
        ('equity', 'stock',  'fetch_prices_daily',             'TR.Volume',                 'stock_prices_daily',        'pub_equity',   'stock_prices_daily',        'volume',              'EK'),
        ('equity', 'stock',  'fetch_fundamentals_quarterly',   'TR.BookValuePerShare',      'fundamentals_quarterly',    'pub_equity',   'stock_fundamentals_quarterly', 'book_value_per_share', 'EK'),
        ('equity', 'stock',  'fetch_fundamentals_quarterly',   'TR.SharesOutstanding',      'fundamentals_quarterly',    'pub_equity',   'stock_fundamentals_quarterly', 'shares_outstanding',   'EK'),
        ('equity', 'stock',  'fetch_fundamentals_quarterly',   'TR.GICSSector',             'fundamentals_quarterly',    'pub_equity',   'stock_fundamentals_quarterly', 'gics_sector',         'EK'),
        ('equity', 'stock',  'fetch_dividends',                'TR.DivUnadjustedGross',     'stock_dividends',           'pub_equity',   'stock_dividends',           'gross_amount',        'EK'),
        ('equity', 'index',  'fetch_constituents',             'TR.IndexConstituentRIC',    'index_constituents',        'pub_equity',   'index_constituents',        'constituent_ric',     'EK'),
        ('options', 'index', 'fetch_option_chains_index',      'TR.CallPutInd',             'options_chains',            'pub_options',  'options_chains',            'option_type',         'EK'),
        ('options', 'option','fetch_option_prices',            'TR.BidPrice',               'options_prices',            'pub_options',  'options_prices',            'price_bid',           'EK'),
        ('options', 'option','fetch_option_prices',            'TR.AskPrice',               'options_prices',            'pub_options',  'options_prices',            'price_ask',           'EK'),
        ('rates',  'rate',   'fetch_usd_rates',                None,                        'usd_rates',                 'pub_rates',    'usd_rates',                 'rate_value',          'EK'),
        ('futures','future', 'fetch_futures_chains',           'TR.FutExpirDate',           'futures_chains',            'pub_futures',  'futures_chains',            'expiry_date',         'EK'),
        ('futures','future', 'fetch_futures_prices',           'TR.BidPrice',               'futures_prices',            'pub_futures',  'futures_prices',            'price_bid',           'EK'),
        ('futures','future', 'fetch_futures_prices',           'TR.SettlePrice',            'futures_prices',            'pub_futures',  'futures_prices',            'price_settle',        'EK'),
    ]

    for domain, entity_type, module, eikon_field, logical_name, tgt_schema, tgt_table, tgt_col, src in rows:
        existing = conn.execute(text(
            "SELECT 1 FROM pub_config.data_sources "
            "WHERE logical_name = :ln AND target_column = :col"
        ), {'ln': logical_name, 'col': tgt_col}).scalar()
        if not existing:
            conn.execute(text("""
                INSERT INTO pub_config.data_sources
                    (domain, entity_type, module, eikon_field,
                     logical_name, target_schema, target_table, target_column, source_code)
                VALUES
                    (:domain, :entity_type, :module, :eikon_field,
                     :logical_name, :target_schema, :target_table, :target_column, :source_code)
            """), {
                'domain': domain, 'entity_type': entity_type,
                'module': module, 'eikon_field': eikon_field,
                'logical_name': logical_name, 'target_schema': tgt_schema,
                'target_table': tgt_table, 'target_column': tgt_col,
                'source_code': src,
            })

    log.info('  data_sources rows OK')


# ---------------------------------------------------------------------------
# 4c  pub_config.stock_indices / futures_underlyings / rates
# ---------------------------------------------------------------------------

def step_4c(conn) -> None:
    log.info('=== 4c: pub_config Steuertabellen ===')

    # stock_indices
    _exec(conn, """
        CREATE TABLE IF NOT EXISTS pub_config.stock_indices (
            index_ric                      VARCHAR PRIMARY KEY,
            index_name                     VARCHAR,
            currency                       CHAR(3),
            fetch_index_prices             BOOLEAN DEFAULT TRUE,
            fetch_index_option_chain       BOOLEAN DEFAULT FALSE,
            fetch_constituents             BOOLEAN DEFAULT FALSE,
            fetch_constituent_options      BOOLEAN DEFAULT FALSE,
            fetch_constituent_prices       BOOLEAN DEFAULT FALSE,
            fetch_constituent_fundamentals BOOLEAN DEFAULT FALSE,
            fetch_constituent_dividends    BOOLEAN DEFAULT FALSE,
            active                         BOOLEAN DEFAULT TRUE
        )
    """, 'CREATE TABLE pub_config.stock_indices')

    # pub_equity.index_overview wurde vollständig in pub_config.stock_indices migriert
    # (März 2026) und wurde anschließend gedroppt. Keine Migrationsschritte mehr nötig.

    # futures_underlyings
    _exec(conn, """
        CREATE TABLE IF NOT EXISTS pub_config.futures_underlyings (
            underlying_ric    VARCHAR PRIMARY KEY,
            underlying_name   VARCHAR,
            futures_chain_ric VARCHAR,
            fetch_chains      BOOLEAN DEFAULT TRUE,
            fetch_prices      BOOLEAN DEFAULT TRUE,
            active            BOOLEAN DEFAULT TRUE
        )
    """, 'CREATE TABLE pub_config.futures_underlyings')

    fov_exists = conn.execute(text(
        "SELECT to_regclass('pub_futures.futures_overview')"
    )).scalar()

    if fov_exists:
        already = conn.execute(text(
            "SELECT COUNT(*) FROM pub_config.futures_underlyings"
        )).scalar()
        if already == 0:
            # Explizite Spalten — futures_overview kann zusaetzliche Spalten haben
            fov_cols = {
                row[0] for row in conn.execute(text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='pub_futures' AND table_name='futures_overview'"
                ))
            }
            has_name  = 'underlying_name'   in fov_cols
            has_chain = 'futures_chain_ric' in fov_cols
            has_act   = 'active'            in fov_cols
            col_name  = 'underlying_name'   if has_name  else 'NULL'
            col_chain = 'futures_chain_ric' if has_chain else 'NULL'
            col_act   = 'active'            if has_act   else 'TRUE'
            _exec(conn, f"""
                INSERT INTO pub_config.futures_underlyings
                    (underlying_ric, underlying_name, futures_chain_ric, active)
                SELECT underlying_ric, {col_name}, {col_chain}, {col_act}
                FROM pub_futures.futures_overview
            """, 'INSERT futures_overview -> futures_underlyings')
        _exec(conn, 'DROP TABLE IF EXISTS pub_futures.futures_overview',
              'DROP futures_overview')

    # rates
    _exec(conn, """
        CREATE TABLE IF NOT EXISTS pub_config.rates (
            series_id   VARCHAR PRIMARY KEY,
            tenor       VARCHAR  NOT NULL,
            tenor_days  INTEGER  NOT NULL,
            rate_type   VARCHAR  NOT NULL,
            source      VARCHAR  NOT NULL,
            active      BOOLEAN  DEFAULT TRUE
        )
    """, 'CREATE TABLE pub_config.rates')

    rate_rows = [
        ('DGS1MO', '1m',  30,    'treasury_cmt', 'FRED'),
        ('DGS3MO', '3m',  91,    'treasury_cmt', 'FRED'),
        ('DGS6MO', '6m',  182,   'treasury_cmt', 'FRED'),
        ('DGS1',   '1y',  365,   'treasury_cmt', 'FRED'),
        ('DGS2',   '2y',  730,   'treasury_cmt', 'FRED'),
        ('DGS3',   '3y',  1095,  'treasury_cmt', 'FRED'),
        ('DGS5',   '5y',  1825,  'treasury_cmt', 'FRED'),
        ('DGS7',   '7y',  2555,  'treasury_cmt', 'FRED'),
        ('DGS10',  '10y', 3650,  'treasury_cmt', 'FRED'),
        ('DGS20',  '20y', 7300,  'treasury_cmt', 'FRED'),
        ('DGS30',  '30y', 10950, 'treasury_cmt', 'FRED'),
    ]
    for series_id, tenor, tenor_days, rate_type, source in rate_rows:
        existing = conn.execute(text(
            "SELECT 1 FROM pub_config.rates WHERE series_id = :sid"
        ), {'sid': series_id}).scalar()
        if not existing:
            conn.execute(text("""
                INSERT INTO pub_config.rates (series_id, tenor, tenor_days, rate_type, source)
                VALUES (:sid, :tenor, :days, :rtype, :src)
            """), {'sid': series_id, 'tenor': tenor, 'days': tenor_days,
                   'rtype': rate_type, 'src': source})
    log.info('  pub_config.rates rows OK')


# ---------------------------------------------------------------------------
# 4d  pub_config.pipeline_runs
# ---------------------------------------------------------------------------

def step_4d(conn) -> None:
    log.info('=== 4d: pub_config.pipeline_runs ===')
    _exec(conn, """
        CREATE TABLE IF NOT EXISTS pub_config.pipeline_runs (
            id            SERIAL PRIMARY KEY,
            module        VARCHAR      NOT NULL,
            mode          VARCHAR      NOT NULL,
            started_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
            finished_at   TIMESTAMPTZ,
            rows_written  INTEGER,
            status        VARCHAR      NOT NULL,
            error_msg     TEXT,
            snapshot_date DATE
        )
    """, 'CREATE TABLE pub_config.pipeline_runs')


# ---------------------------------------------------------------------------
# 4e  Equity-Tabellen umbenennen + Spalten harmonisieren
# ---------------------------------------------------------------------------

def step_4e(conn) -> None:
    log.info('=== 4e: Equity-Tabellen umbenennen + Spalten harmonisieren ===')

    # prices_daily -> stock_prices_daily
    _exec(conn,
          "ALTER TABLE pub_equity.prices_daily RENAME TO stock_prices_daily",
          'RENAME prices_daily -> stock_prices_daily')

    # dividends -> stock_dividends
    _exec(conn,
          "ALTER TABLE pub_equity.dividends RENAME TO stock_dividends",
          'RENAME dividends -> stock_dividends')

    # fundamentals_quarterly -> stock_fundamentals_quarterly
    _exec(conn,
          "ALTER TABLE pub_equity.fundamentals_quarterly RENAME TO stock_fundamentals_quarterly",
          'RENAME fundamentals_quarterly -> stock_fundamentals_quarterly')

    # fundamentals_annual droppen
    _exec(conn,
          "DROP TABLE IF EXISTS pub_equity.fundamentals_annual",
          'DROP fundamentals_annual')

    # index_prices_daily: Spalten umbenennen
    for old, new in (
        ('close_local', 'close_price'),
        ('open_local',  'open_price'),
        ('high_local',  'high_price'),
        ('low_local',   'low_price'),
    ):
        _exec(conn,
              f"ALTER TABLE pub_equity.index_prices_daily RENAME COLUMN {old} TO {new}",
              f'RENAME index_prices_daily.{old} -> {new}')

    # index_prices_daily: close_usd droppen
    _exec(conn,
          "ALTER TABLE pub_equity.index_prices_daily DROP COLUMN IF EXISTS close_usd",
          'DROP index_prices_daily.close_usd')

    # crncy-Spalte
    for tbl in ('stock_prices_daily', 'index_prices_daily'):
        _exec(conn,
              f"ALTER TABLE pub_equity.{tbl} "
              f"ADD COLUMN IF NOT EXISTS crncy VARCHAR(3) NOT NULL DEFAULT 'LOC'",
              f'ADD COLUMN {tbl}.crncy')


# ---------------------------------------------------------------------------
# 4f  pub_options.options_chains — Unified Table + Migration
# ---------------------------------------------------------------------------

def step_4f(conn) -> None:
    log.info('=== 4f: options_chains Unified Table ===')

    # Neue Tabelle anlegen
    _exec(conn, """
        CREATE TABLE IF NOT EXISTS pub_options.options_chains_new (
            underlying_ric   VARCHAR      NOT NULL,
            option_ric       VARCHAR      NOT NULL,
            option_ric_hist  VARCHAR,
            expiry_date      DATE         NOT NULL,
            strike           NUMERIC      NOT NULL,
            option_type      VARCHAR(4)   NOT NULL,
            expiry_type      CHAR(1),
            first_seen_date  DATE         NOT NULL,
            source           CHAR(2)      NOT NULL DEFAULT 'EK',
            created_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
            PRIMARY KEY (underlying_ric, option_ric)
        )
    """, 'CREATE TABLE options_chains_new')

    # Schritt 1: Aus options_chains_history (bereits Basisform)
    hist_exists = conn.execute(text(
        "SELECT to_regclass('pub_options.options_chains_history')"
    )).scalar()

    if hist_exists:
        _exec(conn, """
            INSERT INTO pub_options.options_chains_new
                (underlying_ric, option_ric, option_ric_hist, expiry_date,
                 strike, option_type, expiry_type, first_seen_date, source)
            SELECT underlying_ric, option_ric, option_ric_hist, expiry_date,
                   strike, option_type, expiry_type, expiry_date, 'EK'
            FROM pub_options.options_chains_history
            ON CONFLICT DO NOTHING
        """, 'INSERT options_chains_history -> options_chains_new')

    # Schritt 2: Aus altem options_chains (/ entfernen, option_ric_hist berechnen)
    old_exists = conn.execute(text(
        "SELECT to_regclass('pub_options.options_chains')"
    )).scalar()

    if old_exists:
        # Pruefen ob die alte Tabelle noch snapshot_date hat (alter Stil)
        old_cols = {
            row[0] for row in conn.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='pub_options' AND table_name='options_chains'"
            ))
        }
        if 'snapshot_date' in old_cols:
            log.info('  Migriere aus altem options_chains (mit snapshot_date) ...')
            # Python-seitige Migration: / entfernen, hist-RIC berechnen
            from modules.utils import build_option_ric_hist

            rows = conn.execute(text("""
                SELECT underlying_ric,
                       LTRIM(option_ric, '/') AS option_ric,
                       expiry_date, strike, option_type, expiry_type,
                       MIN(snapshot_date) AS first_seen_date
                FROM pub_options.options_chains
                GROUP BY underlying_ric, LTRIM(option_ric, '/'),
                         expiry_date, strike, option_type, expiry_type
            """)).fetchall()

            inserted = 0
            for r in rows:
                underlying_ric = r[0]
                option_ric     = r[1]
                expiry_date    = r[2]
                strike         = r[3]
                option_type    = r[4]
                expiry_type    = r[5]
                first_seen     = r[6]

                try:
                    option_ric_hist = build_option_ric_hist(option_ric, expiry_date)
                except Exception:
                    option_ric_hist = None

                conn.execute(text("""
                    INSERT INTO pub_options.options_chains_new
                        (underlying_ric, option_ric, option_ric_hist, expiry_date,
                         strike, option_type, expiry_type, first_seen_date, source)
                    VALUES
                        (:ur, :or_, :orh, :exp, :str_, :ot, :et, :fsd, 'EK')
                    ON CONFLICT DO NOTHING
                """), {
                    'ur': underlying_ric, 'or_': option_ric,
                    'orh': option_ric_hist, 'exp': expiry_date,
                    'str_': float(strike), 'ot': option_type,
                    'et': expiry_type, 'fsd': first_seen,
                })
                inserted += 1

            log.info(f'  {inserted} Zeilen aus options_chains migriert')

            # Altes options_chains umbenennen (Backup behalten bis Verifikation)
            _exec(conn,
                  "ALTER TABLE pub_options.options_chains RENAME TO options_chains_bak",
                  'RENAME options_chains -> options_chains_bak')
        else:
            log.info('  options_chains hat kein snapshot_date – bereits migriert, skip')

    # Neue Tabelle zur aktiven machen
    new_exists = conn.execute(text(
        "SELECT to_regclass('pub_options.options_chains_new')"
    )).scalar()
    active_exists = conn.execute(text(
        "SELECT to_regclass('pub_options.options_chains')"
    )).scalar()

    if new_exists and not active_exists:
        _exec(conn,
              "ALTER TABLE pub_options.options_chains_new RENAME TO options_chains",
              'RENAME options_chains_new -> options_chains')

    # options_chains_history droppen (nach erfolgreicher Migration)
    if hist_exists:
        _exec(conn, 'DROP TABLE IF EXISTS pub_options.options_chains_history',
              'DROP options_chains_history')


# ---------------------------------------------------------------------------
# 4g  options_prices: Basisform herstellen
# ---------------------------------------------------------------------------

def step_4g(conn) -> None:
    log.info('=== 4g: options_prices Basisform ===')

    # Pruefen ob /-Prefixe vorhanden sind
    n = conn.execute(text(
        "SELECT COUNT(*) FROM pub_options.options_prices WHERE option_ric LIKE '/%' LIMIT 1"
    )).scalar()

    if n == 0:
        log.info('  Keine /-Prefixe vorhanden – skip')
        return

    log.warning(
        '  options_prices hat /-Prefixe. Tabelle ist zu gross fuer direkten UPDATE. '
        'Bitte schema/fix_options_prices.py separat ausfuehren (Laufzeit ~20-30 min).'
    )


# ---------------------------------------------------------------------------
# 4h  source + created_at in alle uebrigen Tabellen
# ---------------------------------------------------------------------------

def step_4h(conn) -> None:
    log.info('=== 4h: source + created_at in alle Tabellen ===')

    tables_ek = [
        ('pub_equity',   'stock_prices_daily'),
        ('pub_equity',   'index_prices_daily'),
        ('pub_equity',   'stock_fundamentals_quarterly'),
        ('pub_equity',   'stock_dividends'),
        ('pub_equity',   'index_constituents'),
        ('pub_options',  'options_prices'),
        ('pub_futures',  'futures_chains'),
        ('pub_futures',  'futures_prices'),
    ]

    for schema, table in tables_ek:
        _exec(conn,
              f"ALTER TABLE {schema}.{table} "
              f"ADD COLUMN IF NOT EXISTS source CHAR(2) DEFAULT 'EK'",
              f'ADD COLUMN {schema}.{table}.source')
        _exec(conn,
              f"ALTER TABLE {schema}.{table} "
              f"ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT now()",
              f'ADD COLUMN {schema}.{table}.created_at')

    # pub_rates.usd_rates: Sonderbehandlung
    # Bestehende Tabelle hat rate_type ('par'/'zero') und source VARCHAR(32) ('FRED'/'bootstrapped').
    # Ziel: source VARCHAR(32) -> source_system, neue source CHAR(2) ('EK'/'FM') hinzufuegen.
    usd_cols = {
        row[0] for row in conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='pub_rates' AND table_name='usd_rates'"
        ))
    }

    if 'source' in usd_cols and 'source_system' not in usd_cols:
        # Alte source-Spalte (VARCHAR) umbenennen, neue CHAR(2) ergaenzen
        _exec(conn,
              "ALTER TABLE pub_rates.usd_rates RENAME COLUMN source TO source_system",
              'RENAME usd_rates.source -> source_system')
        _exec(conn,
              "ALTER TABLE pub_rates.usd_rates ADD COLUMN source CHAR(2) DEFAULT 'EK'",
              'ADD COLUMN usd_rates.source CHAR(2)')
        _exec(conn,
              "UPDATE pub_rates.usd_rates SET source = 'FM' WHERE rate_type = 'zero'",
              "UPDATE usd_rates source='FM' for zero rates")
    elif 'source' not in usd_cols:
        _exec(conn,
              "ALTER TABLE pub_rates.usd_rates ADD COLUMN source CHAR(2) DEFAULT 'EK'",
              'ADD COLUMN usd_rates.source CHAR(2)')
        _exec(conn,
              "UPDATE pub_rates.usd_rates SET source = 'FM' WHERE rate_type = 'zero'",
              "UPDATE usd_rates source='FM' for zero rates")

    _exec(conn,
          "ALTER TABLE pub_rates.usd_rates ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT now()",
          'ADD COLUMN usd_rates.created_at')


# ---------------------------------------------------------------------------
# 4i  Datenbankindizes
# ---------------------------------------------------------------------------

def step_4i(conn) -> None:
    log.info('=== 4i: Datenbankindizes ===')

    indices = [
        ('idx_options_prices_price_date',
         'CREATE INDEX IF NOT EXISTS idx_options_prices_price_date '
         'ON pub_options.options_prices (price_date)'),
        ('idx_options_chains_expiry',
         'CREATE INDEX IF NOT EXISTS idx_options_chains_expiry '
         'ON pub_options.options_chains (underlying_ric, expiry_date)'),
        ('idx_stock_prices_trade_date',
         'CREATE INDEX IF NOT EXISTS idx_stock_prices_trade_date '
         'ON pub_equity.stock_prices_daily (trade_date)'),
        ('idx_index_prices_trade_date',
         'CREATE INDEX IF NOT EXISTS idx_index_prices_trade_date '
         'ON pub_equity.index_prices_daily (trade_date)'),
        ('idx_usd_rates_trade_date',
         'CREATE INDEX IF NOT EXISTS idx_usd_rates_trade_date '
         'ON pub_rates.usd_rates (trade_date)'),
    ]

    for name, sql in indices:
        _exec(conn, sql, f'CREATE INDEX {name}')


# ---------------------------------------------------------------------------
# Hauptprogramm
# ---------------------------------------------------------------------------

def main() -> None:
    log.info('Starte Session-1-Migration ...')

    # Jeder Schritt laeuft in einer eigenen Transaktion — ein Fehler blockiert nicht die naechsten.
    steps = [
        ('4a', step_4a),
        ('4b', step_4b),
        ('4c', step_4c),
        ('4d', step_4d),
        ('4e', step_4e),
        ('4f', step_4f),
        ('4g', step_4g),
        ('4h', step_4h),
        ('4i', step_4i),
    ]

    for label, fn in steps:
        try:
            with engine.begin() as conn:
                fn(conn)
        except Exception as e:
            log.error('Schritt %s fehlgeschlagen: %s', label, e)
            raise

    log.info('Migration abgeschlossen.')


if __name__ == '__main__':
    main()
