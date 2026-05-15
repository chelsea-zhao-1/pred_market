"""
Diagnostic script: detect false arbitrage alerts caused by cross-window pricing
at market rotation boundaries.

Usage:
    python analyze_rotation_artifacts.py <alerts_csv>

Example:
    python analyze_rotation_artifacts.py ../live_market_data/arb_alerts_20260227_131942.csv
"""

import csv
import sys
from datetime import datetime, timezone, timedelta

import pytz

EST_TZ = pytz.timezone("US/Eastern")

# Flag an alert as a rotation artifact if one side has a price near settlement
# and the other is near 50/50 (i.e., from a different window).
EXTREME_THRESHOLD = 0.20   # price < this or > (1 - this) is "near settlement"
MIDRANGE_LOW      = 0.30   # price within [MIDRANGE_LOW, MIDRANGE_HIGH] is "near 50/50"
MIDRANGE_HIGH     = 0.70

# How many seconds around each :00/:15/:30/:45 boundary to consider a rotation zone.
ZONE_RADIUS = 90  # seconds


def nearest_boundary_secs(dt_est):
    """Return signed seconds from dt_est to the nearest :00/:15/:30/:45 boundary."""
    minute = dt_est.minute
    second = dt_est.second
    total_secs = minute * 60 + second

    boundaries = [0, 15 * 60, 30 * 60, 45 * 60, 60 * 60]
    best = None
    for b in boundaries:
        dist = total_secs - b
        if best is None or abs(dist) < abs(best):
            best = dist
    return best  # negative = before boundary, positive = after


def boundary_label(dt_est):
    """Return the nearest :00/:15/:30/:45 boundary as a string."""
    secs = nearest_boundary_secs(dt_est)
    boundary_dt = dt_est - timedelta(seconds=secs)
    return boundary_dt.strftime("%H:%M")


def is_artifact(kalshi_yes_ask, poly_no_ask):
    """True if one side is near settlement and the other is near 50/50."""
    k, p = kalshi_yes_ask, poly_no_ask
    k_extreme = k < EXTREME_THRESHOLD or k > (1 - EXTREME_THRESHOLD)
    p_extreme = p < EXTREME_THRESHOLD or p > (1 - EXTREME_THRESHOLD)
    k_mid = MIDRANGE_LOW < k < MIDRANGE_HIGH
    p_mid = MIDRANGE_LOW < p < MIDRANGE_HIGH
    return (k_extreme and p_mid) or (p_extreme and k_mid)


def load_csv(path):
    rows = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                dt = datetime.strptime(row["timestamp_est"], "%Y-%m-%d %H:%M:%S.%f")
                dt = EST_TZ.localize(dt)
                rows.append({
                    "dt": dt,
                    "leg": row["leg"],
                    "kalshi_yes_ask": float(row["kalshi_yes_ask"]) if row["kalshi_yes_ask"] else None,
                    "kalshi_no_ask":  float(row["kalshi_no_ask"])  if row["kalshi_no_ask"]  else None,
                    "poly_yes_ask":   float(row["poly_yes_ask"])   if row["poly_yes_ask"]   else None,
                    "poly_no_ask":    float(row["poly_no_ask"])    if row["poly_no_ask"]    else None,
                    "net_profit":     float(row["net_profit"]),
                    "cost":           float(row["cost"]),
                })
            except Exception:
                continue
    return rows


def classify(rows):
    for r in rows:
        secs = nearest_boundary_secs(r["dt"])
        r["secs_to_boundary"] = secs
        r["in_zone"] = abs(secs) <= ZONE_RADIUS
        r["boundary"] = boundary_label(r["dt"])

        k = r["kalshi_yes_ask"] if r["leg"] == "A" else r["kalshi_no_ask"]
        p = r["poly_no_ask"]    if r["leg"] == "A" else r["poly_yes_ask"]
        if k is not None and p is not None:
            r["artifact"] = is_artifact(k, p)
        else:
            r["artifact"] = False
    return rows


def bucket_label(secs):
    if secs < -60:
        return "-90s to -61s"
    elif secs < -30:
        return "-60s to -31s"
    elif secs < 0:
        return "-30s to  -1s  ← lead window"
    elif secs < 30:
        return "  0s to +29s  ← post-rotation"
    elif secs < 60:
        return "+30s to +59s"
    else:
        return "+60s to +90s"


