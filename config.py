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

# ── Limit Bid Strategy ─────────────────────────────────────────────────────
BID_PRICE = float(os.getenv("BID_PRICE", "0.02"))       # Limit buy price per share
TOKENS_PER_SIDE = int(os.getenv("TOKENS_PER_SIDE", "150"))  # Shares per side
MAX_RISK_PER_MARKET = float(os.getenv("MAX_RISK_PER_MARKET", "50"))  # Max $ per market

# ── Hard Safety Caps (cannot be overridden by dashboard or learner) ─────────
HARD_MAX_BID_PRICE = 0.50       # ABSOLUTE ceiling — no bid above $0.50/share ever
HARD_MAX_TOKENS = 500           # ABSOLUTE ceiling — no more than 500 shares/side
MAX_DAILY_SPEND = float(os.getenv("MAX_DAILY_SPEND", "200"))  # Max $ spent per day (0=unlimited)

# ── Time Window (seconds before market close) ──────────────────────────────
BID_WINDOW_OPEN = int(os.getenv("BID_WINDOW_OPEN", "7"))     # Post bids when <=7s left
BID_WINDOW_CLOSE = int(os.getenv("BID_WINDOW_CLOSE", "0"))   # Stop posting when <=0s left

# ── Polling ─────────────────────────────────────────────────────────────────
MARKET_POLL_INTERVAL = int(os.getenv("MARKET_POLL_INTERVAL", "60"))

# ── Risk Management ────────────────────────────────────────────────────────
CANCEL_DELAY_AFTER_CLOSE = int(os.getenv("CANCEL_DELAY_AFTER_CLOSE", "30"))
MAX_BIDS_PER_DAY = int(os.getenv("MAX_BIDS_PER_DAY", "0"))  # 0 = unlimited

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
