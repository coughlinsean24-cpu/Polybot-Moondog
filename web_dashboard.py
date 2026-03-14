"""
Polybot Snipez -- Web Dashboard & Bot Controller
Flask + SocketIO backend that serves a real browser-based dashboard.

Replaces the old Rich terminal dashboard. Bot engine runs in a background
thread; the web UI shows live markets, prices, bids, fills, and P&L.
Controls let you edit bid_price, tokens_per_side, and start/stop the bot
directly from the browser.

Launch:  python web_dashboard.py
Open:    http://localhost:5050
"""

import io
import os
import re
import sys
import csv
import glob
import json
import time
import signal
import threading
from datetime import datetime, timezone, timedelta
from collections import deque

# Fix Windows console encoding before any output
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import requests as _requests

from flask import Flask, render_template_string, request, redirect, url_for, session, Response
from flask_socketio import SocketIO

import config
from logger import (
    log, log_signal, log_trade, log_fire_pair,
    log_outcome, log_cancel, log_error, log_heartbeat,
    log_resolution,
)
from polymarket_client import (
    MarketWindow, OrderBookSnapshot, ScannedMarket,
    get_clob_client, fetch_active_btc_markets, fetch_active_markets,
    fetch_orderbook, fetch_orderbooks_parallel, fetch_full_orderbook,
    place_limit_buy, cancel_order, get_order_status,
    seconds_until, get_adaptive_poll_interval,
    check_market_resolution, scan_active_markets,
)
import polymarket_client as _pm
from ws_feed import PriceFeed
from binance_ws import BinanceFeed
from data_recorder import recorder as data_rec
from adaptive_learner import learner as ai_learner, extract_features
from arb import ArbEngine


# ── Settings Persistence ────────────────────────────────────────────────────
_SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")

def _save_settings():
    """Persist current engine settings to disk so they survive restarts."""
    try:
        data = {
            "bid_price": engine.bid_price,
            "tokens_per_side": engine.tokens_per_side,
            "bid_window_open": engine.bid_window_open,
            "bid_window_close": engine.bid_window_close,
            "asset_config": {a: dict(c) for a, c in engine.asset_config.items()},
            "ob_filter_enabled": engine.ob_filter_enabled,
            "ob_min_size": engine.ob_min_size,
            "ob_max_imbalance": engine.ob_max_imbalance,
            "scanner": dict(engine.scanner_cfg),
            "scanner_auto_bid": engine.scanner_auto_bid,
            "auto_trade_enabled": engine.auto_trade_enabled,
            "arb": {
                "enabled": engine.arb.enabled if engine.arb else config.ARB_ENABLED,
                "min_edge": engine.arb.min_edge if engine.arb else config.ARB_MIN_EDGE,
                "trade_size": engine.arb.trade_size if engine.arb else config.ARB_TRADE_SIZE,
                "max_positions": engine.arb.max_positions if engine.arb else config.ARB_MAX_POSITIONS,
                "cooldown": engine.arb.cooldown if engine.arb else config.ARB_COOLDOWN,
                "fill_timeout": engine.arb.fill_timeout if engine.arb else config.ARB_FILL_TIMEOUT,
                "max_daily_spend": engine.arb.max_daily_spend if engine.arb else config.ARB_MAX_DAILY_SPEND,
            },
        }
        with open(_SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        log.info(f"[SETTINGS] Saved to {_SETTINGS_FILE}")
    except Exception as e:
        log.error(f"[SETTINGS] Failed to save: {e}")


def _load_settings():
    """Restore engine settings from disk if settings.json exists."""
    if not os.path.exists(_SETTINGS_FILE):
        log.info("[SETTINGS] No settings.json found, using defaults")
        return
    try:
        with open(_SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        engine.bid_price = 0.01  # Always fixed at $0.01
        engine.tokens_per_side = int(data.get("tokens_per_side", engine.tokens_per_side))
        engine.bid_window_open = int(data.get("bid_window_open", engine.bid_window_open))
        engine.bid_window_close = int(data.get("bid_window_close", engine.bid_window_close))
        engine.ob_filter_enabled = bool(data.get("ob_filter_enabled", engine.ob_filter_enabled))
        # Enforce minimum OB filter thresholds — never allow zero (log-only)
        engine.ob_min_size = max(50.0, float(data.get("ob_min_size", engine.ob_min_size)))
        engine.ob_max_imbalance = max(3.0, float(data.get("ob_max_imbalance", engine.ob_max_imbalance)))
        # Per-asset config
        saved_ac = data.get("asset_config", {})
        for a, cfg in saved_ac.items():
            if a in engine.asset_config and isinstance(cfg, dict):
                for k, v in cfg.items():
                    if k in engine.asset_config[a]:
                        if k == "enabled":
                            engine.asset_config[a][k] = bool(v)
                        elif k in ("tokens_per_side", "bid_window_open"):
                            engine.asset_config[a][k] = int(v)
                        elif k == "bid_price":
                            engine.asset_config[a][k] = 0.01  # Always force $0.01
                        else:
                            engine.asset_config[a][k] = float(v)
        engine.btc_dollar_range = engine.asset_config.get("BTC", {}).get("dollar_range", 2.0)
        # Scanner config
        scanner = data.get("scanner", {})
        if scanner and isinstance(scanner, dict):
            for k, v in scanner.items():
                if k in engine.scanner_cfg:
                    if k in ("categories", "auto_bid_categories"):
                        if isinstance(v, list):
                            engine.scanner_cfg[k] = v
                    elif k in ("scan_interval", "auto_bid_size"):
                        engine.scanner_cfg[k] = int(v)
                    else:
                        engine.scanner_cfg[k] = float(v)
        engine.scanner_auto_bid = bool(data.get("scanner_auto_bid", False))
        engine.auto_trade_enabled = bool(data.get("auto_trade_enabled", False))
        # Arb settings — applied to engine.arb once it's initialized
        arb_cfg = data.get("arb", {})
        if arb_cfg and isinstance(arb_cfg, dict):
            engine._arb_saved_cfg = arb_cfg  # Stash for init_arb() to apply later
        log.info(f"[SETTINGS] Restored from {_SETTINGS_FILE}")
        enabled = [a for a, c in engine.asset_config.items() if c.get('enabled')]
        log.info(f"[SETTINGS] Assets enabled: {enabled}, bid_price={engine.bid_price}, tokens={engine.tokens_per_side}")
    except Exception as e:
        log.error(f"[SETTINGS] Failed to load: {e}")


# ── Per-Asset "Riding the Line" Filters ─────────────────────────────────────
# Each asset has different price scales and 5-min volatility profiles,
# so distance%, candle range, and dollar range thresholds must be tuned
# individually.
#
#   distance_pct_max: max |price - candle_open| / price before we skip
#   candle_range_max: max 5-min candle high-low before we skip (momentum)
#   dollar_range:     max |price - candle_open| in $ before we skip (coarse)
#   expansion_max:    max candle_range / prior_candle_range before we skip
#   expansion_floor:  min candle_range $ for expansion rule to engage

ASSET_FILTERS = {
    "BTC": {
        "bid_price": 0.01,              # $0.01 per share — 97% win rate at this level
        "tokens_per_side": 100,         # shares per side (per-asset)
        "bid_window_open": 60,          # seconds before close to cancel loser side
        "distance_pct_max": 0.0008,     # 0.080%  (~$78 at $97k)
        "candle_range_max": 50.0,       # $50 max 5-min candle
        "dollar_range": 2.0,            # $2 default UI-editable range
        "expansion_max": 1.5,           # 1.5x prior candle
        "expansion_floor": 30.0,        # only if range > $30
    },
    "ETH": {
        "bid_price": 0.01,              # $0.01 per share — 97% win rate at this level
        "tokens_per_side": 100,         # shares per side (per-asset)
        "bid_window_open": 60,          # seconds before close to cancel loser side
        "distance_pct_max": 0.0008,     # 0.08% (tightened from 0.10% — ETH has worse signal-to-noise)
        "candle_range_max": 5.0,        # $5 max (tightened from $8 — reduce momentum traps)
        "dollar_range": 0.80,           # $0.80 (tightened from $1)
        "expansion_max": 1.3,           # 1.3x (tightened from 1.5x)
        "expansion_floor": 3.0,         # $3 (tightened from $4)
    },
    "SOL": {
        "bid_price": 0.01,              # $0.01 per share — 97% win rate at this level
        "tokens_per_side": 100,         # shares per side (per-asset)
        "bid_window_open": 60,          # seconds before close to cancel loser side
        "distance_pct_max": 0.0015,     # 0.15%  (~$0.25 at $170)
        "candle_range_max": 0.80,       # $0.80 max
        "dollar_range": 0.15,           # $0.15 default
        "expansion_max": 1.5,
        "expansion_floor": 0.40,
    },
    "XRP": {
        "bid_price": 0.01,              # $0.01 per share — 97% win rate at this level
        "tokens_per_side": 100,         # shares per side (per-asset)
        "bid_window_open": 60,          # seconds before close to cancel loser side
        "distance_pct_max": 0.0020,     # 0.20%  (~$0.005 at $2.50)
        "candle_range_max": 0.010,      # $0.01 max
        "dollar_range": 0.003,          # $0.003 default
        "expansion_max": 1.5,
        "expansion_floor": 0.005,
    },
}

# Fallback filter for any unknown asset (permissive — let bids through)
_DEFAULT_FILTER = {
    "distance_pct_max": 0.0020,
    "candle_range_max": 999999.0,
    "dollar_range": 0.0,
    "expansion_max": 2.0,
    "expansion_floor": 999999.0,
}

def get_asset_filter(asset: str) -> dict:
    """Return the riding-the-line filter dict for an asset."""
    return ASSET_FILTERS.get(asset.upper(), _DEFAULT_FILTER)


# ── Flask app ───────────────────────────────────────────────────────────────

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET", "polybot-snipez-2026")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ── Dashboard password (empty = no auth required) ──────────────────────────
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")

LOGIN_HTML = """
<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Polybot Snipez - Login</title>
<style>
  body{background:#0d1117;color:#e6edf3;font-family:'Segoe UI',system-ui,sans-serif;
       display:flex;justify-content:center;align-items:center;min-height:100vh;}
  .box{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:40px;
       text-align:center;min-width:320px;}
  h2{margin-bottom:20px;color:#58a6ff;}
  input{background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:10px 16px;
        border-radius:6px;font-size:16px;width:100%;margin-bottom:16px;}
  button{background:#238636;color:#fff;border:none;padding:10px 24px;border-radius:6px;
         font-size:16px;cursor:pointer;width:100%;}
  button:hover{background:#2ea043;}
  .err{color:#f85149;margin-bottom:12px;font-size:14px;}
</style></head><body>
<div class="box">
  <h2>&#x1f512; Polybot Snipez</h2>
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
  <form method="POST">
    <input type="password" name="password" placeholder="Dashboard password" autofocus>
    <button type="submit">Login</button>
  </form>
</div></body></html>
"""

@app.route("/login", methods=["GET", "POST"])
def login():
    if not DASHBOARD_PASSWORD:
        return redirect("/")
    if request.method == "POST":
        if request.form.get("password") == DASHBOARD_PASSWORD:
            session["authed"] = True
            return redirect("/")
        return render_template_string(LOGIN_HTML, error="Wrong password")
    return render_template_string(LOGIN_HTML, error=None)

@app.before_request
def require_auth():
    if not DASHBOARD_PASSWORD:          # no password set => open access
        return
    if request.endpoint in ("login", "static"):
        return
    # Allow socket.io requests (they use the session cookie from login)
    if request.path.startswith("/socket.io"):
        if not session.get("authed"):
            return  # socket.io handles its own auth; don't redirect
        return
    if not session.get("authed"):
        return redirect("/login")


# ══════════════════════════════════════════════════════════════════════════════
#  BOT ENGINE  (runs in background thread, pushes state to web UI via SocketIO)
# ══════════════════════════════════════════════════════════════════════════════

class BidRecord:
    """Tracks a pair of limit-buy orders for one market."""
    def __init__(self, market_id: str, question: str):
        self.market_id = market_id
        self.question = question
        self.order_id_up: str | None = None
        self.order_id_down: str | None = None
        self.posted_at: float = 0.0
        self.bid_price: float = 0.0
        self.tokens: int = 0
        self.up_filled: bool = False
        self.down_filled: bool = False
        self.up_fill_size: float = 0.0
        self.down_fill_size: float = 0.0
        self.cancelled: bool = False
        self.cancel_phase_done: bool = False  # True once smart-cancel has run at T-20s

        # Post-place OB imbalance tracking
        self.ob_imbalance: float = 0.0      # Current ask-size ratio (higher = more imbalanced)
        self.ob_heavy_side: str = ""         # "UP" or "DOWN" — side with larger ask size
        self.ob_up_ask_size: float = 0.0
        self.ob_down_ask_size: float = 0.0
        self.ob_snapshots: int = 0           # How many OB snapshots we've taken since placement

    def to_dict(self):
        return {
            "market_id": self.market_id,
            "question": self.question,
            "order_id_up": self.order_id_up,
            "order_id_down": self.order_id_down,
            "posted_at": self.posted_at,
            "bid_price": self.bid_price,
            "tokens": self.tokens,
            "up_filled": self.up_filled,
            "down_filled": self.down_filled,
            "up_fill_size": self.up_fill_size,
            "down_fill_size": self.down_fill_size,
            "cancelled": self.cancelled,
            "ob_imbalance": round(self.ob_imbalance, 2),
            "ob_heavy_side": self.ob_heavy_side,
            "ob_up_ask_size": self.ob_up_ask_size,
            "ob_down_ask_size": self.ob_down_ask_size,
        }


class AfterHoursEvent:
    """Tracks a single fill event that occurred near market close.
    Captures timing, price proximity to the target line (candle open),
    and which side filled — for post-hoc pattern analysis."""

    def __init__(self, market_id: str, question: str, asset: str):
        self.market_id = market_id
        self.question = question
        self.asset = asset
        self.ts: float = time.time()               # Unix timestamp of fill detection
        self.ts_str: str = ""                       # Human-readable UTC time
        self.side: str = ""                         # "UP", "DOWN", or "BOTH"
        self.fill_size: float = 0.0                 # Shares filled (max of up/down for BOTH)
        self.bid_price: float = 0.0                # Bid price that was used for this order
        self.secs_to_close: float = 0.0            # + = pre-close, - = post-close
        self.up_ask: float = 0.0                   # UP ask at detection time
        self.down_ask: float = 0.0                 # DOWN ask at detection time
        self.combined_ask: float = 0.0             # UP + DOWN
        self.ask_diff: float = 0.0                 # |UP - DOWN| (symmetry indicator)
        self.price_distance: float = 0.0           # |price - candle_open| in $
        self.price_distance_pct: float = 0.0       # distance as % of price
        self.range_pct: float = 0.0                # distance as % of candle range (universal)
        self.candle_range: float = 0.0             # 5-min candle high-low range
        self.asset_price: float = 0.0              # Current asset price
        self.candle_open: float = 0.0              # 5-min candle open price
        self.on_the_line: bool = False             # True if price very close to open
        self.winner: str = "pending"               # "Up", "Down", or "pending"
        self.won: str = "pending"                  # "WIN", "LOSS", "BOTH" (guaranteed), or "pending"
        self.fill_cost: float = 0.0                # bid_price * fill_size (total $ spent)

    def to_dict(self) -> dict:
        return {
            "market_id": self.market_id,
            "question": self.question,
            "asset": self.asset,
            "ts": self.ts,
            "ts_str": self.ts_str,
            "side": self.side,
            "fill_size": self.fill_size,
            "bid_price": round(self.bid_price, 4),
            "fill_cost": round(self.fill_cost, 4),
            "secs_to_close": round(self.secs_to_close, 1),
            "up_ask": round(self.up_ask, 4),
            "down_ask": round(self.down_ask, 4),
            "combined_ask": round(self.combined_ask, 4),
            "ask_diff": round(self.ask_diff, 4),
            "price_distance": round(self.price_distance, 4),
            "price_distance_pct": round(self.price_distance_pct * 100, 4),
            "range_pct": round(self.range_pct * 100, 2),
            "candle_range": round(self.candle_range, 4),
            "asset_price": round(self.asset_price, 2),
            "candle_open": round(self.candle_open, 2),
            "on_the_line": self.on_the_line,
            "winner": self.winner,
            "won": self.won,
            "is_ours": True,
        }


# ── Post-Close Orderbook Monitor ────────────────────────────────────────────
# Watches ALL crypto 5m markets (not just ones we bid on) around close time.
# Takes orderbook snapshots, detects fills by comparing bid levels,
# then tags each fill with the resolution winner for pattern analysis.

class ObservedFill:
    """A fill detected on any market by comparing orderbook snapshots.
    This tracks ALL market activity, not just our own bids."""

    def __init__(self):
        self.market_id: str = ""
        self.question: str = ""
        self.asset: str = ""
        self.ts: float = time.time()
        self.ts_str: str = ""
        self.side: str = ""                  # "UP" or "DOWN"
        self.fill_price: float = 0.0         # Price level where fill was detected
        self.fill_amount: float = 0.0        # Shares filled (size decrease)
        self.fill_cost: float = 0.0          # fill_price * fill_amount
        self.secs_after_close: float = 0.0   # Seconds after market close (positive = after)
        self.snap_label: str = ""            # Which snapshot pair detected it (e.g. "close→+30s")
        self.up_ask: float = 0.0             # UP best ask at detection (from REST book)
        self.down_ask: float = 0.0           # DOWN best ask at detection (from REST book)
        self.combined_ask: float = 0.0
        self.up_best_bid: float = 0.0        # UP best bid at detection
        self.down_best_bid: float = 0.0      # DOWN best bid at detection
        self.up_spread: float = 0.0          # UP ask - bid spread
        self.down_spread: float = 0.0        # DOWN ask - bid spread
        self.up_ask_depth: float = 0.0       # Total $ on UP ask side
        self.down_ask_depth: float = 0.0     # Total $ on DOWN ask side
        self.up_bid_depth: float = 0.0       # Total $ on UP bid side
        self.down_bid_depth: float = 0.0     # Total $ on DOWN bid side
        self.up_ask_levels: int = 0          # # of distinct UP ask price levels
        self.down_ask_levels: int = 0        # # of distinct DOWN ask price levels
        self.up_bid_levels: int = 0          # # of distinct UP bid price levels
        self.down_bid_levels: int = 0        # # of distinct DOWN bid price levels
        self.asset_price: float = 0.0
        self.candle_open: float = 0.0
        self.price_distance: float = 0.0
        self.range_pct: float = 0.0
        self.candle_range: float = 0.0
        self.on_the_line: bool = False
        self.winner: str = "pending"
        self.won: str = "pending"            # "WIN", "LOSS", or "pending"
        self.is_ours: bool = False           # True if we had a bid on this market

    def to_dict(self) -> dict:
        return {
            "market_id": self.market_id,
            "question": self.question,
            "asset": self.asset,
            "ts": self.ts,
            "ts_str": self.ts_str,
            "side": self.side,
            "fill_price": round(self.fill_price, 4),
            "fill_amount": round(self.fill_amount, 1),
            "fill_cost": round(self.fill_cost, 4),
            "secs_after_close": round(self.secs_after_close, 1),
            "snap_label": self.snap_label,
            "up_ask": round(self.up_ask, 4),
            "down_ask": round(self.down_ask, 4),
            "combined_ask": round(self.combined_ask, 4),
            "up_best_bid": round(self.up_best_bid, 4),
            "down_best_bid": round(self.down_best_bid, 4),
            "up_spread": round(self.up_spread, 4),
            "down_spread": round(self.down_spread, 4),
            "up_ask_depth": round(self.up_ask_depth, 2),
            "down_ask_depth": round(self.down_ask_depth, 2),
            "up_bid_depth": round(self.up_bid_depth, 2),
            "down_bid_depth": round(self.down_bid_depth, 2),
            "up_ask_levels": self.up_ask_levels,
            "down_ask_levels": self.down_ask_levels,
            "up_bid_levels": self.up_bid_levels,
            "down_bid_levels": self.down_bid_levels,
            "asset_price": round(self.asset_price, 2),
            "candle_open": round(self.candle_open, 2),
            "price_distance": round(self.price_distance, 4),
            "range_pct": round(self.range_pct * 100, 2),
            "candle_range": round(self.candle_range, 4),
            "on_the_line": self.on_the_line,
            "winner": self.winner,
            "won": self.won,
            "is_ours": self.is_ours,
        }


class PostCloseMonitor:
    """Monitors orderbooks on ALL crypto 5m markets around close time.
    Takes snapshots at close and post-close, detects fills by comparing
    bid levels, then checks resolution to find winning side.

    Runs as background threads — one per closing market."""

    def __init__(self):
        self._monitored: set = set()   # market_ids currently being monitored
        self._lock = threading.Lock()

    def should_monitor(self, market_id: str, secs_to_close: float) -> bool:
        """Return True if this market should start being monitored (close to closing)."""
        if market_id in self._monitored:
            return False
        # Start monitoring when market is within 10 seconds of close
        return -2 <= secs_to_close <= 10

    def start_monitoring(self, market: MarketWindow):
        """Spawn a background thread to monitor this market through close + post-close."""
        with self._lock:
            if market.market_id in self._monitored:
                return
            self._monitored.add(market.market_id)

        t = threading.Thread(
            target=self._monitor_market,
            args=(market,),
            daemon=True,
            name=f"pcm-{market.asset}-{market.market_id[:8]}",
        )
        t.start()

    def _monitor_market(self, market: MarketWindow):
        """Main monitoring routine for a single market.
        Takes orderbook snapshots at close and post-close intervals,
        detects fills by comparing bid sizes, then checks resolution."""
        try:
            mid = market.market_id
            asset = market.asset
            secs = seconds_until(market.end_time)

            # Wait until market closes (if we started a few seconds early)
            if secs > 0:
                time.sleep(secs + 0.5)

            # ── Snapshot schedule: close, +15s, +30s, +60s, +90s ──
            snapshot_times = [0, 15, 30, 60, 90]
            snapshots = []  # list of (label, seconds_after_close, {up_bids, down_bids, up_ask, down_ask})

            for i, delay in enumerate(snapshot_times):
                if i > 0:
                    time.sleep(snapshot_times[i] - snapshot_times[i - 1])

                try:
                    book_up = fetch_full_orderbook(market.token_id_up)
                    book_down = fetch_full_orderbook(market.token_id_down)
                except Exception as e:
                    log.info(f"[PCM] snapshot error {asset} +{delay}s: {e}")
                    continue

                # Extract best ask from REST orderbook (WS feed goes stale after close)
                up_asks_raw = book_up.get("asks", [])
                down_asks_raw = book_down.get("asks", [])
                up_best_ask = min((float(a["price"]) for a in up_asks_raw), default=0.0)
                down_best_ask = min((float(a["price"]) for a in down_asks_raw), default=0.0)

                # Calculate total ask-side depth for context
                up_ask_depth = sum(float(a["size"]) * float(a["price"]) for a in up_asks_raw)
                down_ask_depth = sum(float(a["size"]) * float(a["price"]) for a in down_asks_raw)
                up_bid_depth = sum(b["size"] * b["price"] for b in book_up.get("bids", []))
                down_bid_depth = sum(b["size"] * b["price"] for b in book_down.get("bids", []))

                # Count distinct price levels
                up_ask_levels = len(up_asks_raw)
                down_ask_levels = len(down_asks_raw)
                up_bid_levels = len(book_up.get("bids", []))
                down_bid_levels = len(book_down.get("bids", []))

                # Best bid for spread calculation
                up_best_bid = max((float(b["price"]) for b in book_up.get("bids", [])), default=0.0)
                down_best_bid = max((float(b["price"]) for b in book_down.get("bids", [])), default=0.0)

                label = "close" if delay == 0 else f"+{delay}s"
                snap = {
                    "label": label,
                    "secs": delay,
                    "up_bids": {round(b["price"], 2): b["size"] for b in book_up.get("bids", [])},
                    "down_bids": {round(b["price"], 2): b["size"] for b in book_down.get("bids", [])},
                    "up_ask": round(up_best_ask, 4),
                    "down_ask": round(down_best_ask, 4),
                    "up_best_bid": round(up_best_bid, 4),
                    "down_best_bid": round(down_best_bid, 4),
                    "up_ask_depth": round(up_ask_depth, 2),
                    "down_ask_depth": round(down_ask_depth, 2),
                    "up_bid_depth": round(up_bid_depth, 2),
                    "down_bid_depth": round(down_bid_depth, 2),
                    "up_ask_levels": up_ask_levels,
                    "down_ask_levels": down_ask_levels,
                    "up_bid_levels": up_bid_levels,
                    "down_bid_levels": down_bid_levels,
                }
                snapshots.append(snap)
                log.info(f"[PCM] {asset} {label}: UP bids={len(snap['up_bids'])} DOWN bids={len(snap['down_bids'])}")

            # ── Detect fills by comparing consecutive snapshots ──
            fills_detected = []
            for i in range(1, len(snapshots)):
                prev = snapshots[i - 1]
                curr = snapshots[i]
                snap_label = f"{prev['label']}→{curr['label']}"
                secs_after = curr["secs"]

                # Compare UP side bids
                for price_level, prev_size in prev["up_bids"].items():
                    curr_size = curr["up_bids"].get(price_level, 0.0)
                    if curr_size < prev_size:
                        filled = prev_size - curr_size
                        fills_detected.append({
                            "side": "UP",
                            "price": price_level,
                            "amount": filled,
                            "secs": secs_after,
                            "snap_label": snap_label,
                            "up_ask": curr["up_ask"],
                            "down_ask": curr["down_ask"],
                            "up_best_bid": curr.get("up_best_bid", 0.0),
                            "down_best_bid": curr.get("down_best_bid", 0.0),
                            "up_ask_depth": curr.get("up_ask_depth", 0.0),
                            "down_ask_depth": curr.get("down_ask_depth", 0.0),
                            "up_bid_depth": curr.get("up_bid_depth", 0.0),
                            "down_bid_depth": curr.get("down_bid_depth", 0.0),
                            "up_ask_levels": curr.get("up_ask_levels", 0),
                            "down_ask_levels": curr.get("down_ask_levels", 0),
                            "up_bid_levels": curr.get("up_bid_levels", 0),
                            "down_bid_levels": curr.get("down_bid_levels", 0),
                        })

                # Compare DOWN side bids
                for price_level, prev_size in prev["down_bids"].items():
                    curr_size = curr["down_bids"].get(price_level, 0.0)
                    if curr_size < prev_size:
                        filled = prev_size - curr_size
                        fills_detected.append({
                            "side": "DOWN",
                            "price": price_level,
                            "amount": filled,
                            "secs": secs_after,
                            "snap_label": snap_label,
                            "up_ask": curr["up_ask"],
                            "down_ask": curr["down_ask"],
                            "up_best_bid": curr.get("up_best_bid", 0.0),
                            "down_best_bid": curr.get("down_best_bid", 0.0),
                            "up_ask_depth": curr.get("up_ask_depth", 0.0),
                            "down_ask_depth": curr.get("down_ask_depth", 0.0),
                            "up_bid_depth": curr.get("up_bid_depth", 0.0),
                            "down_bid_depth": curr.get("down_bid_depth", 0.0),
                            "up_ask_levels": curr.get("up_ask_levels", 0),
                            "down_ask_levels": curr.get("down_ask_levels", 0),
                            "up_bid_levels": curr.get("up_bid_levels", 0),
                            "down_bid_levels": curr.get("down_bid_levels", 0),
                        })

            # ── Check resolution ──
            winner = "pending"
            for attempt in range(4):
                if attempt > 0:
                    time.sleep(30)
                try:
                    result = check_market_resolution(mid)
                    if result["resolved"]:
                        winner = result["winner"]
                        break
                except Exception:
                    pass

            # ── Build ObservedFill events ──
            is_ours = mid in engine.bids_posted
            ast = engine.btc_feed.get(asset)
            now_ts = time.time()
            ts_str = datetime.now(timezone.utc).strftime("%H:%M:%S")

            for fd in fills_detected:
                ev = ObservedFill()
                ev.market_id = mid
                ev.question = market.question
                ev.asset = asset
                ev.ts = now_ts
                ev.ts_str = ts_str
                ev.side = fd["side"]
                ev.fill_price = fd["price"]
                ev.fill_amount = fd["amount"]
                ev.fill_cost = fd["price"] * fd["amount"]
                ev.secs_after_close = fd["secs"]
                ev.snap_label = fd["snap_label"]
                ev.up_ask = fd["up_ask"]
                ev.down_ask = fd["down_ask"]
                ev.combined_ask = round(fd["up_ask"] + fd["down_ask"], 4)
                ev.up_best_bid = fd.get("up_best_bid", 0.0)
                ev.down_best_bid = fd.get("down_best_bid", 0.0)
                ev.up_spread = round(fd["up_ask"] - fd.get("up_best_bid", 0.0), 4) if fd["up_ask"] > 0 and fd.get("up_best_bid", 0) > 0 else 0.0
                ev.down_spread = round(fd["down_ask"] - fd.get("down_best_bid", 0.0), 4) if fd["down_ask"] > 0 and fd.get("down_best_bid", 0) > 0 else 0.0
                ev.up_ask_depth = fd.get("up_ask_depth", 0.0)
                ev.down_ask_depth = fd.get("down_ask_depth", 0.0)
                ev.up_bid_depth = fd.get("up_bid_depth", 0.0)
                ev.down_bid_depth = fd.get("down_bid_depth", 0.0)
                ev.up_ask_levels = fd.get("up_ask_levels", 0)
                ev.down_ask_levels = fd.get("down_ask_levels", 0)
                ev.up_bid_levels = fd.get("up_bid_levels", 0)
                ev.down_bid_levels = fd.get("down_bid_levels", 0)
                ev.is_ours = is_ours

                if ast and ast.price > 0:
                    ev.asset_price = ast.price
                    ev.candle_open = ast.candle_open
                    ev.price_distance = ast.distance
                    ev.candle_range = ast.candle_range
                    ev.range_pct = (ast.distance / ast.candle_range) if ast.candle_range > 0 else 0.0
                    ev.on_the_line = ev.range_pct < 0.25 if ast.candle_range > 0 else False

                ev.winner = winner
                if winner != "pending":
                    if fd["side"] == "UP":
                        ev.won = "WIN" if winner == "Up" else "LOSS"
                    elif fd["side"] == "DOWN":
                        ev.won = "WIN" if winner == "Down" else "LOSS"

                ev_d = ev.to_dict()
                engine.afterhours_events.appendleft(ev_d)
                _ah_save_event(ev_d)
                try:
                    socketio.emit("ah_new_event", ev_d)
                except Exception:
                    pass

            if fills_detected:
                win_count = sum(1 for fd in fills_detected
                              if (fd["side"] == "UP" and winner == "Up") or
                                 (fd["side"] == "DOWN" and winner == "Down"))
                log.info(f"[PCM] {asset} {market.question[:40]}: {len(fills_detected)} fills detected, "
                         f"winner={winner}, {win_count} on winning side")
            else:
                log.info(f"[PCM] {asset} {market.question[:40]}: no fills detected post-close")

            # ── Also update any existing AH events for this market with winner ──
            if winner != "pending":
                _update_afterhours_resolution(mid, winner)
                _ah_save_all()

        except Exception as e:
            log.error(f"[PCM] monitor error: {e}")
        finally:
            with self._lock:
                self._monitored.discard(market.market_id)


# ── After-Hours Persistence Helpers ──────────────────────────────────────────

_AH_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "afterhours_fills.jsonl")


def _ah_save_event(ev_dict: dict):
    """Append a single event dict to the persistent JSONL file."""
    try:
        os.makedirs(os.path.dirname(_AH_FILE), exist_ok=True)
        with open(_AH_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev_dict, default=str) + "\n")
    except Exception as e:
        log.debug(f"[AH-persist] save error: {e}")


def _ah_save_all():
    """Rewrite the entire JSONL file from the current deque (after resolution updates or clear)."""
    try:
        os.makedirs(os.path.dirname(_AH_FILE), exist_ok=True)
        with open(_AH_FILE, "w", encoding="utf-8") as f:
            for ev in engine.afterhours_events:
                f.write(json.dumps(ev, default=str) + "\n")
    except Exception as e:
        log.debug(f"[AH-persist] save-all error: {e}")


def _ah_load():
    """Load persisted events back into engine.afterhours_events on startup."""
    if not os.path.exists(_AH_FILE):
        return
    count = 0
    try:
        with open(_AH_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                    engine.afterhours_events.append(ev)
                    count += 1
                except json.JSONDecodeError:
                    continue
        if count:
            log.info(f"[AH-persist] Loaded {count} events from disk")
    except Exception as e:
        log.debug(f"[AH-persist] load error: {e}")


# ── Trade Journal Persistence ────────────────────────────────────────────────
_TJ_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "trade_journal.jsonl")


