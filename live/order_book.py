"""Thread-safe order book for a single side of a market.

Used by both Kalshi and Polymarket WS clients to maintain local depth.
"""

import threading


class OrderBook:
    """Price-level book: maps price (cents) to quantity.

    Thread-safe for concurrent writes (from WS thread) and reads
    (from whichever thread triggers the arbitrage check).
    """

    def __init__(self):
        self._levels = {}  # price_cents -> quantity
        self._lock = threading.Lock()

    def snapshot(self, levels):
        """Full replace. levels: list of (price_cents, qty)."""
        with self._lock:
            self._levels = {p: q for p, q in levels if q > 0}

    def update(self, price_cents, delta):
        """Incremental delta (signed int) at a single price level."""
        with self._lock:
            new_qty = self._levels.get(price_cents, 0) + delta
            if new_qty <= 0:
                self._levels.pop(price_cents, None)
            else:
                self._levels[price_cents] = new_qty

    def clear(self):
        """Reset on market rotation."""
        with self._lock:
            self._levels.clear()

    def get_levels_ascending(self):
        """Return [(price_dollars, qty), ...] sorted cheapest first."""
        with self._lock:
            return sorted(
                [(p / 100, q) for p, q in self._levels.items()],
                key=lambda x: x[0],
            )

    def get_levels_descending(self):
        """Return [(price_dollars, qty), ...] sorted most expensive first."""
        with self._lock:
            return sorted(
                [(p / 100, q) for p, q in self._levels.items()],
                key=lambda x: -x[0],
            )
