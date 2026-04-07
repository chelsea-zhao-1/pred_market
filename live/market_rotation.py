"""
Market Rotation — 15-minute window math and ticker generation.

Computes Kalshi tickers and Polymarket slugs for BTC 15-minute
up/down markets based on wall-clock time (ET).
"""

from datetime import datetime, timedelta

import pytz

from lookup_market import fetch_market, parse_market

EST_TZ = pytz.timezone("US/Eastern")

MONTHS = [
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
    "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
]


def current_window_times(now_et=None):
    """Return (start, end) datetimes for the active 15-min window.

    Windows are aligned to :00, :15, :30, :45 boundaries in ET.
    """
    if now_et is None:
        now_et = datetime.now(EST_TZ)
    minute = now_et.minute
    window_start_minute = (minute // 15) * 15
    start = now_et.replace(minute=window_start_minute, second=0, microsecond=0)
    end = start + timedelta(minutes=15)
    return start, end


def next_window_times(current_end):
    """Given the current window's end time, return (start, end) for the next window."""
    return current_end, current_end + timedelta(minutes=15)


def kalshi_ticker_for(expiry_dt):
    """Generate a Kalshi ticker string from an expiry datetime (ET).

    Format: KXBTC15M-{YY}{MON}{DD}{HHMM}-{MM}
    Example: KXBTC15M-26FEB151230-30
    """
    yy = expiry_dt.strftime("%y")
    mon = MONTHS[expiry_dt.month - 1]
    dd = expiry_dt.strftime("%d")
    hhmm = expiry_dt.strftime("%H%M")
    mm = expiry_dt.strftime("%M")
    return f"KXBTC15M-{yy}{mon}{dd}{hhmm}-{mm}"


def parse_kalshi_ticker(ticker):
    """Parse a Kalshi ticker to extract the expiry datetime (ET).

    Returns a timezone-aware datetime in US/Eastern.
    """
    # KXBTC15M-26FEB151230-30
    parts = ticker.split("-")
    # parts = ["KXBTC15M", "26FEB151230", "30"]
    date_time_str = parts[1]  # "26FEB151230"

    yy = int(date_time_str[:2])
    mon_str = date_time_str[2:5]
    dd = int(date_time_str[5:7])
    hhmm = date_time_str[7:]

    month = MONTHS.index(mon_str) + 1
    year = 2000 + yy
    hour = int(hhmm[:2])
    minute = int(hhmm[2:])

    return EST_TZ.localize(datetime(year, month, dd, hour, minute))


def poly_slug_for(start_dt):
    """Generate a Polymarket slug from the window start datetime.

    Format: btc-updown-15m-{unix_timestamp}
    """
    unix_ts = int(start_dt.timestamp())
    return f"btc-updown-15m-{unix_ts}"


def fetch_poly_config(slug):
    """Fetch Polymarket market config for a slug.

    Returns (asset_ids, label_map, ordered_labels) or None if not found.
    """
    try:
        market = fetch_market(slug)
        config = parse_market(market)
    except (SystemExit, Exception) as e:
        print(f"  Could not fetch Poly market '{slug}': {e}")
        return None

    outcomes = config.get("outcomes", [])
    if not outcomes:
        return None

    asset_ids = []
    label_map = {}
    ordered_labels = []
    for o in outcomes:
        tid = o["token_id"]
        label = o["label"]
        asset_ids.append(tid)
        label_map[tid] = label
        ordered_labels.append(label)

    return asset_ids, label_map, ordered_labels
