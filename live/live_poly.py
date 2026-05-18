"""
Polymarket WebSocket Client — Real-time market data collection.

SETUP:
  1. Find your market on polymarket.com and copy the slug from the URL.
     e.g. https://polymarket.com/event/bitcoin-up-or-down-february-9-10pm-et
                                      ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  2. Run lookup_market.py to fetch token IDs and save market_config.json:
       python lookup_market.py <slug>
     Example:
       python lookup_market.py bitcoin-up-or-down-february-9-10pm-et
  3. Run this script — it auto-loads market_config.json:
       python live_poly.py
     Data is saved to ../live_market_data/poly_market_data.csv.
  If no market_config.json exists, the hardcoded ASSET_IDS below are used
  as a fallback.
"""
from websocket import WebSocketApp
import csv
import json
import os
import queue
import threading
import time
from datetime import datetime
import pytz

from order_book import OrderBook

# --- USER CONFIGURATION---
def load_config():
    """Load market config from market_config.json if it exists.
    Returns (asset_ids, label_map, ordered_labels) or (None, None, None).
    """
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "market_config.json")
    if not os.path.exists(config_path):
        return None, None, None
    try:
        with open(config_path, "r") as f:
            config = json.load(f)
        outcomes = config["outcomes"]
        token_ids = [o["token_id"] for o in outcomes if o.get("token_id")]
        label_map = {o["token_id"]: o["label"] for o in outcomes if o.get("token_id")}
        ordered_labels = [o["label"] for o in outcomes if o.get("token_id")]
        if token_ids:
            print(f"Loaded {len(token_ids)} asset(s) from market_config.json "
                  f"(market: {config.get('question', 'unknown')})")
            return token_ids, label_map, ordered_labels
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        print(f"Warning: Could not parse market_config.json: {e}")
    return None, None, None

_HARDCODED_ASSET_IDS = [
    "67048479954843695179319250146078537481933450337322498206259397152035850997921",
]
_config_ids, LABEL_MAP, ORDERED_LABELS = load_config()
ASSET_IDS = _config_ids or _HARDCODED_ASSET_IDS

# frequency settings
PRINT_INTERVAL = 15.0  # Seconds between prints
CHANGE_THRESHOLD = 0.01  # Minimum change in bid/ask to trigger immediate print

# Configuration
WS_URL = "wss://ws-subscriptions-clob.polymarket.com"
MARKET_CHANNEL = "market"

# Data folder and file (resolved relative to script location)
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "live_market_data")
DATA_FILE = os.path.join(DATA_DIR, "poly_market_data.csv")


class _CsvWriter:
    """Background CSV writer — enqueues rows and flushes off the hot path."""

    def __init__(self, file, writer):
        self._file = file
        self._writer = writer
        self._q: queue.Queue = queue.Queue()
        threading.Thread(target=self._run, daemon=True, name="csv-writer-poly").start()

    def writerow(self, row):
        self._q.put(row)

    def close(self):
        self._q.put(None)

    def _run(self):
        while True:
            row = self._q.get()
            if row is None:
                break
            self._writer.writerow(row)
            self._file.flush()


