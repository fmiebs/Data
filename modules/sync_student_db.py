"""
sync_student_db.py
------------------
Spiegelt den fuer den Unterricht benoetigten Teil von research_db in die
Student-Datenbank auf dem FACM-Server (Praefix DB_FACM_ in der .env).

Gespiegelt wird (Schema- und Tabellennamen bleiben IDENTISCH, damit jede
Abfrage und jedes Notebook unveraendert gegen beide Datenbanken laeuft):

    pub_equity.index_constituents    nur index_ric = '.SPX'
    pub_equity.stock_prices_daily    alle SPX-RICs + deren Vorgaenger-RICs
    pub_config.ric_org_map           vollstaendig (1160 Zeilen, Basis der Dedup)

Vorgaenger-RICs muessen mit: nach einem Neuziehen der Historie traegt
index_constituents nur noch die aktuelle Schreibweise (z.B. FITB.N), die
aeltere Kursreihe liegt aber unter FITB.OQ. Ohne die Vorgaenger waere die
Reihe abgeschnitten.

Angelegte Views auf dem Ziel:
    pub_config.v_ric_canonical          RIC -> kanonischer RIC (successor_ric)
    pub_equity.v_stock_prices_canonical Kurse auf kanonischen RIC umgeschluesselt
    pub_config.mv_primary_ric           Primaergattung je Unternehmen (materialisiert)
    pub_equity.v_stock_prices_company   ein Kurs je Unternehmen und Tag
    pub_equity.v_spx_companies_<LABEL>  Stichtags-Universum  <- einzige Freigabe
                                        fuer die Rolle FACM_students_PM

Uebertragung per COPY-Strom (psycopg2 copy_expert), Preise in Jahres-Chunks.
Idempotent: je Tabelle TRUNCATE + Neuladen, damit kein Drift entsteht.

Aufruf:
    python -m modules.sync_student_db
"""
import io
import logging

from sqlalchemy import text

from config.db import engine, get_engine
from config.logging_setup import setup_logging
from config.pipeline import run_finish, run_start

TARGET_PREFIX = 'DB_FACM_'
CHUNK_FROM    = 2010          # Preise werden ab hier jahresweise uebertragen

# Lehrveranstaltung Portfolio Management: fixes Universum zum Stichtag.
# PM_SNAPSHOT ist das Snapshot-Datum (Monatsende) der zugrunde liegenden
# SPX-Zusammensetzung, PM_LABEL geht in den View-Namen ein.
PM_SNAPSHOT = '2026-10-31'
PM_LABEL    = '20261101'
PM_VIEW     = f'pub_equity.v_spx_companies_{PM_LABEL}'
PM_ROLE     = 'FACM_students_PM'   # Grossbuchstaben: in SQL immer in "..."

logger = setup_logging('sync_student_db')

# Unternehmens-Universum: SPX-Constituents und deren Vorgaenger-RICs
_SPX_RICS = """
    SELECT DISTINCT constituent_ric AS ric
    FROM pub_equity.index_constituents WHERE index_ric = '.SPX'
    UNION
    SELECT m.ric FROM pub_config.ric_org_map m
    WHERE m.successor_ric IN (SELECT DISTINCT constituent_ric
                              FROM pub_equity.index_constituents
                              WHERE index_ric = '.SPX')
"""

TABLES = {
    'pub_equity.index_constituents': "SELECT {cols} FROM pub_equity.index_constituents "
                                     "WHERE index_ric = '.SPX'",
    'pub_config.ric_org_map':        'SELECT {cols} FROM pub_config.ric_org_map',
}


# ---------------------------------------------------------------------------
# DDL per Introspektion — garantiert identische Spalten wie in der Quelle
# ---------------------------------------------------------------------------

def _columns(conn, schema: str, table: str) -> list[tuple]:
    return conn.execute(text("""
        SELECT column_name, data_type, character_maximum_length,
               numeric_precision, numeric_scale, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema = :s AND table_name = :t
        ORDER BY ordinal_position"""), {'s': schema, 't': table}).fetchall()


def _pk(conn, schema: str, table: str) -> list[str]:
    row = conn.execute(text("""
        SELECT array_agg(a.attname ORDER BY k.ord)
        FROM pg_constraint c
        JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON TRUE
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
        WHERE c.conrelid = to_regclass(:full) AND c.contype = 'p'"""),
        {'full': f'{schema}.{table}'}).scalar()
    return list(row) if row else []


