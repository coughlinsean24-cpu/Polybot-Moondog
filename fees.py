"""
Polybot Snipez — Polymarket Fee Model

Polymarket charges TAKERS only.  The fee is quoted in USDC and scales with
the variance of the outcome, so it is largest at 50c and shrinks toward the
extremes:

    fee_usdc = shares * rate * price * (1 - price)

`rate` is the market's base fee, published by the CLOB in basis points via
GET /fee-rate?token_id=...  ("base_fee": 1000 == 0.10).  The docs quote 0.07
for the crypto category, but the 5-minute Up/Down markets this bot trades
report 1000 bps, so the rate is always read live per token and only falls
back to the configured default when the endpoint cannot be reached.

Makers are not charged.  A resting sell that is never crossed by us costs
nothing, which is the whole reason the take-profit leg is a resting limit.

Worked example (entry at 90c, rate 0.10):
    1 share  -> 0.10 * 0.90 * 0.10 = $0.009   (0.9c per share)
    556 sh   -> $5.00 on a $500 entry
That is ~10% of the 9c gross move, so it is never ignored in P&L.
"""

import threading
import time

import requests

import config

# Fees are rounded to 5 decimal places by the exchange; anything smaller is
# dropped entirely.
FEE_DECIMALS = 5
MIN_FEE = 0.00001

_FEE_RATE_URL = "/fee-rate"

# token_id -> (rate, fetched_at)
_rate_cache: dict[str, tuple[float, float]] = {}
_cache_lock = threading.Lock()
_CACHE_TTL = 3600.0  # base fees do not move intraday


def default_rate() -> float:
    """Configured fallback fee coefficient (not bps)."""
    return max(0.0, config.S9099_FEE_RATE_DEFAULT_BPS / 10_000.0)


def fee_rate_for_token(token_id: str, timeout: float = 4.0) -> float:
    """
    Return the market's fee coefficient for a token (e.g. 0.10).

    Reads GET {CLOB_URL}/fee-rate?token_id=... and caches the answer.
    Falls back to the configured default on any failure — never raises, and
    never returns a rate lower than the default, so a dead endpoint cannot
    make a trade look cheaper than it is.
    """
    if not token_id:
        return default_rate()

    now = time.time()
    with _cache_lock:
        cached = _rate_cache.get(token_id)
        if cached and now - cached[1] < _CACHE_TTL:
            return cached[0]

    rate = default_rate()
    try:
        resp = requests.get(
            f"{config.CLOB_URL}{_FEE_RATE_URL}",
            params={"token_id": token_id},
            timeout=timeout,
        )
        if resp.status_code == 200:
            bps = float(resp.json().get("base_fee", 0) or 0)
            if bps > 0:
                rate = bps / 10_000.0
    except Exception:
        pass  # keep the default — fee estimates must never block a decision

    with _cache_lock:
        _rate_cache[token_id] = (rate, now)
    return rate


def taker_fee(shares: float, price: float, rate: float | None = None) -> float:
    """
    USDC fee for a TAKER fill of `shares` at `price`.

        fee = shares * rate * price * (1 - price)
    """
    if shares <= 0 or price <= 0 or price >= 1:
        return 0.0
    r = default_rate() if rate is None else rate
    fee = shares * r * price * (1.0 - price)
    fee = round(fee, FEE_DECIMALS)
    return fee if fee >= MIN_FEE else 0.0


def maker_fee(shares: float, price: float, rate: float | None = None) -> float:
    """
    USDC fee for a MAKER fill.  Zero under the published fee schedule.

    S9099_FEES_CHARGE_MAKER exists only so the bot keeps reporting honest
    numbers if Polymarket ever starts charging makers; leave it false.
    """
    if not config.S9099_FEES_CHARGE_MAKER:
        return 0.0
    return taker_fee(shares, price, rate)


def fee_for_fill(shares: float, price: float, is_taker: bool,
                 rate: float | None = None) -> float:
    """Fee for one fill, picking the maker/taker schedule."""
    return taker_fee(shares, price, rate) if is_taker else maker_fee(shares, price, rate)


def clear_cache():
    """Drop cached fee rates (tests, or after a config change)."""
    with _cache_lock:
        _rate_cache.clear()
