# Cross-Platform Soft Arbitrage Scanner — Full Implementation Plan

## Background & Design Decisions

### What we're building
A real-time scanner that monitors Kalshi and Polymarket BTC 15-minute up/down markets simultaneously and alerts when cross-platform mispricings create positive expected-value opportunities.

### The markets
- **Kalshi**: KXBTC15M series. Ticker format: `KXBTC15M-26FEB141445-45` where `26FEB14` = date, `1445` = expiry time (2:45 PM), `-45` = expiry minute (redundant with time). Settlement uses **CF Benchmarks** Bitcoin Real-Time Index, specifically a **60-second simple average** before expiry. Payout: $1 if condition met.
- **Polymarket**: BTC up/down 15-min markets. Slug format: `btc-updown-15m-{unix_ts}`. Resolution: "Up" if BTC at end ≥ BTC at start. Uses **Chainlink BTC/USD** data stream. Payout: $1 if condition met.

### Why "soft" arbitrage (not guaranteed)
Both markets ask essentially the same question ("did BTC go up?") but with different oracles:
- **CF Benchmarks** (Kalshi) vs **Chainlink** (Polymarket) — different price feeds
- Kalshi uses a 60-second average; Polymarket likely uses point-in-time spot
- In rare edge cases near the boundary, one oracle could say "up" while the other says "down"
- However, the correlation is extremely high. A 5+ cent mispricing has positive expected value.

### Scope: v1 = Alert Only
Detection + alerting to console + CSV log. No auto-execution. Fees ignored for now (add `min_margin` buffer later).

---

## Architecture

```
┌──────────────────┐          ┌─────────────────────────┐
│  KalshiWebSocket │──callback──>│                         │
│  (asyncio thread)│          │   ArbitrageDetector      │
└──────────────────┘          │                         │
                              │  On every tick:         │
┌──────────────────┐          │  1. Update prices       │
│  PolyWebSocket   │──callback──>│  2. Check both legs     │──> ALERT + CSV log
│  (own thread)    │          │  3. Alert if cost < $1  │
└──────────────────┘          └─────────────────────────┘
```

Single process, three threads:
1. **Main thread**: creates detector, starts client threads, waits for Ctrl+C
2. **Kalshi thread**: runs `asyncio.run(client.run())` — the Kalshi WS client uses asyncio
3. **Poly thread**: runs `client.run()` — the Poly WS client uses threading/websocket-client

Both clients call back into a shared `ArbitrageDetector` protected by `threading.Lock`.

---

## Core Arbitrage Logic

On every price update from either platform, check two legs:

```
Leg A: Buy YES @ Kalshi + Buy NO @ Polymarket
  cost_a = kalshi_yes_ask + poly_no_ask
  If cost_a < 1.00: profit = 1.00 - cost_a (one side MUST pay $1)

Leg B: Buy NO @ Kalshi + Buy YES @ Polymarket
  cost_b = kalshi_no_ask + poly_yes_ask
  If cost_b < 1.00: profit = 1.00 - cost_b (one side MUST pay $1)
```

