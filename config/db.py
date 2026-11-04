"""
config/db.py
------------
SQLAlchemy-Engines fuer alle Postgres-Server.

Je Server ein Variablen-Praefix in der .env:
    DB_       lokaler Server  (Default, -> engine)
    DB_FACM_  FACM-Server

Verwendung:
    from config.db import engine              # lokal, unveraendert
    from config.db import get_engine
    engine_facm = get_engine('DB_FACM_')

Optional je Praefix: <PRAEFIX>SSLMODE (z.B. 'require'). Ohne Angabe gilt der
libpq-Default 'prefer' — TLS wenn der Server es anbietet, sonst Klartext.
"""
import os
from functools import lru_cache
from urllib.parse import quote_plus

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.pool import QueuePool

load_dotenv(r'C:\Users\Miebs\PycharmProjects\Config\.env')

_REQUIRED = ('USER', 'PASS', 'HOST', 'NAME')
CONNECT_TIMEOUT = 10  # s — ohne Timeout haengt ein Lauf bei totem Server minutenlang


def _url(prefix: str) -> str:
    """Baut die Connection-URL aus den <PRAEFIX>*-Variablen der .env."""
    cfg = {k: os.getenv(prefix + k)
           for k in (*_REQUIRED, 'PORT', 'SSLMODE')}

    missing = [prefix + k for k in _REQUIRED if not cfg[k]]
    if missing:
        raise RuntimeError(f'.env: {", ".join(missing)} fehlt')

    # quote_plus: @ : / # ? im Passwort wuerden die URL sonst falsch zerlegen
    url = (f'postgresql://{quote_plus(cfg["USER"])}:{quote_plus(cfg["PASS"])}'
           f'@{cfg["HOST"]}:{cfg["PORT"] or 5432}/{cfg["NAME"]}')
    return f'{url}?sslmode={cfg["SSLMODE"]}' if cfg['SSLMODE'] else url


@lru_cache(maxsize=None)
def get_engine(prefix: str = 'DB_') -> Engine:
    """Engine je .env-Praefix. Gecacht, damit pro Server nur ein Pool entsteht."""
    return create_engine(
        _url(prefix),
        poolclass=QueuePool,
        pool_size=5,
        max_overflow=10,
        pool_timeout=30,
        pool_recycle=1800,
        pool_pre_ping=True,
        connect_args={'connect_timeout': CONNECT_TIMEOUT},
    )


engine = get_engine()