def _tj_save_entry(entry: dict):
    """Append a single trade journal entry to the persistent JSONL file."""
    try:
        os.makedirs(os.path.dirname(_TJ_FILE), exist_ok=True)
        with open(_TJ_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception as e:
        log.debug(f"[TJ-persist] save error: {e}")


def _tj_save_all():
    """Rewrite the entire JSONL file from the current deque."""
    try:
        os.makedirs(os.path.dirname(_TJ_FILE), exist_ok=True)
        with open(_TJ_FILE, "w", encoding="utf-8") as f:
            for entry in engine.trade_journal:
                f.write(json.dumps(entry, default=str) + "\n")
    except Exception as e:
        log.debug(f"[TJ-persist] save-all error: {e}")


def _tj_load():
    """Load persisted trade journal entries on startup."""
    if not os.path.exists(_TJ_FILE):
        return
    count = 0
    try:
        with open(_TJ_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    engine.trade_journal.append(entry)
                    count += 1
                except json.JSONDecodeError:
                    continue
        if count:
            log.info(f"[TJ-persist] Loaded {count} trade journal entries from disk")
    except Exception as e:
        log.debug(f"[TJ-persist] load error: {e}")


def tj_record_trade(trade_data: dict) -> dict:
    """Record a manual trade with comprehensive market context snapshot.
    
    trade_data should contain at minimum:
        market_id, question, asset, side, price, size, order_id (if placed),
        and optionally: notes, strategy, confidence
    
    Automatically enriches with: asset price, candle data, OB snapshot,
    timing, WS state. Returns the full enriched entry.
    """
    now = datetime.now(timezone.utc)
    entry = {
        "id": f"tj_{int(time.time())}_{trade_data.get('market_id', '')[:8]}",
        "ts": time.time(),
        "ts_str": now.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),

        # ── Trade Details ──
        "market_id": trade_data.get("market_id", ""),
        "question": trade_data.get("question", ""),
        "asset": trade_data.get("asset", ""),
        "side": trade_data.get("side", ""),               # "UP", "DOWN", "BOTH"
        "action": trade_data.get("action", "BUY"),         # "BUY" or "SELL"
        "price": float(trade_data.get("price", 0)),
        "size": int(trade_data.get("size", 0)),
        "cost": float(trade_data.get("price", 0)) * int(trade_data.get("size", 0)),
        "order_id": trade_data.get("order_id", ""),
        "order_id_up": trade_data.get("order_id_up", ""),
        "order_id_down": trade_data.get("order_id_down", ""),
        "token_id": trade_data.get("token_id", ""),
        "token_id_up": trade_data.get("token_id_up", ""),
        "token_id_down": trade_data.get("token_id_down", ""),
        "end_time": trade_data.get("end_time", ""),

        # ── Manual Annotations ──
        "notes": trade_data.get("notes", ""),
        "strategy": trade_data.get("strategy", ""),        # "line_ride", "momentum", "ob_read", etc.
        "confidence": trade_data.get("confidence", ""),     # "high", "medium", "low"
        "reason": trade_data.get("reason", ""),             # Why you took this trade

        # ── Market Context (auto-captured) ──
        "secs_to_close": 0.0,
        "asset_price": 0.0,
        "candle_open": 0.0,
        "candle_high": 0.0,
        "candle_low": 0.0,
        "candle_range": 0.0,
        "price_distance": 0.0,
        "price_distance_pct": 0.0,
        "range_pct": 0.0,
        "price_vs_open": "",             # "ABOVE" or "BELOW"
        "prior_candle_range": 0.0,
        "expansion_ratio": 0.0,          # candle_range / prior_candle_range

        # ── Orderbook Snapshot (auto-captured) ──
        "up_ask": 0.0,
        "up_bid": 0.0,
        "up_ask_size": 0.0,
        "up_bid_size": 0.0,
        "down_ask": 0.0,
        "down_bid": 0.0,
        "down_ask_size": 0.0,
        "down_bid_size": 0.0,
        "combined_ask": 0.0,
        "ask_diff": 0.0,
        "ob_imbalance": 0.0,
        "ob_heavy_side": "",

        # ── Outcome (filled in later) ──
        "status": "open",               # "open", "filled", "partial", "cancelled", "resolved"
        "filled": False,
        "fill_size": 0.0,
        "fill_time": "",
        "up_filled": False,
        "down_filled": False,
        "up_fill_size": 0.0,
        "down_fill_size": 0.0,
        "winner": "pending",             # "Up", "Down", or "pending"
        "won": "pending",                # "WIN", "LOSS", "BOTH", or "pending"
        "pnl": 0.0,
        "payout": 0.0,
        "resolved_at": "",
    }

    # ── Auto-Enrich with live market data ──
    mid = trade_data.get("market_id", "")
    market = engine.watch_list.get(mid)

    if market:
        entry["secs_to_close"] = round(seconds_until(market.end_time), 1)
        entry["end_time"] = market.end_time.isoformat()

        # WS orderbook
        ws_up = engine.ws_feed.get_price(market.token_id_up)
        ws_down = engine.ws_feed.get_price(market.token_id_down)
        if ws_up and ws_up.valid:
            entry["up_ask"] = round(ws_up.best_ask, 4)
            entry["up_bid"] = round(ws_up.best_bid, 4)
            entry["up_ask_size"] = round(ws_up.best_ask_size, 1)
            entry["up_bid_size"] = round(ws_up.best_bid_size, 1)
        if ws_down and ws_down.valid:
            entry["down_ask"] = round(ws_down.best_ask, 4)
            entry["down_bid"] = round(ws_down.best_bid, 4)
            entry["down_ask_size"] = round(ws_down.best_ask_size, 1)
            entry["down_bid_size"] = round(ws_down.best_bid_size, 1)
        entry["combined_ask"] = round(entry["up_ask"] + entry["down_ask"], 4)
        entry["ask_diff"] = round(abs(entry["up_ask"] - entry["down_ask"]), 4)
        min_sz = min(entry["up_ask_size"], entry["down_ask_size"])
        max_sz = max(entry["up_ask_size"], entry["down_ask_size"])
        entry["ob_imbalance"] = round(max_sz / min_sz, 2) if min_sz > 0 else 0.0
        entry["ob_heavy_side"] = "UP" if entry["up_ask_size"] > entry["down_ask_size"] else "DOWN"

        # Token IDs (so we can check fill status later)
        entry["token_id_up"] = entry["token_id_up"] or market.token_id_up
        entry["token_id_down"] = entry["token_id_down"] or market.token_id_down

    # Asset price from Binance feed
    asset = trade_data.get("asset", "BTC")
    ast = engine.btc_feed.get(asset)
    if ast and ast.price > 0:
        entry["asset_price"] = round(ast.price, 6)
        entry["candle_open"] = round(ast.candle_open, 6)
        entry["candle_high"] = round(ast.candle_high, 6)
        entry["candle_low"] = round(ast.candle_low, 6)
        entry["candle_range"] = round(ast.candle_range, 6)
        entry["price_distance"] = round(ast.distance, 6)
        entry["price_distance_pct"] = round(ast.distance / ast.price, 8) if ast.price > 0 else 0.0
        entry["range_pct"] = round(ast.distance / ast.candle_range, 4) if ast.candle_range > 0 else 0.0
        entry["price_vs_open"] = "ABOVE" if ast.price >= ast.candle_open else "BELOW"
        if ast.prior_candle_range > 0:
            entry["prior_candle_range"] = round(ast.prior_candle_range, 6)
            entry["expansion_ratio"] = round(ast.candle_range / ast.prior_candle_range, 4) if ast.prior_candle_range > 0 else 0.0

    # Save & emit
    engine.trade_journal.appendleft(entry)
    _tj_save_entry(entry)
    try:
        socketio.emit("tj_new_entry", entry)
    except Exception:
        pass

    engine.add_log(f"JOURNAL: {entry['side']} {asset} ${entry['price']} x {entry['size']}  {entry['question'][:50]}", "trade")
    return entry


def tj_update_trade(trade_id: str, updates: dict):
    """Update an existing trade journal entry (fill status, resolution, notes)."""
    for entry in engine.trade_journal:
        if entry.get("id") == trade_id:
            entry.update(updates)
            _tj_save_all()
            try:
                socketio.emit("tj_updated", entry)
            except Exception:
                pass
            return entry
    return None


def tj_check_fills():
    """Check fill status for all open trade journal entries with order IDs."""
    for entry in engine.trade_journal:
        if entry.get("status") not in ("open",):
            continue

        changed = False

        # Check single-side orders
        if entry.get("order_id") and not entry.get("filled"):
            try:
                status = get_order_status(entry["order_id"])
                if status["status"] in ("matched", "partial"):
                    entry["filled"] = True
                    entry["fill_size"] = status["size_matched"]
                    entry["fill_time"] = datetime.now(timezone.utc).strftime("%H:%M:%S")
                    entry["status"] = "filled"
                    changed = True
                elif status["status"] == "cancelled":
                    entry["status"] = "cancelled"
                    changed = True
            except Exception:
                pass

        # Check UP/DOWN pair orders
        if entry.get("order_id_up") and not entry.get("up_filled"):
            try:
                status = get_order_status(entry["order_id_up"])
                if status["status"] in ("matched", "partial"):
                    entry["up_filled"] = True
                    entry["up_fill_size"] = status["size_matched"]
                    entry["fill_time"] = entry.get("fill_time") or datetime.now(timezone.utc).strftime("%H:%M:%S")
                    changed = True
            except Exception:
                pass

        if entry.get("order_id_down") and not entry.get("down_filled"):
            try:
                status = get_order_status(entry["order_id_down"])
                if status["status"] in ("matched", "partial"):
                    entry["down_filled"] = True
                    entry["down_fill_size"] = status["size_matched"]
                    entry["fill_time"] = entry.get("fill_time") or datetime.now(timezone.utc).strftime("%H:%M:%S")
                    changed = True
            except Exception:
                pass

        # Update status for pair orders
        if entry.get("up_filled") or entry.get("down_filled"):
            if entry.get("up_filled") and entry.get("down_filled"):
                entry["status"] = "both_filled"
            else:
                entry["status"] = "filled"
            entry["filled"] = True

        if changed:
            _tj_save_all()
            try:
                socketio.emit("tj_updated", entry)
            except Exception:
                pass


def tj_resolve_trade(trade_id: str, winner: str):
    """Resolve a trade journal entry with the market outcome."""
    for entry in engine.trade_journal:
        if entry.get("id") != trade_id:
            continue

        entry["winner"] = winner
        entry["resolved_at"] = datetime.now(timezone.utc).strftime("%H:%M:%S")

        side = entry.get("side", "")
        price = entry.get("price", 0)

        if side == "BOTH":
            # Both-side strategy
            if entry.get("up_filled") and entry.get("down_filled"):
                entry["won"] = "BOTH"
                payout = max(entry.get("up_fill_size", 0), entry.get("down_fill_size", 0)) * 1.0
                cost = price * (entry.get("up_fill_size", 0) + entry.get("down_fill_size", 0))
                entry["payout"] = round(payout, 4)
                entry["pnl"] = round(payout - cost, 4)
            elif entry.get("up_filled"):
                entry["won"] = "WIN" if winner == "Up" else "LOSS"
                fill = entry.get("up_fill_size", 0)
                entry["payout"] = round(fill * 1.0, 4) if winner == "Up" else 0.0
                entry["pnl"] = round(entry["payout"] - price * fill, 4)
            elif entry.get("down_filled"):
                entry["won"] = "WIN" if winner == "Down" else "LOSS"
                fill = entry.get("down_fill_size", 0)
                entry["payout"] = round(fill * 1.0, 4) if winner == "Down" else 0.0
                entry["pnl"] = round(entry["payout"] - price * fill, 4)
            else:
                entry["won"] = "NONE"
                entry["pnl"] = 0.0
        elif side == "UP":
            fill = entry.get("fill_size", 0) or entry.get("up_fill_size", 0)
            if fill > 0:
                entry["won"] = "WIN" if winner == "Up" else "LOSS"
                entry["payout"] = round(fill * 1.0, 4) if winner == "Up" else 0.0
                entry["pnl"] = round(entry["payout"] - price * fill, 4)
            else:
                entry["won"] = "NONE"
                entry["pnl"] = 0.0
        elif side == "DOWN":
            fill = entry.get("fill_size", 0) or entry.get("down_fill_size", 0)
            if fill > 0:
                entry["won"] = "WIN" if winner == "Down" else "LOSS"
                entry["payout"] = round(fill * 1.0, 4) if winner == "Down" else 0.0
                entry["pnl"] = round(entry["payout"] - price * fill, 4)
            else:
                entry["won"] = "NONE"
                entry["pnl"] = 0.0

        entry["status"] = "resolved"
        _tj_save_all()

        try:
            socketio.emit("tj_updated", entry)
        except Exception:
            pass

        engine.add_log(
            f"JOURNAL RESOLVED: {entry['won']} PnL=${entry['pnl']:.2f}  {entry['question'][:50]}",
            "profit" if entry["pnl"] > 0 else "error" if entry["pnl"] < 0 else "info",
        )
        return entry
    return None


def tj_get_stats() -> dict:
    """Compute comprehensive stats from the trade journal for pattern analysis."""
    trades = list(engine.trade_journal)
    total = len(trades)
    resolved = [t for t in trades if t.get("status") == "resolved"]
    wins = [t for t in resolved if t.get("won") == "WIN"]
    losses = [t for t in resolved if t.get("won") == "LOSS"]
    both = [t for t in resolved if t.get("won") == "BOTH"]
    open_trades = [t for t in trades if t.get("status") in ("open", "filled", "both_filled")]

    total_pnl = sum(t.get("pnl", 0) for t in resolved)
    total_cost = sum(t.get("cost", 0) for t in trades)
    win_pnl = sum(t.get("pnl", 0) for t in wins)
    loss_pnl = sum(t.get("pnl", 0) for t in losses)
    both_pnl = sum(t.get("pnl", 0) for t in both)

    # Per-asset breakdown
    assets = {}
    for t in resolved:
        a = t.get("asset", "?")
        if a not in assets:
            assets[a] = {"wins": 0, "losses": 0, "both": 0, "pnl": 0.0}
        if t.get("won") == "WIN":
            assets[a]["wins"] += 1
        elif t.get("won") == "LOSS":
            assets[a]["losses"] += 1
        elif t.get("won") == "BOTH":
            assets[a]["both"] += 1
        assets[a]["pnl"] += t.get("pnl", 0)

    # Per-strategy breakdown
    strategies = {}
    for t in resolved:
        s = t.get("strategy", "none") or "none"
        if s not in strategies:
            strategies[s] = {"wins": 0, "losses": 0, "pnl": 0.0}
        if t.get("won") in ("WIN", "BOTH"):
            strategies[s]["wins"] += 1
        elif t.get("won") == "LOSS":
            strategies[s]["losses"] += 1
        strategies[s]["pnl"] += t.get("pnl", 0)

    # Win patterns (what do winners have in common?)
    win_patterns = {}
    if wins or both:
        all_wins = wins + both
        win_patterns = {
            "avg_secs_to_close": round(sum(t.get("secs_to_close", 0) for t in all_wins) / len(all_wins), 1) if all_wins else 0,
            "avg_range_pct": round(sum(t.get("range_pct", 0) for t in all_wins) / len(all_wins), 4) if all_wins else 0,
            "avg_ob_imbalance": round(sum(t.get("ob_imbalance", 0) for t in all_wins) / len(all_wins), 2) if all_wins else 0,
            "avg_candle_range": round(sum(t.get("candle_range", 0) for t in all_wins) / len(all_wins), 4) if all_wins else 0,
            "avg_ask_diff": round(sum(t.get("ask_diff", 0) for t in all_wins) / len(all_wins), 4) if all_wins else 0,
            "avg_combined_ask": round(sum(t.get("combined_ask", 0) for t in all_wins) / len(all_wins), 4) if all_wins else 0,
            "price_above_pct": round(sum(1 for t in all_wins if t.get("price_vs_open") == "ABOVE") / len(all_wins) * 100, 1) if all_wins else 0,
            "price_below_pct": round(sum(1 for t in all_wins if t.get("price_vs_open") == "BELOW") / len(all_wins) * 100, 1) if all_wins else 0,
            "confidence_dist": {},
            "strategy_dist": {},
        }
        for t in all_wins:
            c = t.get("confidence", "none") or "none"
            win_patterns["confidence_dist"][c] = win_patterns["confidence_dist"].get(c, 0) + 1
            s = t.get("strategy", "none") or "none"
            win_patterns["strategy_dist"][s] = win_patterns["strategy_dist"].get(s, 0) + 1

    # Loss patterns
    loss_patterns = {}
    if losses:
        loss_patterns = {
            "avg_secs_to_close": round(sum(t.get("secs_to_close", 0) for t in losses) / len(losses), 1),
            "avg_range_pct": round(sum(t.get("range_pct", 0) for t in losses) / len(losses), 4),
            "avg_ob_imbalance": round(sum(t.get("ob_imbalance", 0) for t in losses) / len(losses), 2),
            "avg_candle_range": round(sum(t.get("candle_range", 0) for t in losses) / len(losses), 4),
            "avg_ask_diff": round(sum(t.get("ask_diff", 0) for t in losses) / len(losses), 4),
            "avg_combined_ask": round(sum(t.get("combined_ask", 0) for t in losses) / len(losses), 4),
            "price_above_pct": round(sum(1 for t in losses if t.get("price_vs_open") == "ABOVE") / len(losses) * 100, 1),
            "price_below_pct": round(sum(1 for t in losses if t.get("price_vs_open") == "BELOW") / len(losses) * 100, 1),
        }

    return {
        "total": total,
        "open": len(open_trades),
        "resolved": len(resolved),
        "wins": len(wins),
        "losses": len(losses),
        "both": len(both),
        "win_rate": round(len(wins + both) / len(resolved) * 100, 1) if resolved else 0,
        "total_pnl": round(total_pnl, 2),
        "total_cost": round(total_cost, 2),
        "win_pnl": round(win_pnl, 2),
        "loss_pnl": round(loss_pnl, 2),
        "both_pnl": round(both_pnl, 2),
        "avg_pnl": round(total_pnl / len(resolved), 2) if resolved else 0,
        "best_trade": round(max((t.get("pnl", 0) for t in resolved), default=0), 2),
        "worst_trade": round(min((t.get("pnl", 0) for t in resolved), default=0), 2),
        "assets": assets,
        "strategies": strategies,
        "win_patterns": win_patterns,
        "loss_patterns": loss_patterns,
    }


class Engine:
    """Bot engine state & control."""

    def __init__(self):
        self.running = False          # Is the main loop active?
        self.bot_thread = None
        self.watch_list: dict[str, MarketWindow] = {}
        self.bids_posted: dict[str, BidRecord] = {}
        self.bids_today: int = 0
        self.bid_date: str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.cancel_timers: dict[str, threading.Timer] = {}
        self.ws_feed: PriceFeed = PriceFeed()
        self.btc_feed: BinanceFeed = BinanceFeed()

        # Live-editable params (start from config, user can change via UI)
        self.bid_price: float = config.BID_PRICE
        self.tokens_per_side: int = config.TOKENS_PER_SIDE
        self.bid_window_open: int = 60    # seconds before close to cancel loser side
        self.bid_window_close: int = config.BID_WINDOW_CLOSE  # seconds before close to stop posting

        # Per-asset full config (UI-editable).  Initialised from ASSET_FILTERS defaults.
        # Each asset has: enabled, distance_pct_max, candle_range_max, dollar_range,
        #                  expansion_max, expansion_floor
        self.asset_config: dict[str, dict] = {}
        for a, f in ASSET_FILTERS.items():
            self.asset_config[a] = {
                "enabled": False,           # user must enable via UI
                "bid_price": f["bid_price"],
                "tokens_per_side": f.get("tokens_per_side", config.TOKENS_PER_SIDE),
                "bid_window_open": f.get("bid_window_open", config.BID_WINDOW_OPEN),
                "distance_pct_max": f["distance_pct_max"],
                "candle_range_max": f["candle_range_max"],
                "dollar_range": f["dollar_range"],
                "expansion_max": f["expansion_max"],
                "expansion_floor": f["expansion_floor"],
            }
        # BTC + ETH + SOL + XRP enabled by default (SOL=95.7% WR, XRP=100% WR)
        self.asset_config["BTC"]["enabled"] = True
        self.asset_config["ETH"]["enabled"] = True
        self.asset_config["SOL"]["enabled"] = True
        self.asset_config["XRP"]["enabled"] = True

        # Legacy accessor — btc_dollar_range still used by some log/push paths
        self.btc_dollar_range: float = self.asset_config["BTC"]["dollar_range"]

        # Orderbook depth filter — NOW ACTIVE with real thresholds
        self.ob_filter_enabled: bool = True    # Master toggle for orderbook balance filter
        self.ob_min_size: float = 50.0         # Min tokens at best ask on EACH side to allow bid (blocks thin books)
        self.ob_max_imbalance: float = 5.0     # Max ratio between sides' ask sizes (blocks lopsided books)

        # BTC price tracking (legacy — read from btc_feed directly where possible)
        self.btc_price: float = 0.0           # Current BTC price from Binance
        self.btc_candle_open: float = 0.0     # 5-min candle open price
        self.btc_distance: float = 0.0        # |current - open|
        self.btc_last_fetch: float = 0.0      # timestamp of last fetch

        # ── Trade Frequency Controls ──────────────────────────────────────
        self.min_cooldown_secs: float = 120.0     # Min 2 minutes between bids (any asset)
        self.max_bids_per_hour: int = 6           # Max 6 bids per rolling hour
        self._recent_bid_times: deque = deque(maxlen=100)  # Timestamps of recent bids

        # Activity log
        self.activity_log: deque = deque(maxlen=50)

        # P&L  (only count actually filled orders)
        self.total_spent: float = 0.0
        self.total_payout: float = 0.0
        self.daily_spend: float = 0.0         # Resets each UTC day

        # Counters
        self.markets_fired: int = 0
        self.fills_detected: int = 0
        self.errors: int = 0

        # Price cache for dashboard (market_id -> dict)
        self.market_prices: dict[str, dict] = {}

        # Markets where bids already failed — don't retry
        self.failed_markets: set[str] = set()

        # ── Auto-Trade Toggle ──────────────────────────────────────────
        # When False, bot loop still runs (monitoring, feeds, fills, cancels)
        # but will NOT auto-place new orders via post_bids().
        self.auto_trade_enabled: bool = False   # OFF by default — user trades manually

        # ── After-Hours Fill Tracker ──
        # Stores fill events that occurred close to market close (positive = pre-close,
        # negative = post-close). Cleared on bot restart; persists for the session.
        # Now also includes ObservedFill events from the PostCloseMonitor.
        self.afterhours_events: deque = deque(maxlen=2000)

        # ── Post-Close Monitor (watches ALL markets, not just ones we bid on) ──
        self.pcm: PostCloseMonitor = PostCloseMonitor()

        # ── Manual Trade Journal ─────────────────────────────────────────
        # Comprehensive tracker for manual trades — captures every data point
        # for pattern analysis of winners vs losers.
        self.trade_journal: deque = deque(maxlen=5000)

        # ── Market Scanner ──
        self.scanner_results: list[dict] = []      # List of dicts for dashboard
        self.scanner_last_scan: float = 0.0         # Timestamp of last scan
        self.scanner_running: bool = False           # Whether scanner loop is active
        self.scanner_auto_bid: bool = False          # Auto-bid on matching markets
        self.scanner_thread = None
        # Scanner filter settings (UI-editable)
        self.scanner_cfg = {
            "max_hours": 24.0,          # Max hours to expiry
            "min_liquidity": 0.0,       # Min liquidity ($)
            "max_liquidity": 999999.0,  # Max liquidity ($)
            "scan_interval": 30,        # Seconds between scans
            "categories": ["crypto-5m", "crypto-15m", "sports", "esports", "over-under", "weather", "other"],
            "auto_bid_price": 0.05,     # Price for auto-bid
            "auto_bid_size": 100,       # Tokens per side for auto-bid
            "auto_bid_max_ask": 0.15,   # Max ask price to auto-bid
            "auto_bid_min_liq": 50.0,   # Min liquidity for auto-bid
            "auto_bid_categories": [],  # Categories eligible for auto-bid (empty = none)
        }

        # ── Combined-Ask Arbitrage Engine ──
        self.arb: ArbEngine | None = None  # Initialized after ws_feed.start()

    def init_arb(self):
        """Initialize the arb engine (call after ws_feed is ready)."""
        self.arb = ArbEngine(self.ws_feed)
        self.arb.log_callback = self.add_log
        # Apply any saved settings from settings.json
        cfg = getattr(self, '_arb_saved_cfg', None)
        if cfg and isinstance(cfg, dict):
            self.arb.enabled = bool(cfg.get("enabled", self.arb.enabled))
            self.arb.min_edge = float(cfg.get("min_edge", self.arb.min_edge))
            self.arb.trade_size = float(cfg.get("trade_size", self.arb.trade_size))
            self.arb.max_positions = int(cfg.get("max_positions", self.arb.max_positions))
            self.arb.cooldown = float(cfg.get("cooldown", self.arb.cooldown))
            self.arb.fill_timeout = float(cfg.get("fill_timeout", self.arb.fill_timeout))
            self.arb.max_daily_spend = float(cfg.get("max_daily_spend", self.arb.max_daily_spend))
            log.info(f"[ARB] Restored settings: enabled={self.arb.enabled}, edge={self.arb.min_edge}, size=${self.arb.trade_size}")

    def reset_daily_counter(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self.bid_date:
            log.info(f"New day: {today}. Resetting daily bid counter (was {self.bids_today}), daily spend ${self.daily_spend:.2f}.")
            self.bids_today = 0
            self.daily_spend = 0.0
            self.bid_date = today

    def add_log(self, msg: str, level: str = "info"):
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self.activity_log.appendleft({"ts": ts, "msg": msg, "level": level})
        # Push to connected browsers
        socketio.emit("log", {"ts": ts, "msg": msg, "level": level})


engine = Engine()
_ah_load()   # Restore persisted after-hours events from disk
_tj_load()   # Restore persisted trade journal entries from disk


# ── Price Sync from Binance ──────────────────────────────────────────────────

def sync_prices_from_ws():
    """Pull latest price data from the multi-asset Binance WebSocket feed.
    BTC fields are also written to engine.btc_* for backward compat.
    Falls back to REST for BTC if WS has no data (age > 10s)."""
    bf = engine.btc_feed  # BinanceFeed — tracks all assets

    # BTC — with REST fallback (most critical asset)
    btc = bf.get("BTC")
    if btc.price > 0 and btc.price_age() < 10:
        engine.btc_price = btc.price
        engine.btc_candle_open = btc.candle_open
        engine.btc_distance = btc.distance
        engine.btc_last_fetch = time.time()
    else:
        fetch_btc_price_rest()

    # Keep btc_dollar_range in sync with asset_config
    engine.btc_dollar_range = engine.asset_config.get("BTC", {}).get("dollar_range", 2.0)


# Alias for backward compat (some code still calls sync_btc_from_ws)
sync_btc_from_ws = sync_prices_from_ws


def fetch_btc_price_rest():
    """REST fallback — fetch current BTC price and 5-min candle open from Binance."""
    try:
        now_ms = int(time.time() * 1000)
        candle_start = now_ms - (now_ms % 300_000)  # 5-min boundary
        resp = _requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": "BTCUSDT", "interval": "5m",
                    "startTime": candle_start, "limit": 1},
            timeout=5,
        )
        resp.raise_for_status()
        kline = resp.json()[0]
        candle_open = float(kline[1])
        current_price = float(kline[4])  # close = latest price in current candle

        engine.btc_candle_open = candle_open
        engine.btc_price = current_price
        engine.btc_distance = abs(current_price - candle_open)
        engine.btc_last_fetch = time.time()
    except Exception as e:
        log_error("fetch_btc_price_rest", e)


# Cache for prior candle range (avoid hammering Binance on every bid pair)
_prior_candle_cache: dict = {"range": 0.0, "ts": 0.0}


def _fetch_prior_candle_range() -> float:
    """Fetch the high-low range of the PREVIOUS 5-min BTC candle from Binance.
    Cached for 60 seconds to avoid excessive API calls."""
    if time.time() - _prior_candle_cache["ts"] < 60:
        return _prior_candle_cache["range"]
    try:
        now_ms = int(time.time() * 1000)
        candle_start = now_ms - (now_ms % 300_000)
        prev_start = candle_start - 300_000
        resp = _requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": "BTCUSDT", "interval": "5m",
                    "startTime": prev_start, "limit": 1},
            timeout=5,
        )
        resp.raise_for_status()
        kline = resp.json()[0]
        high = float(kline[2])
        low = float(kline[3])
        rng = round(high - low, 2)
        _prior_candle_cache["range"] = rng
        _prior_candle_cache["ts"] = time.time()
        return rng
    except Exception:
        return _prior_candle_cache["range"]  # return stale value on error


# ── Bot Functions ───────────────────────────────────────────────────────────

def _log_bid_eval(market: MarketWindow, secs: float):
    """Log limit-bid evaluation during the bid window so the activity log shows countdown."""
    ws_up = engine.ws_feed.get_price(market.token_id_up)
    ws_down = engine.ws_feed.get_price(market.token_id_down)
    up_ask = ws_up.best_ask if ws_up and ws_up.valid else 0
    dn_ask = ws_down.best_ask if ws_down and ws_down.valid else 0
    combined = up_ask + dn_ask

    extra = ""
    # Show Binance distance/range for the market's asset
    ast = engine.btc_feed.get(market.asset)
    if ast and ast.price > 0:
        dist_pct = (ast.distance / ast.price * 100) if ast.price > 0 else 0
        extra = (
            f"{market.asset} dist=${ast.distance:.4f} ({dist_pct:.3f}%) "
            f"range=${ast.candle_range:.4f} prior=${ast.prior_candle_range:.4f}"
        )

    # Compact market name: asset tag + time range
    _tm = re.search(r'(\d{1,2}:\d{2}[AP]M\s*-\s*\d{1,2}:\d{2}[AP]M\s*ET)', market.question)
    name = _tm.group(1) if _tm else market.question[-30:]
    asset_tag = market.asset

    engine.add_log(
        f"BID-EVAL T-{secs:.0f}s [{asset_tag}] {name} | UP=${up_ask:.3f} DN=${dn_ask:.3f} "
        f"comb=${combined:.3f}{(' | ' + extra) if extra else ''}",
        "info",
    )


def post_bids(market: MarketWindow) -> bool:
    """PHASE 1 — Queue Early: Place BOTH sides immediately to get front-of-queue.
    Only queues the NEXT upcoming market per asset (one at a time).
    The smart cancel (Phase 2) runs later at T-20s to drop the loser."""
    if market.market_id in engine.bids_posted:
        return False
    if market.market_id in engine.failed_markets:
        return False
    # Skip if arb engine already has a position on this market
    if engine.arb and market.market_id in engine.arb.positions:
        return False

    secs = seconds_until(market.end_time)

    # ── Per-Asset Enabled Check ────────────────────────────────────────────
    asset = market.asset
    ac = engine.asset_config.get(asset, {})
    if not ac.get("enabled", False):
        return False   # asset disabled by user — silently skip

    # ── ONE market per asset at a time ─────────────────────────────────────
    # Only queue the next upcoming market. If this asset already has a bid
    # whose cancel phase hasn't completed yet, skip. Once the cancel phase
    # is done (at T-20s), we immediately release the slot so the NEXT market
    # can be queued ~4+ minutes early → maximum queue priority.
    for _mid, _bid in engine.bids_posted.items():
        _mkt = engine.watch_list.get(_mid)
        if _mkt and getattr(_mkt, 'asset', '') == asset:
            if not _bid.cancel_phase_done:
                return False  # cancel phase not yet done — hold the slot

    # Don't post if market already closed
    if secs < engine.bid_window_close:
        return False

    if config.MAX_BIDS_PER_DAY > 0 and engine.bids_today >= config.MAX_BIDS_PER_DAY:
        return False

    # ── Cooldown between trades ────────────────────────────────────────
    if engine._recent_bid_times:
        last_bid_time = engine._recent_bid_times[-1]
        elapsed = time.time() - last_bid_time
        if elapsed < engine.min_cooldown_secs:
            return False  # Too soon after last bid — silently skip

    # ── Hourly rate limit ──────────────────────────────────────────────
    now_check = time.time()
    hour_ago = now_check - 3600.0
    bids_last_hour = sum(1 for t in engine._recent_bid_times if t > hour_ago)
    if bids_last_hour >= engine.max_bids_per_hour:
        return False  # Hourly cap hit — silently skip

    # ── Adaptive Learner Gate ───────────────────────────────────────────
    ws_up = engine.ws_feed.get_price(market.token_id_up)
    ws_down = engine.ws_feed.get_price(market.token_id_down)

    _ast = engine.btc_feed.get(market.asset)
    if _ast and _ast.price > 0 and _ast.candle_high > 0:
        candle_range = _ast.candle_range
        _feed_price = _ast.price
        _feed_open  = _ast.candle_open
        _feed_dist  = _ast.distance
        _prior_cr   = _ast.prior_candle_range if _ast.prior_candle_range > 0 else _fetch_prior_candle_range()
    else:
        candle_range = 0.0
        _feed_price = 0.0
        _feed_open  = 0.0
        _feed_dist  = 0.0
        _prior_cr   = 0.0

    ai_features = extract_features(
        btc_price=_feed_price,
        btc_candle_open=_feed_open,
        btc_distance=_feed_dist,
        candle_range=candle_range,
        prior_candle_range=_prior_cr,
        secs_remaining=secs,
        ws_up=ws_up,
        ws_down=ws_down,
        bid_price=engine.bid_price,
        market_id=market.market_id,
        question=market.question,
    )

    allowed, prob, reason = ai_learner.should_bid(ai_features)
    ai_learner.add_prediction(market.market_id, market.question, prob, allowed, reason)

    if not allowed:
        engine.add_log(
            f"AI SKIP: {reason}  {market.question}  [{secs:.0f}s left]",
            "warn",
        )
        return False

    # ── Orderbook Depth Filter (blocks thin/lopsided books) ────────────
    if engine.ob_filter_enabled and (engine.ob_min_size > 0 or engine.ob_max_imbalance > 0):
        _ws_up_f = engine.ws_feed.get_price(market.token_id_up)
        _ws_dn_f = engine.ws_feed.get_price(market.token_id_down)
        ob_blocked = False
        ob_reason = ""

        if _ws_up_f.valid and _ws_dn_f.valid:
            _up_sz = _ws_up_f.best_ask_size
            _dn_sz = _ws_dn_f.best_ask_size
            _min_sz = min(_up_sz, _dn_sz)
            _imbal = (max(_up_sz, _dn_sz) / min(_up_sz, _dn_sz)) if _min_sz > 0 else 999.0

            if engine.ob_min_size > 0 and _min_sz < engine.ob_min_size:
                ob_blocked = True
                ob_reason = f"thin_book (min_ask_size={_min_sz:.0f} < {engine.ob_min_size:.0f})"
            elif engine.ob_max_imbalance > 0 and _imbal > engine.ob_max_imbalance:
                ob_blocked = True
                ob_reason = f"lopsided (imbalance={_imbal:.1f}x > {engine.ob_max_imbalance:.1f}x)"
        else:
            # No WS data available — block by default (don't bid blind)
            ob_blocked = True
            ob_reason = "no_ws_data (refusing to bid blind)"

        if ob_blocked:
            engine.add_log(
                f"OB BLOCK: {ob_reason}  {market.question}  [{secs:.0f}s left]",
                "warn",
            )
            # Log the OB filter decision
            _log_ob_filter(market,
                {"reason": ob_reason, "up_ask_size": _ws_up_f.best_ask_size if _ws_up_f.valid else 0,
                 "down_ask_size": _ws_dn_f.best_ask_size if _ws_dn_f.valid else 0},
                f"BLOCKED_{ob_reason.split('(')[0].strip()}", secs)
            return False

    # ── Calculate price & tokens ──────────────────────────────────────
    ac = engine.asset_config.get(asset, {})
    asset_tokens = ac.get("tokens_per_side", engine.tokens_per_side)
    tokens = min(asset_tokens, config.HARD_MAX_TOKENS)
    asset_bid_price = ac.get("bid_price", engine.bid_price)
    price = min(asset_bid_price, config.HARD_MAX_BID_PRICE)
    log.info(
        f"[BID-PRICE] asset={asset} ac_bid_price={ac.get('bid_price','MISSING')} "
        f"global_bid={engine.bid_price} resolved={asset_bid_price} "
        f"cap={config.HARD_MAX_BID_PRICE} final={price}"
    )
    total_cost = price * tokens * 2  # always 2 sides at queue time

    if total_cost > config.MAX_RISK_PER_MARKET:
        tokens = int(config.MAX_RISK_PER_MARKET / (price * 2))
        if tokens < 10:
            engine.add_log(f"Risk cap prevents bids on {market.question}", "warn")
            return False
        total_cost = price * tokens * 2

    # Daily spend limit
    if config.MAX_DAILY_SPEND > 0 and (engine.daily_spend + total_cost) > config.MAX_DAILY_SPEND:
        engine.add_log(
            f"SKIP (daily spend ${engine.daily_spend:.2f} + ${total_cost:.2f} would exceed ${config.MAX_DAILY_SPEND:.0f} cap)  {market.question}",
            "warn",
        )
        return False

    engine.add_log(
        f"QUEUE-EARLY [{market.asset}] UP+DOWN: {market.question}  ${price} x {tokens}  "
        f"(${total_cost:.2f} max cost)  [{secs:.0f}s left]  — cancel loser at T-{engine.bid_window_open}s",
        "trade",
    )

    bid = BidRecord(market.market_id, market.question)
    bid.bid_price = price
    bid.tokens = tokens
    bid.posted_at = time.time()

    # Place BOTH sides for queue priority
    bid.order_id_up = place_limit_buy(
        token_id=market.token_id_up, price=price,
        size=tokens, market_id=market.market_id,
    )
    bid.order_id_down = place_limit_buy(
        token_id=market.token_id_down, price=price,
        size=tokens, market_id=market.market_id,
    )

    if not bid.order_id_up and not bid.order_id_down:
        err_detail = _pm.last_order_error or "unknown"
        engine.add_log(f"BID(s) FAILED: {err_detail}", "error")
        engine.errors += 1
        engine.failed_markets.add(market.market_id)
        return False

    engine.bids_posted[market.market_id] = bid
    market.fired = True
    engine.bids_today += 1
    engine.markets_fired += 1
    engine._recent_bid_times.append(time.time())  # Track for cooldown/rate-limit

    # Record features with learner for later outcome matching
    ai_learner.record_bid(market.market_id, ai_features)

    # ── Capture enrichment context at bid time ──────────────────────────
    ws_up = engine.ws_feed.get_price(market.token_id_up)
    ws_down = engine.ws_feed.get_price(market.token_id_down)
    ob_ctx = {}
    if ws_up.valid and ws_down.valid:
        ob_ctx = {
            "up_ask": ws_up.best_ask, "up_bid": ws_up.best_bid,
            "up_ask_size": ws_up.best_ask_size, "up_bid_size": ws_up.best_bid_size,
            "down_ask": ws_down.best_ask, "down_bid": ws_down.best_bid,
            "down_ask_size": ws_down.best_ask_size, "down_bid_size": ws_down.best_bid_size,
            "combined_ask": round(ws_up.best_ask + ws_down.best_ask, 4),
        }

    _bid_ast = engine.btc_feed.get(market.asset)
    _cr = _bid_ast.candle_range if (_bid_ast and _bid_ast.candle_high > 0) else 0.0
    btc_ctx = {
        "asset": market.asset,
        "btc_price": round(_bid_ast.price, 6) if _bid_ast else 0,
        "btc_candle_open": round(_bid_ast.candle_open, 6) if _bid_ast else 0,
        "btc_distance": round(_bid_ast.distance, 6) if _bid_ast else 0,
        "btc_dollar_range": engine.asset_config.get(market.asset, {}).get("dollar_range", 0),
        "btc_candle_range": round(_cr, 6),
        "btc_candle_high": round(_bid_ast.candle_high, 6) if _bid_ast else 0,
        "btc_candle_low": round(_bid_ast.candle_low, 6) if _bid_ast else 0,
        "btc_prior_candle_range": round(_bid_ast.prior_candle_range, 6) if (_bid_ast and _bid_ast.prior_candle_range > 0) else _fetch_prior_candle_range(),
    }

    meta_ctx = {
        "question": market.question,
        "end_time": market.end_time.isoformat(),
    }

    enrichment = {**ob_ctx, **btc_ctx, **meta_ctx}

    if bid.order_id_up:
        log_trade(market.market_id, "UP", market.token_id_up,
                  price, tokens, bid.order_id_up, secs, paper=config.PAPER_TRADING,
                  **enrichment)
    if bid.order_id_down:
        log_trade(market.market_id, "DOWN", market.token_id_down,
                  price, tokens, bid.order_id_down, secs, paper=config.PAPER_TRADING,
                  **enrichment)

    schedule_cancel(market)
    return True


# ── CANCEL_WINDOW: controlled by engine.bid_window_open (dashboard "Cancel At" setting) ──


def cancel_loser_side(market: MarketWindow) -> bool:
    """PHASE 2 — Cancel Late: At T-Ns (N = dashboard 'Cancel At'), read the
    PRICE FEED to determine which side is winning and cancel the loser.

    Primary signal: asset price vs candle open (above open → UP wins, below → DOWN).
    Secondary signal: WS orderbook asks (UP_ask > DN_ask → UP leaning).
    Fallback: if no price data AND no WS data → cancel BOTH (refuse blind)."""
    bid = engine.bids_posted.get(market.market_id)
    if not bid or bid.cancel_phase_done or bid.cancelled:
        return False

    secs = seconds_until(market.end_time)

    # Only run once, at T-Ns or less (N = engine.bid_window_open, dashboard "Cancel At")
    cancel_at = engine.bid_window_open
    if secs > cancel_at:
        return False

    bid.cancel_phase_done = True  # Mark done so we don't run again

    asset = market.asset

    # ── Signal 1: Price Feed (primary — most reliable) ─────────────────
    price_signal = None  # "UP", "DOWN", or None
    _ast = engine.btc_feed.get(asset)
    if _ast and _ast.price > 0 and _ast.candle_open > 0:
        dist = _ast.price - _ast.candle_open
        # If price is ABOVE candle open → market trending UP → cancel DOWN
        # If price is BELOW candle open → market trending DOWN → cancel UP
        # Dead-zone: if distance is tiny (within 0.001% of price) → uncertain
        pct = abs(dist) / _ast.price if _ast.price > 0 else 0
        if pct > 0.00001:  # any meaningful distance
            price_signal = "UP" if dist > 0 else "DOWN"
        engine.add_log(
            f"CANCEL-SIGNAL [{asset}] price={_ast.price:.2f} open={_ast.candle_open:.2f} "
            f"dist={dist:+.4f} ({pct*100:.4f}%) → signal={price_signal or 'FLAT'}  [{secs:.0f}s]",
            "info",
        )

    # ── Signal 2: WS Orderbook asks (secondary) ────────────────────────
    ws_up = engine.ws_feed.get_price(market.token_id_up)
    ws_down = engine.ws_feed.get_price(market.token_id_down)
    ob_signal = None  # "UP", "DOWN", or None
    up_ask = 0.0
    dn_ask = 0.0

    if ws_up and ws_up.valid and ws_down and ws_down.valid:
        up_ask = ws_up.best_ask
        dn_ask = ws_down.best_ask
        # Lower thresholds: even a slight lean (0.52/0.48) is meaningful
        if up_ask > dn_ask + 0.02:   # UP ask higher → market says UP wins
            ob_signal = "UP"
        elif dn_ask > up_ask + 0.02:  # DN ask higher → market says DOWN wins
            ob_signal = "DOWN"

    # ── Combine signals ────────────────────────────────────────────────
    cancel_up = False
    cancel_down = False
    decision = "KEEP_BOTH"

    if price_signal == "UP":
        # Price says UP wins → cancel DOWN
        cancel_down = True
        decision = "CANCEL_DOWN"
        src = "PRICE"
        if ob_signal == "UP":
            src = "PRICE+OB"
    elif price_signal == "DOWN":
        # Price says DOWN wins → cancel UP
        cancel_up = True
        decision = "CANCEL_UP"
        src = "PRICE"
        if ob_signal == "DOWN":
            src = "PRICE+OB"
    elif ob_signal == "UP":
        # No price signal but OB says UP
        cancel_down = True
        decision = "CANCEL_DOWN"
        src = "OB-ONLY"
    elif ob_signal == "DOWN":
        # No price signal but OB says DOWN
        cancel_up = True
        decision = "CANCEL_UP"
        src = "OB-ONLY"
    elif not _ast or _ast.price <= 0:
        # No price feed AND no WS data → truly blind, cancel both
        if not (ws_up and ws_up.valid and ws_down and ws_down.valid):
            decision = "CANCEL_BOTH_BLIND"
        else:
            # WS data exists but no lean either way → price is flat on the line
            decision = "KEEP_BOTH"
    else:
        # Price is exactly on the open line, OB is neutral → keep both
        decision = "KEEP_BOTH"

    # ── Log decision ───────────────────────────────────────────────────
    if cancel_down or cancel_up:
        engine.add_log(
            f"SMART-CANCEL [{asset}] {decision}({src}) KEEP-{'UP' if cancel_down else 'DOWN'}  "
            f"UP${up_ask:.2f} DN${dn_ask:.2f}  {market.question}  [{secs:.0f}s]",
            "trade",
        )
    elif decision == "CANCEL_BOTH_BLIND":
        engine.add_log(
            f"SMART-CANCEL [{asset}] CANCEL-BOTH (no price feed, no WS data)  "
            f"{market.question}  [{secs:.0f}s]",
            "warn",
        )
    else:
        engine.add_log(
            f"SMART-CANCEL [{asset}] KEEP-BOTH (flat/uncertain)  "
            f"UP${up_ask:.2f} DN${dn_ask:.2f}  {market.question}  [{secs:.0f}s]",
            "info",
        )

    # ── Execute cancellation ───────────────────────────────────────────
    if cancel_down and bid.order_id_down and not bid.down_filled:
        ok = cancel_order(bid.order_id_down)
        if ok:
            engine.add_log(f"  ↳ Cancelled DOWN order {bid.order_id_down[:12]}...", "cancel")
            bid.order_id_down = None
        else:
            engine.add_log(f"  ↳ Failed to cancel DOWN order", "error")

    if cancel_up and bid.order_id_up and not bid.up_filled:
        ok = cancel_order(bid.order_id_up)
        if ok:
            engine.add_log(f"  ↳ Cancelled UP order {bid.order_id_up[:12]}...", "cancel")
            bid.order_id_up = None
        else:
            engine.add_log(f"  ↳ Failed to cancel UP order", "error")

    if decision == "CANCEL_BOTH_BLIND":
        if bid.order_id_up and not bid.up_filled:
            cancel_order(bid.order_id_up)
            bid.order_id_up = None
        if bid.order_id_down and not bid.down_filled:
            cancel_order(bid.order_id_down)
            bid.order_id_down = None

    # Log OB data for analysis
    ob_detail = {}
    if ws_up and ws_up.valid and ws_down and ws_down.valid:
        ob_detail = _build_ob_detail(ws_up, ws_down, market)
    if _ast and _ast.price > 0:
        ob_detail["asset_price"] = round(_ast.price, 6)
        ob_detail["candle_open"] = round(_ast.candle_open, 6)
        ob_detail["price_distance"] = round(_ast.price - _ast.candle_open, 6)
    ob_detail["price_signal"] = price_signal
    ob_detail["ob_signal"] = ob_signal
    ob_detail["source"] = src if (cancel_down or cancel_up) else "none"
    _log_ob_filter(market, ob_detail, decision, secs)

    return True


