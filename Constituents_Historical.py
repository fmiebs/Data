import eikon as ek
import pandas as pd
from sqlalchemy import create_engine, text
from datetime import datetime
import os
import time
from dotenv import load_dotenv

# --- CONFIGURATION ---
RIC = '.DJI'
START_DATE = '2015-01-01'
END_DATE = datetime.now().strftime('%Y-%m-%d')
FREQUENCY = 'MS'
SCHEMA = 'pub_equity'
TABLE = 'index_constituents'
SLEEP_TIME = 0.5  # Safety pause in seconds between requests

# Load credentials
load_dotenv()
# Using your specific .env variable name here:
APP_KEY = os.getenv("REFINITIV_APP_KEY")

ek.set_app_key(APP_KEY)
engine = create_engine(
    f'postgresql://{os.getenv("DB_USER")}:{os.getenv("DB_PASS")}@{os.getenv("DB_HOST")}:{os.getenv("DB_PORT")}/{os.getenv("DB_NAME")}')


def get_date_range(start, end, freq):
    """Generates a list of dates based on start, end, and frequency."""
    return pd.date_range(start=start, end=end, freq=freq).strftime('%Y-%m-%d').tolist()


def check_data_exists(ric, date_str):
    """Checks if data for a specific RIC and date already exists in the database."""
    query = text(f"SELECT COUNT(*) FROM {SCHEMA}.{TABLE} WHERE index_ric = :ric AND snapshot_date = :date")
    with engine.connect() as conn:
        count = conn.execute(query, {"ric": ric, "date": date_str}).scalar()
    return count > 0


def fetch_constituents(ric, date_str):
    """
    Fetches index constituents with fixed column mapping for Refinitiv naming.
    """
    try:
        # Requesting specific fields
        data, err = ek.get_data(f"0#{ric}", ["TR.CommonName", "TR.TickerSymbol"], {"SDate": date_str})

        if data is not None and not data.empty:
            df = data.copy()

            # Refinitiv often returns 'Company Common Name' for 'TR.CommonName'
            # We map these precisely to our database column names
            rename_map = {
                'Instrument': 'constituent_ric',
                'Company Common Name': 'company_name',  # This was the culprit
                'Ticker Symbol': 'symbol'
            }

            df = df.rename(columns=rename_map)

            # Ensure the other metadata is added
            df['index_ric'] = ric
            df['snapshot_date'] = date_str

            # Double Check: Only keep columns that actually exist in our DB table
            valid_columns = ['index_ric', 'constituent_ric', 'symbol', 'company_name', 'snapshot_date']
            df = df[valid_columns]

            return df
        return None
    except Exception as e:
        # ... (rest of the error handling remains the same)
        if "429" in str(e) or "limit" in str(e).lower():
            print(f"⚠️ API Limit hit at {date_str}. Sleeping for 60 seconds...")
            time.sleep(60)
            return fetch_constituents(ric, date_str)
        else:
            print(f"❌ Error fetching data for {date_str}: {e}")
            return None


def run_sync():
    """Main orchestration function with rate limiting."""
    print(f"Starting sync for {RIC} (Variable: REFINITIV_APP_KEY used)")

    dates = get_date_range(START_DATE, END_DATE, FREQUENCY)

    # Ensure Index exists in overview
    with engine.connect() as conn:
        conn.execute(text(f"INSERT INTO {SCHEMA}.index_overview (index_ric) VALUES (:ric) ON CONFLICT DO NOTHING"),
                     {"ric": RIC})
        conn.commit()

    for date in dates:
        if check_data_exists(RIC, date):
            print(f"Skipping {date}: Already in Database.")
            continue

        print(f"Processing {date}...")
        df = fetch_constituents(RIC, date)

        if df is not None:
            df.to_sql(TABLE, engine, schema=SCHEMA, if_exists='append', index=False)
            print(f"✅ Stored {len(df)} rows for {date}.")

        # Rate Limiting
        time.sleep(SLEEP_TIME)


if __name__ == "__main__":
    run_sync()