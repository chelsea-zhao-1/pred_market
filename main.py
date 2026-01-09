import requests
import pandas as pd
import time
from datetime import datetime, timezone
import base64
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend

# ==============================================================================
# CONFIGURATION
# ==============================================================================

class GameConfig:
    def __init__(self, game_name, start_time_iso, end_time_iso, poly_market_a_id, poly_market_a_name, poly_market_b_id, poly_market_b_name, kalshi_ticker):
        self.game_name = game_name
        self.start_dt = pd.Timestamp(start_time_iso).tz_convert("UTC")
        self.end_dt = pd.Timestamp(end_time_iso).tz_convert("UTC")
        self.start_ts = int(self.start_dt.timestamp())
        self.end_ts = int(self.end_dt.timestamp())
        
        self.poly_market_a_id = poly_market_a_id
        self.poly_market_a_name = poly_market_a_name
        self.poly_market_b_id = poly_market_b_id
        self.poly_market_b_name = poly_market_b_name
        
        self.kalshi_ticker = kalshi_ticker

# --- USER: UPDATE THIS SECTION FOR YOUR GAME ---
CONFIG = GameConfig(
    game_name="giants_vs_raiders",
    # Times (ISO Format with Timezone)
    start_time_iso="2025-12-28 16:00:00-05:00", # EST
    end_time_iso="2025-12-28 19:00:00-05:00",   # EST
    
    # Polymarket IDs (Search on Polymarket CLOB or website)
    poly_market_a_name="giants",
    poly_market_a_id='73918557025200502509758871521442098141094234730265102200780859356079067046938',
    poly_market_b_name="raiders",
    poly_market_b_id='33566079885445560469389463113194331939749019446295875471687366161299733602645',
    
    # Kalshi Ticker
    kalshi_ticker="KXNFLGAME-25DEC28NYGLV" 
)

# --- General Settings ---
POLY_FIDELITY = 1 # 1-minute intervals
KALSHI_KEY_ID = "42c80c6e-03de-49d1-84ed-6bd1132acb9c"
KALSHI_PRIVATE_KEY_PATH = "proj1.txt"
KALSHI_HOST = "https://api.elections.kalshi.com" # Updated Host
KALSHI_API_VERSION_PATH = "/trade-api/v2"

# ==============================================================================
# POLYMARKET FUNCTIONS
# ==============================================================================

def fetch_polymarket_data(market_id, name):
    print(f"Fetching Polymarket data for {name}...")
    url = f"https://clob.polymarket.com/prices-history?market={market_id}&fidelity={POLY_FIDELITY}&startTs={CONFIG.start_ts}&endTs={CONFIG.end_ts}"
    response = requests.get(url)
    response.raise_for_status()
    data = response.json()
    
    if "history" in data:
        df = pd.DataFrame(data["history"])
        df['datetime_est'] = pd.to_datetime(df['t'], unit='s', utc=True).dt.tz_convert('US/Eastern')
        df = df.rename(columns={'p': f'price_{name}'})
        return df[['t', 'datetime_est', f'price_{name}']]
    else:
        print(f"No history data found for {name}")
        return pd.DataFrame()

# ==============================================================================
# KALSHI AUTHENTICATION
# ==============================================================================

def load_private_key_from_file(file_path):
    try:
        with open(file_path, "rb") as key_file:
            private_key = serialization.load_pem_private_key(
                key_file.read(),
                password=None,  # or provide a password if your key is encrypted
                backend=default_backend()
            )
        return private_key
    except FileNotFoundError:
        print(f"Error: Private key file not found at {file_path}")
        return None
    except Exception as e:
        print(f"Error loading private key: {e}")
        return None

def sign_pss_text(private_key: rsa.RSAPrivateKey, text: str) -> str:
    message = text.encode('utf-8')
    try:
        signature = private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH
            ),
            hashes.SHA256()
        )
        return base64.b64encode(signature).decode('utf-8')
    except InvalidSignature as e:
        raise ValueError("RSA sign PSS failed") from e

def get_kalshi_headers(method, path):
    current_time = datetime.now()
    timestamp = current_time.timestamp()
    current_time_milliseconds = int(timestamp * 1000)
    timestamp_str = str(current_time_milliseconds)

    private_key = load_private_key_from_file(KALSHI_PRIVATE_KEY_PATH)
    if not private_key:
        raise ValueError("Could not load private key. Please check configuration.")

    # Strip query parameters from path before signing
    path_without_query = path.split('?')[0]
    msg_string = timestamp_str + method + path_without_query
    sig = sign_pss_text(private_key, msg_string)

    headers = {
        'KALSHI-ACCESS-KEY': KALSHI_KEY_ID,
        'KALSHI-ACCESS-SIGNATURE': sig,
        'KALSHI-ACCESS-TIMESTAMP': timestamp_str
    }
    return headers

# ==============================================================================
# KALSHI DATA FETCHING
# ==============================================================================

