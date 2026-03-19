import os
import eikon as ek
from dotenv import load_dotenv

load_dotenv(r'C:\Users\Miebs\PycharmProjects\Config\.env')
ek.set_app_key(os.getenv('REFINITIV_APP_KEY'))
