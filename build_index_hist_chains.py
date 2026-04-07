"""
build_index_hist_chains.py
---------------------------
Rekonstruiert historische monatliche Option-Chains für Index-Optionen
und schreibt Stammdaten in pub_options.options_chains sowie
Preiszeitreihen direkt in pub_options.options_prices.

Unterstützte Indizes (in INDEX_CONFIG):
  .FTSE       FTSE 100     LFE{strike}{letter}{yy2}.L         25pt, ±50%
  .STOXX50E   EuroStoxx50  STXE{strike*10}{letter}{yy1}.EX    50pt, ±40%
  .AXJO       ASX 200      AXJO{strike}{letter}{yy1}.AX        25pt, ±40%
  .SSMI       SMI          OSMI{strike*10}{letter}{yy1}.EX    100pt, ±40%
  .NSEI       Nifty 50     NIF{strike_6d}{letter}{yy1}.NS      50pt, ±40%
  DXJ         DXJ ETF      DXJ{ltr}{DD}{yy2}{strike*100}.U     $2,  ±35%
  XIU.TO      XIU ETF      XIU{ltr}{DD}{yy2}{strike*100}.M     $1,  ±35%
  .OEX        S&P 100      OEX{ltr_lc}{DD}{yy2}{strike*10}.U  $25,  ±35%
  .DJI        Dow Jones    DJX{ltr}{DD}{yy2}{strike*100}.U     $5,  ±35%
  .SPX        S&P 500      SPX{ltr_lc}{DD}{yy2}{strike*10_5d}.U $5, ±60%

Historical suffix convention: {base_ric}^{CALL_LETTER_UC}{yy2digit}
^-Suffix verwendet immer den Call-Monatsbuchstaben — auch für Puts.

Resumable: bereits in DB vorhandene Verfallsdaten werden übersprungen.

Aufruf:
  python build_index_hist_chains.py --index .FTSE [--start 2016-01] [--end 2025-12]
"""
import argparse
import datetime
import time
from collections import defaultdict
from dataclasses import dataclass

import eikon as ek
import pandas as pd
from dateutil.relativedelta import relativedelta
from sqlalchemy import text

from config.db import engine
from config import eikon_init  # noqa
from modules.utils import _MONTHLY_EXPIRY_FN, canonical_monthly_expiry

SCHEMA        = 'pub_options'
TABLE         = 'options_chains'
PRICES_TABLE  = 'options_prices'
BATCH_SIZE    = 75
SLEEP_TIME    = 0.15


# ---------------------------------------------------------------------------
# Index configuration
# ---------------------------------------------------------------------------

