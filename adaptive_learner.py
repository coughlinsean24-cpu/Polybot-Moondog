"""
Polybot Snipez — Adaptive Learner
Online Bayesian model that learns which market conditions lead to
both-fill (WIN) vs single-fill (LOSS).

Key insight: Both-fill = guaranteed profit ($0.98/share).
             Single-fill = guaranteed loss (97% adverse selection).

The learner gates bid decisions based on predicted both-fill probability.
It uses:
  1. Feature extraction from live market + BTC + orderbook state
  2. Bayesian logistic regression (online, no external ML deps)
  3. Persistent model state saved to disk after each update
  4. Automatic bootstrapping from historical trade logs

Features (all available at bid-decision time):
  - btc_distance:       |BTC price - candle open| in $
  - btc_distance_pct:   btc_distance / btc_price * 100
  - candle_range:        Current 5-min candle high-low range
  - prior_candle_range:  Previous 5-min candle range
  - secs_remaining:      Seconds until market close
  - combined_ask:        up_ask + down_ask (market efficiency)
  - ask_imbalance:       max(up_ask_size, down_ask_size) / min(...)
  - min_ask_size:        min(up_ask_size, down_ask_size) — liquidity depth
  - total_ask_size:      up_ask_size + down_ask_size
  - hour_utc:            Hour of day (UTC) — market activity proxy
  - bid_price:           Our bid price ($0.02-$0.05)
"""

import json
import math
import os
import random
import time
import threading
from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict
from typing import Optional

# ── Config ──────────────────────────────────────────────────────────────────

MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "learner_data")
MODEL_FILE = os.path.join(MODEL_DIR, "model_state.json")
TRADE_HISTORY_FILE = os.path.join(MODEL_DIR, "trade_features.jsonl")

# Feature names in fixed order for weight vector
FEATURE_NAMES = [
    "bias",                # Always 1.0 — intercept term
    "btc_distance",        # $ distance from candle open
    "btc_distance_pct",    # % distance relative to BTC price
    "candle_range",        # Current 5-min candle range
    "prior_candle_range",  # Previous 5-min candle range
    "secs_remaining",      # Seconds until close
    "combined_ask",        # up_ask + down_ask
    "ask_imbalance",       # Ratio of ask sizes between sides
    "min_ask_size",        # Minimum ask size across sides
    "total_ask_size",      # Total ask liquidity
    "hour_sin",            # sin(2π * hour/24) — cyclical time encoding
    "hour_cos",            # cos(2π * hour/24) — cyclical time encoding
    "bid_price",           # Our bid price
    "spread_from_50c",     # |combined_ask - 1.0| — how far from fair value
    "distance_x_range",    # btc_distance * candle_range — interaction term
    "size_imbal_x_dist",   # ask_imbalance * btc_distance — interaction
]

NUM_FEATURES = len(FEATURE_NAMES)

# Learner defaults
DEFAULT_CONFIDENCE_THRESHOLD = 0.05   # Min predicted both-fill prob to allow bid (low — we're still gathering data)
DEFAULT_LEARNING_RATE = 0.5           # SGD learning rate for online updates
DEFAULT_L2_LAMBDA = 0.01              # L2 regularization strength
MIN_SAMPLES_TO_GATE = 50              # Don't gate decisions until we have this many outcome samples
WARM_UP_TRADES = 20                   # First N trades: always allow (exploration phase)
EXPLORATION_RATE = 0.15               # Even after warm-up, randomly allow 15% of "skip" trades for data collection


# ── Feature Extraction ──────────────────────────────────────────────────────

