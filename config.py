"""
Polybot Snipez — Configuration Module
Loads all settings from .env and provides typed access to bot parameters.

Strategy: Post low-ball limit BUY orders on BOTH Up and Down sides of BTC
5-minute markets, then wait for panicked sellers to fill them. One side
always pays $1.00 at settlement. Profit = $1.00 × shares − total cost.
"""

import os
from dotenv import load_dotenv

# Load .env from the same directory as this file
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))


# ── Polymarket API Credentials ──────────────────────────────────────────────
POLYMARKET_API_KEY = os.getenv("POLYMARKET_API_KEY", "")
POLYMARKET_API_SECRET = os.getenv("POLYMARKET_API_SECRET", "")
POLYMARKET_PASSPHRASE = os.getenv("POLYMARKET_PASSPHRASE", "")
POLYMARKET_PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")
POLYMARKET_PROXY_ADDRESS = os.getenv("POLYMARKET_PROXY_ADDRESS", "")

# ── Polymarket API Endpoints ────────────────────────────────────────────────
CLOB_URL = os.getenv("POLYMARKET_CLOB_URL", "https://clob.polymarket.com")
GAMMA_URL = os.getenv("POLYMARKET_GAMMA_URL", "https://gamma-api.polymarket.com")

# ── Polygon Chain ID (137 = mainnet) ────────────────────────────────────────
CHAIN_ID = 137

# ── Trading Toggle ──────────────────────────────────────────────────────────
TRADING_ENABLED = os.getenv("TRADING_ENABLED", "true").lower() == "true"
PAPER_TRADING = os.getenv("PAPER_TRADING", "true").lower() == "true"

# ── Manual-Only Mode ────────────────────────────────────────────────────────
# When True, NO code path may place an order on its own. Every order must
# originate from an explicit click in the dashboard. This disables:
#   - the 5-min Up/Down auto-bid engine (post_bids)
#   - the combined-ask arbitrage engine's automatic scanning
#   - the scanner's auto-bid sweep
# and refuses the dashboard toggles that would turn any of them back on.
# Manual bids, manual exits and manual cancels are unaffected.
# Defaults to True — automation is opt-in, not opt-out.
MANUAL_ONLY = os.getenv("MANUAL_ONLY", "true").lower() == "true"

# ── Limit Bid Strategy ─────────────────────────────────────────────────────
BID_PRICE = float(os.getenv("BID_PRICE", "0.01"))       # Limit buy price per share (lowered from $0.02 to $0.01)
TOKENS_PER_SIDE = int(os.getenv("TOKENS_PER_SIDE", "100"))  # Shares per side (locked at 100 for consistency)
MAX_RISK_PER_MARKET = float(os.getenv("MAX_RISK_PER_MARKET", "10"))  # Max $ per market (tightened from $50)

# ── Hard Safety Caps (cannot be overridden by dashboard or learner) ─────────
HARD_MAX_BID_PRICE = 0.50       # ABSOLUTE ceiling — no bid above $0.50/share ever
HARD_MAX_TOKENS = 500           # ABSOLUTE ceiling — no more than 500 shares/side
MAX_DAILY_SPEND = float(os.getenv("MAX_DAILY_SPEND", "40"))  # Max $ spent per day (tightened from $200)

# ── Time Window (seconds before market close) ──────────────────────────────
BID_WINDOW_OPEN = int(os.getenv("BID_WINDOW_OPEN", "60"))    # Cancel loser side at T-60s
BID_WINDOW_CLOSE = int(os.getenv("BID_WINDOW_CLOSE", "0"))   # Stop posting when <=0s left

# ── Polling ─────────────────────────────────────────────────────────────────
MARKET_POLL_INTERVAL = int(os.getenv("MARKET_POLL_INTERVAL", "60"))

