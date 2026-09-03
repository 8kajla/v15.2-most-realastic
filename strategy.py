from __future__ import annotations
from dataclasses import dataclass
import json, time, random
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

BANDS: Tuple[Tuple[str, float, float, str], ...] = (
    ("C00_05", 0.00, 0.05, "CHEAP"),
    ("C05_10", 0.05, 0.10, "CHEAP"),
    ("C10_15", 0.10, 0.15, "CHEAP"),
    ("C15_20", 0.15, 0.20, "CHEAP"),
    ("C20_30", 0.20, 0.30, "CHEAP"),
    ("M30_40", 0.30, 0.40, "MID"),
    ("M40_50", 0.40, 0.50, "MID"),
    ("M50_60", 0.50, 0.60, "MID"),
    ("M60_70", 0.60, 0.70, "MID"),
    ("R70_80", 0.70, 0.80, "CORE"),
    ("R80_90", 0.80, 0.90, "CORE"),
    ("H90_95", 0.90, 0.95, "HIGH"),
    ("H95_100", 0.95, 1.00, "HIGH"),
)

TRAJECTORY_SHARE = {
    "CHEAP": {"rising": 0.200, "falling": 0.564, "flat": 0.236},
    "MID": {"rising": 0.410, "falling": 0.450, "flat": 0.140},
    "CORE": {"rising": 0.542, "falling": 0.334, "flat": 0.124},
    "HIGH": {"rising": 0.610, "falling": 0.171, "flat": 0.219},
}

BAND_INDEX = {band: i for i, (band, *_rest) in enumerate(BANDS)}


@dataclass
class Signal:
    side: str
    price: float
    score: float
    notional: float
    reason: str


class EmpiricalTraderProcess:
    """
    Observable trader-process model.

    Uses measured distributions for:
      * global intertrade cadence
      * fine price-band frequency
      * same-side continuation
      * fine-band x entry-number notional

    It deliberately does NOT claim to know the trader's hidden trigger.
    """

    PERSISTENCE = 0.893

    def __init__(self, behavior: dict, seed: int = 20260831):
        self.rng = random.Random(seed)

        gap_rows = behavior.get("intertrade_gap_histogram_seconds") or []
        self.gaps = [float(x["gap_seconds"]) for x in gap_rows]
        self.gap_weights = [float(x["count"]) for x in gap_rows]

        band_rows = behavior.get("fine_bands") or []
        self.band_values = [str(x["fine_band"]) for x in band_rows]
        self.band_weights = [float(x["trade_share"]) for x in band_rows]

        if not self.gaps or not any(self.gap_weights):
            raise ValueError("trader_behavior.json missing intertrade gap distribution")
        if not self.band_values or not any(self.band_weights):
            raise ValueError("trader_behavior.json missing fine-band distribution")

    def sample_gap(self) -> float:
        return float(self.rng.choices(self.gaps, weights=self.gap_weights, k=1)[0])

    def sample_target_band(self) -> str:
        return str(self.rng.choices(self.band_values, weights=self.band_weights, k=1)[0])

    def should_continue_side(self) -> bool:
        return self.rng.random() < self.PERSISTENCE

    @staticmethod
    def distance_to_band(actual_band: str, target_band: str) -> int:
        return abs(BAND_INDEX.get(actual_band, 999) - BAND_INDEX.get(target_band, 999))



class DistributionController:
    """Steer accepted trades toward the trader's measured fine-band shares."""

    def __init__(self, behavior):
        self.trade_targets = {
            str(x["fine_band"]): float(x["trade_share"])
            for x in behavior.get("fine_bands", [])
        }
        self.capital_targets = {
            str(x["fine_band"]): float(x["notional_share"])
            for x in behavior.get("fine_bands", [])
        }
        self.bands = list(self.trade_targets)
        if not self.bands or set(self.bands) != set(self.capital_targets):
            raise ValueError("invalid trader fine-band distributions")
        self.trade_counts = {b: 0 for b in self.bands}
        self.capital = {b: 0.0 for b in self.bands}
        self.total_trades = 0
        self.total_capital = 0.0

    def observe(self, band, notional):
        band = str(band)
        if band not in self.trade_counts:
            raise ValueError(f"unknown empirical band {band}")
        self.trade_counts[band] += 1
        self.capital[band] += max(0.0, float(notional))
        self.total_trades += 1
        self.total_capital += max(0.0, float(notional))

    def projected_error(self, band, notional):
        n = self.total_trades + 1
        d = self.total_capital + max(0.0, float(notional))
        trade_error = 0.0
        capital_error = 0.0
        for b in self.bands:
            actual_t = (self.trade_counts[b] + (1 if b == band else 0)) / n
            actual_c = (
                self.capital[b] + (float(notional) if b == band else 0.0)
            ) / d if d else 0.0
            trade_error += (actual_t - self.trade_targets[b]) ** 2
            capital_error += (actual_c - self.capital_targets[b]) ** 2
        return trade_error + capital_error

    def choose_band(self, candidates):
        if not candidates:
            return None
        # If several candidates share a band, use the smallest projected error
        # for that band. The actual side/market is selected afterward.
        best_band = None
        best_error = float("inf")
        for band in {c["band"] for c in candidates}:
            target_sizes = [float(c["target"]) for c in candidates if c["band"] == band]
            score = min(self.projected_error(band, x) for x in target_sizes)
            if score < best_error:
                best_error = score
                best_band = band
        return best_band

    def shares(self):
        return {
            "trade": {
                b: self.trade_counts[b] / self.total_trades
                if self.total_trades else 0.0 for b in self.bands
            },
            "capital": {
                b: self.capital[b] / self.total_capital
                if self.total_capital else 0.0 for b in self.bands
            },
        }


