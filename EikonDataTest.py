import eikon as ek
import os
import pandas as pd
from dotenv import load_dotenv

load_dotenv(r'C:\Users\Miebs\PycharmProjects\Config\.env')
ek.set_app_key(os.getenv('REFINITIV_APP_KEY'))

def try_fetch(ric, fields, params):
    df, err = ek.get_data(ric, fields, params)
    data_cols = [c for c in df.columns if c not in ('Instrument', None)]
    rows = df[data_cols].dropna(how='all').shape[0] if data_cols else 0
    err_code = err[0]['code'] if err else None
    return rows, err_code, df

# =============================================================================
# SCHRITT 7: Ist der März-RIC wirklich daily?
# =============================================================================
print("=== SCHRITT 7: März 2026 — täglich? (kein Frq) ===")
rows, ec, df = try_fetch('HSI16700C6.HF',
                         ['TR.BIDPRICE', 'TR.ASKPRICE'],
                         {'SDate': '2026-03-01', 'EDate': '2026-03-28'})
print(f"Zeilen: {rows}, err: {ec}")
print(df.to_string())

# =============================================================================
# SCHRITT 8b: 2025 ohne Frq='D' — war das der Bug?
# =============================================================================
print("\n=== SCHRITT 8b: 2025-RICs ohne Frq='D' ===")
for ric, s, e in [
    ('HSI16700L5.HF', '2025-12-01', '2025-12-31'),
    ('HSI20000D5.HF', '2025-04-01', '2025-04-30'),
]:
    rows, ec, _ = try_fetch(ric, ['TR.BIDPRICE', 'TR.ASKPRICE'], {'SDate': s, 'EDate': e})
    print(f"  {ric} (kein Frq) → {rows} Zeilen, err={ec}")

# =============================================================================
# SCHRITT 8c: 2025 mit alternativen Feldern
# =============================================================================
print("\n=== SCHRITT 8c: 2025 — alternative Felder ===")
test_ric = 'HSI20000D5.HF'
for fields in [['TR.BIDPRICE', 'TR.ASKPRICE'], ['TRDPRC_1'], ['CLOSE'], ['BID', 'ASK']]:
    rows, ec, _ = try_fetch(test_ric, fields, {'SDate': '2025-04-01', 'EDate': '2025-04-30'})
    print(f"  {str(fields):35s} → {rows} Zeilen, err={ec}")

# =============================================================================
# SCHRITT 9: Welche Felder funktionieren? (auf 2024-RIC der sicher klappt)
# =============================================================================
print("\n=== SCHRITT 9: Feldvarianten auf HSI20000D4.HF (2024-04, stabil) ===")
for fields in [
    ['TR.BIDPRICE', 'TR.ASKPRICE'],
    ['TRDPRC_1'],
    ['CLOSE', 'HIGH', 'LOW'],
    ['TR.OPENPRICE', 'TR.HIGHPRICE', 'TR.LOWPRICE', 'TR.CLOSEPRICE'],
    ['TR.SETTLEMENTPRICE'],
    ['TR.Volume'],
    ['TR.OpenInterest'],
]:
    rows, ec, _ = try_fetch('HSI20000D4.HF', fields, {'SDate': '2024-04-01', 'EDate': '2024-04-30'})
    print(f"  {str(fields):55s} → {rows} Zeilen, err={ec}")

# =============================================================================
# SCHRITT 10: Rollover-Check final — was liefert D6 für 2016?
# =============================================================================
print("\n=== SCHRITT 10: Rollover-Check ===")
rows16, ec16, df16 = try_fetch('HSI20000D6.HF',
                                ['TR.BIDPRICE', 'TR.ASKPRICE'],
                                {'SDate': '2016-04-01', 'EDate': '2016-04-30'})
rows26, ec26, _    = try_fetch('HSI20000D6.HF',
                                ['TR.BIDPRICE', 'TR.ASKPRICE'],
                                {'SDate': '2026-04-01', 'EDate': '2026-04-30'})
print(f"D6 mit SDate 2016: {rows16} Zeilen, err={ec16}")
print(f"D6 mit SDate 2026: {rows26} Zeilen, err={ec26}")
