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
"""

import asyncio
import csv
import math
import os
import queue
import threading
import time
from datetime import datetime

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

import pytz

from live_kalshi import KalshiWebSocket
from live_poly import PolymarketWebSocket, load_config, MARKET_CHANNEL
from market_rotation import (
    parse_kalshi_ticker, kalshi_ticker_for, poly_slug_for,
    next_window_times, fetch_poly_config,
)

# ==============================================================================
# CONFIGURATION
# ==============================================================================

KALSHI_TICKER = "KXBTC15M-26MAY151300-00"  # Update per 15-min window

KALSHI_KEY_ID = os.environ["KALSHI_KEY_ID"]
KALSHI_PRIVATE_KEY_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "kalshi-main-key.key"
)


POLY_WS_URL = "wss://ws-subscriptions-clob.polymarket.com"

# Min per-contract profit after slippage + fees to trigger an alert and execute.
MIN_PROFIT_DOLLARS = 0.05

# ── Execution settings ────────────────────────────────────────────────────────
# Set EXECUTION_ENABLED = True only when ready to trade real money.
# Requires: live/poly_credentials.json with {"private_key": "0x..."}
#           pip install simplefix py-clob-client
EXECUTION_ENABLED = False

# Hard cap on contracts per detected opportunity while validating.
MAX_CONTRACTS_CAP = 5

# Don't alert if either platform's data is older than this (seconds).
STALE_THRESHOLD = 5.0

# Seconds before window expiry to pre-fetch the next Poly market config (API call only).
PREFETCH_LEAD_TIME = 30
# Seconds before window expiry to apply the rotation to both platforms.
APPLY_LEAD_TIME = 5

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
    "max_contracts", "total_profit",
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
    """Polymarket taker fee per share: feeRate * p * (1 - p), feeRate = 0.07 for takers."""
    if price <= 0 or price >= 1:
        return 0.0
    return round(0.07 * price * (1 - price), 4)


# ==============================================================================
# DEPTH-AWARE FILL CALCULATION
# ==============================================================================

def _walk_depth(kalshi_asks, poly_asks):
    """Walk two sorted-ascending ask ladders to find max fillable contracts.

    At each price-level combination, computes all-in cost (price + fees).
    Fills min(available_kalshi, available_poly) contracts per level pair.
    Stops when marginal cost >= $1.00.

    Returns (total_contracts, total_profit).
    """
    if not kalshi_asks or not poly_asks:
        return (0, 0.0)

    ki, pi = 0, 0
    k_remaining = kalshi_asks[0][1]
    p_remaining = poly_asks[0][1]
    total = 0
    total_profit = 0.0

    while ki < len(kalshi_asks) and pi < len(poly_asks):
        k_price = kalshi_asks[ki][0]
        p_price = poly_asks[pi][0]

        cost = k_price + p_price + kalshi_taker_fee(k_price) + poly_taker_fee(p_price)
        if cost >= 1.0:
            break

        fillable = min(k_remaining, p_remaining)
        total += fillable
        total_profit += (1.0 - cost) * fillable
        k_remaining -= fillable
        p_remaining -= fillable

        if k_remaining == 0:
            ki += 1
            if ki < len(kalshi_asks):
                k_remaining = kalshi_asks[ki][1]
        if p_remaining == 0:
            pi += 1
            if pi < len(poly_asks):
                p_remaining = poly_asks[pi][1]

    return (total, round(total_profit, 4))


# ==============================================================================
# ARBITRAGE DETECTOR
# ==============================================================================

class _CsvWriter:
    """Background CSV writer — enqueues rows so flushes don't block WS threads."""

    def __init__(self, file, writer):
        self._file = file
        self._writer = writer
        self._q: queue.Queue = queue.Queue()
        threading.Thread(target=self._run, daemon=True, name="csv-writer-alerts").start()

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


