"""
Polybot Snipez — Market Data Recorder
Saves every price evaluation tick to CSV for backtesting & analysis.

Output files (in data/ directory):
  - ticks_YYYY-MM-DD.csv     — every price evaluation (per-market, per-cycle)
  - markets_YYYY-MM-DD.csv   — market metadata (discovered markets)
  - signals_YYYY-MM-DD.csv   — only rows where combined < threshold (near-misses + fires)

90c -> 99c strategy (strategy_9099.py):
  - candidates_YYYY-MM-DD.csv         — every threshold crossing + why we did or did not trade it
  - candidate_outcomes_YYYY-MM-DD.csv — what happened next (targets, depth, resolution)
  - trades_9099_YYYY-MM-DD.csv        — one row per trade, entry through exit
"""

import csv
import os
import threading
import time
from datetime import datetime, timezone


# ── Data directory ──────────────────────────────────────────────────────────
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)

_today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

TICK_FILE = os.path.join(DATA_DIR, f"ticks_{_today_str}.csv")
MARKET_FILE = os.path.join(DATA_DIR, f"markets_{_today_str}.csv")
SIGNAL_FILE = os.path.join(DATA_DIR, f"signals_{_today_str}.csv")

# CSV headers
TICK_HEADERS = [
    "timestamp",           # ISO UTC
    "epoch",               # unix timestamp (for precise sorting)
    "market_id",           # condition ID
    "market_name",         # human-readable (e.g. "3:05PM-3:10PM ET")
    "secs_remaining",      # seconds until market close
    "up_ask",              # best ask price for Up token
    "up_ask_size",         # depth at best ask (Up)
    "up_bid",              # best bid price for Up token
    "up_bid_size",         # depth at best bid (Up)
    "down_ask",            # best ask price for Down token
    "down_ask_size",       # depth at best ask (Down)
    "down_bid",            # best bid price for Down token
    "down_bid_size",       # depth at best bid (Down)
    "combined_ask",        # up_ask + down_ask
    "spread",              # combined_ask - 1.0 (market efficiency)
    "source",              # "ws" or "http"
    "fired",               # True if this tick triggered a trade
]

MARKET_HEADERS = [
    "timestamp",
    "market_id",
    "question",
    "token_id_up",
    "token_id_down",
    "end_time",
    "status",
]

SIGNAL_HEADERS = TICK_HEADERS  # Same schema, just filtered