def _build_ob_detail(ws_up, ws_down, market: MarketWindow) -> dict:
    """Build orderbook detail dict for logging."""
    detail = {
        "market_id": market.market_id,
        "question": market.question,
        "ts": time.time(),
    }
    if ws_up.valid:
        detail.update({
            "up_ask": ws_up.best_ask, "up_ask_size": ws_up.best_ask_size,
            "up_bid": ws_up.best_bid, "up_bid_size": ws_up.best_bid_size,
        })
    if ws_down.valid:
        detail.update({
            "down_ask": ws_down.best_ask, "down_ask_size": ws_down.best_ask_size,
            "down_bid": ws_down.best_bid, "down_bid_size": ws_down.best_bid_size,
        })
    if ws_up.valid and ws_down.valid:
        up_sz = ws_up.best_ask_size
        down_sz = ws_down.best_ask_size
        detail["combined_ask"] = round(ws_up.best_ask + ws_down.best_ask, 4)
        detail["ask_size_ratio"] = round(
            max(up_sz, down_sz) / min(up_sz, down_sz), 2
        ) if min(up_sz, down_sz) > 0 else 999.0
    return detail


def _log_ob_filter(market: MarketWindow, ob_detail: dict, decision: str, secs_remaining: float):
    """Log every orderbook filter decision to JSONL for pattern analysis."""
    import json as _json
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "market_id": market.market_id,
        "asset": getattr(market, 'asset', 'BTC'),
        "question": market.question,
        "decision": decision,
        "secs_remaining": round(secs_remaining, 1),
        "ob_min_size": engine.ob_min_size,
        "ob_max_imbalance": engine.ob_max_imbalance,
        "bid_price": engine.bid_price,
        "tokens_per_side": engine.tokens_per_side,
        "btc_price": engine.btc_price,
        "btc_distance": round(engine.btc_distance, 2),
        **ob_detail,
    }
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = os.path.join("logs", f"ob_filter_{today}.jsonl")
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(_json.dumps(record) + "\n")
    except Exception:
        pass  # non-critical


def _record_afterhours_fill(bid: "BidRecord", side: str, fill_size: float, market=None):
    """Capture a fill event with timing and price-proximity context for the After-Hours tab.
    Called immediately when a fill is detected on any side."""
    try:
        # Get the market window for timing & price data
        if market is None:
            market = engine.watch_list.get(bid.market_id)

        asset = getattr(market, "asset", "BTC") if market else "BTC"
        ev = AfterHoursEvent(bid.market_id, bid.question, asset)
        ev.ts_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
        ev.side = side
        ev.fill_size = fill_size
        ev.bid_price = getattr(bid, 'bid_price', 0.0)

        # Seconds relative to market close: positive = still open, negative = already closed
        if market:
            ev.secs_to_close = seconds_until(market.end_time)
        else:
            ev.secs_to_close = 0.0

        # Orderbook prices at fill detection time
        if market:
            ws_up = engine.ws_feed.get_price(market.token_id_up)
            ws_down = engine.ws_feed.get_price(market.token_id_down)
            if ws_up and ws_up.valid:
                ev.up_ask = ws_up.best_ask
            if ws_down and ws_down.valid:
                ev.down_ask = ws_down.best_ask
            ev.combined_ask = round(ev.up_ask + ev.down_ask, 4)
            ev.ask_diff = round(abs(ev.up_ask - ev.down_ask), 4)

        # Asset price vs candle open (proximity to the "line")
        ast = engine.btc_feed.get(asset)
        if ast and ast.price > 0:
            ev.asset_price = ast.price
            ev.candle_open = ast.candle_open
            ev.price_distance = ast.distance
            ev.price_distance_pct = (ast.distance / ast.price) if ast.price > 0 else 0.0
            ev.candle_range = ast.candle_range
            # range_pct = distance as fraction of candle range (universal across assets)
            ev.range_pct = (ast.distance / ast.candle_range) if ast.candle_range > 0 else 0.0
            # "On the line" = price within 25% of the candle range from the open
            ev.on_the_line = ev.range_pct < 0.25 if ast.candle_range > 0 else ev.price_distance_pct < 0.0005

        # Compute fill cost
        ev.fill_cost = ev.bid_price * ev.fill_size

        ev_d = ev.to_dict()
        engine.afterhours_events.appendleft(ev_d)
        _ah_save_event(ev_d)
        try:
            socketio.emit("ah_new_event", ev_d)
        except Exception:
            pass
    except Exception as _ah_err:
        log.debug(f"[afterhours] record error: {_ah_err}")


def _update_afterhours_resolution(market_id: str, winner: str):
    """After a market resolves, tag all AH events for that market with
    the winner and whether the filled side won or lost."""
    try:
        for ev in engine.afterhours_events:
            if ev.get("market_id") != market_id:
                continue
            if ev.get("won") == "BOTH":
                continue  # already tagged as guaranteed win
            ev["winner"] = winner
            side = ev.get("side", "")
            if side == "BOTH":
                ev["won"] = "BOTH"
            elif side == "UP":
                ev["won"] = "WIN" if winner == "Up" else "LOSS"
            elif side == "DOWN":
                ev["won"] = "WIN" if winner == "Down" else "LOSS"
            else:
                ev["won"] = "pending"
        # Caller is responsible for calling _ah_save_all() after batch
        try:
            socketio.emit("ah_resolution", {"market_id": market_id, "winner": winner})
        except Exception:
            pass
    except Exception as _e:
        log.debug(f"[afterhours] resolution update error: {_e}")


def check_fills():
    """Check all posted bids for fills and track P&L accurately."""
    for mid, bid in list(engine.bids_posted.items()):
        if bid.cancelled:
            continue

        market = engine.watch_list.get(mid)

        _new_up_fill = False
        _new_down_fill = False

        if bid.order_id_up and not bid.up_filled:
            try:
                status = get_order_status(bid.order_id_up)
                if status["status"] in ("matched", "partial"):
                    bid.up_filled = True
                    bid.up_fill_size = status["size_matched"]
                    _new_up_fill = True
                    fill_cost = bid.bid_price * bid.up_fill_size
                    engine.total_spent += fill_cost
                    engine.daily_spend += fill_cost
                    engine.fills_detected += 1
                    engine.add_log(
                        f"FILL UP: {bid.up_fill_size:.0f} sh @ ${bid.bid_price} (${fill_cost:.2f})  {bid.question}",
                        "fill",
                    )
            except Exception as e:
                log_error(f"check_fill_up {mid}", e)

        if bid.order_id_down and not bid.down_filled:
            try:
                status = get_order_status(bid.order_id_down)
                if status["status"] in ("matched", "partial"):
                    bid.down_filled = True
                    bid.down_fill_size = status["size_matched"]
                    _new_down_fill = True
                    fill_cost = bid.bid_price * bid.down_fill_size
                    engine.total_spent += fill_cost
                    engine.daily_spend += fill_cost
                    engine.fills_detected += 1
                    engine.add_log(
                        f"FILL DOWN: {bid.down_fill_size:.0f} sh @ ${bid.bid_price} (${fill_cost:.2f})  {bid.question}",
                        "fill",
                    )
            except Exception as e:
                log_error(f"check_fill_down {mid}", e)

        # ── IMMEDIATE CANCEL: When one side fills, cancel the other ──
        # This prevents adverse selection: if the loser side fills,
        # the winner side order was never going to fill anyway ($0.01
        # for a $1 token). Cancel immediately to avoid the opposite
        # side ALSO getting adversely filled.
        if _new_up_fill and not bid.down_filled and bid.order_id_down:
            try:
                ok = cancel_order(bid.order_id_down)
                if ok:
                    engine.add_log(
                        f"REACTIVE-CANCEL: UP filled → cancelled DOWN order {bid.order_id_down[:12]}…  {bid.question}",
                        "cancel",
                    )
                    bid.order_id_down = None
                    bid.cancel_phase_done = True
                else:
                    engine.add_log(
                        f"REACTIVE-CANCEL: UP filled → FAILED to cancel DOWN  {bid.question}",
                        "error",
                    )
            except Exception as e:
                log_error(f"reactive_cancel_down {mid}", e)

        if _new_down_fill and not bid.up_filled and bid.order_id_up:
            try:
                ok = cancel_order(bid.order_id_up)
                if ok:
                    engine.add_log(
                        f"REACTIVE-CANCEL: DOWN filled → cancelled UP order {bid.order_id_up[:12]}…  {bid.question}",
                        "cancel",
                    )
                    bid.order_id_up = None
                    bid.cancel_phase_done = True
                else:
                    engine.add_log(
                        f"REACTIVE-CANCEL: DOWN filled → FAILED to cancel UP  {bid.question}",
                        "error",
                    )
            except Exception as e:
                log_error(f"reactive_cancel_up {mid}", e)

        # ── After-Hours Fill Tracking ──
        # Record EVERY fill individually (UP/DOWN) as it happens.
        # When both sides fill, also add a BOTH summary row and mark
        # the individual rows with won="BOTH" (guaranteed profit).
        if _new_up_fill:
            _record_afterhours_fill(bid, "UP", bid.up_fill_size, market)
        if _new_down_fill:
            _record_afterhours_fill(bid, "DOWN", bid.down_fill_size, market)

        if bid.up_filled and bid.down_filled and not getattr(bid, '_both_logged', False):
            bid._both_logged = True
            payout = max(bid.up_fill_size, bid.down_fill_size) * 1.0
            total_cost = bid.bid_price * (bid.up_fill_size + bid.down_fill_size)
            profit = payout - total_cost
            engine.total_payout += payout
            engine.add_log(
                f"BOTH FILLED! {bid.question}  Cost: ${total_cost:.2f}  Payout: ${payout:.2f}  Profit: ${profit:.2f}",
                "profit",
            )
            _record_afterhours_fill(bid, "BOTH", max(bid.up_fill_size, bid.down_fill_size), market)
            # Mark individual UP/DOWN events for this market as won="BOTH"
            for _ev in engine.afterhours_events:
                if _ev.get("market_id") == bid.market_id and _ev.get("side") in ("UP", "DOWN"):
                    _ev["won"] = "BOTH"
                    _ev["winner"] = "BOTH"

        # Single side filled — track estimated payout (resolves to $1 if wins, $0 if loses)
        if (bid.up_filled or bid.down_filled) and not (bid.up_filled and bid.down_filled) and bid.cancelled and not getattr(bid, '_single_logged', False):
            bid._single_logged = True
            side = "UP" if bid.up_filled else "DOWN"
            fill_size = bid.up_fill_size if bid.up_filled else bid.down_fill_size
            engine.add_log(
                f"SINGLE FILL {side}: {fill_size:.0f} sh  {bid.question}  (payout pending resolution)",
                "trade",
            )


def monitor_ob_imbalance():
    """Monitor orderbook imbalance for all active (unfilled) bid pairs.
    Updates BidRecord with current imbalance state and logs snapshots.
    This is LOG-ONLY — no auto-cancel. Data collected for future analysis."""
    import json as _json
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = os.path.join("logs", f"ob_monitor_{today}.jsonl")

    for mid, bid in engine.bids_posted.items():
        if bid.cancelled:
            continue
        # Only monitor while both sides are still unfilled (that's when data matters)
        if bid.up_filled and bid.down_filled:
            continue

        market = engine.watch_list.get(mid)
        if not market:
            continue

        ws_up = engine.ws_feed.get_price(market.token_id_up)
        ws_down = engine.ws_feed.get_price(market.token_id_down)

        if not ws_up.valid or not ws_down.valid:
            continue

        up_ask_size = ws_up.best_ask_size
        down_ask_size = ws_down.best_ask_size

        # Calculate imbalance ratio
        if min(up_ask_size, down_ask_size) > 0:
            ratio = max(up_ask_size, down_ask_size) / min(up_ask_size, down_ask_size)
        else:
            ratio = 999.0

        heavy = "UP" if up_ask_size > down_ask_size else "DOWN"

        # Update bid record
        bid.ob_imbalance = ratio
        bid.ob_heavy_side = heavy
        bid.ob_up_ask_size = up_ask_size
        bid.ob_down_ask_size = down_ask_size
        bid.ob_snapshots += 1

        # Log snapshot (every call = every ~1-2 seconds)
        secs = seconds_until(market.end_time)
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "market_id": mid,
            "secs_remaining": round(secs, 1),
            "secs_since_post": round(time.time() - bid.posted_at, 1),
            "up_ask": ws_up.best_ask, "up_ask_size": up_ask_size,
            "up_bid": ws_up.best_bid, "up_bid_size": ws_up.best_bid_size,
            "down_ask": ws_down.best_ask, "down_ask_size": down_ask_size,
            "down_bid": ws_down.best_bid, "down_bid_size": ws_down.best_bid_size,
            "imbalance": round(ratio, 2),
            "heavy_side": heavy,
            "up_filled": bid.up_filled, "down_filled": bid.down_filled,
            "btc_price": engine.btc_price,
            "btc_distance": round(engine.btc_distance, 2),
            "snapshot_num": bid.ob_snapshots,
        }
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(_json.dumps(record) + "\n")
        except Exception:
            pass  # non-critical


def schedule_cancel(market: MarketWindow):
    """Post-close handler: NO cancellation of unfilled orders.
    Only marks the bid as done and schedules resolution check for fills."""
    secs_until_close = seconds_until(market.end_time) + config.CANCEL_DELAY_AFTER_CLOSE
    if secs_until_close < 0:
        secs_until_close = 5

    def do_post_close():
        bid = engine.bids_posted.get(market.market_id)
        if not bid or bid.cancelled:
            return
        bid.cancelled = True  # mark as processed (no new actions)

        fills = []
        if bid.up_filled:
            fills.append(f"UP={bid.up_fill_size:.0f}")
        if bid.down_filled:
            fills.append(f"DOWN={bid.down_fill_size:.0f}")
        fill_str = ", ".join(fills) if fills else "NONE"
        unfilled = []
        if bid.order_id_up and not bid.up_filled:
            unfilled.append("UP")
        if bid.order_id_down and not bid.down_filled:
            unfilled.append("DOWN")
        unfilled_str = ", ".join(unfilled) if unfilled else "NONE"
        engine.add_log(
            f"Market closed | Fills: {fill_str} | Unfilled kept: {unfilled_str} | {market.question}",
            "cancel",
        )

        # Schedule resolution check for filled orders
        if bid.up_filled or bid.down_filled:
            _schedule_resolution_check(market.market_id, bid)

    timer = threading.Timer(secs_until_close, do_post_close)
    timer.daemon = True
    timer.start()
    engine.cancel_timers[market.market_id] = timer


def _schedule_resolution_check(market_id: str, bid: BidRecord,
                               delay: float = 60.0, retries: int = 3):
    """Check market resolution after a delay. Retries up to `retries` times
    with increasing delays if the market hasn't resolved yet."""

    def do_check(attempt: int = 1):
        try:
            result = check_market_resolution(market_id)
            if result["resolved"]:
                winner = result["winner"]
                # Calculate P&L for this market
                pnl = 0.0
                if bid.up_filled and bid.down_filled:
                    # Both filled — guaranteed win
                    payout = max(bid.up_fill_size, bid.down_fill_size) * 1.0
                    cost = bid.bid_price * (bid.up_fill_size + bid.down_fill_size)
                    pnl = payout - cost
                elif bid.up_filled:
                    cost = bid.bid_price * bid.up_fill_size
                    if winner == "Up":
                        pnl = (bid.up_fill_size * 1.0) - cost
                        engine.total_payout += bid.up_fill_size * 1.0
                    else:
                        pnl = -cost
                elif bid.down_filled:
                    cost = bid.bid_price * bid.down_fill_size
                    if winner == "Down":
                        pnl = (bid.down_fill_size * 1.0) - cost
                        engine.total_payout += bid.down_fill_size * 1.0
                    else:
                        pnl = -cost

                log_resolution(
                    market_id=market_id,
                    question=bid.question,
                    winner_side=winner,
                    up_filled=bid.up_filled,
                    down_filled=bid.down_filled,
                    up_fill_size=bid.up_fill_size,
                    down_fill_size=bid.down_fill_size,
                    bid_price=bid.bid_price,
                    pnl=pnl,
                )
                result_icon = "WIN" if pnl > 0 else "LOSS"
                engine.add_log(
                    f"RESOLVED {result_icon}: {bid.question}  winner={winner}  P&L=${pnl:.2f}",
                    "profit" if pnl > 0 else "error",
                )

                # Feed outcome to adaptive learner
                ai_learner.record_outcome(
                    market_id=market_id,
                    both_filled=bid.up_filled and bid.down_filled,
                    up_filled=bid.up_filled,
                    down_filled=bid.down_filled,
                    pnl=pnl,
                    winner_side=winner,
                )

                # ── Update After-Hours events with resolution outcome ──
                _update_afterhours_resolution(market_id, winner)
                _ah_save_all()
            elif attempt < retries:
                # Not resolved yet — retry with longer delay
                t = threading.Timer(delay * attempt, lambda: do_check(attempt + 1))
                t.daemon = True
                t.start()
            else:
                log.debug(f"Resolution check timed out after {retries} attempts: {market_id}")
        except Exception as e:
            log_error(f"resolution_check {market_id}", e)

    t = threading.Timer(delay, lambda: do_check(1))
    t.daemon = True
    t.start()


def refresh_watch_list():
    """Discover new 5-min Up/Down markets (all assets) and prune expired ones."""
    markets = fetch_active_markets()       # BTC + ETH + SOL + XRP + …
    now = datetime.now(timezone.utc)
    added = 0
    asset_added: dict[str, int] = {}

    for m in markets:
        if m.end_time > now + timedelta(seconds=engine.bid_window_close):
            if m.market_id not in engine.watch_list:
                engine.watch_list[m.market_id] = m
                engine.ws_feed.subscribe(m.token_id_up, m.token_id_down, m.market_id)
                data_rec.record_market(
                    market_id=m.market_id, question=m.question,
                    token_id_up=m.token_id_up, token_id_down=m.token_id_down,
                    end_time=m.end_time,
                )
                added += 1
                asset_added[m.asset] = asset_added.get(m.asset, 0) + 1

    expired = [
        mid for mid, mkt in engine.watch_list.items()
        if seconds_until(mkt.end_time) < -config.CANCEL_DELAY_AFTER_CLOSE - 30
    ]
    for mid in expired:
        mkt = engine.watch_list[mid]
        engine.ws_feed.unsubscribe(mkt.token_id_up, mkt.token_id_down)
        del engine.watch_list[mid]
        engine.market_prices.pop(mid, None)
        engine.failed_markets.discard(mid)

    if added or expired:
        assets_str = ", ".join(f"{a}:{n}" for a, n in sorted(asset_added.items())) if asset_added else ""
        engine.add_log(
            f"Markets: +{added} new ({assets_str}), -{len(expired)} expired, "
            f"{len(engine.watch_list)} active",
            "info",
        )


def update_prices():
    """Pull latest prices from WS feed (or HTTP fallback) into cache."""
    for mid, mkt in engine.watch_list.items():
        secs = seconds_until(mkt.end_time)
        ws_up = engine.ws_feed.get_price(mkt.token_id_up)
        ws_down = engine.ws_feed.get_price(mkt.token_id_down)

        if ws_up.valid and ws_down.valid:
            bid = engine.bids_posted.get(mid)
            # Determine status
            if bid and bid.up_filled and bid.down_filled:
                status = "both_filled"
            elif bid and (bid.up_filled or bid.down_filled):
                status = "partial_fill"
            elif bid:
                status = "bids_live"
            elif engine.bid_window_close <= secs <= engine.bid_window_open:
                status = "in_range"
            elif secs < 0:
                status = "expired"
            else:
                status = "watching"

            # Extract compact name with asset tag
            _tm = re.search(r'(\d{1,2}:\d{2}[AP]M\s*-\s*\d{1,2}:\d{2}[AP]M\s*ET)', mkt.question)
            time_part = _tm.group(1) if _tm else (mkt.question[-24:] if len(mkt.question) > 24 else mkt.question)
            name = f"[{mkt.asset}] {time_part}"

            engine.market_prices[mid] = {
                "market_id": mid,
                "name": name,
                "asset": mkt.asset,
                "secs": secs,
                "up_ask": ws_up.best_ask,
                "down_ask": ws_down.best_ask,
                "combined": ws_up.best_ask + ws_down.best_ask,
                "up_bid": ws_up.best_bid,
                "down_bid": ws_down.best_bid,
                "up_ask_size": ws_up.best_ask_size,
                "down_ask_size": ws_down.best_ask_size,
                "status": status,
                "bid_price": bid.bid_price if bid else 0,
                "up_filled": bid.up_filled if bid else False,
                "down_filled": bid.down_filled if bid else False,
                "price_age": time.time() - max(ws_up.timestamp, ws_down.timestamp),
            }


def bot_loop():
    """Main bot loop — runs in background thread."""
    engine.add_log("Bot engine starting...", "info")

    # Validate config
    errors = config.validate_config()
    if errors and not config.PAPER_TRADING:
        for e in errors:
            engine.add_log(f"Config error: {e}", "error")
        engine.running = False
        return

    # Init CLOB client
    try:
        client = get_clob_client()
        engine.add_log("CLOB client connected", "info")
    except Exception as e:
        engine.add_log(f"CLOB client failed: {e}", "error")
        if not config.PAPER_TRADING:
            engine.running = False
            return

    # Verify API auth (live mode)
    if not config.PAPER_TRADING:
        try:
            orders = client.get_orders()
            cnt = len(orders) if orders else 0
            engine.add_log(f"API auth OK -- {cnt} existing open orders", "info")
        except Exception as e:
            engine.add_log(f"API auth FAILED: {e}", "error")
            engine.running = False
            return

    # Start subsystems
    data_rec.start()
    engine.ws_feed.start()
    engine.btc_feed.start()
    engine.init_arb()
    engine.add_log("WebSocket feed + Binance WS + data recorder + ARB engine started", "info")

    # Bootstrap adaptive learner from historical trade logs
    try:
        n = ai_learner.bootstrap_from_logs()
        engine.add_log(f"AI learner bootstrapped from {n} historical trades", "info")
    except Exception as e:
        engine.add_log(f"AI learner bootstrap failed: {e}", "error")

    mode = "PAPER" if config.PAPER_TRADING else "LIVE"
    engine.add_log(
        f"Bot RUNNING in {mode} mode  |  "
        f"${engine.bid_price} x {engine.tokens_per_side}/side",
        "info",
    )

    last_market_refresh = 0
    last_fill_check = 0
    last_ob_monitor = 0
    last_heartbeat = 0
    last_push = 0
    last_btc_fetch = 0

    while engine.running:
        try:
            now_ts = time.time()
            engine.reset_daily_counter()

            # Sync BTC price from WebSocket (updates every ~100ms)
            # Only sync into engine state every 0.3s to avoid overhead
            if now_ts - last_btc_fetch >= 0.3:
                sync_btc_from_ws()
                last_btc_fetch = now_ts

            # Refresh markets
            if now_ts - last_market_refresh >= config.MARKET_POLL_INTERVAL:
                refresh_watch_list()
                last_market_refresh = now_ts

            # Post limit bids on both sides of markets in window
            if config.TRADING_ENABLED:
                _bid_log_due = (now_ts - getattr(engine, '_last_bid_eval_log', 0)) >= 2.0
                # Debug: log closest market per asset every 10s
                if (now_ts - getattr(engine, '_last_bid_debug', 0)) >= 10.0 and engine.watch_list:
                    by_asset: dict[str, tuple[float, str]] = {}
                    for _mid, _m in engine.watch_list.items():
                        _s = seconds_until(_m.end_time)
                        a = getattr(_m, 'asset', 'BTC')
                        if a not in by_asset or abs(_s) < abs(by_asset[a][0]):
                            by_asset[a] = (_s, _m.question[:30])
                    parts = " | ".join(f"{a}:{s:.0f}s" for a, (s, _) in sorted(by_asset.items()))
                    log.debug(f"BID-DEBUG closest: {parts}  watch={len(engine.watch_list)} posted={len(engine.bids_posted)}")
                    engine._last_bid_debug = now_ts
                # Debug: log near-window markets every 5s
                if (now_ts - getattr(engine, '_last_bid_iter_debug', 0)) >= 5.0:
                    near = [(f"{getattr(m, 'asset', '?')}:{mid[:6]}", round(seconds_until(m.end_time), 1))
                            for mid, m in engine.watch_list.items()
                            if -10 < seconds_until(m.end_time) < 30]
                    if near:
                        log.debug(f"BID-ITER near-window: {near}")
                    engine._last_bid_iter_debug = now_ts
                # Sort by soonest-ending first so each asset queues
                # the next upcoming market (not a far-future one)
                _sorted_markets = sorted(
                    engine.watch_list.items(),
                    key=lambda kv: kv[1].end_time,
                )
                for market_id, market in _sorted_markets:
                    if not engine.running:
                        break
                    secs = seconds_until(market.end_time)

                    # ── PHASE 2: Cancel Late — cancel loser at T-Ns ──
                    # Check FIRST so posted markets get their cancel phase
                    if market_id in engine.bids_posted:
                        bid_rec = engine.bids_posted[market_id]
                        if not bid_rec.cancel_phase_done and secs <= engine.bid_window_open:
                            cancel_loser_side(market)
                        continue  # already posted — skip bid placement

                    # Activity log: show evaluation countdown during bid window
                    _ac = engine.asset_config.get(market.asset, {})
                    _asset_wo = _ac.get("bid_window_open", engine.bid_window_open)
                    in_eval = engine.bid_window_close <= secs <= _asset_wo + 5
                    if in_eval and _bid_log_due:
                        try:
                            _log_bid_eval(market, secs)
                        except Exception as eval_err:
                            log.error(f"BID-EVAL error [{market.asset}]: {eval_err}")
                        engine._last_bid_eval_log = now_ts

                    # ── PHASE 1: Queue Early — place BOTH sides ASAP ──
                    if secs > engine.bid_window_close and engine.auto_trade_enabled:
                        post_bids(market)

            # Check fills
            if now_ts - last_fill_check >= 3.0:
                check_fills()
                last_fill_check = now_ts

            # ── Combined-Ask Arbitrage scan ─────────────────────────────
            if engine.arb and engine.arb.enabled:
                for _arb_mid, _arb_mkt in engine.watch_list.items():
                    if not engine.running:
                        break
                    # Skip markets we already have low-ball bids on
                    if _arb_mid in engine.bids_posted:
                        continue
                    engine.arb.scan_opportunity(_arb_mkt)
                # Check arb fills & cancel stale
                engine.arb.check_fills()
                engine.arb.cancel_stale()

            # Monitor OB imbalance for active bids (log-only, every 2s)
            if now_ts - last_ob_monitor >= 2.0 and engine.bids_posted:
                monitor_ob_imbalance()
                last_ob_monitor = now_ts

            # Update price cache
            update_prices()

            # Push full state to browser every 1.5s (prices covered by 200ms tick)
            if now_ts - last_push >= 1.5:
                push_state()
                last_push = now_ts

            # Record ticks
            for mid, mkt in engine.watch_list.items():
                secs = seconds_until(mkt.end_time)
                if secs <= 0 or secs > 300:
                    continue
                ws_up = engine.ws_feed.get_price(mkt.token_id_up)
                ws_down = engine.ws_feed.get_price(mkt.token_id_down)
                if ws_up.valid and ws_down.valid:
                    data_rec.record_tick(
                        market_id=mkt.market_id, market_name=mkt.question,
                        secs_remaining=secs,
                        up_ask=ws_up.best_ask, up_ask_size=ws_up.best_ask_size,
                        up_bid=ws_up.best_bid, up_bid_size=ws_up.best_bid_size,
                        down_ask=ws_down.best_ask, down_ask_size=ws_down.best_ask_size,
                        down_bid=ws_down.best_bid, down_bid_size=ws_down.best_bid_size,
                        source="ws", fired=mkt.market_id in engine.bids_posted,
                    )

            data_rec.flush()

            # ── Subsystem watchdog: restart anything that died ──
            # WS price feed — thread exited (_running=False) means it fully crashed
            ws_dead = not engine.ws_feed._running
            # Also treat "running but disconnected for >60s" as dead
            if engine.ws_feed._running and not engine.ws_feed.connected:
                ws_discon = time.time() - getattr(engine, '_ws_discon_since', 0)
                if not hasattr(engine, '_ws_discon_since') or engine._ws_discon_since == 0:
                    engine._ws_discon_since = time.time()
                elif ws_discon > 60:
                    ws_dead = True
            else:
                engine._ws_discon_since = 0

            if ws_dead:
                ws_age = time.time() - getattr(engine, '_last_ws_restart', 0)
                if ws_age > 30:
                    engine._last_ws_restart = time.time()
                    engine._ws_discon_since = 0
                    engine.add_log("Watchdog: restarting dead WS price feed", "warn")
                    try:
                        engine.ws_feed.stop()
                    except Exception:
                        pass
                    engine.ws_feed._running = False
                    engine.ws_feed.start()

            # Binance WS feed — same logic
            btc_dead = not engine.btc_feed._running
            if engine.btc_feed._running and not engine.btc_feed.connected:
                btc_discon = time.time() - getattr(engine, '_btc_discon_since', 0)
                if not hasattr(engine, '_btc_discon_since') or engine._btc_discon_since == 0:
                    engine._btc_discon_since = time.time()
                elif btc_discon > 60:
                    btc_dead = True
            else:
                engine._btc_discon_since = 0

            if btc_dead:
                btc_age = time.time() - getattr(engine, '_last_btc_restart', 0)
                if btc_age > 30:
                    engine._last_btc_restart = time.time()
                    engine._btc_discon_since = 0
                    engine.add_log("Watchdog: restarting dead Binance feed", "warn")
                    try:
                        engine.btc_feed.stop()
                    except Exception:
                        pass
                    engine.btc_feed._running = False
                    engine.btc_feed.start()

            # Heartbeat
            if now_ts - last_heartbeat >= 300:
                log_heartbeat(
                    active_markets=len(engine.watch_list),
                    fired_today=engine.bids_today,
                    paper=config.PAPER_TRADING,
                )
                last_heartbeat = now_ts

            # Sleep
            has_active = any(
                engine.bid_window_close <= seconds_until(mkt.end_time) <= engine.bid_window_open
                for mkt in engine.watch_list.values()
            )
            time.sleep(0.5 if has_active else 1.0)

        except Exception as e:
            log_error("bot_loop", e)
            engine.add_log(f"Error: {e}", "error")
            engine.errors += 1
            time.sleep(5)

    # Cleanup
    engine.add_log("Bot stopping...", "info")
    if engine.arb:
        engine.arb.shutdown()
    for timer in engine.cancel_timers.values():
        timer.cancel()
    for mid, bid in engine.bids_posted.items():
        if not bid.cancelled:
            for oid in [bid.order_id_up, bid.order_id_down]:
                if oid:
                    cancel_order(oid)
                    log_cancel(mid, oid, "shutdown")
    # NOTE: ws_feed and btc_feed stay alive for PCM background monitoring
    data_rec.flush()
    engine.add_log("Bot stopped", "info")
    push_state()


def _ah_resolve_pending():
    """Background sweep: find AH events still 'pending' and retry resolution.
    Called periodically from pcm_background_loop to catch markets that took
    longer to resolve than the initial 4×30s window."""
    try:
        # Collect unique market_ids that still have pending events
        pending_mids = set()
        for ev in engine.afterhours_events:
            if ev.get("won") == "pending" and ev.get("market_id"):
                pending_mids.add(ev["market_id"])

        if not pending_mids:
            return

        resolved_count = 0
        for mid in pending_mids:
            try:
                result = check_market_resolution(mid)
                if result["resolved"]:
                    winner = result["winner"]
                    _update_afterhours_resolution(mid, winner)
                    resolved_count += 1
                    log.info(f"[PCM-resolve] Resolved pending market {mid[:12]}… → {winner}")
            except Exception:
                pass
            time.sleep(0.5)  # Rate limit API calls

        if resolved_count > 0:
            _ah_save_all()   # single rewrite for all resolutions in this sweep
            log.info(f"[PCM-resolve] Resolved {resolved_count}/{len(pending_mids)} pending markets")
    except Exception as e:
        log.error(f"[PCM-resolve] error: {e}")


def pcm_background_loop():
    """Always-on background loop that monitors ALL crypto 5m markets for
    post-close fill activity.  Runs independently of the bot start/stop state
    so we always collect data, even when the bot isn't trading."""
    log.info("[PCM] Background monitor starting — will track ALL market fills")

    # Start feeds if not already running (idempotent)
    engine.ws_feed.start()
    engine.btc_feed.start()
    data_rec.start()
    log.info("[PCM] Feeds started (WS + Binance + data recorder)")

    last_refresh = 0
    last_resolve_sweep = 0
    REFRESH_INTERVAL = 30  # seconds between market discovery polls
    RESOLVE_INTERVAL = 120  # seconds between pending-resolution sweeps

    while True:
        try:
            now_ts = time.time()

            # Refresh watch_list so we always know about all active markets
            if now_ts - last_refresh >= REFRESH_INTERVAL:
                refresh_watch_list()
                last_refresh = now_ts

            # Periodically retry resolution for pending events
            if now_ts - last_resolve_sweep >= RESOLVE_INTERVAL:
                _ah_resolve_pending()
                last_resolve_sweep = now_ts

            # Check all markets for approaching close → trigger PCM
            for _pcm_mid, _pcm_mkt in list(engine.watch_list.items()):
                _pcm_secs = seconds_until(_pcm_mkt.end_time)
                if engine.pcm.should_monitor(_pcm_mid, _pcm_secs):
                    engine.pcm.start_monitoring(_pcm_mkt)

            time.sleep(1.0)

        except Exception as e:
            log.error(f"[PCM] background loop error: {e}")
            time.sleep(5)


def _price_tick_loop():
    """Fast per-asset price push — emits lightweight price_tick every 200ms.
    Runs in its own daemon thread so the Move display updates in near real-time
    even when the full push_state() cycle is slower."""
    from binance_ws import ASSETS
    while True:
        try:
            bf = engine.btc_feed
            if bf:
                payload = {}
                for asset_key in ASSETS:
                    ast = bf.get(asset_key)
                    if ast and ast.price > 0:
                        dec = 2 if ast.price > 10 else 4 if ast.price > 0.1 else 6
                        payload[asset_key] = {
                            "p": round(ast.price, dec),
                            "o": round(ast.candle_open, dec),
                            "d": round(ast.distance, dec),
                            "r": round(ast.candle_range, dec),
                            "h": round(ast.candle_high, dec),
                            "l": round(ast.candle_low, dec),
                        }
                if payload:
                    payload["ws"] = bf.connected
                    payload["age"] = round(bf.price_age(), 1) if bf.price_age() >= 0 else -1
                    socketio.emit("price_tick", payload)
        except Exception:
            pass
        time.sleep(0.2)

# Keep legacy name for backward compat
_btc_tick_loop = _price_tick_loop

# Start the fast price tick thread immediately (daemon — dies with app)
_price_tick_thread = threading.Thread(target=_price_tick_loop, daemon=True, name="price-tick")
_price_tick_thread.start()