@dataclass
class TradeFeatures:
    """Features extracted at bid-decision time."""
    # Raw values (for logging)
    btc_price: float = 0.0
    btc_candle_open: float = 0.0
    btc_distance: float = 0.0
    candle_range: float = 0.0
    prior_candle_range: float = 0.0
    secs_remaining: float = 0.0
    up_ask: float = 0.0
    down_ask: float = 0.0
    up_ask_size: float = 0.0
    down_ask_size: float = 0.0
    combined_ask: float = 0.0
    ask_imbalance: float = 0.0
    min_ask_size: float = 0.0
    total_ask_size: float = 0.0
    hour_utc: int = 0
    bid_price: float = 0.0
    market_id: str = ""
    question: str = ""
    timestamp: float = 0.0

    # Outcome (filled after resolution)
    both_filled: Optional[bool] = None
    up_filled: bool = False
    down_filled: bool = False
    pnl: float = 0.0
    winner_side: str = ""

    def to_feature_vector(self) -> list[float]:
        """Convert to normalized feature vector for the model.

        Normalization uses price-relative (%) values so the same ranges
        work for any asset (BTC, ETH, SOL, XRP).
        """
        price = max(self.btc_price, 1.0)  # avoid div by zero
        dist_pct = (self.btc_distance / price) * 100.0       # e.g. 0.08% → 0.08
        range_pct = (self.candle_range / price) * 100.0       # candle range as %
        prior_range_pct = (self.prior_candle_range / price) * 100.0

        hour_rad = 2.0 * math.pi * self.hour_utc / 24.0
        hour_sin = math.sin(hour_rad)
        hour_cos = math.cos(hour_rad)

        spread_from_50c = abs(self.combined_ask - 1.0) if self.combined_ask > 0 else 0.0

        # Normalize features to [0, 1] range — percentage-based for asset independence
        return [
            1.0,                                          # bias
            _norm(dist_pct, 0, 0.5),                      # distance_pct  (0-0.5%)
            _norm(dist_pct, 0, 0.5),                      # distance_pct2 (keeps vector size)
            _norm(range_pct, 0, 1.0),                     # candle_range_pct (0-1%)
            _norm(prior_range_pct, 0, 1.0),               # prior_candle_range_pct
            _norm(self.secs_remaining, 0, 180),            # secs_remaining
            _norm(self.combined_ask, 0, 1.0),              # combined_ask
            _norm(self.ask_imbalance, 1, 20),              # ask_imbalance
            _norm(self.min_ask_size, 0, 5000),             # min_ask_size
            _norm(self.total_ask_size, 0, 10000),          # total_ask_size
            hour_sin,                                      # hour_sin
            hour_cos,                                      # hour_cos
            _norm(self.bid_price, 0, 0.10),                # bid_price
            _norm(spread_from_50c, 0, 1.0),                # spread_from_50c
            _norm(dist_pct * range_pct, 0, 0.25),          # dist_x_range_pct
            _norm(self.ask_imbalance * dist_pct, 0, 5.0),  # imbal_x_dist_pct
        ]

    def to_dict(self) -> dict:
        """Serialize for JSONL logging."""
        d = asdict(self)
        d["feature_vector"] = self.to_feature_vector()
        return d


def _norm(val: float, low: float, high: float) -> float:
    """Normalize to [0, 1] range, clamped."""
    if high <= low:
        return 0.0
    return max(0.0, min(1.0, (val - low) / (high - low)))