class ArbitrageDetector:
    """Thread-safe cross-platform arbitrage detector.

    Receives price updates from both Kalshi and Polymarket WS clients
    and checks whether buying YES on one + NO on the other costs < $1.

    Lock is held only for the price snapshot — depth walk and alert run
    outside the lock so neither WS thread blocks waiting on the other.
    """

    # Suppress duplicate alerts for the same leg within this window (seconds).
    _ALERT_DEDUP_SECS = 1.0

    def __init__(self):
        self.kalshi_prices = None
        self.poly_prices = None
        self.kalshi_updated_at = 0.0
        self.poly_updated_at = 0.0
        self.kalshi_client = None  # set after construction for depth access
        self.poly_client = None    # set after construction for depth access
        self._executor = None      # set via set_executor() if EXECUTION_ENABLED
        self.lock = threading.Lock()
        self._alert_lock = threading.Lock()
        self._last_alert: dict = {}  # leg -> last alert timestamp

        os.makedirs(DATA_DIR, exist_ok=True)
        session_ts = datetime.now(EST_TZ).strftime("%Y%m%d_%H%M%S")
        self.alerts_file = os.path.join(DATA_DIR, f"arb_alerts_{session_ts}.csv")
        _f = open(self.alerts_file, "w", newline="")
        _w = csv.writer(_f)
        _w.writerow(ALERTS_HEADER)
        _f.flush()
        self._csv = _CsvWriter(_f, _w)

    def update_kalshi(self, prices):
        with self.lock:
            self.kalshi_prices = prices
            self.kalshi_updated_at = time.time()
            k, p = self.kalshi_prices, self.poly_prices
            k_age, p_age = self.kalshi_updated_at, self.poly_updated_at
        # Lock released — depth walk runs without blocking the Poly WS thread.
        self._check(k, p, k_age, p_age)

    def update_poly(self, prices):
        with self.lock:
            self.poly_prices = prices
            self.poly_updated_at = time.time()
            k, p = self.kalshi_prices, self.poly_prices
            k_age, p_age = self.kalshi_updated_at, self.poly_updated_at
        # Lock released — depth walk runs without blocking the Kalshi asyncio loop.
        self._check(k, p, k_age, p_age)

    def _check(self, k, p, k_age, p_age):
        if k is None or p is None:
            return

        now = time.time()
        if (now - k_age) > STALE_THRESHOLD:
            return
        if (now - p_age) > STALE_THRESHOLD:
            return

        # Leg A: Buy YES @ Kalshi + Buy NO @ Polymarket
        k_yes_ask = k.get("yes_ask")
        p_no_ask = p.get("no_ask")
        if k_yes_ask is not None and p_no_ask is not None:
            mc, tp = self._compute_fill_depth("A")
            if mc > 0 and (tp / mc) >= MIN_PROFIT_DOLLARS:
                k_fee = kalshi_taker_fee(k_yes_ask)
                p_fee = poly_taker_fee(p_no_ask)
                cost_a = k_yes_ask + p_no_ask
                net = 1.0 - cost_a - k_fee - p_fee
                self._alert("A", cost_a, k_fee, p_fee, net, k, p, mc, tp)

        # Leg B: Buy NO @ Kalshi + Buy YES @ Polymarket
        k_no_ask = k.get("no_ask")
        p_yes_ask = p.get("yes_ask")
        if k_no_ask is not None and p_yes_ask is not None:
            mc, tp = self._compute_fill_depth("B")
            if mc > 0 and (tp / mc) >= MIN_PROFIT_DOLLARS:
                k_fee = kalshi_taker_fee(k_no_ask)
                p_fee = poly_taker_fee(p_yes_ask)
                cost_b = k_no_ask + p_yes_ask
                net = 1.0 - cost_b - k_fee - p_fee
                self._alert("B", cost_b, k_fee, p_fee, net, k, p, mc, tp)

    def _compute_fill_depth(self, leg):
        """Max contracts fillable at a profit, walking both ask ladders."""
        if self.kalshi_client is None or self.poly_client is None:
            return (0, 0.0)
        if leg == "A":
            k_asks = self.kalshi_client.get_yes_asks()
            p_asks = self.poly_client.get_no_asks()
        else:
            k_asks = self.kalshi_client.get_no_asks()
            p_asks = self.poly_client.get_yes_asks()
        return _walk_depth(k_asks, p_asks)

    def _alert(self, leg, cost, k_fee, p_fee, net_profit, k, p, max_contracts=0, total_profit=0.0):
        # Dedup: when both platforms tick within ~100ms of each other, _check()
        # runs twice with identical prices. Skip the second fire per leg.
        with self._alert_lock:
            now = time.time()
            if now - self._last_alert.get(leg, 0.0) < self._ALERT_DEDUP_SECS:
                return
            self._last_alert[leg] = now

        ts_ms = int(now * 1000)
        utc_dt = datetime.fromtimestamp(now, tz=pytz.UTC)
        ts_est = utc_dt.astimezone(EST_TZ).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        total_fees = k_fee + p_fee

        if leg == "A":
            desc = f"Buy YES@Kalshi ({k.get('yes_ask')}) + NO@Poly ({p.get('no_ask')})"
        else:
            desc = f"Buy NO@Kalshi ({k.get('no_ask')}) + YES@Poly ({p.get('yes_ask')})"

        depth_net = round(total_profit / max_contracts, 4) if max_contracts else 0.0
        print(f"\n{'*' * 60}")
        print(f"*** ARBITRAGE DETECTED — Leg {leg} ***")
        print(f"*** {desc}")
        print(f"*** Top-of-book net: ${net_profit:.4f}  |  Depth-adj net/contract: ${depth_net:.4f}")
        print(f"*** Max fillable: {max_contracts} contracts  |  Total profit: ${total_profit:.4f}")
        print(f"*** {ts_est}")
        print(f"{'*' * 60}\n")

        row = [
            ts_ms, ts_est, leg,
            k.get("yes_ask", ""), k.get("no_ask", ""),
            p.get("yes_ask", ""), p.get("no_ask", ""),
            round(cost, 4), round(k_fee, 4), round(p_fee, 4),
            round(net_profit, 4), max_contracts, round(total_profit, 4),
        ]
        self._csv.writerow(row)

        if self._executor is not None:
            from order_executor import ArbitrageOpportunity
            contracts = min(max_contracts, MAX_CONTRACTS_CAP)
            if leg == "A":
                k_side, k_price = "yes", k["yes_ask"]
                poly_token = self.poly_client.data[1]  # data[1] = NO (Down) token
                p_price = p["no_ask"]
            else:
                k_side, k_price = "no", k["no_ask"]
                poly_token = self.poly_client.data[0]  # data[0] = YES (Up) token
                p_price = p["yes_ask"]

            opp = ArbitrageOpportunity(
                leg=leg,
                kalshi_ticker=self.kalshi_client.tickers[0],
                kalshi_side=k_side,
                kalshi_price=k_price,
                poly_token_id=poly_token,
                poly_price=p_price,
                contracts=contracts,
                expected_profit=round(total_profit * contracts / max_contracts, 4)
                    if max_contracts > 0 else 0.0,
            )
            self._executor.execute(opp)

    def set_executor(self, executor):
        """Wire in the OrderExecutor. Call once from main() before starting threads."""
        self._executor = executor

    def rotate(self):
        """Clear stale prices between market windows."""
        with self.lock:
            self.kalshi_prices = None
            self.poly_prices = None
            self.kalshi_updated_at = 0.0
            self.poly_updated_at = 0.0

    def close(self):
        self._csv.close()


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