def push_state():
    """Push full engine state to all connected browsers."""
    ws_stats = engine.ws_feed.stats()
    btc_stats = engine.btc_feed.stats()
    now = datetime.now(timezone.utc)

    # Sort markets by time remaining
    markets = sorted(engine.market_prices.values(), key=lambda m: m.get("secs", 9999))

    # ALL watch_list markets for dropdown (even without WS data)
    all_markets = []
    for mid, mkt in engine.watch_list.items():
        secs = seconds_until(mkt.end_time)
        _tm = re.search(r'(\d{1,2}:\d{2}[AP]M\s*-\s*\d{1,2}:\d{2}[AP]M\s*ET)', mkt.question)
        time_part = _tm.group(1) if _tm else (mkt.question[-24:] if len(mkt.question) > 24 else mkt.question)
        all_markets.append({
            "market_id": mid,
            "name": f"[{mkt.asset}] {time_part}",
            "asset": mkt.asset,
            "secs": secs,
        })
    all_markets.sort(key=lambda m: m["secs"])

    # Bids summary
    bids_list = []
    for bid in engine.bids_posted.values():
        bids_list.append(bid.to_dict())

    data = {
        "running": engine.running,
        "mode": "PAPER" if config.PAPER_TRADING else "LIVE",
        "bid_price": engine.bid_price,
        "tokens_per_side": engine.tokens_per_side,
        "trading_enabled": config.TRADING_ENABLED,
        "markets": markets,
        "bids": bids_list,
        "bids_today": engine.bids_today,
        "markets_watched": len(engine.watch_list),
        "markets_fired": engine.markets_fired,
        "fills_detected": engine.fills_detected,
        "total_spent": round(engine.total_spent, 2),
        "total_payout": round(engine.total_payout, 2),
        "total_profit": round(engine.total_payout - engine.total_spent, 2),
        "daily_spend": round(engine.daily_spend, 2),
        "max_daily_spend": config.MAX_DAILY_SPEND,
        "errors": engine.errors,
        "ws_connected": ws_stats["connected"],
        "ws_messages": ws_stats["messages"],
        "ws_valid": ws_stats.get("valid_tokens", 0),
        "ws_active": ws_stats.get("active_tokens", 0),
        "ws_stale": ws_stats.get("stale_tokens", 0),
        "ws_idle": ws_stats.get("idle_tokens", 0),
        "ws_never": ws_stats.get("never_received", 0),
        "ws_subscribed": ws_stats.get("subscribed_tokens", 0),
        "btc_ws_connected": btc_stats["connected"],
        "btc_ws_age": round(btc_stats["price_age"], 1) if btc_stats["price_age"] >= 0 else -1,
        "activity": list(engine.activity_log)[:30],
        "time_utc": now.strftime("%H:%M:%S UTC"),
        "bid_window_open": engine.bid_window_open,
        "bid_window_close": engine.bid_window_close,
        "btc_dollar_range": engine.btc_dollar_range,
        "btc_price": round(engine.btc_price, 2),
        "btc_candle_open": round(engine.btc_candle_open, 2),
        "btc_distance": round(engine.btc_distance, 2),
        "asset_config": {a: dict(c) for a, c in engine.asset_config.items()},
        "max_risk": config.MAX_RISK_PER_MARKET,
        "paper": config.PAPER_TRADING,
        "ob_filter_enabled": engine.ob_filter_enabled,
        "ob_min_size": engine.ob_min_size,
        "ob_max_imbalance": engine.ob_max_imbalance,
        "learner": ai_learner.stats(),
        "learner_predictions": ai_learner.get_recent_predictions(),
        "scanner_results": engine.scanner_results,
        "scanner_running": engine.scanner_running,
        "scanner_auto_bid": engine.scanner_auto_bid,
        "scanner_cfg": dict(engine.scanner_cfg),
        "scanner_last_scan": engine.scanner_last_scan,
        "ah_count": len(engine.afterhours_events),
        "auto_trade_enabled": engine.auto_trade_enabled,
        "tj_count": len(engine.trade_journal),
        "tj_entries": list(engine.trade_journal)[:200],
        "all_markets": all_markets,
        "arb": engine.arb.stats() if engine.arb else {},
        "arb_positions": engine.arb.get_positions_list() if engine.arb else [],
    }
    socketio.emit("state", data)


# ══════════════════════════════════════════════════════════════════════════════
#  FLASK ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template_string(
        DASHBOARD_HTML,
        sv_bid_price=engine.bid_price,
        sv_tokens=engine.tokens_per_side,
        sv_window_open=engine.bid_window_open,
        sv_window_close=engine.bid_window_close,
        sv_dollar_range=engine.btc_dollar_range,
        sv_asset_config={a: dict(c) for a, c in engine.asset_config.items()},
        sv_ob_enabled=engine.ob_filter_enabled,
        sv_ob_min_size=engine.ob_min_size,
        sv_ob_max_imbalance=engine.ob_max_imbalance,
        sv_running=engine.running,
        sv_scanner_cfg=dict(engine.scanner_cfg),
        sv_scanner_auto_bid=engine.scanner_auto_bid,
    )


@app.route("/export-trades")
def export_trades():
    """Export all trade data as CSV — joins orders + resolutions by market_id."""
    from logger import LOG_DIR
    log_files = sorted(glob.glob(os.path.join(LOG_DIR, "trades_*.jsonl")))

    # Collect all records grouped by market_id
    markets = {}   # market_id -> { "up": {...}, "down": {...}, "resolution": {...}, "cancels": [...] }
    all_records = []
    for fpath in log_files:
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        all_records.append(rec)
                    except json.JSONDecodeError:
                        continue
        except Exception:
            continue

    for rec in all_records:
        mid = rec.get("market_id", "")
        if not mid:
            continue
        if mid not in markets:
            markets[mid] = {"up": None, "down": None, "resolution": None, "cancels": []}
        rtype = rec.get("type", "")
        if rtype == "order_placed":
            side = rec.get("side", "").upper()
            if side == "UP":
                markets[mid]["up"] = rec
            elif side == "DOWN":
                markets[mid]["down"] = rec
        elif rtype == "resolution":
            markets[mid]["resolution"] = rec
        elif rtype == "cancel":
            markets[mid]["cancels"].append(rec)

    # Build CSV rows
    output = io.StringIO()
    fieldnames = [
        "timestamp", "asset", "question", "market_id", "end_time",
        "bid_price", "tokens_per_side", "secs_remaining",
        "order_id_up", "order_id_down", "paper",
        "asset_price", "candle_open", "candle_high", "candle_low",
        "candle_range", "prior_candle_range", "distance", "dollar_range",
        "winner_side", "up_filled", "down_filled",
        "up_fill_size", "down_fill_size", "pnl",
        "cancelled_up", "cancelled_down", "cancel_reason",
        "resolution_time",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()

    for mid, data in sorted(markets.items(), key=lambda x: (x[1].get("up") or x[1].get("down") or {}).get("timestamp", "")):
        up = data["up"] or {}
        down = data["down"] or {}
        res = data["resolution"] or {}
        cancels = data["cancels"]

        # Determine cancel state
        cancel_ids = {c.get("order_id") for c in cancels}
        cancelled_up = up.get("order_id", "") in cancel_ids if up else False
        cancelled_down = down.get("order_id", "") in cancel_ids if down else False
        cancel_reason = cancels[0].get("reason", "") if cancels else ""

        # Use UP order as primary source (has enrichment data), fall back to DOWN
        primary = up or down

        row = {
            "timestamp": primary.get("timestamp", ""),
            "asset": primary.get("asset", ""),
            "question": primary.get("question", res.get("question", "")),
            "market_id": mid,
            "end_time": primary.get("end_time", ""),
            "bid_price": primary.get("price", res.get("bid_price", "")),
            "tokens_per_side": primary.get("size", ""),
            "secs_remaining": primary.get("secs_remaining", ""),
            "order_id_up": up.get("order_id", ""),
            "order_id_down": down.get("order_id", ""),
            "paper": primary.get("paper", ""),
            "asset_price": primary.get("btc_price", ""),
            "candle_open": primary.get("btc_candle_open", ""),
            "candle_high": primary.get("btc_candle_high", ""),
            "candle_low": primary.get("btc_candle_low", ""),
            "candle_range": primary.get("btc_candle_range", ""),
            "prior_candle_range": primary.get("btc_prior_candle_range", ""),
            "distance": primary.get("btc_distance", ""),
            "dollar_range": primary.get("btc_dollar_range", ""),
            "winner_side": res.get("winner_side", ""),
            "up_filled": res.get("up_filled", ""),
            "down_filled": res.get("down_filled", ""),
            "up_fill_size": res.get("up_fill_size", ""),
            "down_fill_size": res.get("down_fill_size", ""),
            "pnl": res.get("pnl", ""),
            "cancelled_up": cancelled_up,
            "cancelled_down": cancelled_down,
            "cancel_reason": cancel_reason,
            "resolution_time": res.get("timestamp", ""),
        }
        writer.writerow(row)

    csv_data = output.getvalue()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=polybot_trades_{today}.csv"},
    )


@app.route("/export-afterhours")
def export_afterhours():
    """Export all PCM observed fills as CSV for pattern analysis."""
    events = list(engine.afterhours_events)

    output = io.StringIO()
    fieldnames = [
        "timestamp", "asset", "question", "market_id",
        "side", "fill_price", "fill_amount", "fill_cost",
        "secs_after_close", "snap_label",
        "up_ask", "down_ask", "combined_ask",
        "winner", "won", "is_ours",
        "asset_price", "candle_open", "price_distance",
        "candle_range", "range_pct", "on_the_line",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()

    for ev in events:
        writer.writerow({
            "timestamp": ev.get("ts_str", ""),
            "asset": ev.get("asset", ""),
            "question": ev.get("question", ""),
            "market_id": ev.get("market_id", ""),
            "side": ev.get("side", ""),
            "fill_price": ev.get("fill_price", ""),
            "fill_amount": ev.get("fill_amount", ""),
            "fill_cost": ev.get("fill_cost", ""),
            "secs_after_close": ev.get("secs_after_close", ""),
            "snap_label": ev.get("snap_label", ""),
            "up_ask": ev.get("up_ask", ""),
            "down_ask": ev.get("down_ask", ""),
            "combined_ask": ev.get("combined_ask", ""),
            "winner": ev.get("winner", ""),
            "won": ev.get("won", ""),
            "is_ours": ev.get("is_ours", ""),
            "asset_price": ev.get("asset_price", ""),
            "candle_open": ev.get("candle_open", ""),
            "price_distance": ev.get("price_distance", ""),
            "candle_range": ev.get("candle_range", ""),
            "range_pct": ev.get("range_pct", ""),
            "on_the_line": ev.get("on_the_line", ""),
        })

    csv_data = output.getvalue()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=afterhours_fills_{today}.csv"},
    )


# ── SocketIO Events ─────────────────────────────────────────────────────────

@socketio.on("connect")
def on_connect():
    push_state()
    # Send full AH events list on connect (separate from state to keep state payload small)
    socketio.emit("ah_events", list(engine.afterhours_events))


@socketio.on("start_bot")
def on_start_bot():
    if engine.running:
        return
    engine.running = True
    engine.bot_thread = threading.Thread(target=bot_loop, daemon=True)
    engine.bot_thread.start()
    engine.add_log("Bot started from dashboard", "info")


@socketio.on("stop_bot")
def on_stop_bot():
    engine.running = False
    engine.add_log("Stop requested from dashboard", "warn")


@socketio.on("update_params")
def on_update_params(data):
    """Live-update bid price, tokens, window open/close from the dashboard."""
    try:
        new_price = 0.01  # Always fixed at $0.01
        new_tokens = int(data.get("tokens_per_side", engine.tokens_per_side))

        # Hard safety clamp — dashboard can NEVER exceed these
        new_tokens = min(new_tokens, config.HARD_MAX_TOKENS)
        new_window_open = int(data.get("bid_window_open", engine.bid_window_open))
        new_window_close = int(data.get("bid_window_close", engine.bid_window_close))
        # Per-asset full config: expect {"BTC": {"enabled": true, "dollar_range": 2.0, ...}, ...}
        new_asset_config = {a: dict(c) for a, c in engine.asset_config.items()}
        incoming_cfg = data.get("asset_config", {})
        if isinstance(incoming_cfg, dict):
            _editable_keys = ("enabled", "bid_price", "tokens_per_side", "bid_window_open",
                              "distance_pct_max", "candle_range_max",
                              "dollar_range", "expansion_max", "expansion_floor")
            for ak, av in incoming_cfg.items():
                if ak not in new_asset_config:
                    continue   # ignore unknown assets
                if isinstance(av, dict):
                    for pk in _editable_keys:
                        if pk in av:
                            if pk == "enabled":
                                new_asset_config[ak][pk] = bool(av[pk])
                            elif pk in ("tokens_per_side", "bid_window_open"):
                                try:
                                    new_asset_config[ak][pk] = int(av[pk])
                                except (ValueError, TypeError):
                                    pass
                            else:
                                try:
                                    new_asset_config[ak][pk] = float(av[pk])
                                except (ValueError, TypeError):
                                    pass
        # Force all per-asset bid_price to $0.01 regardless of incoming data
        for ak in new_asset_config:
            new_asset_config[ak]["bid_price"] = 0.01
        new_ob_enabled = bool(data.get("ob_filter_enabled", engine.ob_filter_enabled))
        new_ob_min_size = float(data.get("ob_min_size", engine.ob_min_size))
        new_ob_max_imbalance = float(data.get("ob_max_imbalance", engine.ob_max_imbalance))

        # new_price is always 0.01, no validation needed
        if new_tokens < 1 or new_tokens > config.HARD_MAX_TOKENS:
            engine.add_log(f"Invalid tokens: {new_tokens} (must be 1-{config.HARD_MAX_TOKENS})", "error")
            return
        if new_window_open < 5 or new_window_open > 120:
            engine.add_log(f"Invalid cancel-at: {new_window_open}s (must be 5-120)", "error")
            return
        if new_window_close < 0 or new_window_close > 300:
            engine.add_log(f"Invalid window close: {new_window_close}s (must be 0-300)", "error")
            return
        if new_window_open <= new_window_close:
            engine.add_log(f"Window open ({new_window_open}s) must be > close ({new_window_close}s)", "error")
            return
        for _ak, _acfg in new_asset_config.items():
            bp = _acfg.get("bid_price", 0.02)
            if bp <= 0 or bp > config.HARD_MAX_BID_PRICE:
                engine.add_log(f"Invalid {_ak} bid price: ${bp} (must be $0.01-${config.HARD_MAX_BID_PRICE})", "error")
                return
            _at = _acfg.get("tokens_per_side", 100)
            if _at < 1 or _at > config.HARD_MAX_TOKENS:
                engine.add_log(f"Invalid {_ak} shares/side: {_at} (must be 1-{config.HARD_MAX_TOKENS})", "error")
                return
            _aw = _acfg.get("bid_window_open", 30)
            if _aw < 5 or _aw > 120:
                engine.add_log(f"Invalid {_ak} cancel-at: {_aw}s (must be 5-120)", "error")
                return
            if _aw <= new_window_close:
                engine.add_log(f"{_ak} cancel-at ({_aw}s) must be > bid stop ({new_window_close}s)", "error")
                return
            dr = _acfg.get("dollar_range", 0)
            if dr < 0 or dr > 5000:
                engine.add_log(f"Invalid {_ak} dollar range: ${dr} (must be 0-5000, 0=disabled)", "error")
                return
            cr = _acfg.get("candle_range_max", 0)
            if cr < 0:
                engine.add_log(f"Invalid {_ak} candle range max: {cr} (must be >= 0)", "error")
                return
        if new_ob_min_size < 0 or new_ob_min_size > 10000:
            engine.add_log(f"Invalid OB min size: {new_ob_min_size} (must be 0-10000)", "error")
            return
        if new_ob_max_imbalance < 0 or new_ob_max_imbalance > 100:
            engine.add_log(f"Invalid OB max imbalance: {new_ob_max_imbalance} (must be 0-100, 0=disabled)", "error")
            return

        changes = []
        if new_price != engine.bid_price:
            changes.append(f"Bid ${engine.bid_price}->${new_price}")
        if new_tokens != engine.tokens_per_side:
            changes.append(f"Shares {engine.tokens_per_side}->{new_tokens}")
        if new_window_open != engine.bid_window_open:
            changes.append(f"Cancel At {engine.bid_window_open}s->{new_window_open}s")
        if new_window_close != engine.bid_window_close:
            changes.append(f"Window Close {engine.bid_window_close}s->{new_window_close}s")
        for _ak in sorted(new_asset_config):
            old_c = engine.asset_config.get(_ak, {})
            new_c = new_asset_config[_ak]
            if new_c.get("enabled") != old_c.get("enabled"):
                changes.append(f"{_ak} {'ON' if new_c['enabled'] else 'OFF'}")
            for _pk in ("tokens_per_side", "bid_window_open"):
                old_v = old_c.get(_pk, 0)
                new_v = new_c.get(_pk, 0)
                if new_v != old_v:
                    changes.append(f"{_ak}.{_pk} {old_v}->{new_v}")
            for _pk in ("bid_price", "distance_pct_max", "candle_range_max", "dollar_range", "expansion_max", "expansion_floor"):
                old_v = old_c.get(_pk, 0)
                new_v = new_c.get(_pk, 0)
                if new_v != old_v:
                    changes.append(f"{_ak}.{_pk} {old_v:.4g}->{new_v:.4g}")
        if new_ob_enabled != engine.ob_filter_enabled:
            changes.append(f"OB Filter {'ON' if new_ob_enabled else 'OFF'}")
        if new_ob_min_size != engine.ob_min_size:
            changes.append(f"OB MinSize {engine.ob_min_size:.0f}->{new_ob_min_size:.0f}")
        if new_ob_max_imbalance != engine.ob_max_imbalance:
            changes.append(f"OB MaxImbal {engine.ob_max_imbalance:.1f}->{new_ob_max_imbalance:.1f}")

        engine.bid_price = new_price
        engine.tokens_per_side = new_tokens
        engine.bid_window_open = new_window_open
        engine.bid_window_close = new_window_close
        engine.asset_config = new_asset_config
        engine.btc_dollar_range = engine.asset_config.get("BTC", {}).get("dollar_range", 2.0)
        engine.ob_filter_enabled = new_ob_enabled
        engine.ob_min_size = new_ob_min_size
        engine.ob_max_imbalance = new_ob_max_imbalance

        if changes:
            cost = new_price * new_tokens * 2
            engine.add_log(
                f"Params updated: {', '.join(changes)}  (${cost:.2f} if both fill)",
                "info",
            )
            log.info(f"[PARAMS] {', '.join(changes)}")
        # Always log asset bid prices for debugging
        for _ak in sorted(engine.asset_config):
            _bp = engine.asset_config[_ak].get("bid_price", "?")
            log.info(f"[PARAMS] {_ak} bid_price={_bp}")
        _save_settings()
        push_state()
    except (ValueError, TypeError) as e:
        engine.add_log(f"Bad param update: {e}", "error")


@socketio.on("update_learner_params")
def on_update_learner_params(data):
    """Live-update learner parameters from dashboard."""
    try:
        if "confidence_threshold" in data:
            val = float(data["confidence_threshold"])
            if 0.0 <= val <= 1.0:
                old = ai_learner.confidence_threshold
                ai_learner.confidence_threshold = val
                if val != old:
                    engine.add_log(f"AI threshold {old:.0%}->{val:.0%}", "info")
        if "learning_rate" in data:
            val = float(data["learning_rate"])
            if 0.001 <= val <= 10.0:
                ai_learner.learning_rate = val
        ai_learner._save_model()  # Persist threshold/learning_rate changes
        push_state()
    except (ValueError, TypeError) as e:
        engine.add_log(f"Bad learner param: {e}", "error")


@socketio.on("toggle_learner")
def on_toggle_learner():
    """Enable/disable the adaptive learner gate."""
    ai_learner.enabled = not ai_learner.enabled
    state = "ON" if ai_learner.enabled else "OFF"
    engine.add_log(f"AI learner gate {state}", "info")
    ai_learner._save_model()  # Persist enabled state
    push_state()


@socketio.on("reset_learner")
def on_reset_learner():
    """Reset learner model weights (keeps trade history on disk)."""
    ai_learner.reset_model()
    engine.add_log("AI learner model RESET", "warn")
    push_state()


# ── Market Scanner SocketIO Events ──────────────────────────────────────────

def _scanner_log(msg: str, level: str = "info"):
    """Emit a log message to both main log and scanner-specific log channel."""
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    engine.activity_log.appendleft({"ts": ts, "msg": msg, "level": level})
    socketio.emit("log", {"ts": ts, "msg": msg, "level": level})
    socketio.emit("scanner_log", {"ts": ts, "msg": msg, "level": level})

def _run_scanner():
    """Background scanner loop — queries Gamma API periodically."""
    _scanner_log("Market Scanner started", "info")
    while engine.scanner_running:
        try:
            cfg = engine.scanner_cfg
            markets = scan_active_markets(
                max_hours_to_expiry=cfg["max_hours"],
                min_liquidity=cfg["min_liquidity"],
                max_liquidity=cfg["max_liquidity"],
            )

            # Filter by selected categories
            allowed_cats = set(cfg.get("categories", []))
            if allowed_cats:
                markets = [m for m in markets if m.category in allowed_cats]

            # Fetch best ask prices for top markets (limit to 40 to avoid rate-limiting)
            top_markets = markets[:40]
            if top_markets:
                token_pairs = [(m.token_id_yes, m.token_id_no) for m in top_markets]
                books = fetch_orderbooks_parallel(token_pairs)
                for m in top_markets:
                    pair = books.get(m.token_id_yes)
                    if pair:
                        book_yes, book_no = pair
                        m.best_ask_yes = book_yes.best_ask if book_yes.valid else 0
                        m.best_ask_no = book_no.best_ask if book_no.valid else 0

            # Convert to dicts for dashboard
            now = datetime.now(timezone.utc)
            result_dicts = []
            for m in markets:
                secs = (m.end_time - now).total_seconds()
                result_dicts.append({
                    "market_id": m.market_id,
                    "question": m.question,
                    "outcome_yes": m.outcome_yes,
                    "outcome_no": m.outcome_no,
                    "secs": round(secs, 0),
                    "end_time": m.end_time.isoformat(),
                    "liquidity": round(m.liquidity, 2),
                    "volume": round(m.volume, 2),
                    "category": m.category,
                    "slug": m.slug,
                    "best_ask_yes": round(m.best_ask_yes, 3) if m.best_ask_yes > 0 else 0,
                    "best_ask_no": round(m.best_ask_no, 3) if m.best_ask_no > 0 else 0,
                    "combined": round(m.best_ask_yes + m.best_ask_no, 3) if m.best_ask_yes > 0 and m.best_ask_no > 0 else 0,
                    "token_id_yes": m.token_id_yes,
                    "token_id_no": m.token_id_no,
                })

            engine.scanner_results = result_dicts
            engine.scanner_last_scan = time.time()

            # Auto-bid logic
            if engine.scanner_auto_bid and cfg.get("auto_bid_categories"):
                _scanner_auto_bid(top_markets, cfg)

            cat_key = "category"
            cat_set = set(r[cat_key] for r in result_dicts)
            cat_summary = ", ".join(
                f"{cat}:{sum(1 for r in result_dicts if r[cat_key] == cat)}"
                for cat in cat_set
            )
            _scanner_log(
                f"Scanner: {len(result_dicts)} markets found ({cat_summary})",
                "info",
            )
        except Exception as e:
            log_error("scanner_loop", e)
            _scanner_log(f"Scanner error: {e}", "error")

        # Wait for next scan interval
        interval = engine.scanner_cfg.get("scan_interval", 30)
        for _ in range(int(interval)):
            if not engine.scanner_running:
                break
            time.sleep(1)

    _scanner_log("Market Scanner stopped", "warn")


def _scanner_auto_bid(markets: list, cfg: dict):
    """Auto-bid on scanner markets matching criteria."""
    auto_cats = set(cfg.get("auto_bid_categories", []))
    max_ask = cfg.get("auto_bid_max_ask", 0.15)
    min_liq = cfg.get("auto_bid_min_liq", 50.0)
    bid_price = min(cfg.get("auto_bid_price", 0.05), config.HARD_MAX_BID_PRICE)
    bid_size = cfg.get("auto_bid_size", 100)

    for m in markets:
        if m.category not in auto_cats:
            continue
        if m.liquidity < min_liq:
            continue
        # Skip if already in watch list or bids posted
        if m.market_id in engine.bids_posted:
            continue
        # Skip crypto markets already handled by main bot
        if m.category in ("crypto-5m",) and m.market_id in engine.watch_list:
            continue
        # Check ask prices
        if m.best_ask_yes <= 0 or m.best_ask_no <= 0:
            continue
        if m.best_ask_yes > max_ask or m.best_ask_no > max_ask:
            continue

        # Place bids on both sides
        _scanner_log(
            f"SCANNER AUTO-BID: {m.question}  "
            f"${bid_price} x {bid_size}/side  "
            f"(asks: {m.outcome_yes}=${m.best_ask_yes:.3f} {m.outcome_no}=${m.best_ask_no:.3f}  "
            f"liq=${m.liquidity:.0f})",
            "trade",
        )

        oid_yes = place_limit_buy(
            token_id=m.token_id_yes, price=bid_price,
            size=bid_size, market_id=m.market_id,
        )
        oid_no = place_limit_buy(
            token_id=m.token_id_no, price=bid_price,
            size=bid_size, market_id=m.market_id,
        )

        if oid_yes or oid_no:
            _scanner_log(
                f"SCANNER BIDS POSTED: {m.question[:60]}  "
                f"(yes={oid_yes is not None}, no={oid_no is not None})",
                "trade",
            )
        else:
            _scanner_log(f"SCANNER BIDS FAILED: {m.question[:60]}", "error")


@socketio.on("start_scanner")
def on_start_scanner():
    if engine.scanner_running:
        return
    engine.scanner_running = True
    engine.scanner_thread = threading.Thread(target=_run_scanner, daemon=True)
    engine.scanner_thread.start()
    push_state()


@socketio.on("stop_scanner")
def on_stop_scanner():
    engine.scanner_running = False
    _scanner_log("Scanner stop requested", "warn")
    push_state()


@socketio.on("update_scanner_params")
def on_update_scanner_params(data):
    """Live-update scanner filter parameters from dashboard."""
    try:
        cfg = engine.scanner_cfg
        if "max_hours" in data:
            cfg["max_hours"] = max(0.1, min(168.0, float(data["max_hours"])))
        if "min_liquidity" in data:
            cfg["min_liquidity"] = max(0.0, float(data["min_liquidity"]))
        if "max_liquidity" in data:
            cfg["max_liquidity"] = max(0.0, float(data["max_liquidity"]))
        if "scan_interval" in data:
            cfg["scan_interval"] = max(10, min(300, int(data["scan_interval"])))
        if "categories" in data:
            cfg["categories"] = [c for c in data["categories"] if isinstance(c, str)]
        if "auto_bid_price" in data:
            cfg["auto_bid_price"] = max(0.01, min(config.HARD_MAX_BID_PRICE, float(data["auto_bid_price"])))
        if "auto_bid_size" in data:
            cfg["auto_bid_size"] = max(10, min(10000, int(data["auto_bid_size"])))
        if "auto_bid_max_ask" in data:
            cfg["auto_bid_max_ask"] = max(0.01, min(0.90, float(data["auto_bid_max_ask"])))
        if "auto_bid_min_liq" in data:
            cfg["auto_bid_min_liq"] = max(0.0, float(data["auto_bid_min_liq"]))
        if "auto_bid_categories" in data:
            cfg["auto_bid_categories"] = [c for c in data["auto_bid_categories"] if isinstance(c, str)]
        _scanner_log("Scanner params updated", "info")
        _save_settings()
        push_state()
    except (ValueError, TypeError) as e:
        _scanner_log(f"Bad scanner param: {e}", "error")


@socketio.on("toggle_scanner_auto_bid")
def on_toggle_scanner_auto_bid():
    engine.scanner_auto_bid = not engine.scanner_auto_bid
    state = "ON" if engine.scanner_auto_bid else "OFF"
    _scanner_log(f"Scanner auto-bid {state}", "info")
    _save_settings()
    push_state()


@socketio.on("scanner_manual_bid")
def on_scanner_manual_bid(data):
    """Place a manual bid on a scanned market from the dashboard."""
    try:
        market_id = data.get("market_id", "")
        token_yes = data.get("token_id_yes", "") or data.get("token_yes", "")
        token_no = data.get("token_id_no", "") or data.get("token_no", "")
        price = min(float(data.get("bid_price", data.get("price", 0.05))), config.HARD_MAX_BID_PRICE)
        size = max(10, min(10000, int(data.get("tokens", data.get("size", 100)))))

        if not market_id or not token_yes or not token_no:
            _scanner_log("Manual bid failed: missing market data", "error")
            return

        question = data.get("question", market_id[:16])
        _scanner_log(
            f"MANUAL BID: {question[:60]}  ${price} x {size}/side",
            "trade",
        )

        oid_yes = place_limit_buy(token_id=token_yes, price=price, size=size, market_id=market_id)
        oid_no = place_limit_buy(token_id=token_no, price=price, size=size, market_id=market_id)

        if oid_yes or oid_no:
            _scanner_log(f"MANUAL BIDS POSTED: {question[:60]}", "trade")
        else:
            _scanner_log(f"MANUAL BIDS FAILED: {question[:60]}", "error")

        push_state()
    except Exception as e:
        _scanner_log(f"Manual bid error: {e}", "error")


@socketio.on("clear_afterhours")
def on_clear_afterhours():
    """Clear the after-hours fill events buffer (client request)."""
    engine.afterhours_events.clear()
    _ah_save_all()   # clear the disk file too
    push_state()


# ── Trade Journal Socket Events ──────────────────────────────────────────────

@socketio.on("toggle_auto_trade")
def on_toggle_auto_trade(data):
    """Toggle auto-trade on/off from dashboard."""
    val = data.get("enabled", False) if isinstance(data, dict) else False
    engine.auto_trade_enabled = bool(val)
    _save_settings()
    engine.add_log(f"Auto-trade {'ENABLED' if engine.auto_trade_enabled else 'DISABLED'}", "trade")
    push_state()


# ── Arbitrage Socket Events ──────────────────────────────────────────────────

@socketio.on("arb_toggle")
def on_arb_toggle(data):
    """Toggle arb engine on/off."""
    if not engine.arb:
        return
    val = data.get("enabled", False) if isinstance(data, dict) else False
    engine.arb.enabled = bool(val)
    _save_settings()
    engine.add_log(f"[ARB] {'ENABLED' if engine.arb.enabled else 'DISABLED'}", "trade")
    push_state()


@socketio.on("arb_update_params")
def on_arb_update_params(data):
    """Update arb engine parameters from dashboard."""
    if not engine.arb or not isinstance(data, dict):
        return
    if "min_edge" in data:
        engine.arb.min_edge = max(0.005, min(0.20, float(data["min_edge"])))
    if "trade_size" in data:
        engine.arb.trade_size = max(0.50, min(100.0, float(data["trade_size"])))
    if "max_positions" in data:
        engine.arb.max_positions = max(1, min(10, int(data["max_positions"])))
    if "cooldown" in data:
        engine.arb.cooldown = max(5, min(300, float(data["cooldown"])))
    if "fill_timeout" in data:
        engine.arb.fill_timeout = max(10, min(120, float(data["fill_timeout"])))
    if "max_daily_spend" in data:
        engine.arb.max_daily_spend = max(1.0, min(500.0, float(data["max_daily_spend"])))
    _save_settings()
    engine.add_log(
        f"[ARB] Params updated: edge={engine.arb.min_edge:.3f} size=${engine.arb.trade_size:.2f} "
        f"max_pos={engine.arb.max_positions} cooldown={engine.arb.cooldown:.0f}s "
        f"timeout={engine.arb.fill_timeout:.0f}s daily_cap=${engine.arb.max_daily_spend:.0f}",
        "info",
    )
    push_state()


@socketio.on("arb_manual")
def on_arb_manual(data):
    """Manually trigger an arb on a specific market from the dashboard."""
    if not engine.arb:
        engine.add_log("[ARB] Engine not initialized", "error")
        return
    mid = data.get("market_id", "") if isinstance(data, dict) else ""
    if not mid or mid not in engine.watch_list:
        engine.add_log(f"[ARB] Market {mid[:12]} not in watch list", "error")
        return
    market = engine.watch_list[mid]
    # Temporarily bypass cooldown for manual trigger
    old_cd = engine.arb.cooldowns.pop(mid, None)
    pos = engine.arb.scan_opportunity(market)
    if not pos:
        # Restore cooldown if scan failed
        if old_cd is not None:
            engine.arb.cooldowns[mid] = old_cd
        engine.add_log(f"[ARB] No opportunity on {market.asset} (combined ask >= threshold)", "warn")
    push_state()


@socketio.on("tj_quick_snipe")
def on_tj_quick_snipe(data):
    """Quick snipe: rapidly place limit buys on both sides (or one side) and log to journal.
    
    data: { market_id, side ("UP"|"DOWN"|"BOTH"), price_cents (int 1-99), size (int) }
    """
    try:
        mid = data.get("market_id", "")
        side = data.get("side", "BOTH").upper()
        price_cents = int(data.get("price_cents", 1))
        price = round(price_cents / 100.0, 2)
        size = int(data.get("size", 100))

        market = engine.watch_list.get(mid)
        if not market:
            socketio.emit("qs_result", {"ok": False, "msg": f"Market not found"})
            engine.add_log(f"[QS] Market {mid[:12]} not found", "error")
            return

        question = market.question
        asset = market.asset

        order_id_up = None
        order_id_down = None

        if side in ("UP", "BOTH"):
            order_id_up = place_limit_buy(
                token_id=market.token_id_up, price=price, size=size, market_id=mid
            )
        if side in ("DOWN", "BOTH"):
            order_id_down = place_limit_buy(
                token_id=market.token_id_down, price=price, size=size, market_id=mid
            )

        placed = []
        if order_id_up: placed.append("UP")
        if order_id_down: placed.append("DOWN")

        if not placed:
            socketio.emit("qs_result", {"ok": False, "msg": "Order placement failed"})
            engine.add_log(f"[QS] FAILED snipe on {question[:50]}", "error")
            return

        # Log to trade journal
        entry = tj_record_trade({
            "market_id": mid,
            "question": question,
            "asset": asset,
            "side": side,
            "price": price,
            "size": size,
            "order_id": order_id_up or order_id_down or "",
            "order_id_up": order_id_up or "",
            "order_id_down": order_id_down or "",
            "token_id_up": market.token_id_up,
            "token_id_down": market.token_id_down,
            "strategy": "quick_snipe",
            "notes": f"Quick snipe {side} @ {price_cents}¢ x{size}",
        })

        sides_str = " + ".join(placed)
        cost = price * size * len(placed)
        msg = f"✅ {sides_str} placed @ {price_cents}¢ x{size} (${cost:.2f})"
        socketio.emit("qs_result", {"ok": True, "msg": msg})
        engine.add_log(f"[QS] {asset} {msg}", "success")
        push_state()

    except Exception as e:
        socketio.emit("qs_result", {"ok": False, "msg": str(e)})
        engine.add_log(f"[QS] Quick snipe error: {e}", "error")
        log.exception("[QS] Quick snipe error")


@socketio.on("tj_place_trade")
def on_tj_place_trade(data):
    """Place a manual trade and log it to the trade journal.
    
    data: { market_id, side ("UP"|"DOWN"|"BOTH"), price, size, notes, strategy, confidence, reason }
    """
    try:
        mid = data.get("market_id", "")
        side = data.get("side", "BOTH").upper()
        price = float(data.get("price", 0.01))
        size = int(data.get("size", 100))

        market = engine.watch_list.get(mid)
        if not market:
            engine.add_log(f"[TJ] Market {mid[:12]} not found in watch_list", "error")
            return

        question = market.question
        asset = market.asset

        order_id_up = None
        order_id_down = None
        order_id = None

        if side in ("UP", "BOTH"):
            order_id_up = place_limit_buy(
                token_id=market.token_id_up, price=price, size=size, market_id=mid
            )

        if side in ("DOWN", "BOTH"):
            order_id_down = place_limit_buy(
                token_id=market.token_id_down, price=price, size=size, market_id=mid
            )

        if side == "UP":
            order_id = order_id_up
        elif side == "DOWN":
            order_id = order_id_down

        if not order_id_up and not order_id_down and not order_id:
            engine.add_log(f"[TJ] Order placement FAILED for {question[:50]}", "error")
            return

        entry = tj_record_trade({
            "market_id": mid,
            "question": question,
            "asset": asset,
            "side": side,
            "price": price,
            "size": size,
            "order_id": order_id or "",
            "order_id_up": order_id_up or "",
            "order_id_down": order_id_down or "",
            "token_id_up": market.token_id_up,
            "token_id_down": market.token_id_down,
            "notes": data.get("notes", ""),
            "strategy": data.get("strategy", ""),
            "confidence": data.get("confidence", ""),
            "reason": data.get("reason", ""),
        })

        push_state()
    except Exception as e:
        engine.add_log(f"[TJ] Place trade error: {e}", "error")
        log.exception("[TJ] Place trade error")


@socketio.on("tj_log_only")
def on_tj_log_only(data):
    """Log a trade to the journal WITHOUT placing orders (already placed externally)."""
    try:
        entry = tj_record_trade(data)
        push_state()
    except Exception as e:
        engine.add_log(f"[TJ] Log-only error: {e}", "error")


@socketio.on("tj_update_entry")
def on_tj_update_entry(data):
    """Update a trade journal entry (notes, outcome, etc.)."""
    try:
        trade_id = data.pop("id", "")
        if trade_id:
            tj_update_trade(trade_id, data)
            push_state()
    except Exception as e:
        engine.add_log(f"[TJ] Update error: {e}", "error")


@socketio.on("tj_resolve")
def on_tj_resolve(data):
    """Resolve a trade journal entry: { id, winner: "Up"|"Down" }"""
    try:
        trade_id = data.get("id", "")
        winner = data.get("winner", "")
        if trade_id and winner:
            tj_resolve_trade(trade_id, winner)
            push_state()
    except Exception as e:
        engine.add_log(f"[TJ] Resolve error: {e}", "error")


@socketio.on("tj_check_fills")
def on_tj_check_fills_event():
    """Manually trigger a fill check for all open journal trades."""
    try:
        tj_check_fills()
        push_state()
    except Exception as e:
        engine.add_log(f"[TJ] Fill check error: {e}", "error")


@socketio.on("tj_cancel_trade")
def on_tj_cancel_trade(data):
    """Cancel an open trade's orders and mark it cancelled in journal."""
    try:
        trade_id = data.get("id", "")
        for entry in engine.trade_journal:
            if entry.get("id") != trade_id:
                continue
            cancelled = 0
            for oid_key in ("order_id", "order_id_up", "order_id_down"):
                oid = entry.get(oid_key, "")
                if oid:
                    try:
                        if cancel_order(oid):
                            cancelled += 1
                    except Exception:
                        pass
            entry["status"] = "cancelled"
            entry["notes"] = (entry.get("notes", "") + " [CANCELLED]").strip()
            _tj_save_all()
            engine.add_log(f"[TJ] Cancelled trade {trade_id[:16]} ({cancelled} orders)", "info")
            try:
                socketio.emit("tj_updated", entry)
            except Exception:
                pass
            push_state()
            return
    except Exception as e:
        engine.add_log(f"[TJ] Cancel error: {e}", "error")


@socketio.on("tj_get_stats")
def on_tj_get_stats():
    """Send computed trade journal stats to the requesting client."""
    try:
        stats = tj_get_stats()
        socketio.emit("tj_stats", stats)
    except Exception as e:
        engine.add_log(f"[TJ] Stats error: {e}", "error")


@socketio.on("tj_clear")
def on_tj_clear():
    """Clear all trade journal entries."""
    engine.trade_journal.clear()
    _tj_save_all()
    push_state()
    engine.add_log("Trade journal CLEARED", "info")


@socketio.on("tj_export")
def on_tj_export():
    """Send full trade journal data for CSV export."""
    try:
        socketio.emit("tj_export_data", list(engine.trade_journal))
    except Exception as e:
        engine.add_log(f"[TJ] Export error: {e}", "error")


# ══════════════════════════════════════════════════════════════════════════════
#  HTML DASHBOARD (single-page, embedded)
# ══════════════════════════════════════════════════════════════════════════════