class PolymarketWebSocket:
    """
    WebSocket client for Polymarket market or user channels.
    Handles connection, subscription, and message handling.
    """

    def __init__(self, channel_type, url, data, label_map=None, ordered_labels=None,
                 message_callback=None, verbose=True, print_interval=15.0, change_threshold=0.05,
                 on_price_update=None):
        self.channel_type = channel_type
        self.url = url
        self.data = data  # asset_ids for market
        self.message_callback = message_callback
        self.on_price_update = on_price_update
        self.verbose = verbose
        self.print_interval = print_interval  # Minimum seconds between prints
        self.change_threshold = change_threshold  # Minimum change in bid/ask to trigger print
        self.last_log_time = 0.0
        self.latest_data = {}  # Store latest bid/ask per asset_id
        self.last_trade_prices = {}  # Store last trade price per asset_id
        self.label_map = label_map or {}  # token_id -> label (e.g. "Up", "Down")
        self.ordered_labels = ordered_labels or []  # ["Up", "Down"] for CSV column order

        # Order book depth (maintained via book events on the market channel)
        self.books = {
            aid: {"bids": OrderBook(), "asks": OrderBook()}
            for aid in self.data
        }

        os.makedirs(DATA_DIR, exist_ok=True)

        _f = open(DATA_FILE, "a", newline="", encoding="utf-8")
        _w = csv.writer(_f)
        if os.path.getsize(DATA_FILE) == 0:
            _w.writerow([
                "timestamp_ms", "timestamp_est",
                "yes_bid", "yes_ask", "no_bid", "no_ask",
                "yes_mid", "no_mid"
            ])
            _f.flush()
        self._csv = _CsvWriter(_f, _w)

        # Build full WebSocket URL
        self.full_url = f"{url}/ws/{channel_type}"
        self.ws = WebSocketApp(
            self.full_url,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close,
            on_open=self.on_open,
        )

    @staticmethod
    def _ms_to_est(timestamp_ms):
        utc_dt = datetime.fromtimestamp(int(timestamp_ms) / 1000, tz=pytz.UTC)
        return utc_dt.astimezone(pytz.timezone("US/Eastern")).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    @staticmethod
    def _calc_mid(bid, ask, last):
        if bid is not None and ask is not None and (ask - bid) < 0.10:
            return round((bid + ask) / 2, 4)
        if last is not None:
            return last
        return ""

    def _handle_event(self, data):
        """Process a single event dict from the WebSocket."""
        event_type = data.get("event_type")
        timestamp = data.get("timestamp")

        if event_type == "last_trade_price":
            asset_id = data.get("asset_id")
            price = data.get("price")
            if asset_id and price is not None:
                self.last_trade_prices[asset_id] = float(price)

        elif event_type == "price_change":
            current_time = time.time()
            should_log = (current_time - self.last_log_time) >= self.print_interval
            any_update = False
            any_significant = False

            for change in data.get("price_changes", []):
                asset_id = change.get("asset_id")
                best_bid = float(change.get("best_bid", 0))
                best_ask = float(change.get("best_ask", 0))

                if asset_id:
                    prev = self.latest_data.get(asset_id, {})
                    prev_bid = float(prev.get("bid", 0))
                    prev_ask = float(prev.get("ask", 0))

                    bid_changed = abs(best_bid - prev_bid) >= self.change_threshold
                    ask_changed = abs(best_ask - prev_ask) >= self.change_threshold

                    self.latest_data[asset_id] = {"bid": best_bid, "ask": best_ask}
                    any_update = True
                    if bid_changed or ask_changed:
                        any_significant = True

            if any_update:
                if self.on_price_update and len(self.data) >= 2:
                    yes_data = self.latest_data.get(self.data[0], {})
                    no_data = self.latest_data.get(self.data[1], {})
                    self.on_price_update({
                        "yes_bid": yes_data.get("bid"),
                        "yes_ask": yes_data.get("ask"),
                        "no_bid": no_data.get("bid"),
                        "no_ask": no_data.get("ask"),
                    })

                if any_significant or should_log:
                    timestamp_est = self._ms_to_est(timestamp)
                    row = [timestamp, timestamp_est]
                    parts = []
                    yes_mid = ""
                    no_mid = ""
                    for i, asset_id in enumerate(self.data):
                        d = self.latest_data.get(asset_id, {})
                        bid = d.get("bid", "")
                        ask = d.get("ask", "")
                        last = self.last_trade_prices.get(asset_id)
                        mid = self._calc_mid(
                            bid if bid != "" else None,
                            ask if ask != "" else None,
                            last
                        )
                        side = "YES" if i == 0 else "NO"
                        row.extend([bid, ask])
                        mid_str = mid if mid != "" else "?"
                        parts.append(f"{side} {bid}/{ask} mid={mid_str}")
                        if i == 0:
                            yes_mid = mid
                        else:
                            no_mid = mid
                    row.extend([yes_mid, no_mid])
                    print(f"[{timestamp_est}] {' | '.join(parts)}")
                    self._csv.writerow(row)
                    self.last_log_time = current_time

        elif event_type == "book":
            asset_id = data.get("asset_id")
            if asset_id and asset_id in self.books:
                bids = [(round(float(b["price"]) * 100), float(b["size"]))
                        for b in data.get("bids", [])]
                asks = [(round(float(s["price"]) * 100), float(s["size"]))
                        for s in data.get("asks", [])]
                self.books[asset_id]["bids"].snapshot(bids)
                self.books[asset_id]["asks"].snapshot(asks)
                print(f"[Poly book] snapshot asset={asset_id[:8]}... bids={len(bids)} asks={len(asks)}")

        else:
            print(f"[Poly] Unknown event_type={event_type!r}")

    def on_message(self, ws, message):
        """Handle incoming WebSocket messages and extract key data."""
        if self.verbose:
            # Skip non-JSON messages (e.g., pings)
            if not message or not message.strip() or not (message.startswith('{') or message.startswith('[')):
                return
            try:
                raw = json.loads(message)
                # API sometimes sends a list of events instead of a single event
                events = raw if isinstance(raw, list) else [raw]
                for data in events:
                    if not isinstance(data, dict):
                        continue
                    self._handle_event(data)
            except (json.JSONDecodeError, ValueError, TypeError) as e:
                # Silently ignore parsing errors (e.g., pings, malformed messages)
                pass
        if self.message_callback:
            self.message_callback(message)

    def on_error(self, ws, error):
        """Handle WebSocket errors."""
        print("WebSocket Error:", error)

    def on_close(self, ws, close_status_code, close_msg):
        print("WebSocket Closed:", close_status_code, close_msg)
        for books in self.books.values():
            books["bids"].clear()
            books["asks"].clear()

    def on_open(self, ws):
        """Handle WebSocket opening and send subscription."""
        # Always clear books on (re)connect — on_close may not fire on hard drops
        for book_pair in self.books.values():
            book_pair["bids"].clear()
            book_pair["asks"].clear()

        if self.channel_type == MARKET_CHANNEL:
            subscription = {"assets_ids": self.data, "type": MARKET_CHANNEL}
            ws.send(json.dumps(subscription))
            print(f"Subscribed to market channel for assets: {self.data}")
        else:
            print("Invalid channel type")
            return

        # Start ping thread to keep connection alive
        ping_thread = threading.Thread(target=self._ping_loop, args=(ws,))
        ping_thread.daemon = True
        ping_thread.start()

    def subscribe_to_assets(self, asset_ids):
        """Subscribe to additional asset IDs (market channel only)."""
        if self.channel_type == MARKET_CHANNEL:
            message = {"assets_ids": asset_ids, "operation": "subscribe"}
            self.ws.send(json.dumps(message))
            for aid in asset_ids:
                self.books[aid] = {"bids": OrderBook(), "asks": OrderBook()}
            print(f"Subscribed to additional assets: {asset_ids}")

    def unsubscribe_from_assets(self, asset_ids):
        """Unsubscribe from asset IDs (market channel only)."""
        if self.channel_type == MARKET_CHANNEL:
            message = {"assets_ids": asset_ids, "operation": "unsubscribe"}
            self.ws.send(json.dumps(message))
            for aid in asset_ids:
                self.books.pop(aid, None)
            print(f"Unsubscribed from assets: {asset_ids}")

    # ---------- depth accessors ----------

    def get_yes_asks(self):
        """YES asks for the YES (first) asset, ascending."""
        if len(self.data) < 1:
            return []
        return self.books.get(self.data[0], {}).get("asks", OrderBook()).get_levels_ascending()

    def get_no_asks(self):
        """NO asks for the NO (second) asset, ascending."""
        if len(self.data) < 2:
            return []
        return self.books.get(self.data[1], {}).get("asks", OrderBook()).get_levels_ascending()

    def _ping_loop(self, ws):
        """Send ping every 5 seconds to keep connection alive."""
        while True:
            try:
                ws.send("PING")
                # print("Sent PING")  # Debug: confirm ping sent
                time.sleep(5)
            except Exception as e:
                print("Ping error:", e)
                break

    def close(self):
        self._csv.close()

    def run(self):
        """Start the WebSocket connection with reconnection logic."""
        reconnect_delay = 5
        max_delay = 60

        while True:
            try:
                self.ws.run_forever()
                # If run_forever exits cleanly, break
                break
            except Exception as e:
                print(f"WebSocket connection lost: {e}. Reconnecting in {reconnect_delay}s...")
                time.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, max_delay)
                # Reinitialize WebSocket for reconnection
                self.ws = WebSocketApp(
                    self.full_url,
                    on_message=self.on_message,
                    on_error=self.on_error,
                    on_close=self.on_close,
                    on_open=self.on_open,
                )


# Main Execution
if __name__ == "__main__":
    # Create market connection
    market_ws = PolymarketWebSocket(
        MARKET_CHANNEL, WS_URL, ASSET_IDS,
        label_map=LABEL_MAP, ordered_labels=ORDERED_LABELS,
        verbose=True, print_interval=PRINT_INTERVAL, change_threshold=CHANGE_THRESHOLD
    )
    # Example: Subscribe to additional assets
    # market_ws.subscribe_to_assets(["123"])

    # Run market channel
    market_ws.run()