import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.pool import QueuePool

load_dotenv(r'C:\Users\Miebs\PycharmProjects\Config\.env')

_DATABASE_URL = (
    f'postgresql://{os.getenv("DB_USER")}:{os.getenv("DB_PASS")}'
    f'@{os.getenv("DB_HOST")}:{os.getenv("DB_PORT")}/{os.getenv("DB_NAME")}'
)

engine = create_engine(
    _DATABASE_URL,
    poolclass=QueuePool,
    pool_size=5,
    max_overflow=10,
    pool_timeout=30,
    pool_recycle=1800,
    pool_pre_ping=True,
)