@dataclass
class IndexConfig:
    underlying_ric:    str
    ric_prefix:        str
    ric_suffix:        str
    call_letters:      list[str]
    put_letters:       list[str]
    strike_step:       int
    range_pct:         float = 0.50
    strike_multiplier: int   = 1
    strike_pad:        int   = 0
    year_digits:       int   = 2
    date_in_ric:       bool  = False
    letter_case:       str   = 'upper'
    expiry_convention: str   = 'us'
    level_divisor:     float = 1.0

    def get_expiry(self, year: int, month: int) -> datetime.date:
        fn = _MONTHLY_EXPIRY_FN.get(self.expiry_convention, canonical_monthly_expiry)
        return fn(year, month)

    def _letter(self, letter: str) -> str:
        return letter.lower() if self.letter_case == 'lower' else letter.upper()

    def call_letter(self, letter: str) -> str:
        """Returns the call letter for the same month (converts put letter → call letter)."""
        lu = letter.upper()
        call_uc = [l.upper() for l in self.call_letters]
        put_uc  = [l.upper() for l in self.put_letters]
        if lu in put_uc:
            return call_uc[put_uc.index(lu)]
        return lu

    def build_hist_suffix(self, letter: str, yy2: str) -> str:
        """Always ^{CALL_LETTER_UC}{2-digit-year} — put letters are mapped to call letters."""
        return f'^{self.call_letter(letter)}{yy2}'

    def build_ric(self, letter: str, yy_main: str, strike: int,
                  expiry: datetime.date = None) -> str:
        ric_code = strike * self.strike_multiplier
        s = (str(ric_code).zfill(self.strike_pad)
             if self.strike_pad else str(ric_code))
        l = self._letter(letter)
        if self.date_in_ric:
            assert expiry is not None
            dd = expiry.strftime('%d')
            return f'{self.ric_prefix}{l}{dd}{yy_main}{s}{self.ric_suffix}'
        else:
            return f'{self.ric_prefix}{s}{l}{yy_main}{self.ric_suffix}'

    def decode_ric(self, ric: str) -> tuple[int, str]:
        """Returns (actual_strike, 'CALL'|'PUT')."""
        inner = ric[len(self.ric_prefix):-len(self.ric_suffix)]
        if self.date_in_ric:
            letter = inner[0].upper()
            code   = int(inner[5:])
        else:
            if self.year_digits == 1:
                letter = inner[-2].upper()
                code   = int(inner[:-2])
            else:
                letter = inner[-3].upper()
                code   = int(inner[:-3])
        actual = code // self.strike_multiplier
        opt    = 'CALL' if letter in [l.upper() for l in self.call_letters] else 'PUT'
        return actual, opt

    def get_strike_candidates(self, level: float) -> list[int]:
        lo = max(self.strike_step,
                 round(level * (1 - self.range_pct) / self.strike_step) * self.strike_step)
        hi = round(level * (1 + self.range_pct) / self.strike_step) * self.strike_step
        return list(range(lo, hi + 1, self.strike_step))


_CALLS = list('ABCDEFGHIJKL')
_PUTS  = list('MNOPQRSTUVWX')

