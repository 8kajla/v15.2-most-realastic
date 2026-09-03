from __future__ import annotations

import csv
import sys
from collections import defaultdict
from pathlib import Path


def rows(path):
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def f(v):
    try:
        return float(v)
    except Exception:
        return 0.0


def main(root: str):
    root = Path(root)
    orders = rows(root / "realistic_orders.csv")
    fills = rows(root / "realistic_fills.csv")
    unfilled = rows(root / "realistic_unfilled.csv") if (root / "realistic_unfilled.csv").exists() else []

    by_order = defaultdict(float)
    latency = []
    for r in fills:
        oid = r.get("order_id", "")
        if r.get("status") in {"FILLED", "PARTIAL"}:
            by_order[oid] += f(r.get("shares"))
        if r.get("fill_latency_s") not in (None, ""):
            latency.append(f(r.get("fill_latency_s")))

    total = len(orders)
    filled = sum(1 for r in orders if r.get("status") == "FILLED")
    expired = len(unfilled)
    print(f"signals={total} filled={filled} expired_unfilled={expired} fill_rate={(filled/total if total else 0):.2%} expire_rate={(expired/total if total else 0):.2%}")
    if latency:
        s = sorted(latency)
        p50 = s[(len(s)-1)//2]
        p90 = s[min(len(s)-1, int(.9*len(s)))]
        print(f"fill_latency_avg_s={sum(s)/len(s):.3f} p50_s={p50:.3f} p90_s={p90:.3f}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "./data")
