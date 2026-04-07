# Crypto Prediction Market Arbitrage Scanner

Cross-platform soft arbitrage scanner for BTC 15-minute up/down prediction markets on Kalshi and Polymarket.

## Project Structure

```
dual_market_arbitrage_crypto/
├── live/
│   ├── arbitrage_scanner.py    # Main scanner — runs both WS clients, detects mispricings
│   ├── live_kalshi.py          # Kalshi WebSocket client (asyncio)
│   ├── live_poly.py            # Polymarket WebSocket client (threading)
│   ├── lookup_market.py        # Fetches Poly market config from Gamma API
│   ├── market_config.json      # Auto-generated Poly market config (token IDs)
│   └── ARBITRAGE_PLAN.md       # Full design document for the arbitrage scanner
├── live_market_data/
│   ├── kalshi_market_data.csv  # Kalshi price data (appended in real-time)
│   ├── poly_market_data.csv    # Polymarket price data (appended in real-time)
│   └── arbitrage_alerts.csv    # Logged arbitrage opportunities
├── kalshi-main-key.key         # Kalshi API private key (not committed)
├── requirements.txt
├── README.md                   # Usage guide
└── claude.md                   # This file
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

## Running the Scanner

```bash
cd live
python lookup_market.py <polymarket-slug>   # configure Poly market
# Edit KALSHI_TICKER in arbitrage_scanner.py for current window
python arbitrage_scanner.py
```

## Dependencies

Python 3.12+. Key packages: `websockets`, `websocket-client`, `cryptography`, `pytz`, `requests`.