def _rotate_market(kalshi_client, poly_client, detector, current_expiry, prefetched_poly=None):
    """Rotate both platforms to the next 15-minute window.

    prefetched_poly: (asset_ids, label_map, ordered_labels) cached from the
    pre-fetch phase. If None, falls back to a live Gamma API call.

    Returns (new_expiry, new_poly_asset_ids) on success, or
    (current_expiry, None) if the Poly market isn't available yet.
    """
    next_start, next_end = next_window_times(current_expiry)
    next_ticker = kalshi_ticker_for(next_end)
    next_slug = poly_slug_for(next_start)

    print(f"\n{'=' * 60}")
    print(f"ROTATING to next window: {next_start.strftime('%H:%M')}–{next_end.strftime('%H:%M')} ET")
    print(f"  Kalshi ticker: {next_ticker}")
    print(f"  Poly slug:     {next_slug}")

    if prefetched_poly is not None:
        new_asset_ids, new_label_map, new_ordered_labels = prefetched_poly
    else:
        poly_result = fetch_poly_config(next_slug)
        if poly_result is None:
            print(f"  WARNING: Poly market not found yet. Will retry next cycle.")
            print(f"{'=' * 60}\n")
            return current_expiry, None
        new_asset_ids, new_label_map, new_ordered_labels = poly_result

    # Rotate Polymarket (synchronous — already has unsub/sub methods)
    old_poly_ids = poly_client.data
    poly_client.unsubscribe_from_assets(old_poly_ids)
    poly_client.subscribe_to_assets(new_asset_ids)
    poly_client.data = new_asset_ids
    poly_client.label_map = new_label_map
    poly_client.ordered_labels = new_ordered_labels

    # Rotate Kalshi (async — schedule on its event loop)
    if kalshi_client._loop is not None:
        future = asyncio.run_coroutine_threadsafe(
            kalshi_client.update_tickers([next_ticker]),
            kalshi_client._loop,
        )
        try:
            future.result(timeout=5)
        except Exception as e:
            print(f"  WARNING: Kalshi ticker update failed: {e}")

    # Reset detector to prevent cross-window comparisons
    detector.rotate()

    print(f"  Rotation complete.")
    print(f"{'=' * 60}\n")
    return next_end, new_asset_ids