INDEX_CONFIG: dict[str, IndexConfig] = {

    '.FTSE': IndexConfig(
        underlying_ric='.FTSE', ric_prefix='LFE', ric_suffix='.L',
        call_letters=_CALLS, put_letters=_PUTS,
        strike_step=5, range_pct=0.60,
        strike_multiplier=1, year_digits=2,
        expiry_convention='us',
    ),

    '.STOXX50E': IndexConfig(
        underlying_ric='.STOXX50E', ric_prefix='STXE', ric_suffix='.EX',
        call_letters=_CALLS, put_letters=_PUTS,
        strike_step=5, range_pct=0.60,
        strike_multiplier=10, year_digits=1,
        expiry_convention='us',
    ),

    '.AXJO': IndexConfig(
        underlying_ric='.AXJO', ric_prefix='AXJO', ric_suffix='.AX',
        call_letters=_CALLS, put_letters=_PUTS,
        strike_step=5, range_pct=0.60,
        strike_multiplier=1, year_digits=1,
        expiry_convention='asx',
    ),

    '.SSMI': IndexConfig(
        underlying_ric='.SSMI', ric_prefix='OSMI', ric_suffix='.EX',
        call_letters=_CALLS, put_letters=_PUTS,
        strike_step=5, range_pct=0.60,
        strike_multiplier=10, year_digits=1,
        expiry_convention='us',
    ),

    '.NSEI': IndexConfig(
        underlying_ric='.NSEI', ric_prefix='NIF', ric_suffix='.NS',
        call_letters=_CALLS, put_letters=_PUTS,
        strike_step=5, range_pct=0.60,
        strike_multiplier=1, strike_pad=6, year_digits=1,
        expiry_convention='nse',
    ),

    'DXJ': IndexConfig(
        underlying_ric='DXJ', ric_prefix='DXJ', ric_suffix='.U',
        call_letters=_CALLS, put_letters=_PUTS,
        strike_step=5, range_pct=0.60,
        strike_multiplier=100, year_digits=2, date_in_ric=True,
        expiry_convention='us',
    ),

    'XIU.TO': IndexConfig(
        underlying_ric='XIU.TO', ric_prefix='XIU', ric_suffix='.M',
        call_letters=_CALLS, put_letters=_PUTS,
        strike_step=5, range_pct=0.60,
        strike_multiplier=100, strike_pad=5, year_digits=2, date_in_ric=True,
        expiry_convention='mx',
    ),

    '.OEX': IndexConfig(
        underlying_ric='.OEX', ric_prefix='OEX', ric_suffix='.U',
        call_letters=_CALLS, put_letters=_PUTS,
        strike_step=5, range_pct=0.60,
        strike_multiplier=10, year_digits=2, date_in_ric=True,
        letter_case='lower',
        expiry_convention='us',
    ),

    '.DJI': IndexConfig(
        underlying_ric='.DJI', ric_prefix='DJX', ric_suffix='.U',
        call_letters=_CALLS, put_letters=_PUTS,
        strike_step=5, range_pct=0.60,
        strike_multiplier=100, year_digits=2, date_in_ric=True,
        level_divisor=100.0,
        expiry_convention='us',
    ),

    '.SPX': IndexConfig(
        underlying_ric='.SPX', ric_prefix='SPX', ric_suffix='.U',
        call_letters=list('abcdefghijkl'), put_letters=list('mnopqrstuvwx'),
        strike_step=5, range_pct=0.60,
        strike_multiplier=10, strike_pad=5, year_digits=2, date_in_ric=True,
        letter_case='lower',
        expiry_convention='us',
    ),
}


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_index_level(underlying_ric: str, d: datetime.date,
                    level_divisor: float = 1.0) -> float | None:
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT close_price FROM pub_equity.index_prices_daily "
            "WHERE index_ric = :ric AND trade_date <= :d "
            "ORDER BY trade_date DESC LIMIT 1"
        ), {"ric": underlying_ric, "d": d}).scalar()
    if row is not None:
        return float(row) / level_divisor

    try:
        sdate = (d - datetime.timedelta(days=7)).strftime('%Y-%m-%d')
        edate = d.strftime('%Y-%m-%d')
        data, _ = ek.get_data(
            underlying_ric,
            ['TR.PriceClose.Date', 'TR.PriceClose'],
            {'SDate': sdate, 'EDate': edate}
        )
        if data is not None and not data.empty:
            col = next((c for c in data.columns if c and 'close' in str(c).lower()), None)
            if col:
                val = data[col].dropna()
                if not val.empty:
                    return float(val.iloc[-1]) / level_divisor
    except Exception:
        pass
    return None


def get_known_rics(underlying_ric: str) -> tuple[set[str], dict, dict]:
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT option_ric, expiry_date, strike, option_type "
            f"FROM {SCHEMA}.{TABLE} WHERE underlying_ric = :u"
        ), {"u": underlying_ric}).fetchall()

    ric_set = set()
    known_strikes = defaultdict(lambda: {'CALL': set(), 'PUT': set()})
    ric_expiry: dict[str, datetime.date] = {}
    for ric, expiry, strike, opt_type in rows:
        ric_set.add(ric)
        known_strikes[expiry][opt_type].add(int(strike))
        ric_expiry[ric] = expiry
    return ric_set, known_strikes, ric_expiry


# ---------------------------------------------------------------------------
# Eikon fetch
# ---------------------------------------------------------------------------

def _do_fetch(fetch_rics: list[str], sdate: str, edate: str,
              retries: int = 0) -> tuple[list[str], pd.DataFrame] | None:
    """Fetches bid/ask time series. Returns (active_rics, price_df) or None on error."""
    try:
        data, _ = ek.get_data(
            fetch_rics,
            ['TR.BIDPRICE.Date', 'TR.BIDPRICE', 'TR.ASKPRICE'],
            {'SDate': sdate, 'EDate': edate}
        )
    except Exception as e:
        if ('429' in str(e) or 'limit' in str(e).lower()) and retries < 24:
            print(f"  Rate limit – waiting 1h (retry {retries+1}/24)...")
            time.sleep(3600)
            return _do_fetch(fetch_rics, sdate, edate, retries + 1)
        print(f"  ERROR in _do_fetch: {e}")
        return None

    if data is None or data.empty:
        return [], pd.DataFrame()

    instr_col = data.columns[0]
    bid_col   = next((c for c in data.columns if c and 'bid' in str(c).lower()), None)
    ask_col   = next((c for c in data.columns if c and 'ask' in str(c).lower()), None)

    if bid_col is None and ask_col is None:
        return [], pd.DataFrame()

    has_price = (
        (data[bid_col].notna() if bid_col else False) |
        (data[ask_col].notna() if ask_col else False)
    )
    active = [r for r in data.loc[has_price, instr_col].unique().tolist() if r]
    return active, data.loc[has_price].copy()


