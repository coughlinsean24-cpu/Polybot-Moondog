"""
Polybot Snipez — Market Data Recorder
Saves every price evaluation tick to CSV for backtesting & analysis.

Output files (in data/ directory):
  - ticks_YYYY-MM-DD.csv     — every price evaluation (per-market, per-cycle)
  - markets_YYYY-MM-DD.csv   — market metadata (discovered markets)
  - signals_YYYY-MM-DD.csv   — only rows where combined < threshold (near-misses + fires)
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


# ── Module-level singleton ──────────────────────────────────────────────────
recorder = DataRecorder()