# ── Risk Management ────────────────────────────────────────────────────────
CANCEL_DELAY_AFTER_CLOSE = int(os.getenv("CANCEL_DELAY_AFTER_CLOSE", "30"))
MAX_BIDS_PER_DAY = int(os.getenv("MAX_BIDS_PER_DAY", "20"))  # Hard cap: max 20 bids per day (was unlimited)

# ── Combined-Ask Arbitrage ────────────────────────────────────────────────
# Buy BOTH sides when up_ask + down_ask < $1.00.  One side always pays $1.
# Guaranteed profit = $1.00 − combined_cost per share.
ARB_ENABLED = os.getenv("ARB_ENABLED", "true").lower() == "true"
ARB_MIN_EDGE = float(os.getenv("ARB_MIN_EDGE", "0.03"))       # Min profit per $1 payout (3¢)
ARB_TRADE_SIZE = float(os.getenv("ARB_TRADE_SIZE", "5.00"))   # Max $ per arb trade
ARB_MAX_POSITIONS = int(os.getenv("ARB_MAX_POSITIONS", "3"))   # Max concurrent arb positions
ARB_COOLDOWN = float(os.getenv("ARB_COOLDOWN", "30"))         # Seconds between arbs on same market
ARB_FILL_TIMEOUT = float(os.getenv("ARB_FILL_TIMEOUT", "45")) # Cancel unfilled arb orders after Ns
ARB_MAX_DAILY_SPEND = float(os.getenv("ARB_MAX_DAILY_SPEND", "25.00"))  # Daily spend cap for arb

# ── Manual Exit Defaults ───────────────────────────────────────────────────
# Default resting exit price for manually placed bids, in $ per share.
# A buy that fills is followed by a GTC limit SELL at this price, placed as
# soon as the conditional tokens settle into the wallet.
MANUAL_EXIT_PRICE = float(os.getenv("MANUAL_EXIT_PRICE", "0.85"))
MANUAL_EXIT_ENABLED = os.getenv("MANUAL_EXIT_ENABLED", "true").lower() == "true"
# How long to keep polling for token settlement before giving up (seconds).
MANUAL_SETTLE_TIMEOUT = float(os.getenv("MANUAL_SETTLE_TIMEOUT", "120"))


# ── Scalper Defaults ───────────────────────────────────────────────────────
SCALP_TRADE_SIZE = float(os.getenv("SCALP_TRADE_SIZE", "2.50"))      # $ per scalp trade
SCALP_ENTRY_ASK_MAX = float(os.getenv("SCALP_ENTRY_ASK_MAX", "0.50"))
SCALP_PROFIT_TARGET = float(os.getenv("SCALP_PROFIT_TARGET", "0.04"))
SCALP_STOP_LOSS = float(os.getenv("SCALP_STOP_LOSS", "0.05"))
SCALP_COOLDOWN = float(os.getenv("SCALP_COOLDOWN", "15"))
SCALP_EXIT_BEFORE = int(os.getenv("SCALP_EXIT_BEFORE", "30"))
SCALP_MAX_POSITIONS = int(os.getenv("SCALP_MAX_POSITIONS", "2"))


def validate_config() -> list[str]:
    """Return a list of configuration errors. Empty list = all good."""
    errors = []
    if not POLYMARKET_API_KEY:
        errors.append("POLYMARKET_API_KEY is not set")
    if not POLYMARKET_API_SECRET:
        errors.append("POLYMARKET_API_SECRET is not set")
    if not POLYMARKET_PASSPHRASE:
        errors.append("POLYMARKET_PASSPHRASE is not set")
    if not POLYMARKET_PRIVATE_KEY:
        errors.append("POLYMARKET_PRIVATE_KEY is not set")
    if BID_PRICE <= 0 or BID_PRICE > HARD_MAX_BID_PRICE:
        errors.append(f"BID_PRICE={BID_PRICE} out of range (0, {HARD_MAX_BID_PRICE}]")
    if TOKENS_PER_SIDE < 1:
        errors.append(f"TOKENS_PER_SIDE={TOKENS_PER_SIDE} must be >= 1")
    if MAX_RISK_PER_MARKET <= 0:
        errors.append(f"MAX_RISK_PER_MARKET={MAX_RISK_PER_MARKET} must be > 0")
    if BID_WINDOW_OPEN < BID_WINDOW_CLOSE:
        errors.append(f"BID_WINDOW_OPEN={BID_WINDOW_OPEN} must be >= BID_WINDOW_CLOSE={BID_WINDOW_CLOSE}")
    return errors