def fetch_batch(cfg: IndexConfig, rics: list[str], sdate: str, edate: str,
                yy2: str) -> tuple[list[str], pd.DataFrame] | None:
    """Fetches historical RICs. Returns (normalised base RICs, price_df) or None on error."""
    hist_rics = []
    for r in rics:
        inner  = r[len(cfg.ric_prefix):-len(cfg.ric_suffix)]
        if cfg.date_in_ric:
            letter = inner[0]
        elif cfg.year_digits == 1:
            letter = inner[-2]
        else:
            letter = inner[-3]
        hist_rics.append(r + cfg.build_hist_suffix(letter, yy2))

    result = _do_fetch(hist_rics, sdate, edate)
    if result is None:
        return None

    raw, price_df = result
    base_rics = list({r.split('^')[0] for r in raw})
    return base_rics, price_df


def _write_prices(price_df: pd.DataFrame) -> int:
    """Normalise RICs, deduplicate and write to options_prices. Returns row count written."""
    if price_df is None or price_df.empty:
        return 0

    instr_col = price_df.columns[0]
    date_col  = next((c for c in price_df.columns if c and 'date' in str(c).lower()), None)
    bid_col   = next((c for c in price_df.columns if c and 'bid'  in str(c).lower()), None)
    ask_col   = next((c for c in price_df.columns if c and 'ask'  in str(c).lower()), None)

    if date_col is None:
        return 0

    df = price_df.copy()
    df['option_ric'] = df[instr_col]  # hist RIC inkl. ^-Suffix — eindeutig über 10-Jahres-Zyklen
    df['price_date'] = pd.to_datetime(df[date_col], utc=True).dt.date
    df['price_bid']  = df[bid_col].where(df[bid_col].notna(), other=None) if bid_col else None
    df['price_ask']  = df[ask_col].where(df[ask_col].notna(), other=None) if ask_col else None

    df = df[df['price_date'].notna()]
    df = df.drop_duplicates(subset=['option_ric', 'price_date'])
    df = df[df['price_bid'].notna() | df['price_ask'].notna()]

    if df.empty:
        return 0

    records = [
        {
            'option_ric': row['option_ric'],
            'price_date': row['price_date'],
            'price_bid':  row['price_bid']  if pd.notna(row['price_bid'])  else None,
            'price_ask':  row['price_ask']  if pd.notna(row['price_ask'])  else None,
        }
        for _, row in df.iterrows()
    ]

    with engine.begin() as conn:
        conn.execute(text(f"""
            INSERT INTO {SCHEMA}.{PRICES_TABLE}
                (option_ric, price_date, price_bid, price_ask)
            VALUES (:option_ric, :price_date, :price_bid, :price_ask)
            ON CONFLICT (option_ric, price_date) DO NOTHING
        """), records)

    return len(records)


# ---------------------------------------------------------------------------
# Per-month processing
# ---------------------------------------------------------------------------

def _make_chain_records(cfg: IndexConfig, active_rics: list[str],
                        expiry: datetime.date, yy2: str) -> list[dict]:
    """Build options_chains insert records from a list of active base RICs."""
    first   = expiry.replace(day=1)
    records = []
    for ric in active_rics:
        try:
            strike, opt_type = cfg.decode_ric(ric)
        except Exception:
            continue
        inner  = ric[len(cfg.ric_prefix):-len(cfg.ric_suffix)]
        letter = (inner[0] if cfg.date_in_ric
                  else inner[-2] if cfg.year_digits == 1
                  else inner[-3])
        records.append({
            'underlying_ric':  cfg.underlying_ric,
            'option_ric':      ric,
            'option_ric_hist': ric + cfg.build_hist_suffix(letter, yy2),
            'expiry_date':     expiry,
            'strike':          strike,
            'option_type':     opt_type,
            'expiry_type':     'M',
            'first_seen_date': first,
            'source':          'EK',
        })
    return records


