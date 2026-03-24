"""
modules/utils.py
Gemeinsame Hilfsfunktionen fuer die Options-Pipeline.
"""
import datetime

# US Federal Holidays, die auf einen Freitag fallen und den Monatsverfall verschieben.
# Jaehrlich pruefen und ggf. erweitern!
US_FRIDAY_HOLIDAYS: set[datetime.date] = {
    datetime.date(2026, 6, 19),  # Juneteenth 2026 faellt auf Freitag
    # Naechste Juneteenth-Freitage: 2032-06-19
}


def third_friday(year: int, month: int) -> datetime.date:
    """3. Freitag eines Monats."""
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(4 - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=2)


def canonical_monthly_expiry(year: int, month: int) -> datetime.date:
    """
    Tatsaechliches Datum des Standard-Monatsverfalls (3. Freitag).
    Bei US-Feiertag (Freitag): Verschiebung auf Donnerstag davor.
    """
    tf = third_friday(year, month)
    if tf in US_FRIDAY_HOLIDAYS:
        return tf - datetime.timedelta(days=1)
    return tf


def classify_expiry(expiry_date: datetime.date,
                    snapshot_date: datetime.date,
                    asset_type: str) -> str:
    """
    Klassifiziert ein Verfallsdatum.

    Parameters:
        expiry_date:   Fälligkeitsdatum der Option
        snapshot_date: snapshot_date der Chain (= 1. des Monats)
        asset_type:    'index' oder 'equity'

    Returns:
        'M'  Standard-Monatsverfall (3. Freitag, feiertagsbereinigt)
        'L'  LEAPS: tau > 1.0 zum snapshot_date (nur equity)
        'W'  Weekly: alles andere
    """
    # LEAPS: nur fuer Einzelaktien, tau > 1.0 zum snapshot_date
    if asset_type == 'equity':
        tau = (expiry_date - snapshot_date).days / 365.25
        if tau > 1.0:
            return 'L'

    # M: Standard-Monatsverfall (gilt fuer Index und Equity gleich)
    if expiry_date == canonical_monthly_expiry(expiry_date.year, expiry_date.month):
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