Uses **ASK prices** (what you'd actually pay), not midpoints or bids.

---

## Implementation Steps

### Step 1: Add `on_price_update` callback to both WS clients

#### `live/live_kalshi.py`
- Add `on_price_update=None` parameter to `KalshiWebSocket.__init__()`
- Store as `self.on_price_update`
- At the END of `_log_ticker()` (after CSV write), call:
  ```python
  if self.on_price_update:
      self.on_price_update(prices)
  ```
- The `prices` dict already contains: `yes_bid, yes_ask, no_bid, no_ask, yes_last, no_last`

#### `live/live_poly.py`
- Add `on_price_update=None` parameter to `PolymarketWebSocket.__init__()`
- Store as `self.on_price_update`
- At the END of `_handle_event()` in the `if any_changed:` block (after CSV write), build and send a unified prices dict:
  ```python
  if self.on_price_update:
      # self.data[0] = yes/Up asset, self.data[1] = no/Down asset
      yes_data = self.latest_data.get(self.data[0], {})
      no_data = self.latest_data.get(self.data[1], {})
      self.on_price_update({
          "yes_bid": yes_data.get("bid"),
          "yes_ask": yes_data.get("ask"),
          "no_bid": no_data.get("bid"),
          "no_ask": no_data.get("ask"),
      })
  ```

### Step 2: Create `live/arbitrage_scanner.py`

#### ArbitrageDetector class
```python
class ArbitrageDetector:
    def __init__(self, min_margin=0.0):
        self.min_margin = min_margin
        self.kalshi_prices = None   # dict with yes_ask, no_ask, etc.
        self.poly_prices = None     # dict with yes_ask, no_ask, etc.
        self.lock = threading.Lock()
        # CSV setup for logging alerts
        self._setup_csv()

    def update_kalshi(self, prices):
        with self.lock:
            self.kalshi_prices = prices
            self._check()

    def update_poly(self, prices):
        with self.lock:
            self.poly_prices = prices
            self._check()

    def _check(self):
        if self.kalshi_prices is None or self.poly_prices is None:
            return
        k = self.kalshi_prices
        p = self.poly_prices

        # Leg A: Kalshi YES + Poly NO
        k_yes_ask = k.get("yes_ask")
        p_no_ask = p.get("no_ask")
        if k_yes_ask is not None and p_no_ask is not None:
            cost_a = k_yes_ask + p_no_ask
            if cost_a < 1.0 - self.min_margin:
                self._alert("A", cost_a, k, p)

        # Leg B: Kalshi NO + Poly YES
        k_no_ask = k.get("no_ask")
        p_yes_ask = p.get("yes_ask")
        if k_no_ask is not None and p_yes_ask is not None:
            cost_b = k_no_ask + p_yes_ask
            if cost_b < 1.0 - self.min_margin:
                self._alert("B", cost_b, k, p)

    def _alert(self, leg, cost, k, p):
        profit = round(1.0 - cost, 4)
        # Loud console output
        # CSV log row
```

#### Runner (main block)
```python
if __name__ == "__main__":
    detector = ArbitrageDetector(min_margin=0.0)

    # Kalshi client — runs in a thread with its own asyncio loop
    kalshi_client = KalshiWebSocket(
        tickers=[KALSHI_TICKER],
        key_id=KALSHI_KEY_ID,
        private_key_path=KALSHI_PRIVATE_KEY_PATH,
        on_price_update=detector.update_kalshi,
    )

    # Poly client — runs in its own thread
    poly_ws = PolymarketWebSocket(
        channel_type="market",
        url="wss://ws-subscriptions-clob.polymarket.com",
        data=POLY_ASSET_IDS,  # from market_config.json
        label_map=LABEL_MAP,
        ordered_labels=ORDERED_LABELS,
        on_price_update=detector.update_poly,
    )

    # Start threads
    kalshi_thread = threading.Thread(
        target=lambda: asyncio.run(kalshi_client.run()),
        daemon=True
    )
    poly_thread = threading.Thread(target=poly_ws.run, daemon=True)

    kalshi_thread.start()
    poly_thread.start()

    # Main thread waits
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        kalshi_client.close()
```

### Step 3: Arbitrage alerts CSV

File: `live_market_data/arbitrage_alerts.csv`
Columns:
```
timestamp_ms, timestamp_est, leg, kalshi_yes_ask, kalshi_no_ask, poly_yes_ask, poly_no_ask, cost, profit
```

---

## Files Summary

| Action | File | What to change |
|--------|------|---------------|
| Modify | `live/live_kalshi.py` | Add `on_price_update` param to `__init__`, call from `_log_ticker()` |
| Modify | `live/live_poly.py` | Add `on_price_update` param to `__init__`, call from `_handle_event()` |
| Create | `live/arbitrage_scanner.py` | ArbitrageDetector class + runner wiring both clients |

---

## Configuration (set at top of arbitrage_scanner.py)

```python
KALSHI_TICKER = "KXBTC15M-26FEB141445-45"  # Update per 15-min window
# Poly config auto-loaded from live/market_config.json (run lookup_market.py first)
MIN_MARGIN = 0.0  # Increase to filter noise (e.g., 0.02 = only alert if 2+ cents profit)
```

---

## How to run

1. Set up the Polymarket market config:
   ```bash
   cd live
   python lookup_market.py <slug>   # e.g., btc-updown-15m-1771097400
   ```
2. Update `KALSHI_TICKER` in `arbitrage_scanner.py` for the current 15-min window
3. Run:
   ```bash
   python live/arbitrage_scanner.py
   ```
4. Watch for alerts. Both clients also still write their own CSVs (`kalshi_market_data.csv`, `poly_market_data.csv`)

---

## Verification checklist

1. Both WS clients connect and stream prices (confirm console output from each)
2. Detector cross-checks on every tick (add debug print showing both sides' latest asks)
3. Alerts fire correctly — test by temporarily setting `min_margin = -0.10` to force triggers
4. CSV logging works for alerts
5. Monitor a full live 15-min window for real opportunities

---

## Future enhancements (not in v1)

- **Fee integration**: Subtract platform fees from profit calculation before alerting
- **Auto-execution**: Place orders on both platforms when opportunity detected
- **Market rotation**: Auto-detect and switch to next 15-min market as current one expires
- **Backtesting**: Use historical CSV data to analyze how often opportunities appeared and what the resolution divergence rate is between oracles
- **Liquidity check**: Verify order book depth before alerting (is there enough size at the ask?)
- **Staleness guard**: Don't alert if one platform's data is >5 seconds old