def process_month(cfg: IndexConfig, expiry: datetime.date,
                  known_rics: set[str], known_strikes: dict,
                  ric_expiry: dict) -> tuple[set[str], list]:
    yy2     = str(expiry.year)[-2:]
    yy_main = str(expiry.year)[-cfg.year_digits:]
    mi      = expiry.month - 1
    c_let   = cfg.call_letters[mi]
    p_let   = cfg.put_letters[mi]

    level = get_index_level(cfg.underlying_ric, expiry, cfg.level_divisor)
    if not level:
        print(f"  No index level for {expiry}, skipping.")
        return set(), []

    existing  = known_strikes.get(expiry, {'CALL': set(), 'PUT': set()})
    range_stk = set(cfg.get_strike_candidates(level))
    all_stk   = range_stk | existing['CALL'] | existing['PUT']

    all_rics = (
        [cfg.build_ric(c_let, yy_main, s, expiry) for s in sorted(all_stk)] +
        [cfg.build_ric(p_let, yy_main, s, expiry) for s in sorted(all_stk)]
    )

    def _needs_fetch(r: str) -> bool:
        if r not in known_rics:
            return True
        if cfg.year_digits == 1:
            stored = ric_expiry.get(r)
            if stored is not None and stored < expiry:
                return True
        return False

    missing_rics = [r for r in all_rics if _needs_fetch(r)]
    if not missing_rics:
        return set(), []

    sdate = (expiry - relativedelta(months=18)).strftime('%Y-%m-%d')
    edate = expiry.strftime('%Y-%m-%d')

    active         = []
    all_price_dfs  = []
    failed_batches = []

    for i in range(0, len(missing_rics), BATCH_SIZE):
        batch  = missing_rics[i:i + BATCH_SIZE]
        result = fetch_batch(cfg, batch, sdate, edate, yy2)
        if result is None:
            failed_batches.append((batch, sdate, edate, yy2, expiry))
        else:
            base_rics, price_df = result
            active.extend(base_rics)
            if not price_df.empty:
                all_price_dfs.append(price_df)
        time.sleep(SLEEP_TIME)

    if not active:
        return set(), failed_batches

    records = _make_chain_records(cfg, active, expiry, yy2)
    if not records:
        return set(), failed_batches

    new_rics = _write_records(records, known_strikes)

    # Write prices collected during the fetch
    if all_price_dfs:
        n_prices = _write_prices(pd.concat(all_price_dfs, ignore_index=True))
    else:
        n_prices = 0

    return new_rics, failed_batches


def _write_records(records: list[dict], known_strikes: dict) -> set[str]:
    """Insert records into options_chains, update known_strikes in-memory."""
    if not records:
        return set()
    for attempt in range(3):
        try:
            with engine.begin() as conn:
                conn.execute(text(f"""
                    INSERT INTO {SCHEMA}.{TABLE}
                        (underlying_ric, option_ric, option_ric_hist,
                         expiry_date, strike, option_type, expiry_type,
                         first_seen_date, source)
                    VALUES
                        (:underlying_ric, :option_ric, :option_ric_hist,
                         :expiry_date, :strike, :option_type, :expiry_type,
                         :first_seen_date, :source)
                    ON CONFLICT (underlying_ric, option_ric) DO NOTHING
                """), records)
            break
        except Exception as e:
            if 'deadlock' in str(e).lower() and attempt < 2:
                print(f"  Deadlock on write – retry {attempt+1}/2 ...")
                time.sleep(2)
            else:
                print(f"  DB write error: {e}")
                return set()
    new_ric_set = set()
    for r in records:
        new_ric_set.add(r['option_ric'])
        known_strikes[r['expiry_date']][r['option_type']].add(r['strike'])
    return new_ric_set


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description='Build historical monthly index option chains.')
    p.add_argument('--index', required=True,
                   choices=list(INDEX_CONFIG.keys()),
                   help=f'Underlying RIC. Available: {list(INDEX_CONFIG.keys())}')
    p.add_argument('--start', default='2016-01',
                   help='First month YYYY-MM (default: 2016-01)')
    p.add_argument('--end', default=None,
                   help='Last month YYYY-MM excl. (default: current month)')
    return p.parse_args()


