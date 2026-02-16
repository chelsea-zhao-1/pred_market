"""
Cross-Platform Soft Arbitrage Scanner — Kalshi + Polymarket

Monitors both platforms' BTC 15-minute up/down markets in real-time
and alerts when cross-platform mispricings create +EV opportunities.

SETUP:
  1. Configure the Polymarket market:
       python lookup_market.py <slug>
     This saves market_config.json with token IDs.

  2. Set KALSHI_TICKER below to the current Kalshi market ticker.

  3. Run:
       python arbitrage_scanner.py

See ARBITRAGE_PLAN.md for full design details.
"""

import asyncio
import csv
import math
import os
import threading
import time
from datetime import datetime

import pytz

from live_kalshi import KalshiWebSocket
from live_poly import PolymarketWebSocket, load_config, MARKET_CHANNEL

# ==============================================================================
# CONFIGURATION
# ==============================================================================

KALSHI_TICKER = "KXBTC15M-26FEB151230-30"  # Update per 15-min window

KALSHI_KEY_ID = "42c80c6e-03de-49d1-84ed-6bd1132acb9c"
KALSHI_PRIVATE_KEY_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "kalshi-main-key.key"
)

POLY_WS_URL = "wss://ws-subscriptions-clob.polymarket.com"

# Minimum NET profit (after fees) to trigger an alert.
MIN_MARGIN = 0.0

# Don't alert if either platform's data is older than this (seconds).
STALE_THRESHOLD = 5.0

PRINT_INTERVAL = 15.0
CHANGE_THRESHOLD = 0.01

DATA_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "live_market_data"
)

ALERTS_HEADER = [
    "timestamp_ms", "timestamp_est", "leg",
    "kalshi_yes_ask", "kalshi_no_ask",
    "poly_yes_ask", "poly_no_ask",
    "cost", "kalshi_fee", "poly_fee", "net_profit",
]

EST_TZ = pytz.timezone("US/Eastern")


# ==============================================================================
# FEE CALCULATIONS
# ==============================================================================

def kalshi_taker_fee(price):
    """Kalshi taker fee per contract: ceil_to_cent(0.07 * p * (1-p))."""
    if price <= 0 or price >= 1:
        return 0.0
    return math.ceil(0.07 * price * (1 - price) * 100) / 100


def poly_taker_fee(price):
    """Polymarket taker fee per share for 15-min crypto markets.

    Formula: 0.25 * p * (p * (1-p))^2, rounded to 4 decimal places.
    """
    if price <= 0 or price >= 1:
        return 0.0
    return round(0.25 * price * (price * (1 - price)) ** 2, 4)


# ==============================================================================
# ARBITRAGE DETECTOR
# ==============================================================================