class DataRecorder:
    """Thread-safe CSV writer for market data."""

    def __init__(self):
        self._lock = threading.Lock()
        self._tick_count = 0
        self._signal_count = 0
        self._market_count = 0
        self._started = False

        # Buffer writes for performance — flush every N rows or T seconds
        self._tick_buffer: list[list] = []
        self._signal_buffer: list[list] = []
        self._buffer_size = 100        # flush every 100 rows
        self._last_flush = time.time()
        self._flush_interval = 5.0     # or every 5 seconds

    def start(self):
        """Initialize CSV files with headers if they don't exist."""
        if self._started:
            return
        self._started = True
        self._ensure_headers(TICK_FILE, TICK_HEADERS)
        self._ensure_headers(MARKET_FILE, MARKET_HEADERS)
        self._ensure_headers(SIGNAL_FILE, SIGNAL_HEADERS)

    def record_tick(self, market_id: str, market_name: str,
                    secs_remaining: float,
                    up_ask: float, up_ask_size: float,
                    up_bid: float, up_bid_size: float,
                    down_ask: float, down_ask_size: float,
                    down_bid: float, down_bid_size: float,
                    source: str = "ws", fired: bool = False):
        """Record a single price evaluation tick."""
        now = datetime.now(timezone.utc)
        epoch = time.time()
        combined = up_ask + down_ask
        spread = combined - 1.0

        row = [
            now.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],  # ms precision
            f"{epoch:.3f}",
            market_id,
            market_name,
            f"{secs_remaining:.1f}",
            f"{up_ask:.4f}",
            f"{up_ask_size:.1f}",
            f"{up_bid:.4f}",
            f"{up_bid_size:.1f}",
            f"{down_ask:.4f}",
            f"{down_ask_size:.1f}",
            f"{down_bid:.4f}",
            f"{down_bid_size:.1f}",
            f"{combined:.4f}",
            f"{spread:.4f}",
            source,
            str(fired),
        ]

        with self._lock:
            self._tick_buffer.append(row)
            self._tick_count += 1

            # Also record as signal if combined is noteworthy (< 1.0 = any discount)
            if combined < 1.0:
                self._signal_buffer.append(row)
                self._signal_count += 1

            # Flush if buffer is full or enough time has passed
            if (len(self._tick_buffer) >= self._buffer_size or
                    time.time() - self._last_flush >= self._flush_interval):
                self._flush()

    def record_market(self, market_id: str, question: str,
                      token_id_up: str, token_id_down: str,
                      end_time: datetime, status: str = "active"):
        """Record a newly discovered market."""
        now = datetime.now(timezone.utc)
        row = [
            now.strftime("%Y-%m-%d %H:%M:%S"),
            market_id,
            question,
            token_id_up,
            token_id_down,
            end_time.strftime("%Y-%m-%d %H:%M:%S"),
            status,
        ]
        with self._lock:
            self._market_count += 1
            self._write_rows(MARKET_FILE, [row])

    def flush(self):
        """Force flush all buffered data to disk."""
        with self._lock:
            self._flush()

    def stats(self) -> dict:
        """Return recording statistics."""
        return {
            "ticks": self._tick_count,
            "signals": self._signal_count,
            "markets": self._market_count,
            "buffer": len(self._tick_buffer),
        }

    # ── Internal ────────────────────────────────────────────────────────

    def _flush(self):
        """Write buffered data to disk (must hold self._lock)."""
        if self._tick_buffer:
            self._write_rows(TICK_FILE, self._tick_buffer)
            self._tick_buffer = []
        if self._signal_buffer:
            self._write_rows(SIGNAL_FILE, self._signal_buffer)
            self._signal_buffer = []
        self._last_flush = time.time()

    def _write_rows(self, filepath: str, rows: list[list]):
        """Append rows to a CSV file."""
        try:
            with open(filepath, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerows(rows)
        except Exception:
            pass  # Don't let recording failures crash the bot

    def _ensure_headers(self, filepath: str, headers: list[str]):
        """Write CSV headers if file doesn't exist or is empty."""
        if not os.path.exists(filepath) or os.path.getsize(filepath) == 0:
            try:
                with open(filepath, "w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow(headers)
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════════════════
#  90c -> 99c STRATEGY DATA COLLECTION
#
#  Three append-only CSVs, rotated by UTC date:
#    candidates_*.csv         one row every time a side crosses the entry
#                             threshold — traded or not, with the reason
#    candidate_outcomes_*.csv what happened to that candidate afterwards
#                             (max/min, first touch of each target, depth,
#                             resolution) — this is the file that answers
#                             "how often does 90c reach 99c with X s left?"
#    trades_9099_*.csv        one row per actual trade, entry through exit
#
#  A generic CsvLog is used so adding a column later is a header edit, and a
#  row with unknown/missing keys still writes cleanly.
# ══════════════════════════════════════════════════════════════════════════════

class CsvLog:
    """Thread-safe, date-rotating, append-only CSV writer keyed by column name."""

    def __init__(self, prefix: str, headers: list[str]):
        self.prefix = prefix
        self.headers = headers
        self._lock = threading.Lock()
        self._date = ""
        self._path = ""
        self.rows_written = 0

    @property
    def path(self) -> str:
        """Current file path, rolling over at UTC midnight."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._date:
            self._date = today
            self._path = os.path.join(DATA_DIR, f"{self.prefix}_{today}.csv")
            self._ensure_headers(self._path)
        return self._path

    def write(self, row: dict):
        """Append one row. Unknown keys are ignored, missing keys blank."""
        try:
            path = self.path
            values = [_csv_value(row.get(h, "")) for h in self.headers]
            with self._lock:
                with open(path, "a", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow(values)
                self.rows_written += 1
        except Exception:
            pass  # recording must never crash the trading loop

    def _ensure_headers(self, path: str):
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            try:
                with open(path, "w", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow(self.headers)
            except Exception:
                pass


def _csv_value(v):
    """Render a value for CSV — floats rounded, bools as True/False."""
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, float):
        return f"{v:.6f}".rstrip("0").rstrip(".") if v else "0"
    if v is None:
        return ""
    return v


CANDIDATE_HEADERS = [
    "candidate_id",
    "timestamp",              # ISO UTC of the trigger
    "epoch",
    "market_id",
    "asset",
    "question",
    "market_start_time",      # expiration - 300s (5-min window)
    "market_end_time",
    "secs_remaining",
    "side",                   # "Up" / "Down"
    "observe_level",          # which threshold this crossing is for
    "token_id",
    "side_price",             # the price that crossed the threshold (ask)
    "opposite_price",
    "bid",
    "ask",
    "spread",
    "bid_depth",              # shares at best bid
    "ask_depth",              # shares at best ask
    "available_liquidity",    # shares buyable at or under our entry limit
    "tp_depth",               # shares bid at/above the take-profit price
    "data_age",
    # ── settlement reference ──
    # These markets resolve on a Chainlink TWAP-60s stream. Binance is a
    # labelled proxy; the official columns stay empty unless we really read
    # the official source.
    "binance_price",
    "binance_window_open",
    "binance_window_twap",
    "binance_window_coverage",
    "binance_window_samples",
    "proxy_twap_distance",
    "proxy_spot_distance",
    "proxy_reference_source",
    "chainlink_feed_price",
    "chainlink_feed_age",
    "chainlink_feed_available",
    "official_resolution_reference",
    "official_reference_source",
    "official_reference_available",
    "official_reference_reason",
    "distance_from_official_reference",
    "favourable_distance",
    "distance_source",
    "distance_is_official",
    "reference_note",
    "entry_threshold",        # config in force at the time
    "tp_price",
    "max_secs_remaining",
    "qualified",              # True/False
    "reason_qualified",
    "reason_rejected",
    "traded",
    "trade_id",
    "mode",                   # PAPER / LIVE
]

CANDIDATE_OUTCOME_HEADERS = [
    "candidate_id",
    "market_id",
    "asset",
    "side",
    "observe_level",
    "trigger_timestamp",
    "trigger_epoch",
    "trigger_price",
    "trigger_secs_remaining",
    "traded",
    "trade_id",
    "max_price_after",
    "max_price_at",           # seconds after trigger
    "min_price_after",
    "min_price_at",
    "reached_95", "secs_to_95", "depth_at_95",
    "reached_97", "secs_to_97", "depth_at_97",
    "reached_98", "secs_to_98", "depth_at_98",
    "reached_99", "secs_to_99", "depth_at_99",
    "tp_target",              # take-profit price tracked
    "reached_tp",             # market printed the TP price (bid side)
    "secs_to_tp",
    "max_depth_at_tp",        # best depth seen bid at/above TP
    "our_tp_filled",          # did OUR resting order actually fill
    # ── shadow take-profit: would a sell rested HERE have filled? ──
    # tp_price_reached      the market got there
    # tp_queue_adjusted_fill enough volume went through to reach us
    # The difference between these two columns is the strategy's real edge.
    "shadow_qty",
    "shadow_queue_ahead",
    "shadow_level_size",
    "shadow_level_size_min",
    "shadow_consumed",
    "tp_price_reached",
    "secs_to_tp_price_reached",
    "tp_queue_adjusted_fill",
    "secs_to_queue_adjusted_fill",
    "queue_fill_evidence",
    "ticks_observed",
    "final_price",
    "resolution",             # "Up" / "Down" / "unknown"
    "prediction_correct",     # did the side we flagged win
    "closed_reason",
]

TRADE_9099_HEADERS = [
    "trade_id",
    "candidate_id",
    "mode",                   # PAPER / LIVE
    "market_id",
    "asset",
    "question",
    "side",
    "token_id",
    "market_end_time",
    # ── signal + entry ──
    "signal_timestamp",
    "signal_secs_remaining",
    "entry_submitted_at",
    "entry_requested_price",
    "entry_requested_qty",
    "entry_order_id",
    "entry_order_type",       # GTC limit
    "entry_liquidity",        # maker / taker / unknown
    "entry_fill_timestamp",
    "entry_fill_price",
    "entry_filled_qty",
    "entry_partial",
    "entry_fee_estimated",
    "entry_fee_actual",
    "entry_fee_source",       # estimate | clob_trades
    "entry_cost",
    # ── take profit ──
    "tp_submitted_at",
    "tp_price",
    "tp_qty",
    "tp_order_id",
    "tp_liquidity",
    "tp_fill_timestamp",
    "tp_fill_price",
    "tp_filled_qty",
    "tp_partial",
    "tp_fee_estimated",
    "tp_fee_actual",
    "tp_fee_source",
    # ── queue evidence ──
    "tp_queue_ahead",
    "tp_level_size_at_submit",
    "tp_level_size_min",
    "tp_bid_depth_at_submit",
    "tp_secs_remaining_at_submit",
    "tp_consumed_volume",
    "tp_price_reached",
    "tp_price_reached_at",
    "tp_queue_adjusted_fill",
    "tp_fill_evidence",
    # ── stop / emergency exit ──
    "stop_price",
    "stop_triggered_at",
    "stop_reason",
    "exit_order_id",
    "exit_liquidity",
    "final_exit_price",
    "final_exit_qty",
    "exit_fee_actual",
    "exit_reason",            # tp_filled / stop / expiry / resolution / ...
    # ── result ──
    "gross_pnl",
    "fees_total",
    "realized_pnl",
    "realized_pnl_pct",
    "closed_at",
    "hold_secs",
    # ── post-entry price behaviour ──
    "max_price_after_entry",
    "min_price_after_entry",
    "secs_to_95", "secs_to_97", "secs_to_98", "secs_to_99",
    "reached_99",
    "liquidity_at_99",
    "tp_actually_filled",
    # ── market outcome ──
    "market_result",
    "prediction_correct",
    "settled_value",          # $1.00 or $0.00 per share if held to resolution
    # ── context at entry ──
    "underlying_price",
    "settlement_threshold",
    "distance_from_threshold",
    "spread_at_entry",
    "ask_depth_at_entry",
    "tp_depth_at_entry",
    # ── fees: the signing parameter and the economic rate are NOT the same ──
    "fee_rate_raw",           # order feeRateBps (a ceiling, not a price)
    "economic_fee_rate",      # feeSchedule.rate — what actually costs money
    "fee_exponent",
    "fee_taker_only",
    "fee_schedule_source",
    # ── settlement reference ──
    "proxy_cross_seen",
    "proxy_cross_at",
    "binance_price",
    "binance_window_open",
    "binance_window_twap",
    "binance_window_coverage",
    "binance_window_samples",
    "proxy_twap_distance",
    "proxy_spot_distance",
    "proxy_reference_source",
    "chainlink_feed_price",
    "chainlink_feed_age",
    "chainlink_feed_available",
    "official_resolution_reference",
    "official_reference_source",
    "official_reference_available",
    "official_reference_reason",
    "distance_from_official_reference",
    "favourable_distance",
    "distance_source",
    "distance_is_official",
    "reference_note",
    "bankroll_before",
    "bankroll_after",
]

candidate_log = CsvLog("candidates", CANDIDATE_HEADERS)
candidate_outcome_log = CsvLog("candidate_outcomes", CANDIDATE_OUTCOME_HEADERS)
trade_9099_log = CsvLog("trades_9099", TRADE_9099_HEADERS)


# ── Module-level singleton ──────────────────────────────────────────────────
recorder = DataRecorder()
