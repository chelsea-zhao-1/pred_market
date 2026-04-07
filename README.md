# Crypto Prediction Market Arbitrage Scanner

Real-time cross-platform arbitrage detection and execution for BTC 15-minute up/down markets on Kalshi and Polymarket.

## Prerequisites

```bash
source .venv/bin/activate
pip install -r requirements.txt
```

You also need a Kalshi API key (RSA private key file) at `kalshi-main-key.key` and a `.env` with your Kalshi key ID.

## Quick Start — Arbitrage Scanner

1. **Configure the Polymarket market:**
   ```bash
   cd live
   python lookup_market.py <slug>
   ```
   Find the slug from the Polymarket URL (e.g., `btc-updown-15m-1771101900`).

2. **Set the Kalshi ticker** in `live/arbitrage_scanner.py`:
   ```python
   KALSHI_TICKER = "KXBTC15M-26APR071200-00"
   ```

3. **Run:**
   ```bash
   python arbitrage_scanner.py
   ```

   Both WebSocket clients connect simultaneously. The scanner alerts when buying YES on one platform + NO on the other costs < $1. Market rotation to the next 15-min window is handled automatically.

## Order Execution

Order execution is disabled by default. To enable live trading, set `EXECUTION_ENABLED = True` in `arbitrage_scanner.py`. The `KalshiRestClient` in `order_executor.py` is verified working (auth + balance check pass).

## Running Individual Collectors

**Polymarket:**
```bash
cd live
python live_poly.py
```

**Kalshi:**
```bash
cd live
python live_kalshi.py
```

## Files

| File | Purpose |
|------|---------|
| `live/arbitrage_scanner.py` | Main entry point — connects both feeds, detects mispricings, triggers orders |
| `live/order_executor.py` | `KalshiRestClient` — REST order placement with RSA-PSS auth |
| `live/order_book.py` | Depth and profit calculation helpers |
| `live/market_rotation.py` | Auto-rotates Kalshi ticker at each 15-min window boundary |
| `live/live_kalshi.py` | Kalshi WebSocket client (asyncio) |
| `live/live_poly.py` | Polymarket WebSocket client (threading) |
| `live/lookup_market.py` | Fetches Poly market config from Gamma API |

## Output Files

| File | Contents |
|------|----------|
| `live_market_data/poly_market_data.csv` | Polymarket bid/ask per tick |
| `live_market_data/kalshi_market_data.csv` | Kalshi yes/no bid/ask per tick |
| `live_market_data/arb_alerts_<ts>.csv` | Detected arbitrage opportunities (per session) |
| `live_market_data/kalshi_orders_<ts>.csv` | Kalshi order log (per session) |

**CSV columns** (uniform across price data files):
```
timestamp_ms, timestamp_est, yes_bid, yes_ask, no_bid, no_ask, yes_mid, no_mid
```
Kalshi also includes `market_ticker`.

## Configuration

Both feed scripts log when prices change by >= $0.01 or every 15 seconds. Edit `PRINT_INTERVAL` and `CHANGE_THRESHOLD` at the top of each script to adjust.

The Polymarket config (`market_config.json`) persists between runs. Re-run `lookup_market.py` with a new slug when switching markets.