class ArbitrageDetector:
    """Thread-safe cross-platform arbitrage detector.

    Receives price updates from both Kalshi and Polymarket WS clients
    and checks whether buying YES on one + NO on the other costs < $1.
    """

    def __init__(self, min_margin=0.0):
        self.min_margin = min_margin
        self.kalshi_prices = None
        self.poly_prices = None
        self.kalshi_updated_at = 0.0
        self.poly_updated_at = 0.0
        self.lock = threading.Lock()

        os.makedirs(DATA_DIR, exist_ok=True)
        session_ts = datetime.now(EST_TZ).strftime("%Y%m%d_%H%M%S")
        self.alerts_file = os.path.join(DATA_DIR, f"arb_alerts_{session_ts}.csv")
        self._csv_file = open(self.alerts_file, "w", newline="")
        self._csv_writer = csv.writer(self._csv_file)
        self._csv_writer.writerow(ALERTS_HEADER)
        self._csv_file.flush()

    def update_kalshi(self, prices):
        with self.lock:
            self.kalshi_prices = prices
            self.kalshi_updated_at = time.time()
            self._check()

    def update_poly(self, prices):
        with self.lock:
            self.poly_prices = prices
            self.poly_updated_at = time.time()
            self._check()

    def _check(self):
        k = self.kalshi_prices
        p = self.poly_prices
        if k is None or p is None:
            return

        now = time.time()
        if (now - self.kalshi_updated_at) > STALE_THRESHOLD:
            return
        if (now - self.poly_updated_at) > STALE_THRESHOLD:
            return

        # Leg A: Buy YES @ Kalshi + Buy NO @ Polymarket
        k_yes_ask = k.get("yes_ask")
        p_no_ask = p.get("no_ask")
        if k_yes_ask is not None and p_no_ask is not None:
            k_fee = kalshi_taker_fee(k_yes_ask)
            p_fee = poly_taker_fee(p_no_ask)
            cost_a = k_yes_ask + p_no_ask
            net = 1.0 - cost_a - k_fee - p_fee
            if net > self.min_margin:
                self._alert("A", cost_a, k_fee, p_fee, net, k, p)

        # Leg B: Buy NO @ Kalshi + Buy YES @ Polymarket
        k_no_ask = k.get("no_ask")
        p_yes_ask = p.get("yes_ask")
        if k_no_ask is not None and p_yes_ask is not None:
            k_fee = kalshi_taker_fee(k_no_ask)
            p_fee = poly_taker_fee(p_yes_ask)
            cost_b = k_no_ask + p_yes_ask
            net = 1.0 - cost_b - k_fee - p_fee
            if net > self.min_margin:
                self._alert("B", cost_b, k_fee, p_fee, net, k, p)

    def _alert(self, leg, cost, k_fee, p_fee, net_profit, k, p):
        ts_ms = int(time.time() * 1000)
        utc_dt = datetime.fromtimestamp(ts_ms / 1000, tz=pytz.UTC)
        ts_est = utc_dt.astimezone(EST_TZ).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        total_fees = k_fee + p_fee

        if leg == "A":
            desc = f"Buy YES@Kalshi ({k.get('yes_ask')}) + NO@Poly ({p.get('no_ask')})"
        else:
            desc = f"Buy NO@Kalshi ({k.get('no_ask')}) + YES@Poly ({p.get('yes_ask')})"

        print(f"\n{'*' * 60}")
        print(f"*** ARBITRAGE DETECTED — Leg {leg} ***")
        print(f"*** {desc}")
        print(f"*** Cost: ${cost:.4f}  |  Fees: ${total_fees:.4f}  |  Net: ${net_profit:.4f}")
        print(f"*** {ts_est}")
        print(f"{'*' * 60}\n")

        row = [
            ts_ms, ts_est, leg,
            k.get("yes_ask", ""), k.get("no_ask", ""),
            p.get("yes_ask", ""), p.get("no_ask", ""),
            round(cost, 4), round(k_fee, 4), round(p_fee, 4),
            round(net_profit, 4),
        ]
        self._csv_writer.writerow(row)
        self._csv_file.flush()

    def close(self):
        self._csv_file.close()


# ==============================================================================
# RUNNER
# ==============================================================================

def _load_poly_config():
    """Load Polymarket asset IDs from market_config.json."""
    config_ids, label_map, ordered_labels = load_config()
    if config_ids is None:
        print("Error: No market_config.json found. Run lookup_market.py first.")
        raise SystemExit(1)
    return config_ids, label_map, ordered_labels


def main():
    poly_asset_ids, label_map, ordered_labels = _load_poly_config()

    detector = ArbitrageDetector(min_margin=MIN_MARGIN)

    # --- Kalshi client ---
    kalshi_client = KalshiWebSocket(
        tickers=[KALSHI_TICKER],
        key_id=KALSHI_KEY_ID,
        private_key_path=KALSHI_PRIVATE_KEY_PATH,
        print_interval=PRINT_INTERVAL,
        change_threshold=CHANGE_THRESHOLD,
        on_price_update=detector.update_kalshi,
    )

    # --- Polymarket client ---
    poly_client = PolymarketWebSocket(
        channel_type=MARKET_CHANNEL,
        url=POLY_WS_URL,
        data=poly_asset_ids,
        label_map=label_map,
        ordered_labels=ordered_labels,
        verbose=True,
        print_interval=PRINT_INTERVAL,
        change_threshold=CHANGE_THRESHOLD,
        on_price_update=detector.update_poly,
    )

    # --- Start both in threads ---
    def run_kalshi():
        asyncio.run(kalshi_client.run())

    kalshi_thread = threading.Thread(target=run_kalshi, daemon=True)
    poly_thread = threading.Thread(target=poly_client.run, daemon=True)

    print(f"Starting arbitrage scanner...")
    print(f"  Kalshi ticker:  {KALSHI_TICKER}")
    print(f"  Poly assets:    {len(poly_asset_ids)} outcome(s)")
    print(f"  Min net profit: ${MIN_MARGIN:.4f} (after taker fees)")
    print(f"  Stale threshold: {STALE_THRESHOLD}s")
    print(f"  Alerts log:     {detector.alerts_file}")
    print()

    kalshi_thread.start()
    poly_thread.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopped by user.")
    finally:
        kalshi_client.close()
        detector.close()


if __name__ == "__main__":
    main()
