import os
from sqlalchemy import create_engine
from dotenv import load_dotenv

load_dotenv(r'C:\Users\Miebs\PycharmProjects\Config\.env')

engine = create_engine(
    f'postgresql://{os.getenv("DB_USER")}:{os.getenv("DB_PASS")}'
    f'@{os.getenv("DB_HOST")}:{os.getenv("DB_PORT")}/{os.getenv("DB_NAME")}'
)