def iter_months(cfg: IndexConfig, start: str, end: str | None):
    sy, sm = int(start[:4]), int(start[5:7])
    d = datetime.date(sy, sm, 1)
    if end:
        ey, em = int(end[:4]), int(end[5:7])
        end_d  = datetime.date(ey, em, 1)
    else:
        end_d = datetime.date.today().replace(day=1)
    while d < end_d:
        yield cfg.get_expiry(d.year, d.month)
        d = (d.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)


if __name__ == '__main__':
    args = parse_args()
    cfg  = INDEX_CONFIG[args.index]

    months                                = list(iter_months(cfg, args.start, args.end))
    known_rics, known_strikes, ric_expiry = get_known_rics(cfg.underlying_ric)

    print(f"{cfg.underlying_ric} hist chains | start={args.start} | "
          f"months={len(months)} | rics already in DB={len(known_rics)}")

    t0          = time.time()
    retry_queue = []

    for idx, expiry in enumerate(months):
        level            = get_index_level(cfg.underlying_ric, expiry, cfg.level_divisor) or 0
        new_rics, failed = process_month(cfg, expiry, known_rics, known_strikes, ric_expiry)
        known_rics      |= new_rics
        retry_queue.extend(failed)
        elapsed = time.time() - t0
        avg     = elapsed / (idx + 1)
        eta     = avg * (len(months) - idx - 1)
        print(f"[{idx+1:3d}/{len(months)}] {expiry}  "
              f"{cfg.underlying_ric}~{level:,.0f}  "
              f"{len(new_rics):4d} new | {len(failed):2d} failed | "
              f"{elapsed:5.0f}s | ETA {eta/60:.1f}min",
              flush=True)

    # ---- Retry failed batches ----
    permanently_failed: list[tuple] = []
    if retry_queue:
        print(f"\nRetrying {len(retry_queue)} failed batch(es)...")
        for batch, sdate, edate, yy2, expiry in retry_queue:
            result = fetch_batch(cfg, batch, sdate, edate, yy2)
            if result is None:
                print(f"  Retry failed again: expiry={expiry}, {len(batch)} RICs – skipping.")
                permanently_failed.append((batch, sdate, edate, yy2, expiry))
                time.sleep(SLEEP_TIME)
                continue
            base_rics, price_df = result
            records = _make_chain_records(cfg, base_rics, expiry, yy2)
            new_rics = _write_records(records, known_strikes)
            if not price_df.empty:
                _write_prices(price_df)
            known_rics |= new_rics
            print(f"  Retry OK: expiry={expiry}, {len(new_rics)} opts written.")
            time.sleep(SLEEP_TIME)

    # ---- Final status ----
    elapsed_total = time.time() - t0
    if permanently_failed:
        failed_expiries = sorted({str(expiry) for _, _, _, _, expiry in permanently_failed})
        resume_month   = min(str(e)[:7] for e in failed_expiries)
        print(f"\nINCOMPLETE — {len(permanently_failed)} batch(es) permanently failed.")
        print(f"  Failed expiries: {', '.join(failed_expiries)}")
        print(f"  Resume with: python build_index_hist_chains.py "
              f"--index {cfg.underlying_ric} --start {resume_month}")
        print(f"  Total time: {elapsed_total:.0f}s")
    else:
        print(f"\nDone (all batches successful). {elapsed_total:.0f}s total")
