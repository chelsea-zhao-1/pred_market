import asyncio
import csv
import json
import os
import queue
import threading
import time
import base64
from datetime import datetime

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

import pytz
import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from order_book import OrderBook

WS_URL = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
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


class _CsvWriter:
    """Background CSV writer — enqueues rows and flushes off the hot path."""

    def __init__(self, file, writer):
        self._file = file
        self._writer = writer
        self._q: queue.Queue = queue.Queue()
        threading.Thread(target=self._run, daemon=True, name="csv-writer").start()

    def writerow(self, row):
        self._q.put(row)

    def close(self):
        self._q.put(None)  # sentinel

    def _run(self):
        while True:
            row = self._q.get()
            if row is None:
                break
            self._writer.writerow(row)
            self._file.flush()


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
        self._ws = None         # set during connect() for mid-session operations
        self._cmd_id = 1        # monotonic command ID for subscribe/unsubscribe
        self._loop = None       # event loop ref for cross-thread scheduling
        self._subscription_sids = []  # sids returned by last subscribe; used for unsubscribe

        # Order book depth (maintained via orderbook_delta channel)
        self.yes_bids_book = OrderBook()
        self.no_bids_book = OrderBook()

        self._private_key = self._load_private_key()

        os.makedirs(DATA_DIR, exist_ok=True)
        file_exists = os.path.isfile(DATA_FILE) and os.path.getsize(DATA_FILE) > 0
        _f = open(DATA_FILE, "a", newline="")
        _w = csv.writer(_f)
        if not file_exists:
            _w.writerow(CSV_HEADER)
            _f.flush()
        self._csv = _CsvWriter(_f, _w)

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
        def _dollars(field):
            val = msg.get(field)
            if val is not None:
                try:
                    return float(val)
                except (TypeError, ValueError):
                    pass
            return None

        yes_bid = _dollars("yes_bid_dollars")
        yes_ask = _dollars("yes_ask_dollars")
        yes_last = _dollars("price_dollars")

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
        self._csv.writerow(row)

        yb = prices.get("yes_bid", "?")
        ya = prices.get("yes_ask", "?")
        nb = prices.get("no_bid", "?")
        na = prices.get("no_ask", "?")
        ym = yes_mid if yes_mid != "" else "?"
        nm = no_mid if no_mid != "" else "?"
        print(f"[{ts_est}] {ticker}: YES {yb}/{ya} mid={ym} | NO {nb}/{na} mid={nm}")

        if self.on_price_update:
            self.on_price_update(prices)

    # ---------- depth accessors ----------

    def get_yes_asks(self):
        """YES asks derived from NO bids: ask = 1.0 - no_bid_price.

        Returns [(price_dollars, qty), ...] sorted ascending (cheapest first).
        """
        no_bids = self.no_bids_book.get_levels_descending()
        return [(round(1.0 - price, 4), qty) for price, qty in no_bids]

    def get_no_asks(self):
        """NO asks derived from YES bids: ask = 1.0 - yes_bid_price.

        Returns [(price_dollars, qty), ...] sorted ascending (cheapest first).
        """
        yes_bids = self.yes_bids_book.get_levels_descending()
        return [(round(1.0 - price, 4), qty) for price, qty in yes_bids]

    # ---------- message routing ----------

    def _handle_message(self, data):
        msg_type = data.get("type")

        if msg_type == "ticker":
            msg = data.get("msg", {})
            ticker = msg.get("market_ticker")
            if ticker is None or ticker not in self.tickers:
                return

            prices = self._extract_prices(msg)

            if self._should_log(ticker, prices):
                self._log_ticker(ticker, prices)

        elif msg_type == "orderbook_snapshot":
            msg = data.get("msg", {})
            ticker = msg.get("market_ticker")
            if ticker is not None and ticker not in self.tickers:
                return
            # API now sends yes_dollars_fp/no_dollars_fp: [["price_str", "qty_str"], ...]
            yes_levels = [
                (round(float(e[0]) * 100), float(e[1]))
                for e in msg.get("yes_dollars_fp", [])
            ]
            no_levels = [
                (round(float(e[0]) * 100), float(e[1]))
                for e in msg.get("no_dollars_fp", [])
            ]
            self.yes_bids_book.snapshot(yes_levels)
            self.no_bids_book.snapshot(no_levels)
            print(f"Orderbook snapshot: {len(yes_levels)} yes levels, {len(no_levels)} no levels")

        elif msg_type == "orderbook_delta":
            msg = data.get("msg", {})
            ticker = msg.get("market_ticker")
            if ticker is not None and ticker not in self.tickers:
                return
            side = msg.get("side")
            # API now sends price_dollars (str) and delta_fp (str)
            price_str = msg.get("price_dollars")
            delta_str = msg.get("delta_fp")
            if side and price_str is not None and delta_str is not None:
                price_cents = round(float(price_str) * 100)
                delta = float(delta_str)
                book = self.yes_bids_book if side == "yes" else self.no_bids_book
                book.update(price_cents, delta)

        elif msg_type == "subscribed":
            msg = data.get("msg", {})
            # API may return "sid" (int, singular) or "sids" (list) — handle both
            sid = msg.get("sid")
            sids = msg.get("sids") or []
            if sid is not None:
                sids = [sid]
            self._subscription_sids.extend(sids)
            print(f"Subscription confirmed (sids: {sids})")

        elif msg_type == "error":
            print(f"Error from Kalshi: {data.get('msg', data)}")

        else:
            if msg_type not in (None,):
                print(f"[Kalshi] Unhandled message type: {msg_type}")

    # ---------- connection ----------

    async def connect(self):
        headers = self._generate_auth_headers()

        async with websockets.connect(WS_URL, additional_headers=headers) as ws:
            self._ws = ws
            self._subscription_sids = []  # fresh slate on every (re)connect

            self._cmd_id += 1
            await ws.send(json.dumps({
                "id": self._cmd_id,
                "cmd": "subscribe",
                "params": {"channels": ["ticker", "orderbook_delta"], "market_tickers": self.tickers},
            }))
            print(f"Subscribed to ticker + orderbook_delta for: {self.tickers}")

            async for message in ws:
                try:
                    data = json.loads(message)
                except json.JSONDecodeError:
                    continue
                self._handle_message(data)

    async def update_tickers(self, new_tickers):
        """Swap subscriptions mid-connection: unsubscribe old, subscribe new."""
        if self._ws is None:
            print("Warning: Cannot update tickers — no active connection.")
            return

        old = self.tickers
        # Update self.tickers first so the message filter rejects old-ticker
        # messages as soon as the event loop resumes after the awaits below.
        self.tickers = new_tickers

        # Unsubscribe old: use stored sids if available, else fall back to market_tickers
        self._cmd_id += 1
        if self._subscription_sids:
            unsub_params = {"sids": self._subscription_sids}
        else:
            unsub_params = {"channels": ["ticker", "orderbook_delta"], "market_tickers": old}
        await self._ws.send(json.dumps({
            "id": self._cmd_id,
            "cmd": "unsubscribe",
            "params": unsub_params,
        }))

        # Clear stale data for old tickers and reset sids for the new subscription
        for t in old:
            self.latest_data.pop(t, None)
        self.yes_bids_book.clear()
        self.no_bids_book.clear()
        self._subscription_sids = []

        self._cmd_id += 1
        await self._ws.send(json.dumps({
            "id": self._cmd_id,
            "cmd": "subscribe",
            "params": {"channels": ["ticker", "orderbook_delta"], "market_tickers": new_tickers},
        }))

        print(f"Kalshi rotation: {old} -> {new_tickers}")

    async def run(self):
        self._loop = asyncio.get_running_loop()
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
        self._csv.close()


# ==============================================================================
# ENTRY POINT
# ==============================================================================

if __name__ == "__main__":
    TARGET_TICKERS = ["KXBTC15M-26FEB271930-30"]  # Update per 15-min window
    KALSHI_KEY_ID = os.environ["KALSHI_KEY_ID"]
    KALSHI_PRIVATE_KEY_PATH = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "kalshi-main-key.key"
    )

    client = KalshiWebSocket(
        tickers=TARGET_TICKERS,
        key_id=KALSHI_KEY_ID,
        private_key_path=KALSHI_PRIVATE_KEY_PATH,
        print_interval=15.0,
        change_threshold=0.01,
    )
    try:
        asyncio.run(client.run())
    except KeyboardInterrupt:
        print("\nStopped by user.")
    finally:
        client.close()
