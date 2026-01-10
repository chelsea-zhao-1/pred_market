from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend
import base64
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.exceptions import InvalidSignature
import requests
import datetime


# ==============================================================================
# CONFIGURATION - UPDATE THESE VALUES FOR YOUR ACCOUNT
# ==============================================================================
PRIVATE_KEY_FILE = 'kalshi-main-key.key'
KALSHI_ACCESS_KEY = '42c80c6e-03de-49d1-84ed-6bd1132acb9c'
BASE_URL = 'https://api.elections.kalshi.com'
TICKER = 'KXNCAAFGAME-26JAN09OREIND-IND'
API_PATH = f'/trade-api/v2/markets/{TICKER}/orderbook'
HTTP_METHOD = "GET"
QUERY_PARAMS = None

# ==============================================================================
# HELPER FUNCTIONS
# ==============================================================================
def load_private_key_from_file(file_path):
    try:
        with open(file_path, "rb") as key_file:
            private_key = serialization.load_pem_private_key(
                key_file.read(),
                password=None,  # Change if your key is password-protected
                backend=default_backend()
            )
        return private_key
    except FileNotFoundError:
        raise FileNotFoundError(f"Private key file not found: {file_path}")
    except Exception as e:
        raise ValueError(f"Error loading private key: {e}")


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


def get_kalshi_headers(method: str, path: str, private_key: rsa.RSAPrivateKey) -> dict:
    current_time = datetime.datetime.now()
    timestamp_ms = int(current_time.timestamp() * 1000)
    timestamp_str = str(timestamp_ms)
    
    path_without_query = path.split('?')[0]
    msg_string = timestamp_str + method + path_without_query
    signature = sign_pss_text(private_key, msg_string)
    
    headers = {
        'KALSHI-ACCESS-KEY': KALSHI_ACCESS_KEY,
        'KALSHI-ACCESS-SIGNATURE': signature,
        'KALSHI-ACCESS-TIMESTAMP': timestamp_str
    }
    return headers


def make_kalshi_request(method: str, base_url: str, path: str, 
                        private_key: rsa.RSAPrivateKey, 
                        query_params: dict = None, 
                        json_data: dict = None) -> requests.Response:
    headers = get_kalshi_headers(method, path, private_key)
    url = base_url + path
    
    if method.upper() == "GET":
        response = requests.get(url, headers=headers, params=query_params)
    elif method.upper() == "POST":
        headers['Content-Type'] = 'application/json'
        response = requests.post(url, headers=headers, params=query_params, json=json_data)
    else:
        raise ValueError(f"Unsupported HTTP method: {method}")
    
    return response


# ==============================================================================
# MAIN EXECUTION
# ==============================================================================

if __name__ == "__main__":
    try:
        # Load private key
        print(f"Loading private key from: {PRIVATE_KEY_FILE}")
        private_key = load_private_key_from_file(PRIVATE_KEY_FILE)
        print("✓ Private key loaded successfully")
        
        # Make API request
        print(f"\nFetching orderbook for: {TICKER}")
        print(f"Making {HTTP_METHOD} request to: {BASE_URL}{API_PATH}")
        response = make_kalshi_request(
            method=HTTP_METHOD,
            base_url=BASE_URL,
            path=API_PATH,
            private_key=private_key,
            query_params=QUERY_PARAMS
        )
        
        # Check response
        print(f"\nResponse Status: {response.status_code}")
        if response.status_code == 200:
            print("✓ Request successful!")
            print("\nOrderbook Data:")
            try:
                import json
                data = response.json()
                print(json.dumps(data, indent=2))
            except:
                print(response.text)
        else:
            print(f"✗ Request failed with status {response.status_code}")
            print(f"Error: {response.text}")
            
    except FileNotFoundError as e:
        print(f"✗ Error: {e}")
        print("Please check that your PRIVATE_KEY_FILE path is correct.")
    except Exception as e:
        print(f"✗ Error: {e}")
        import traceback
        traceback.print_exc()
