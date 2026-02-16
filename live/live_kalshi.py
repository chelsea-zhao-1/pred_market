import asyncio
import json
import csv
import os
import time
import base64
from datetime import datetime

import pytz
import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

# ==============================================================================
# CONFIGURATION
# ==============================================================================

TARGET_TICKERS = ["KXBTC15M-26FEB151230-30"]  # Replace with your ticker(s)
PRINT_INTERVAL = 15.0       # Seconds between forced log entries
CHANGE_THRESHOLD = 0.01     # Minimum price change to trigger immediate log

KALSHI_KEY_ID = "42c80c6e-03de-49d1-84ed-6bd1132acb9c"
KALSHI_PRIVATE_KEY_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "kalshi-main-key.key"
)

WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
WS_PATH = "/trade-api/ws/v2"

DATA_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "live_market_data"
)
DATA_FILE = os.path.join(DATA_DIR, "kalshi_market_data.csv")

CSV_HEADER = [
    "timestamp_ms", "timestamp_est", "market_ticker",
    "yes_bid", "yes_ask", "no_bid", "no_ask",
    "yes_mid", "no_mid"
]


class KalshiWebSocket:
    """Async WebSocket client for Kalshi real-time market data."""

    def __init__(self, tickers, key_id, private_key_path,
                 print_interval=15.0, change_threshold=0.01,
                 on_price_update=None):
        self.tickers = tickers
        self.key_id = key_id
        self.private_key_path = private_key_path
        self.print_interval = print_interval
        self.change_threshold = change_threshold
        self.on_price_update = on_price_update

        self.est_tz = pytz.timezone("US/Eastern")
        self.latest_data = {}   # {ticker: {yes_bid, yes_ask, no_bid, no_ask}}
        self.last_log_time = 0.0

        self._private_key = self._load_private_key()

        # CSV setup
        os.makedirs(DATA_DIR, exist_ok=True)
        file_exists = os.path.isfile(DATA_FILE) and os.path.getsize(DATA_FILE) > 0
        self._csv_file = open(DATA_FILE, "a", newline="")
        self._csv_writer = csv.writer(self._csv_file)
        if not file_exists:
            self._csv_writer.writerow(CSV_HEADER)
            self._csv_file.flush()

    # ---------- auth ----------

    def _load_private_key(self):
        with open(self.private_key_path, "rb") as f:
            return serialization.load_pem_private_key(f.read(), password=None)

    def _generate_auth_headers(self):
        timestamp_ms = str(int(time.time() * 1000))
        message = f"{timestamp_ms}GET{WS_PATH}".encode("utf-8")

        signature = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )

        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        }

    # ---------- helpers ----------

    @staticmethod
    def _ms_to_est(timestamp_ms):
        utc_dt = datetime.fromtimestamp(int(timestamp_ms) / 1000, tz=pytz.UTC)
        est_dt = utc_dt.astimezone(pytz.timezone("US/Eastern"))
        return est_dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    @staticmethod
    def _extract_prices(msg):
        """Extract yes_bid/yes_ask from ticker message, derive no side.

        Kalshi has a single order book -- no prices are the complement of yes.
        """
        def _get(field_dollars, field_cents):
            val = msg.get(field_dollars)
            if val is not None:
                try:
                    return float(val)
                except (TypeError, ValueError):
                    pass
            cents = msg.get(field_cents)
            if cents is not None:
                try:
                    return float(cents) / 100
                except (TypeError, ValueError):
                    pass
            return None

        yes_bid = _get("yes_bid_dollars", "yes_bid")
        yes_ask = _get("yes_ask_dollars", "yes_ask")
        yes_last = _get("price_dollars", "price")

        return {
            "yes_bid": yes_bid,
            "yes_ask": yes_ask,
            "no_bid":  round(1 - yes_ask, 4) if yes_ask is not None else None,
            "no_ask":  round(1 - yes_bid, 4) if yes_bid is not None else None,
            "yes_last": yes_last,
            "no_last":  round(1 - yes_last, 4) if yes_last is not None else None,
        }

    def _should_log(self, ticker, prices):
        prev = self.latest_data.get(ticker)
        if prev is None:
            return True

        for key in ("yes_bid", "yes_ask", "no_bid", "no_ask"):
            old_val = prev.get(key)
            new_val = prices.get(key)
            if old_val is None or new_val is None:
                continue
            if abs(new_val - old_val) >= self.change_threshold:
                return True

        if time.time() - self.last_log_time >= self.print_interval:
            return True

        return False

    @staticmethod
    def _calc_mid(bid, ask, last):
        if bid is not None and ask is not None and (ask - bid) < 0.10:
            return round((bid + ask) / 2, 4)
        if last is not None:
            return last
        return ""

    def _log_ticker(self, ticker, prices):
        ts_ms = int(time.time() * 1000)
        ts_est = self._ms_to_est(ts_ms)

        self.latest_data[ticker] = prices
        self.last_log_time = time.time()

        yes_mid = self._calc_mid(prices.get("yes_bid"), prices.get("yes_ask"), prices.get("yes_last"))
        no_mid = self._calc_mid(prices.get("no_bid"), prices.get("no_ask"), prices.get("no_last"))

        row = [
            ts_ms, ts_est, ticker,
            prices.get("yes_bid", ""),
            prices.get("yes_ask", ""),
            prices.get("no_bid", ""),
            prices.get("no_ask", ""),
            yes_mid,
            no_mid,
        ]
        self._csv_writer.writerow(row)
        self._csv_file.flush()

        yb = prices.get("yes_bid", "?")
        ya = prices.get("yes_ask", "?")
        nb = prices.get("no_bid", "?")
        na = prices.get("no_ask", "?")
        ym = yes_mid if yes_mid != "" else "?"
        nm = no_mid if no_mid != "" else "?"
        print(f"[{ts_est}] {ticker}: YES {yb}/{ya} mid={ym} | NO {nb}/{na} mid={nm}")

        if self.on_price_update:
            self.on_price_update(prices)

    # ---------- message routing ----------

    def _handle_message(self, data):
        msg_type = data.get("type")

        if msg_type == "ticker":
            msg = data.get("msg", {})
            ticker = msg.get("market_ticker")
            if ticker is None:
                return

            prices = self._extract_prices(msg)

            if self._should_log(ticker, prices):
                self._log_ticker(ticker, prices)

        elif msg_type == "subscribed":
            sids = data.get("msg", {}).get("sids", [])
            print(f"Subscription confirmed (sids: {sids})")

        elif msg_type == "error":
            print(f"Error from Kalshi: {data.get('msg', data)}")

    # ---------- connection ----------

    async def connect(self):
        headers = self._generate_auth_headers()

        async with websockets.connect(WS_URL, additional_headers=headers) as ws:
            subscribe_msg = {
                "id": 1,
                "cmd": "subscribe",
                "params": {
                    "channels": ["ticker"],
                    "market_tickers": self.tickers,
                },
            }
            await ws.send(json.dumps(subscribe_msg))
            print(f"Subscribed to ticker for: {self.tickers}")

            async for message in ws:
                try:
                    data = json.loads(message)
                except json.JSONDecodeError:
                    continue
                self._handle_message(data)

    async def run(self):
        reconnect_delay = 5
        max_delay = 60

        while True:
            connected_at = time.time()
            try:
                await self.connect()
            except (websockets.exceptions.ConnectionClosed,
                    websockets.exceptions.ConnectionClosedError,
                    ConnectionError, OSError) as e:
                print(f"Connection lost: {e}. Reconnecting in {reconnect_delay}s...")
            except Exception as e:
                print(f"Unexpected error: {e}. Reconnecting in {reconnect_delay}s...")

            # Reset backoff if connection was stable for > 30s
            if time.time() - connected_at > 30:
                reconnect_delay = 5
            else:
                reconnect_delay = min(reconnect_delay * 2, max_delay)

            await asyncio.sleep(reconnect_delay)

    def close(self):
        self._csv_file.close()


# ==============================================================================
# ENTRY POINT
# ==============================================================================

if __name__ == "__main__":
    client = KalshiWebSocket(
        tickers=TARGET_TICKERS,
        key_id=KALSHI_KEY_ID,
        private_key_path=KALSHI_PRIVATE_KEY_PATH,
        print_interval=PRINT_INTERVAL,
        change_threshold=CHANGE_THRESHOLD,
    )
    try:
        asyncio.run(client.run())
    except KeyboardInterrupt:
        print("\nStopped by user.")
    finally:
        client.close()
