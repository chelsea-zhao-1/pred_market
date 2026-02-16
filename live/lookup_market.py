"""
Polymarket Market Lookup

Fetches market info from the Gamma API given a market slug.
Prints a summary and saves a JSON config file for use by live_poly.py.

Usage:
    python3 lookup_market.py bitcoin-up-or-down-february-2-5pm-et

"""

import requests
import json
import sys
import os
from datetime import datetime, timezone

GAMMA_API_URL = "https://gamma-api.polymarket.com/markets"
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "market_config.json")


def fetch_market(slug):
    """Fetch market data from Gamma API for a given slug."""
    try:
        resp = requests.get(GAMMA_API_URL, params={"slug": slug}, timeout=10)
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        print(f"Error: API returned status {resp.status_code}")
        sys.exit(1)
    except requests.exceptions.RequestException as e:
        print(f"Error: Network request failed: {e}")
        sys.exit(1)

    data = resp.json()
    if not data:
        print(f"Error: No market found for slug '{slug}'")
        sys.exit(1)

    return data[0]


def parse_market(market):
    """Extract relevant fields into a config dict.

    Note: clobTokenIds, outcomes, and outcomePrices are JSON-encoded
    strings inside the JSON response, not native arrays.
    """
    token_ids = json.loads(market.get("clobTokenIds", "[]"))
    outcomes = json.loads(market.get("outcomes", "[]"))
    prices = json.loads(market.get("outcomePrices", "[]"))

    outcome_list = []
    for i, label in enumerate(outcomes):
        outcome_list.append({
            "label": label,
            "token_id": token_ids[i] if i < len(token_ids) else None,
            "price": prices[i] if i < len(prices) else None,
        })

    return {
        "slug": market.get("slug", ""),
        "question": market.get("question", ""),
        "description": market.get("description", ""),
        "active": market.get("active", False),
        "closed": market.get("closed", False),
        "accepting_orders": market.get("acceptingOrders", False),
        "outcomes": outcome_list,
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def print_summary(config):
    """Print a human-readable summary to stdout."""
    print(f"\nMarket: {config['question']}")

    desc = config.get("description", "")
    if desc:
        truncated = (desc[:200] + "...") if len(desc) > 200 else desc
        print(f"Description: {truncated}")

    status_parts = []
    if config.get("active"):
        status_parts.append("Active")
    if config.get("closed"):
        status_parts.append("Closed")
    if config.get("accepting_orders"):
        status_parts.append("Accepting Orders")
    print(f"Status: {', '.join(status_parts) or 'Unknown'}")

    print(f"\nOutcomes:")
    for o in config["outcomes"]:
        tid = o["token_id"] or "N/A"
        # Show truncated token ID for readability, full ID in config file
        tid_display = tid[:12] + "..." + tid[-8:] if len(tid) > 24 else tid
        price = o["price"] if o["price"] is not None else "N/A"
        print(f"  {o['label']:<6} price: {price:<8} token_id: {tid_display}")


def save_config(config, path):
    """Write config dict to JSON file."""
    with open(path, "w") as f:
        json.dump(config, f, indent=2)
    print(f"\nConfig saved to {path}")


def main():
    if len(sys.argv) != 2:
        print("Usage: python lookup_market.py <slug>")
        print("Example: python lookup_market.py will-donald-trump-win-the-2024-us-presidential-election")
        sys.exit(1)

    slug = sys.argv[1]
    market = fetch_market(slug)
    config = parse_market(market)
    print_summary(config)
    save_config(config, CONFIG_PATH)


if __name__ == "__main__":
    main()
