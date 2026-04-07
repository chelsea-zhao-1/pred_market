# Crypto Prediction Market Arbitrage Scanner

Cross-platform soft arbitrage scanner for BTC 15-minute up/down prediction markets on Kalshi and Polymarket.

## Project Structure

```
dual_market_arbitrage_crypto/
├── live/
│   ├── arbitrage_scanner.py        # Main scanner — runs both WS clients, detects mispricings, triggers orders
│   ├── live_kalshi.py              # Kalshi WebSocket client (asyncio)
│   ├── live_poly.py                # Polymarket WebSocket client (threading)
│   ├── order_executor.py           # KalshiRestClient — places orders via REST (RSA-PSS auth)
│   ├── order_book.py               # Depth/profit calc helpers
│   ├── market_rotation.py          # Auto-rotates Kalshi ticker each 15-min window
│   ├── lookup_market.py            # Fetches Poly market config from Gamma API
│   ├── analyze_rotation_artifacts.py  # Post-hoc analysis of rotation timing artifacts
│   └── market_config.json          # Auto-generated Poly market config (token IDs)
├── live_market_data/
│   ├── kalshi_market_data.csv      # Kalshi price data (appended in real-time)
│   ├── poly_market_data.csv        # Polymarket price data (appended in real-time)
│   ├── arb_alerts_<timestamp>.csv  # Logged arbitrage opportunities (per session)
│   └── kalshi_orders_<timestamp>.csv  # Kalshi order log (per session)
├── kalshi-main-key.key             # Kalshi API private key (not committed)
├── requirements.txt
├── README.md                       # Usage guide
└── claude.md                       # This file
```

## Key Concepts

- **Kalshi** uses CF Benchmarks (60-second average) as its oracle. Ticker format: `KXBTC15M-{date}{time}-{minute}`.
- **Polymarket** uses Chainlink BTC/USD as its oracle. "Up" = BTC at end ≥ BTC at start.
- **Soft arbitrage**: Both markets ask "did BTC go up?" but use different oracles. When the combined ask prices across platforms sum to < $1, there's a positive expected-value opportunity (high probability, not guaranteed due to oracle divergence).
- Both platforms pay $1 per contract on the winning side.

## CSV Column Convention

Both data files use uniform column names:
- `yes_bid`, `yes_ask`, `no_bid`, `no_ask` — raw order book prices
- `yes_mid`, `no_mid` — midpoint when spread < $0.10, else last traded price

## Order Execution

- `order_executor.py` contains `KalshiRestClient` — verified working (auth + balance check).
- `EXECUTION_ENABLED = False` in `arbitrage_scanner.py`. Flip to `True` to trade live.
- Order body: `{ticker, action:"buy", side:"yes"/"no", count, yes_price or no_price (int 1–99), time_in_force:"immediate_or_cancel"}`.
- Auth signing: pre-hash string is `f"{timestamp_ms}{METHOD}{full_path}"` where `full_path` must include the `/trade-api/v2` prefix.

## Running the Scanner

```bash
cd live
python lookup_market.py <polymarket-slug>   # configure Poly market
# Edit KALSHI_TICKER in arbitrage_scanner.py for current window
python arbitrage_scanner.py
```

Market rotation is handled automatically by `market_rotation.py` — it parses the Kalshi ticker expiry and queues the next ticker 30 seconds before window close.

## Dependencies

Python 3.12+. Key packages: `websockets`, `websocket-client`, `cryptography`, `pytz`, `requests`.
