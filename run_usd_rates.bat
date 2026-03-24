@echo off
cd /d "C:\Users\Miebs\PycharmProjects\Data"
"C:\Users\Miebs\PycharmProjects\.venv_universal\Scripts\python.exe" -m modules.fetch_usd_rates >> "C:\Users\Miebs\PycharmProjects\Data\logs\usd_rates.log" 2>&1
