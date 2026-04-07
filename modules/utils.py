"""
modules/utils.py
Gemeinsame Hilfsfunktionen fuer die Options-Pipeline.
"""
import calendar
import datetime
import logging
import time

logger = logging.getLogger(__name__)

# US Federal Holidays, die auf einen Freitag fallen und den Monatsverfall verschieben.
# Jaehrlich pruefen und ggf. erweitern!
US_FRIDAY_HOLIDAYS: set[datetime.date] = {
    # Good Friday (Karfreitag) - US-Boersenferientag, trifft auf 3. Freitag April:
    datetime.date(2019, 4, 19),  # Good Friday 2019
    datetime.date(2022, 4, 15),  # Good Friday 2022
    datetime.date(2025, 4, 18),  # Good Friday 2025
    datetime.date(2030, 4, 19),  # Good Friday 2030
    datetime.date(2033, 4, 15),  # Good Friday 2033
    # Juneteenth - trifft auf 3. Freitag Juni:
    datetime.date(2026, 6, 19),  # Juneteenth 2026
    # Naechste Juneteenth-Freitage: 2032-06-19
}


# ---------------------------------------------------------------------------
# US / Eurex / Euronext / ICE: 3. Freitag (feiertagsbereinigt fuer US)
# ---------------------------------------------------------------------------

def third_friday(year: int, month: int) -> datetime.date:
    """3. Freitag eines Monats."""
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(4 - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=2)


def canonical_monthly_expiry(year: int, month: int) -> datetime.date:
    """
    3. Freitag, bei US-Feiertag Verschiebung auf Donnerstag davor.
    Gilt fuer: US (SPX, OEX, DJI), Eurex (DAX, SMI), Euronext (AEX), ICE (FTSE), B3 (BVSP).
    """
    tf = third_friday(year, month)
    if tf in US_FRIDAY_HOLIDAYS:
        return tf - datetime.timedelta(days=1)
    return tf


# ---------------------------------------------------------------------------
# KRX (KOSPI 200): 2. Donnerstag des Monats
# ---------------------------------------------------------------------------

def _second_thursday(year: int, month: int) -> datetime.date:
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(3 - d.weekday()) % 7)   # erster Donnerstag
    return d + datetime.timedelta(weeks=1)                  # zweiter Donnerstag


# ---------------------------------------------------------------------------
# HKEx (HSI): vorletzter Handelstag des Monats (Business Day vor dem letzten)
# ---------------------------------------------------------------------------

def _second_to_last_business_day(year: int, month: int) -> datetime.date:
    last_day = calendar.monthrange(year, month)[1]
    d = datetime.date(year, month, last_day)
    # letzten Handelstag finden
    while d.weekday() >= 5:
        d -= datetime.timedelta(days=1)
    # einen Handelstag zurueck
    d -= datetime.timedelta(days=1)
    while d.weekday() >= 5:
        d -= datetime.timedelta(days=1)
    return d


# ---------------------------------------------------------------------------
# NSE (Nifty 50): letzter Dienstag des Monats
# ---------------------------------------------------------------------------

def _last_tuesday(year: int, month: int) -> datetime.date:
    last_day = calendar.monthrange(year, month)[1]
    d = datetime.date(year, month, last_day)
    while d.weekday() != 1:                                 # 1 = Dienstag
        d -= datetime.timedelta(days=1)
    return d


# ---------------------------------------------------------------------------
# MX (Montreal Exchange, XIU.TO): letzter Freitag des Monats
# ---------------------------------------------------------------------------

def _last_friday(year: int, month: int) -> datetime.date:
    last_day = calendar.monthrange(year, month)[1]
    d = datetime.date(year, month, last_day)
    while d.weekday() != 4:                                 # 4 = Freitag
        d -= datetime.timedelta(days=1)
    return d


# ---------------------------------------------------------------------------
# B3 (BVSP): Mittwoch am naechsten zum 15. des Monats
# ---------------------------------------------------------------------------

def _wednesday_nearest_15th(year: int, month: int) -> datetime.date:
    mid = datetime.date(year, month, 15)
    best = None
    for delta in range(-7, 8):
        candidate = mid + datetime.timedelta(days=delta)
        if candidate.weekday() == 2:                        # 2 = Mittwoch
            if best is None or abs((candidate - mid).days) < abs((best - mid).days):
                best = candidate
    return best


# ---------------------------------------------------------------------------
# ASX (XJO): 3. Donnerstag des Monats
# ---------------------------------------------------------------------------

def _third_thursday(year: int, month: int) -> datetime.date:
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(3 - d.weekday()) % 7)   # erster Donnerstag
    return d + datetime.timedelta(weeks=2)                  # dritter Donnerstag


# ---------------------------------------------------------------------------
# Mapping: underlying_ric -> Monatsverfall-Funktion
# ---------------------------------------------------------------------------