def main():
    poly_asset_ids, label_map, ordered_labels = _load_poly_config()

    detector = ArbitrageDetector()

    # --- Executor setup (only when EXECUTION_ENABLED = True) ---
    kalshi_rest_client = None
    executor = None
    if EXECUTION_ENABLED:
        import json
        from order_executor import KalshiRestClient, PolyOrderClient, OrderExecutor

        creds_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "poly_credentials.json")
        with open(creds_path) as f:
            creds = json.load(f)

        kalshi_rest_client = KalshiRestClient(KALSHI_KEY_ID, KALSHI_PRIVATE_KEY_PATH)
        kalshi_rest_client.connect()

        poly_order_client = PolyOrderClient(creds["private_key"])
        executor = OrderExecutor(kalshi_rest_client, poly_order_client)
        detector.set_executor(executor)
        print(f"[Scanner] Execution ENABLED — max {MAX_CONTRACTS_CAP} contracts/opportunity")

    # Parse initial ticker to know when the first window expires.
    current_expiry = parse_kalshi_ticker(KALSHI_TICKER)

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

    # Wire client references for depth-aware fill calculation
    detector.kalshi_client = kalshi_client
    detector.poly_client = poly_client

    # --- Start both in threads ---
    def run_kalshi():
        asyncio.run(kalshi_client.run())

    kalshi_thread = threading.Thread(target=run_kalshi, daemon=True)
    poly_thread = threading.Thread(target=poly_client.run, daemon=True)

    print(f"Starting arbitrage scanner (with auto-rotation)...")
    print(f"  Kalshi ticker:  {KALSHI_TICKER}")
    print(f"  Window expires: {current_expiry.strftime('%Y-%m-%d %H:%M ET')}")
    print(f"  Poly assets:    {len(poly_asset_ids)} outcome(s)")
    print(f"  Min profit/contract: ${MIN_PROFIT_DOLLARS:.4f} (depth-adj, after slippage + fees)")
    print(f"  Stale threshold: {STALE_THRESHOLD}s")
    print(f"  Alerts log:     {detector.alerts_file}")
    print()

    kalshi_thread.start()
    poly_thread.start()

    next_poly_config = None  # pre-fetched (asset_ids, label_map, ordered_labels)

    try:
        while True:
            now_et = datetime.now(EST_TZ)
            secs_until_expiry = (current_expiry - now_et).total_seconds()

            # Phase 1: Pre-fetch next Poly config (API call only, no subscription change)
            if secs_until_expiry <= PREFETCH_LEAD_TIME and next_poly_config is None:
                next_start, next_end = next_window_times(current_expiry)
                next_slug = poly_slug_for(next_start)
                result = fetch_poly_config(next_slug)
                if result is not None:
                    next_poly_config = result
                    print(f"  Pre-fetched Poly config for "
                          f"{next_start.strftime('%H:%M')}–{next_end.strftime('%H:%M')}.")
                else:
                    print(f"  WARNING: Next Poly market not available yet — will retry.")

            # Phase 2: Apply rotation to both platforms right before expiry
            if secs_until_expiry <= APPLY_LEAD_TIME:
                new_expiry, new_ids = _rotate_market(
                    kalshi_client, poly_client, detector, current_expiry,
                    prefetched_poly=next_poly_config,
                )
                if new_ids is not None:
                    current_expiry = new_expiry
                    next_poly_config = None
                else:
                    time.sleep(2)
                    continue

            # Sleep in 1s increments so KeyboardInterrupt is responsive
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopped by user.")
    finally:
        kalshi_client.close()
        poly_client.close()
        detector.close()
        if executor:
            executor.close()
        if kalshi_rest_client:
            kalshi_rest_client.disconnect()


if __name__ == "__main__":
    main()
