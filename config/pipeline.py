"""
config/pipeline.py
------------------
Hilfsfunktionen fuer das Pipeline-Run-Log in pub_config.pipeline_runs.

Verwendung:
    from config.pipeline import run_start, run_finish

    run_id = run_start('fetch_prices_daily', 'live', snapshot_date='2026-03-01')
    try:
        ...
        run_finish(run_id, rows_written=1234)
    except Exception as e:
        run_finish(run_id, error=str(e))
        raise
"""
import logging
from datetime import date

from sqlalchemy import text

from config.db import engine

logger = logging.getLogger(__name__)


def run_start(module: str, mode: str, snapshot_date: str | None = None) -> int | None:
    """
    Schreibt einen 'running'-Eintrag in pub_config.pipeline_runs.

    Returns:
        id des neuen Eintrags, oder None bei Fehler.
    """
    snap = date.fromisoformat(snapshot_date) if snapshot_date else None
    try:
        with engine.begin() as conn:
            row = conn.execute(text("""
                INSERT INTO pub_config.pipeline_runs
                    (module, mode, status, snapshot_date)
                VALUES (:module, :mode, 'running', :snap)
                RETURNING id
            """), {'module': module, 'mode': mode, 'snap': snap}).fetchone()
        return row[0] if row else None
    except Exception as e:
        logger.warning(f'pipeline_runs INSERT failed: {e}')
        return None


def run_finish(run_id: int | None, rows_written: int | None = None,
               error: str | None = None) -> None:
    """
    Schliesst einen pipeline_runs-Eintrag mit 'success' oder 'error' ab.
    """
    if run_id is None:
        return
    status = 'error' if error else 'success'
    try:
        with engine.begin() as conn:
            conn.execute(text("""
                UPDATE pub_config.pipeline_runs
                SET finished_at  = now(),
                    rows_written = :rows,
                    status       = :status,
                    error_msg    = :error
                WHERE id = :id
            """), {'rows': rows_written, 'status': status,
                   'error': error, 'id': run_id})
    except Exception as e:
        logger.warning(f'pipeline_runs UPDATE failed: {e}')