_MONTHLY_EXPIRY_FN = {
    'us':   canonical_monthly_expiry,       # SPX, OEX, DJI, DAX, SMI, AEX, FTSE
    'krx':  _second_thursday,              # KOSPI 200
    'nse':  _last_tuesday,                 # Nifty 50 (seit Umstellung von Do auf Di)
    'hkex': _second_to_last_business_day,  # Hang Seng
    'asx':  _third_thursday,               # ASX 200
    'b3':   _wednesday_nearest_15th,       # IBOVESPA
    'mx':   _last_friday,                  # Montreal Exchange (XIU.TO)
}

_UNDERLYING_CONVENTION: dict[str, str] = {
    '.KS200':  'krx',
    '.NSEI':   'nse',
    '.HSI':    'hkex',
    '.AXJO':   'asx',
    '.BVSP':   'b3',
    # Alle anderen (SPX, OEX, DJI, GDAXI, SSMI, AEX, FTSE) -> 'us' (default)
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def classify_expiry(expiry_date: datetime.date,
                    snapshot_date: datetime.date,
                    asset_type: str,
                    underlying_ric: str | None = None) -> str:
    """
    Klassifiziert ein Verfallsdatum.

    Parameters:
        expiry_date:    Faelligkeitsdatum der Option
        snapshot_date:  snapshot_date der Chain (= 1. des Monats)
        asset_type:     'index' oder 'equity'
        underlying_ric: optionaler Underlying-RIC fuer exchange-spezifische Konvention

    Returns:
        'M'  Standard-Monatsverfall (exchange-spezifisch)
        'L'  LEAPS: tau > 1.0 zum snapshot_date (nur equity)
        'W'  Weekly: alles andere
    """
    # LEAPS: nur fuer Einzelaktien
    if asset_type == 'equity':
        tau = (expiry_date - snapshot_date).days / 365.25
        if tau > 1.0:
            return 'L'

    # Exchange-spezifisches Monatsverfall-Kriterium
    convention = _UNDERLYING_CONVENTION.get(underlying_ric, 'us') if underlying_ric else 'us'
    monthly_fn = _MONTHLY_EXPIRY_FN[convention]

    if expiry_date == monthly_fn(expiry_date.year, expiry_date.month):
        return 'M'

    return 'W'


def classify_futures_expiry(expiry_date: datetime.date) -> str:
    """
    Klassifiziert ein Futures-Verfallsdatum.

    Returns:
        'Q'  Quartalskontakt (Mar/Jun/Sep/Dec)
        'M'  Serienmonat (alle anderen Monate)
    """
    return 'Q' if expiry_date.month in (3, 6, 9, 12) else 'M'


# ---------------------------------------------------------------------------
# Eikon Retry-Wrapper
# ---------------------------------------------------------------------------

def eikon_fetch(fn, *args, max_retries: int = 24, sleep: float = 3600.0, **kwargs):
    """
    Ruft fn(*args, **kwargs) auf und wiederholt bei Rate-Limit-Fehlern (HTTP 429).

    Returns:
        Rückgabewert von fn, oder None bei permanentem Fehler.
    """
    for attempt in range(max_retries + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if ('429' in str(e) or 'limit' in str(e).lower()) and attempt < max_retries:
                logger.warning(
                    f'Rate limit – waiting {sleep}s ({attempt + 1}/{max_retries})'
                )
                time.sleep(sleep)
            else:
                logger.error(f'Eikon fetch failed after {attempt} retries: {e}')
                return None


# ---------------------------------------------------------------------------
# Historischer RIC (alle Index-Optionen)
# ---------------------------------------------------------------------------

_CALL_LETTERS_UC = list('ABCDEFGHIJKL')


def build_option_ric_hist(option_ric: str, expiry_date: datetime.date) -> str:
    """
    Baut historischen RIC mit ^-Suffix fuer beliebige Index-Optionen.

    Suffix-Konvention: ^{CALL_BUCHSTABE_DES_VERFALLMONATS}{2-stelliges-Jahr}
    Call-Buchstabe wird immer aus dem Verfallmonat abgeleitet (A=Jan..L=Dez),
    unabhaengig davon ob die Option ein Call oder Put ist.

    Beispiele:
        SPXb202669000.U, date(2026, 2, 20)  ->  SPXb202669000.U^B26
        LFE7500A26.L,    date(2026, 1, 16)  ->  LFE7500A26.L^A26
        STXE40000L5.EX,  date(2025, 12, 19) ->  STXE40000L5.EX^L25
        AXJO5800M6.AX,   date(2026, 1, 16)  ->  AXJO5800M6.AX^A26

    Parameters:
        option_ric:   Basisform (kein fuehrendes /, kein ^)
        expiry_date:  Verfallsdatum der Option

    Returns:
        Historischer RIC (mit ^BUCHSTABEJJ-Suffix)
    """
    suffix_letter = _CALL_LETTERS_UC[expiry_date.month - 1]
    yy = str(expiry_date.year)[-2:]
    return f'{option_ric}^{suffix_letter}{yy}'