def print_config_summary():
    """Print a human-readable summary of the active configuration."""
    mode = "PAPER TRADING" if PAPER_TRADING else "LIVE TRADING"
    enabled = "ENABLED" if TRADING_ENABLED else "PAUSED"
    manual = "MANUAL ONLY (no auto-trading)" if MANUAL_ONLY else "AUTOMATION ALLOWED"
    cost_per_market = BID_PRICE * TOKENS_PER_SIDE * 2
    profit_if_both = (TOKENS_PER_SIDE * 1.0) - cost_per_market
    profit_if_one = (TOKENS_PER_SIDE * 1.0) - (BID_PRICE * TOKENS_PER_SIDE)
    daily = "Unlimited" if MAX_BIDS_PER_DAY == 0 else str(MAX_BIDS_PER_DAY)
    print(f"""
+======================================================+
|          POLYBOT SNIPEZ -- CONFIGURATION              |
+======================================================+
|  Mode:              {mode:<35}|
|  Trading:           {enabled:<35}|
|  Orders:            {manual:<35}|
|  CLOB URL:          {CLOB_URL:<35}|
+------------------------------------------------------+
|  LIMIT BID STRATEGY                                  |
|  Bid price:         ${BID_PRICE:<34}|
|  Shares per side:   {TOKENS_PER_SIDE:<35}|
|  Cost if both fill: ${cost_per_market:<34.2f}|
|  Profit (both):     ${profit_if_both:<34.2f}|
|  Profit (one side): ${profit_if_one:<34.2f}|
+------------------------------------------------------+
|  TIMING                                              |
|  Post bids:         {BID_WINDOW_OPEN}s - {BID_WINDOW_CLOSE}s before close{' ' * (16 - len(str(BID_WINDOW_OPEN)) - len(str(BID_WINDOW_CLOSE)))}|
|  Cancel delay:      {CANCEL_DELAY_AFTER_CLOSE}s after close{' ' * (23 - len(str(CANCEL_DELAY_AFTER_CLOSE)))}|
|  Max bids/day:      {daily:<35}|
+------------------------------------------------------+
|  Max risk/market:   ${MAX_RISK_PER_MARKET:<34}|
+======================================================+
""")


# ══════════════════════════════════════════════════════════════════════════════
#  90c -> 99c LATE-MARKET STRATEGY  (strategy_9099.py)
#
#  Buy a side once it trades up to ~$0.90 late in a 5-minute market, then rest
#  a limit SELL at ~$0.99 for the shares that actually filled.  The aim is the
#  90->99 move, not settlement.
#
#  Every value below is a knob; nothing in the strategy hardcodes a threshold.
# ══════════════════════════════════════════════════════════════════════════════

# ── Live-trading ignition (two keys, fails closed) ─────────────────────────
# Real orders require ALL of: LIVE_TRADING=true, PAPER_TRADING=false,
# TRADING_ENABLED=true, MANUAL_ONLY=false.  Any missing/absent value leaves
# the strategy in paper mode — a typo can never promote it to live.
LIVE_TRADING = os.getenv("LIVE_TRADING", "false").lower() == "true"


def strategy_9099_is_live() -> bool:
    """True only when every gate is explicitly open. Fail closed."""
    return bool(
        LIVE_TRADING
        and not PAPER_TRADING
        and TRADING_ENABLED
        and not MANUAL_ONLY
    )


def strategy_9099_mode() -> str:
    """'LIVE' or 'PAPER' — what the strategy will actually do."""
    return "LIVE" if strategy_9099_is_live() else "PAPER"