class CapitalFirstStrategy:
    VERSION = "V15.2_40PCT_EXACT_DISTRIBUTION"
    DATA_FILE = Path(__file__).with_name("trader_behavior.json")
    BANDS = BANDS
    HARD_CUTOFF = 60.0

    def __init__(
        self,
        bankroll=1000,
        start_sec=0,
        stop_sec=240,
        hard_cutoff_seconds=60,
        max_total_exposure=300,
        min_trade_gap_seconds=0,
        behavior_file=None,
        seed=20260831,
        **_,
    ):
        self.bankroll = float(bankroll)
        self.start_sec = max(0.0, float(start_sec))
        self.stop_sec = min(300.0, float(stop_sec))
        self.hard_cutoff_seconds = max(60.0, float(hard_cutoff_seconds))
        self.max_total_exposure = max(0.0, float(max_total_exposure))
        self.min_trade_gap_seconds = max(0.0, float(min_trade_gap_seconds))
        self._last_trade_at: Optional[float] = None

        path = Path(behavior_file) if behavior_file else self.DATA_FILE
        with path.open(encoding="utf-8") as f:
            self.behavior = json.load(f)

        self.notional_scale = float(self.behavior.get("notional_scale", 0.4))
        self.process = EmpiricalTraderProcess(self.behavior, seed=seed)
        self.cadence = self.process
        self.distribution = DistributionController(self.behavior)
        self.fine_band_trade_share = {
            str(x["fine_band"]): float(x["trade_share"])
            for x in self.behavior.get("fine_bands", [])
        }
        self.entry_medians = self.behavior["entry_median_by_fine_band"]

    @classmethod
    def fine_band(cls, price):
        p = float(price)
        for band, lo, hi, regime in cls.BANDS:
            if lo <= p < hi:
                return band, regime
        if p == 1.0:
            return "H95_100", "HIGH"
        return None, None

    def entry_target(self, price, market="BTC", entry_count=0):
        del market
        band, _ = self.fine_band(price)
        if not band:
            return 0.0
        lookup = self.entry_medians.get(band, {})
        key = str(int(entry_count) + 1) if int(entry_count) < 20 else "21+"
        value = lookup.get(key)
        if value is None:
            for fallback in ("1", "2", "21+"):
                if fallback in lookup:
                    value = lookup[fallback]
                    break
        return max(0.10, float(value or 0.0))

    capital_target = entry_target

    @staticmethod
    def _points(history):
        out = []
        for item in history or []:
            try:
                if isinstance(item, dict):
                    ts = float(item["ts"])
                    price = float(item.get("best_bid", item.get("mid")))
                else:
                    ts, price = float(item[0]), float(item[1])
                if 0.0 < price < 1.0:
                    out.append((ts, price))
            except (TypeError, ValueError, KeyError, IndexError):
                continue
        return sorted(out)

    @classmethod
    def movement(cls, price, history, now):
        points = cls._points(history)
        result = {}
        for seconds in (1, 3, 5, 10, 30):
            previous = [p for ts, p in points if ts <= float(now) - seconds]
            result[f"m{seconds}"] = float(price) - previous[-1] if previous else 0.0
        return result

    @staticmethod
    def _trajectory_class(delta):
        return "rising" if delta > 0 else ("falling" if delta < 0 else "flat")

    def _candidate(
        self,
        market,
        side,
        bid,
        ask,
        depth,
        history,
        now,
        thesis_side,
        entries,
        burst_age,
    ):
        if bid is None:
            return None
        try:
            bid = float(bid)
            ask = None if ask is None else float(ask)
            depth = None if depth is None else float(depth)
        except (TypeError, ValueError):
            return None
        if not 0.0 < bid < 1.0:
            return None
        band, regime = self.fine_band(bid)
        if not regime:
            return None
        mv = self.movement(bid, history, now)
        trajectory = self._trajectory_class(mv["m5"])
        trajectory_share = TRAJECTORY_SHARE[regime][trajectory]
        return {
            "side": side,
            "bid": bid,
            "ask": ask,
            "depth": depth,
            "band": band,
            "regime": regime,
            "trajectory": trajectory,
            "trajectory_likelihood": trajectory_share,
            "band_prior": self.fine_band_trade_share.get(band, 0.0),
            "same_side": bool(thesis_side and side == thesis_side),
            "target": self.entry_target(bid, market, entries),
            "movement": mv,
            "entries": int(entries),
            "burst_age": float(burst_age),
        }

    def choose_process_candidate(self, candidates, target_band=None, thesis_side=None):
        if not candidates:
            return None

        pool = list(candidates)
        if thesis_side:
            if self.process.should_continue_side():
                same = [c for c in pool if c["side"] == thesis_side]
                if same:
                    pool = same
            else:
                flips = [c for c in pool if c["side"] != thesis_side]
                if flips:
                    pool = flips

        if target_band is None:
            target_band = self.distribution.choose_band(pool)
        if target_band is None:
            return None

        targeted = [c for c in pool if c["band"] == target_band] or pool
        return min(
            targeted,
            key=lambda c: (
                abs(BAND_INDEX[c["band"]] - BAND_INDEX[target_band]),
                -c["trajectory_likelihood"],
            ),
        )

    def choose_distribution_band(self, candidates):
        return self.distribution.choose_band(candidates)

    def observe_trade_distribution(self, band, notional):
        self.distribution.observe(band, notional)

    def distribution_snapshot(self):
        return self.distribution.shares()

    def sample_target_band(self):
        return self.process.sample_target_band()

    def sample_delay(self):
        return self.process.sample_gap()

    def decide(
        self,
        elapsed,
        up_ask,
        down_ask,
        up_bid,
        down_bid,
        up_history,
        down_history,
        current_exposure,
        available_cash,
        up_depth=0,
        down_depth=0,
        now=None,
        asset_exposure=0,
        total_exposure=0,
        market_entry_count=0,
        seconds_since_first_entry=0,
        thesis_side=None,
        thesis_price=None,
        asset=None,
        market=None,
        process_target_band=None,
    ):
        del current_exposure, asset_exposure, thesis_price
        now = time.time() if now is None else float(now)
        elapsed = float(elapsed)

        if elapsed < self.start_sec:
            return None
        if elapsed >= self.stop_sec:
            return None
        if self.stop_sec - elapsed <= self.hard_cutoff_seconds:
            return None

        m = str(market or asset or "BTC").upper()
        candidates = [
            c for c in (
                self._candidate(m, "Up", up_bid, up_ask, up_depth, up_history, now, thesis_side, market_entry_count, seconds_since_first_entry),
                self._candidate(m, "Down", down_bid, down_ask, down_depth, down_history, now, thesis_side, market_entry_count, seconds_since_first_entry),
            )
            if c is not None
        ]
        if not candidates:
            return None

        target_band = process_target_band or self.distribution.choose_band(candidates)
        best = self.choose_process_candidate(candidates, target_band, thesis_side=thesis_side)
        if best is None:
            return None

        remaining = max(0.0, self.max_total_exposure - float(total_exposure))
        target = float(best["target"])
        notion = min(target, max(0.0, float(available_cash)), remaining)
        if notion < 0.10:
            return None

        self._last_trade_at = now
        mv = best["movement"]
        reason = (
            f"{self.VERSION} target_band={target_band} band={best['band']} "
            f"regime={best['regime']} trajectory={best['trajectory']} "
            f"band_share={best['band_prior']:.6f} "
            f"trajectory_share={best['trajectory_likelihood']:.3f} "
            f"same_side={best['same_side']} passive=bid "
            f"target_40pct=${target:.2f} entry_count={market_entry_count} "
            f"burst_age={float(seconds_since_first_entry):.1f}s "
            f"bid={best['bid']:.4f} "
            f"ask={best['ask'] if best['ask'] is not None else 0:.4f} "
            f"depth={best['depth'] if best['depth'] is not None else 0:.2f} "
            f"m1={mv['m1']:+.4f} m3={mv['m3']:+.4f} "
            f"m5={mv['m5']:+.4f} m10={mv['m10']:+.4f} "
            f"m30={mv['m30']:+.4f} elapsed={elapsed:.1f}s "
            f"left={self.stop_sec-elapsed:.1f}s"
        )
        return Signal(best["side"], best["bid"], best["trajectory_likelihood"], round(notion, 2), reason)

    def size(self, price, regime=None, market="BTC", entry_count=0, **_):
        del regime
        return self.entry_target(price, market, entry_count)
