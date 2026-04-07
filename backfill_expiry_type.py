"""
backfill_expiry_type.py
Klassifiziert alle bestehenden Zeilen in pub_options.options_chains.
Idempotent: Zeilen mit expiry_type IS NOT NULL werden uebersprungen.
"""
import datetime
from sqlalchemy import text
from config.db import engine
from modules.utils import classify_expiry

with engine.begin() as con:
    rows = con.execute(text("""
        SELECT DISTINCT
            oc.expiry_date,
            oc.snapshot_date,
            CASE
                WHEN si.index_ric IS NOT NULL THEN 'index'
                ELSE 'equity'
            END AS asset_type
        FROM pub_options.options_chains oc
        LEFT JOIN pub_config.stock_indices si
          ON si.index_ric = oc.underlying_ric
        WHERE oc.expiry_type IS NULL
        ORDER BY oc.snapshot_date, oc.expiry_date
    """)).fetchall()

    print(f"{len(rows)} distinct (expiry_date, snapshot_date, asset_type) zu klassifizieren...")

    for expiry_date, snapshot_date, asset_type in rows:
        etype = classify_expiry(expiry_date, snapshot_date, asset_type)
        con.execute(text("""
            UPDATE pub_options.options_chains oc
            SET expiry_type = :etype
            WHERE oc.expiry_date    = :expiry_date
              AND oc.snapshot_date  = :snapshot_date
              AND oc.expiry_type IS NULL
              AND (
                  CASE WHEN EXISTS (
                      SELECT 1 FROM pub_config.stock_indices io
                      WHERE si.index_ric = oc.underlying_ric
                  ) THEN 'index' ELSE 'equity' END
              ) = :asset_type
        """), {
            'etype':         etype,
            'expiry_date':   expiry_date,
            'snapshot_date': snapshot_date,
            'asset_type':    asset_type,
        })

    print("Backfill abgeschlossen.")