def strategy_9099_block_reasons() -> list[str]:
    """Why the strategy is in paper mode (empty list = it is live)."""
    reasons = []
    if not LIVE_TRADING:
        reasons.append("LIVE_TRADING is false")
    if PAPER_TRADING:
        reasons.append("PAPER_TRADING is true")
    if not TRADING_ENABLED:
        reasons.append("TRADING_ENABLED is false")
    if MANUAL_ONLY:
        reasons.append("MANUAL_ONLY is true")
    return reasons


# ── Master switches ────────────────────────────────────────────────────────
# ENABLED runs evaluation + candidate data collection (it never places orders
# on its own).  AUTO_TRADE is what lets it act on a qualifying candidate.
S9099_ENABLED = os.getenv("S9099_ENABLED", "true").lower() == "true"
S9099_AUTO_TRADE = os.getenv("S9099_AUTO_TRADE", "true").lower() == "true"
# Kill switch — flip to true (or hit the dashboard button) to stop new entries
# immediately while existing positions are still managed to an exit.
S9099_KILL_SWITCH = os.getenv("S9099_KILL_SWITCH", "false").lower() == "true"

# Assets to watch (subset of the 5-min Up/Down universe)
S9099_ASSETS = [
    a.strip().upper()
    for a in os.getenv("S9099_ASSETS", "BTC,ETH,SOL,XRP").split(",")
    if a.strip()
]

# ── Entry ──────────────────────────────────────────────────────────────────
S9099_ENTRY_PRICE_MIN = float(os.getenv("S9099_ENTRY_PRICE_MIN", "0.90"))
# Do not chase a side that has already run past this — there is no move left.
S9099_ENTRY_PRICE_MAX = float(os.getenv("S9099_ENTRY_PRICE_MAX", "0.96"))
# Pay up to this many ticks above the ask to get filled (0 = ask exactly).
S9099_ENTRY_SLIPPAGE = float(os.getenv("S9099_ENTRY_SLIPPAGE", "0.00"))
# Cancel an entry that has not filled after N seconds.
S9099_ENTRY_TIMEOUT = float(os.getenv("S9099_ENTRY_TIMEOUT", "8"))
# After the FIRST partial fill, wait this long for the rest before giving up,
# cancelling the remainder and resting the take-profit on what we actually got.
# With seconds left on the clock, a resting exit on 20 shares beats waiting.
S9099_PARTIAL_FILL_GRACE = float(os.getenv("S9099_PARTIAL_FILL_GRACE", "1.5"))

# ── Take profit ────────────────────────────────────────────────────────────
S9099_TAKE_PROFIT_PRICE = float(os.getenv("S9099_TAKE_PROFIT_PRICE", "0.99"))

# ── Time-to-expiry filter ─────────────────────────────────────────────────
# Enter only inside [MIN, MAX] seconds remaining.  MIN keeps us from buying
# with too little time for the 99c sell to be hit.
S9099_MAX_SECS_REMAINING = float(os.getenv("S9099_MAX_SECS_REMAINING", "60"))
S9099_MIN_SECS_REMAINING = float(os.getenv("S9099_MIN_SECS_REMAINING", "10"))

# ── Confirmation / market-quality filters ─────────────────────────────────
S9099_MAX_SPREAD = float(os.getenv("S9099_MAX_SPREAD", "0.03"))
# Minimum shares resting at the ask we are about to lift.
S9099_MIN_LIQUIDITY = float(os.getenv("S9099_MIN_LIQUIDITY", "50"))
# Minimum shares bid at/above the take-profit price for the exit to be
# realistic.  0 disables the check (we still record the depth either way).
S9099_MIN_TP_DEPTH = float(os.getenv("S9099_MIN_TP_DEPTH", "0"))
# How far the underlying must sit on the winning side of the 5-min candle
# open (the settlement threshold), as a % of spot.  0.02% of $100k BTC = $20.
S9099_MIN_UNDERLYING_MARGIN_PCT = float(os.getenv("S9099_MIN_UNDERLYING_MARGIN_PCT", "0.02"))
# Reject if the WS price is older than this (seconds).
S9099_MAX_DATA_AGE = float(os.getenv("S9099_MAX_DATA_AGE", "5"))