DASHBOARD_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Polybot Snipez</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.5/socket.io.min.js"></script>
<style>
  :root {
    --bg: #0d1117; --card: #161b22; --border: #30363d;
    --text: #e6edf3; --dim: #8b949e; --green: #3fb950;
    --red: #f85149; --yellow: #d29922; --cyan: #58a6ff;
    --orange: #d18616; --purple: #bc8cff;
  }
  * { margin:0; padding:0; box-sizing:border-box; }
  body {
    font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
    background: var(--bg); color: var(--text);
    min-height: 100vh;
  }

  /* ── Header ── */
  .header {
    background: linear-gradient(135deg, #1a1e2e 0%, #0d1117 100%);
    border-bottom: 1px solid var(--border);
    padding: 12px 24px;
    display: flex; align-items: center; justify-content: space-between;
  }
  .header h1 { font-size: 20px; font-weight: 700; letter-spacing: 1px; }
  .header h1 span { color: var(--cyan); }
  .header-right { display: flex; align-items: center; gap: 16px; }
  .mode-badge {
    padding: 4px 12px; border-radius: 4px; font-size: 12px;
    font-weight: 700; text-transform: uppercase;
  }
  .mode-live { background: var(--red); color: #fff; }
  .mode-paper { background: var(--yellow); color: #000; }
  .status-dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
  .status-dot.on { background: var(--green); box-shadow: 0 0 6px var(--green); }
  .status-dot.off { background: var(--red); }
  .clock { color: var(--dim); font-size: 13px; font-family: monospace; }

  /* ── Controls Bar ── */
  .controls {
    background: var(--card); border-bottom: 1px solid var(--border);
    padding: 10px 24px; display: flex; align-items: center; gap: 20px;
    flex-wrap: wrap;
  }
  .control-group { display: flex; align-items: center; gap: 6px; }
  .control-group label { color: var(--dim); font-size: 12px; text-transform: uppercase; font-weight: 600; }
  .control-group input {
    background: var(--bg); border: 1px solid var(--border); color: var(--cyan);
    padding: 6px 10px; border-radius: 4px; width: 90px; font-size: 14px;
    font-family: monospace; font-weight: 600; text-align: center;
  }
  .control-group input:focus { outline: none; border-color: var(--cyan); }
  .btn {
    padding: 8px 20px; border: none; border-radius: 4px; font-size: 13px;
    font-weight: 700; cursor: pointer; text-transform: uppercase;
    letter-spacing: 0.5px; transition: all 0.15s;
  }
  .btn:hover { filter: brightness(1.15); }
  .btn-start { background: var(--green); color: #000; }
  .btn-stop { background: var(--red); color: #fff; }
  .btn-apply { background: var(--cyan); color: #000; }
  .cost-preview {
    color: var(--dim); font-size: 12px; font-family: monospace;
  }
  .cost-preview .val { color: var(--yellow); font-weight: 600; }

  /* ── Info Hover Tooltips ── */
  .info-tip { position:relative; display:inline-block; cursor:help; margin-left:3px; }
  .info-tip .info-icon { display:inline-flex; align-items:center; justify-content:center; width:14px; height:14px; border-radius:50%; background:var(--border); color:var(--dim); font-size:9px; font-weight:700; font-style:normal; vertical-align:middle; }
  .info-tip .info-bubble { visibility:hidden; opacity:0; position:absolute; z-index:100; bottom:125%; left:50%; transform:translateX(-50%); background:#1c2333; color:var(--text); font-size:11px; font-weight:400; text-transform:none; letter-spacing:0; line-height:1.4; padding:8px 10px; border-radius:6px; border:1px solid var(--border); white-space:normal; width:220px; box-shadow:0 4px 12px rgba(0,0,0,0.4); pointer-events:none; transition:opacity 0.15s; }
  .info-tip .info-bubble::after { content:''; position:absolute; top:100%; left:50%; transform:translateX(-50%); border:6px solid transparent; border-top-color:#1c2333; }
  .info-tip:hover .info-bubble { visibility:visible; opacity:1; }

  /* ── Dashboard Grid ── */
  .grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    grid-template-rows: auto 1fr;
    gap: 12px; padding: 12px 24px;
    max-height: calc(100vh - 110px);
  }
  .card {
    background: var(--card); border: 1px solid var(--border);
    border-radius: 8px; overflow: hidden;
  }
  .card-title {
    padding: 8px 14px; font-size: 12px; font-weight: 700;
    text-transform: uppercase; letter-spacing: 1px;
    border-bottom: 1px solid var(--border); color: var(--dim);
  }
  .card-body { padding: 10px 14px; }

  /* Stats row */
  .stats-row {
    grid-column: 1 / -1;
    display: grid; grid-template-columns: repeat(6, 1fr); gap: 12px;
  }
  .stat-card {
    background: var(--card); border: 1px solid var(--border);
    border-radius: 8px; padding: 12px 14px; text-align: center;
  }
  .stat-label { font-size: 10px; color: var(--dim); text-transform: uppercase; letter-spacing: 1px; }
  .stat-value { font-size: 22px; font-weight: 700; margin-top: 4px; font-family: monospace; }
  .stat-value.green { color: var(--green); }
  .stat-value.red { color: var(--red); }
  .stat-value.cyan { color: var(--cyan); }
  .stat-value.yellow { color: var(--yellow); }

  /* Markets table */
  .markets-card { grid-column: 1 / -1; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  thead th {
    text-align: left; padding: 6px 10px; color: var(--dim); font-size: 11px;
    text-transform: uppercase; border-bottom: 1px solid var(--border);
    font-weight: 600; letter-spacing: 0.5px;
  }
  tbody td { padding: 7px 10px; border-bottom: 1px solid #1c2333; font-family: monospace; }
  tbody tr:hover { background: #1c2333; }
  .price { font-weight: 600; }
  .price.good { color: var(--green); }
  .price.mid { color: var(--yellow); }
  .price.high { color: var(--dim); }
  .time-cell { font-weight: 700; }
  .time-cell.hot { color: var(--red); }
  .time-cell.warm { color: var(--yellow); }
  .time-cell.cool { color: var(--cyan); }

  .badge {
    display: inline-block; padding: 2px 8px; border-radius: 3px;
    font-size: 10px; font-weight: 700; text-transform: uppercase;
  }
  .badge-watching { background: #1c2333; color: var(--dim); }
  .badge-in_range { background: #2d2000; color: var(--yellow); }
  .badge-bids_live { background: #0c2d4a; color: var(--cyan); }
  .badge-partial_fill { background: #2d2000; color: var(--orange); }
  .badge-both_filled { background: #0d2818; color: var(--green); }
  .badge-expired { background: #1c2333; color: var(--dim); }

  /* Activity log */
  .log-card { grid-column: 1 / -1; max-height: 320px; }
  .log-body { max-height: 260px; overflow-y: auto; font-size: 12px; font-family: monospace; }
  .log-line { padding: 3px 0; display: flex; gap: 8px; }
  .log-ts { color: var(--dim); min-width: 60px; }
  .log-msg { word-break: break-word; }
  .log-msg.info { color: var(--text); }
  .log-msg.warn { color: var(--yellow); }
  .log-msg.error { color: var(--red); }
  .log-msg.trade { color: var(--cyan); }
  .log-msg.fill { color: var(--green); font-weight: 700; }
  .log-msg.profit { color: var(--yellow); font-weight: 700; }
  .log-msg.cancel { color: var(--dim); }

  /* P&L card */
  .pnl-row { display: flex; justify-content: space-between; padding: 4px 0; }
  .pnl-label { color: var(--dim); font-size: 12px; }
  .pnl-value { font-family: monospace; font-weight: 600; }
  .pnl-value.spent { color: var(--yellow); }
  .pnl-value.payout { color: var(--green); }
  .pnl-value.profit-pos { color: var(--green); }
  .pnl-value.profit-neg { color: var(--red); }
  .pnl-value.neutral { color: var(--dim); }

  /* Empty state */
  .empty { text-align: center; color: var(--dim); padding: 30px; font-style: italic; }

  /* ── AI Learner Panel ── */
  .learner-section {
    padding: 12px 24px 0 24px;
  }
  .learner-grid {
    display: grid;
    grid-template-columns: 1fr 1fr 1fr;
    gap: 12px;
  }
  .learner-stats {
    display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px;
  }
  .learner-stat {
    text-align: center; padding: 6px 4px;
    background: var(--bg); border-radius: 4px;
  }
  .learner-stat .ls-label { font-size: 9px; color: var(--dim); text-transform: uppercase; letter-spacing: 0.5px; }
  .learner-stat .ls-value { font-size: 16px; font-weight: 700; font-family: monospace; margin-top: 2px; }
  .weight-bar-container { display: flex; align-items: center; gap: 6px; margin: 3px 0; font-size: 11px; }
  .weight-bar-label { min-width: 110px; color: var(--dim); font-family: monospace; text-align: right; font-size: 10px; }
  .weight-bar-wrap { flex: 1; height: 12px; background: var(--bg); border-radius: 3px; position: relative; overflow: hidden; }
  .weight-bar { height: 100%; border-radius: 3px; transition: width 0.3s; }
  .weight-bar.pos { background: var(--green); }
  .weight-bar.neg { background: var(--red); }
  .weight-bar-val { min-width: 50px; font-family: monospace; font-size: 10px; }
  .pred-table { width: 100%; font-size: 11px; border-collapse: collapse; }
  .pred-table th { color: var(--dim); font-size: 10px; text-transform: uppercase; padding: 3px 6px; text-align: left; border-bottom: 1px solid var(--border); }
  .pred-table td { padding: 3px 6px; font-family: monospace; border-bottom: 1px solid #1c2333; }
  .pred-allow { color: var(--green); }
  .pred-skip { color: var(--red); }
  .learner-controls { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-top: 8px; }
  .learner-controls label { color: var(--dim); font-size: 11px; text-transform: uppercase; font-weight: 600; }
  .learner-controls input[type="range"] {
    width: 100px; accent-color: var(--cyan);
  }
  .learner-controls .range-val { color: var(--cyan); font-family: monospace; font-size: 12px; min-width: 40px; }
  .btn-sm {
    padding: 4px 12px; border: none; border-radius: 3px; font-size: 11px;
    font-weight: 700; cursor: pointer; text-transform: uppercase;
  }
  .btn-toggle-on { background: var(--green); color: #000; }
  .btn-toggle-off { background: var(--border); color: var(--dim); }
  .btn-reset { background: var(--red); color: #fff; opacity: 0.7; }
  .btn-reset:hover { opacity: 1; }
  .gate-status {
    font-size: 11px; font-weight: 700; padding: 3px 8px; border-radius: 3px;
    display: inline-block;
  }
  .gate-on { background: #0d2818; color: var(--green); }
  .gate-warmup { background: #2d2000; color: var(--yellow); }
  .gate-off { background: #1c2333; color: var(--dim); }

  /* ── Strategy Tabs ── */
  .tab-bar {
    display: flex; gap: 0; background: var(--card);
    border-bottom: 2px solid var(--border);
    padding: 0 24px;
  }
  .tab-btn {
    padding: 10px 28px; border: none; background: transparent;
    color: var(--dim); font-size: 13px; font-weight: 700;
    text-transform: uppercase; letter-spacing: 0.5px;
    cursor: pointer; border-bottom: 2px solid transparent;
    margin-bottom: -2px; transition: all 0.15s;
  }
  .tab-btn:hover { color: var(--text); }
  .tab-btn.active {
    color: var(--cyan); border-bottom-color: var(--cyan);
  }
  .tab-content { display: none; }
  .tab-content.active { display: block; }

  /* ── Scanner-specific styles ── */
  .scanner-controls {
    background: var(--card); border-bottom: 1px solid var(--border);
    padding: 10px 24px; display: flex; align-items: flex-start; gap: 16px;
    flex-wrap: wrap;
  }
  .scanner-section-label {
    font-size: 10px; font-weight: 700; text-transform: uppercase;
    letter-spacing: 0.5px; color: var(--dim); margin-bottom: 4px;
  }
  .scanner-filter-group {
    display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  }
  .scanner-cat-row {
    display: flex; gap: 8px; flex-wrap: wrap; align-items: center;
  }
  .scanner-cat-row label {
    font-size: 11px; color: var(--text); cursor: pointer;
    display: flex; align-items: center; gap: 3px;
  }
  .scanner-cat-row input[type="checkbox"] { accent-color: var(--cyan); }
  .scanner-stats-bar {
    display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px;
    padding: 12px 24px 0 24px;
  }
  .scanner-status {
    font-size: 11px; font-weight: 700; padding: 3px 10px; border-radius: 3px;
    display: inline-block; margin-left: 8px;
  }
  .scanner-status.on { background: #0d2818; color: var(--green); }
  .scanner-status.off { background: #1c2333; color: var(--dim); }
  .scanner-results-wrap {
    padding: 12px 24px;
  }
  .scanner-results-wrap table {
    font-size: 12px;
  }
  .scanner-results-wrap th {
    font-size: 10px; white-space: nowrap;
  }
  .scanner-results-wrap td {
    font-size: 11px; white-space: nowrap;
  }
  .scanner-autobid-section {
    background: var(--card); border: 1px solid var(--border);
    border-radius: 8px; padding: 10px 16px; margin-top: 4px;
  }
  .cat-badge {
    display: inline-block; padding: 1px 6px; border-radius: 3px;
    font-size: 9px; font-weight: 700; text-transform: uppercase;
  }
  .cat-badge.crypto-5m { background: #0c2d4a; color: var(--cyan); }
  .cat-badge.crypto-15m { background: #0c2d4a; color: #5bcefa; }
  .cat-badge.sports { background: #2d2000; color: var(--orange); }
  .cat-badge.esports { background: #2d0040; color: #c78dff; }
  .cat-badge.over-under { background: #2d2000; color: var(--yellow); }
  .cat-badge.weather { background: #002d1a; color: #5bfa8d; }
  .cat-badge.other { background: #1c2333; color: var(--dim); }
  .btn-bid-sm {
    padding: 2px 8px; border: 1px solid var(--cyan); background: transparent;
    color: var(--cyan); border-radius: 3px; font-size: 10px; cursor: pointer;
    font-weight: 700; text-transform: uppercase;
  }
  .btn-bid-sm:hover { background: var(--cyan); color: #000; }
  .scan-row-input {
    width: 62px; padding: 2px 4px; font-size: 11px; text-align: center;
    background: var(--bg); border: 1px solid var(--border); color: var(--text);
    border-radius: 3px;
  }
  .scan-row-input:focus { border-color: var(--cyan); outline: none; }
  .scan-row-actions {
    display: flex; align-items: center; gap: 4px; justify-content: center;
  }

  /* ── After-Hours Tracker styles ── */
  .ah-controls {
    background: var(--card); border-bottom: 1px solid var(--border);
    padding: 10px 24px; display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
  }
  .ah-stats-bar {
    display: grid; grid-template-columns: repeat(8, 1fr); gap: 12px;
    padding: 12px 24px 0 24px;
  }
  .ah-wrap {
    padding: 12px 24px;
  }
  .ah-wrap table {
    font-size: 11px; width: 100%; border-collapse: collapse;
  }
  .ah-wrap th {
    font-size: 10px; white-space: nowrap; padding: 4px 6px;
    text-align: left; color: var(--dim); text-transform: uppercase;
    border-bottom: 1px solid var(--border); position: sticky; top: 0;
    background: var(--card);
  }
  .ah-wrap td {
    font-size: 11px; font-family: monospace; padding: 3px 6px;
    border-bottom: 1px solid #1c2333; white-space: nowrap;
  }
  .ah-wrap tr:hover td { background: #1c2333; }
  /* Side badges */
  .ah-side { font-weight: 700; font-size: 11px; padding: 1px 6px; border-radius: 3px; }
  .ah-side.UP { background: #0c2d0c; color: var(--green); }
  .ah-side.DOWN { background: #2d0c0c; color: var(--red); }
  .ah-side.BOTH { background: #0c2d4a; color: var(--cyan); }
  /* Timing cell: positive (pre-close) vs negative (post-close) */
  .ah-pre { color: var(--green); }
  .ah-post { color: var(--red); }
  .ah-at-line { color: var(--cyan); font-weight: 700; }
  /* Summary breakdown cards */
  .ah-breakdown {
    display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px;
    padding: 12px 24px;
  }
  .ah-breakdown-card {
    background: var(--card); border: 1px solid var(--border);
    border-radius: 8px; padding: 14px 16px;
  }
  .ah-breakdown-card h4 {
    font-size: 11px; text-transform: uppercase; color: var(--dim);
    letter-spacing: 0.5px; margin-bottom: 10px;
  }
  .ah-breakdown-row {
    display: flex; justify-content: space-between; align-items: center;
    padding: 3px 0; font-size: 12px; font-family: monospace;
  }
  .ah-breakdown-label { color: var(--dim); }
  .ah-breakdown-val { font-weight: 700; }
  .ah-table-wrap {
    max-height: 420px; overflow-y: auto;
    background: var(--card); border: 1px solid var(--border);
    border-radius: 8px;
  }
  /* ── Two-Level Accordion: Asset → Market ── */
  .ah-groups-wrap {
    padding: 12px 24px; display: flex; flex-direction: column; gap: 10px;
    max-height: 700px; overflow-y: auto;
  }
  /* Level 1: Asset accordion */
  .ah-asset-group {
    background: var(--card); border: 1px solid var(--border);
    border-radius: 8px; overflow: hidden;
  }
  .ah-asset-group.expanded { border-color: var(--cyan); }
  .ah-asset-header {
    display: flex; align-items: center; gap: 12px; padding: 12px 16px;
    cursor: pointer; user-select: none; transition: background 0.15s;
    background: linear-gradient(90deg, rgba(0,255,255,0.04), transparent);
  }
  .ah-asset-header:hover { background: #1c2333; }
  .ah-asset-chevron {
    font-size: 14px; color: var(--dim); transition: transform 0.2s;
    width: 18px; text-align: center; flex-shrink: 0;
  }
  .ah-asset-group.expanded .ah-asset-chevron { transform: rotate(90deg); }
  .ah-asset-name {
    font-family: monospace; font-size: 15px; font-weight: 700;
    color: var(--cyan); min-width: 60px;
  }
  .ah-asset-meta {
    display: flex; gap: 14px; align-items: center; font-size: 12px;
    font-family: monospace; color: var(--dim); flex: 1;
  }
  .ah-asset-meta .am-count { color: var(--text); font-weight: 700; }
  .ah-asset-meta .am-up { color: var(--green); }
  .ah-asset-meta .am-down { color: var(--red); }
  .ah-asset-meta .am-wins { color: var(--green); font-weight: 700; }
  .ah-asset-meta .am-losses { color: var(--red); font-weight: 700; }
  .ah-asset-body {
    display: none; padding: 4px 8px 8px 8px;
    display: flex; flex-direction: column; gap: 6px;
  }
  .ah-asset-group:not(.expanded) .ah-asset-body { display: none; }
  .ah-asset-group.expanded .ah-asset-body { display: flex; flex-direction: column; gap: 6px; }

  /* Level 2: Market accordion (nested under asset) */
  .ah-group {
    background: var(--bg); border: 1px solid var(--border);
    border-radius: 6px; overflow: hidden; margin-left: 4px;
  }
  .ah-group.expanded { border-color: #445; }
  .ah-group-header {
    display: flex; align-items: center; gap: 10px; padding: 8px 12px;
    cursor: pointer; user-select: none; transition: background 0.15s;
  }
  .ah-group-header:hover { background: #1c2333; }
  .ah-group-chevron {
    font-size: 11px; color: var(--dim); transition: transform 0.2s;
    width: 14px; text-align: center; flex-shrink: 0;
  }
  .ah-group.expanded .ah-group-chevron { transform: rotate(90deg); }
  .ah-group-time {
    font-family: monospace; font-size: 12px; font-weight: 700;
    color: var(--cyan); min-width: 140px;
  }
  .ah-group-assets {
    display: flex; gap: 4px; flex-shrink: 0;
  }
  .ah-group-asset-badge {
    font-size: 9px; font-weight: 700; padding: 1px 5px;
    border-radius: 3px; background: #0c2d4a; color: var(--cyan);
  }
  .ah-group-meta {
    display: flex; gap: 10px; align-items: center; font-size: 11px;
    font-family: monospace; color: var(--dim); flex: 1;
  }
  .ah-group-meta .gm-fills { color: var(--text); font-weight: 700; }
  .ah-group-meta .gm-up { color: var(--green); }
  .ah-group-meta .gm-down { color: var(--red); }
  .ah-group-meta .gm-cheap { color: var(--yellow); }
  .ah-group-winner {
    font-size: 11px; font-weight: 700; padding: 2px 8px;
    border-radius: 3px; flex-shrink: 0;
  }
  .ah-group-winner.win { background: #0c2d0c; color: var(--green); }
  .ah-group-winner.loss { background: #2d0c0c; color: var(--red); }
  .ah-group-winner.pending { background: #1c2333; color: var(--dim); }
  .ah-group-winner.mixed { background: #2d2d0c; color: var(--yellow); }
  .ah-group-body {
    display: none; border-top: 1px solid var(--border);
  }
  .ah-group.expanded .ah-group-body { display: block; }
  .ah-group-body table {
    font-size: 11px; width: 100%; border-collapse: collapse;
  }
  .ah-group-body th {
    font-size: 10px; white-space: nowrap; padding: 4px 6px;
    text-align: left; color: var(--dim); text-transform: uppercase;
    border-bottom: 1px solid var(--border); background: var(--card);
  }
  .ah-group-body td {
    font-size: 11px; font-family: monospace; padding: 3px 6px;
    border-bottom: 1px solid #1c2333; white-space: nowrap;
  }
  .ah-group-body tr:hover td { background: #1c2333; }
  .ah-group-expand-all {
    display: flex; gap: 8px; padding: 0 24px 8px 24px;
  }
  .ah-group-expand-all button {
    font-size: 11px; padding: 4px 12px; border-radius: 4px;
    border: 1px solid var(--border); background: var(--card);
    color: var(--dim); cursor: pointer; font-family: monospace;
  }
  .ah-group-expand-all button:hover { color: var(--cyan); border-color: var(--cyan); }

  /* scrollbar */
  ::-webkit-scrollbar { width: 6px; }
  ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
</style>
</head>
<body>

<!-- ── Header ── -->
<div class="header">
  <h1><span>POLYBOT</span> SNIPEZ</h1>
  <div class="header-right">
    <span class="status-dot off" id="ws-dot" title="WebSocket"></span>
    <span style="font-size:12px;color:var(--dim)" id="ws-label">WS: OFF</span>
    <span class="mode-badge" id="mode-badge">--</span>
    <span class="clock" id="clock">--:--:-- UTC</span>
  </div>
</div>

<!-- ── Strategy Tabs ── -->
<div class="tab-bar">
  <button class="tab-btn active" onclick="switchTab('limit-bid')" id="tab-btn-limit-bid">Limit Bid Strategy</button>
  <button class="tab-btn" onclick="switchTab('scanner')" id="tab-btn-scanner">Market Scanner</button>
  <button class="tab-btn" onclick="switchTab('afterhours')" id="tab-btn-afterhours">After-Hours Fills <span id="ah-tab-badge" style="background:#0c2d4a;color:var(--cyan);border-radius:10px;padding:0 6px;font-size:10px;margin-left:4px;">0</span></button>
  <button class="tab-btn" onclick="switchTab('journal')" id="tab-btn-journal" style="background:linear-gradient(135deg,#0a1628,#0d2137);border:1px solid var(--green);">Trade Journal <span id="tj-tab-badge" style="background:#0c2d4a;color:var(--green);border-radius:10px;padding:0 6px;font-size:10px;margin-left:4px;">0</span></button>
  <button class="tab-btn" onclick="switchTab('arb')" id="tab-btn-arb" style="background:linear-gradient(135deg,#0a1628,#1a0a28);border:1px solid var(--purple);">Arb Engine <span id="arb-tab-badge" style="background:#1a0c2d;color:var(--purple);border-radius:10px;padding:0 6px;font-size:10px;margin-left:4px;">0</span></button>
</div>

<!-- ══════ TAB 1: LIMIT BID STRATEGY ══════ -->
<div class="tab-content active" id="tab-limit-bid">

<!-- ── Controls ── -->
<div class="controls">
  <!-- Bid price is ALWAYS $0.01 — shown as label, not editable -->
  <input type="hidden" id="inp-price" value="0.01">
  <div class="control-group">
    <label style="color:var(--green); font-weight:700;">Bid: $0.01</label>
  </div>
  <div class="control-group">
    <label>Shares/Side</label>
    <input type="number" id="inp-tokens" step="10" min="1" max="10000" value="{{ sv_tokens }}" title="Shares to bid on each side per market">
  </div>
  <div class="control-group" style="border-left: 1px solid var(--border); padding-left: 16px; margin-left: 4px;">
    <label>Cancel At (sec)</label>
    <input type="number" id="inp-window-open" step="5" min="5" max="120" value="{{ sv_window_open }}" title="Cancel losing side this many seconds before close">
  </div>
  <input type="hidden" id="inp-window-close" value="{{ sv_window_close }}">

  <!-- Market toggles — prominent checkboxes -->
  <div class="control-group" style="border-left: 1px solid var(--border); padding-left: 16px; margin-left: 4px;">
    <label style="font-weight:700; color:var(--cyan); margin-bottom:2px;">Markets:</label>
  </div>
  {% for _a in ['BTC','ETH','SOL','XRP'] %}
  <div class="control-group">
    <label style="display:flex; align-items:center; gap:6px; cursor:pointer; font-size:13px; font-weight:600; padding:4px 8px; border:1px solid var(--border); border-radius:6px; background:var(--bg);" id="market-label-{{ _a }}">
      <input type="checkbox" id="inp-enabled-{{ _a }}" {{ 'checked' if sv_asset_config.get(_a, {}).get('enabled') else '' }} title="Enable/disable {{ _a }}" onchange="updateMarketLabel('{{ _a }}')" style="width:16px; height:16px; cursor:pointer;">
      {{ _a }}
    </label>
  </div>
  {% endfor %}

  <!-- OB Filter (hidden inputs — keep functionality but remove from main bar) -->
  <input type="hidden" id="inp-ob-filter" {{ 'checked' if sv_ob_enabled else '' }}>
  <input type="hidden" id="inp-ob-min-size" value="{{ sv_ob_min_size }}">
  <input type="hidden" id="inp-ob-max-imbalance" value="{{ sv_ob_max_imbalance }}">

  <button class="btn btn-apply" id="btn-apply" onclick="applyParams()">Apply</button>
  <div class="cost-preview">
    Max Cost/mkt: <span class="val" id="cost-preview">--</span>
    &nbsp;|&nbsp; Payout: <span class="val" id="payout-preview">--</span>
    &nbsp;|&nbsp; Profit: <span class="val" id="profit-preview">--</span>
    <span id="cost-per-asset" style="font-size:0.85em; opacity:0.7; margin-left:6px;"></span>
  </div>
  <div style="flex:1"></div>
  <div class="cost-preview" id="asset-ticker" style="text-align:right; min-width:480px; display:flex; gap:12px; flex-wrap:wrap; justify-content:flex-end;">
    <span class="val" id="btc-src" style="font-size:0.75em;opacity:0.6">(REST)</span>
    <span>BTC <span class="val" id="tick-BTC-price">--</span> <span class="val" id="tick-BTC-dist" style="font-size:0.85em">--</span></span>
    <span>ETH <span class="val" id="tick-ETH-price">--</span> <span class="val" id="tick-ETH-dist" style="font-size:0.85em">--</span></span>
    <span>SOL <span class="val" id="tick-SOL-price">--</span> <span class="val" id="tick-SOL-dist" style="font-size:0.85em">--</span></span>
    <span>XRP <span class="val" id="tick-XRP-price">--</span> <span class="val" id="tick-XRP-dist" style="font-size:0.85em">--</span></span>
  </div>
  <button class="btn {{ 'btn-stop' if sv_running else 'btn-start' }}" id="btn-run" onclick="toggleBot()">{{ 'STOP BOT' if sv_running else 'START BOT' }}</button>
</div>

<!-- Per-asset hidden inputs (all synced from global controls) -->
{% for _a in ['BTC','ETH','SOL','XRP'] %}
{% set _c = sv_asset_config.get(_a, {}) %}
<input type="hidden" id="inp-ac-{{ _a }}-bid_price" value="0.01">
<input type="hidden" id="inp-ac-{{ _a }}-tokens_per_side" value="{{ _c.get('tokens_per_side', 100) }}">
<input type="hidden" id="inp-ac-{{ _a }}-bid_window_open" value="{{ _c.get('bid_window_open', 30) }}">
<input type="hidden" id="inp-ac-{{ _a }}-dollar_range" value="0">
<input type="hidden" id="inp-ac-{{ _a }}-distance_pct_max" value="0">
<input type="hidden" id="inp-ac-{{ _a }}-candle_range_max" value="0">
<input type="hidden" id="inp-ac-{{ _a }}-expansion_max" value="0">
<input type="hidden" id="inp-ac-{{ _a }}-expansion_floor" value="0">
{% endfor %}

<!-- ── Stats ── -->
<div style="padding: 12px 24px 0 24px">
<div class="stats-row">
  <div class="stat-card"><div class="stat-label">Markets</div><div class="stat-value cyan" id="st-markets">0</div></div>
  <div class="stat-card"><div class="stat-label">Bids Today</div><div class="stat-value yellow" id="st-bids">0</div></div>
  <div class="stat-card"><div class="stat-label">Fills</div><div class="stat-value green" id="st-fills">0</div></div>
  <div class="stat-card"><div class="stat-label">Spent</div><div class="stat-value yellow" id="st-spent">$0</div></div>
  <div class="stat-card"><div class="stat-label">Total Payout</div><div class="stat-value green" id="st-payout">$0</div></div>
  <div class="stat-card"><div class="stat-label">Profit</div><div class="stat-value" id="st-profit">$0</div></div>
  <div class="stat-card" style="display:flex; align-items:center; justify-content:center;">
    <button onclick="window.location='/export-trades'" class="btn" style="background:var(--cyan); color:var(--bg); font-size:12px; padding:8px 16px; font-weight:700; cursor:pointer; border-radius:4px;">&#x1F4E5; Export Trades</button>
  </div>
</div>
</div>

<!-- ── AI Learner Panel ── -->
<div class="learner-section">
  <div class="learner-grid">
    <!-- Learner Stats -->
    <div class="card">
      <div class="card-title" style="display:flex; justify-content:space-between; align-items:center;">
        <span>AI Learner</span>
        <span class="gate-status gate-off" id="ai-gate-status">OFF</span>
      </div>
      <div class="card-body">
        <div class="learner-stats">
          <div class="learner-stat"><div class="ls-label">Trades</div><div class="ls-value cyan" id="ai-trades">0</div></div>
          <div class="learner-stat"><div class="ls-label">Both Fill</div><div class="ls-value green" id="ai-both">0</div></div>
          <div class="learner-stat"><div class="ls-label">BF Rate</div><div class="ls-value yellow" id="ai-rate">0%</div></div>
          <div class="learner-stat"><div class="ls-label">Skips</div><div class="ls-value red" id="ai-skips">0</div></div>
          <div class="learner-stat"><div class="ls-label">Allows</div><div class="ls-value green" id="ai-allows">0</div></div>
          <div class="learner-stat"><div class="ls-label">Overrides</div><div class="ls-value" style="color:var(--dim)" id="ai-overrides">0</div></div>
        </div>
        <div style="margin-top:8px; display:flex; justify-content:space-between; font-size:11px;">
          <span style="color:var(--dim)">Gated P&L: <span id="ai-gated-pnl" style="font-family:monospace;font-weight:700">$0</span></span>
          <span style="color:var(--dim)">Warm-up: <span id="ai-warmup" style="color:var(--yellow);font-family:monospace">-</span></span>
        </div>
        <div class="learner-controls">
          <button class="btn-sm btn-toggle-off" id="ai-toggle" onclick="toggleLearner()">OFF</button>
          <label>Threshold</label>
          <input type="range" id="ai-threshold" min="0" max="100" value="25" oninput="updateThresholdLabel()">
          <span class="range-val" id="ai-threshold-val">25%</span>
          <button class="btn-sm btn-apply" onclick="applyLearnerParams()" style="font-size:10px;padding:3px 8px;">APPLY</button>
          <div style="flex:1"></div>
          <button class="btn-sm btn-reset" onclick="if(confirm('Reset AI model weights?')) socket.emit('reset_learner')">RESET</button>
        </div>
      </div>
    </div>

    <!-- Feature Weights -->
    <div class="card">
      <div class="card-title">Top Feature Weights</div>
      <div class="card-body" id="ai-weights" style="font-size:11px;">
        <div class="empty" style="padding:10px">No data yet</div>
      </div>
    </div>

    <!-- Recent Predictions -->
    <div class="card">
      <div class="card-title">Recent Predictions</div>
      <div class="card-body" style="padding:0; max-height:160px; overflow-y:auto;">
        <table class="pred-table">
          <thead><tr><th>Time</th><th>Market</th><th>Prob</th><th>Result</th></tr></thead>
          <tbody id="ai-predictions">
            <tr><td colspan="4" class="empty" style="padding:10px">No predictions yet</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</div>

<!-- ── Main Grid ── -->
<div class="grid">
  <!-- Markets Table -->
  <div class="card markets-card">
    <div class="card-title">Active Markets</div>
    <div class="card-body" style="padding:0; overflow-x:auto;">
      <table>
        <thead>
          <tr>
            <th>Market</th>
            <th style="text-align:right">Closes In</th>
            <th style="text-align:right">Up Ask</th>
            <th style="text-align:right">Down Ask</th>
            <th style="text-align:right">Combined</th>
            <th style="text-align:right">Age</th>
            <th style="text-align:center">Status</th>
          </tr>
        </thead>
        <tbody id="markets-body">
          <tr><td colspan="7" class="empty">Waiting for markets...</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- Activity Log -->
  <div class="card log-card">
    <div class="card-title">Activity Log</div>
    <div class="card-body log-body" id="log-body">
      <div class="empty">Waiting for activity...</div>
    </div>
  </div>
</div>
</div><!-- /tab-limit-bid -->

<!-- ══════ TAB 2: MARKET SCANNER ══════ -->
<div class="tab-content" id="tab-scanner">

<!-- Scanner Filter Controls -->
<div class="scanner-controls">
  <div>
    <div class="scanner-section-label">Scan Filters</div>
    <div class="scanner-filter-group">
      <div class="control-group">
        <label>Max Hours to Expiry</label>
        <input type="number" id="scan-max-hours" step="0.5" min="0.1" max="168" value="{{ sv_scanner_cfg.max_hours }}">
      </div>
      <div class="control-group">
        <label>Min Liquidity $</label>
        <input type="number" id="scan-min-liq" step="1" min="0" max="100000" value="{{ sv_scanner_cfg.min_liquidity }}">
      </div>
      <div class="control-group">
        <label>Max Liquidity $</label>
        <input type="number" id="scan-max-liq" step="10" min="0" max="999999" value="{{ sv_scanner_cfg.max_liquidity }}">
      </div>
      <div class="control-group">
        <label>Scan Interval (sec)</label>
        <input type="number" id="scan-interval" step="5" min="10" max="300" value="{{ sv_scanner_cfg.scan_interval }}">
      </div>
    </div>
  </div>
  <div>
    <div class="scanner-section-label">Categories</div>
    <div class="scanner-cat-row" id="scan-cat-row">
      <label><input type="checkbox" value="crypto-5m" class="scan-cat-cb"> Crypto 5m</label>
      <label><input type="checkbox" value="crypto-15m" class="scan-cat-cb"> Crypto 15m</label>
      <label><input type="checkbox" value="sports" class="scan-cat-cb"> Sports</label>
      <label><input type="checkbox" value="esports" class="scan-cat-cb"> Esports</label>
      <label><input type="checkbox" value="over-under" class="scan-cat-cb"> Over/Under</label>
      <label><input type="checkbox" value="weather" class="scan-cat-cb"> Weather</label>
      <label><input type="checkbox" value="other" class="scan-cat-cb"> Other</label>
    </div>
  </div>
  <div style="display:flex; align-items:flex-end; gap:8px; margin-left:auto;">
    <button class="btn btn-apply" onclick="applyScannerParams()">Apply</button>
    <button class="btn btn-start" id="btn-scanner" onclick="toggleScanner()">START SCANNER</button>
  </div>
</div>

<!-- Scanner Stats Bar -->
<div class="scanner-stats-bar">
  <div class="stat-card">
    <div class="stat-label">Markets Found</div>
    <div class="stat-value cyan" id="scan-st-found">0</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Scanner Status</div>
    <div class="stat-value" id="scan-st-status"><span class="scanner-status off">STOPPED</span></div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Last Scan</div>
    <div class="stat-value" id="scan-st-last" style="font-size:13px; color:var(--dim)">--</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Auto-Bid</div>
    <div class="stat-value" id="scan-st-autobid"><span class="scanner-status off">OFF</span></div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Categories</div>
    <div class="stat-value" id="scan-st-cats" style="font-size:12px; color:var(--dim)">--</div>
  </div>
</div>

<!-- Auto-Bid Configuration -->
<div style="padding: 12px 24px 0 24px;">
  <div class="scanner-autobid-section">
    <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:8px;">
      <div class="scanner-section-label" style="margin:0">Auto-Bid Settings</div>
      <button class="btn-sm" id="btn-scan-autobid" onclick="toggleScannerAutoBid()" style="min-width:60px;">OFF</button>
    </div>
    <div class="scanner-filter-group">
      <div class="control-group">
        <label>Bid Price $</label>
        <input type="number" id="scan-ab-price" step="0.01" min="0.01" max="0.50" value="{{ sv_scanner_cfg.auto_bid_price }}">
      </div>
      <div class="control-group">
        <label>Tokens/Side</label>
        <input type="number" id="scan-ab-size" step="10" min="10" max="10000" value="{{ sv_scanner_cfg.auto_bid_size }}">
      </div>
      <div class="control-group">
        <label>Max Ask $</label>
        <input type="number" id="scan-ab-max-ask" step="0.01" min="0.01" max="0.50" value="{{ sv_scanner_cfg.auto_bid_max_ask }}">
      </div>
      <div class="control-group">
        <label>Min Liquidity $</label>
        <input type="number" id="scan-ab-min-liq" step="1" min="0" max="10000" value="{{ sv_scanner_cfg.auto_bid_min_liq }}">
      </div>
    </div>
    <div style="margin-top:6px;">
      <div class="scanner-section-label">Auto-Bid Categories</div>
      <div class="scanner-cat-row" id="scan-ab-cat-row">
        <label><input type="checkbox" value="crypto-5m" class="scan-ab-cat-cb"> Crypto 5m</label>
        <label><input type="checkbox" value="crypto-15m" class="scan-ab-cat-cb"> Crypto 15m</label>
        <label><input type="checkbox" value="sports" class="scan-ab-cat-cb"> Sports</label>
        <label><input type="checkbox" value="esports" class="scan-ab-cat-cb"> Esports</label>
        <label><input type="checkbox" value="over-under" class="scan-ab-cat-cb"> Over/Under</label>
        <label><input type="checkbox" value="weather" class="scan-ab-cat-cb"> Weather</label>
        <label><input type="checkbox" value="other" class="scan-ab-cat-cb"> Other</label>
      </div>
    </div>
    <button class="btn btn-apply" onclick="applyScannerParams()" style="margin-top:8px;">Apply</button>
  </div>
</div>

<!-- Scanner Results Table -->
<div class="scanner-results-wrap">
  <div class="card">
    <div class="card-title">Scanned Markets <span id="scan-count-label" style="color:var(--dim);font-size:11px;margin-left:8px;">(0)</span></div>
    <div class="card-body" style="padding:0; max-height:500px; overflow-y:auto;">
      <table>
        <thead>
          <tr>
            <th>Market</th>
            <th>Category</th>
            <th style="text-align:right">Time Left</th>
            <th style="text-align:right">Liquidity</th>
            <th style="text-align:right">Ask YES</th>
            <th style="text-align:right">Ask NO</th>
            <th style="text-align:right">Combined</th>
            <th style="text-align:center">Bid $</th>
            <th style="text-align:center">Shares</th>
            <th style="text-align:center">Action</th>
          </tr>
        </thead>
        <tbody id="scan-results-body">
          <tr><td colspan="10" class="empty">Scanner not started</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</div>

<!-- Scanner Log -->
<div style="padding: 0 24px 24px 24px;">
  <div class="card log-card" style="grid-column:auto;">
    <div class="card-title">Scanner Log</div>
    <div class="card-body log-body" id="scan-log-body">
      <div class="empty">Waiting for scanner activity...</div>
    </div>
  </div>
</div>

</div><!-- /tab-scanner -->

<!-- ══════ TAB 4: AFTER-HOURS FILLS ══════ -->
<div class="tab-content" id="tab-afterhours">

<!-- Filter Controls -->
<div class="ah-controls">
  <div class="control-group">
    <label>Asset</label>
    <select id="ah-filter-asset" style="background:var(--bg);border:1px solid var(--border);color:var(--cyan);padding:6px 10px;border-radius:4px;font-size:13px;font-family:monospace;">
      <option value="ALL">ALL</option>
      <option value="BTC">BTC</option>
      <option value="ETH">ETH</option>
      <option value="SOL">SOL</option>
      <option value="XRP">XRP</option>
    </select>
  </div>
  <div class="control-group">
    <label>Side</label>
    <select id="ah-filter-side" style="background:var(--bg);border:1px solid var(--border);color:var(--cyan);padding:6px 10px;border-radius:4px;font-size:13px;font-family:monospace;">
      <option value="ALL">ALL</option>
      <option value="UP">UP only</option>
      <option value="DOWN">DOWN only</option>
    </select>
  </div>
  <div class="control-group">
    <label>Source</label>
    <select id="ah-filter-source" style="background:var(--bg);border:1px solid var(--border);color:var(--cyan);padding:6px 10px;border-radius:4px;font-size:13px;font-family:monospace;">
      <option value="ALL">All fills</option>
      <option value="OBSERVED" selected>Observed (all markets)</option>
      <option value="OURS">Our bids only</option>
    </select>
  </div>
  <div class="control-group">
    <label title="Max fill price. 0 = all. E.g. 0.03 to see cheap fills only.">Max Price $</label>
    <input type="number" id="ah-filter-max-price" step="0.01" min="0" max="1" value="0"
      title="Only show fills at or below this price. 0 = show all. 0.03 = cheap fills."
      style="width:80px;">
  </div>
  <div class="control-group">
    <label>Result</label>
    <select id="ah-filter-result" style="background:var(--bg);border:1px solid var(--border);color:var(--cyan);padding:6px 10px;border-radius:4px;font-size:13px;font-family:monospace;">
      <option value="ALL">ALL</option>
      <option value="WIN">Wins only</option>
      <option value="LOSS">Losses only</option>
      <option value="PENDING">Pending</option>
    </select>
  </div>
  <div class="control-group">
    <label style="display:flex;align-items:center;gap:4px;cursor:pointer;">
      <input type="checkbox" id="ah-filter-online" title="Only show fills where price was within 25% of candle range from open">
      Near Line
    </label>
  </div>
  <button class="btn btn-apply" onclick="ahApplyFilter()">Filter</button>
  <button class="btn" style="background:#1a6b3c;color:#fff;" onclick="ahExportCSV()">&#x2B73; Export CSV</button>
  <button class="btn" style="background:var(--border);color:var(--text);" onclick="ahClearEvents()">Clear</button>
  <div style="flex:1"></div>
  <div class="cost-preview" style="font-size:11px;">
    Total events: <span class="val" id="ah-total-count">0</span>
    &nbsp;|&nbsp; Observed: <span class="val cyan" id="ah-obs-count">0</span>
    &nbsp;|&nbsp; Our Fills: <span class="val green" id="ah-our-count">0</span>
  </div>
</div>

<!-- Stats Bar -->
<div class="ah-stats-bar">
  <div class="stat-card"><div class="stat-label">Total Observed</div><div class="stat-value cyan" id="ah-st-total">0</div></div>
  <div class="stat-card"><div class="stat-label">UP Fills</div><div class="stat-value green" id="ah-st-up">0</div></div>
  <div class="stat-card"><div class="stat-label">DOWN Fills</div><div class="stat-value red" id="ah-st-down">0</div></div>
  <div class="stat-card"><div class="stat-label">Wins</div><div class="stat-value green" id="ah-st-wins">0</div></div>
  <div class="stat-card"><div class="stat-label">Losses</div><div class="stat-value red" id="ah-st-losses">0</div></div>
  <div class="stat-card"><div class="stat-label">Cheap (&le;$0.02)</div><div class="stat-value yellow" id="ah-st-cheap">0</div></div>
  <div class="stat-card"><div class="stat-label">Cheap Wins</div><div class="stat-value green" id="ah-st-cheap-wins">0</div></div>
  <div class="stat-card"><div class="stat-label">Markets Monitored</div><div class="stat-value" id="ah-st-markets">0</div></div>
</div>

<!-- Breakdown Cards -->
<div class="ah-breakdown">
  <!-- Win/Loss Analysis -->
  <div class="ah-breakdown-card">
    <h4>Post-Close Fill Analysis</h4>
    <div class="ah-breakdown-row"><span class="ah-breakdown-label">Fills on winning side</span><span class="ah-breakdown-val green" id="ah-bk-wins">0</span></div>
    <div class="ah-breakdown-row"><span class="ah-breakdown-label">Fills on losing side</span><span class="ah-breakdown-val red" id="ah-bk-losses">0</span></div>
    <div class="ah-breakdown-row"><span class="ah-breakdown-label">Pending resolution</span><span class="ah-breakdown-val" id="ah-bk-pending">0</span></div>
    <div class="ah-breakdown-row" style="border-top:1px solid var(--border);margin-top:6px;padding-top:6px;">
      <span class="ah-breakdown-label">Win-side fill rate</span>
      <span class="ah-breakdown-val green" id="ah-bk-win-rate">--</span>
    </div>
  </div>
  <!-- Cheap Fill Pattern -->
  <div class="ah-breakdown-card">
    <h4>Cheap Fill Pattern (&le; $0.02)</h4>
    <div class="ah-breakdown-row"><span class="ah-breakdown-label">Total cheap fills</span><span class="ah-breakdown-val yellow" id="ah-bk-cheap-total">0</span></div>
    <div class="ah-breakdown-row"><span class="ah-breakdown-label">Cheap on winning side</span><span class="ah-breakdown-val green" id="ah-bk-cheap-wins">0</span></div>
    <div class="ah-breakdown-row"><span class="ah-breakdown-label">Cheap on losing side</span><span class="ah-breakdown-val red" id="ah-bk-cheap-losses">0</span></div>
    <div class="ah-breakdown-row" style="border-top:1px solid var(--border);margin-top:6px;padding-top:6px;">
      <span class="ah-breakdown-label">Cheap win-side rate</span>
      <span class="ah-breakdown-val green" id="ah-bk-cheap-rate">--</span>
    </div>
  </div>
  <!-- By Asset -->
  <div class="ah-breakdown-card">
    <h4>Fills by Asset</h4>
    <div class="ah-breakdown-row"><span class="ah-breakdown-label">BTC</span><span class="ah-breakdown-val cyan" id="ah-bk-asset-BTC">0</span></div>
    <div class="ah-breakdown-row"><span class="ah-breakdown-label">ETH</span><span class="ah-breakdown-val cyan" id="ah-bk-asset-ETH">0</span></div>
    <div class="ah-breakdown-row"><span class="ah-breakdown-label">SOL</span><span class="ah-breakdown-val cyan" id="ah-bk-asset-SOL">0</span></div>
    <div class="ah-breakdown-row"><span class="ah-breakdown-label">XRP</span><span class="ah-breakdown-val cyan" id="ah-bk-asset-XRP">0</span></div>
  </div>
  <!-- Timing Pattern -->
  <div class="ah-breakdown-card">
    <h4>Timing Breakdown</h4>
    <div class="ah-breakdown-row"><span class="ah-breakdown-label">0-15s after close</span><span class="ah-breakdown-val" id="ah-bk-t15">0</span></div>
    <div class="ah-breakdown-row"><span class="ah-breakdown-label">15-30s after close</span><span class="ah-breakdown-val" id="ah-bk-t30">0</span></div>
    <div class="ah-breakdown-row"><span class="ah-breakdown-label">30-60s after close</span><span class="ah-breakdown-val" id="ah-bk-t60">0</span></div>
    <div class="ah-breakdown-row"><span class="ah-breakdown-label">60-90s after close</span><span class="ah-breakdown-val" id="ah-bk-t90">0</span></div>
  </div>
</div>

<!-- Expand/Collapse All -->
<div class="ah-group-expand-all">
  <button onclick="ahExpandAll()">&#x25BC; Expand All</button>
  <button onclick="ahCollapseAll()">&#x25B6; Collapse All</button>
  <span style="font-size:11px;color:var(--dim);margin-left:8px;font-family:monospace;" id="ah-group-count">0 timeframes</span>
</div>

<!-- Grouped Events (accordion) -->
<div class="ah-groups-wrap" id="ah-groups-container">
  <div style="text-align:center;padding:40px;color:var(--dim);font-size:13px;">
    Monitoring all crypto 5m markets for post-close fills. Data appears after each market closes.
  </div>
</div>

</div><!-- /tab-afterhours -->

<!-- ══════ TAB 4: TRADE JOURNAL ══════ -->
<div class="tab-content" id="tab-journal">

<!-- ── Auto-Trade Toggle + Actions Bar ── -->
<div style="display:flex;align-items:center;gap:16px;padding:10px 16px;background:var(--card);border:1px solid var(--border);border-radius:10px;margin-bottom:12px;flex-wrap:wrap;">
  <div style="display:flex;align-items:center;gap:8px;">
    <span style="font-weight:700;color:var(--text);">Auto-Trade:</span>
    <label style="position:relative;display:inline-block;width:48px;height:24px;cursor:pointer;">
      <input type="checkbox" id="tj-auto-toggle" onchange="tjToggleAutoTrade()" style="opacity:0;width:0;height:0;">
      <span style="position:absolute;top:0;left:0;right:0;bottom:0;background:#333;border-radius:12px;transition:.3s;" id="tj-auto-slider"></span>
    </label>
    <span id="tj-auto-label" style="font-size:12px;color:var(--dim);">OFF</span>
  </div>
  <div style="border-left:1px solid var(--border);height:20px;"></div>
  <button class="btn btn-start" onclick="tjCheckFills()" style="padding:4px 12px;font-size:12px;">Check Fills</button>
  <button class="btn" onclick="tjRequestStats()" style="padding:4px 12px;font-size:12px;background:var(--cyan);color:#000;">Refresh Stats</button>
  <button class="btn" onclick="tjExport()" style="padding:4px 12px;font-size:12px;background:#444;color:var(--text);">Export CSV</button>
  <button class="btn btn-stop" onclick="if(confirm('Clear ALL journal entries?')) tjClear()" style="padding:4px 12px;font-size:12px;">Clear All</button>
  <div style="flex:1;"></div>
  <span style="font-size:12px;color:var(--dim);" id="tj-summary">0 trades</span>
</div>

<!-- ── Manual Trade Entry Form ── -->
<div style="background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px 16px;margin-bottom:12px;">
  <div style="font-weight:700;color:var(--green);margin-bottom:8px;font-size:14px;">📝 Log Manual Trade</div>
  <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end;">
    <div style="flex:2;min-width:200px;">
      <label style="font-size:11px;color:var(--dim);display:block;">Market (select from active)</label>
      <select id="tj-market" style="width:100%;padding:6px 8px;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:6px;font-size:12px;">
        <option value="">-- Select Market --</option>
      </select>
    </div>
    <div style="min-width:80px;">
      <label style="font-size:11px;color:var(--dim);display:block;">Side</label>
      <select id="tj-side" style="width:100%;padding:6px 8px;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:6px;font-size:12px;">
        <option value="BOTH">BOTH</option>
        <option value="UP">UP only</option>
        <option value="DOWN">DOWN only</option>
      </select>
    </div>
    <div style="min-width:70px;">
      <label style="font-size:11px;color:var(--dim);display:block;">Price</label>
      <input type="number" id="tj-price" value="0.01" step="0.01" min="0.01" max="0.99" style="width:100%;padding:6px 8px;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:6px;font-size:12px;">
    </div>
    <div style="min-width:70px;">
      <label style="font-size:11px;color:var(--dim);display:block;">Size</label>
      <input type="number" id="tj-size" value="100" step="10" min="1" max="10000" style="width:100%;padding:6px 8px;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:6px;font-size:12px;">
    </div>
    <div style="min-width:100px;">
      <label style="font-size:11px;color:var(--dim);display:block;">Strategy</label>
      <select id="tj-strategy" style="width:100%;padding:6px 8px;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:6px;font-size:12px;">
        <option value="">None</option>
        <option value="line_ride">Line Ride</option>
        <option value="momentum">Momentum</option>
        <option value="ob_read">OB Read</option>
        <option value="scalp">Scalp</option>
        <option value="fade">Fade</option>
        <option value="reversal">Reversal</option>
        <option value="breakout">Breakout</option>
        <option value="quick_snipe">Quick Snipe</option>
      </select>
    </div>
    <div style="min-width:80px;">
      <label style="font-size:11px;color:var(--dim);display:block;">Confidence</label>
      <select id="tj-confidence" style="width:100%;padding:6px 8px;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:6px;font-size:12px;">
        <option value="">-</option>
        <option value="high">High</option>
        <option value="medium">Medium</option>
        <option value="low">Low</option>
      </select>
    </div>
    <div style="flex:2;min-width:150px;">
      <label style="font-size:11px;color:var(--dim);display:block;">Notes / Reason</label>
      <input type="text" id="tj-notes" placeholder="Why this trade?" style="width:100%;padding:6px 8px;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:6px;font-size:12px;">
    </div>
    <button class="btn btn-start" onclick="tjPlaceTrade()" style="padding:6px 16px;font-size:13px;font-weight:700;">🎯 Place & Log</button>
    <button class="btn" onclick="tjLogOnly()" style="padding:6px 12px;font-size:12px;background:#444;color:var(--text);" title="Log without placing order">Log Only</button>
  </div>
</div>

<!-- ── Quick Snipe Bar ── -->
<div style="background:linear-gradient(135deg,#0a1628,#102040);border:2px solid var(--green);border-radius:10px;padding:12px 16px;margin-bottom:12px;box-shadow:0 0 15px rgba(0,255,136,0.15);">
  <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;">
    <div style="font-weight:700;color:var(--green);font-size:15px;white-space:nowrap;">⚡ Quick Snipe</div>
    <div style="display:flex;align-items:center;gap:6px;">
      <label style="font-size:11px;color:var(--dim);white-space:nowrap;">Shares</label>
      <input type="number" id="qs-size" value="100" step="10" min="1" max="10000" style="width:80px;padding:6px 8px;background:var(--bg);color:var(--text);border:1px solid var(--green);border-radius:6px;font-size:14px;font-weight:700;text-align:center;">
    </div>
    <div style="display:flex;align-items:center;gap:6px;">
      <label style="font-size:11px;color:var(--dim);white-space:nowrap;">Price ¢</label>
      <input type="number" id="qs-price" value="1" step="1" min="1" max="99" style="width:70px;padding:6px 8px;background:var(--bg);color:var(--text);border:1px solid var(--green);border-radius:6px;font-size:14px;font-weight:700;text-align:center;">
      <span style="font-size:11px;color:var(--dim);">= $<span id="qs-price-dec">0.01</span></span>
    </div>
    <button onclick="tjQuickSnipe()" style="padding:8px 28px;font-size:15px;font-weight:900;background:linear-gradient(135deg,#00c853,#00e676);color:#000;border:none;border-radius:8px;cursor:pointer;box-shadow:0 0 12px rgba(0,200,83,0.4);transition:all 0.2s;white-space:nowrap;" onmouseover="this.style.transform='scale(1.05)';this.style.boxShadow='0 0 20px rgba(0,200,83,0.6)'" onmouseout="this.style.transform='scale(1)';this.style.boxShadow='0 0 12px rgba(0,200,83,0.4)'">
      🚀 SNIPE BOTH SIDES
    </button>
    <div style="display:flex;align-items:center;gap:6px;">
      <label style="font-size:11px;color:var(--dim);white-space:nowrap;">Side</label>
      <select id="qs-side" style="padding:6px 8px;background:var(--bg);color:var(--text);border:1px solid var(--green);border-radius:6px;font-size:12px;">
        <option value="BOTH" selected>BOTH</option>
        <option value="UP">UP only</option>
        <option value="DOWN">DOWN only</option>
      </select>
    </div>
    <div style="flex:1;"></div>
    <div id="qs-cost" style="font-size:13px;color:var(--cyan);font-weight:700;white-space:nowrap;">Cost: $2.00</div>
    <div id="qs-status" style="font-size:12px;color:var(--dim);min-width:120px;text-align:right;"></div>
  </div>
</div>

<!-- ── Stats Panel ── -->
<div id="tj-stats-panel" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:8px;margin-bottom:12px;">
  <div class="stat-card" style="text-align:center;padding:8px;">
    <div class="stat-label">Total Trades</div>
    <div class="stat-value" id="tj-st-total">0</div>
  </div>
  <div class="stat-card" style="text-align:center;padding:8px;">
    <div class="stat-label">Open</div>
    <div class="stat-value" id="tj-st-open" style="color:var(--cyan);">0</div>
  </div>
  <div class="stat-card" style="text-align:center;padding:8px;">
    <div class="stat-label">Wins</div>
    <div class="stat-value green" id="tj-st-wins">0</div>
  </div>
  <div class="stat-card" style="text-align:center;padding:8px;">
    <div class="stat-label">Losses</div>
    <div class="stat-value red" id="tj-st-losses">0</div>
  </div>
  <div class="stat-card" style="text-align:center;padding:8px;">
    <div class="stat-label">Win Rate</div>
    <div class="stat-value" id="tj-st-winrate">0%</div>
  </div>
  <div class="stat-card" style="text-align:center;padding:8px;">
    <div class="stat-label">Total PnL</div>
    <div class="stat-value" id="tj-st-pnl">$0.00</div>
  </div>
  <div class="stat-card" style="text-align:center;padding:8px;">
    <div class="stat-label">Best Trade</div>
    <div class="stat-value green" id="tj-st-best">$0.00</div>
  </div>
  <div class="stat-card" style="text-align:center;padding:8px;">
    <div class="stat-label">Worst Trade</div>
    <div class="stat-value red" id="tj-st-worst">$0.00</div>
  </div>
</div>

<!-- ── Pattern Comparison (Wins vs Losses) ── -->
<div id="tj-patterns" style="display:none;background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px 16px;margin-bottom:12px;">
  <div style="font-weight:700;color:var(--cyan);margin-bottom:8px;font-size:14px;">🔍 Pattern Analysis: Winners vs Losers</div>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;">
    <div>
      <div style="font-weight:600;color:var(--green);margin-bottom:6px;font-size:13px;">✅ Winner Patterns</div>
      <div id="tj-win-patterns" style="font-size:12px;color:var(--dim);line-height:1.8;"></div>
    </div>
    <div>
      <div style="font-weight:600;color:var(--red);margin-bottom:6px;font-size:13px;">❌ Loser Patterns</div>
      <div id="tj-loss-patterns" style="font-size:12px;color:var(--dim);line-height:1.8;"></div>
    </div>
  </div>
</div>

<!-- ── Per-Asset Breakdown ── -->
<div id="tj-asset-breakdown" style="display:none;background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px 16px;margin-bottom:12px;">
  <div style="font-weight:700;color:var(--yellow);margin-bottom:8px;font-size:14px;">📊 Per-Asset Breakdown</div>
  <div id="tj-asset-table-wrap"></div>
</div>

<!-- ── Trade Table ── -->
<div style="background:var(--card);border:1px solid var(--border);border-radius:10px;overflow:hidden;">
  <div style="padding:8px 16px;display:flex;align-items:center;gap:12px;border-bottom:1px solid var(--border);">
    <span style="font-weight:700;color:var(--text);font-size:14px;">Trade History</span>
    <select id="tj-filter-status" onchange="tjRenderTable()" style="padding:4px 8px;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:6px;font-size:11px;">
      <option value="all">All Status</option>
      <option value="open">Open</option>
      <option value="filled">Filled</option>
      <option value="resolved">Resolved</option>
      <option value="cancelled">Cancelled</option>
    </select>
    <select id="tj-filter-outcome" onchange="tjRenderTable()" style="padding:4px 8px;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:6px;font-size:11px;">
      <option value="all">All Outcomes</option>
      <option value="WIN">Wins</option>
      <option value="LOSS">Losses</option>
      <option value="BOTH">Both Filled</option>
      <option value="pending">Pending</option>
    </select>
    <select id="tj-filter-asset" onchange="tjRenderTable()" style="padding:4px 8px;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:6px;font-size:11px;">
      <option value="all">All Assets</option>
      <option value="BTC">BTC</option>
      <option value="ETH">ETH</option>
      <option value="SOL">SOL</option>
      <option value="XRP">XRP</option>
    </select>
    <div style="flex:1;"></div>
    <span style="font-size:11px;color:var(--dim);" id="tj-filter-count">Showing 0 trades</span>
  </div>
  <div style="overflow-x:auto;max-height:500px;overflow-y:auto;">
    <table class="data-table" style="width:100%;font-size:11px;">
      <thead>
        <tr style="position:sticky;top:0;background:var(--card);z-index:1;">
          <th style="min-width:65px;">Time</th>
          <th style="min-width:40px;">Asset</th>
          <th style="min-width:35px;">Side</th>
          <th style="min-width:45px;">Price</th>
          <th style="min-width:35px;">Size</th>
          <th style="min-width:50px;">Cost</th>
          <th style="min-width:55px;">Status</th>
          <th style="min-width:50px;">Outcome</th>
          <th style="min-width:50px;">PnL</th>
          <th style="min-width:60px;">OB State</th>
          <th style="min-width:50px;">Distance</th>
          <th style="min-width:55px;">Range%</th>
          <th style="min-width:50px;">Secs Left</th>
          <th style="min-width:60px;">Strategy</th>
          <th style="min-width:50px;">Conf</th>
          <th style="min-width:100px;">Notes</th>
          <th style="min-width:80px;">Actions</th>
        </tr>
      </thead>
      <tbody id="tj-table-body">
        <tr><td colspan="17" class="empty">No journal entries yet</td></tr>
      </tbody>
    </table>
  </div>
</div>

<!-- ── Expanded Trade Detail (hidden by default, shown on click) ── -->
<div id="tj-detail-panel" style="display:none;background:var(--card);border:1px solid var(--green);border-radius:10px;padding:16px;margin-top:12px;">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
    <span style="font-weight:700;color:var(--green);font-size:14px;">📋 Trade Detail</span>
    <button onclick="document.getElementById('tj-detail-panel').style.display='none'" style="background:none;border:none;color:var(--dim);cursor:pointer;font-size:16px;">✕</button>
  </div>
  <div id="tj-detail-content" style="display:grid;grid-template-columns:1fr 1fr;gap:12px;font-size:12px;"></div>
</div>

</div><!-- /tab-journal -->

<!-- ══════ TAB 5: ARB ENGINE ══════ -->
<div class="tab-content" id="tab-arb">

<!-- ── Arb Controls ── -->
<div style="display:flex;align-items:center;gap:16px;padding:12px 16px;background:var(--card);border:1px solid var(--border);border-radius:10px;margin-bottom:12px;flex-wrap:wrap;">
  <div style="display:flex;align-items:center;gap:8px;">
    <span style="color:var(--purple);font-weight:700;">Combined-Ask Arbitrage</span>
    <label style="display:flex;align-items:center;gap:6px;cursor:pointer;">
      <input type="checkbox" id="arb-enabled" onchange="arbToggle(this.checked)"
             style="width:18px;height:18px;accent-color:var(--purple);">
      <span id="arb-status-label" style="font-size:12px;color:var(--dim);">OFF</span>
    </label>
  </div>
  <div style="flex:1;"></div>
  <span id="arb-stats-summary" style="font-size:12px;color:var(--dim);"></span>
</div>

<!-- ── Arb Parameters ── -->
<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px;margin-bottom:12px;">
  <div class="controls" style="padding:12px;">
    <label style="font-size:11px;color:var(--dim);">Min Edge (profit/share)</label>
    <div style="display:flex;gap:6px;align-items:center;">
      <span style="color:var(--dim);">$</span>
      <input type="number" id="arb-min-edge" step="0.005" min="0.005" max="0.20" value="0.03"
             style="background:var(--bg);border:1px solid var(--border);color:var(--text);padding:6px 8px;border-radius:6px;width:80px;font-size:13px;">
      <button onclick="arbSaveParams()" style="background:var(--purple);color:#fff;border:none;padding:4px 10px;border-radius:6px;cursor:pointer;font-size:11px;">Set</button>
    </div>
  </div>
  <div class="controls" style="padding:12px;">
    <label style="font-size:11px;color:var(--dim);">Trade Size ($/arb)</label>
    <div style="display:flex;gap:6px;align-items:center;">
      <span style="color:var(--dim);">$</span>
      <input type="number" id="arb-trade-size" step="0.50" min="0.50" max="100" value="5.00"
             style="background:var(--bg);border:1px solid var(--border);color:var(--text);padding:6px 8px;border-radius:6px;width:80px;font-size:13px;">
    </div>
  </div>
  <div class="controls" style="padding:12px;">
    <label style="font-size:11px;color:var(--dim);">Max Positions</label>
    <input type="number" id="arb-max-pos" step="1" min="1" max="10" value="3"
           style="background:var(--bg);border:1px solid var(--border);color:var(--text);padding:6px 8px;border-radius:6px;width:60px;font-size:13px;">
  </div>
  <div class="controls" style="padding:12px;">
    <label style="font-size:11px;color:var(--dim);">Cooldown (s)</label>
    <input type="number" id="arb-cooldown" step="5" min="5" max="300" value="30"
           style="background:var(--bg);border:1px solid var(--border);color:var(--text);padding:6px 8px;border-radius:6px;width:60px;font-size:13px;">
  </div>
  <div class="controls" style="padding:12px;">
    <label style="font-size:11px;color:var(--dim);">Fill Timeout (s)</label>
    <input type="number" id="arb-timeout" step="5" min="10" max="120" value="45"
           style="background:var(--bg);border:1px solid var(--border);color:var(--text);padding:6px 8px;border-radius:6px;width:60px;font-size:13px;">
  </div>
  <div class="controls" style="padding:12px;">
    <label style="font-size:11px;color:var(--dim);">Daily Cap ($)</label>
    <div style="display:flex;gap:6px;align-items:center;">
      <span style="color:var(--dim);">$</span>
      <input type="number" id="arb-daily-cap" step="1" min="1" max="500" value="25"
             style="background:var(--bg);border:1px solid var(--border);color:var(--text);padding:6px 8px;border-radius:6px;width:70px;font-size:13px;">
    </div>
  </div>
</div>

<!-- ── Live Combined Asks (shows edge on all watched markets) ── -->
<div style="background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px;margin-bottom:12px;">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
    <span style="font-weight:700;color:var(--purple);font-size:13px;">Live Combined Asks</span>
    <span style="font-size:11px;color:var(--dim);">Green = arb opportunity (edge ≥ min)</span>
  </div>
  <div id="arb-live-markets" style="max-height:280px;overflow-y:auto;font-size:12px;"></div>
</div>

<!-- ── Arb Positions (active + recent resolved) ── -->
<div style="background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px;">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
    <span style="font-weight:700;color:var(--purple);font-size:13px;">Arb Positions</span>
    <span id="arb-pnl-summary" style="font-size:12px;color:var(--dim);"></span>
  </div>
  <table class="data-table" style="width:100%;font-size:11px;">
    <thead><tr>
      <th>Asset</th><th>Combined</th><th>Edge</th><th>Shares</th>
      <th>Cost</th><th>Up</th><th>Down</th><th>Profit</th><th>Status</th>
    </tr></thead>
    <tbody id="arb-positions-body"></tbody>
  </table>
</div>

</div><!-- /tab-arb -->

<script>
const socket = io();
let botRunning = {{ 'true' if sv_running else 'false' }};
let scannerRunning = false;
let scannerAutoBid = {{ 'true' if sv_scanner_auto_bid else 'false' }};
let socketConnected = false;

// ── Tab Switching ──
function switchTab(tab) {
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
  document.getElementById('tab-btn-' + tab).classList.add('active');
  document.getElementById('tab-' + tab).classList.add('active');
}

// ── Connection status feedback ──
socket.on('connect', () => {
  socketConnected = true;
  console.log('SocketIO connected');
  document.getElementById('btn-run').style.opacity = '1';
});
socket.on('disconnect', () => {
  socketConnected = false;
  console.log('SocketIO disconnected');
  document.getElementById('btn-run').style.opacity = '0.5';
});
socket.on('connect_error', (err) => {
  console.error('SocketIO connect error:', err);
  document.getElementById('btn-run').style.opacity = '0.5';
});

// ── Dirty-flag for inputs: block server overwrites while user is editing ──
const dirtyInputs = {};
function markDirty(id) { dirtyInputs[id] = Date.now(); }
function isDirty(id) { return dirtyInputs[id] && (Date.now() - dirtyInputs[id]) < 3000; }
// Build list of per-asset config input IDs for dirty tracking
const _acFields = ['bid_price','tokens_per_side','bid_window_open','dollar_range','distance_pct_max','candle_range_max','expansion_max','expansion_floor'];
const _acAssets = ['BTC','ETH','SOL','XRP'];
const _acInputIds = [];
_acAssets.forEach(a => _acFields.forEach(f => _acInputIds.push('inp-ac-' + a + '-' + f)));

['inp-price','inp-tokens','inp-window-open','inp-window-close',
 'inp-ob-min-size','inp-ob-max-imbalance',
 'scan-max-hours','scan-min-liq','scan-max-liq','scan-interval',
 'scan-ab-price','scan-ab-size','scan-ab-max-ask','scan-ab-min-liq'].concat(_acInputIds).forEach(id => {
  const el = document.getElementById(id);
  if (!el) return;
  el.addEventListener('focus', () => markDirty(id));
  el.addEventListener('input', () => markDirty(id));
  el.addEventListener('keydown', () => markDirty(id));
  el.addEventListener('mousedown', () => markDirty(id));
});

// ── Market toggle label styling ──
function updateMarketLabel(asset) {
  const cb = document.getElementById('inp-enabled-' + asset);
  const lbl = document.getElementById('market-label-' + asset);
  if (!cb || !lbl) return;
  if (cb.checked) {
    lbl.style.borderColor = 'var(--green)';
    lbl.style.background = 'rgba(0,200,100,0.10)';
    lbl.style.opacity = '1';
  } else {
    lbl.style.borderColor = '#555';
    lbl.style.background = 'rgba(255,255,255,0.03)';
    lbl.style.opacity = '0.5';
  }
  updateCostPreview();
}
// Init all market labels on load
_acAssets.forEach(a => updateMarketLabel(a));

// ── Scanner: Initialize category checkboxes from server ──
(function initScannerCats() {
  const cats = {{ sv_scanner_cfg.categories | tojson }};
  const abCats = {{ sv_scanner_cfg.auto_bid_categories | tojson }};
  document.querySelectorAll('.scan-cat-cb').forEach(cb => {
    cb.checked = cats.includes(cb.value);
  });
  document.querySelectorAll('.scan-ab-cat-cb').forEach(cb => {
    cb.checked = abCats.includes(cb.value);
  });
  // Init auto-bid button state
  const abBtn = document.getElementById('btn-scan-autobid');
  if (scannerAutoBid) {
    abBtn.textContent = 'ON'; abBtn.className = 'btn-sm btn-toggle-on';
  } else {
    abBtn.textContent = 'OFF'; abBtn.className = 'btn-sm btn-toggle-off';
  }
})();

// ── Scanner Functions ──
function applyScannerParams() {
  const cats = [];
  document.querySelectorAll('.scan-cat-cb').forEach(cb => { if (cb.checked) cats.push(cb.value); });
  const abCats = [];
  document.querySelectorAll('.scan-ab-cat-cb').forEach(cb => { if (cb.checked) abCats.push(cb.value); });
  socket.emit('update_scanner_params', {
    max_hours: parseFloat(document.getElementById('scan-max-hours').value),
    min_liquidity: parseFloat(document.getElementById('scan-min-liq').value),
    max_liquidity: parseFloat(document.getElementById('scan-max-liq').value),
    scan_interval: parseInt(document.getElementById('scan-interval').value),
    categories: cats,
    auto_bid_price: parseFloat(document.getElementById('scan-ab-price').value),
    auto_bid_size: parseInt(document.getElementById('scan-ab-size').value),
    auto_bid_max_ask: parseFloat(document.getElementById('scan-ab-max-ask').value),
    auto_bid_min_liq: parseFloat(document.getElementById('scan-ab-min-liq').value),
    auto_bid_categories: abCats
  });
}

function toggleScanner() {
  if (scannerRunning) {
    socket.emit('stop_scanner');
  } else {
    socket.emit('start_scanner');
  }
}

function toggleScannerAutoBid() {
  socket.emit('toggle_scanner_auto_bid');
}

function scannerManualBid(rowIdx, marketId, tokenYes, tokenNo, question) {
  const priceEl = document.getElementById('scan-row-price-' + rowIdx);
  const sizeEl = document.getElementById('scan-row-size-' + rowIdx);
  const price = priceEl ? parseFloat(priceEl.value) || 0.05 : 0.05;
  const size = sizeEl ? parseInt(sizeEl.value) || 100 : 100;
  socket.emit('scanner_manual_bid', {
    market_id: marketId,
    token_id_yes: tokenYes,
    token_id_no: tokenNo,
    question: question,
    bid_price: price,
    tokens: size
  });
}

// ── Cost preview live update ──
function updateCostPreview() {
  const tokens = parseInt(document.getElementById('inp-tokens').value) || 0;
  let enabledCount = 0;
  const names = [];
  _acAssets.forEach(a => {
    const enEl = document.getElementById('inp-enabled-' + a);
    if (enEl && enEl.checked) { enabledCount++; names.push(a); }
  });
  const totalCost = 0.01 * tokens * 2 * enabledCount;
  const totalPayout = tokens * 1.0 * enabledCount;
  const totalProfit = totalPayout - totalCost;
  if (enabledCount === 0) {
    document.getElementById('cost-preview').textContent = '--';
    document.getElementById('payout-preview').textContent = '--';
    document.getElementById('profit-preview').textContent = '--';
    document.getElementById('cost-per-asset').textContent = '(no assets enabled)';
  } else {
    document.getElementById('cost-preview').textContent = '$' + totalCost.toFixed(2) + (enabledCount > 1 ? ' total' : '');
    document.getElementById('payout-preview').textContent = '$' + totalPayout.toFixed(2);
    document.getElementById('profit-preview').textContent = '$' + totalProfit.toFixed(2);
    document.getElementById('cost-per-asset').textContent = enabledCount > 1 ? '(' + names.join(' | ') + ')' : '';
  }
}
// Listen to shares input + market toggles
document.getElementById('inp-tokens').addEventListener('input', updateCostPreview);
_acAssets.forEach(a => {
  const en = document.getElementById('inp-enabled-' + a);
  if (en) en.addEventListener('change', updateCostPreview);
});
updateCostPreview();

// ── Apply params ──
function applyParams() {
  const price = 0.01;  // Always fixed at $0.01
  const tokens = parseInt(document.getElementById('inp-tokens').value);
  const windowOpen = parseInt(document.getElementById('inp-window-open').value);
  const windowClose = parseInt(document.getElementById('inp-window-close').value);
  const obEnabled = document.getElementById('inp-ob-filter').checked;
  const obMinSize = parseFloat(document.getElementById('inp-ob-min-size').value);
  const obMaxImbalance = parseFloat(document.getElementById('inp-ob-max-imbalance').value);

  // Propagate global values to all per-asset hidden inputs
  _acAssets.forEach(a => {
    const bp = document.getElementById('inp-ac-' + a + '-bid_price');
    if (bp) bp.value = 0.01;
    const tk = document.getElementById('inp-ac-' + a + '-tokens_per_side');
    if (tk) tk.value = tokens;
    const wo = document.getElementById('inp-ac-' + a + '-bid_window_open');
    if (wo) wo.value = windowOpen;
  });

  // Build full per-asset config from hidden inputs
  const assetConfig = {};
  _acAssets.forEach(a => {
    assetConfig[a] = {
      enabled: document.getElementById('inp-enabled-' + a).checked,
      bid_price: 0.01,
      tokens_per_side: tokens,
      bid_window_open: windowOpen,
      dollar_range: parseFloat(document.getElementById('inp-ac-' + a + '-dollar_range').value) || 0,
      distance_pct_max: parseFloat(document.getElementById('inp-ac-' + a + '-distance_pct_max').value) || 0,
      candle_range_max: parseFloat(document.getElementById('inp-ac-' + a + '-candle_range_max').value) || 0,
      expansion_max: parseFloat(document.getElementById('inp-ac-' + a + '-expansion_max').value) || 0,
      expansion_floor: parseFloat(document.getElementById('inp-ac-' + a + '-expansion_floor').value) || 0,
    };
  });

  socket.emit('update_params', {
    bid_price: price,
    tokens_per_side: tokens,
    bid_window_open: windowOpen,
    bid_window_close: windowClose,
    asset_config: assetConfig,
    ob_filter_enabled: obEnabled,
    ob_min_size: obMinSize,
    ob_max_imbalance: obMaxImbalance
  });
}

// ── Start / Stop ──
function toggleBot() {
  if (botRunning) {
    socket.emit('stop_bot');
  } else {
    socket.emit('start_bot');
  }
}

// ── Format time ──
function fmtTime(secs) {
  if (secs <= 0) return '0s';
  const m = Math.floor(secs / 60);
  const s = Math.floor(secs % 60);
  return m > 0 ? m + ':' + String(s).padStart(2, '0') : s + 's';
}

// ── Receive full state ──
socket.on('state', (d) => {
  botRunning = d.running;

  // Run button
  const btn = document.getElementById('btn-run');
  if (d.running) {
    btn.textContent = 'STOP BOT';
    btn.className = 'btn btn-stop';
  } else {
    btn.textContent = 'START BOT';
    btn.className = 'btn btn-start';
  }

  // Mode badge
  const mb = document.getElementById('mode-badge');
  mb.textContent = d.mode;
  mb.className = d.mode === 'LIVE' ? 'mode-badge mode-live' : 'mode-badge mode-paper';

  // Clock
  document.getElementById('clock').textContent = d.time_utc;

  // WS dot (OB feed)
  const dot = document.getElementById('ws-dot');
  const wl = document.getElementById('ws-label');
  if (d.ws_connected) {
    dot.className = 'status-dot on';
    wl.textContent = 'OB: ' + d.ws_active + '/' + d.ws_subscribed + ' active';
    if (d.ws_idle > 0) wl.textContent += ' (' + d.ws_idle + ' idle)';
    if (d.ws_never > 0) wl.textContent += ' (' + d.ws_never + ' no data)';
  } else {
    dot.className = 'status-dot off';
    wl.textContent = 'OB: OFF';
  }

  // Params sync — bid price is always $0.01 (hidden), only sync tokens + window
  document.getElementById('inp-price').value = 0.01;
  if (!isDirty('inp-tokens') && document.activeElement.id !== 'inp-tokens')
    document.getElementById('inp-tokens').value = d.tokens_per_side;
  if (!isDirty('inp-window-open') && document.activeElement.id !== 'inp-window-open')
    document.getElementById('inp-window-open').value = d.bid_window_open;
  document.getElementById('inp-window-close').value = d.bid_window_close;
  // Per-asset config sync (all hidden inputs + market toggle checkboxes)
  if (d.asset_config) {
    _acAssets.forEach(a => {
      const cfg = d.asset_config[a];
      if (!cfg) return;
      const enEl = document.getElementById('inp-enabled-' + a);
      if (enEl) enEl.checked = cfg.enabled;
      // Sync all hidden per-asset inputs
      _acFields.forEach(f => {
        const el = document.getElementById('inp-ac-' + a + '-' + f);
        if (el && cfg[f] !== undefined) el.value = (f === 'bid_price') ? 0.01 : cfg[f];
      });
      updateMarketLabel(a);
    });
  }
  document.getElementById('inp-ob-filter').checked = d.ob_filter_enabled;
  document.getElementById('inp-ob-min-size').value = d.ob_min_size;
  document.getElementById('inp-ob-max-imbalance').value = d.ob_max_imbalance;
  updateCostPreview();

  // BTC WS source indicator (WS vs REST fallback) — prices come via price_tick
  const btcSrc = document.getElementById('btc-src');
  if (d.btc_ws_connected && d.btc_ws_age >= 0 && d.btc_ws_age < 5) {
    btcSrc.textContent = '(WS)';
    btcSrc.style.color = 'var(--green)';
  } else {
    btcSrc.textContent = '(REST)';
    btcSrc.style.color = 'var(--yellow)';
  }

  // Stats
  document.getElementById('st-markets').textContent = d.markets_watched;
  document.getElementById('st-bids').textContent = d.bids_today;
  document.getElementById('st-fills').textContent = d.fills_detected;
  document.getElementById('st-spent').textContent = '$' + d.total_spent.toFixed(2);
  document.getElementById('st-payout').textContent = '$' + d.total_payout.toFixed(2);

  const profitEl = document.getElementById('st-profit');
  const profitVal = d.total_profit;
  profitEl.textContent = (profitVal >= 0 ? '+$' : '-$') + Math.abs(profitVal).toFixed(2);
  profitEl.className = 'stat-value ' + (profitVal > 0 ? 'green' : profitVal < 0 ? 'red' : '');

  // Markets table
  const tbody = document.getElementById('markets-body');
  if (!d.markets || d.markets.length === 0) {
    tbody.innerHTML = '<tr><td colspan="7" class="empty">No active markets</td></tr>';
  } else {
    let html = '';
    // Build bid lookup for OB imbalance display
    const bidMap = {};
    if (d.bids) d.bids.forEach(b => { bidMap[b.market_id] = b; });

    d.markets.filter(m => m.status !== 'watching').forEach(m => {
      const secs = m.secs;
      const timeClass = secs <= 30 ? 'hot' : secs <= 60 ? 'warm' : 'cool';
      const combClass = m.combined > 0 && m.combined < 0.50 ? 'good' :
                        m.combined > 0 && m.combined < 1.0 ? 'mid' : 'high';
      const ageStr = m.price_age < 1 ? m.price_age.toFixed(1)+'s' :
                     m.price_age < 10 ? m.price_age.toFixed(0)+'s' : '>10s';
      const ageClass = m.price_age < 2 ? 'good' : m.price_age < 5 ? 'mid' : 'high';

      // OB imbalance badge for active bids
      let obBadge = '';
      const bid = bidMap[m.market_id];
      if (bid && bid.ob_imbalance > 0 && !bid.cancelled) {
        const imb = bid.ob_imbalance;
        const imbClass = imb > 5 ? 'high' : imb > 2 ? 'mid' : 'good';
        obBadge = ' <span class="price ' + imbClass + '" style="font-size:0.8em">' +
          bid.ob_heavy_side + ' ' + imb.toFixed(1) + 'x</span>';
      }

      html += '<tr>' +
        '<td>' + (m.name || '--') + '</td>' +
        '<td class="time-cell ' + timeClass + '" style="text-align:right">' + fmtTime(secs) + '</td>' +
        '<td class="price" style="text-align:right">' + (m.up_ask > 0 ? '$'+m.up_ask.toFixed(3) : '--') + '</td>' +
        '<td class="price" style="text-align:right">' + (m.down_ask > 0 ? '$'+m.down_ask.toFixed(3) : '--') + '</td>' +
        '<td class="price ' + combClass + '" style="text-align:right">' + (m.combined > 0 ? '$'+m.combined.toFixed(3) : '--') + '</td>' +
        '<td class="price ' + ageClass + '" style="text-align:right">' + ageStr + '</td>' +
        '<td style="text-align:center"><span class="badge badge-' + m.status + '">' +
          m.status.replace('_', ' ') + '</span>' + obBadge + '</td>' +
        '</tr>';
    });
    tbody.innerHTML = html;
  }

  // Activity log
  const logEl = document.getElementById('log-body');
  if (d.activity && d.activity.length > 0) {
    let logHtml = '';
    d.activity.forEach(e => {
      logHtml += '<div class="log-line"><span class="log-ts">' + e.ts +
        '</span><span class="log-msg ' + e.level + '">' + e.msg + '</span></div>';
    });
    logEl.innerHTML = logHtml;
  }

  // ── AI Learner Panel ──
  if (d.learner) {
    const L = d.learner;
    document.getElementById('ai-trades').textContent = L.total_trades;
    document.getElementById('ai-both').textContent = L.both_fills;
    document.getElementById('ai-rate').textContent = L.both_fill_rate + '%';
    document.getElementById('ai-skips').textContent = L.learner_skips;
    document.getElementById('ai-allows').textContent = L.learner_allows;
    document.getElementById('ai-overrides').textContent = L.learner_overrides;

    const gPnl = document.getElementById('ai-gated-pnl');
    gPnl.textContent = (L.gated_pnl >= 0 ? '+$' : '-$') + Math.abs(L.gated_pnl).toFixed(2);
    gPnl.style.color = L.gated_pnl >= 0 ? 'var(--green)' : 'var(--red)';

    const warmEl = document.getElementById('ai-warmup');
    if (L.warm_up_remaining > 0) {
      warmEl.textContent = L.warm_up_remaining + ' left';
    } else if (L.min_samples_remaining > 0) {
      warmEl.textContent = 'min ' + L.min_samples_remaining + ' left';
    } else {
      warmEl.textContent = 'ACTIVE';
      warmEl.style.color = 'var(--green)';
    }

    // Gate status badge
    const gs = document.getElementById('ai-gate-status');
    if (!L.enabled) {
      gs.textContent = 'DISABLED'; gs.className = 'gate-status gate-off';
    } else if (L.is_gating) {
      gs.textContent = 'GATING'; gs.className = 'gate-status gate-on';
    } else {
      gs.textContent = 'WARM-UP'; gs.className = 'gate-status gate-warmup';
    }

    // Toggle button
    const tb = document.getElementById('ai-toggle');
    tb.textContent = L.enabled ? 'ON' : 'OFF';
    tb.className = L.enabled ? 'btn-sm btn-toggle-on' : 'btn-sm btn-toggle-off';

    // Threshold slider (don't update while user is dragging)
    const slider = document.getElementById('ai-threshold');
    if (document.activeElement !== slider) {
      slider.value = Math.round(L.confidence_threshold * 100);
      document.getElementById('ai-threshold-val').textContent = Math.round(L.confidence_threshold * 100) + '%';
    }

    // Feature weights bars
    if (L.top_features && L.top_features.length > 0) {
      const maxW = Math.max(...L.top_features.map(f => Math.abs(f.weight)), 0.01);
      let wHtml = '';
      L.top_features.slice(0, 6).forEach(f => {
        const pct = Math.min(Math.abs(f.weight) / maxW * 100, 100);
        const cls = f.weight >= 0 ? 'pos' : 'neg';
        const valColor = f.weight >= 0 ? 'var(--green)' : 'var(--red)';
        wHtml += '<div class="weight-bar-container">' +
          '<span class="weight-bar-label">' + f.name + '</span>' +
          '<div class="weight-bar-wrap"><div class="weight-bar ' + cls + '" style="width:' + pct.toFixed(0) + '%"></div></div>' +
          '<span class="weight-bar-val" style="color:' + valColor + '">' + f.weight.toFixed(3) + '</span>' +
          '</div>';
      });
      document.getElementById('ai-weights').innerHTML = wHtml;
    }

    // Recent predictions
    if (d.learner_predictions && d.learner_predictions.length > 0) {
      let pHtml = '';
      d.learner_predictions.slice().reverse().forEach(p => {
        const cls = p.allowed ? 'pred-allow' : 'pred-skip';
        const icon = p.allowed ? 'ALLOW' : 'SKIP';
        pHtml += '<tr>' +
          '<td>' + p.ts + '</td>' +
          '<td style="max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + p.market + '</td>' +
          '<td>' + p.prob + '%</td>' +
          '<td class="' + cls + '">' + icon + '</td>' +
          '</tr>';
      });
      document.getElementById('ai-predictions').innerHTML = pHtml;
    }
  }

  // ══════════ SCANNER TAB RENDERING ══════════
  scannerRunning = d.scanner_running || false;
  scannerAutoBid = d.scanner_auto_bid || false;

  // Scanner button
  const scanBtn = document.getElementById('btn-scanner');
  if (scannerRunning) {
    scanBtn.textContent = 'STOP SCANNER';
    scanBtn.className = 'btn btn-stop';
  } else {
    scanBtn.textContent = 'START SCANNER';
    scanBtn.className = 'btn btn-start';
  }

  // Scanner status badge
  const scanStatusEl = document.getElementById('scan-st-status');
  if (scannerRunning) {
    scanStatusEl.innerHTML = '<span class="scanner-status on">RUNNING</span>';
  } else {
    scanStatusEl.innerHTML = '<span class="scanner-status off">STOPPED</span>';
  }

  // Auto-bid badge + button
  const abBadge = document.getElementById('scan-st-autobid');
  const abBtn2 = document.getElementById('btn-scan-autobid');
  if (scannerAutoBid) {
    abBadge.innerHTML = '<span class="scanner-status on">ON</span>';
    abBtn2.textContent = 'ON'; abBtn2.className = 'btn-sm btn-toggle-on';
  } else {
    abBadge.innerHTML = '<span class="scanner-status off">OFF</span>';
    abBtn2.textContent = 'OFF'; abBtn2.className = 'btn-sm btn-toggle-off';
  }

  // Last scan time
  if (d.scanner_last_scan > 0) {
    const ago = Math.round((Date.now() / 1000) - d.scanner_last_scan);
    document.getElementById('scan-st-last').textContent = ago < 5 ? 'just now' : ago + 's ago';
  }

  // Scanner config sync (dirty-flag protected)
  if (d.scanner_cfg) {
    const sc2 = d.scanner_cfg;
    if (!isDirty('scan-max-hours') && document.activeElement.id !== 'scan-max-hours')
      document.getElementById('scan-max-hours').value = sc2.max_hours;
    if (!isDirty('scan-min-liq') && document.activeElement.id !== 'scan-min-liq')
      document.getElementById('scan-min-liq').value = sc2.min_liquidity;
    if (!isDirty('scan-max-liq') && document.activeElement.id !== 'scan-max-liq')
      document.getElementById('scan-max-liq').value = sc2.max_liquidity;
    if (!isDirty('scan-interval') && document.activeElement.id !== 'scan-interval')
      document.getElementById('scan-interval').value = sc2.scan_interval;
    if (!isDirty('scan-ab-price') && document.activeElement.id !== 'scan-ab-price')
      document.getElementById('scan-ab-price').value = sc2.auto_bid_price;
    if (!isDirty('scan-ab-size') && document.activeElement.id !== 'scan-ab-size')
      document.getElementById('scan-ab-size').value = sc2.auto_bid_size;
    if (!isDirty('scan-ab-max-ask') && document.activeElement.id !== 'scan-ab-max-ask')
      document.getElementById('scan-ab-max-ask').value = sc2.auto_bid_max_ask;
    if (!isDirty('scan-ab-min-liq') && document.activeElement.id !== 'scan-ab-min-liq')
      document.getElementById('scan-ab-min-liq').value = sc2.auto_bid_min_liq;

    // Category checkboxes (only sync when not focused)
    if (document.activeElement.className !== 'scan-cat-cb') {
      document.querySelectorAll('.scan-cat-cb').forEach(cb => {
        cb.checked = (sc2.categories || []).includes(cb.value);
      });
    }
    if (document.activeElement.className !== 'scan-ab-cat-cb') {
      document.querySelectorAll('.scan-ab-cat-cb').forEach(cb => {
        cb.checked = (sc2.auto_bid_categories || []).includes(cb.value);
      });
    }

    // Categories stat
    const catCounts = {};
    (d.scanner_results || []).forEach(m => {
      catCounts[m.category] = (catCounts[m.category] || 0) + 1;
    });
    const catParts = Object.entries(catCounts).map(([k,v]) => k + ':' + v);
    document.getElementById('scan-st-cats').textContent = catParts.length > 0 ? catParts.join(', ') : '--';
  }

  // Scanner results table
  const scanResults = d.scanner_results || [];
  document.getElementById('scan-st-found').textContent = scanResults.length;
  document.getElementById('scan-count-label').textContent = '(' + scanResults.length + ')';

  const scanBody = document.getElementById('scan-results-body');
  if (scanResults.length === 0) {
    scanBody.innerHTML = '<tr><td colspan="10" class="empty">' +
      (scannerRunning ? 'Scanning...' : 'Scanner not started') + '</td></tr>';
  } else {
    // Preserve per-row input values across state updates
    const prevPrices = {};
    const prevSizes = {};
    scanBody.querySelectorAll('input[id^="scan-row-price-"]').forEach(el => {
      const idx = el.id.replace('scan-row-price-', '');
      if (document.activeElement === el || el.dataset.dirty === '1') prevPrices[idx] = el.value;
    });
    scanBody.querySelectorAll('input[id^="scan-row-size-"]').forEach(el => {
      const idx = el.id.replace('scan-row-size-', '');
      if (document.activeElement === el || el.dataset.dirty === '1') prevSizes[idx] = el.value;
    });

    const defaultPrice = parseFloat(document.getElementById('scan-ab-price').value) || 0.05;
    const defaultSize = parseInt(document.getElementById('scan-ab-size').value) || 100;
    let sHtml = '';
    scanResults.forEach((m, idx) => {
      const secsLeft = Math.max(0, Math.round((new Date(m.end_time).getTime() / 1000) - (Date.now() / 1000)));
      const timeClass = secsLeft <= 60 ? 'hot' : secsLeft <= 300 ? 'warm' : 'cool';
      const askY = m.best_ask_yes > 0 ? '$' + m.best_ask_yes.toFixed(3) : '--';
      const askN = m.best_ask_no > 0 ? '$' + m.best_ask_no.toFixed(3) : '--';
      const combined = (m.best_ask_yes > 0 && m.best_ask_no > 0) ?
        (m.best_ask_yes + m.best_ask_no) : 0;
      const combStr = combined > 0 ? '$' + combined.toFixed(3) : '--';
      const combClass = combined > 0 && combined < 0.50 ? 'good' :
                         combined > 0 && combined < 1.0 ? 'mid' : 'high';
      const catCls = m.category.replace(/[^a-z0-9-]/g, '');
      const question = (m.question || '').length > 60 ?
        m.question.substring(0, 57) + '...' : (m.question || '--');
      // Escape for JS onclick
      const escQ = (m.question || '').replace(/'/g, "\\'").replace(/"/g, '&quot;');
      // Use preserved value if user was editing, otherwise default
      const rowPrice = (idx in prevPrices) ? prevPrices[idx] : defaultPrice;
      const rowSize = (idx in prevSizes) ? prevSizes[idx] : defaultSize;
      sHtml += '<tr>' +
        '<td title="' + (m.question || '').replace(/"/g, '&quot;') + '">' + question + '</td>' +
        '<td><span class="cat-badge ' + catCls + '">' + m.category + '</span></td>' +
        '<td class="time-cell ' + timeClass + '" style="text-align:right">' + fmtTime(secsLeft) + '</td>' +
        '<td style="text-align:right">$' + (m.liquidity || 0).toFixed(0) + '</td>' +
        '<td class="price" style="text-align:right">' + askY + '</td>' +
        '<td class="price" style="text-align:right">' + askN + '</td>' +
        '<td class="price ' + combClass + '" style="text-align:right">' + combStr + '</td>' +
        '<td style="text-align:center">' +
          '<input type="number" class="scan-row-input" id="scan-row-price-' + idx + '" ' +
          'step="0.01" min="0.01" max="0.50" value="' + rowPrice + '" ' +
          'oninput="this.dataset.dirty=1">' +
        '</td>' +
        '<td style="text-align:center">' +
          '<input type="number" class="scan-row-input" id="scan-row-size-' + idx + '" ' +
          'step="10" min="10" max="10000" value="' + rowSize + '" ' +
          'oninput="this.dataset.dirty=1">' +
        '</td>' +
        '<td style="text-align:center">' +
          '<button class="btn-bid-sm" onclick="scannerManualBid(' + idx + ',\'' + m.market_id + '\',\'' +
          m.token_id_yes + '\',\'' + m.token_id_no + '\',\'' + escQ + '\')">' +
          'BID</button>' +
        '</td>' +
        '</tr>';
    });
    scanBody.innerHTML = sHtml;
  }

  // ══════════ AFTER-HOURS FILLS TAB ══════════
  // AH events now delivered via separate socket events (ah_events / ah_new_event)
  // Just update the badge count from the lightweight ah_count field
  const ahBadge = document.getElementById('ah-tab-badge');
  if (ahBadge && d.ah_count != null) ahBadge.textContent = d.ah_count;

  // ══════════ TRADE JOURNAL TAB ══════════
  if (d.tj_entries) {
    tjEntries = d.tj_entries;
    tjRenderTable();
  }
  if (d.tj_count !== undefined) {
    const tjBadge = document.getElementById('tj-tab-badge');
    if (tjBadge) tjBadge.textContent = d.tj_count;
    const tjSummary = document.getElementById('tj-summary');
    if (tjSummary) tjSummary.textContent = d.tj_count + ' trades';
  }
  if (d.auto_trade_enabled !== undefined) {
    tjSyncAutoToggle(d.auto_trade_enabled);
  }
  if (d.all_markets) {
    tjPopulateMarkets(d.all_markets);
  }

  // ══════════ ARB ENGINE TAB ══════════
  if (d.arb) {
    arbUpdateUI(d.arb, d.arb_positions || [], d.markets || []);
  }
});

// ── AH events: full list on connect, incremental on new fill ──
socket.on('ah_events', function(events) {
  ahRenderAll(events);
});
socket.on('ah_new_event', function(ev) {
  _ahAllEvents.unshift(ahNorm(ev));
  const badge = document.getElementById('ah-tab-badge');
  if (badge) badge.textContent = _ahAllEvents.length;
  ahUpdateStats(_ahAllEvents);
  ahApplyFilter();
});
socket.on('ah_resolution', function(data) {
  // Update local AH events with resolution outcome
  const mid = data.market_id, winner = data.winner;
  _ahAllEvents.forEach(function(e) {
    if (e.market_id !== mid) return;
    if (e.won === 'BOTH') return;
    e.winner = winner;
    if (e.side === 'BOTH') e.won = 'BOTH';
    else if (e.side === 'UP') e.won = (winner === 'Up') ? 'WIN' : 'LOSS';
    else if (e.side === 'DOWN') e.won = (winner === 'Down') ? 'WIN' : 'LOSS';
    else e.won = 'pending';
  });
  ahUpdateStats(_ahAllEvents);
  ahApplyFilter();
});

// ── After-Hours Fills: state & render ──
let _ahAllEvents = [];   // full list from server (both ObservedFill and AfterHoursEvent)

// Normalise event: handle both ObservedFill and legacy AfterHoursEvent field names
function ahNorm(e) {
  return {
    ts_str:        e.ts_str || '--',
    asset:         e.asset  || '--',
    question:      e.question || '',
    side:          e.side   || '--',
    fill_price:    e.fill_price != null ? e.fill_price : (e.bid_price || 0),
    fill_amount:   e.fill_amount != null ? e.fill_amount : (e.fill_size || 0),
    fill_cost:     e.fill_cost != null ? e.fill_cost : ((e.bid_price || 0) * (e.fill_size || 0)),
    secs_after:    e.secs_after_close != null ? e.secs_after_close : (e.secs_to_close != null ? -e.secs_to_close : 0),
    snap_label:    e.snap_label || (e.secs_to_close != null ? 'legacy' : '--'),
    winner:        e.winner || '--',
    won:           e.won || 'pending',
    up_ask:        e.up_ask || 0,
    down_ask:      e.down_ask || 0,
    up_spread:     e.up_spread || 0,
    down_spread:   e.down_spread || 0,
    up_best_bid:   e.up_best_bid || 0,
    down_best_bid: e.down_best_bid || 0,
    up_ask_depth:  e.up_ask_depth || 0,
    down_ask_depth: e.down_ask_depth || 0,
    up_bid_depth:  e.up_bid_depth || 0,
    down_bid_depth: e.down_bid_depth || 0,
    up_ask_levels: e.up_ask_levels || 0,
    down_ask_levels: e.down_ask_levels || 0,
    up_bid_levels: e.up_bid_levels || 0,
    down_bid_levels: e.down_bid_levels || 0,
    range_pct:     e.range_pct != null ? e.range_pct : -1,
    on_the_line:   e.on_the_line || false,
    is_ours:       e.is_ours != null ? e.is_ours : true,   // legacy events are always our bids
    market_id:     e.market_id || '',
    _raw:          e
  };
}

function ahRenderAll(events) {
  _ahAllEvents = (events || []).map(ahNorm);
  // Update tab badge
  const badge = document.getElementById('ah-tab-badge');
  if (badge) badge.textContent = _ahAllEvents.length;

  // Top-bar counters
  const obsCount = _ahAllEvents.filter(e => !e.is_ours).length;
  const ourCount = _ahAllEvents.filter(e => e.is_ours).length;
  const el = id => document.getElementById(id);
  if (el('ah-total-count')) el('ah-total-count').textContent = _ahAllEvents.length;
  if (el('ah-obs-count'))   el('ah-obs-count').textContent  = obsCount;
  if (el('ah-our-count'))   el('ah-our-count').textContent  = ourCount;

  ahUpdateStats(_ahAllEvents);
  ahApplyFilter();   // renders filtered table
}

function ahUpdateStats(events) {
  const el = id => document.getElementById(id);
  if (!el('ah-st-total')) return;

  const upEvs    = events.filter(e => e.side === 'UP');
  const downEvs  = events.filter(e => e.side === 'DOWN');
  const winEvs   = events.filter(e => e.won === 'WIN');
  const lossEvs  = events.filter(e => e.won === 'LOSS');
  const cheapEvs = events.filter(e => e.fill_price <= 0.02);
  const cheapWins = cheapEvs.filter(e => e.won === 'WIN');

  // Unique markets monitored
  const uniqMarkets = new Set(events.map(e => e.market_id)).size;

  el('ah-st-total').textContent  = events.length;
  el('ah-st-up').textContent     = upEvs.length;
  el('ah-st-down').textContent   = downEvs.length;
  el('ah-st-wins').textContent   = winEvs.length;
  el('ah-st-losses').textContent = lossEvs.length;
  el('ah-st-cheap').textContent  = cheapEvs.length;
  el('ah-st-cheap-wins').textContent = cheapWins.length;
  el('ah-st-markets').textContent = uniqMarkets;

  // Breakdown: Post-Close Fill Analysis
  const resolved = winEvs.length + lossEvs.length;
  const pending = events.filter(e => e.won === 'pending').length;
  el('ah-bk-wins').textContent    = winEvs.length;
  el('ah-bk-losses').textContent  = lossEvs.length;
  el('ah-bk-pending').textContent = pending;
  el('ah-bk-win-rate').textContent = resolved > 0 ? Math.round(winEvs.length / resolved * 100) + '%' : '--';

  // Cheap Fill Pattern
  const cheapResolved = cheapEvs.filter(e => e.won === 'WIN' || e.won === 'LOSS');
  const cheapLosses   = cheapEvs.filter(e => e.won === 'LOSS');
  el('ah-bk-cheap-total').textContent  = cheapEvs.length;
  el('ah-bk-cheap-wins').textContent   = cheapWins.length;
  el('ah-bk-cheap-losses').textContent = cheapLosses.length;
  el('ah-bk-cheap-rate').textContent   = cheapResolved.length > 0
    ? Math.round(cheapWins.length / cheapResolved.length * 100) + '%' : '--';

  // By asset
  ['BTC','ETH','SOL','XRP'].forEach(a => {
    const cnt = events.filter(e => e.asset === a).length;
    const aEl = el('ah-bk-asset-' + a);
    if (aEl) aEl.textContent = cnt;
  });

  // Timing breakdown (secs_after is seconds after close)
  const t15 = events.filter(e => e.secs_after >= 0  && e.secs_after < 15).length;
  const t30 = events.filter(e => e.secs_after >= 15 && e.secs_after < 30).length;
  const t60 = events.filter(e => e.secs_after >= 30 && e.secs_after < 60).length;
  const t90 = events.filter(e => e.secs_after >= 60 && e.secs_after <= 90).length;
  if (el('ah-bk-t15')) el('ah-bk-t15').textContent = t15;
  if (el('ah-bk-t30')) el('ah-bk-t30').textContent = t30;
  if (el('ah-bk-t60')) el('ah-bk-t60').textContent = t60;
  if (el('ah-bk-t90')) el('ah-bk-t90').textContent = t90;
}

function ahApplyFilter() {
  const gv = (id, def) => { const x = document.getElementById(id); return x ? x.value : def; };
  const gn = (id) => { const x = document.getElementById(id); return x ? (parseFloat(x.value) || 0) : 0; };
  const gc = (id) => { const x = document.getElementById(id); return x ? x.checked : false; };

  const filterAsset   = gv('ah-filter-asset', 'ALL');
  const filterSide    = gv('ah-filter-side', 'ALL');
  const filterSource  = gv('ah-filter-source', 'ALL');
  const filterMaxPrice = gn('ah-filter-max-price');
  const filterResult  = gv('ah-filter-result', 'ALL');
  const filterOnline  = gc('ah-filter-online');

  let filtered = _ahAllEvents.filter(e => {
    if (filterAsset !== 'ALL' && e.asset !== filterAsset) return false;
    if (filterSide  !== 'ALL' && e.side  !== filterSide)  return false;
    if (filterSource === 'OBSERVED' && e.is_ours) return false;
    if (filterSource === 'OURS'     && !e.is_ours) return false;
    if (filterMaxPrice > 0 && e.fill_price > filterMaxPrice) return false;
    if (filterResult === 'WIN'     && e.won !== 'WIN')     return false;
    if (filterResult === 'LOSS'    && e.won !== 'LOSS')    return false;
    if (filterResult === 'PENDING' && e.won !== 'pending') return false;
    if (filterOnline && !e.on_the_line) return false;
    return true;
  });

  ahRenderGroups(filtered);
}

function ahRenderGroups(events) {
  const container = document.getElementById('ah-groups-container');
  const countEl = document.getElementById('ah-group-count');
  if (!container) return;

  if (events.length === 0) {
    container.innerHTML = '<div style="text-align:center;padding:40px;color:var(--dim);font-size:13px;">No events match current filters.</div>';
    if (countEl) countEl.textContent = '0 assets';
    return;
  }

  // ── Level 1: Group by asset ──
  const ASSET_ORDER = ['BTC', 'ETH', 'SOL', 'XRP'];
  const byAsset = {};
  events.forEach(e => {
    const a = e.asset || 'OTHER';
    if (!byAsset[a]) byAsset[a] = [];
    byAsset[a].push(e);
  });
  const assetKeys = ASSET_ORDER.filter(a => byAsset[a]);
  // Add any non-standard assets
  Object.keys(byAsset).forEach(a => { if (!assetKeys.includes(a)) assetKeys.push(a); });

  let totalMarkets = 0;

  let html = '';
  assetKeys.forEach((asset, aIdx) => {
    const assetEvents = byAsset[asset];

    // ── Level 2: Group by market_id within this asset ──
    const markets = {};
    assetEvents.forEach(e => {
      const key = e.market_id || 'unknown';
      if (!markets[key]) markets[key] = [];
      markets[key].push(e);
    });
    const marketKeys = Object.keys(markets).sort((a, b) => {
      const aTs = Math.max(...markets[a].map(e => e._raw?.ts || 0));
      const bTs = Math.max(...markets[b].map(e => e._raw?.ts || 0));
      return bTs - aTs;
    });
    totalMarkets += marketKeys.length;

    // Asset-level stats
    const aUp = assetEvents.filter(e => e.side === 'UP').length;
    const aDown = assetEvents.filter(e => e.side === 'DOWN').length;
    const aWins = assetEvents.filter(e => e.won === 'WIN').length;
    const aLosses = assetEvents.filter(e => e.won === 'LOSS').length;
    const aCheap = assetEvents.filter(e => e.fill_price <= 0.02).length;

    html += '<div class="ah-asset-group expanded" data-asset="' + asset + '">' +
      '<div class="ah-asset-header" onclick="ahToggleAsset(this)">' +
        '<span class="ah-asset-chevron">&#x25B6;</span>' +
        '<span class="ah-asset-name">' + asset + '</span>' +
        '<span class="ah-asset-meta">' +
          '<span class="am-count">' + marketKeys.length + ' market' + (marketKeys.length !== 1 ? 's' : '') + '</span>' +
          '<span class="am-count">' + assetEvents.length + ' fill' + (assetEvents.length !== 1 ? 's' : '') + '</span>' +
          '<span class="am-up">&#x25B2; ' + aUp + '</span>' +
          '<span class="am-down">&#x25BC; ' + aDown + '</span>' +
          (aCheap > 0 ? '<span style="color:var(--yellow)">' + aCheap + ' cheap</span>' : '') +
          (aWins > 0 ? '<span class="am-wins">' + aWins + 'W</span>' : '') +
          (aLosses > 0 ? '<span class="am-losses">' + aLosses + 'L</span>' : '') +
        '</span>' +
      '</div>' +
      '<div class="ah-asset-body">';

    // Each market under this asset
    marketKeys.forEach((mKey, mIdx) => {
      const evs = markets[mKey];
      const first = evs[0];

      // Extract time window from question
      let timeLabel = first.question;
      const tmMatch = timeLabel.match(/(\d{1,2}:\d{2}[AP]M\s*-\s*\d{1,2}:\d{2}[AP]M\s*ET)/);
      timeLabel = tmMatch ? tmMatch[1] : (timeLabel.length > 45 ? timeLabel.substring(0, 42) + '...' : timeLabel);

      // Stats for market header
      const upCount = evs.filter(e => e.side === 'UP').length;
      const downCount = evs.filter(e => e.side === 'DOWN').length;
      const cheapCount = evs.filter(e => e.fill_price <= 0.02).length;
      const wins = evs.filter(e => e.won === 'WIN').length;
      const losses = evs.filter(e => e.won === 'LOSS').length;
      const pending = evs.filter(e => e.won === 'pending').length;

      let winnerCls = 'pending', winnerText = 'PENDING';
      if (pending === 0 && wins > 0 && losses === 0) { winnerCls = 'win'; winnerText = wins + 'W'; }
      else if (pending === 0 && losses > 0 && wins === 0) { winnerCls = 'loss'; winnerText = losses + 'L'; }
      else if (wins > 0 && losses > 0) { winnerCls = 'mixed'; winnerText = wins + 'W/' + losses + 'L'; }
      else if (pending > 0 && (wins > 0 || losses > 0)) { winnerCls = 'mixed'; winnerText = (wins+losses) + 'R/' + pending + 'P'; }

      const winnerSide = evs.find(e => e.winner !== '--' && e.winner !== 'pending');
      const winnerSideStr = winnerSide ? winnerSide.winner : '';

      html += '<div class="ah-group" data-market="' + mKey + '">' +
        '<div class="ah-group-header" onclick="ahToggleGroup(this)">' +
          '<span class="ah-group-chevron">&#x25B6;</span>' +
          '<span class="ah-group-time">' + timeLabel + '</span>' +
          '<span class="ah-group-meta">' +
            '<span class="gm-fills">' + evs.length + ' fill' + (evs.length !== 1 ? 's' : '') + '</span>' +
            '<span class="gm-up">&#x25B2; ' + upCount + '</span>' +
            '<span class="gm-down">&#x25BC; ' + downCount + '</span>' +
            (cheapCount > 0 ? '<span class="gm-cheap">' + cheapCount + ' cheap</span>' : '') +
            (winnerSideStr ? '<span style="color:var(--dim)">W: <b style="color:' + (winnerSideStr === 'Up' ? 'var(--green)' : 'var(--red)') + '">' + winnerSideStr + '</b></span>' : '') +
          '</span>' +
          '<span class="ah-group-winner ' + winnerCls + '">' + winnerText + '</span>' +
        '</div>' +
        '<div class="ah-group-body">' +
          '<table><thead><tr>' +
            '<th>Time</th><th>Side</th><th>Shares</th><th>Price $</th><th>Cost $</th>' +
            '<th>After Close</th><th>Snap</th><th>Winner</th><th>Result</th>' +
            '<th>UP Ask</th><th>DN Ask</th><th>Spread</th><th>Range %</th><th>Src</th>' +
          '</tr></thead><tbody>';

      evs.forEach(e => {
        const sideCls = 'ah-side ' + e.side;
        const secsStr = e.secs_after.toFixed(0) + 's';
        const priceStr = '$' + e.fill_price.toFixed(2);
        const costStr  = '$' + e.fill_cost.toFixed(2);
        const sharesStr = e.fill_amount > 0 ? Math.round(e.fill_amount).toString() : '--';
        const upAskStr   = e.up_ask   > 0 ? '$' + e.up_ask.toFixed(3)   : '--';
        const downAskStr = e.down_ask > 0 ? '$' + e.down_ask.toFixed(3) : '--';
        // Spread: show spread for the side of this fill
        const spread = e.side === 'UP' ? (e._raw?.up_spread || 0) : (e._raw?.down_spread || 0);
        const spreadStr = spread > 0 ? '$' + spread.toFixed(3) : '--';
        const rangePctStr = (e.range_pct >= 0) ? e.range_pct.toFixed(1) + '%' : '--';
        const rangePctColor = e.range_pct <= 25 ? 'var(--green)' : (e.range_pct <= 50 ? 'var(--yellow)' : 'var(--red)');
        const winnerStr = e.winner !== '--' ? e.winner : '<span style="color:#666">--</span>';
        let resultStr = e.won;
        let resultCls = '';
        if (e.won === 'WIN')  resultCls = 'color:var(--green);font-weight:700';
        else if (e.won === 'LOSS') resultCls = 'color:var(--red);font-weight:700';
        else resultCls = 'color:#666';
        const srcStr = e.is_ours
          ? '<span style="background:var(--green);color:#000;padding:1px 5px;border-radius:3px;font-size:10px">OURS</span>'
          : '<span style="background:var(--border);color:var(--cyan);padding:1px 5px;border-radius:3px;font-size:10px">OBS</span>';

        html += '<tr>' +
          '<td>' + e.ts_str + '</td>' +
          '<td><span class="' + sideCls + '">' + e.side + '</span></td>' +
          '<td>' + sharesStr + '</td>' +
          '<td style="color:var(--yellow)">' + priceStr + '</td>' +
          '<td>' + costStr + '</td>' +
          '<td>' + secsStr + '</td>' +
          '<td style="font-size:10px">' + (e.snap_label || '--') + '</td>' +
          '<td>' + winnerStr + '</td>' +
          '<td style="' + resultCls + '">' + resultStr + '</td>' +
          '<td>' + upAskStr + '</td>' +
          '<td>' + downAskStr + '</td>' +
          '<td>' + spreadStr + '</td>' +
          '<td style="color:' + rangePctColor + ';font-weight:700">' + rangePctStr + '</td>' +
          '<td>' + srcStr + '</td>' +
          '</tr>';
      });

      html += '</tbody></table></div></div>';
    });

    html += '</div></div>';  // close ah-asset-body, ah-asset-group
  });

  if (countEl) countEl.textContent = assetKeys.length + ' asset' + (assetKeys.length !== 1 ? 's' : '') + ', ' + totalMarkets + ' market' + (totalMarkets !== 1 ? 's' : '');
  container.innerHTML = html;
}

function ahToggleAsset(headerEl) {
  const group = headerEl.parentElement;
  group.classList.toggle('expanded');
}
function ahToggleGroup(headerEl) {
  const group = headerEl.parentElement;
  group.classList.toggle('expanded');
}
function ahExpandAll() {
  document.querySelectorAll('.ah-asset-group, .ah-group').forEach(g => g.classList.add('expanded'));
}
function ahCollapseAll() {
  document.querySelectorAll('.ah-asset-group, .ah-group').forEach(g => g.classList.remove('expanded'));
}

function ahExportCSV() {
  // Client-side CSV from currently filtered data — includes all columns for pattern analysis
  if (_ahAllEvents.length === 0) { alert('No after-hours data to export.'); return; }
  const cols = ['timestamp','asset','question','market_id','side','fill_price','fill_amount','fill_cost',
    'secs_after_close','snap_label','up_ask','down_ask','up_spread','down_spread',
    'up_best_bid','down_best_bid','up_ask_depth','down_ask_depth','up_bid_depth','down_bid_depth',
    'up_ask_levels','down_ask_levels','up_bid_levels','down_bid_levels',
    'combined_ask','winner','won','is_ours',
    'asset_price','candle_open','price_distance','candle_range','range_pct','on_the_line'];
  const header = cols.join(',');
  const esc = v => { const s = String(v ?? ''); return s.includes(',') || s.includes('"') || s.includes('\n') ? '"' + s.replace(/"/g, '""') + '"' : s; };
  const rows = _ahAllEvents.map(e => {
    const r = e._raw || e;
    return cols.map(c => {
      if (c === 'timestamp') return esc(e.ts_str);
      if (c === 'secs_after_close') return esc(e.secs_after);
      if (c === 'combined_ask') return esc(r.combined_ask || '');
      return esc(r[c] != null ? r[c] : (e[c] != null ? e[c] : ''));
    }).join(',');
  });
  const csv = header + '\n' + rows.join('\n');
  const blob = new Blob([csv], {type: 'text/csv'});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  const d = new Date().toISOString().slice(0,10);
  a.download = 'afterhours_fills_' + d + '.csv';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

function ahClearEvents() {
  _ahAllEvents = [];
  ahRenderAll([]);
  socket.emit('clear_afterhours');
}

// ── Fast per-asset price tick (200ms) ──
socket.on('price_tick', (d) => {
  const assets = ['BTC','ETH','SOL','XRP'];
  assets.forEach(a => {
    const t = d[a];
    if (!t) return;
    const priceEl = document.getElementById('tick-' + a + '-price');
    const distEl = document.getElementById('tick-' + a + '-dist');
    if (priceEl) {
      const fmt = t.p > 10 ? '$' + t.p.toLocaleString() : '$' + t.p.toFixed(t.p > 0.1 ? 4 : 6);
      priceEl.textContent = fmt;
    }
    if (distEl) {
      const dd = Math.abs(t.d);
      const fmtD = dd > 10 ? dd.toFixed(0) : dd > 0.1 ? dd.toFixed(2) : dd.toFixed(4);
      distEl.textContent = (t.d >= 0 ? '+' : '-') + '$' + fmtD;
      // Color: get current asset dollar range from config input
      const rangeEl = document.getElementById('inp-ac-' + a + '-dollar_range');
      const limit = rangeEl ? parseFloat(rangeEl.value) : 0;
      distEl.style.color = (limit > 0 && dd > limit) ? 'var(--red)' : 'var(--green)';
    }
  });
});

// ── Single log push ──
socket.on('log', (e) => {
  const logEl = document.getElementById('log-body');
  const div = document.createElement('div');
  div.className = 'log-line';
  div.innerHTML = '<span class="log-ts">' + e.ts +
    '</span><span class="log-msg ' + e.level + '">' + e.msg + '</span>';
  logEl.prepend(div);
  // Trim to 50
  while (logEl.children.length > 50) logEl.removeChild(logEl.lastChild);
});

// ── Scanner log push ──
socket.on('scanner_log', (e) => {
  const logEl = document.getElementById('scan-log-body');
  const div = document.createElement('div');
  div.className = 'log-line';
  div.innerHTML = '<span class="log-ts">' + e.ts +
    '</span><span class="log-msg ' + (e.level || 'info') + '">' + e.msg + '</span>';
  logEl.prepend(div);
  while (logEl.children.length > 50) logEl.removeChild(logEl.lastChild);
});

// ── AI Learner Controls ──
function toggleLearner() { socket.emit('toggle_learner'); }
function updateThresholdLabel() {
  document.getElementById('ai-threshold-val').textContent = document.getElementById('ai-threshold').value + '%';
}
function applyLearnerParams() {
  const threshold = parseInt(document.getElementById('ai-threshold').value) / 100;
  socket.emit('update_learner_params', { confidence_threshold: threshold });
}

// ══════════════════════════════════════════════════════════════════════════════
//  TRADE JOURNAL JS
// ══════════════════════════════════════════════════════════════════════════════

let tjEntries = [];
let tjStats = null;

// ── Auto-trade toggle ──
function tjToggleAutoTrade() {
  const checked = document.getElementById('tj-auto-toggle').checked;
  socket.emit('toggle_auto_trade', { enabled: checked });
}
function tjSyncAutoToggle(enabled) {
  const tog = document.getElementById('tj-auto-toggle');
  const slider = document.getElementById('tj-auto-slider');
  const label = document.getElementById('tj-auto-label');
  if (tog) tog.checked = enabled;
  if (slider) slider.style.background = enabled ? 'var(--green)' : '#333';
  if (label) {
    label.textContent = enabled ? 'ON' : 'OFF';
    label.style.color = enabled ? 'var(--green)' : 'var(--dim)';
  }
}

// ── Place a manual trade ──
function tjPlaceTrade() {
  const mid = document.getElementById('tj-market').value;
  if (!mid) { alert('Select a market first'); return; }
  socket.emit('tj_place_trade', {
    market_id: mid,
    side: document.getElementById('tj-side').value,
    price: parseFloat(document.getElementById('tj-price').value),
    size: parseInt(document.getElementById('tj-size').value),
    strategy: document.getElementById('tj-strategy').value,
    confidence: document.getElementById('tj-confidence').value,
    notes: document.getElementById('tj-notes').value,
    reason: document.getElementById('tj-notes').value,
  });
  document.getElementById('tj-notes').value = '';
}

// ── Log-only (no order placement) ──
// ── Quick Snipe ──
function tjQuickSnipe() {
  const mid = document.getElementById('tj-market').value;
  if (!mid) { alert('Select a market first'); return; }
  const btn = event.target.closest('button');
  const origText = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '⏳ PLACING...';
  btn.style.background = '#555';
  const statusEl = document.getElementById('qs-status');
  statusEl.textContent = 'Placing orders...';
  statusEl.style.color = 'var(--cyan)';
  socket.emit('tj_quick_snipe', {
    market_id: mid,
    side: document.getElementById('qs-side').value,
    price_cents: parseInt(document.getElementById('qs-price').value),
    size: parseInt(document.getElementById('qs-size').value),
  });
  setTimeout(() => {
    btn.disabled = false;
    btn.innerHTML = origText;
    btn.style.background = 'linear-gradient(135deg,#00c853,#00e676)';
  }, 2000);
}

// Update cost display when inputs change
document.addEventListener('DOMContentLoaded', () => {
  function qsUpdateCost() {
    const price = parseInt(document.getElementById('qs-price')?.value || 1);
    const size = parseInt(document.getElementById('qs-size')?.value || 100);
    const side = document.getElementById('qs-side')?.value || 'BOTH';
    const priceDec = (price / 100).toFixed(2);
    const sides = side === 'BOTH' ? 2 : 1;
    const cost = (price / 100) * size * sides;
    const decEl = document.getElementById('qs-price-dec');
    if (decEl) decEl.textContent = priceDec;
    const costEl = document.getElementById('qs-cost');
    if (costEl) costEl.textContent = 'Cost: $' + cost.toFixed(2);
  }
  ['qs-price','qs-size','qs-side'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('input', qsUpdateCost);
    if (el) el.addEventListener('change', qsUpdateCost);
  });
});

socket.on('qs_result', (d) => {
  const statusEl = document.getElementById('qs-status');
  if (d.ok) {
    statusEl.textContent = d.msg;
    statusEl.style.color = 'var(--green)';
  } else {
    statusEl.textContent = '❌ ' + d.msg;
    statusEl.style.color = 'var(--red)';
  }
  setTimeout(() => { statusEl.textContent = ''; }, 8000);
});

function tjLogOnly() {
  const mid = document.getElementById('tj-market').value;
  if (!mid) { alert('Select a market first'); return; }
  socket.emit('tj_log_only', {
    market_id: mid,
    side: document.getElementById('tj-side').value,
    price: parseFloat(document.getElementById('tj-price').value),
    size: parseInt(document.getElementById('tj-size').value),
    strategy: document.getElementById('tj-strategy').value,
    confidence: document.getElementById('tj-confidence').value,
    notes: document.getElementById('tj-notes').value,
    reason: document.getElementById('tj-notes').value,
  });
  document.getElementById('tj-notes').value = '';
}

// ── Action buttons ──
function tjCheckFills() { socket.emit('tj_check_fills'); }
function tjRequestStats() { socket.emit('tj_get_stats'); }
function tjExport() { socket.emit('tj_export'); }
function tjClear() { socket.emit('tj_clear'); tjEntries = []; tjRenderTable(); }

function tjCancelTrade(id) {
  if (confirm('Cancel this trade and its orders?')) {
    socket.emit('tj_cancel_trade', { id: id });
  }
}
function tjResolveTrade(id, winner) {
  socket.emit('tj_resolve', { id: id, winner: winner });
}

// ── Populate market dropdown from state ──
function tjPopulateMarkets(markets) {
  const sel = document.getElementById('tj-market');
  if (!sel) return;
  const curVal = sel.value;
  let html = '<option value="">-- Select Market --</option>';
  if (markets && markets.length > 0) {
    // Show ALL active markets (not expired), sorted by time remaining (shortest first)
    markets.filter(m => m.secs > 0).sort((a,b) => a.secs - b.secs).forEach(m => {
      let timeStr;
      if (m.secs < 60) timeStr = m.secs.toFixed(0) + 's';
      else if (m.secs < 3600) timeStr = (m.secs/60).toFixed(1) + 'm';
      else timeStr = (m.secs/3600).toFixed(1) + 'h';
      const marker = m.secs < 300 ? '🔴 ' : m.secs < 600 ? '🟡 ' : '';
      html += '<option value="' + m.market_id + '">' + marker + (m.name || m.asset || '?') + ' (' + timeStr + ')</option>';
    });
  }
  sel.innerHTML = html;
  if (curVal) sel.value = curVal;  // preserve selection
}

// ── Render trade table ──
function tjRenderTable() {
  const tbody = document.getElementById('tj-table-body');
  if (!tbody) return;

  const filterStatus = document.getElementById('tj-filter-status').value;
  const filterOutcome = document.getElementById('tj-filter-outcome').value;
  const filterAsset = document.getElementById('tj-filter-asset').value;

  let filtered = tjEntries.filter(t => {
    if (filterStatus !== 'all' && t.status !== filterStatus) return false;
    if (filterOutcome !== 'all' && t.won !== filterOutcome) return false;
    if (filterAsset !== 'all' && t.asset !== filterAsset) return false;
    return true;
  });

  document.getElementById('tj-filter-count').textContent = 'Showing ' + filtered.length + ' trades';

  if (filtered.length === 0) {
    tbody.innerHTML = '<tr><td colspan="17" class="empty">No matching trades</td></tr>';
    return;
  }

  let html = '';
  filtered.forEach(t => {
    const pnlClass = t.pnl > 0 ? 'green' : t.pnl < 0 ? 'red' : '';
    const pnlStr = t.status === 'resolved' ? (t.pnl >= 0 ? '+' : '') + '$' + t.pnl.toFixed(2) : '-';
    const wonBadge = t.won === 'WIN' ? '<span style="color:var(--green);font-weight:700;">WIN</span>' :
                     t.won === 'LOSS' ? '<span style="color:var(--red);font-weight:700;">LOSS</span>' :
                     t.won === 'BOTH' ? '<span style="color:var(--cyan);font-weight:700;">BOTH</span>' :
                     '<span style="color:var(--dim);">-</span>';
    const statusBadge = t.status === 'open' ? '<span class="badge badge-active">open</span>' :
                        t.status === 'filled' ? '<span class="badge badge-filled" style="background:#0a3014;color:var(--green);">filled</span>' :
                        t.status === 'both_filled' ? '<span class="badge" style="background:#0a2030;color:var(--cyan);">both</span>' :
                        t.status === 'resolved' ? '<span class="badge badge-resolved" style="background:#1a1a0a;color:var(--yellow);">done</span>' :
                        t.status === 'cancelled' ? '<span class="badge" style="background:#2a0a0a;color:var(--red);">canx</span>' :
                        '<span class="badge">' + (t.status||'-') + '</span>';
    const obStr = t.up_ask > 0 ? 'U:' + t.up_ask.toFixed(2) + ' D:' + t.down_ask.toFixed(2) : '-';
    const distStr = t.price_distance > 0 ? '$' + t.price_distance.toFixed(2) : '-';
    const rangeStr = t.range_pct > 0 ? (t.range_pct * 100).toFixed(1) + '%' : '-';

    let actions = '';
    if (t.status === 'open' || t.status === 'filled' || t.status === 'both_filled') {
      actions += '<button onclick="tjCancelTrade(\'' + t.id + '\')" style="font-size:10px;padding:2px 6px;background:#3a0a0a;color:var(--red);border:1px solid var(--red);border-radius:4px;cursor:pointer;margin:1px;">Cancel</button> ';
    }
    if ((t.status === 'filled' || t.status === 'both_filled') && t.won === 'pending') {
      actions += '<button onclick="tjResolveTrade(\'' + t.id + '\',\'Up\')" style="font-size:10px;padding:2px 6px;background:#0a3014;color:var(--green);border:1px solid var(--green);border-radius:4px;cursor:pointer;margin:1px;">Up Won</button> ';
      actions += '<button onclick="tjResolveTrade(\'' + t.id + '\',\'Down\')" style="font-size:10px;padding:2px 6px;background:#0a1430;color:var(--cyan);border:1px solid var(--cyan);border-radius:4px;cursor:pointer;margin:1px;">Down Won</button>';
    }
    actions += ' <button onclick="tjShowDetail(\'' + t.id + '\')" style="font-size:10px;padding:2px 6px;background:var(--bg);color:var(--dim);border:1px solid var(--border);border-radius:4px;cursor:pointer;margin:1px;">🔍</button>';

    html += '<tr style="cursor:pointer;" ondblclick="tjShowDetail(\'' + t.id + '\')">' +
      '<td>' + (t.time || '-') + '</td>' +
      '<td style="font-weight:700;">' + (t.asset || '-') + '</td>' +
      '<td>' + (t.side || '-') + '</td>' +
      '<td>$' + (t.price||0).toFixed(2) + '</td>' +
      '<td>' + (t.size||0) + '</td>' +
      '<td>$' + (t.cost||0).toFixed(2) + '</td>' +
      '<td>' + statusBadge + '</td>' +
      '<td>' + wonBadge + '</td>' +
      '<td class="' + pnlClass + '" style="font-weight:700;">' + pnlStr + '</td>' +
      '<td style="font-size:10px;">' + obStr + '</td>' +
      '<td>' + distStr + '</td>' +
      '<td>' + rangeStr + '</td>' +
      '<td>' + (t.secs_to_close > 0 ? t.secs_to_close.toFixed(0) + 's' : '-') + '</td>' +
      '<td style="font-size:10px;">' + (t.strategy || '-') + '</td>' +
      '<td style="font-size:10px;">' + (t.confidence || '-') + '</td>' +
      '<td style="font-size:10px;max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="' + (t.notes||'').replace(/"/g,'&quot;') + '">' + (t.notes || '-') + '</td>' +
      '<td style="white-space:nowrap;">' + actions + '</td>' +
      '</tr>';
  });
  tbody.innerHTML = html;
}

// ── Show full detail for a trade ──
function tjShowDetail(id) {
  const t = tjEntries.find(e => e.id === id);
  if (!t) return;

  const panel = document.getElementById('tj-detail-panel');
  const content = document.getElementById('tj-detail-content');
  panel.style.display = 'block';

  const fmtRow = (label, val, color) => '<div style="padding:3px 0;border-bottom:1px solid var(--border);"><span style="color:var(--dim);font-size:11px;">' + label + ':</span> <span style="color:' + (color || 'var(--text)') + ';font-weight:600;">' + val + '</span></div>';

  let leftHtml = '<div style="font-weight:700;color:var(--cyan);margin-bottom:6px;">Trade Info</div>';
  leftHtml += fmtRow('ID', t.id);
  leftHtml += fmtRow('Time', t.ts_str || t.time);
  leftHtml += fmtRow('Market ID', (t.market_id||'').slice(0,16) + '...');
  leftHtml += fmtRow('Question', t.question || '-');
  leftHtml += fmtRow('Asset', t.asset);
  leftHtml += fmtRow('Side', t.side, t.side === 'UP' ? 'var(--green)' : t.side === 'DOWN' ? 'var(--red)' : 'var(--cyan)');
  leftHtml += fmtRow('Price', '$' + (t.price||0).toFixed(2));
  leftHtml += fmtRow('Size', t.size);
  leftHtml += fmtRow('Cost', '$' + (t.cost||0).toFixed(2));
  leftHtml += fmtRow('Status', t.status);
  leftHtml += fmtRow('Outcome', t.won, t.won === 'WIN' ? 'var(--green)' : t.won === 'LOSS' ? 'var(--red)' : 'var(--dim)');
  leftHtml += fmtRow('PnL', t.pnl !== 0 ? '$' + t.pnl.toFixed(2) : '-', t.pnl > 0 ? 'var(--green)' : t.pnl < 0 ? 'var(--red)' : 'var(--dim)');
  leftHtml += fmtRow('Winner', t.winner || 'pending');
  leftHtml += fmtRow('Strategy', t.strategy || '-');
  leftHtml += fmtRow('Confidence', t.confidence || '-');
  leftHtml += fmtRow('Notes', t.notes || '-');

  let rightHtml = '<div style="font-weight:700;color:var(--yellow);margin-bottom:6px;">Market Snapshot</div>';
  rightHtml += fmtRow('Secs to Close', t.secs_to_close > 0 ? t.secs_to_close.toFixed(1) + 's' : '-');
  rightHtml += fmtRow('Asset Price', t.asset_price > 0 ? '$' + t.asset_price.toFixed(2) : '-');
  rightHtml += fmtRow('Candle Open', t.candle_open > 0 ? '$' + t.candle_open.toFixed(2) : '-');
  rightHtml += fmtRow('Candle High', t.candle_high > 0 ? '$' + t.candle_high.toFixed(2) : '-');
  rightHtml += fmtRow('Candle Low', t.candle_low > 0 ? '$' + t.candle_low.toFixed(2) : '-');
  rightHtml += fmtRow('Candle Range', t.candle_range > 0 ? '$' + t.candle_range.toFixed(6) : '-');
  rightHtml += fmtRow('Price Distance', t.price_distance > 0 ? '$' + t.price_distance.toFixed(6) : '-');
  rightHtml += fmtRow('Distance %', t.price_distance_pct > 0 ? (t.price_distance_pct * 100).toFixed(4) + '%' : '-');
  rightHtml += fmtRow('Range %', t.range_pct > 0 ? (t.range_pct * 100).toFixed(1) + '%' : '-');
  rightHtml += fmtRow('Price vs Open', t.price_vs_open || '-', t.price_vs_open === 'ABOVE' ? 'var(--green)' : 'var(--red)');
  rightHtml += fmtRow('Expansion Ratio', t.expansion_ratio > 0 ? t.expansion_ratio.toFixed(2) + 'x' : '-');
  rightHtml += '<div style="font-weight:700;color:var(--cyan);margin-top:12px;margin-bottom:6px;">Order Book</div>';
  rightHtml += fmtRow('Up Ask', t.up_ask > 0 ? '$' + t.up_ask.toFixed(4) + ' (' + (t.up_ask_size||0).toFixed(0) + ')' : '-');
  rightHtml += fmtRow('Up Bid', t.up_bid > 0 ? '$' + t.up_bid.toFixed(4) + ' (' + (t.up_bid_size||0).toFixed(0) + ')' : '-');
  rightHtml += fmtRow('Down Ask', t.down_ask > 0 ? '$' + t.down_ask.toFixed(4) + ' (' + (t.down_ask_size||0).toFixed(0) + ')' : '-');
  rightHtml += fmtRow('Down Bid', t.down_bid > 0 ? '$' + t.down_bid.toFixed(4) + ' (' + (t.down_bid_size||0).toFixed(0) + ')' : '-');
  rightHtml += fmtRow('Combined Ask', t.combined_ask > 0 ? '$' + t.combined_ask.toFixed(4) : '-');
  rightHtml += fmtRow('Ask Diff', t.ask_diff > 0 ? '$' + t.ask_diff.toFixed(4) : '-');
  rightHtml += fmtRow('OB Imbalance', t.ob_imbalance > 0 ? t.ob_imbalance.toFixed(2) + 'x ' + t.ob_heavy_side : '-');
  rightHtml += '<div style="font-weight:700;color:var(--green);margin-top:12px;margin-bottom:6px;">Fill Info</div>';
  rightHtml += fmtRow('Fill Time', t.fill_time || '-');
  rightHtml += fmtRow('Up Filled', t.up_filled ? 'YES (' + (t.up_fill_size||0) + ')' : 'No', t.up_filled ? 'var(--green)' : 'var(--dim)');
  rightHtml += fmtRow('Down Filled', t.down_filled ? 'YES (' + (t.down_fill_size||0) + ')' : 'No', t.down_filled ? 'var(--green)' : 'var(--dim)');
  rightHtml += fmtRow('Payout', t.payout > 0 ? '$' + t.payout.toFixed(2) : '-');
  rightHtml += fmtRow('Order Up', t.order_id_up ? t.order_id_up.slice(0,12) + '...' : '-');
  rightHtml += fmtRow('Order Down', t.order_id_down ? t.order_id_down.slice(0,12) + '...' : '-');

  content.innerHTML = '<div>' + leftHtml + '</div><div>' + rightHtml + '</div>';
}

// ── Stats rendering ──
function tjRenderStats(stats) {
  if (!stats) return;
  tjStats = stats;
  document.getElementById('tj-st-total').textContent = stats.total;
  document.getElementById('tj-st-open').textContent = stats.open;
  document.getElementById('tj-st-wins').textContent = stats.wins;
  document.getElementById('tj-st-losses').textContent = stats.losses;

  const wrEl = document.getElementById('tj-st-winrate');
  wrEl.textContent = stats.win_rate + '%';
  wrEl.className = 'stat-value ' + (stats.win_rate >= 50 ? 'green' : stats.win_rate > 0 ? 'red' : '');

  const pnlEl = document.getElementById('tj-st-pnl');
  pnlEl.textContent = (stats.total_pnl >= 0 ? '+' : '') + '$' + stats.total_pnl.toFixed(2);
  pnlEl.className = 'stat-value ' + (stats.total_pnl > 0 ? 'green' : stats.total_pnl < 0 ? 'red' : '');

  document.getElementById('tj-st-best').textContent = '+$' + stats.best_trade.toFixed(2);
  document.getElementById('tj-st-worst').textContent = '$' + stats.worst_trade.toFixed(2);

  // Pattern comparison
  const patPanel = document.getElementById('tj-patterns');
  if (stats.win_patterns && Object.keys(stats.win_patterns).length > 0) {
    patPanel.style.display = 'block';
    const wp = stats.win_patterns;
    const lp = stats.loss_patterns || {};

    let wHtml = '';
    wHtml += '<div>Avg Secs to Close: <b style="color:var(--text);">' + (wp.avg_secs_to_close||0).toFixed(0) + 's</b></div>';
    wHtml += '<div>Avg Range %: <b style="color:var(--text);">' + ((wp.avg_range_pct||0)*100).toFixed(1) + '%</b></div>';
    wHtml += '<div>Avg OB Imbalance: <b style="color:var(--text);">' + (wp.avg_ob_imbalance||0).toFixed(2) + 'x</b></div>';
    wHtml += '<div>Avg Candle Range: <b style="color:var(--text);">$' + (wp.avg_candle_range||0).toFixed(4) + '</b></div>';
    wHtml += '<div>Avg Ask Diff: <b style="color:var(--text);">$' + (wp.avg_ask_diff||0).toFixed(4) + '</b></div>';
    wHtml += '<div>Avg Combined Ask: <b style="color:var(--text);">$' + (wp.avg_combined_ask||0).toFixed(4) + '</b></div>';
    wHtml += '<div>Price Above Open: <b style="color:var(--text);">' + (wp.price_above_pct||0).toFixed(0) + '%</b></div>';
    wHtml += '<div>Price Below Open: <b style="color:var(--text);">' + (wp.price_below_pct||0).toFixed(0) + '%</b></div>';
    if (wp.strategy_dist) {
      wHtml += '<div style="margin-top:6px;font-weight:600;color:var(--green);">Strategies:</div>';
      Object.entries(wp.strategy_dist).forEach(([k,v]) => {
        wHtml += '<div>&nbsp;&nbsp;' + k + ': <b>' + v + '</b></div>';
      });
    }
    document.getElementById('tj-win-patterns').innerHTML = wHtml;

    let lHtml = '';
    if (Object.keys(lp).length > 0) {
      lHtml += '<div>Avg Secs to Close: <b style="color:var(--text);">' + (lp.avg_secs_to_close||0).toFixed(0) + 's</b></div>';
      lHtml += '<div>Avg Range %: <b style="color:var(--text);">' + ((lp.avg_range_pct||0)*100).toFixed(1) + '%</b></div>';
      lHtml += '<div>Avg OB Imbalance: <b style="color:var(--text);">' + (lp.avg_ob_imbalance||0).toFixed(2) + 'x</b></div>';
      lHtml += '<div>Avg Candle Range: <b style="color:var(--text);">$' + (lp.avg_candle_range||0).toFixed(4) + '</b></div>';
      lHtml += '<div>Avg Ask Diff: <b style="color:var(--text);">$' + (lp.avg_ask_diff||0).toFixed(4) + '</b></div>';
      lHtml += '<div>Avg Combined Ask: <b style="color:var(--text);">$' + (lp.avg_combined_ask||0).toFixed(4) + '</b></div>';
      lHtml += '<div>Price Above Open: <b style="color:var(--text);">' + (lp.price_above_pct||0).toFixed(0) + '%</b></div>';
      lHtml += '<div>Price Below Open: <b style="color:var(--text);">' + (lp.price_below_pct||0).toFixed(0) + '%</b></div>';
    } else {
      lHtml = '<div style="color:var(--dim);">No losses yet</div>';
    }
    document.getElementById('tj-loss-patterns').innerHTML = lHtml;
  } else {
    patPanel.style.display = 'none';
  }

  // Asset breakdown
  const assetPanel = document.getElementById('tj-asset-breakdown');
  if (stats.assets && Object.keys(stats.assets).length > 0) {
    assetPanel.style.display = 'block';
    let aHtml = '<table class="data-table" style="width:100%;font-size:12px;"><thead><tr><th>Asset</th><th>Wins</th><th>Losses</th><th>Both</th><th>PnL</th><th>Win Rate</th></tr></thead><tbody>';
    Object.entries(stats.assets).forEach(([asset, s]) => {
      const total = s.wins + s.losses + s.both;
      const wr = total > 0 ? ((s.wins + s.both) / total * 100).toFixed(0) : '0';
      const pnlClass = s.pnl > 0 ? 'green' : s.pnl < 0 ? 'red' : '';
      aHtml += '<tr><td style="font-weight:700;">' + asset + '</td><td class="green">' + s.wins + '</td><td class="red">' + s.losses + '</td><td>' + s.both + '</td><td class="' + pnlClass + '">' + (s.pnl >= 0 ? '+' : '') + '$' + s.pnl.toFixed(2) + '</td><td>' + wr + '%</td></tr>';
    });
    aHtml += '</tbody></table>';
    document.getElementById('tj-asset-table-wrap').innerHTML = aHtml;
  } else {
    assetPanel.style.display = 'none';
  }
}

// ── Wire up state updates to TJ tab ──
socket.on('state', (d) => {
  // Update journal data
  if (d.tj_entries) {
    tjEntries = d.tj_entries;
    tjRenderTable();
  }
  // Update badges
  if (d.tj_count !== undefined) {
    const badge = document.getElementById('tj-tab-badge');
    if (badge) badge.textContent = d.tj_count;
    const summary = document.getElementById('tj-summary');
    if (summary) summary.textContent = d.tj_count + ' trades';
  }
  // Auto-trade toggle sync
  if (d.auto_trade_enabled !== undefined) {
    tjSyncAutoToggle(d.auto_trade_enabled);
  }
  // Populate market dropdown from active markets
  if (d.all_markets) {
    tjPopulateMarkets(d.all_markets);
  }
});

// ── Socket events for TJ updates ──
socket.on('tj_new_entry', (entry) => {
  // Prepend to local array and re-render
  tjEntries.unshift(entry);
  tjRenderTable();
  const badge = document.getElementById('tj-tab-badge');
  if (badge) badge.textContent = tjEntries.length;
});

socket.on('tj_updated', (entry) => {
  // Replace in local array
  const idx = tjEntries.findIndex(e => e.id === entry.id);
  if (idx >= 0) tjEntries[idx] = entry;
  tjRenderTable();
});

socket.on('tj_stats', (stats) => {
  tjRenderStats(stats);
});

socket.on('tj_export_data', (entries) => {
  if (!entries || entries.length === 0) { alert('No data to export'); return; }
  // Build CSV
  const keys = Object.keys(entries[0]);
  let csv = keys.join(',') + '\n';
  entries.forEach(e => {
    csv += keys.map(k => {
      let v = e[k];
      if (v === null || v === undefined) v = '';
      v = String(v).replace(/"/g, '""');
      if (v.includes(',') || v.includes('"') || v.includes('\n')) v = '"' + v + '"';
      return v;
    }).join(',') + '\n';
  });
  const blob = new Blob([csv], { type: 'text/csv' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = 'trade_journal_' + new Date().toISOString().slice(0,10) + '.csv';
  a.click();
  URL.revokeObjectURL(url);
});

// Auto-request stats when switching to journal tab
const origSwitchTab = switchTab;
switchTab = function(tab) {
  origSwitchTab(tab);
  if (tab === 'journal') {
    socket.emit('tj_get_stats');
  }
};

// ══════════════════════════════════════════════════════════════════════════
//  ARB ENGINE
// ══════════════════════════════════════════════════════════════════════════

function arbToggle(on) {
  socket.emit('arb_toggle', {enabled: on});
}

function arbSaveParams() {
  socket.emit('arb_update_params', {
    min_edge: parseFloat(document.getElementById('arb-min-edge').value) || 0.03,
    trade_size: parseFloat(document.getElementById('arb-trade-size').value) || 5.0,
    max_positions: parseInt(document.getElementById('arb-max-pos').value) || 3,
    cooldown: parseFloat(document.getElementById('arb-cooldown').value) || 30,
    fill_timeout: parseFloat(document.getElementById('arb-timeout').value) || 45,
    max_daily_spend: parseFloat(document.getElementById('arb-daily-cap').value) || 25,
  });
}

function arbManual(mid) {
  socket.emit('arb_manual', {market_id: mid});
}

function arbUpdateUI(arb, positions, markets) {
  // Badge
  const badge = document.getElementById('arb-tab-badge');
  if (badge) badge.textContent = arb.active_count || 0;

  // Toggle checkbox
  const cb = document.getElementById('arb-enabled');
  if (cb && document.activeElement !== cb) cb.checked = arb.enabled;
  const lbl = document.getElementById('arb-status-label');
  if (lbl) {
    lbl.textContent = arb.enabled ? 'ON' : 'OFF';
    lbl.style.color = arb.enabled ? 'var(--purple)' : 'var(--dim)';
  }

  // Sync param inputs (only if not focused)
  function syncInput(id, val) {
    const el = document.getElementById(id);
    if (el && document.activeElement !== el) el.value = val;
  }
  syncInput('arb-min-edge', arb.min_edge);
  syncInput('arb-trade-size', arb.trade_size);
  syncInput('arb-max-pos', arb.max_positions);
  syncInput('arb-cooldown', arb.cooldown);
  syncInput('arb-timeout', arb.fill_timeout);
  syncInput('arb-daily-cap', arb.max_daily_spend);

  // Stats summary
  const ss = document.getElementById('arb-stats-summary');
  if (ss) {
    ss.innerHTML = `Opps: ${arb.opportunities_seen} | Arbs: ${arb.total_arbs} | ` +
      `Active: ${arb.active_count} | ` +
      `Spent: $${arb.total_spent.toFixed(2)} | ` +
      `Profit: <span style="color:${arb.total_profit >= 0 ? 'var(--green)' : 'var(--red)'}">` +
      `$${arb.total_profit.toFixed(2)}</span> | ` +
      `Daily: $${arb.daily_spend.toFixed(2)}/$${arb.max_daily_spend.toFixed(0)}`;
  }

  // PnL summary above positions
  const ps = document.getElementById('arb-pnl-summary');
  if (ps) {
    const wc = arb.both_filled_count || 0;
    const pc = arb.partial_count || 0;
    ps.innerHTML = `${wc} both-filled | ${pc} partial | ` +
      `P&L: <span style="color:${arb.total_profit >= 0 ? 'var(--green)' : 'var(--red)'}">` +
      `$${arb.total_profit.toFixed(2)}</span>`;
  }

  // ── Live combined asks ──
  const lm = document.getElementById('arb-live-markets');
  if (lm && markets && markets.length > 0) {
    const minEdge = arb.min_edge || 0.03;
    let html = '<table style="width:100%;border-collapse:collapse;">' +
      '<tr style="color:var(--dim);font-size:10px;"><th>Asset</th><th>Time</th>' +
      '<th>Up Ask</th><th>Down Ask</th><th>Combined</th><th>Edge</th><th>Liq</th><th></th></tr>';
    markets.forEach(m => {
      if (!m.up_ask || !m.down_ask) return;
      const combined = m.up_ask + m.down_ask;
      const edge = 1.0 - combined;
      const isOpp = edge >= minEdge;
      const rowColor = isOpp ? 'color:var(--green);font-weight:700;' : '';
      const secs = m.secs || 0;
      const timeStr = secs > 0 ? Math.floor(secs) + 's' : 'closed';
      const asset = m.asset || '?';
      // Extract time part from name
      const nm = m.name || m.question || '';
      const tmMatch = nm.match(/(\d{1,2}:\d{2}[AP]M)/);
      const tmLabel = tmMatch ? tmMatch[1] : nm.slice(-12);
      const minLiq = Math.min(m.up_ask_size || 0, m.down_ask_size || 0);
      html += '<tr style="border-bottom:1px solid var(--border);' + rowColor + '">' +
        '<td style="padding:3px 6px;">' + asset + '</td>' +
        '<td style="padding:3px 6px;">' + tmLabel + ' (' + timeStr + ')</td>' +
        '<td style="padding:3px 6px;">$' + m.up_ask.toFixed(2) + '</td>' +
        '<td style="padding:3px 6px;">$' + m.down_ask.toFixed(2) + '</td>' +
        '<td style="padding:3px 6px;">$' + combined.toFixed(3) + '</td>' +
        '<td style="padding:3px 6px;' + (isOpp ? 'color:var(--green);' : '') + '">' +
          (edge > 0 ? '+' : '') + (edge * 100).toFixed(1) + '%</td>' +
        '<td style="padding:3px 6px;">' + Math.floor(minLiq) + '</td>' +
        '<td style="padding:3px 6px;">' +
          (isOpp && secs > 15 ? '<button onclick="arbManual(\'' + m.market_id + '\')" ' +
          'style="background:var(--purple);color:#fff;border:none;padding:2px 8px;' +
          'border-radius:4px;cursor:pointer;font-size:10px;">ARB</button>' : '') +
        '</td></tr>';
    });
    html += '</table>';
    lm.innerHTML = html;
  }

  // ── Positions table ──
  const tbody = document.getElementById('arb-positions-body');
  if (tbody && positions) {
    if (positions.length === 0) {
      tbody.innerHTML = '<tr><td colspan="9" style="text-align:center;color:var(--dim);padding:16px;">No arb positions yet</td></tr>';
    } else {
      let html = '';
      positions.forEach(p => {
        const upIcon = p.up_filled ? '&#x2705;' : (p.resolved ? '&#x274C;' : '&#x23F3;');
        const dnIcon = p.down_filled ? '&#x2705;' : (p.resolved ? '&#x274C;' : '&#x23F3;');
        let statusColor = 'var(--dim)';
        let statusText = p.outcome || 'active';
        if (p.outcome === 'both_filled') { statusColor = 'var(--green)'; statusText = 'BOTH FILLED'; }
        else if (p.outcome.startsWith('partial_')) { statusColor = 'var(--yellow)'; statusText = 'PARTIAL'; }
        else if (p.outcome === 'cancelled' || p.outcome === 'shutdown') { statusColor = 'var(--dim)'; }
        else if (!p.resolved) { statusColor = 'var(--cyan)'; statusText = p.age_secs + 's'; }

        const profitColor = p.profit >= 0 ? 'var(--green)' : 'var(--red)';
        html += '<tr style="border-bottom:1px solid var(--border);">' +
          '<td style="padding:4px 6px;">' + p.asset + '</td>' +
          '<td style="padding:4px 6px;">$' + p.combined.toFixed(3) + '</td>' +
          '<td style="padding:4px 6px;color:var(--green);">' + (p.edge * 100).toFixed(1) + '%</td>' +
          '<td style="padding:4px 6px;">' + p.shares + '</td>' +
          '<td style="padding:4px 6px;">$' + p.cost.toFixed(2) + '</td>' +
          '<td style="padding:4px 6px;">' + upIcon + ' $' + p.up_price.toFixed(2) + '</td>' +
          '<td style="padding:4px 6px;">' + dnIcon + ' $' + p.down_price.toFixed(2) + '</td>' +
          '<td style="padding:4px 6px;color:' + profitColor + ';">' +
            (p.resolved ? '$' + p.profit.toFixed(2) : '--') + '</td>' +
          '<td style="padding:4px 6px;color:' + statusColor + ';">' + statusText + '</td>' +
        '</tr>';
      });
      tbody.innerHTML = html;
    }
  }
}
</script>
</body>
</html>
"""


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def _kill_old_instances():
    """Kill any already-running web_dashboard.py processes (prevents duplicates)."""
    import subprocess
    try:
        out = subprocess.check_output(
            ['wmic', 'process', 'where',
             "name='python.exe' and CommandLine like '%web_dashboard%'",
             'get', 'ProcessId'],
            text=True, stderr=subprocess.DEVNULL
        )
        my_pid = os.getpid()
        for line in out.strip().splitlines():
            line = line.strip()
            if line.isdigit():
                pid = int(line)
                if pid != my_pid:
                    print(f"  Killing old instance (PID {pid})...")
                    subprocess.call(['taskkill', '/F', '/PID', str(pid)],
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
    except Exception:
        pass  # WMIC not available or no matches — fine


if __name__ == "__main__":
    _kill_old_instances()
    print(r"""
    +===================================================+
    |          POLYBOT SNIPEZ -- WEB DASHBOARD           |
    |    Open http://localhost:5050 in your browser      |
    +===================================================+
    """)
    # Suppress Flask's default request logs
    import logging as _logging
    _logging.getLogger("werkzeug").setLevel(_logging.WARNING)

    # ── Restore saved settings (bid prices, filters, scanner config) ──
    _load_settings()
    engine.add_log("Settings loaded from disk", "info")

    # ── Start always-on PCM background monitor (tracks ALL markets) ──
    _pcm_thread = threading.Thread(target=pcm_background_loop, daemon=True, name="pcm-bg")
    _pcm_thread.start()
    engine.add_log("Post-Close Monitor started (always-on, all markets)", "info")

    # ── Bot does NOT auto-start — user must click Start in dashboard ──
    engine.add_log("Waiting for manual start from dashboard...", "info")

    socketio.run(app, host="0.0.0.0", port=5050, debug=False, allow_unsafe_werkzeug=True)
