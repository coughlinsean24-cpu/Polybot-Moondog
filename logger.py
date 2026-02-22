"""
Polybot Snipez — Logging & Monitoring Module
Structured logging for trades, signals, and bot health.
"""

import logging
import os
import json
from datetime import datetime, timezone

# ── Log directory setup ─────────────────────────────────────────────────────
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

# ── Date-stamped log files ──────────────────────────────────────────────────
today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

# Main bot log (all events)
BOT_LOG_FILE = os.path.join(LOG_DIR, f"bot_{today_str}.log")

# Trade-specific log (only fires and outcomes)
TRADE_LOG_FILE = os.path.join(LOG_DIR, f"trades_{today_str}.jsonl")


def _setup_logger(name: str, log_file: str, level=logging.DEBUG) -> logging.Logger:
    """Create a logger with both file and console handlers."""
    logger = logging.getLogger(name)
    logger.setLevel(level)

    # Avoid duplicate handlers on re-import
    if logger.handlers:
        return logger

    # File handler — detailed
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))

    # Console handler — clean output
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%H:%M:%S"
    ))

    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


# The main bot logger
log = _setup_logger("polybot", BOT_LOG_FILE)


def log_signal(market_id: str, secs_remaining: float,
               best_ask_up: float, best_ask_down: float,
               depth_up: float, depth_down: float,
               combined: float, fired: bool, reason: str = ""):
    """Log every signal evaluation (fired or skipped)."""
    status = "FIRE" if fired else "SKIP"
    msg = (
        f"[{status}] market={market_id} | "
        f"secs={secs_remaining:.0f} | "
        f"up_ask={best_ask_up:.3f} down_ask={best_ask_down:.3f} "
        f"combined={combined:.3f} | "
        f"depth_up={depth_up:.0f} depth_down={depth_down:.0f}"
    )
    if reason:
        msg += f" | reason={reason}"

    if fired:
        log.info(msg)
    else:
        log.debug(msg)


def log_trade(market_id: str, side: str, token_id: str,
              price: float, size: int, order_id: str,
              secs_remaining: float, paper: bool = False,
              **extra):
    """Log an individual order placement.

    Accepts optional keyword args that are merged into the JSONL record:
        btc_price, btc_candle_open, btc_distance, btc_dollar_range,
        btc_prior_candle_range, up_ask, up_bid, up_ask_size, up_bid_size,
        down_ask, down_bid, down_ask_size, down_bid_size, combined_ask, etc.
    """
    trade_type = "PAPER" if paper else "LIVE"
    log.info(
        f"[ORDER-{trade_type}] market={market_id} side={side} "
        f"price={price:.4f} size={size} order_id={order_id} "
        f"secs_remaining={secs_remaining:.0f}"
    )

    # Also append to JSONL trade log
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "type": "order_placed",
        "paper": paper,
        "market_id": market_id,
        "side": side,
        "token_id": token_id,
        "price": price,
        "size": size,
        "order_id": order_id,
        "secs_remaining": round(secs_remaining, 1),
    }
    # Merge any extra context fields (BTC price, orderbook snapshot, etc.)
    if extra:
        record.update(extra)
    _append_jsonl(record)


def log_fire_pair(market_id: str, best_ask_up: float, best_ask_down: float,
                  combined: float, target_tokens: int,
                  secs_remaining: float, paper: bool = False):
    """Log when both sides are fired together."""
    trade_type = "PAPER" if paper else "LIVE"
    expected_cost = combined * target_tokens
    expected_payout = 0.99 * target_tokens
    expected_profit = expected_payout - expected_cost
    roi = (expected_profit / expected_cost * 100) if expected_cost > 0 else 0

    log.info(
        f"[FIRE-{trade_type}] market={market_id} | "
        f"up={best_ask_up:.3f} down={best_ask_down:.3f} combined={combined:.3f} | "
        f"tokens={target_tokens} cost=${expected_cost:.2f} | "
        f"expected_payout=${expected_payout:.2f} expected_profit=${expected_profit:.2f} "
        f"ROI={roi:.0f}% | secs={secs_remaining:.0f}"
    )

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "type": "fire_pair",
        "paper": paper,
        "market_id": market_id,
        "up_ask": best_ask_up,
        "down_ask": best_ask_down,
        "combined": combined,
        "target_tokens": target_tokens,
        "expected_cost": round(expected_cost, 4),
        "expected_payout": round(expected_payout, 4),
        "expected_profit": round(expected_profit, 4),
        "roi_pct": round(roi, 1),
        "secs_remaining": round(secs_remaining, 1),
    }
    _append_jsonl(record)


def log_outcome(market_id: str, result: str, details: str = ""):
    """Log market outcome after settlement."""
    log.info(f"[OUTCOME] market={market_id} result={result} {details}")
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "type": "outcome",
        "market_id": market_id,
        "result": result,
        "details": details,
    }
    _append_jsonl(record)


def log_cancel(market_id: str, order_id: str, reason: str, **extra):
    """Log order cancellation."""
    log.info(f"[CANCEL] market={market_id} order_id={order_id} reason={reason}")
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "type": "cancel",
        "market_id": market_id,
        "order_id": order_id,
        "reason": reason,
    }
    if extra:
        record.update(extra)
    _append_jsonl(record)


def log_resolution(market_id: str, question: str, winner_side: str,
                   up_filled: bool, down_filled: bool,
                   up_fill_size: float, down_fill_size: float,
                   bid_price: float, pnl: float, **extra):
    """Log market resolution result with full context."""
    log.info(
        f"[RESOLUTION] market={market_id} winner={winner_side} "
        f"up_filled={up_filled} down_filled={down_filled} pnl=${pnl:.2f}"
    )
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "type": "resolution",
        "market_id": market_id,
        "question": question,
        "winner_side": winner_side,
        "up_filled": up_filled,
        "down_filled": down_filled,
        "up_fill_size": up_fill_size,
        "down_fill_size": down_fill_size,
        "bid_price": bid_price,
        "pnl": round(pnl, 4),
    }
    if extra:
        record.update(extra)
    _append_jsonl(record)


def log_error(context: str, error: Exception):
    """Log an error with context."""
    log.error(f"[ERROR] {context}: {type(error).__name__}: {error}")


def log_heartbeat(active_markets: int, fired_today: int, paper: bool):
    """Periodic health log."""
    mode = "PAPER" if paper else "LIVE"
    log.info(
        f"[HEARTBEAT-{mode}] active_markets={active_markets} "
        f"fired_today={fired_today}"
    )


def _append_jsonl(record: dict):
    """Append a JSON record to the trade log file."""
    try:
        with open(TRADE_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        log.error(f"Failed to write trade log: {e}")
