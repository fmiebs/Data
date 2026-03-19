import eikon as ek
import os
from dotenv import load_dotenv

load_dotenv(r'C:\Users\Miebs\PycharmProjects\Config\.env')
ek.set_app_key(os.getenv('REFINITIV_APP_KEY'))


# Funktionierender Test
#chain_df, _ = ek.get_data('0#SPX*.U', ['DSPLY_NAME'])
df, err = ek.get_data('0#DJX*.U', ['EXPIR_DATE', 'STRIKE_PRC', 'PUTCALLIND'])
print(df['PUTCALLIND'].unique())
print(df['PUTCALLIND'].value_counts())
x=0