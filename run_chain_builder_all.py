"""
run_chain_builder_all.py
------------------------
Runs build_index_hist_chains.py sequentially for all indices in
pub_config.stock_indices where fetch_index_options_hist = TRUE.
Backfill starts at 2016-01.
"""
import subprocess
import sys
import time

from sqlalchemy import text
from config.db import engine

LOG_DIR = 'logs'
START   = '2016-01'


def get_indices() -> list[str]:
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT index_ric FROM pub_config.stock_indices "
            "WHERE fetch_index_options_hist_backfill = TRUE "
            "ORDER BY index_ric"
        )).fetchall()
    return [r[0] for r in rows]


indices = get_indices()

if not indices:
    print("No indices with fetch_index_options_hist = TRUE found.")
    sys.exit(0)

print(f"Indices to process: {indices}")

for index in indices:
    log_file = f"{LOG_DIR}/chain_builder_{index.replace('.', '').replace('/', '')}.log"
    print(f'\n{"="*60}', flush=True)
    print(f'Starting chain builder: {index}', flush=True)
    print(f'Log: {log_file}', flush=True)
    print(f'{"="*60}', flush=True)

    with open(log_file, 'a') as f:
        result = subprocess.run(
            [sys.executable, '-u', 'build_index_hist_chains.py',
             '--index', index, '--start', START],
            stdout=f,
            stderr=f,
        )

    if result.returncode == 0:
        print(f'Done: {index}', flush=True)
    else:
        print(f'ERROR (exit {result.returncode}): {index} — check {log_file}', flush=True)

    time.sleep(2)

print('\nAll indices done.')
