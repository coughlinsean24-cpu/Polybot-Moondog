"""
Polybot Snipez — Polymarket Fee Model

Three different numbers get called "the fee". They are kept apart here,
because conflating the first two overstated our costs by ~43%:

  fee_rate_raw       The ORDER SIGNING parameter. The CLOB's
                     GET /fee-rate returns {"base_fee": 1000} and Gamma
                     reports makerBaseFee = takerBaseFee = 1000 on the same
                     market. It is the feeRateBps field carried inside the
                     signed order — a ceiling the order authorises, not a
                     price. The giveaway is that the MAKER side reads 1000 on
                     a market whose schedule says makers are never charged.

  economic_fee_rate  What actually costs money. Gamma's feeSchedule, which on
                     every 5-minute crypto market (BTC/ETH/SOL/XRP, feeType
                     crypto_fees_v2) reads:
                         {rate: 0.07, exponent: 1, takerOnly: true,
                          rebateRate: 0.2}
                     matching the published crypto category rate of 0.07:
                         fee = size * rate * (p * (1 - p)) ** exponent
                     charged in USDC, takers only.

  actual_fee         What the exchange says it charged on our executed order.
                     Read back from the CLOB trades endpoint in live mode and
                     preferred over any formula. Unavailable in paper mode,
                     where the estimate is all there is — and is labelled so.

Worked example at the 0.07 economic rate (was 0.10 under the old reading):
    27 shares @ $0.90 -> 27 * 0.07 * 0.9 * 0.1 = $0.17   (0.63c/share)
    resting sell @ $0.99, maker                 = $0.00
Still ~7% of a 9c gross move, so it is never ignored — just no longer
inflated by half again.

The maker rebateRate (0.2) is NOT modelled: a rebate would only improve our
side, and assuming money we have not seen would be the same mistake in the
other direction.
"""

import threading
import time

import requests

import config

# Fees are rounded to 5 decimal places by the exchange; smaller is dropped.
FEE_DECIMALS = 5
MIN_FEE = 0.00001

_FEE_RATE_URL = "/fee-rate"
_GAMMA_MARKETS = "/markets"

_lock = threading.Lock()
_signing_cache: dict[str, tuple] = {}    # token_id -> (bps, ts)
_schedule_cache: dict[str, tuple] = {}   # key -> (Schedule, ts)
_CACHE_TTL = 3600.0


class Schedule:
    """A market's economic fee schedule."""

    __slots__ = ("rate", "exponent", "taker_only", "rebate_rate", "source")

    def __init__(self, rate: float, exponent: float = 1.0, taker_only: bool = True,
                 rebate_rate: float = 0.0, source: str = "default"):
        self.rate = rate
        self.exponent = exponent
        self.taker_only = taker_only
        self.rebate_rate = rebate_rate
        self.source = source     # "gamma" when read live, "default" when not

    def as_dict(self) -> dict:
        return {
            "economic_fee_rate": self.rate,
            "fee_exponent": self.exponent,
            "fee_taker_only": self.taker_only,
            "fee_rebate_rate": self.rebate_rate,
            "fee_schedule_source": self.source,
        }

    def __repr__(self):
        return (f"Schedule(rate={self.rate}, exponent={self.exponent}, "
                f"taker_only={self.taker_only}, source={self.source})")


def default_schedule() -> Schedule:
    return Schedule(
        rate=config.S9099_FEE_ECONOMIC_RATE_DEFAULT,
        exponent=config.S9099_FEE_EXPONENT_DEFAULT,
        taker_only=config.S9099_FEE_TAKER_ONLY_DEFAULT,
        source="default",
    )


# ── the signing parameter (NOT a price) ──────────────────────────────────

def signing_fee_rate_bps(token_id: str, timeout: float = 4.0) -> float:
    """
    The order's feeRateBps parameter, from GET /fee-rate.

    Recorded for completeness and for signing; never used to price a fill.
    """
    if not token_id:
        return config.S9099_FEE_SIGNING_BPS_DEFAULT
    now = time.time()
    with _lock:
        hit = _signing_cache.get(token_id)
        if hit and now - hit[1] < _CACHE_TTL:
            return hit[0]

    bps = config.S9099_FEE_SIGNING_BPS_DEFAULT
    try:
        resp = requests.get(f"{config.CLOB_URL}{_FEE_RATE_URL}",
                            params={"token_id": token_id}, timeout=timeout)
        if resp.status_code == 200:
            value = float(resp.json().get("base_fee", 0) or 0)
            if value > 0:
                bps = value
    except Exception:
        pass
    with _lock:
        _signing_cache[token_id] = (bps, now)
    return bps


