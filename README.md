# Crypto Prediction Market Arbitrage Scanner

Real-time cross-platform arbitrage detection for BTC 15-minute up/down markets on Kalshi and Polymarket.

## Prerequisites

```bash
source .venv/bin/activate
pip install -r requirements.txt
```

You also need a Kalshi API key (RSA private key file) at `kalshi-main-key.key`.

## Quick Start — Arbitrage Scanner

1. **Configure the Polymarket market:**
   ```bash
   cd live
   python lookup_market.py <slug>
   ```
   Find the slug from the Polymarket URL (e.g., `btc-updown-15m-1771101900`).

2. **Set the Kalshi ticker** in `live/arbitrage_scanner.py`:
   ```python
   KALSHI_TICKER = "KXBTC15M-26FEB141600-00"
   ```

3. **Run:**
   ```bash
   python arbitrage_scanner.py
   ```

   Both WebSocket clients connect simultaneously. The scanner alerts when buying YES on one platform + NO on the other costs < $1.

## Running Individual Collectors

You can also run each collector standalone:

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

## Output Files

| File | Contents |
|------|----------|
| `live_market_data/poly_market_data.csv` | Polymarket bid/ask for each outcome |
| `live_market_data/kalshi_market_data.csv` | Kalshi yes/no bid/ask |
| `live_market_data/arbitrage_alerts.csv` | Detected arbitrage opportunities |

**CSV columns** (uniform across both data files):
```
timestamp_ms, timestamp_est, yes_bid, yes_ask, no_bid, no_ask, yes_mid, no_mid
```
Kalshi also includes `market_ticker`.

## Configuration

Both scripts log when prices change by >= $0.01 or every 15 seconds. Edit `PRINT_INTERVAL` and `CHANGE_THRESHOLD` at the top of each script to adjust.

The Polymarket config (`market_config.json`) persists between runs. Re-run `lookup_market.py` with a new slug when switching markets.