def fetch_kalshi_trades(ticker):
    """Fetches trades for a specific ticker using RSA authentication."""
    path = f"{KALSHI_API_VERSION_PATH}/markets/trades" # Updated endpoint
    url = f"{KALSHI_HOST}{path}"
    
    all_trades = []
    cursor = None
    
    print(f"Starting Kalshi trade fetch for ticker: {ticker}")
    
    while True:
        params = {
            "limit": 100,
            "ticker": ticker,
            "min_ts": CONFIG.start_ts,
            "max_ts": CONFIG.end_ts
        }
        if cursor:
            params["cursor"] = cursor
        
        try:
            # Generate headers for this specific request
            headers = get_kalshi_headers("GET", path)
            
            response = requests.get(url, headers=headers, params=params)
            response.raise_for_status()
            data = response.json()
            
            trades = data.get("trades", [])
            if not trades:
                break
                
            all_trades.extend(trades)
            print(f"Fetched {len(trades)} trades. Total so far: {len(all_trades)}")
            
            cursor = data.get("cursor")
            if not cursor:
                break
                
            time.sleep(0.2) # Rate limiting
            
        except Exception as e:
            print(f"Error fetching Kalshi trades: {e}")
            if "401" in str(e) or "403" in str(e):
                print("Authentication failed. Check your Key ID and Private Key.")
            break
            
    return all_trades

def process_kalshi_trades(trades):
    if not trades:
        return pd.DataFrame()

    df = pd.DataFrame(trades)
    
    # Convert timestamp
    if 'created_time' in df.columns:
        df['datetime_est'] = pd.to_datetime(df['created_time']).dt.tz_convert('US/Eastern')
    elif 'time' in df.columns:
        df['datetime_est'] = pd.to_datetime(df['time']).dt.tz_convert('US/Eastern')
        
    # Filter by time range
    df = df[(df['datetime_est'] >= CONFIG.start_dt.tz_convert('US/Eastern')) & (df['datetime_est'] <= CONFIG.end_dt.tz_convert('US/Eastern'))]
    df = df.sort_values('datetime_est')
    
    # Rename price column if needed (Kalshi usually has 'price' or 'yes_price')
    # Assuming 'price' is in cents, convert to probability
    if 'price' in df.columns:
        df['price_kalshi'] = df['price'] / 100.0
    
    return df[['datetime_est', 'price_kalshi', 'count', 'taker_side']] # Keep relevant columns

# ==============================================================================
# MAIN EXECUTION
# ==============================================================================

if __name__ == "__main__":
    # 1. Fetch Polymarket Data
    df_poly_a = fetch_polymarket_data(CONFIG.poly_market_a_id, CONFIG.poly_market_a_name)
    df_poly_b = fetch_polymarket_data(CONFIG.poly_market_b_id, CONFIG.poly_market_b_name)

    if not df_poly_a.empty and not df_poly_b.empty:
        # Merge Polymarket data
        df_poly = pd.merge(df_poly_a, df_poly_b[['t', f'price_{CONFIG.poly_market_b_name}']], on='t', how='outer')
        df_poly = df_poly.sort_values('t')
        
        # Recalculate datetime for all rows
        df_poly['datetime_est'] = pd.to_datetime(df_poly['t'], unit='s', utc=True).dt.tz_convert('US/Eastern')
        
        # Forward fill prices
        df_poly[f'price_{CONFIG.poly_market_a_name}'] = df_poly[f'price_{CONFIG.poly_market_a_name}'].ffill().bfill()
        df_poly[f'price_{CONFIG.poly_market_b_name}'] = df_poly[f'price_{CONFIG.poly_market_b_name}'].ffill().bfill()
        
        df_poly = df_poly[['datetime_est', f'price_{CONFIG.poly_market_a_name}', f'price_{CONFIG.poly_market_b_name}']]
        
        out_poly_file = f'polymarket_{CONFIG.game_name}_raw.csv'
        df_poly.to_csv(out_poly_file, index=False)
        print(f"Saved Polymarket data to '{out_poly_file}'")

    # 2. Fetch Kalshi Data
    if KALSHI_KEY_ID != "YOUR_KEY_ID_HERE" and KALSHI_PRIVATE_KEY_PATH != "YOUR_PRIVATE_KEY_FILE_PATH_HERE":
        kalshi_trades = fetch_kalshi_trades(CONFIG.kalshi_ticker)
        df_kalshi = process_kalshi_trades(kalshi_trades)
        
        if not df_kalshi.empty:
            print(f"Kalshi data fetched: {len(df_kalshi)} rows.")
            # Save raw Kalshi trades
            out_kalshi_file = f'kalshi_{CONFIG.game_name}_raw_trades.csv'
            df_kalshi.to_csv(out_kalshi_file, index=False)
            print(f"Saved Kalshi raw trades to {out_kalshi_file}")
        else:
            print("No Kalshi trades found or error occurred.")
    else:
        print("\n[WARNING] Kalshi credentials not set. Skipping Kalshi data fetch.")
        print("Please update KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PATH in main.py")