def _coltype(col: tuple) -> str:
    name, dtype, charlen, prec, scale, _, _ = col
    if dtype == 'character varying':
        return f'VARCHAR({charlen})' if charlen else 'VARCHAR'
    if dtype == 'character':
        return f'CHAR({charlen})' if charlen else 'CHAR'
    if dtype == 'numeric':
        return f'NUMERIC({prec},{scale})' if prec is not None else 'NUMERIC'
    return {'timestamp with time zone': 'TIMESTAMPTZ',
            'timestamp without time zone': 'TIMESTAMP'}.get(dtype, dtype.upper())


def _create_table_sql(conn, schema: str, table: str) -> tuple[str, list[str]]:
    cols = _columns(conn, schema, table)
    if not cols:
        raise RuntimeError(f'{schema}.{table}: keine Spalten gefunden')

    defs = []
    for col in cols:
        name, _, _, _, _, nullable, default = col
        piece = f'    {name} {_coltype(col)}'
        if default is not None and 'nextval' not in str(default):
            piece += f' DEFAULT {default}'
        if nullable == 'NO':
            piece += ' NOT NULL'
        defs.append(piece)

    pk = _pk(conn, schema, table)
    if pk:
        defs.append(f'    PRIMARY KEY ({", ".join(pk)})')

    ddl = (f'CREATE TABLE IF NOT EXISTS {schema}.{table} (\n'
           + ',\n'.join(defs) + '\n)')
    return ddl, [c[0] for c in cols]


# ---------------------------------------------------------------------------
# COPY-Strom
# ---------------------------------------------------------------------------

def _copy(src_sql: str, schema: str, table: str, cols: list[str]) -> int:
    """COPY (src_sql) TO STDOUT  ->  COPY schema.table FROM STDIN. Gibt Zeilen zurueck."""
    collist = ', '.join(cols)
    buf = io.StringIO()

    src_raw = engine.raw_connection()
    try:
        cur = src_raw.cursor()
        cur.copy_expert(f'COPY ({src_sql}) TO STDOUT WITH (FORMAT csv)', buf)
        cur.close()
    finally:
        src_raw.close()

    data = buf.getvalue()
    buf.close()
    if not data:
        return 0

    tgt_raw = get_engine(TARGET_PREFIX).raw_connection()
    try:
        cur = tgt_raw.cursor()
        cur.copy_expert(
            f'COPY {schema}.{table} ({collist}) FROM STDIN WITH (FORMAT csv)',
            io.StringIO(data))
        cur.close()
        tgt_raw.commit()
    finally:
        tgt_raw.close()

    return data.count('\n')


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