# ── the economic schedule (what costs money) ─────────────────────────────

def schedule_for_market(condition_id: str = "", token_id: str = "",
                        timeout: float = 5.0) -> Schedule:
    """
    Read this market's feeSchedule from Gamma. Falls back to the configured
    default — never to zero, so a lookup failure cannot make a trade look
    cheaper than it is.
    """
    key = condition_id or token_id
    if not key:
        return default_schedule()
    now = time.time()
    with _lock:
        hit = _schedule_cache.get(key)
        if hit and now - hit[1] < _CACHE_TTL:
            return hit[0]

    schedule = default_schedule()
    try:
        params = ({"condition_ids": condition_id} if condition_id
                  else {"clob_token_ids": token_id})
        resp = requests.get(f"{config.GAMMA_URL}{_GAMMA_MARKETS}",
                            params=params, timeout=timeout)
        if resp.status_code == 200:
            data = resp.json()
            market = data[0] if isinstance(data, list) and data else {}
            raw = market.get("feeSchedule") or {}
            if market.get("feesEnabled") is False:
                schedule = Schedule(rate=0.0, source="gamma_fees_disabled")
            elif raw.get("rate") is not None:
                schedule = Schedule(
                    rate=float(raw.get("rate", 0) or 0),
                    exponent=float(raw.get("exponent", 1) or 1),
                    taker_only=bool(raw.get("takerOnly", True)),
                    rebate_rate=float(raw.get("rebateRate", 0) or 0),
                    source="gamma",
                )
    except Exception:
        pass
    with _lock:
        _schedule_cache[key] = (schedule, now)
    return schedule


# ── estimates ────────────────────────────────────────────────────────────

def estimate_fee(shares: float, price: float, is_taker: bool,
                 schedule: Schedule | None = None) -> float:
    """
    Estimated USDC fee for a fill:  size * rate * (p * (1 - p)) ** exponent

    Makers pay nothing under `taker_only` (which every 5-min crypto market
    currently sets), unless S9099_FEES_CHARGE_MAKER is turned on to model a
    future change.
    """
    sched = schedule or default_schedule()
    if shares <= 0 or price <= 0 or price >= 1 or sched.rate <= 0:
        return 0.0
    if not is_taker and sched.taker_only and not config.S9099_FEES_CHARGE_MAKER:
        return 0.0
    variance = price * (1.0 - price)
    if sched.exponent != 1:
        variance = variance ** sched.exponent
    fee = round(shares * sched.rate * variance, FEE_DECIMALS)
    return fee if fee >= MIN_FEE else 0.0


# Kept so existing callers and tests keep working; both now take a Schedule.
def taker_fee(shares: float, price: float, schedule: Schedule | None = None) -> float:
    return estimate_fee(shares, price, True, schedule)


def maker_fee(shares: float, price: float, schedule: Schedule | None = None) -> float:
    return estimate_fee(shares, price, False, schedule)


def fee_for_fill(shares: float, price: float, is_taker: bool,
                 schedule: Schedule | None = None) -> float:
    return estimate_fee(shares, price, is_taker, schedule)


# ── actual fees, straight from the exchange ──────────────────────────────

def actual_fee_for_order(order_id: str, trades_fn=None) -> tuple:
    """
    (fee, source) for one of our executed orders, from the CLOB trades feed.

    Returns (None, reason) when the exchange cannot tell us — paper mode, no
    credentials, no trades yet, or a response with no fee field. Callers must
    fall back to the estimate and label it as such, never silently.
    """
    if not order_id:
        return None, "no_order_id"
    if config.PAPER_TRADING:
        return None, "paper_mode"
    if not config.S9099_USE_ACTUAL_FEES:
        return None, "disabled"
    try:
        if trades_fn is None:
            from polymarket_client import get_trades_for_order as trades_fn  # noqa: N813
        trades = trades_fn(order_id) or []
    except Exception as e:
        return None, f"lookup_error:{type(e).__name__}"
    if not trades:
        return None, "no_trades_reported"

    total = 0.0
    found = False
    for trade in trades:
        for key in ("fee", "fee_amount", "taker_fee", "fee_paid", "fees"):
            if key in trade and trade[key] not in (None, ""):
                try:
                    total += float(trade[key])
                    found = True
                except (TypeError, ValueError):
                    continue
                break
    if not found:
        return None, "no_fee_field_in_trades"
    return round(total, FEE_DECIMALS), "clob_trades"


def clear_cache():
    """Drop cached schedules and signing rates (tests, or after a config change)."""
    with _lock:
        _signing_cache.clear()
        _schedule_cache.clear()