# ── Position sizing ───────────────────────────────────────────────────────
# "fixed_dollars" -> S9099_FIXED_DOLLARS per entry
# "percent_bankroll" -> S9099_MAX_POSITION_PERCENT of available balance
S9099_POSITION_SIZE_MODE = os.getenv("S9099_POSITION_SIZE_MODE", "fixed_dollars").strip().lower()
S9099_FIXED_DOLLARS = float(os.getenv("S9099_FIXED_DOLLARS", "25"))
S9099_MAX_POSITION_PERCENT = float(os.getenv("S9099_MAX_POSITION_PERCENT", "10"))
# Starting bankroll for paper mode (live mode reads the real USDC balance).
S9099_PAPER_BANKROLL = float(os.getenv("S9099_PAPER_BANKROLL", "500"))
# Hard ceiling on any single entry, whatever the sizing maths says.
S9099_MAX_POSITION_DOLLARS = float(os.getenv("S9099_MAX_POSITION_DOLLARS", "100"))
# Polymarket rejects orders below 5 shares, so a smaller size is no size.
S9099_MIN_SHARES = int(os.getenv("S9099_MIN_SHARES", "5"))

# ── Emergency exit ────────────────────────────────────────────────────────
# Bail out if the side we bought trades down to this bid.
S9099_STOP_PRICE = float(os.getenv("S9099_STOP_PRICE", "0.80"))
# Exit anything still open at T-minus this many seconds.  0 disables and lets
# the position ride into settlement.
S9099_EXIT_BEFORE_EXPIRY = float(os.getenv("S9099_EXIT_BEFORE_EXPIRY", "3"))
# Bail out if the underlying crosses back through the settlement threshold.
S9099_STOP_ON_THRESHOLD_CROSS = os.getenv("S9099_STOP_ON_THRESHOLD_CROSS", "true").lower() == "true"
# Bail out on a fast adverse move: this many cents down inside the window.
S9099_STOP_VELOCITY_DROP = float(os.getenv("S9099_STOP_VELOCITY_DROP", "0.05"))
S9099_STOP_VELOCITY_WINDOW = float(os.getenv("S9099_STOP_VELOCITY_WINDOW", "5"))

# ── Risk limits ───────────────────────────────────────────────────────────
S9099_MAX_OPEN_POSITIONS = int(os.getenv("S9099_MAX_OPEN_POSITIONS", "1"))
S9099_MAX_DAILY_LOSS = float(os.getenv("S9099_MAX_DAILY_LOSS", "50"))
S9099_MAX_CONSECUTIVE_LOSSES = int(os.getenv("S9099_MAX_CONSECUTIVE_LOSSES", "3"))
S9099_MIN_BALANCE = float(os.getenv("S9099_MIN_BALANCE", "50"))
S9099_MAX_TRADES_PER_DAY = int(os.getenv("S9099_MAX_TRADES_PER_DAY", "40"))
# Stop submitting if this many consecutive API calls fail.
S9099_MAX_API_ERRORS = int(os.getenv("S9099_MAX_API_ERRORS", "5"))
# Seconds before the same market may be re-evaluated after a trade closes.
S9099_MARKET_COOLDOWN = float(os.getenv("S9099_MARKET_COOLDOWN", "30"))

# ── Optional opposite-side 1c hedge (off; not part of the MVP) ────────────
S9099_ENABLE_OPPOSITE_HEDGE = os.getenv("S9099_ENABLE_OPPOSITE_HEDGE", "false").lower() == "true"
S9099_HEDGE_MAX_PRICE = float(os.getenv("S9099_HEDGE_MAX_PRICE", "0.01"))