VIEWS = [
    # 1) RIC -> kanonischer RIC. Eine Stufe genuegt: successor_ric enthaelt
    #    per Konstruktion keine Ketten (geprueft beim Eintragen).
    """
    CREATE OR REPLACE VIEW pub_config.v_ric_canonical AS
    SELECT m.ric,
           COALESCE(m.successor_ric, m.ric) AS canonical_ric,
           m.org_permid,
           m.company_name
    FROM pub_config.ric_org_map m
    """,

    # 2) Kurse auf den kanonischen RIC umgeschluesselt. Liegt fuer einen Tag
    #    sowohl eine Zeile des kanonischen RIC als auch eine des Vorgaengers
    #    vor, gewinnt der kanonische RIC.
    """
    CREATE OR REPLACE VIEW pub_equity.v_stock_prices_canonical AS
    SELECT DISTINCT ON (c.canonical_ric, p.trade_date)
           c.canonical_ric AS ric,
           p.trade_date,
           p.close_price, p.open_price, p.high_price, p.low_price,
           p.volume, p.crncy, p.market_cap, p.mcap_crncy,
           p.ric AS source_ric
    FROM pub_equity.stock_prices_daily p
    JOIN pub_config.v_ric_canonical c ON c.ric = p.ric
    ORDER BY c.canonical_ric, p.trade_date, (p.ric = c.canonical_ric) DESC
    """,

    # 3) Primaergattung je Unternehmen: die liquidere Gattung (Medianvolumen).
    #    GOOGL vor GOOG, FOXA vor FOX, NWSA vor NWS.
    #    Materialisiert, weil das Perzentil sonst bei jeder Abfrage ueber die
    #    gesamte Kurstabelle laeuft.
    """
    CREATE MATERIALIZED VIEW IF NOT EXISTS pub_config.mv_primary_ric AS
    WITH vol AS (
        SELECT v.ric,
               m.org_permid,
               PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY v.volume) AS med_volume,
               COUNT(*) AS n_tage
        FROM pub_equity.v_stock_prices_canonical v
        JOIN pub_config.ric_org_map m ON m.ric = v.ric
        WHERE v.volume IS NOT NULL
        GROUP BY 1, 2
    )
    SELECT DISTINCT ON (org_permid)
           org_permid, ric AS primary_ric, med_volume, n_tage
    FROM vol
    ORDER BY org_permid, med_volume DESC NULLS LAST, n_tage DESC, ric
    """,

    # 4) Zielview fuer Studierende: genau ein Kurs je Unternehmen und Tag.
    """
    CREATE OR REPLACE VIEW pub_equity.v_stock_prices_company AS
    SELECT pr.org_permid,
           m.company_name,
           v.ric,
           v.trade_date,
           v.close_price, v.open_price, v.high_price, v.low_price,
           v.volume, v.crncy, v.market_cap, v.mcap_crncy
    FROM pub_config.mv_primary_ric pr
    JOIN pub_equity.v_stock_prices_canonical v ON v.ric = pr.primary_ric
    JOIN pub_config.ric_org_map m              ON m.ric = pr.primary_ric
    """,
]

PM_VIEW_SQL = f"""
    CREATE OR REPLACE VIEW {PM_VIEW} AS
    WITH universum AS (
        SELECT DISTINCT m.org_permid
        FROM pub_equity.index_constituents ic
        JOIN pub_config.ric_org_map m ON m.ric = ic.constituent_ric
        WHERE ic.index_ric = '.SPX'
          AND ic.snapshot_date = DATE '{PM_SNAPSHOT}'
    )
    SELECT v.*
    FROM pub_equity.v_stock_prices_company v
    JOIN universum u ON u.org_permid = v.org_permid
"""

# Leserecht ausschliesslich auf die Stichtags-View. Weil die View postgres
# gehoert, brauchen die Studierenden KEIN Recht auf stock_prices_daily,
# index_constituents oder ric_org_map.
PM_GRANT_SQL = [
    f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{PM_ROLE}') "
    f'THEN CREATE ROLE "{PM_ROLE}" NOLOGIN; END IF; END $$',
    f'GRANT USAGE ON SCHEMA pub_equity TO "{PM_ROLE}"',
    f'GRANT SELECT ON {PM_VIEW} TO "{PM_ROLE}"',
]

COMMENTS = [
    f"COMMENT ON VIEW {PM_VIEW} IS "
    f"'Portfolio Management: die Unternehmen der SPX-Zusammensetzung zum Stichtag "
    f"(Snapshot {PM_SNAPSHOT}) mit vollstaendiger Kurshistorie. Ein Kurs je "
    f"Unternehmen und Handelstag; Aktiengattungen ueber org_permid "
    f"zusammengefasst, RIC-Wechsel aufgeloest. Einzige Freigabe fuer Rolle "
    f"{PM_ROLE}.'",

    "COMMENT ON VIEW pub_equity.v_stock_prices_company IS "
    "'Ein Kurs je Unternehmen und Handelstag. Aktiengattungen sind ueber "
    "org_permid zusammengefasst; gewaehlt wird die liquidere Gattung "
    "(hoeheres Medianvolumen, siehe pub_config.mv_primary_ric). "
    "RIC-Wechsel sind ueber ric_org_map.successor_ric aufgeloest.'",

    "COMMENT ON VIEW pub_equity.v_stock_prices_canonical IS "
    "'Kurse auf den aktuellen (kanonischen) RIC umgeschluesselt. Bei RIC-Wechsel "
    "wie FITB.OQ -> FITB.N ergibt das eine durchgehende Reihe; source_ric zeigt, "
    "aus welchem RIC die Zeile stammt.'",

    "COMMENT ON MATERIALIZED VIEW pub_config.mv_primary_ric IS "
    "'Je Unternehmen (org_permid) die Gattung mit dem hoeheren Medianvolumen. "
    "Nach jedem Sync mit REFRESH MATERIALIZED VIEW aktualisieren.'",
]