def extract_features(
    btc_price: float,
    btc_candle_open: float,
    btc_distance: float,
    candle_range: float,
    prior_candle_range: float,
    secs_remaining: float,
    ws_up,   # LivePrice object or dict
    ws_down, # LivePrice object or dict
    bid_price: float,
    market_id: str = "",
    question: str = "",
) -> TradeFeatures:
    """Extract features from current market state at bid-decision time."""

    # Handle both LivePrice objects and dicts
    if hasattr(ws_up, 'best_ask'):
        up_ask = ws_up.best_ask
        up_ask_size = ws_up.best_ask_size
        down_ask = ws_down.best_ask
        down_ask_size = ws_down.best_ask_size
        up_valid = ws_up.valid
        down_valid = ws_down.valid
    else:
        up_ask = ws_up.get('best_ask', 0)
        up_ask_size = ws_up.get('best_ask_size', 0)
        down_ask = ws_down.get('best_ask', 0)
        down_ask_size = ws_down.get('best_ask_size', 0)
        up_valid = ws_up.get('valid', False)
        down_valid = ws_down.get('valid', False)

    combined_ask = up_ask + down_ask if up_valid and down_valid else 0.0

    if up_ask_size > 0 and down_ask_size > 0:
        ask_imbalance = max(up_ask_size, down_ask_size) / min(up_ask_size, down_ask_size)
    else:
        ask_imbalance = 999.0

    min_ask_size = min(up_ask_size, down_ask_size)
    total_ask_size = up_ask_size + down_ask_size

    now_utc = datetime.now(timezone.utc)
    hour_utc = now_utc.hour

    return TradeFeatures(
        btc_price=btc_price,
        btc_candle_open=btc_candle_open,
        btc_distance=btc_distance,
        candle_range=candle_range,
        prior_candle_range=prior_candle_range,
        secs_remaining=secs_remaining,
        up_ask=up_ask,
        down_ask=down_ask,
        up_ask_size=up_ask_size,
        down_ask_size=down_ask_size,
        combined_ask=combined_ask,
        ask_imbalance=ask_imbalance,
        min_ask_size=min_ask_size,
        total_ask_size=total_ask_size,
        hour_utc=hour_utc,
        bid_price=bid_price,
        market_id=market_id,
        question=question,
        timestamp=time.time(),
    )


# ── Bayesian Online Logistic Regression ─────────────────────────────────────

def _sigmoid(z: float) -> float:
    """Numerically stable sigmoid."""
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    else:
        ez = math.exp(z)
        return ez / (1.0 + ez)