# ── Fees ──────────────────────────────────────────────────────────────────
# Fallback only — the live per-token rate is read from the CLOB's /fee-rate.
# 1000 bps (0.10) is what the 5-min crypto markets currently report.
S9099_FEE_RATE_DEFAULT_BPS = float(os.getenv("S9099_FEE_RATE_DEFAULT_BPS", "1000"))
S9099_FEES_CHARGE_MAKER = os.getenv("S9099_FEES_CHARGE_MAKER", "false").lower() == "true"

# ── Candidate tracking (data collection) ──────────────────────────────────
# Keep following a candidate after the trigger — even one we did not trade —
# so we can measure P(reach 99c | reached 90c with X seconds left).
S9099_TRACK_CANDIDATES = os.getenv("S9099_TRACK_CANDIDATES", "true").lower() == "true"
# Start tracking a candidate as soon as it crosses the threshold with this
# many seconds left, even when that is outside the (narrower) entry window —
# that is what makes the 90s/60s/45s/30s/20s/15s/10s buckets comparable.
S9099_CANDIDATE_MAX_SECS = float(os.getenv("S9099_CANDIDATE_MAX_SECS", "120"))
# Price levels whose first-touch time we record.
S9099_TRACK_TARGETS = [
    float(x) for x in os.getenv("S9099_TRACK_TARGETS", "0.95,0.97,0.98,0.99").split(",") if x.strip()
]
# Keep tracking this long past market close (resolution can lag).
S9099_TRACK_AFTER_CLOSE = float(os.getenv("S9099_TRACK_AFTER_CLOSE", "20"))


def validate_strategy_9099() -> list[str]:
    """Return a list of 90/99 configuration errors. Empty list = all good."""
    errors = []
    if not (0 < S9099_ENTRY_PRICE_MIN < 1):
        errors.append(f"S9099_ENTRY_PRICE_MIN={S9099_ENTRY_PRICE_MIN} must be in (0, 1)")
    if S9099_ENTRY_PRICE_MAX < S9099_ENTRY_PRICE_MIN:
        errors.append("S9099_ENTRY_PRICE_MAX must be >= S9099_ENTRY_PRICE_MIN")
    if not (0 < S9099_TAKE_PROFIT_PRICE < 1):
        errors.append(f"S9099_TAKE_PROFIT_PRICE={S9099_TAKE_PROFIT_PRICE} must be in (0, 1)")
    if S9099_TAKE_PROFIT_PRICE <= S9099_ENTRY_PRICE_MIN:
        errors.append("S9099_TAKE_PROFIT_PRICE must be above S9099_ENTRY_PRICE_MIN")
    if S9099_STOP_PRICE >= S9099_ENTRY_PRICE_MIN:
        errors.append("S9099_STOP_PRICE must be below S9099_ENTRY_PRICE_MIN")
    if S9099_MIN_SECS_REMAINING > S9099_MAX_SECS_REMAINING:
        errors.append("S9099_MIN_SECS_REMAINING must be <= S9099_MAX_SECS_REMAINING")
    if S9099_POSITION_SIZE_MODE not in ("fixed_dollars", "percent_bankroll"):
        errors.append(
            f"S9099_POSITION_SIZE_MODE={S9099_POSITION_SIZE_MODE!r} must be "
            "'fixed_dollars' or 'percent_bankroll'"
        )
    if not (0 < S9099_MAX_POSITION_PERCENT <= 100):
        errors.append(f"S9099_MAX_POSITION_PERCENT={S9099_MAX_POSITION_PERCENT} must be in (0, 100]")
    if S9099_MAX_POSITION_DOLLARS <= 0:
        errors.append("S9099_MAX_POSITION_DOLLARS must be > 0")
    if S9099_MIN_SHARES < 5:
        errors.append("S9099_MIN_SHARES must be >= 5 (Polymarket minimum order size)")
    return errors