ROLE_SQL = [
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'student_ro') "
    "THEN CREATE ROLE student_ro NOLOGIN; END IF; END $$",
    'GRANT USAGE ON SCHEMA pub_equity, pub_config TO student_ro',
    'GRANT SELECT ON ALL TABLES IN SCHEMA pub_equity, pub_config TO student_ro',
    'ALTER DEFAULT PRIVILEGES IN SCHEMA pub_equity, pub_config '
    'GRANT SELECT ON TABLES TO student_ro',
]


# ---------------------------------------------------------------------------

def run(snapshot_date: str | None = None, mode: str = 'live',
        start_date: str | None = None, end_date: str | None = None) -> None:  # noqa: ARG001
    target = get_engine(TARGET_PREFIX)
    with target.connect() as c:
        dbname = c.execute(text('SELECT current_database()')).scalar()
    logger.info('sync_student_db | Ziel: %s', dbname)
    run_id = run_start('sync_student_db', mode, snapshot_date)

    total = 0
    try:
        # --- Schemas + Tabellen ---
        with target.begin() as tc:
            for schema in ('pub_equity', 'pub_config'):
                tc.execute(text(f'CREATE SCHEMA IF NOT EXISTS {schema}'))

        plan: dict[str, list[str]] = {}
        with engine.connect() as sc:
            for full in list(TABLES) + ['pub_equity.stock_prices_daily']:
                schema, table = full.split('.')
                ddl, cols = _create_table_sql(sc, schema, table)
                plan[full] = cols
                with target.begin() as tc:
                    tc.execute(text(ddl))
                logger.info('Tabelle bereit: %s (%d Spalten)', full, len(cols))

        with target.begin() as tc:
            tc.execute(text('CREATE INDEX IF NOT EXISTS idx_stock_prices_trade_date '
                            'ON pub_equity.stock_prices_daily (trade_date)'))

        # --- kleine Tabellen ---
        for full, sql in TABLES.items():
            schema, table = full.split('.')
            cols = plan[full]
            with target.begin() as tc:
                tc.execute(text(f'TRUNCATE {full}'))
            n = _copy(sql.format(cols=', '.join(cols)), schema, table, cols)
            total += n
            logger.info('%s: %d Zeilen uebertragen', full, n)

        # --- Preise jahresweise ---
        full = 'pub_equity.stock_prices_daily'
        cols = plan[full]
        with target.begin() as tc:
            tc.execute(text(f'TRUNCATE {full}'))
        with engine.connect() as sc:
            last_year = sc.execute(text(
                'SELECT EXTRACT(YEAR FROM MAX(trade_date))::int '
                'FROM pub_equity.stock_prices_daily')).scalar()
        for year in range(CHUNK_FROM, last_year + 1):
            sql = (f"SELECT {', '.join('p.' + c for c in cols)} "
                   f"FROM pub_equity.stock_prices_daily p "
                   f"WHERE p.ric IN ({_SPX_RICS}) "
                   f"  AND p.trade_date >= DATE '{year}-01-01' "
                   f"  AND p.trade_date <  DATE '{year + 1}-01-01'")
            n = _copy(sql, 'pub_equity', 'stock_prices_daily', cols)
            total += n
            logger.info('  %s %d: %d Zeilen', full, year, n)

        # --- Views, Kommentare, Rechte ---
        with target.begin() as tc:
            for sql in VIEWS:
                tc.execute(text(sql))
            tc.execute(text('REFRESH MATERIALIZED VIEW pub_config.mv_primary_ric'))
            tc.execute(text(PM_VIEW_SQL))
            for sql in COMMENTS:
                tc.execute(text(sql))
            for sql in ROLE_SQL + PM_GRANT_SQL:
                tc.execute(text(sql))
            tc.execute(text(f'GRANT CONNECT ON DATABASE {dbname} TO student_ro'))
        logger.info('Views, Kommentare und Rolle student_ro gesetzt')

        run_finish(run_id, rows_written=total)
        logger.info('sync_student_db done. rows_written=%d', total)
    except Exception as e:
        run_finish(run_id, error=str(e))
        raise


if __name__ == '__main__':
    run()
