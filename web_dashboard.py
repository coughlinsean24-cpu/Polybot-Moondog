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
from scalper import Scalper


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
            "scalper": {
                "trade_size_usd": engine.scalper.cfg.trade_size_usd,
                "entry_ask_max": engine.scalper.cfg.entry_ask_max,
                "profit_target": engine.scalper.cfg.profit_target,
                "stop_loss": engine.scalper.cfg.stop_loss,
                "cooldown_secs": engine.scalper.cfg.cooldown_secs,
                "exit_before_close": engine.scalper.cfg.exit_before_close,
                "max_open_positions": engine.scalper.cfg.max_open_positions,
            },
            "scanner": dict(engine.scanner_cfg),
            "scanner_auto_bid": engine.scanner_auto_bid,
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
        engine.bid_price = float(data.get("bid_price", engine.bid_price))
        engine.tokens_per_side = int(data.get("tokens_per_side", engine.tokens_per_side))
        engine.bid_window_open = int(data.get("bid_window_open", engine.bid_window_open))
        engine.bid_window_close = int(data.get("bid_window_close", engine.bid_window_close))
        engine.ob_filter_enabled = bool(data.get("ob_filter_enabled", engine.ob_filter_enabled))
        engine.ob_min_size = float(data.get("ob_min_size", engine.ob_min_size))
        engine.ob_max_imbalance = float(data.get("ob_max_imbalance", engine.ob_max_imbalance))
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
                        else:
                            engine.asset_config[a][k] = float(v)
        engine.btc_dollar_range = engine.asset_config.get("BTC", {}).get("dollar_range", 2.0)
        # Scalper config
        sc = data.get("scalper", {})
        if sc:
            if "trade_size_usd" in sc: engine.scalper.cfg.trade_size_usd = float(sc["trade_size_usd"])
            if "entry_ask_max" in sc: engine.scalper.cfg.entry_ask_max = float(sc["entry_ask_max"])
            if "profit_target" in sc: engine.scalper.cfg.profit_target = float(sc["profit_target"])
            if "stop_loss" in sc: engine.scalper.cfg.stop_loss = float(sc["stop_loss"])
            if "cooldown_secs" in sc: engine.scalper.cfg.cooldown_secs = float(sc["cooldown_secs"])
            if "exit_before_close" in sc: engine.scalper.cfg.exit_before_close = float(sc["exit_before_close"])
            if "max_open_positions" in sc: engine.scalper.cfg.max_open_positions = int(sc["max_open_positions"])
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
        "bid_price": 0.02,              # $0.02 per share
        "tokens_per_side": 100,         # shares per side (per-asset)
        "bid_window_open": 120,         # seconds before close to start posting
        "distance_pct_max": 0.0008,     # 0.080%  (~$78 at $97k)
        "candle_range_max": 50.0,       # $50 max 5-min candle
        "dollar_range": 2.0,            # $2 default UI-editable range
        "expansion_max": 1.5,           # 1.5x prior candle
        "expansion_floor": 30.0,        # only if range > $30
    },
    "ETH": {
        "bid_price": 0.02,              # $0.02 per share
        "tokens_per_side": 100,         # shares per side (per-asset)
        "bid_window_open": 120,         # seconds before close to start posting
        "distance_pct_max": 0.0010,     # 0.10%  (~$2.70 at $2700)
        "candle_range_max": 8.0,        # $8 max 5-min candle
        "dollar_range": 1.0,            # $1 default
        "expansion_max": 1.5,
        "expansion_floor": 4.0,
    },
    "SOL": {
        "bid_price": 0.02,              # $0.02 per share
        "tokens_per_side": 100,         # shares per side (per-asset)
        "bid_window_open": 120,         # seconds before close to start posting
        "distance_pct_max": 0.0015,     # 0.15%  (~$0.25 at $170)
        "candle_range_max": 0.80,       # $0.80 max
        "dollar_range": 0.15,           # $0.15 default
        "expansion_max": 1.5,
        "expansion_floor": 0.40,
    },
    "XRP": {
        "bid_price": 0.02,              # $0.02 per share
        "tokens_per_side": 100,         # shares per side (per-asset)
        "bid_window_open": 120,         # seconds before close to start posting
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
        self.up_ask: float = 0.0             # UP best ask at detection
        self.down_ask: float = 0.0           # DOWN best ask at detection
        self.combined_ask: float = 0.0
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

                # Also grab WS best prices for context
                ws_up = engine.ws_feed.get_price(market.token_id_up)
                ws_down = engine.ws_feed.get_price(market.token_id_down)

                label = "close" if delay == 0 else f"+{delay}s"
                snap = {
                    "label": label,
                    "secs": delay,
                    "up_bids": {round(b["price"], 2): b["size"] for b in book_up.get("bids", [])},
                    "down_bids": {round(b["price"], 2): b["size"] for b in book_down.get("bids", [])},
                    "up_ask": ws_up.best_ask if ws_up and ws_up.valid else 0.0,
                    "down_ask": ws_down.best_ask if ws_down and ws_down.valid else 0.0,
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

                engine.afterhours_events.appendleft(ev.to_dict())

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

        except Exception as e:
            log.error(f"[PCM] monitor error: {e}")
        finally:
            with self._lock:
                self._monitored.discard(market.market_id)


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
        self.bid_window_open: int = config.BID_WINDOW_OPEN    # seconds before close to start posting
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
        # BTC enabled by default
        self.asset_config["BTC"]["enabled"] = True

        # Legacy accessor — btc_dollar_range still used by some log/push paths
        self.btc_dollar_range: float = self.asset_config["BTC"]["dollar_range"]

        # Orderbook depth filter (log-only mode: enabled but thresholds at 0 = no blocking)
        self.ob_filter_enabled: bool = True    # Master toggle for orderbook balance filter
        self.ob_min_size: float = 0.0          # Min tokens at best ask on EACH side to allow bid (0=log only)
        self.ob_max_imbalance: float = 0.0     # Max ratio between sides' ask sizes (0=log only)

        # BTC price tracking (legacy — read from btc_feed directly where possible)
        self.btc_price: float = 0.0           # Current BTC price from Binance
        self.btc_candle_open: float = 0.0     # 5-min candle open price
        self.btc_distance: float = 0.0        # |current - open|
        self.btc_last_fetch: float = 0.0      # timestamp of last fetch

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

        # ── Scalper (separate strategy) ──
        self.scalper: Scalper = Scalper(self.btc_feed, self.ws_feed)

        # ── After-Hours Fill Tracker ──
        # Stores fill events that occurred close to market close (positive = pre-close,
        # negative = post-close). Cleared on bot restart; persists for the session.
        # Now also includes ObservedFill events from the PostCloseMonitor.
        self.afterhours_events: deque = deque(maxlen=2000)

        # ── Post-Close Monitor (watches ALL markets, not just ones we bid on) ──
        self.pcm: PostCloseMonitor = PostCloseMonitor()

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
engine.scalper._socketio = socketio  # Give scalper access to SocketIO for live log pushes

# Apply config defaults to scalper
engine.scalper.cfg.trade_size_usd = config.SCALP_TRADE_SIZE
engine.scalper.cfg.entry_ask_max = config.SCALP_ENTRY_ASK_MAX
engine.scalper.cfg.profit_target = config.SCALP_PROFIT_TARGET
engine.scalper.cfg.stop_loss = config.SCALP_STOP_LOSS
engine.scalper.cfg.cooldown_secs = config.SCALP_COOLDOWN
engine.scalper.cfg.exit_before_close = config.SCALP_EXIT_BEFORE
engine.scalper.cfg.max_open_positions = config.SCALP_MAX_POSITIONS


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
    """Post limit BUY on both Up and Down sides."""
    if market.market_id in engine.bids_posted:
        return False
    if market.market_id in engine.failed_markets:
        return False

    secs = seconds_until(market.end_time)

    # ── Per-Asset Enabled Check ────────────────────────────────────────────
    asset = market.asset
    ac = engine.asset_config.get(asset, {})

    # Per-asset bid window (fall back to global)
    asset_window_open = ac.get("bid_window_open", engine.bid_window_open)
    if secs > asset_window_open or secs < engine.bid_window_close:
        return False
    if not ac.get("enabled", False):
        return False   # asset disabled by user — silently skip

    # ── Per-Asset Compression / Indecision Filters ──────────────────────────
    # Every asset with Binance candle data gets its own riding-the-line
    # thresholds (distance%, candle range, dollar range, expansion).
    # All thresholds are now read from engine.asset_config (UI-editable).
    af_defaults = get_asset_filter(asset)   # static fallbacks
    af = {k: ac.get(k, af_defaults.get(k, 0)) for k in af_defaults}
    ast = engine.btc_feed.get(asset)  # AssetState from multi-asset feed

    if ast and ast.price > 0 and ast.candle_open > 0:
        # Dollar range filter (per-asset, UI-editable)
        dollar_limit = af["dollar_range"]
        if dollar_limit > 0:
            if ast.distance > dollar_limit:
                engine.add_log(
                    f"SKIP ({asset} moved ${ast.distance:.4f} > ${dollar_limit:.4f} limit)  {market.question}",
                    "warn",
                )
                return False

        dist_pct = ast.distance / ast.price

        # Filter 1: Too directional (% from open)
        if dist_pct > af["distance_pct_max"]:
            engine.add_log(
                f"BID-SKIP ({asset} directional {dist_pct*100:.3f}% > {af['distance_pct_max']*100:.3f}%)  "
                f"{market.question}  [{secs:.0f}s]",
                "warn",
            )
            return False

        # Filter 2: Large candle range = momentum, not compression
        if ast.candle_range > af["candle_range_max"]:
            engine.add_log(
                f"BID-SKIP ({asset} candle_range ${ast.candle_range:.4f} > ${af['candle_range_max']:.4f})  "
                f"{market.question}  [{secs:.0f}s]",
                "warn",
            )
            return False

        # Filter 3: Range expanding vs prior candle
        if ast.prior_candle_range > 0 and ast.candle_range > 0:
            expansion = ast.candle_range / ast.prior_candle_range
            if expansion > af["expansion_max"] and ast.candle_range > af["expansion_floor"]:
                engine.add_log(
                    f"BID-SKIP ({asset} range expanding {expansion:.1f}x, "
                    f"${ast.candle_range:.4f} vs prior ${ast.prior_candle_range:.4f})  "
                    f"{market.question}  [{secs:.0f}s]",
                    "warn",
                )
                return False

    # ── Price symmetry filter — both sides must be cheap & balanced ─────
    ws_up = engine.ws_feed.get_price(market.token_id_up)
    ws_down = engine.ws_feed.get_price(market.token_id_down)

    if ws_up and ws_up.valid and ws_down and ws_down.valid:
        up_ask = ws_up.best_ask
        dn_ask = ws_down.best_ask

        # Filter 4: One side already expensive (market decided, single-fill trap)
        if up_ask > 0.70 or dn_ask > 0.70:
            engine.add_log(
                f"BID-SKIP (side decided: UP=${up_ask:.2f} DN=${dn_ask:.2f})  {market.question}  [{secs:.0f}s]",
                "warn",
            )
            return False

        # Filter 5: Price symmetry — both sides should be similarly priced
        # When prices are balanced (e.g., 0.50/0.50 or 0.45/0.55), market is
        # indecisive and both-fill more likely. Asymmetric = adverse selection.
        ask_diff = abs(up_ask - dn_ask)
        if ask_diff > 0.25:
            engine.add_log(
                f"BID-SKIP (asymmetric: UP=${up_ask:.2f} DN=${dn_ask:.2f} diff={ask_diff:.2f})  "
                f"{market.question}  [{secs:.0f}s]",
                "warn",
            )
            return False

    # ── Orderbook depth filter ──────────────────────────────────────────
    ob_detail = _build_ob_detail(ws_up, ws_down, market)

    if engine.ob_filter_enabled and ws_up.valid and ws_down.valid:
        up_ask_size = ws_up.best_ask_size
        down_ask_size = ws_down.best_ask_size

        # Check minimum size on each side
        if engine.ob_min_size > 0:
            if up_ask_size < engine.ob_min_size or down_ask_size < engine.ob_min_size:
                thin_side = "UP" if up_ask_size < down_ask_size else "DOWN"
                engine.add_log(
                    f"SKIP (thin book: {thin_side} ask_size={min(up_ask_size, down_ask_size):.0f} < {engine.ob_min_size:.0f})  "
                    f"UP={up_ask_size:.0f} DOWN={down_ask_size:.0f}  {market.question}",
                    "warn",
                )
                _log_ob_filter(market, ob_detail, "thin_book", secs)
                return False

        # Check imbalance between sides
        if engine.ob_max_imbalance > 0 and up_ask_size > 0 and down_ask_size > 0:
            ratio = max(up_ask_size, down_ask_size) / min(up_ask_size, down_ask_size)
            if ratio > engine.ob_max_imbalance:
                heavy_side = "UP" if up_ask_size > down_ask_size else "DOWN"
                engine.add_log(
                    f"SKIP (imbalanced: {heavy_side} heavy {ratio:.1f}x > {engine.ob_max_imbalance:.1f}x)  "
                    f"UP={up_ask_size:.0f} DOWN={down_ask_size:.0f}  {market.question}",
                    "warn",
                )
                _log_ob_filter(market, ob_detail, "imbalanced", secs)
                return False

        # Passed filter — log it for data collection
        _log_ob_filter(market, ob_detail, "PASSED", secs)
    elif engine.ob_filter_enabled:
        # WS data not valid — log but don't block (allow bid)
        _log_ob_filter(market, ob_detail, "no_ws_data", secs)

    if config.MAX_BIDS_PER_DAY > 0 and engine.bids_today >= config.MAX_BIDS_PER_DAY:
        return False

    # ── Adaptive Learner Gate ───────────────────────────────────────────
    # Extract features from the market's asset feed (BTC, ETH, SOL, XRP).
    # The learner's feature names still say "btc_" for backward compat with
    # stored model weights; the values come from the correct asset though.
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

    engine.add_log(
        f"AI {reason}  {market.question}",
        "info",
    )
    # ────────────────────────────────────────────────────────────────────

    # Use live-editable values — per-asset bid price & tokens, clamped by hard safety caps
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
    total_cost = price * tokens * 2

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
        f"POSTING BIDS [{market.asset}]: {market.question}  ${price} x {tokens}/side  "
        f"(${total_cost:.2f} if both fill)  [{secs:.0f}s left]",
        "trade",
    )

    bid = BidRecord(market.market_id, market.question)
    bid.bid_price = price
    bid.tokens = tokens
    bid.posted_at = time.time()

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
        engine.add_log(f"BOTH bids FAILED: {err_detail}", "error")
        engine.errors += 1
        engine.failed_markets.add(market.market_id)
        return False

    engine.bids_posted[market.market_id] = bid
    market.fired = True
    engine.bids_today += 1
    engine.markets_fired += 1
    # NOTE: total_spent is now tracked in check_fills() when orders are actually matched

    # Record features with learner for later outcome matching
    ai_learner.record_bid(market.market_id, ai_features)

    # ── Capture enrichment context at bid time ──────────────────────────
    # Orderbook snapshot from WS feed
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

    # Asset price context (include candle_range for learner bootstrap)
    # Keyed as "btc_*" for backward compat with log parsers / learner
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

    # Question / market metadata
    meta_ctx = {
        "question": market.question,
        "end_time": market.end_time.isoformat(),
    }

    # Merge all enrichment into one dict
    enrichment = {**ob_ctx, **btc_ctx, **meta_ctx}

    # Log
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

        engine.afterhours_events.appendleft(ev.to_dict())
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
    engine.add_log("WebSocket feed + Binance WS + data recorder started", "info")

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
                for market_id, market in list(engine.watch_list.items()):
                    if not engine.running:
                        break
                    if market_id in engine.bids_posted:
                        continue
                    secs = seconds_until(market.end_time)
                    # Activity log: show evaluation countdown during bid window
                    _ac = engine.asset_config.get(market.asset, {})
                    _asset_wo = _ac.get("bid_window_open", engine.bid_window_open)
                    in_eval = engine.bid_window_close <= secs <= _asset_wo + 5
                    in_bid  = engine.bid_window_close <= secs <= _asset_wo
                    if in_eval and _bid_log_due:
                        try:
                            _log_bid_eval(market, secs)
                        except Exception as eval_err:
                            log.error(f"BID-EVAL error [{market.asset}]: {eval_err}")
                        engine._last_bid_eval_log = now_ts
                    if in_bid:
                        # Skip if scalper already has a position on this market
                        if hasattr(engine, 'scalper') and market_id in engine.scalper.positions:
                            continue
                        post_bids(market)

                # Sync limit bid market IDs to scalper so it doesn't conflict
                if hasattr(engine, 'scalper'):
                    engine.scalper.limit_bid_markets = set(engine.bids_posted.keys())

            # Check fills
            if now_ts - last_fill_check >= 3.0:
                check_fills()
                last_fill_check = now_ts

            # Monitor OB imbalance for active bids (log-only, every 2s)
            if now_ts - last_ob_monitor >= 2.0 and engine.bids_posted:
                monitor_ob_imbalance()
                last_ob_monitor = now_ts

            # Update price cache
            update_prices()

            # Push full state to browser every 0.5s
            if now_ts - last_push >= 0.5:
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

            # Scalper thread (restart unless user explicitly stopped it)
            if not engine.scalper.running and not engine.scalper._user_stopped:
                engine.add_log("Watchdog: restarting scalper thread", "warn")
                engine.scalper.start()

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
    REFRESH_INTERVAL = 30  # seconds between market discovery polls

    while True:
        try:
            now_ts = time.time()

            # Refresh watch_list so we always know about all active markets
            if now_ts - last_refresh >= REFRESH_INTERVAL:
                refresh_watch_list()
                last_refresh = now_ts

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
        "scalper": engine.scalper.stats(),
        "scanner_results": engine.scanner_results,
        "scanner_running": engine.scanner_running,
        "scanner_auto_bid": engine.scanner_auto_bid,
        "scanner_cfg": dict(engine.scanner_cfg),
        "scanner_last_scan": engine.scanner_last_scan,
        "afterhours_events": list(engine.afterhours_events),
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
        sv_scalper=engine.scalper.cfg,
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


# ── SocketIO Events ─────────────────────────────────────────────────────────

@socketio.on("connect")
def on_connect():
    push_state()


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
        new_price = float(data.get("bid_price", engine.bid_price))
        new_tokens = int(data.get("tokens_per_side", engine.tokens_per_side))

        # Hard safety clamp — dashboard can NEVER exceed these
        new_price = min(new_price, config.HARD_MAX_BID_PRICE)
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
        new_ob_enabled = bool(data.get("ob_filter_enabled", engine.ob_filter_enabled))
        new_ob_min_size = float(data.get("ob_min_size", engine.ob_min_size))
        new_ob_max_imbalance = float(data.get("ob_max_imbalance", engine.ob_max_imbalance))

        if new_price <= 0 or new_price > config.HARD_MAX_BID_PRICE:
            engine.add_log(f"Invalid bid price: ${new_price} (must be $0.01-${config.HARD_MAX_BID_PRICE})", "error")
            return
        if new_tokens < 1 or new_tokens > config.HARD_MAX_TOKENS:
            engine.add_log(f"Invalid tokens: {new_tokens} (must be 1-{config.HARD_MAX_TOKENS})", "error")
            return
        if new_window_open < 5 or new_window_open > 600:
            engine.add_log(f"Invalid window open: {new_window_open}s (must be 5-600)", "error")
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
            _aw = _acfg.get("bid_window_open", 120)
            if _aw < 5 or _aw > 600:
                engine.add_log(f"Invalid {_ak} bid start: {_aw}s (must be 5-600)", "error")
                return
            if _aw <= new_window_close:
                engine.add_log(f"{_ak} bid start ({_aw}s) must be > bid stop ({new_window_close}s)", "error")
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
            changes.append(f"Window Open {engine.bid_window_open}s->{new_window_open}s")
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
        push_state()
    except (ValueError, TypeError) as e:
        engine.add_log(f"Bad learner param: {e}", "error")


@socketio.on("toggle_learner")
def on_toggle_learner():
    """Enable/disable the adaptive learner gate."""
    ai_learner.enabled = not ai_learner.enabled
    state = "ON" if ai_learner.enabled else "OFF"
    engine.add_log(f"AI learner gate {state}", "info")
    push_state()


@socketio.on("reset_learner")
def on_reset_learner():
    """Reset learner model weights (keeps trade history on disk)."""
    ai_learner.reset_model()
    engine.add_log("AI learner model RESET", "warn")
    push_state()


# ── Scalper SocketIO Events ─────────────────────────────────────────────────

@socketio.on("start_scalper")
def on_start_scalper():
    if engine.scalper.running:
        return
    engine.scalper.start()
    engine.add_log("Scalper started from dashboard", "info")
    push_state()


@socketio.on("stop_scalper")
def on_stop_scalper():
    engine.scalper.stop(user_requested=True)
    engine.add_log("Scalper stopped from dashboard", "warn")
    push_state()


@socketio.on("update_scalper_params")
def on_update_scalper_params(data):
    """Live-update scalper parameters from dashboard."""
    try:
        changes = engine.scalper.update_config(data)
        if changes:
            engine.scalper.add_log(f"Params updated: {', '.join(changes)}", "info")
        _save_settings()
        push_state()
    except (ValueError, TypeError) as e:
        engine.scalper.add_log(f"Bad param update: {e}", "error")


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
    push_state()


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

  /* ── Scalper-specific styles ── */
  .scalper-controls {
    background: var(--card); border-bottom: 1px solid var(--border);
    padding: 10px 24px; display: flex; align-items: center; gap: 16px;
    flex-wrap: wrap;
  }
  .scalper-stats-row {
    display: grid; grid-template-columns: repeat(6, 1fr); gap: 12px;
    padding: 12px 24px 0 24px;
  }
  .scalper-positions {
    padding: 12px 24px;
  }
  .pos-card {
    background: var(--card); border: 1px solid var(--border);
    border-radius: 8px; padding: 12px 16px; margin-bottom: 8px;
    display: flex; justify-content: space-between; align-items: center; gap: 20px;
  }
  .pos-side { font-weight: 700; font-size: 14px; min-width: 50px; }
  .pos-side.UP { color: var(--green); }
  .pos-side.DOWN { color: var(--red); }
  .pos-detail { font-family: monospace; font-size: 12px; color: var(--dim); }
  .pos-pnl { font-family: monospace; font-weight: 700; font-size: 16px; }
  .pos-pnl.win { color: var(--green); }
  .pos-pnl.lose { color: var(--red); }
  .pos-state {
    padding: 2px 8px; border-radius: 3px; font-size: 10px;
    font-weight: 700; text-transform: uppercase;
  }
  .pos-state.watching { background: #1c2333; color: var(--dim); }
  .pos-state.buying { background: #2d2000; color: var(--yellow); }
  .pos-state.holding { background: #0c2d4a; color: var(--cyan); }
  .pos-state.selling { background: #2d2000; color: var(--orange); }
  .pos-state.exited { background: #1c2333; color: var(--dim); }

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
  <button class="tab-btn" onclick="switchTab('scalper')" id="tab-btn-scalper">Scalper</button>
  <button class="tab-btn" onclick="switchTab('scanner')" id="tab-btn-scanner">Market Scanner</button>
  <button class="tab-btn" onclick="switchTab('afterhours')" id="tab-btn-afterhours">After-Hours Fills <span id="ah-tab-badge" style="background:#0c2d4a;color:var(--cyan);border-radius:10px;padding:0 6px;font-size:10px;margin-left:4px;">0</span></button>
</div>

<!-- ══════ TAB 1: LIMIT BID STRATEGY ══════ -->
<div class="tab-content active" id="tab-limit-bid">

<!-- ── Controls ── -->
<div class="controls">
  <div class="control-group">
    <label>Bid $ (all)</label>
    <input type="number" id="inp-price" step="0.01" min="0.01" max="0.50" value="{{ sv_bid_price }}">
  </div>
  <div class="control-group">
    <label>Shares/Side (all)</label>
    <input type="number" id="inp-tokens" step="10" min="1" max="10000" value="{{ sv_tokens }}" title="Set all assets' shares/side at once">
  </div>
  <div class="control-group" style="border-left: 1px solid var(--border); padding-left: 16px; margin-left: 4px;">
    <label>Bid Start (all)</label>
    <input type="number" id="inp-window-open" step="5" min="5" max="600" value="{{ sv_window_open }}" title="Set all assets' bid start time at once (seconds before close)">
  </div>
  <div class="control-group">
    <label>Bid Stop (sec)</label>
    <input type="number" id="inp-window-close" step="1" min="0" max="300" value="{{ sv_window_close }}" title="Seconds before close to STOP posting bids">
  </div>
  <!-- Per-asset enable toggles — full config in collapsible panel below -->
  {% for _a in ['BTC','ETH','SOL','XRP'] %}
  <div class="control-group"{% if loop.first %} style="border-left: 1px solid var(--border); padding-left: 16px; margin-left: 4px;"{% endif %}>
    <label style="display:flex; align-items:center; gap:4px; cursor:pointer;">
      <input type="checkbox" id="inp-enabled-{{ _a }}" {{ 'checked' if sv_asset_config.get(_a, {}).get('enabled') else '' }} title="Enable/disable {{ _a }} bidding">
      {{ _a }}
    </label>
  </div>
  {% endfor %}
  <div class="control-group" style="border-left: 1px solid var(--border); padding-left: 16px; margin-left: 4px;">
    <label style="display:flex; align-items:center; gap:6px; cursor:pointer;">
      <input type="checkbox" id="inp-ob-filter" {{ 'checked' if sv_ob_enabled else '' }} title="Enable orderbook depth filter">
      OB Filter
    </label>
  </div>
  <div class="control-group">
    <label>Min Size</label>
    <input type="number" id="inp-ob-min-size" step="10" min="0" max="10000" value="{{ sv_ob_min_size }}" title="Minimum ask size on EACH side to allow bid. 0 = log only, no blocking">
  </div>
  <div class="control-group">
    <label>Max Imbal</label>
    <input type="number" id="inp-ob-max-imbalance" step="0.5" min="0" max="100" value="{{ sv_ob_max_imbalance }}" title="Max ratio between sides ask sizes (larger/smaller). 0 = log only, no blocking">
  </div>
  <button class="btn btn-apply" id="btn-apply" onclick="applyParams()">Apply</button>
  <div class="cost-preview">
    Cost/mkt: <span class="val" id="cost-preview">--</span>
    &nbsp;|&nbsp; Payout/mkt: <span class="val" id="payout-preview">--</span>
    &nbsp;|&nbsp; Profit/mkt: <span class="val" id="profit-preview">--</span>
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

<!-- ── Per-Asset Filter Config (collapsible) ── -->
<div id="asset-config-panel" style="background:var(--card); border-bottom:1px solid var(--border); padding:0 24px; max-height:0; overflow:hidden; transition:max-height 0.3s ease;">
  <div style="padding:10px 0;">
    <table style="width:100%; border-collapse:collapse; font-size:12px; font-family:monospace;">
      <thead>
        <tr style="color:var(--dim); text-transform:uppercase; font-weight:600;">
          <th style="text-align:left; padding:4px 8px;">Asset</th>
          <th style="text-align:center; padding:4px 8px;">Bid $ <span class="info-tip"><span class="info-icon">i</span><span class="info-bubble">The price you pay per share on each side (Up &amp; Down). Lower = cheaper but less likely to fill. Cost per market = bid $ &times; tokens &times; 2 sides.</span></span></th>
          <th style="text-align:center; padding:4px 8px;">Shares/Side <span class="info-tip"><span class="info-icon">i</span><span class="info-bubble">Number of shares to bid on each side (Up &amp; Down) for this asset. Total cost = bid $ &times; shares &times; 2.</span></span></th>
          <th style="text-align:center; padding:4px 8px;">Bid Start <span class="info-tip"><span class="info-icon">i</span><span class="info-bubble">Seconds before market close to START posting bids for this asset. Higher = earlier entry.</span></span></th>
          <th style="text-align:center; padding:4px 8px;">Dollar Range <span class="info-tip"><span class="info-icon">i</span><span class="info-bubble">Max dollar distance the asset price can be from the 5-min candle open before bids are skipped. Filters out markets where price has already moved too far. 0 = disabled.</span></span></th>
          <th style="text-align:center; padding:4px 8px;">Dist % Max <span class="info-tip"><span class="info-icon">i</span><span class="info-bubble">Max percentage distance from candle open as a decimal (e.g. 0.0008 = 0.08%). Skips markets where price has moved too far relative to the asset&rsquo;s value. 0 = disabled.</span></span></th>
          <th style="text-align:center; padding:4px 8px;">Candle Range Max <span class="info-tip"><span class="info-icon">i</span><span class="info-bubble">Max allowed high-low range (in $) of the current 5-min candle. Skips volatile candles where outcome is more unpredictable. 0 = disabled.</span></span></th>
          <th style="text-align:center; padding:4px 8px;">Expansion Max <span class="info-tip"><span class="info-icon">i</span><span class="info-bubble">Max ratio of current candle range to prior candle range. Filters markets where volatility is expanding too fast (breakout candles). 0 = disabled.</span></span></th>
          <th style="text-align:center; padding:4px 8px;">Expansion Floor <span class="info-tip"><span class="info-icon">i</span><span class="info-bubble">Minimum prior candle range (in $) required before the expansion filter activates. Prevents false expansion signals on tiny prior candles. 0 = disabled.</span></span></th>
        </tr>
      </thead>
      <tbody>
        {% for _a in ['BTC','ETH','SOL','XRP'] %}
        {% set _c = sv_asset_config.get(_a, {}) %}
        <tr style="border-top:1px solid var(--border);">
          <td style="padding:6px 8px; font-weight:700; color:var(--cyan);">{{ _a }}</td>
          <td style="text-align:center; padding:4px 4px;">
            <input type="number" id="inp-ac-{{ _a }}-bid_price" step="0.01" min="0.01" max="0.50"
              value="{{ _c.get('bid_price', 0.02) }}" style="width:70px; background:var(--bg); border:1px solid var(--border); color:var(--green); padding:4px 6px; border-radius:3px; text-align:center; font-family:monospace; font-size:12px; font-weight:700;"
              title="{{ _a }}: Bid price per share">
          </td>
          <td style="text-align:center; padding:4px 4px;">
            <input type="number" id="inp-ac-{{ _a }}-tokens_per_side" step="10" min="1" max="10000"
              value="{{ _c.get('tokens_per_side', 100) }}" style="width:70px; background:var(--bg); border:1px solid var(--border); color:var(--yellow); padding:4px 6px; border-radius:3px; text-align:center; font-family:monospace; font-size:12px; font-weight:700;"
              title="{{ _a }}: Shares per side">
          </td>
          <td style="text-align:center; padding:4px 4px;">
            <input type="number" id="inp-ac-{{ _a }}-bid_window_open" step="5" min="5" max="600"
              value="{{ _c.get('bid_window_open', 120) }}" style="width:70px; background:var(--bg); border:1px solid var(--border); color:var(--yellow); padding:4px 6px; border-radius:3px; text-align:center; font-family:monospace; font-size:12px; font-weight:700;"
              title="{{ _a }}: Seconds before close to start bidding">
          </td>
          <td style="text-align:center; padding:4px 4px;">
            <input type="number" id="inp-ac-{{ _a }}-dollar_range" step="any" min="0" max="5000"
              value="{{ _c.get('dollar_range', 0) }}" style="width:80px; background:var(--bg); border:1px solid var(--border); color:var(--cyan); padding:4px 6px; border-radius:3px; text-align:center; font-family:monospace; font-size:12px;"
              title="{{ _a }}: max $ distance from candle open. 0=no filter">
          </td>
          <td style="text-align:center; padding:4px 4px;">
            <input type="number" id="inp-ac-{{ _a }}-distance_pct_max" step="any" min="0" max="1"
              value="{{ _c.get('distance_pct_max', 0) }}" style="width:80px; background:var(--bg); border:1px solid var(--border); color:var(--cyan); padding:4px 6px; border-radius:3px; text-align:center; font-family:monospace; font-size:12px;"
              title="{{ _a }}: max |price-open|/price as decimal (e.g. 0.0008 = 0.08%)">
          </td>
          <td style="text-align:center; padding:4px 4px;">
            <input type="number" id="inp-ac-{{ _a }}-candle_range_max" step="any" min="0" max="99999"
              value="{{ _c.get('candle_range_max', 0) }}" style="width:80px; background:var(--bg); border:1px solid var(--border); color:var(--cyan); padding:4px 6px; border-radius:3px; text-align:center; font-family:monospace; font-size:12px;"
              title="{{ _a }}: max 5-min candle high-low in $">
          </td>
          <td style="text-align:center; padding:4px 4px;">
            <input type="number" id="inp-ac-{{ _a }}-expansion_max" step="any" min="0" max="100"
              value="{{ _c.get('expansion_max', 0) }}" style="width:80px; background:var(--bg); border:1px solid var(--border); color:var(--cyan); padding:4px 6px; border-radius:3px; text-align:center; font-family:monospace; font-size:12px;"
              title="{{ _a }}: max candle_range / prior_candle_range multiplier">
          </td>
          <td style="text-align:center; padding:4px 4px;">
            <input type="number" id="inp-ac-{{ _a }}-expansion_floor" step="any" min="0" max="99999"
              value="{{ _c.get('expansion_floor', 0) }}" style="width:80px; background:var(--bg); border:1px solid var(--border); color:var(--cyan); padding:4px 6px; border-radius:3px; text-align:center; font-family:monospace; font-size:12px;"
              title="{{ _a }}: min candle range $ for expansion rule to engage">
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
</div>
<div style="background:var(--card); border-bottom:1px solid var(--border); padding:0; text-align:center;">
  <button onclick="toggleAssetPanel()" id="btn-asset-panel" style="background:none; border:none; color:var(--dim); cursor:pointer; font-size:11px; padding:3px 12px; font-family:monospace;">
    &#9660; Asset Filters &#9660;
  </button>
</div>

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

<!-- ══════ TAB 2: SCALPER ══════ -->
<div class="tab-content" id="tab-scalper">

<!-- Scalper Controls -->
<div class="scalper-controls">
  <div class="control-group">
    <label>Trade Size $</label>
    <input type="number" id="sc-trade-size" step="0.5" min="0.5" max="100" value="{{ sv_scalper.trade_size_usd }}">
  </div>
  <div class="control-group">
    <label>Max Entry $</label>
    <input type="number" id="sc-entry-max" step="0.01" min="0.10" max="0.90" value="{{ sv_scalper.entry_ask_max }}">
  </div>
  <div class="control-group" style="border-left: 1px solid var(--border); padding-left: 16px; margin-left: 4px;">
    <label>Profit Target $</label>
    <input type="number" id="sc-profit" step="0.005" min="0.005" max="0.50" value="{{ sv_scalper.profit_target }}">
  </div>
  <div class="control-group">
    <label>Stop Loss $</label>
    <input type="number" id="sc-stop" step="0.005" min="0.005" max="0.50" value="{{ sv_scalper.stop_loss }}">
  </div>
  <div class="control-group" style="border-left: 1px solid var(--border); padding-left: 16px; margin-left: 4px;">
    <label>Cooldown (sec)</label>
    <input type="number" id="sc-cooldown" step="1" min="0" max="300" value="{{ sv_scalper.cooldown_secs }}">
  </div>
  <div class="control-group">
    <label>Exit Before (sec)</label>
    <input type="number" id="sc-exit-before" step="5" min="5" max="120" value="{{ sv_scalper.exit_before_close }}">
  </div>
  <div class="control-group">
    <label>Max Positions</label>
    <input type="number" id="sc-max-pos" step="1" min="1" max="10" value="{{ sv_scalper.max_open_positions }}">
  </div>
  <button class="btn btn-apply" onclick="applyScalperParams()">Apply</button>
  <div style="flex:1"></div>
  <div class="cost-preview" id="sc-btc-info" style="text-align:right; min-width:320px;">
    BTC: <span class="val" id="sc-btc-price">--</span>
    &nbsp;|&nbsp; Open: <span class="val" id="sc-btc-open">--</span>
    &nbsp;|&nbsp; Move: <span class="val" id="sc-btc-dist">--</span>
  </div>
  <button class="btn btn-start" id="btn-scalper" onclick="toggleScalper()">START SCALPER</button>
</div>

<!-- Scalper Stats -->
<div class="scalper-stats-row">
  <div class="stat-card"><div class="stat-label">Total Trades</div><div class="stat-value cyan" id="sc-st-trades">0</div></div>
  <div class="stat-card"><div class="stat-label">Wins</div><div class="stat-value green" id="sc-st-wins">0</div></div>
  <div class="stat-card"><div class="stat-label">Losses</div><div class="stat-value red" id="sc-st-losses">0</div></div>
  <div class="stat-card"><div class="stat-label">Win Rate</div><div class="stat-value yellow" id="sc-st-winrate">0%</div></div>
  <div class="stat-card"><div class="stat-label">Total P&L</div><div class="stat-value" id="sc-st-pnl">$0</div></div>
  <div class="stat-card"><div class="stat-label">Markets</div><div class="stat-value cyan" id="sc-st-markets">0</div></div>
</div>

<!-- Active Positions -->
<div class="scalper-positions">
  <div class="card">
    <div class="card-title">Active Positions</div>
    <div class="card-body" id="sc-positions">
      <div class="empty">No active positions</div>
    </div>
  </div>
</div>

<!-- Scalper Grid: History + Log -->
<div class="grid">
  <div class="card">
    <div class="card-title">Recent Trades</div>
    <div class="card-body" style="padding:0; max-height:300px; overflow-y:auto;">
      <table>
        <thead>
          <tr><th>Side</th><th style="text-align:right">Entry</th><th style="text-align:right">Exit</th><th style="text-align:right">Size</th><th style="text-align:right">P&L</th><th>State</th></tr>
        </thead>
        <tbody id="sc-history-body">
          <tr><td colspan="6" class="empty">No trades yet</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <div class="card log-card">
    <div class="card-title">Scalper Log</div>
    <div class="card-body log-body" id="sc-log-body">
      <div class="empty">Waiting for scalper activity...</div>
    </div>
  </div>
</div>

</div><!-- /tab-scalper -->

<!-- ══════ TAB 3: MARKET SCANNER ══════ -->
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

<!-- Events Table -->
<div class="ah-wrap">
  <div class="ah-table-wrap">
    <table>
      <thead>
        <tr>
          <th>Time</th>
          <th>Asset</th>
          <th>Market</th>
          <th>Side</th>
          <th title="Shares filled">Shares</th>
          <th title="Fill price (bid level where fill was detected)">Price $</th>
          <th title="Total cost: price * shares">Cost $</th>
          <th title="Seconds after market close">After Close</th>
          <th title="Snapshot interval where fill was detected">Snapshot</th>
          <th title="Which side won the market (Up/Down)">Winner</th>
          <th title="WIN = filled on winning side, LOSS = filled on losing side">Result</th>
          <th>UP Ask</th>
          <th>DOWN Ask</th>
          <th title="Distance as % of 5-min candle range (universal metric)">Range %</th>
          <th title="Observed from market orderbook or our own bid">Source</th>
        </tr>
      </thead>
      <tbody id="ah-events-body">
        <tr><td colspan="15" class="empty">Monitoring all crypto 5m markets for post-close fills. Data appears after each market closes.</td></tr>
      </tbody>
    </table>
  </div>
</div>

</div><!-- /tab-afterhours -->

<script>
const socket = io();
let botRunning = {{ 'true' if sv_running else 'false' }};
let scalperRunning = false;
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
 'sc-trade-size','sc-entry-max','sc-profit','sc-stop','sc-cooldown','sc-exit-before','sc-max-pos',
 'scan-max-hours','scan-min-liq','scan-max-liq','scan-interval',
 'scan-ab-price','scan-ab-size','scan-ab-max-ask','scan-ab-min-liq'].concat(_acInputIds).forEach(id => {
  const el = document.getElementById(id);
  if (!el) return;
  el.addEventListener('focus', () => markDirty(id));
  el.addEventListener('input', () => markDirty(id));
  el.addEventListener('keydown', () => markDirty(id));
  el.addEventListener('mousedown', () => markDirty(id));
});

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
  let totalCost = 0;
  let totalPayout = 0;
  const parts = [];
  _acAssets.forEach(a => {
    const enEl = document.getElementById('inp-enabled-' + a);
    if (!enEl || !enEl.checked) return;
    const bp = parseFloat(document.getElementById('inp-ac-' + a + '-bid_price').value) || 0;
    const t = parseInt(document.getElementById('inp-ac-' + a + '-tokens_per_side').value) || 0;
    const c = bp * t * 2;
    const p = t * 1.0;
    totalCost += c;
    totalPayout += p;
    parts.push(a + ': $' + c.toFixed(2));
  });
  const totalProfit = totalPayout - totalCost;
  if (parts.length === 0) {
    document.getElementById('cost-preview').textContent = '--';
    document.getElementById('payout-preview').textContent = '--';
    document.getElementById('profit-preview').textContent = '--';
    document.getElementById('cost-per-asset').textContent = '(no assets enabled)';
  } else if (parts.length === 1) {
    document.getElementById('cost-preview').textContent = '$' + totalCost.toFixed(2);
    document.getElementById('payout-preview').textContent = '$' + totalPayout.toFixed(2);
    document.getElementById('profit-preview').textContent = '$' + totalProfit.toFixed(2);
    document.getElementById('cost-per-asset').textContent = '';
  } else {
    document.getElementById('cost-preview').textContent = '$' + totalCost.toFixed(2) + ' total';
    document.getElementById('payout-preview').textContent = '$' + totalPayout.toFixed(2);
    document.getElementById('profit-preview').textContent = '$' + totalProfit.toFixed(2);
    document.getElementById('cost-per-asset').textContent = '(' + parts.join(' | ') + ')';
  }
}
// Listen to global inputs + all per-asset bid price & token changes
document.getElementById('inp-tokens').addEventListener('input', updateCostPreview);
_acAssets.forEach(a => {
  const bp = document.getElementById('inp-ac-' + a + '-bid_price');
  if (bp) bp.addEventListener('input', updateCostPreview);
  const tk = document.getElementById('inp-ac-' + a + '-tokens_per_side');
  if (tk) tk.addEventListener('input', updateCostPreview);
  const en = document.getElementById('inp-enabled-' + a);
  if (en) en.addEventListener('change', updateCostPreview);
});
updateCostPreview();

// ── Toggle asset config panel ──
let assetPanelOpen = false;
function toggleAssetPanel() {
  const panel = document.getElementById('asset-config-panel');
  const btn = document.getElementById('btn-asset-panel');
  assetPanelOpen = !assetPanelOpen;
  if (assetPanelOpen) {
    panel.style.maxHeight = '300px';
    btn.innerHTML = '&#9650; Asset Filters &#9650;';
  } else {
    panel.style.maxHeight = '0';
    btn.innerHTML = '&#9660; Asset Filters &#9660;';
  }
}

// ── Apply params ──
function applyParams() {
  const price = parseFloat(document.getElementById('inp-price').value);
  const tokens = parseInt(document.getElementById('inp-tokens').value);
  const windowOpen = parseInt(document.getElementById('inp-window-open').value);
  const windowClose = parseInt(document.getElementById('inp-window-close').value);
  const obEnabled = document.getElementById('inp-ob-filter').checked;
  const obMinSize = parseFloat(document.getElementById('inp-ob-min-size').value);
  const obMaxImbalance = parseFloat(document.getElementById('inp-ob-max-imbalance').value);

  // If user changed the global "Bid $", propagate to all per-asset bid prices
  // so the top-level control acts as a "set all" master
  if (isDirty('inp-price')) {
    _acAssets.forEach(a => {
      const el = document.getElementById('inp-ac-' + a + '-bid_price');
      if (el) el.value = price;
    });
  }
  // If user changed the global "Shares/Side", propagate to all per-asset tokens
  if (isDirty('inp-tokens')) {
    _acAssets.forEach(a => {
      const el = document.getElementById('inp-ac-' + a + '-tokens_per_side');
      if (el) el.value = tokens;
    });
  }
  // If user changed the global "Bid Start", propagate to all per-asset window open
  if (isDirty('inp-window-open')) {
    _acAssets.forEach(a => {
      const el = document.getElementById('inp-ac-' + a + '-bid_window_open');
      if (el) el.value = windowOpen;
    });
  }

  // Build full per-asset config from UI
  const assetConfig = {};
  _acAssets.forEach(a => {
    assetConfig[a] = {
      enabled: document.getElementById('inp-enabled-' + a).checked,
      bid_price: parseFloat(document.getElementById('inp-ac-' + a + '-bid_price').value) || 0.02,
      tokens_per_side: parseInt(document.getElementById('inp-ac-' + a + '-tokens_per_side').value) || 100,
      bid_window_open: parseInt(document.getElementById('inp-ac-' + a + '-bid_window_open').value) || 120,
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

// ── Scalper: Apply Params ──
function applyScalperParams() {
  socket.emit('update_scalper_params', {
    trade_size_usd: parseFloat(document.getElementById('sc-trade-size').value),
    entry_ask_max: parseFloat(document.getElementById('sc-entry-max').value),
    profit_target: parseFloat(document.getElementById('sc-profit').value),
    stop_loss: parseFloat(document.getElementById('sc-stop').value),
    cooldown_secs: parseInt(document.getElementById('sc-cooldown').value),
    exit_before_close: parseInt(document.getElementById('sc-exit-before').value),
    max_open_positions: parseInt(document.getElementById('sc-max-pos').value)
  });
}

// ── Scalper: Start / Stop ──
function toggleScalper() {
  if (scalperRunning) {
    socket.emit('stop_scalper');
  } else {
    socket.emit('start_scalper');
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

  // Params (only update if user isn't editing — use dirty flag + activeElement)
  if (!isDirty('inp-price') && document.activeElement.id !== 'inp-price')
    document.getElementById('inp-price').value = d.bid_price;
  if (!isDirty('inp-tokens') && document.activeElement.id !== 'inp-tokens')
    document.getElementById('inp-tokens').value = d.tokens_per_side;
  if (!isDirty('inp-window-open') && document.activeElement.id !== 'inp-window-open')
    document.getElementById('inp-window-open').value = d.bid_window_open;
  if (!isDirty('inp-window-close') && document.activeElement.id !== 'inp-window-close')
    document.getElementById('inp-window-close').value = d.bid_window_close;
  // Per-asset full config sync
  if (d.asset_config) {
    _acAssets.forEach(a => {
      const cfg = d.asset_config[a];
      if (!cfg) return;
      // Enabled checkbox (always sync — no dirty flag for checkboxes)
      const enEl = document.getElementById('inp-enabled-' + a);
      if (enEl) enEl.checked = cfg.enabled;
      // Numeric filter params (respect dirty flag)
      _acFields.forEach(f => {
        const id = 'inp-ac-' + a + '-' + f;
        if (!isDirty(id) && document.activeElement.id !== id) {
          const el = document.getElementById(id);
          if (el && cfg[f] !== undefined) el.value = cfg[f];
        }
      });
    });
  }
  document.getElementById('inp-ob-filter').checked = d.ob_filter_enabled;
  if (!isDirty('inp-ob-min-size') && document.activeElement.id !== 'inp-ob-min-size')
    document.getElementById('inp-ob-min-size').value = d.ob_min_size;
  if (!isDirty('inp-ob-max-imbalance') && document.activeElement.id !== 'inp-ob-max-imbalance')
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

  // ══════════ SCALPER TAB RENDERING ══════════
  if (d.scalper) {
    const sc = d.scalper;
    scalperRunning = sc.running;

    // Scalper button
    const scBtn = document.getElementById('btn-scalper');
    if (sc.running) {
      scBtn.textContent = 'STOP SCALPER';
      scBtn.className = 'btn btn-stop';
    } else {
      scBtn.textContent = 'START SCALPER';
      scBtn.className = 'btn btn-start';
    }

    // Scalper params (dirty-flag protected)
    if (!isDirty('sc-trade-size') && document.activeElement.id !== 'sc-trade-size')
      document.getElementById('sc-trade-size').value = sc.trade_size_usd;
    if (!isDirty('sc-entry-max') && document.activeElement.id !== 'sc-entry-max')
      document.getElementById('sc-entry-max').value = sc.entry_ask_max;
    if (!isDirty('sc-profit') && document.activeElement.id !== 'sc-profit')
      document.getElementById('sc-profit').value = sc.profit_target;
    if (!isDirty('sc-stop') && document.activeElement.id !== 'sc-stop')
      document.getElementById('sc-stop').value = sc.stop_loss;
    if (!isDirty('sc-cooldown') && document.activeElement.id !== 'sc-cooldown')
      document.getElementById('sc-cooldown').value = sc.cooldown_secs;
    if (!isDirty('sc-exit-before') && document.activeElement.id !== 'sc-exit-before')
      document.getElementById('sc-exit-before').value = sc.exit_before_close;
    if (!isDirty('sc-max-pos') && document.activeElement.id !== 'sc-max-pos')
      document.getElementById('sc-max-pos').value = sc.max_open_positions;

    // BTC info in scalper tab
    if (d.btc_price > 0) {
      document.getElementById('sc-btc-price').textContent = '$' + d.btc_price.toLocaleString();
      document.getElementById('sc-btc-open').textContent = '$' + d.btc_candle_open.toLocaleString();
      document.getElementById('sc-btc-dist').textContent = '$' + d.btc_distance.toFixed(0);
    }

    // Stats
    document.getElementById('sc-st-trades').textContent = sc.total_trades;
    document.getElementById('sc-st-wins').textContent = sc.winning_trades;
    document.getElementById('sc-st-losses').textContent = sc.losing_trades;
    document.getElementById('sc-st-winrate').textContent = sc.win_rate + '%';
    document.getElementById('sc-st-markets').textContent = sc.markets_tracked;

    const scPnl = document.getElementById('sc-st-pnl');
    scPnl.textContent = (sc.total_pnl >= 0 ? '+$' : '-$') + Math.abs(sc.total_pnl).toFixed(2);
    scPnl.className = 'stat-value ' + (sc.total_pnl > 0 ? 'green' : sc.total_pnl < 0 ? 'red' : '');

    // Active positions
    const posEl = document.getElementById('sc-positions');
    if (!sc.positions || sc.positions.length === 0) {
      posEl.innerHTML = '<div class="empty">No active positions</div>';
    } else {
      let posHtml = '';
      sc.positions.forEach(p => {
        const pnlCls = p.pnl > 0 ? 'green' : p.pnl < 0 ? 'red' : '';
        const pnlStr = (p.pnl >= 0 ? '+' : '') + p.pnl.toFixed(4);
        const sideColor = p.side === 'UP' ? 'var(--green)' : 'var(--red)';
        posHtml += '<div class="pos-card">' +
          '<div class="pos-side" style="color:' + sideColor + '">' + p.side + '</div>' +
          '<div style="flex:1">' +
            '<div style="font-weight:600">' + (p.question || p.market_id.substring(0,12) + '...') + '</div>' +
            '<div style="font-size:0.85em;color:#aaa">' +
              'Entry: $' + p.entry_price.toFixed(3) +
              ' | Ask: $' + p.current_ask.toFixed(3) +
              ' | Bid: $' + p.current_bid.toFixed(3) +
              ' | ' + fmtTime(p.secs_left) + ' left' +
            '</div>' +
          '</div>' +
          '<div class="pos-pnl ' + pnlCls + '">' + pnlStr + '</div>' +
          '<div class="pos-state">' + p.state + '</div>' +
          '</div>';
      });
      posEl.innerHTML = posHtml;
    }

    // Trade history table
    const scHistBody = document.getElementById('sc-history-body');
    if (sc.history && sc.history.length > 0) {
      let hHtml = '';
      sc.history.forEach(h => {
        const pnlCls = h.pnl > 0 ? 'green' : h.pnl < 0 ? 'red' : '';
        const pnlStr = (h.pnl >= 0 ? '+$' : '-$') + Math.abs(h.pnl).toFixed(4);
        hHtml += '<tr>' +
          '<td>' + (h.side || '--') + '</td>' +
          '<td>$' + (h.entry_price || 0).toFixed(3) + '</td>' +
          '<td>$' + (h.exit_price || 0).toFixed(3) + '</td>' +
          '<td class="' + pnlCls + '">' + pnlStr + '</td>' +
          '<td>' + (h.exit_reason || h.state || '--') + '</td>' +
          '</tr>';
      });
      scHistBody.innerHTML = hHtml;
    }

    // Scalper activity log
    const scLogEl = document.getElementById('sc-log-body');
    if (sc.activity && sc.activity.length > 0) {
      let logHtml = '';
      sc.activity.forEach(e => {
        logHtml += '<div class="log-line"><span class="log-ts">' + e.ts +
          '</span><span class="log-msg ' + (e.level || 'info') + '">' + e.msg + '</span></div>';
      });
      scLogEl.innerHTML = logHtml;
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
  const ahEvents = d.afterhours_events || [];
  ahRenderAll(ahEvents);
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

  ahRenderTable(filtered);
}

function ahRenderTable(events) {
  const tbody = document.getElementById('ah-events-body');
  if (!tbody) return;
  if (events.length === 0) {
    tbody.innerHTML = '<tr><td colspan="15" class="empty">No events match current filters.</td></tr>';
    return;
  }

  let html = '';
  events.forEach(e => {
    const sideCls = 'ah-side ' + e.side;
    const secsStr = e.secs_after.toFixed(0) + 's';
    const priceStr = '$' + e.fill_price.toFixed(2);
    const costStr  = '$' + e.fill_cost.toFixed(2);
    const sharesStr = e.fill_amount > 0 ? Math.round(e.fill_amount).toString() : '--';
    const upAskStr   = e.up_ask   > 0 ? '$' + e.up_ask.toFixed(3)   : '--';
    const downAskStr = e.down_ask > 0 ? '$' + e.down_ask.toFixed(3) : '--';
    const rangePctStr = (e.range_pct >= 0) ? e.range_pct.toFixed(1) + '%' : '--';
    const rangePctColor = e.range_pct <= 25 ? 'var(--green)' : (e.range_pct <= 50 ? 'var(--yellow)' : 'var(--red)');

    // Winner / Result styling
    const winnerStr = e.winner !== '--' ? e.winner : '<span style="color:#666">--</span>';
    let resultStr = e.won;
    let resultCls = '';
    if (e.won === 'WIN')  resultCls = 'color:var(--green);font-weight:700';
    else if (e.won === 'LOSS') resultCls = 'color:var(--red);font-weight:700';
    else resultCls = 'color:#666';

    // Source badge
    const srcStr = e.is_ours
      ? '<span style="background:var(--green);color:#000;padding:1px 6px;border-radius:3px;font-size:11px">OUR BID</span>'
      : '<span style="background:var(--border);color:var(--cyan);padding:1px 6px;border-radius:3px;font-size:11px">OBSERVED</span>';

    // Compact market name
    let mktName = e.question;
    const tmMatch = mktName.match(/(\d{1,2}:\d{2}[AP]M\s*-\s*\d{1,2}:\d{2}[AP]M\s*ET)/);
    mktName = tmMatch ? tmMatch[1] : (mktName.length > 40 ? mktName.substring(0, 37) + '...' : mktName);

    html += '<tr>' +
      '<td>' + e.ts_str + '</td>' +
      '<td style="font-weight:700;color:var(--cyan)">' + e.asset + '</td>' +
      '<td title="' + e.question.replace(/"/g, '&quot;') + '">' + mktName + '</td>' +
      '<td><span class="' + sideCls + '">' + e.side + '</span></td>' +
      '<td>' + sharesStr + '</td>' +
      '<td style="color:var(--yellow)">' + priceStr + '</td>' +
      '<td>' + costStr + '</td>' +
      '<td>' + secsStr + '</td>' +
      '<td style="font-size:11px">' + (e.snap_label || '--') + '</td>' +
      '<td>' + winnerStr + '</td>' +
      '<td style="' + resultCls + '">' + resultStr + '</td>' +
      '<td>' + upAskStr + '</td>' +
      '<td>' + downAskStr + '</td>' +
      '<td style="color:' + rangePctColor + ';font-weight:700">' + rangePctStr + '</td>' +
      '<td>' + srcStr + '</td>' +
      '</tr>';
  });
  tbody.innerHTML = html;
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

// ── Scalper log push ──
socket.on('scalper_log', (e) => {
  const logEl = document.getElementById('sc-log-body');
  const div = document.createElement('div');
  div.className = 'log-line';
  div.innerHTML = '<span class="log-ts">' + e.ts +
    '</span><span class="log-msg ' + (e.level || 'info') + '">' + e.msg + '</span>';
  logEl.prepend(div);
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

    # ── Restore saved settings (bid prices, filters, scalper config) ──
    _load_settings()
    engine.add_log("Settings loaded from disk", "info")

    # ── Start always-on PCM background monitor (tracks ALL markets) ──
    _pcm_thread = threading.Thread(target=pcm_background_loop, daemon=True, name="pcm-bg")
    _pcm_thread.start()
    engine.add_log("Post-Close Monitor started (always-on, all markets)", "info")

    # ── Bot & Scalper do NOT auto-start — user must click Start in dashboard ──
    engine.add_log("Waiting for manual start from dashboard...", "info")

    socketio.run(app, host="0.0.0.0", port=5050, debug=False, allow_unsafe_werkzeug=True)