def print_boundary_breakdown(rows):
    # Group by boundary
    from collections import defaultdict
    boundaries = defaultdict(list)
    for r in rows:
        if r["in_zone"]:
            boundaries[r["boundary"]].append(r)

    print("\n" + "=" * 70)
    print("PER-BOUNDARY BREAKDOWN")
    print("=" * 70)

    for bnd in sorted(boundaries):
        bnd_rows = boundaries[bnd]
        print(f"\nROTATION at {bnd}:00  ({len(bnd_rows)} alerts in ±{ZONE_RADIUS}s zone)")

        buckets = defaultdict(list)
        for r in bnd_rows:
            buckets[bucket_label(r["secs_to_boundary"])].append(r)

        bucket_order = [
            "-90s to -61s",
            "-60s to -31s",
            "-30s to  -1s  ← lead window",
            "  0s to +29s  ← post-rotation",
            "+30s to +59s",
            "+60s to +90s",
        ]
        for b in bucket_order:
            if b not in buckets:
                continue
            group = buckets[b]
            avg_net = sum(r["net_profit"] for r in group) / len(group)
            max_net = max(r["net_profit"] for r in group)
            n_art = sum(1 for r in group if r["artifact"])
            art_pct = 100 * n_art / len(group)
            print(f"  {b}:  n={len(group):4d}  avg_net=${avg_net:.4f}  "
                  f"max_net=${max_net:.4f}  artifacts={art_pct:.0f}%")


def print_overall_summary(rows):
    normal = [r for r in rows if not r["in_zone"]]
    zone   = [r for r in rows if r["in_zone"]]

    print("\n" + "=" * 70)
    print("OVERALL SUMMARY")
    print("=" * 70)

    def stats(label, group):
        if not group:
            print(f"  {label}: 0 alerts")
            return
        avg = sum(r["net_profit"] for r in group) / len(group)
        mx  = max(r["net_profit"] for r in group)
        n_art = sum(1 for r in group if r["artifact"])
        art_pct = 100 * n_art / len(group)
        print(f"  {label}: n={len(group):5d}  avg_net=${avg:.4f}  "
              f"max_net=${mx:.4f}  artifacts={art_pct:.0f}%")

    stats("Normal (outside zone)", normal)
    stats(f"Zone   (within ±{ZONE_RADIUS}s of boundary)", zone)

    if zone:
        art = [r for r in zone if r["artifact"]]
        print(f"\n  Artifact alerts in zone: {len(art)} / {len(zone)} "
              f"({100*len(art)/len(zone):.0f}%)")


def print_top_suspicious(rows, n=10):
    zone = [r for r in rows if r["in_zone"]]
    top = sorted(zone, key=lambda r: r["net_profit"], reverse=True)[:n]

    print(f"\n{'=' * 70}")
    print(f"TOP {n} HIGHEST-PROFIT ALERTS IN ROTATION ZONE")
    print("=" * 70)
    print(f"  {'Time':22s}  {'Leg'}  {'K_yes':>6}  {'K_no':>6}  "
          f"{'P_yes':>6}  {'P_no':>6}  {'net':>8}  {'art'}")
    print("  " + "-" * 68)
    for r in top:
        art_flag = "*** ARTIFACT" if r["artifact"] else ""
        print(f"  {r['dt'].strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]:22s}  "
              f"{r['leg']:3s}  "
              f"{r['kalshi_yes_ask'] or '':>6}  {r['kalshi_no_ask'] or '':>6}  "
              f"{r['poly_yes_ask'] or '':>6}  {r['poly_no_ask'] or '':>6}  "
              f"${r['net_profit']:>7.4f}  {art_flag}")


def try_plot(rows):
    try:
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates

        fig, ax = plt.subplots(figsize=(14, 5))
        boundaries_seen = set()

        for r in rows:
            color = "red" if r["artifact"] else ("orange" if r["in_zone"] else "steelblue")
            ax.scatter(r["dt"], r["net_profit"], c=color, s=6, alpha=0.6, linewidths=0)

        # Vertical lines at each rotation boundary visible in data
        min_dt = min(r["dt"] for r in rows)
        max_dt = max(r["dt"] for r in rows)
        from datetime import timedelta as td
        dt = min_dt.replace(minute=0, second=0, microsecond=0)
        while dt <= max_dt + td(hours=1):
            for m in [0, 15, 30, 45]:
                b = dt.replace(minute=m, second=0, microsecond=0)
                if min_dt <= b <= max_dt:
                    ax.axvline(b, color="black", linewidth=0.8, linestyle="--", alpha=0.5)
            dt += td(hours=1)

        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=EST_TZ))
        ax.set_xlabel("Time (EST)")
        ax.set_ylabel("Net Profit / contract ($)")
        ax.set_title("Arbitrage Alerts — Red = artifact, Orange = rotation zone, Blue = normal")
        fig.tight_layout()

        out = "rotation_artifacts.png"
        plt.savefig(out, dpi=150)
        print(f"\n  Plot saved to {out}")
    except ImportError:
        print("\n  (matplotlib not available — skipping plot)")


def main():
    if len(sys.argv) < 2:
        print("Usage: python analyze_rotation_artifacts.py <alerts_csv>")
        sys.exit(1)

    path = sys.argv[1]
    rows = load_csv(path)
    print(f"Loaded {len(rows)} alerts from {path}")
    classify(rows)

    print_boundary_breakdown(rows)
    print_overall_summary(rows)
    print_top_suspicious(rows)
    try_plot(rows)


if __name__ == "__main__":
    main()