class AdaptiveLearner:
    """Online Bayesian learner that predicts both-fill probability.

    Uses logistic regression with SGD updates and L2 regularization.
    Model state is persisted to disk after each update.

    Decision logic:
      1. Extract features from current market state
      2. Predict P(both_fill) using learned weights
      3. If P(both_fill) >= confidence_threshold → ALLOW bid
      4. If P(both_fill) < confidence_threshold → SKIP bid
      5. After outcome is known → update weights with SGD

    During warm-up (first N trades), always allows bids to gather data.
    """

    def __init__(self):
        self._lock = threading.Lock()

        # Model weights (one per feature)
        self.weights: list[float] = [0.0] * NUM_FEATURES

        # Running stats
        self.total_trades: int = 0
        self.both_fills: int = 0
        self.single_fills: int = 0
        self.no_fills: int = 0
        self.learner_skips: int = 0     # Times learner blocked a bid
        self.learner_allows: int = 0    # Times learner allowed a bid
        self.learner_overrides: int = 0  # Times learner was overridden (warm-up / disabled)

        # P&L tracking for learner-gated trades
        self.gated_pnl: float = 0.0
        self.ungated_pnl: float = 0.0   # What P&L would be without learner

        # Config (live-editable)
        self.enabled: bool = True
        self.confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD
        self.learning_rate: float = DEFAULT_LEARNING_RATE
        self.l2_lambda: float = DEFAULT_L2_LAMBDA
        self.min_samples: int = MIN_SAMPLES_TO_GATE
        self.warm_up: int = WARM_UP_TRADES

        # Pending features (keyed by market_id, waiting for outcome)
        self._pending: dict[str, TradeFeatures] = {}

        # Recent predictions for dashboard display
        self._recent_predictions: list[dict] = []

        # Load persisted state
        self._load_model()

    # ── Prediction ──────────────────────────────────────────────────────

    def predict(self, features: TradeFeatures) -> float:
        """Predict P(both_fill) for given features. Returns probability [0, 1]."""
        x = features.to_feature_vector()
        with self._lock:
            z = sum(w * xi for w, xi in zip(self.weights, x))
        return _sigmoid(z)

    def should_bid(self, features: TradeFeatures) -> tuple[bool, float, str]:
        """Decide whether to place bids on this market.

        Returns:
            (allowed: bool, probability: float, reason: str)
        """
        prob = self.predict(features)

        # Always allow during warm-up / exploration phase
        if not self.enabled:
            reason = f"learner_disabled (prob={prob:.1%})"
            with self._lock:
                self.learner_overrides += 1
            return True, prob, reason

        if self.total_trades < self.warm_up:
            reason = f"warm_up ({self.total_trades}/{self.warm_up}, prob={prob:.1%})"
            with self._lock:
                self.learner_overrides += 1
            return True, prob, reason

        if self.total_trades < self.min_samples:
            reason = f"min_samples ({self.total_trades}/{self.min_samples}, prob={prob:.1%})"
            with self._lock:
                self.learner_overrides += 1
            return True, prob, reason

        # Gate decision based on predicted both-fill probability
        if prob >= self.confidence_threshold:
            with self._lock:
                self.learner_allows += 1
            reason = f"ALLOW (prob={prob:.1%} >= {self.confidence_threshold:.0%})"
            return True, prob, reason
        else:
            # Exploration: randomly allow some "skip" trades to keep gathering data
            if random.random() < EXPLORATION_RATE:
                with self._lock:
                    self.learner_overrides += 1
                reason = f"EXPLORE (prob={prob:.1%} < {self.confidence_threshold:.0%}, forced allow for data)"
                return True, prob, reason
            with self._lock:
                self.learner_skips += 1
            reason = f"SKIP (prob={prob:.1%} < {self.confidence_threshold:.0%})"
            return False, prob, reason

    # ── Recording & Learning ────────────────────────────────────────────

    def record_bid(self, market_id: str, features: TradeFeatures):
        """Record features for a bid that was placed. Call before placing orders."""
        with self._lock:
            self._pending[market_id] = features

    def record_outcome(self, market_id: str, both_filled: bool,
                       up_filled: bool, down_filled: bool,
                       pnl: float, winner_side: str = ""):
        """Record the outcome of a trade and update the model.

        Call this when the market resolves (or when fills are known).
        """
        with self._lock:
            features = self._pending.pop(market_id, None)

        if features is None:
            # Trade happened before learner was initialized
            return

        # Update features with outcome
        features.both_filled = both_filled
        features.up_filled = up_filled
        features.down_filled = down_filled
        features.pnl = pnl
        features.winner_side = winner_side

        # Classify outcome
        with self._lock:
            self.total_trades += 1
            if both_filled:
                self.both_fills += 1
            elif up_filled or down_filled:
                self.single_fills += 1
            else:
                self.no_fills += 1
            self.ungated_pnl += pnl

        # Log features + outcome
        self._log_trade(features)

        # Update model weights (online SGD)
        label = 1.0 if both_filled else 0.0
        self._update_weights(features.to_feature_vector(), label)

        # Save model
        self._save_model()

    def _update_weights(self, x: list[float], label: float):
        """Online SGD update for logistic regression with L2 regularization."""
        with self._lock:
            z = sum(w * xi for w, xi in zip(self.weights, x))
            pred = _sigmoid(z)
            error = label - pred  # positive = under-predicted, negative = over-predicted

            for i in range(NUM_FEATURES):
                # Gradient: error * x_i - L2_reg * w_i
                grad = error * x[i] - self.l2_lambda * self.weights[i]
                self.weights[i] += self.learning_rate * grad

    # ── Historical Candle Data ──────────────────────────────────────────

    def _fetch_historical_candle_ranges(self, jsonl_files: list) -> dict:
        """Fetch 5-min candle ranges from Binance for historical trades.

        Returns dict of market_id -> candle_range (high-low) for each trade.
        Uses Binance klines REST API, batching requests to avoid rate limits.
        """
        import requests

        # First pass: collect timestamps + candle opens for each market_id
        market_times = {}  # market_id -> (timestamp_iso, btc_candle_open)
        for f in jsonl_files:
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if rec.get("type") != "order_placed":
                            continue
                        mid = rec.get("market_id", "")
                        if not mid or mid in market_times:
                            continue
                        # Already has candle_range in log? Skip it.
                        if rec.get("btc_candle_range", 0) > 0:
                            continue
                        ts = rec.get("timestamp", "")
                        candle_open = rec.get("btc_candle_open", 0)
                        if ts and candle_open > 0:
                            market_times[mid] = (ts, candle_open)
            except Exception:
                continue

        if not market_times:
            return {}

        # Group by 5-min candle start time (floor to 5-min boundary)
        candle_cache = {}
        kline_cache = {}  # kline_start_ms -> (high, low) to avoid duplicate API calls

        for mid, (ts_iso, candle_open) in market_times.items():
            try:
                dt = datetime.fromisoformat(ts_iso)
                # Floor to 5-min boundary
                minute = (dt.minute // 5) * 5
                candle_start = dt.replace(minute=minute, second=0, microsecond=0)
                start_ms = int(candle_start.timestamp() * 1000)

                if start_ms in kline_cache:
                    high, low = kline_cache[start_ms]
                else:
                    # Fetch single 5-min kline from Binance
                    try:
                        resp = requests.get(
                            "https://api.binance.com/api/v3/klines",
                            params={
                                "symbol": "BTCUSDT",
                                "interval": "5m",
                                "startTime": start_ms,
                                "limit": 1,
                            },
                            timeout=5,
                        )
                        if resp.status_code == 200:
                            data = resp.json()
                            if data:
                                high = float(data[0][2])
                                low = float(data[0][3])
                                kline_cache[start_ms] = (high, low)
                            else:
                                continue
                        else:
                            continue
                    except Exception:
                        continue
                    # Small delay to respect rate limits
                    time.sleep(0.1)

                candle_cache[mid] = high - low
            except Exception:
                continue

        return candle_cache

    # ── Bootstrap from Historical Logs ──────────────────────────────────

    def bootstrap_from_logs(self, log_dir: str = None):
        """Load historical trade data and train the model on it.

        Reads trades_*.jsonl files, matches order_placed records with
        their resolution records, extracts features, and does batch training.
        Also fetches missing candle_range data from Binance when possible.
        """
        if log_dir is None:
            log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")

        if not os.path.exists(log_dir):
            return 0

        # Collect all JSONL files
        import glob
        jsonl_files = sorted(glob.glob(os.path.join(log_dir, "trades_*.jsonl")))

        # Pre-fetch candle ranges from Binance for historical data
        candle_cache = self._fetch_historical_candle_ranges(jsonl_files)

        orders = {}      # market_id -> {side: order_record}
        resolutions = {} # market_id -> resolution_record
        cancels = {}     # market_id -> cancel_record

        for f in jsonl_files:
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        rtype = rec.get("type", "")
                        mid = rec.get("market_id", "")
                        if not mid:
                            continue

                        if rtype == "order_placed":
                            if mid not in orders:
                                orders[mid] = {}
                            orders[mid][rec.get("side", "")] = rec

                        elif rtype == "resolution":
                            resolutions[mid] = rec

                        elif rtype == "cancel":
                            cancels[mid] = rec
            except Exception:
                continue

        # Match orders with resolutions and train
        trained = 0
        for mid, res in resolutions.items():
            if mid not in orders:
                continue
            sides = orders[mid]
            up_order = sides.get("UP", {})
            down_order = sides.get("DOWN", {})

            # Use UP order for enrichment data (both are placed simultaneously)
            order = up_order if up_order else down_order
            if not order:
                continue

            # Extract features from historical data
            btc_price = order.get("btc_price", 0)
            btc_candle_open = order.get("btc_candle_open", 0)
            btc_distance = order.get("btc_distance", 0)
            prior_candle_range = order.get("btc_prior_candle_range", 0)
            secs_remaining = order.get("secs_remaining", 0)
            bid_price = order.get("price", 0.02)

            # Reconstruct OB data from order record
            up_ask = order.get("up_ask", 0)
            down_ask = order.get("down_ask", 0)
            up_ask_size = order.get("up_ask_size", 0)
            down_ask_size = order.get("down_ask_size", 0)
            combined_ask = order.get("combined_ask", up_ask + down_ask)

            if up_ask_size > 0 and down_ask_size > 0:
                ask_imbalance = max(up_ask_size, down_ask_size) / min(up_ask_size, down_ask_size)
            else:
                ask_imbalance = 999.0

            # Parse hour from timestamp
            try:
                ts = datetime.fromisoformat(order.get("timestamp", ""))
                hour_utc = ts.hour
            except Exception:
                hour_utc = 12

            # Get candle_range: first from log record, then from Binance cache
            candle_range = order.get("btc_candle_range", 0)
            if candle_range <= 0:
                # Try Binance historical data (keyed by 5-min candle start timestamp)
                candle_range = candle_cache.get(mid, 0)

            features = TradeFeatures(
                btc_price=btc_price,
                btc_candle_open=btc_candle_open,
                btc_distance=btc_distance,
                candle_range=candle_range,
                prior_candle_range=prior_candle_range,
                secs_remaining=secs_remaining,
                up_ask=up_ask,
                down_ask=down_ask,
                up_ask_size=up_ask_size,
                down_ask_size=down_ask_size,
                combined_ask=combined_ask,
                ask_imbalance=ask_imbalance,
                min_ask_size=min(up_ask_size, down_ask_size),
                total_ask_size=up_ask_size + down_ask_size,
                hour_utc=hour_utc,
                bid_price=bid_price,
                market_id=mid,
                question=res.get("question", ""),
                timestamp=0,
            )

            # Outcome
            both_filled = res.get("up_filled", False) and res.get("down_filled", False)
            features.both_filled = both_filled
            features.up_filled = res.get("up_filled", False)
            features.down_filled = res.get("down_filled", False)
            features.pnl = res.get("pnl", 0)
            features.winner_side = res.get("winner_side", "")

            # Train
            label = 1.0 if both_filled else 0.0
            self._update_weights(features.to_feature_vector(), label)

            # Update counters
            with self._lock:
                self.total_trades += 1
                if both_filled:
                    self.both_fills += 1
                elif features.up_filled or features.down_filled:
                    self.single_fills += 1
                else:
                    self.no_fills += 1
                self.ungated_pnl += features.pnl

            # Log
            self._log_trade(features)
            trained += 1

        if trained > 0:
            self._save_model()

        return trained

    # ── Persistence ─────────────────────────────────────────────────────

    def _save_model(self):
        """Save model state to disk."""
        os.makedirs(MODEL_DIR, exist_ok=True)
        state = {
            "weights": self.weights,
            "total_trades": self.total_trades,
            "both_fills": self.both_fills,
            "single_fills": self.single_fills,
            "no_fills": self.no_fills,
            "learner_skips": self.learner_skips,
            "learner_allows": self.learner_allows,
            "learner_overrides": self.learner_overrides,
            "gated_pnl": self.gated_pnl,
            "ungated_pnl": self.ungated_pnl,
            "confidence_threshold": self.confidence_threshold,
            "learning_rate": self.learning_rate,
            "l2_lambda": self.l2_lambda,
            "feature_names": FEATURE_NAMES,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            with open(MODEL_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
        except Exception:
            pass

    def _load_model(self):
        """Load model state from disk if it exists."""
        if not os.path.exists(MODEL_FILE):
            return
        try:
            with open(MODEL_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)

            w = state.get("weights", [])
            if len(w) == NUM_FEATURES:
                self.weights = w
            else:
                # Feature set changed — reset weights but keep counts
                self.weights = [0.0] * NUM_FEATURES

            self.total_trades = state.get("total_trades", 0)
            self.both_fills = state.get("both_fills", 0)
            self.single_fills = state.get("single_fills", 0)
            self.no_fills = state.get("no_fills", 0)
            self.learner_skips = state.get("learner_skips", 0)
            self.learner_allows = state.get("learner_allows", 0)
            self.learner_overrides = state.get("learner_overrides", 0)
            self.gated_pnl = state.get("gated_pnl", 0.0)
            self.ungated_pnl = state.get("ungated_pnl", 0.0)
            self.confidence_threshold = state.get("confidence_threshold", DEFAULT_CONFIDENCE_THRESHOLD)
            self.learning_rate = state.get("learning_rate", DEFAULT_LEARNING_RATE)
            self.l2_lambda = state.get("l2_lambda", DEFAULT_L2_LAMBDA)
        except Exception:
            pass

    def _log_trade(self, features: TradeFeatures):
        """Append trade features + outcome to JSONL for analysis."""
        os.makedirs(MODEL_DIR, exist_ok=True)
        try:
            with open(TRADE_HISTORY_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(features.to_dict()) + "\n")
        except Exception:
            pass

    # ── Dashboard Stats ─────────────────────────────────────────────────

    def stats(self) -> dict:
        """Return learner stats for dashboard display."""
        with self._lock:
            both_rate = (self.both_fills / self.total_trades * 100) if self.total_trades > 0 else 0.0

            # Top feature weights (absolute value, sorted)
            weight_info = []
            for i, (name, w) in enumerate(zip(FEATURE_NAMES, self.weights)):
                if name == "bias":
                    continue
                weight_info.append({"name": name, "weight": round(w, 4)})
            weight_info.sort(key=lambda x: abs(x["weight"]), reverse=True)

            return {
                "enabled": self.enabled,
                "total_trades": self.total_trades,
                "both_fills": self.both_fills,
                "single_fills": self.single_fills,
                "no_fills": self.no_fills,
                "both_fill_rate": round(both_rate, 1),
                "learner_skips": self.learner_skips,
                "learner_allows": self.learner_allows,
                "learner_overrides": self.learner_overrides,
                "gated_pnl": round(self.gated_pnl, 2),
                "ungated_pnl": round(self.ungated_pnl, 2),
                "confidence_threshold": self.confidence_threshold,
                "learning_rate": self.learning_rate,
                "top_features": weight_info[:6],
                "warm_up_remaining": max(0, self.warm_up - self.total_trades),
                "min_samples_remaining": max(0, self.min_samples - self.total_trades),
                "is_gating": self.enabled and self.total_trades >= self.min_samples,
            }

    def get_recent_predictions(self) -> list[dict]:
        """Return recent predictions for dashboard display."""
        with self._lock:
            return list(self._recent_predictions[-10:])

    def add_prediction(self, market_id: str, question: str,
                       prob: float, allowed: bool, reason: str):
        """Track a prediction for dashboard display."""
        with self._lock:
            self._recent_predictions.append({
                "ts": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                "market": question[-30:] if len(question) > 30 else question,
                "prob": round(prob * 100, 1),
                "allowed": allowed,
                "reason": reason,
            })
            # Keep last 20
            if len(self._recent_predictions) > 20:
                self._recent_predictions = self._recent_predictions[-20:]

    def reset_model(self):
        """Reset model weights (but keep trade history)."""
        with self._lock:
            self.weights = [0.0] * NUM_FEATURES
            self.total_trades = 0
            self.both_fills = 0
            self.single_fills = 0
            self.no_fills = 0
            self.learner_skips = 0
            self.learner_allows = 0
            self.learner_overrides = 0
            self.gated_pnl = 0.0
            self.ungated_pnl = 0.0
            self._recent_predictions.clear()
        self._save_model()


# ── Module-level singleton ──────────────────────────────────────────────────

learner = AdaptiveLearner()
