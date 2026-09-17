"""
Polybot Snipez — 90c -> 99c Late-Market Strategy

    watch 5-min crypto markets
      -> a side trades up to ~$0.90 late in the window
      -> confirmation + liquidity + sizing checks
      -> BUY that side
      -> the moment the entry fills, rest a limit SELL at ~$0.99
         for the shares that ACTUALLY filled
      -> take profit, or emergency-exit if it reverses
      -> capital recycles into the next qualifying market

The point is the 90->99 move, not settlement.  Holding to resolution is the
fallback, not the plan.

Everything here is built on the infrastructure that already exists:
    PriceFeed (ws_feed)          live Polymarket order books
    BinanceFeed (binance_ws)     underlying spot + the 5-min candle open,
                                 which IS the settlement threshold
    polymarket_client            orders, cancels, balances, resolution
    data_recorder                CSV logs
    fees                         Polymarket's real taker fee formula

Two things in this module are deliberately paranoid:

1.  Duplicate orders.  Every market gets one Position and one lock.  An order
    is only ever submitted on a phase transition taken while holding that
    lock, and each (market, intent) pair can be submitted exactly once —
    replaying the same market update ten times cannot place a second order.

2.  Live trading.  Orders reach Polymarket only through LiveBroker, which is
    only constructed when config.strategy_9099_is_live() returns True — that
    needs LIVE_TRADING=true AND PAPER_TRADING=false AND TRADING_ENABLED=true
    AND MANUAL_ONLY=false.  Anything missing or misspelled leaves PaperBroker
    in place, which has no import path to the order functions at all.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

import config
import fees
from settlement_ref import SettlementReference
from logger import log
from data_recorder import candidate_log, candidate_outcome_log, trade_9099_log

# Where open positions are parked so a restart can pick them back up.
STATE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "strategy_9099_state.json"
)

WINDOW_SECONDS = 300  # 5-minute markets


# ══════════════════════════════════════════════════════════════════════════
#  Phases
# ══════════════════════════════════════════════════════════════════════════

class Phase:
    """Lifecycle of one position. Transitions only ever move down this list."""
    IDLE = "IDLE"                    # nothing submitted
    ENTRY_PENDING = "ENTRY_PENDING"  # entry buy is live, waiting on a fill
    POSITION_OPEN = "POSITION_OPEN"  # shares held, take-profit not resting yet
    TP_PENDING = "TP_PENDING"        # take-profit sell is resting
    EXIT_PENDING = "EXIT_PENDING"    # emergency exit is working
    CLOSED = "CLOSED"                # done — recorded, capital released


ACTIVE_PHASES = (Phase.ENTRY_PENDING, Phase.POSITION_OPEN,
                 Phase.TP_PENDING, Phase.EXIT_PENDING)


# ══════════════════════════════════════════════════════════════════════════
#  Brokers
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class OrderState:
    """Normalised view of one order, whatever placed it."""
    order_id: str
    side: str                 # BUY / SELL
    token_id: str
    price: float
    size: float
    filled: float = 0.0
    avg_price: float = 0.0
    status: str = "live"      # live / partial / matched / cancelled / unknown
    liquidity: str = "unknown"  # maker / taker
    created: float = 0.0

    # ── queue bookkeeping (paper sells) ──
    queue_ahead: float = 0.0        # shares that must trade before ours can
    consumed: float = 0.0           # shares traded through our level since we joined
    level_size_at_submit: float = 0.0
    level_size_min: float = 0.0
    level_size_last: float = 0.0
    fill_evidence: str = ""         # marketable / queue_consumed / trade_through
    # Maker or taker is decided by the book WHEN WE SUBMIT: an order that
    # rested and was later hit is a maker fill (free) even though the bid
    # reaches our price at the moment it trades.
    marketable_at_submit: bool = False
    # The old, optimistic answer, kept side by side so the two can be compared.
    optimistic_filled: bool = False
    optimistic_fill_ts: float = 0.0
    queue_fill_ts: float = 0.0

    @property
    def done(self) -> bool:
        return self.status in ("matched", "cancelled")


@dataclass
class QueueFill:
    """How much of a resting sell the executed tape can justify filling."""
    consumed: float = 0.0
    queue_ahead: float = 0.0
    fillable: float = 0.0          # shares of OURS the tape has reached
    optimistic: bool = False       # the market merely quoted/printed our price
    evidence: str = ""             # why we believe we were executed


def evaluate_queue_fill(px, price: float, submitted_ts: float,
                        queue_ahead: float, qty: float,
                        trade_through: bool = True,
                        crossed_on_entry: bool = False) -> QueueFill:
    """
    Decide how much of a resting SELL at `price` the tape justifies filling.

    The book only tells us a price EXISTS. Executed trade prints tell us
    volume actually went through, and only volume ahead of us being consumed
    gets us filled:

        consumed  = taker BUY volume at or below our price since we joined
                    (better-priced asks are lifted before ours)
        fillable  = consumed - queue_ahead

    Three things count as proof rather than inference:
      crossed       the bid was already at/above our price when we SUBMITTED,
                    so we were the aggressor and traded on arrival
      level_cleared while we rested, the bid reached our price AND the ask
                    side moved above it — every offer at our price, ours
                    included, is gone
      trade_through something printed ABOVE our offer while we rested, which
                    cannot happen unless our offer was taken first

    A bid at our price with the ask still sitting there is a crossed book,
    which cannot persist on a real CLOB — it means the two sides of the feed
    are momentarily out of step. That is not a fill, and treating it as one
    would smuggle the old optimism back in.

    Deliberately conservative: it ignores cancellations ahead of us in the
    queue, which in reality would move us up. Better to under-report fills
    than to build a strategy on ones we never got.
    """
    state = QueueFill(queue_ahead=max(0.0, queue_ahead))
    if qty <= 0:
        return state

    best_bid = getattr(px, "best_bid", 0.0) or 0.0
    best_ask = getattr(px, "best_ask", 0.0) or 0.0
    bid_size = getattr(px, "best_bid_size", 0.0) or 0.0

    if crossed_on_entry:
        # We lifted a bid that was already there. Nothing to queue behind.
        state.optimistic = True
        state.fillable = min(qty, bid_size) if bid_size > 0 else qty
        state.evidence = "marketable"
        return state

    if best_bid >= price - 1e-9:
        state.optimistic = True
        if best_ask > price + 1e-9 and not config.S9099_REQUIRE_TAPE_EVIDENCE:
            # Our whole price level has been cleared from the ask side and
            # there is still demand above it. Note this covers the level being
            # CANCELLED as well as traded — either way we move to the front,
            # but it is an inference, which S9099_REQUIRE_TAPE_EVIDENCE drops.
            state.fillable = min(qty, bid_size) if bid_size > 0 else qty
            state.evidence = "level_cleared"
            return state
        # Crossed/locked book — stale feed, not an execution. Fall through to
        # the tape, which cannot be faked by an out-of-step quote.

    state.optimistic = bool(getattr(px, "last_trade_price", 0.0) >= price - 1e-9)

    volume_since = getattr(px, "volume_since", None)
    if volume_since is None:
        # No tape available: fall back to the optimistic read, but say so.
        state.fillable = qty if state.optimistic else 0.0
        state.evidence = "no_tape_optimistic" if state.optimistic else ""
        return state

    state.consumed = volume_since(submitted_ts, side="BUY", max_price=price)
    state.fillable = max(0.0, state.consumed - state.queue_ahead)
    if state.fillable > 0:
        state.evidence = "queue_consumed"

    if trade_through:
        max_print = getattr(px, "max_trade_price_since", lambda _ts: 0.0)(submitted_ts)
        if max_print > price + 1e-9:
            state.fillable = qty
            state.evidence = "trade_through"
            state.optimistic = True
    return state


class PaperBroker:
    """
    Simulated execution against the real live order book.

    Fill rules:

      BUY  at P   fills while best_ask <= P, taking at most the size resting
                  at the ask. We crossed the spread, so it is a TAKER fill.
      SELL at P   marketable (best_bid >= P) fills at once as a TAKER.
      SELL at P   resting above the market is QUEUE-AWARE: it joins behind
                  every ask at or below P and only fills once executed trade
                  prints have eaten through that queue. See
                  evaluate_queue_fill.

    The resting-sell rule used to be "best_bid reached P, so we filled",
    which on a level carrying thousands of shares is wishful thinking. Both
    answers are now kept: `optimistic_filled` for comparison, and the
    queue-adjusted one for the position and the P&L.
    """

    name = "PAPER"

    def __init__(self, feed, starting_balance: float | None = None,
                 book_fn=None, queue_aware: bool | None = None,
                 trade_through: bool | None = None):
        self.feed = feed
        self.balance = float(
            config.S9099_PAPER_BANKROLL if starting_balance is None else starting_balance
        )
        self.orders: dict[str, OrderState] = {}
        self._seq = 0
        self._lock = threading.Lock()
        # Returns the full book for a token; used once per sell to measure the
        # queue in front of us. Without it we cannot know the queue and fall
        # back to the optimistic rule (and label it).
        self.book_fn = book_fn
        self.queue_aware = (config.S9099_QUEUE_AWARE_TP if queue_aware is None
                            else queue_aware)
        self.trade_through = (config.S9099_TRADE_THROUGH_FILLS if trade_through is None
                              else trade_through)

    # ── placement ────────────────────────────────────────────────────────
    def _new_id(self, side: str) -> str:
        with self._lock:
            self._seq += 1
            return f"paper-{side.lower()}-{int(time.time())}-{self._seq}"

    def place_buy(self, token_id: str, price: float, size: float,
                  market_id: str = "") -> str | None:
        oid = self._new_id("buy")
        self.orders[oid] = OrderState(
            order_id=oid, side="BUY", token_id=token_id,
            price=price, size=size, created=time.time(),
        )
        log.info(f"[9099][PAPER] BUY {size:.0f} @ ${price:.3f} ({market_id[:10]}) -> {oid}")
        return oid

    def place_sell(self, token_id: str, price: float, size: float,
                   market_id: str = "") -> str | None:
        oid = self._new_id("sell")
        now = time.time()
        px = self.feed.get_price(token_id)
        order = OrderState(
            order_id=oid, side="SELL", token_id=token_id,
            price=price, size=size, created=now,
            marketable_at_submit=(getattr(px, "best_bid", 0.0) or 0.0) >= price - 1e-9,
        )
        # Measure the queue the moment we join it: every ask at or below our
        # price trades before ours does.
        ahead, level = self.ask_queue_ahead(token_id, price)
        order.queue_ahead = ahead
        order.level_size_at_submit = level
        order.level_size_min = level
        order.level_size_last = level
        self.orders[oid] = order
        log.info(
            f"[9099][PAPER] SELL {size:.0f} @ ${price:.3f} ({market_id[:10]}) -> {oid} "
            f"| {ahead:.0f} sh ahead of us ({level:.0f} at our price)"
        )
        return oid

    def ask_queue_ahead(self, token_id: str, price: float) -> tuple:
        """
        (shares ahead of us, shares resting at our exact price).

        Everything offered at or below our price is in front of a new order
        at that price — cheaper offers get lifted first, and equal offers
        already in the book have time priority.
        """
        if not self.book_fn:
            return 0.0, 0.0
        try:
            book = self.book_fn(token_id) or {}
        except Exception:
            return 0.0, 0.0
        ahead = 0.0
        at_level = 0.0
        for entry in book.get("asks", []):
            try:
                p = float(entry.get("price", 0))
                sz = float(entry.get("size", 0))
            except (TypeError, ValueError):
                continue
            if p <= price + 1e-9:
                ahead += sz
                if abs(p - price) < 1e-9:
                    at_level += sz
        return ahead, at_level

    def cancel(self, order_id: str) -> bool:
        o = self.orders.get(order_id)
        if not o:
            return True            # unknown order is already not working
        if o.status in ("matched", "cancelled"):
            return True
        o.status = "cancelled" if o.filled <= 0 else "partial_cancelled"
        if o.filled > 0:
            o.status = "cancelled"  # keeps whatever filled; rest is gone
        return True

    def status(self, order_id: str) -> OrderState | None:
        self._simulate(order_id)
        return self.orders.get(order_id)

    def available_balance(self) -> float:
        return self.balance

    def restore_order(self, state: OrderState):
        """Re-register an order after a restart so recovery has something to poll."""
        self.orders[state.order_id] = state

    # ── simulation ───────────────────────────────────────────────────────
    def _simulate(self, order_id: str):
        o = self.orders.get(order_id)
        if not o or o.status in ("matched", "cancelled"):
            return
        px = self.feed.get_price(o.token_id)
        if not getattr(px, "valid", False):
            return
        remaining = o.size - o.filled
        if remaining <= 0:
            o.status = "matched"
            return

        if o.side == "BUY":
            ask, ask_size = px.best_ask, px.best_ask_size
            if ask <= 0 or ask > o.price:
                return
            qty = min(remaining, max(0.0, ask_size))
            if qty <= 0:
                return
            self._apply_fill(o, qty, ask, "taker")
            self.balance -= qty * ask
            return

        # ── resting / marketable SELL ────────────────────────────────────
        # Track how the level behaves while we sit on it.
        _, level_now = self.ask_queue_ahead(o.token_id, o.price)
        o.level_size_last = level_now
        o.level_size_min = min(o.level_size_min or level_now, level_now)

        if not self.queue_aware:
            # Legacy optimistic rule, kept only so it can be switched on for
            # a like-for-like comparison.
            bid, bid_size = px.best_bid, px.best_bid_size
            if bid <= 0 or bid < o.price:
                return
            qty = min(remaining, max(0.0, bid_size))
            if qty <= 0:
                return
            o.optimistic_filled = True
            o.fill_evidence = "optimistic_mode"
            self._apply_fill(o, qty, o.price, "maker")
            self.balance += qty * o.price
            return

        state = evaluate_queue_fill(
            px, o.price, o.created, o.queue_ahead, o.size,
            trade_through=self.trade_through,
            crossed_on_entry=o.marketable_at_submit,
        )
        o.consumed = state.consumed
        if state.optimistic and not o.optimistic_filled:
            o.optimistic_filled = True
            o.optimistic_fill_ts = time.time()

        already = o.filled
        target = min(o.size, state.fillable)
        qty = target - already
        if qty <= 1e-9:
            return
        # Crossing on the way in makes us the taker; resting and being hit
        # does not, whatever the book looks like at the moment it trades.
        liq = "taker" if o.marketable_at_submit else "maker"
        o.fill_evidence = state.evidence
        o.queue_fill_ts = o.queue_fill_ts or time.time()
        self._apply_fill(o, min(qty, remaining), o.price, liq)
        self.balance += min(qty, remaining) * o.price

    def _apply_fill(self, o: OrderState, qty: float, price: float, liquidity: str):
        total = o.avg_price * o.filled + price * qty
        o.filled += qty
        o.avg_price = total / o.filled if o.filled else price
        if o.liquidity == "unknown":
            o.liquidity = liquidity
        o.status = "matched" if o.filled >= o.size - 1e-9 else "partial"


class LiveBroker:
    """Real orders on Polymarket. Constructed only when every live gate is open."""

    name = "LIVE"

    def __init__(self):
        if not config.strategy_9099_is_live():
            # Belt and braces: even a direct construction cannot go live
            # unless the config says so.
            raise RuntimeError(
                "LiveBroker refused — live gates are closed: "
                + ", ".join(config.strategy_9099_block_reasons())
            )
        import polymarket_client as pmc  # imported only on the live path
        self._pmc = pmc

    def place_buy(self, token_id: str, price: float, size: float,
                  market_id: str = "") -> str | None:
        return self._pmc.place_limit_buy(
            token_id=token_id, price=round(price, 3), size=int(size), market_id=market_id
        )

    def place_sell(self, token_id: str, price: float, size: float,
                   market_id: str = "") -> str | None:
        return self._pmc.place_limit_sell(
            token_id=token_id, price=round(price, 3), size=int(size), market_id=market_id
        )

    def cancel(self, order_id: str) -> bool:
        return self._pmc.cancel_order(order_id)

    def status(self, order_id: str) -> OrderState | None:
        raw = self._pmc.get_order_status(order_id)
        if not raw:
            return None
        filled = float(raw.get("size_matched", 0) or 0)
        original = float(raw.get("original_size", 0) or 0)
        st = str(raw.get("status", "unknown")).lower()
        if st not in ("live", "matched", "cancelled", "partial", "unknown"):
            st = "unknown"
        price = float(raw.get("price", 0) or 0)
        return OrderState(
            order_id=order_id,
            side=str(raw.get("side", "BUY")).upper(),
            token_id=str(raw.get("token_id", "")),
            price=price,
            size=original,
            filled=filled,
            avg_price=price,
            status=st,
        )

    def available_balance(self) -> float:
        return self._pmc.get_usdc_balance()


def make_broker(feed, book_fn=None) -> PaperBroker | LiveBroker:
    """
    Pick the broker for the current config. Fails closed: anything short of a
    fully-armed live configuration gets the simulator.
    """
    if config.strategy_9099_is_live():
        log.warning("[9099] LIVE BROKER ARMED — real orders, real money")
        return LiveBroker()
    return PaperBroker(feed, book_fn=book_fn)


# ══════════════════════════════════════════════════════════════════════════
#  Candidate tracking
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class Candidate:
    """
    One crossing of ONE observation threshold by one side of one market, plus
    everything that happened to it afterwards — traded or not.

    There is a record per (market, side, level), so 85c / 88c / 90c / 92c /
    95c are each measured on their own terms. The strategy still enters only
    at the configured entry threshold; the rest are observation, and they are
    what will tell us whether 90c was the right choice.

    Each one also carries a SHADOW take-profit: if we had bought here and
    rested a sell at the TP, would the tape have filled it? Both answers are
    kept — the optimistic one (the market printed the price) and the
    queue-adjusted one (enough volume actually went through to reach us).
    """
    candidate_id: str
    market_id: str
    asset: str
    question: str
    side: str
    token_id: str
    end_time: datetime
    level: float = 0.0            # the observation threshold this record is for

    trigger_epoch: float = 0.0
    trigger_price: float = 0.0
    trigger_secs: float = 0.0

    # Snapshot at trigger
    bid: float = 0.0
    ask: float = 0.0
    bid_depth: float = 0.0
    ask_depth: float = 0.0
    opposite_price: float = 0.0
    tp_depth: float = 0.0
    underlying_price: float = 0.0
    threshold: float = 0.0
    distance: float = 0.0
    data_age: float = 0.0

    # Config in force when this candidate was opened, snapshotted so a
    # parameter change mid-session cannot rewrite history.
    tp_target: float = 0.99
    entry_threshold: float = 0.90
    max_secs_cfg: float = 60.0
    track_targets: list = field(default_factory=lambda: [0.95, 0.97, 0.98, 0.99])

    qualified: bool = False
    reason_qualified: str = ""
    reason_rejected: str = ""
    decision_written: bool = False
    traded: bool = False
    trade_id: str = ""

    # Post-trigger tracking (bid-side: what we could actually have sold into)
    max_price: float = 0.0
    max_price_at: float = 0.0
    min_price: float = 1.0
    min_price_at: float = 0.0
    ticks: int = 0
    last_price: float = 0.0
    targets: dict = field(default_factory=dict)   # price -> {"secs":, "depth":}
    max_tp_depth: float = 0.0
    our_tp_filled: bool = False

    # ── shadow take-profit (what a TP placed at this crossing would have done)
    shadow_qty: float = 0.0
    shadow_queue_ahead: float = 0.0     # asks at/below the TP when we joined
    shadow_level_size: float = 0.0      # asks exactly at the TP when we joined
    shadow_level_size_min: float = 0.0
    shadow_consumed: float = 0.0        # volume through the TP level since
    shadow_optimistic_filled: bool = False
    shadow_optimistic_secs: float = 0.0
    shadow_queue_filled: bool = False
    shadow_queue_secs: float = 0.0
    shadow_evidence: str = ""

    # ── settlement reference at the crossing (proxy vs official) ──
    reference: dict = field(default_factory=dict)

    finalised: bool = False
    closed_reason: str = ""
    resolution: str = ""
    _resolve_attempts: int = 0

    @property
    def spread(self) -> float:
        return max(0.0, self.ask - self.bid) if self.ask and self.bid else 0.0

    def observe(self, px, now: float):
        """Feed one book/tape update into the tracker."""
        bid = getattr(px, "best_bid", 0.0) or 0.0
        bid_size = getattr(px, "best_bid_size", 0.0) or 0.0
        if bid <= 0:
            return
        self._observe_shadow_tp(px, now)
        self.ticks += 1
        self.last_price = bid
        dt = now - self.trigger_epoch
        if bid > self.max_price:
            self.max_price = bid
            self.max_price_at = dt
        if bid < self.min_price:
            self.min_price = bid
            self.min_price_at = dt
        for target in self.track_targets:
            key = f"{target:.2f}"
            if key in self.targets:
                continue
            if bid >= target - 1e-9:
                self.targets[key] = {"secs": round(dt, 2), "depth": round(bid_size, 1)}
        tp_key = f"{self.tp_target:.2f}"
        if bid >= self.tp_target - 1e-9:
            self.max_tp_depth = max(self.max_tp_depth, bid_size)
            self.targets.setdefault(tp_key, {"secs": round(dt, 2), "depth": round(bid_size, 1)})

    def _observe_shadow_tp(self, px, now: float):
        """
        Advance the hypothetical take-profit this crossing would have rested.

        Asked and answered separately:
          shadow_optimistic_filled  did the market ever quote/print the TP
          shadow_queue_filled       did enough volume go through the level to
                                    consume the queue that was ahead of us
        """
        if self.shadow_qty <= 0 or self.shadow_queue_filled:
            return
        state = evaluate_queue_fill(
            px, self.tp_target, self.trigger_epoch,
            self.shadow_queue_ahead, self.shadow_qty,
            trade_through=config.S9099_TRADE_THROUGH_FILLS,
            crossed_on_entry=self.bid >= self.tp_target - 1e-9,
        )
        self.shadow_consumed = state.consumed
        dt = round(now - self.trigger_epoch, 2)
        if state.optimistic and not self.shadow_optimistic_filled:
            self.shadow_optimistic_filled = True
            self.shadow_optimistic_secs = dt
        if state.fillable >= self.shadow_qty - 1e-9:
            self.shadow_queue_filled = True
            self.shadow_queue_secs = dt
            self.shadow_evidence = state.evidence

    # ── CSV rows ─────────────────────────────────────────────────────────
    def observation_row(self, mode: str) -> dict:
        start = self.end_time - timedelta(seconds=WINDOW_SECONDS)
        return {
            "candidate_id": self.candidate_id,
            "timestamp": datetime.fromtimestamp(self.trigger_epoch, timezone.utc).isoformat(),
            "epoch": round(self.trigger_epoch, 3),
            "market_id": self.market_id,
            "asset": self.asset,
            "question": self.question,
            "market_start_time": start.isoformat(),
            "market_end_time": self.end_time.isoformat(),
            "secs_remaining": round(self.trigger_secs, 2),
            "side": self.side,
            "observe_level": self.level,
            "token_id": self.token_id,
            "side_price": self.trigger_price,
            "opposite_price": self.opposite_price,
            "bid": self.bid,
            "ask": self.ask,
            "spread": round(self.spread, 4),
            "bid_depth": self.bid_depth,
            "ask_depth": self.ask_depth,
            "available_liquidity": self.ask_depth,
            "tp_depth": self.tp_depth,
            "data_age": round(self.data_age, 3),
            # Settlement reference: proxy and official kept strictly apart.
            **self.reference,
            "entry_threshold": self.entry_threshold,
            "tp_price": self.tp_target,
            "max_secs_remaining": self.max_secs_cfg,
            "qualified": self.qualified,
            "reason_qualified": self.reason_qualified,
            "reason_rejected": self.reason_rejected,
            "traded": self.traded,
            "trade_id": self.trade_id,
            "mode": mode,
        }

    def outcome_row(self) -> dict:
        def tgt(p: float, field_name: str):
            t = self.targets.get(f"{p:.2f}")
            return t[field_name] if t else ("" if field_name == "secs" else 0)

        tp = self.tp_target
        correct = ""
        if self.resolution in ("Up", "Down"):
            correct = (self.resolution == self.side)
        return {
            "candidate_id": self.candidate_id,
            "market_id": self.market_id,
            "asset": self.asset,
            "side": self.side,
            "observe_level": self.level,
            "trigger_timestamp": datetime.fromtimestamp(self.trigger_epoch, timezone.utc).isoformat(),
            "trigger_epoch": round(self.trigger_epoch, 3),
            "trigger_price": self.trigger_price,
            "trigger_secs_remaining": round(self.trigger_secs, 2),
            "traded": self.traded,
            "trade_id": self.trade_id,
            "max_price_after": self.max_price,
            "max_price_at": round(self.max_price_at, 2),
            "min_price_after": self.min_price if self.ticks else 0,
            "min_price_at": round(self.min_price_at, 2),
            "reached_95": "0.95" in self.targets, "secs_to_95": tgt(0.95, "secs"), "depth_at_95": tgt(0.95, "depth"),
            "reached_97": "0.97" in self.targets, "secs_to_97": tgt(0.97, "secs"), "depth_at_97": tgt(0.97, "depth"),
            "reached_98": "0.98" in self.targets, "secs_to_98": tgt(0.98, "secs"), "depth_at_98": tgt(0.98, "depth"),
            "reached_99": "0.99" in self.targets, "secs_to_99": tgt(0.99, "secs"), "depth_at_99": tgt(0.99, "depth"),
            "tp_target": tp,
            "reached_tp": f"{tp:.2f}" in self.targets,
            "secs_to_tp": tgt(tp, "secs"),
            "max_depth_at_tp": round(self.max_tp_depth, 1),
            "our_tp_filled": self.our_tp_filled,
            # ── shadow TP: the same question asked two ways ──
            "shadow_qty": round(self.shadow_qty, 1),
            "shadow_queue_ahead": round(self.shadow_queue_ahead, 1),
            "shadow_level_size": round(self.shadow_level_size, 1),
            "shadow_level_size_min": round(self.shadow_level_size_min, 1),
            "shadow_consumed": round(self.shadow_consumed, 1),
            "tp_price_reached": self.shadow_optimistic_filled,
            "secs_to_tp_price_reached": self.shadow_optimistic_secs or "",
            "tp_queue_adjusted_fill": self.shadow_queue_filled,
            "secs_to_queue_adjusted_fill": self.shadow_queue_secs or "",
            "queue_fill_evidence": self.shadow_evidence,
            "ticks_observed": self.ticks,
            "final_price": self.last_price,
            "resolution": self.resolution or "unknown",
            "prediction_correct": correct,
            "closed_reason": self.closed_reason,
        }


# ══════════════════════════════════════════════════════════════════════════
#  Position
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class Position:
    """One trade, entry through exit. Serialisable so a restart can resume it."""
    trade_id: str
    candidate_id: str
    market_id: str
    asset: str
    question: str
    side: str
    token_id: str
    end_time_iso: str
    mode: str = "PAPER"

    phase: str = Phase.IDLE

    # signal / entry
    signal_epoch: float = 0.0
    signal_secs: float = 0.0
    entry_submitted_at: float = 0.0
    entry_price_req: float = 0.0
    entry_qty_req: float = 0.0
    entry_order_id: str = ""
    entry_liquidity: str = "unknown"
    entry_fill_epoch: float = 0.0
    entry_fill_price: float = 0.0
    entry_filled_qty: float = 0.0
    entry_fee_est: float = 0.0
    entry_fee_actual: float = 0.0
    entry_fee_source: str = "estimate"

    # take profit
    tp_submitted_at: float = 0.0
    tp_price: float = 0.0
    tp_qty: float = 0.0
    tp_order_id: str = ""
    tp_liquidity: str = "unknown"
    tp_fill_epoch: float = 0.0
    tp_fill_price: float = 0.0
    tp_filled_qty: float = 0.0
    tp_fee_actual: float = 0.0
    tp_fee_est: float = 0.0
    tp_attempts: int = 0
    tp_next_attempt: float = 0.0
    tp_fee_source: str = "estimate"
    # ── queue state for the resting take-profit ──
    tp_queue_ahead: float = 0.0        # asks at/below our price when we joined
    tp_level_size: float = 0.0         # asks exactly at our price when we joined
    tp_level_size_min: float = 0.0
    tp_consumed: float = 0.0           # volume through our level since
    tp_bid_depth_at_submit: float = 0.0
    tp_secs_remaining_at_submit: float = 0.0
    tp_fill_evidence: str = ""
    tp_price_reached: bool = False     # the market quoted/printed our price
    tp_price_reached_at: float = 0.0
    tp_queue_adjusted_fill: bool = False

    # emergency exit
    stop_price: float = 0.0
    stop_epoch: float = 0.0
    stop_reason: str = ""
    exit_order_id: str = ""
    exit_liquidity: str = "unknown"
    exit_price: float = 0.0
    exit_qty: float = 0.0
    exit_fee_actual: float = 0.0
    exit_attempts: int = 0
    # True once the only way left to price the trade is the market result.
    awaiting_settlement: bool = False

    # result
    exit_reason: str = ""
    closed_at: float = 0.0
    gross_pnl: float = 0.0
    fees_total: float = 0.0
    realized_pnl: float = 0.0
    realized_pnl_pct: float = 0.0
    settled_value: float = 0.0
    market_result: str = ""

    # context
    fee_rate: float = 0.0              # economic rate (feeSchedule.rate)
    fee_rate_raw_bps: float = 0.0      # order signing parameter, not a price
    fee_exponent: float = 1.0
    fee_taker_only: bool = True
    fee_schedule_source: str = "default"
    reference: dict = field(default_factory=dict)
    underlying_price: float = 0.0
    threshold: float = 0.0
    distance: float = 0.0
    spread_at_entry: float = 0.0
    ask_depth_at_entry: float = 0.0
    tp_depth_at_entry: float = 0.0
    bankroll_before: float = 0.0
    bankroll_after: float = 0.0

    # post-entry behaviour
    max_price_after: float = 0.0
    min_price_after: float = 1.0

    # ── proxy-only observations (recorded, never acted on) ──
    proxy_cross_seen: bool = False
    proxy_cross_at: float = 0.0

    # guards
    submitted: list = field(default_factory=list)   # intents already submitted

    @property
    def open_qty(self) -> float:
        """Shares still held (entry fills minus everything sold)."""
        return max(0.0, self.entry_filled_qty - self.tp_filled_qty - self.exit_qty)

    @property
    def entry_cost(self) -> float:
        return self.entry_fill_price * self.entry_filled_qty

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: dict) -> "Position":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def dashboard_dict(self) -> dict:
        return {
            "trade_id": self.trade_id,
            "market_id": self.market_id,
            "asset": self.asset,
            "question": self.question,
            "side": self.side,
            "phase": self.phase,
            "mode": self.mode,
            "entry_price": round(self.entry_fill_price or self.entry_price_req, 4),
            "shares": round(self.entry_filled_qty or self.entry_qty_req, 1),
            "tp_price": round(self.tp_price, 4),
            "tp_filled": round(self.tp_filled_qty, 1),
            "stop_price": round(self.stop_price, 4),
            "open_qty": round(self.open_qty, 1),
            "cost": round(self.entry_cost, 2),
            "fees": round(self.entry_fee_actual + self.tp_fee_actual + self.exit_fee_actual, 4),
            "realized_pnl": round(self.realized_pnl, 2),
            "exit_reason": self.exit_reason,
            "age": round(time.time() - self.signal_epoch, 1) if self.signal_epoch else 0,
        }


# ══════════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════════

def seconds_until(end_time: datetime) -> float:
    """Seconds from now until end_time (negative once it has passed)."""
    return (end_time - datetime.now(timezone.utc)).total_seconds()


def _parse_dt(value) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _short(mid: str) -> str:
    return mid[:10] if mid else "?"


# ══════════════════════════════════════════════════════════════════════════
#  Engine
# ══════════════════════════════════════════════════════════════════════════

class Strategy9099:
    """
    The 90c -> 99c engine.

    Driven by repeated on_tick(markets) calls — from run_9099.py, or from the
    dashboard's bot loop. It never starts threads of its own, so whoever owns
    the loop owns the concurrency.
    """

    def __init__(self, feed, underlying=None, broker=None, depth_fn=None,
                 resolve_fn=None, clock=time.time, log_callback=None,
                 state_file: str = STATE_FILE):
        self.feed = feed
        self.underlying = underlying
        self._depth_fn = depth_fn
        self.broker = (broker if broker is not None
                       else make_broker(feed, book_fn=self._full_book))
        # What these markets actually resolve from (Chainlink TWAP-60s), and
        # what we can see. Binance is a labelled proxy in here, never truth.
        self.settlement = SettlementReference(binance_feed=underlying)
        self.clock = clock
        self.log_callback = log_callback
        self.state_file = state_file
        self._resolve_fn = resolve_fn

        # ── Live-editable parameters (seeded from config) ────────────────
        self.enabled: bool = config.S9099_ENABLED
        self.auto_trade: bool = config.S9099_AUTO_TRADE
        self.kill_switch: bool = config.S9099_KILL_SWITCH
        self.assets: list[str] = list(config.S9099_ASSETS)

        self.entry_price_min: float = config.S9099_ENTRY_PRICE_MIN
        self.entry_price_max: float = config.S9099_ENTRY_PRICE_MAX
        self.entry_slippage: float = config.S9099_ENTRY_SLIPPAGE
        self.entry_timeout: float = config.S9099_ENTRY_TIMEOUT
        self.partial_fill_grace: float = config.S9099_PARTIAL_FILL_GRACE
        self.tp_price: float = config.S9099_TAKE_PROFIT_PRICE
        self.max_secs_remaining: float = config.S9099_MAX_SECS_REMAINING
        self.min_secs_remaining: float = config.S9099_MIN_SECS_REMAINING
        self.max_spread: float = config.S9099_MAX_SPREAD
        self.min_liquidity: float = config.S9099_MIN_LIQUIDITY
        self.min_tp_depth: float = config.S9099_MIN_TP_DEPTH
        self.min_margin_pct: float = config.S9099_MIN_UNDERLYING_MARGIN_PCT
        self.max_data_age: float = config.S9099_MAX_DATA_AGE

        self.size_mode: str = config.S9099_POSITION_SIZE_MODE
        self.fixed_dollars: float = config.S9099_FIXED_DOLLARS
        self.max_position_percent: float = config.S9099_MAX_POSITION_PERCENT
        self.max_position_dollars: float = config.S9099_MAX_POSITION_DOLLARS
        self.min_shares: int = config.S9099_MIN_SHARES

        self.stop_price: float = config.S9099_STOP_PRICE
        self.exit_before_expiry: float = config.S9099_EXIT_BEFORE_EXPIRY
        self.stop_on_threshold_cross: bool = config.S9099_STOP_ON_THRESHOLD_CROSS
        self.stop_velocity_drop: float = config.S9099_STOP_VELOCITY_DROP
        self.stop_velocity_window: float = config.S9099_STOP_VELOCITY_WINDOW

        self.max_open_positions: int = config.S9099_MAX_OPEN_POSITIONS
        self.max_daily_loss: float = config.S9099_MAX_DAILY_LOSS
        self.max_consecutive_losses: int = config.S9099_MAX_CONSECUTIVE_LOSSES
        self.min_balance: float = config.S9099_MIN_BALANCE
        self.max_trades_per_day: int = config.S9099_MAX_TRADES_PER_DAY
        self.max_api_errors: int = config.S9099_MAX_API_ERRORS
        self.market_cooldown: float = config.S9099_MARKET_COOLDOWN
        self.candidate_max_secs: float = config.S9099_CANDIDATE_MAX_SECS
        self.track_candidates: bool = config.S9099_TRACK_CANDIDATES
        self.track_targets: list[float] = list(config.S9099_TRACK_TARGETS)
        self.track_after_close: float = config.S9099_TRACK_AFTER_CLOSE
        # Thresholds observed independently. The entry level is always one of
        # them, so the traded level is directly comparable with the rest.
        self.observe_thresholds: list[float] = sorted(
            set(config.S9099_OBSERVE_THRESHOLDS) | {self.entry_price_min}
        )
        self.allow_proxy_threshold_stop: bool = config.S9099_ALLOW_PROXY_THRESHOLD_STOP
        if self.candidate_max_secs < self.max_secs_remaining:
            log.warning(
                f"[9099] S9099_CANDIDATE_MAX_SECS ({self.candidate_max_secs:.0f}s) is "
                f"below S9099_MAX_SECS_REMAINING ({self.max_secs_remaining:.0f}s) — "
                f"raising it, or entries above {self.candidate_max_secs:.0f}s could "
                f"never happen"
            )
            self.candidate_max_secs = self.max_secs_remaining

        # ── State ────────────────────────────────────────────────────────
        self.positions: dict[str, Position] = {}          # market_id -> Position
        self.closed: list[Position] = []
        self.candidates: dict[tuple, Candidate] = {}      # (market_id, side) -> Candidate
        self.traded_markets: set[str] = set()             # one position per market, ever
        self.cooldowns: dict[str, float] = {}
        self._pending_resolution: list[tuple] = []        # (kind, obj, first_try)
        self._px_hist: dict[str, list] = {}               # trade_id -> [(ts, bid)]
        self._resolution_cache: dict[str, tuple] = {}     # market_id -> (winner, ts)
        self._depth_cache: dict[tuple, tuple] = {}        # (token, price) -> (depth, ts)
        self._book_cache: dict[str, tuple] = {}           # token -> (book, ts)
        self._schedules: dict[str, fees.Schedule] = {}    # market_id -> Schedule
        self._locks: dict[str, threading.Lock] = {}
        self._global_lock = threading.RLock()

        # ── Counters ─────────────────────────────────────────────────────
        self.candidates_seen = 0
        self.candidates_traded = 0
        self.candidates_rejected = 0
        self.rejection_reasons: dict[str, int] = {}
        self.trades_today = 0
        self.wins = 0
        self.losses = 0
        self.tp_fills = 0               # queue-adjusted: our order really filled
        self.tp_price_reached_count = 0  # the market merely got to our price
        self.emergency_exits = 0
        self.entry_timeouts = 0
        self.realized_pnl = 0.0
        self.daily_loss = 0.0
        self.consecutive_losses = 0
        self.api_errors = 0
        self.day = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        self._balance_cache = (0.0, 0.0)  # (value, fetched_at)
        self._starting_bankroll = self.available_balance()

        self._log(
            f"initialised in {self.mode} mode | entry>=${self.entry_price_min:.2f} "
            f"tp=${self.tp_price:.2f} stop=${self.stop_price:.2f} "
            f"window<={self.max_secs_remaining:.0f}s"
        )
        if self.mode == "PAPER":
            self._log("PAPER MODE — no order will reach Polymarket. "
                      + "; ".join(config.strategy_9099_block_reasons()))
        else:
            self._log("*** LIVE MODE — REAL ORDERS, REAL MONEY ***", "warn")

    # ── plumbing ─────────────────────────────────────────────────────────

    @property
    def mode(self) -> str:
        return getattr(self.broker, "name", "PAPER")

    @property
    def is_live(self) -> bool:
        return self.mode == "LIVE"

    def _log(self, msg: str, level: str = "info"):
        text = f"[9099][{self.mode}] {msg}"
        if level == "error":
            log.error(text)
        elif level == "warn":
            log.warning(text)
        else:
            log.info(text)
        if self.log_callback:
            try:
                self.log_callback(f"[9099] {msg}", level)
            except Exception:
                pass

    def _lock_for(self, market_id: str) -> threading.Lock:
        with self._global_lock:
            lk = self._locks.get(market_id)
            if lk is None:
                lk = threading.Lock()
                self._locks[market_id] = lk
            return lk

    def available_balance(self, max_age: float = 5.0) -> float:
        """Buying power, cached briefly so sizing does not hammer the API."""
        value, fetched = self._balance_cache
        now = self.clock()
        if now - fetched < max_age and fetched > 0:
            return value
        try:
            value = float(self.broker.available_balance())
            self.api_errors = 0
        except Exception as e:
            self.api_errors += 1
            self._log(f"balance lookup failed: {e}", "warn")
            value = self._balance_cache[0]
        self._balance_cache = (value, now)
        return value

    def _reset_daily(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self.day:
            self._log(f"new UTC day {today} — resetting daily counters "
                      f"(trades={self.trades_today}, loss=${self.daily_loss:.2f})")
            self.day = today
            self.trades_today = 0
            self.daily_loss = 0.0

    # ══════════════════════════════════════════════════════════════════
    #  Entry conditions — pure, so they can be tested without a market
    # ══════════════════════════════════════════════════════════════════

    def check_entry_conditions(self, price: float, secs_remaining: float,
                               spread: float, ask_depth: float,
                               tp_depth: float = 0.0,
                               favourable_distance: float | None = None,
                               reference_price: float = 0.0,
                               data_age: float = 0.0,
                               side: str = "Up",
                               signed_distance: float | None = None,
                               underlying_price: float | None = None,
                               ) -> tuple[bool, str]:
        """
        Does this snapshot qualify for an entry?

        Returns (ok, reason).  On failure `reason` is the first unmet
        condition — it is written to the candidate log verbatim, so the
        rejection mix is analysable later.
        """
        if price < self.entry_price_min:
            return False, f"price_below_threshold({price:.3f}<{self.entry_price_min:.2f})"
        if price > self.entry_price_max:
            return False, f"price_above_max({price:.3f}>{self.entry_price_max:.2f})"
        if price >= self.tp_price:
            return False, f"no_room_to_tp({price:.3f}>=({self.tp_price:.2f}))"
        if secs_remaining <= 0:
            return False, "market_expired"
        if secs_remaining > self.max_secs_remaining:
            return False, f"too_early({secs_remaining:.0f}s>{self.max_secs_remaining:.0f}s)"
        if secs_remaining < self.min_secs_remaining:
            return False, f"too_late({secs_remaining:.0f}s<{self.min_secs_remaining:.0f}s)"
        if data_age > self.max_data_age:
            return False, f"stale_data({data_age:.1f}s)"
        if spread > self.max_spread:
            return False, f"spread_too_wide({spread:.3f}>{self.max_spread:.3f})"
        if ask_depth < self.min_liquidity:
            return False, f"thin_book({ask_depth:.0f}<{self.min_liquidity:.0f})"
        if self.min_tp_depth > 0 and tp_depth < self.min_tp_depth:
            return False, f"thin_at_tp({tp_depth:.0f}<{self.min_tp_depth:.0f})"

        # The reference must sit on the winning side of the market's opening
        # price by at least the configured margin.
        #
        # `favourable_distance` is signed for this side and comes from
        # SettlementReference: the official Chainlink TWAP value when that is
        # available, otherwise a Binance TWAP PROXY. It is a filter only — it
        # can stop an entry, it never forces an exit (see _stop_reason).
        #
        # signed_distance/underlying_price are the older, side-agnostic
        # spelling, still accepted so existing callers keep working.
        if self.min_margin_pct > 0:
            price_ref = reference_price or (underlying_price or 0.0)
            if favourable_distance is None:
                if signed_distance is None:
                    return False, "no_reference_distance"
                favourable_distance = signed_distance if side == "Up" else -signed_distance
            if price_ref <= 0:
                return False, "no_reference_price"
            margin_pct = favourable_distance / price_ref * 100.0
            if margin_pct < self.min_margin_pct:
                return False, (
                    f"reference_margin({margin_pct:.4f}%<{self.min_margin_pct:.4f}%)"
                )
        return True, "all_checks_passed"

    def check_risk_gates(self, market_id: str = "") -> tuple[bool, str]:
        """Account-level guards, independent of any particular market."""
        self._reset_daily()
        if not self.enabled:
            return False, "strategy_disabled"
        if self.kill_switch:
            return False, "kill_switch"
        if not self.auto_trade:
            return False, "auto_trade_off"
        if self.api_errors >= self.max_api_errors:
            return False, f"api_unhealthy({self.api_errors}_consecutive_errors)"
        open_count = sum(1 for p in self.positions.values() if p.phase in ACTIVE_PHASES)
        if open_count >= self.max_open_positions:
            return False, f"max_open_positions({open_count})"
        if self.max_trades_per_day and self.trades_today >= self.max_trades_per_day:
            return False, f"max_trades_per_day({self.trades_today})"
        if self.daily_loss >= self.max_daily_loss > 0:
            return False, f"max_daily_loss(${self.daily_loss:.2f})"
        if self.max_consecutive_losses and self.consecutive_losses >= self.max_consecutive_losses:
            return False, f"max_consecutive_losses({self.consecutive_losses})"
        balance = self.available_balance()
        if balance < self.min_balance:
            return False, f"below_min_balance(${balance:.2f}<${self.min_balance:.2f})"
        if market_id:
            if market_id in self.traded_markets or market_id in self.positions:
                return False, "already_traded_this_market"
            cd = self.cooldowns.get(market_id, 0)
            if cd and self.clock() - cd < self.market_cooldown:
                return False, "market_cooldown"
        return True, "ok"

    # ══════════════════════════════════════════════════════════════════
    #  Position sizing
    # ══════════════════════════════════════════════════════════════════

    def calculate_position_size(self, balance: float, price: float,
                                ask_depth: float | None = None,
                                schedule=None) -> int:
        """
        Whole shares to buy at `price`.

        Budget comes from the sizing mode, then gets clamped by the hard
        per-trade dollar cap and by the balance itself.  The taker fee is
        part of the cost of a share, so it is inside the divisor — a $25
        budget never turns into a $25.40 spend.  Depth caps the size so the
        whole order is marketable instead of resting half-filled.
        """
        if price <= 0 or price >= 1 or balance <= 0:
            return 0
        if self.size_mode == "percent_bankroll":
            budget = balance * (self.max_position_percent / 100.0)
        else:
            budget = self.fixed_dollars
        budget = min(budget, self.max_position_dollars, balance)
        if budget <= 0:
            return 0

        sched = schedule or fees.default_schedule()
        fee_per_share = fees.estimate_fee(1.0, price, True, sched)
        cost_per_share = price + fee_per_share
        shares = int(math.floor(budget / cost_per_share))
        if ask_depth is not None and ask_depth > 0:
            shares = min(shares, int(math.floor(ask_depth)))
        if shares < self.min_shares:
            return 0
        return shares

    # ══════════════════════════════════════════════════════════════════
    #  Main entry point
    # ══════════════════════════════════════════════════════════════════

    def on_tick(self, markets):
        """
        Evaluate every market once. Safe to call as often as the feed updates
        — re-processing the same state is a no-op, never a second order.
        """
        if not self.enabled:
            return
        for market in markets:
            try:
                if self.assets and getattr(market, "asset", "").upper() not in self.assets:
                    continue
                self._process_market(market)
            except Exception as e:
                self.api_errors += 1
                self._log(f"process error on {_short(getattr(market, 'market_id', ''))}: {e}", "error")
        self._sweep_finished()

    def _process_market(self, market):
        market_id = market.market_id
        end_time = _parse_dt(market.end_time)
        secs = seconds_until(end_time)
        now = self.clock()

        px_up = self.feed.get_price(market.token_id_up)
        px_down = self.feed.get_price(market.token_id_down)
        sides = (("Up", px_up, px_down), ("Down", px_down, px_up))

        # Sample the proxy into this market's 5-minute window so we can build
        # a TWAP — which is the statistic the market actually resolves on,
        # not the spot-vs-open comparison this used to make.
        window_start = end_time.timestamp() - WINDOW_SECONDS
        self.settlement.observe(getattr(market, "asset", ""), window_start, now)

        # 1. Tracking first, so a print that fills our take-profit on this very
        #    tick is in the candidate record before the trade row is written.
        if secs >= -self.track_after_close:
            for side, px, opp in sides:
                if getattr(px, "valid", False):
                    self._track_side(market, side, px, opp, secs, now, end_time)

        # 2. Then manage an open position — exits outrank new entries.
        pos = self.positions.get(market_id)
        if pos and pos.phase in ACTIVE_PHASES:
            px = px_up if pos.side == "Up" else px_down
            self._advance(pos, px, secs)

        # 3. Only then consider opening something new.
        if secs < -self.track_after_close:
            return
        for side, px, opp in sides:
            if getattr(px, "valid", False):
                self._decide_side(market, side, px, opp, secs)

    # ── candidate observation ────────────────────────────────────────────

    def _track_side(self, market, side, px, opp, secs, now, end_time):
        """
        Record what this side is doing, at every observation threshold it has
        crossed. Never places an order.

        A side at 0.93 has crossed 0.85, 0.88, 0.90 and 0.92, and each gets
        its own record measured from its own moment — that is what makes the
        entry levels comparable instead of assuming 90c is the right one.
        """
        for level in self.observe_thresholds:
            key = (market.market_id, side, level)
            cand = self.candidates.get(key)
            if cand is not None:
                cand.observe(px, now)
            elif (self.track_candidates
                    and px.best_ask >= level - 1e-9
                    and 0 < secs <= self.candidate_max_secs):
                self._open_candidate(market, side, px, opp, secs, now, end_time, level)

    # ── the entry decision ───────────────────────────────────────────────

    def _decide_side(self, market, side, px, opp, secs):
        # Only the crossing at the configured entry threshold is tradeable;
        # every other level is observation.
        cand = self.candidates.get((market.market_id, side, self.entry_price_min))
        if cand is None or cand.decision_written:
            return

        # Still deciding. Re-check every tick until we act or the window shuts.
        ok, reason = self._evaluate_candidate(market, cand, side, px, opp, secs)
        if ok:
            self._enter(market, cand, side, px, secs)
            return
        cand.reason_rejected = reason
        # Once the entry window has passed, the decision is final.
        if secs < self.min_secs_remaining or px.best_ask < self.entry_price_min:
            self._settle_decision(cand)

    def _open_candidate(self, market, side, px, opp, secs, now, end_time,
                        level: float) -> Candidate:
        ask = px.best_ask
        asset = getattr(market, "asset", "?")
        token_id = market.token_id_up if side == "Up" else market.token_id_down
        window_start = end_time.timestamp() - WINDOW_SECONDS
        reference = self.settlement.snapshot(asset, window_start)
        favourable, ref_source, ref_official = self.settlement.distance_for_side(
            asset, window_start, side
        )
        reference["favourable_distance"] = round(favourable, 6)
        reference["distance_source"] = ref_source
        reference["distance_is_official"] = ref_official
        if ref_official:
            reference["distance_from_official_reference"] = round(favourable, 6)

        # The take-profit this crossing would have rested, and the queue it
        # would have joined.
        queue_ahead, level_size = self.ask_queue_ahead(token_id, self.tp_price)
        shadow_qty = self.calculate_position_size(
            self.available_balance(), ask, px.best_ask_size,
            self.schedule_for(market.market_id, token_id),
        )

        cand = Candidate(
            candidate_id=f"c{uuid.uuid4().hex[:12]}",
            market_id=market.market_id,
            asset=asset,
            question=getattr(market, "question", ""),
            side=side,
            token_id=token_id,
            end_time=end_time,
            level=level,
            trigger_epoch=now,
            trigger_price=ask,
            trigger_secs=secs,
            bid=px.best_bid,
            ask=ask,
            bid_depth=px.best_bid_size,
            ask_depth=px.best_ask_size,
            opposite_price=getattr(opp, "best_ask", 0.0),
            tp_depth=self._depth_at(token_id, self.tp_price),
            data_age=max(0.0, now - getattr(px, "timestamp", now)),
            tp_target=self.tp_price,
            entry_threshold=self.entry_price_min,
            max_secs_cfg=self.max_secs_remaining,
            track_targets=list(self.track_targets),
            reference=reference,
            shadow_qty=float(shadow_qty or self.min_shares),
            shadow_queue_ahead=queue_ahead,
            shadow_level_size=level_size,
            shadow_level_size_min=level_size,
        )
        cand.observe(px, now)
        self.candidates[(market.market_id, side, level)] = cand
        self.candidates_seen += 1
        self._log(
            f"CANDIDATE[{level:.2f}] {cand.asset} {side} @ ${ask:.3f} with {secs:.0f}s left "
            f"(bid ${px.best_bid:.3f}, ask depth {px.best_ask_size:.0f}, "
            f"{queue_ahead:.0f} sh ahead at ${self.tp_price:.2f})"
        )
        return cand

    def _evaluate_candidate(self, market, cand, side, px, opp, secs) -> tuple[bool, str]:
        """Market checks, then account checks. Returns (ok, reason)."""
        ok, reason = self.check_entry_conditions(
            price=px.best_ask,
            secs_remaining=secs,
            spread=max(0.0, px.best_ask - px.best_bid),
            ask_depth=px.best_ask_size,
            tp_depth=cand.tp_depth,
            # Margin is measured against the settlement reference — the window
            # TWAP, not spot-vs-candle-open — and `favourable` already has the
            # side's sign applied.
            favourable_distance=cand.reference.get("favourable_distance", 0.0),
            reference_price=self.settlement.binance_price(market.asset),
            data_age=max(0.0, self.clock() - getattr(px, "timestamp", self.clock())),
            side=side,
        )
        if not ok:
            return False, reason
        ok, reason = self.check_risk_gates(market.market_id)
        if not ok:
            return False, reason
        shares = self.calculate_position_size(
            self.available_balance(), px.best_ask, px.best_ask_size,
            self.schedule_for(market.market_id, cand.token_id),
        )
        if shares <= 0:
            return False, f"size_too_small(min {self.min_shares} shares)"
        return True, "all_checks_passed"

    def _schedule_of(self, pos: Position) -> fees.Schedule:
        """Rebuild the fee schedule a position was opened under."""
        return fees.Schedule(
            rate=pos.fee_rate, exponent=pos.fee_exponent,
            taker_only=pos.fee_taker_only, source=pos.fee_schedule_source,
        )

    def _entry_candidate(self, pos: Position) -> Candidate | None:
        """The crossing record this position was opened from."""
        return self.candidates.get((pos.market_id, pos.side, self.entry_price_min))

    def _settle_decision(self, cand: Candidate, traded: bool = False, trade_id: str = ""):
        """Write the one-per-candidate observation row, with the final verdict."""
        if cand.decision_written:
            return
        cand.decision_written = True
        cand.qualified = traded
        cand.traded = traded
        cand.trade_id = trade_id
        if traded:
            cand.reason_qualified = "all_checks_passed"
            cand.reason_rejected = ""
            self.candidates_traded += 1
        else:
            self.candidates_rejected += 1
            bucket = (cand.reason_rejected or "unknown").split("(")[0]
            self.rejection_reasons[bucket] = self.rejection_reasons.get(bucket, 0) + 1
        candidate_log.write(cand.observation_row(self.mode))

    # ══════════════════════════════════════════════════════════════════
    #  Entry
    # ══════════════════════════════════════════════════════════════════

    def _enter(self, market, cand: Candidate, side: str, px, secs: float):
        """Submit the entry buy. Holds the market lock for the whole transition."""
        market_id = market.market_id
        lock = self._lock_for(market_id)
        if not lock.acquire(blocking=False):
            return                      # a transition is already in flight
        try:
            # Re-check under the lock: a duplicate market update that raced us
            # here finds the position already created and does nothing.
            if market_id in self.positions or market_id in self.traded_markets:
                return
            ok, reason = self.check_risk_gates(market_id)
            if not ok:
                cand.reason_rejected = reason
                return

            token_id = market.token_id_up if side == "Up" else market.token_id_down
            schedule = self.schedule_for(market.market_id, token_id)
            balance = self.available_balance()
            limit_price = min(0.999, round(px.best_ask + self.entry_slippage, 3))

            # The book can move between qualifying and submitting. Re-check the
            # price we are actually about to pay — qualifying at 90c is no
            # licence to lift a 98c offer.
            if limit_price > self.entry_price_max or limit_price >= self.tp_price:
                cand.reason_rejected = (
                    f"price_moved_past_max({limit_price:.3f}>{self.entry_price_max:.2f})"
                )
                return

            shares = self.calculate_position_size(balance, limit_price,
                                                  px.best_ask_size, schedule)
            if shares <= 0:
                cand.reason_rejected = f"size_too_small(min {self.min_shares} shares)"
                return

            pos = Position(
                trade_id=f"t{uuid.uuid4().hex[:12]}",
                candidate_id=cand.candidate_id,
                market_id=market_id,
                asset=getattr(market, "asset", "?"),
                question=getattr(market, "question", ""),
                side=side,
                token_id=token_id,
                end_time_iso=_parse_dt(market.end_time).isoformat(),
                mode=self.mode,
                phase=Phase.IDLE,
                signal_epoch=self.clock(),
                signal_secs=secs,
                entry_price_req=limit_price,
                entry_qty_req=shares,
                tp_price=self.tp_price,
                stop_price=self.stop_price,
                fee_rate=schedule.rate,
                fee_rate_raw_bps=fees.signing_fee_rate_bps(token_id),
                fee_exponent=schedule.exponent,
                fee_taker_only=schedule.taker_only,
                fee_schedule_source=schedule.source,
                entry_fee_est=fees.estimate_fee(shares, limit_price, True, schedule),
                tp_fee_est=fees.estimate_fee(shares, self.tp_price, False, schedule),
                reference=dict(cand.reference),
                underlying_price=cand.reference.get("binance_price", 0) or 0.0,
                threshold=cand.reference.get("binance_window_open", 0) or 0.0,
                distance=cand.reference.get("favourable_distance", 0) or 0.0,
                spread_at_entry=max(0.0, px.best_ask - px.best_bid),
                ask_depth_at_entry=px.best_ask_size,
                tp_depth_at_entry=cand.tp_depth,
                bankroll_before=balance,
                max_price_after=px.best_bid,
                min_price_after=px.best_bid,
            )

            # Claim the market BEFORE the order exists: if submission throws,
            # the market stays claimed and no retry can double up.
            self.positions[market_id] = pos
            self.traded_markets.add(market_id)

            order_id = self._submit_once(
                pos, "entry",
                lambda: self.broker.place_buy(token_id, limit_price, shares, market_id),
            )
            pos.entry_submitted_at = self.clock()
            if not order_id:
                pos.phase = Phase.CLOSED
                pos.exit_reason = "entry_rejected"
                pos.closed_at = self.clock()
                self.api_errors += 1
                self._log(f"ENTRY REJECTED {pos.asset} {side} — broker returned no order id", "error")
                self._settle_decision(cand, traded=False)
                cand.reason_rejected = "entry_rejected"
                self._finish(pos)
                return

            pos.entry_order_id = order_id
            pos.entry_liquidity = "taker" if limit_price >= px.best_ask > 0 else "maker"
            pos.phase = Phase.ENTRY_PENDING
            self.trades_today += 1
            self.api_errors = 0
            self._settle_decision(cand, traded=True, trade_id=pos.trade_id)
            self._log(
                f"ENTRY {pos.asset} {side} {shares} sh @ ${limit_price:.3f} "
                f"({secs:.0f}s left, est fee ${pos.entry_fee_est:.2f}) -> TP ${self.tp_price:.2f}",
                "trade",
            )
            self._save_state()
        finally:
            lock.release()

    def _submit_once(self, pos: Position, intent: str, fn):
        """
        Submit an order for `intent` at most once per position, ever.

        The intent key is recorded before the call, so even an exception
        mid-submission cannot produce a second order for the same intent.
        """
        if intent in pos.submitted:
            self._log(f"duplicate {intent} suppressed for {pos.trade_id}", "warn")
            return None
        pos.submitted.append(intent)
        try:
            return fn()
        except Exception as e:
            self.api_errors += 1
            self._log(f"{intent} submission failed for {pos.trade_id}: {e}", "error")
            return None

    # ══════════════════════════════════════════════════════════════════
    #  State machine
    # ══════════════════════════════════════════════════════════════════

    def _advance(self, pos: Position, px, secs: float):
        lock = self._lock_for(pos.market_id)
        if not lock.acquire(blocking=False):
            return
        try:
            self._record_price(pos, px)
            # Sequential, not elif: an entry that fills this tick gets its
            # take-profit resting on the same tick.
            if pos.phase == Phase.ENTRY_PENDING:
                self._advance_entry(pos, px, secs)
            if pos.phase == Phase.POSITION_OPEN:
                self._place_tp(pos, px)
            if pos.phase == Phase.TP_PENDING:
                self._advance_tp(pos, px, secs)
            if pos.phase == Phase.EXIT_PENDING:
                self._advance_exit(pos, px, secs)
        finally:
            lock.release()

    def _record_price(self, pos: Position, px):
        bid = getattr(px, "best_bid", 0.0)
        if bid <= 0:
            return
        pos.max_price_after = max(pos.max_price_after, bid)
        pos.min_price_after = min(pos.min_price_after or bid, bid)
        hist = self._px_hist.setdefault(pos.trade_id, [])
        now = self.clock()
        hist.append((now, bid))
        cutoff = now - max(self.stop_velocity_window, 30.0)
        while hist and hist[0][0] < cutoff:
            hist.pop(0)

    # ── entry ────────────────────────────────────────────────────────────

    def _advance_entry(self, pos: Position, px, secs: float):
        st = self._order_status(pos.entry_order_id)
        if st is None:
            return
        if st.filled > pos.entry_filled_qty:
            pos.entry_filled_qty = st.filled
            pos.entry_fill_price = st.avg_price or st.price or pos.entry_price_req
            if not pos.entry_fill_epoch:
                pos.entry_fill_epoch = self.clock()
            if st.liquidity in ("maker", "taker"):
                pos.entry_liquidity = st.liquidity

        fully = st.status == "matched" or pos.entry_filled_qty >= pos.entry_qty_req - 1e-9
        timed_out = (self.clock() - pos.entry_submitted_at) >= self.entry_timeout
        expired = secs <= 0
        # A partial fill only waits a moment for the rest — the take-profit
        # matters more than the last few shares.
        grace_over = (
            pos.entry_filled_qty > 0
            and (self.clock() - pos.entry_fill_epoch) >= self.partial_fill_grace
        )

        if fully:
            self._entry_done(pos, partial=False)
            return
        if timed_out or expired or grace_over or st.status == "cancelled":
            # Stop chasing: cancel the remainder, keep whatever filled.
            if st.status != "cancelled":
                self.broker.cancel(pos.entry_order_id)
            final = self._order_status(pos.entry_order_id)
            if final and final.filled > pos.entry_filled_qty:
                pos.entry_filled_qty = final.filled
                pos.entry_fill_price = final.avg_price or pos.entry_price_req
                if not pos.entry_fill_epoch:
                    pos.entry_fill_epoch = self.clock()
            if pos.entry_filled_qty > 0:
                self._entry_done(pos, partial=True)
            else:
                self.entry_timeouts += 1
                pos.exit_reason = "entry_never_filled"
                self._log(f"ENTRY UNFILLED {pos.asset} {pos.side} — cancelled after "
                          f"{self.clock() - pos.entry_submitted_at:.1f}s")
                self._close(pos, "entry_never_filled")

    def _entry_done(self, pos: Position, partial: bool):
        pos.phase = Phase.POSITION_OPEN
        sched = self._schedule_of(pos)
        pos.entry_fee_actual = fees.estimate_fee(
            pos.entry_filled_qty, pos.entry_fill_price,
            pos.entry_liquidity == "taker", sched,
        )
        pos.entry_fee_source = "estimate"
        actual, source = fees.actual_fee_for_order(pos.entry_order_id)
        if actual is not None:
            pos.entry_fee_actual = actual
            pos.entry_fee_source = source
        tag = "PARTIAL " if partial else ""
        self._log(
            f"{tag}ENTRY FILLED {pos.asset} {pos.side} {pos.entry_filled_qty:.0f}/"
            f"{pos.entry_qty_req:.0f} sh @ ${pos.entry_fill_price:.3f} "
            f"(fee ${pos.entry_fee_actual:.2f}) — resting TP now",
            "trade",
        )
        self._save_state()

    # ── take profit ──────────────────────────────────────────────────────

    def _place_tp(self, pos: Position, px):
        """
        Rest the take-profit for the shares that ACTUALLY filled.

        Never the requested quantity — selling shares we do not hold is how a
        naked short gets created on a partial fill.
        """
        qty = pos.open_qty
        if qty < self.min_shares:
            pos.exit_reason = "below_min_sell_size"
            self._log(
                f"{pos.asset} {pos.side}: {qty:.0f} shares is under the "
                f"{self.min_shares}-share minimum — holding to resolution",
                "warn",
            )
            pos.phase = Phase.EXIT_PENDING
            self._await_settlement(pos)
            return

        if pos.tp_attempts >= 5:
            pos.exit_reason = "tp_placement_failed"
            pos.phase = Phase.EXIT_PENDING
            self._await_settlement(pos)
            self._log(f"{pos.asset}: take-profit rejected 5x — holding shares", "error")
            return
        if pos.tp_next_attempt and self.clock() < pos.tp_next_attempt:
            return

        pos.tp_attempts += 1
        # What stands between us and a fill, measured as we join:
        #   queue_ahead  every offer at or below our price (they trade first)
        #   bid_depth    buyers already at/above our price (we would cross)
        queue_ahead, level_size = self.ask_queue_ahead(pos.token_id, pos.tp_price)
        pos.tp_queue_ahead = queue_ahead
        pos.tp_level_size = level_size
        pos.tp_level_size_min = level_size
        pos.tp_bid_depth_at_submit = self._depth_at(pos.token_id, pos.tp_price)
        pos.tp_secs_remaining_at_submit = self._secs_left(pos)
        order_id = self._submit_once(
            pos, f"tp:{pos.tp_attempts}",
            lambda: self.broker.place_sell(pos.token_id, pos.tp_price, qty, pos.market_id),
        )
        if not order_id:
            pos.tp_next_attempt = self.clock() + 2.0
            return

        pos.tp_order_id = order_id
        pos.tp_qty = qty
        pos.tp_submitted_at = self.clock()
        # Maker unless the bid is already at our price, in which case we are
        # crossing and will pay taker fees on the way out.
        pos.tp_liquidity = (
            "taker" if (getattr(px, "best_bid", 0.0) or 0.0) >= pos.tp_price - 1e-9
            else "maker"
        )
        pos.tp_fee_est = fees.estimate_fee(qty, pos.tp_price, False, self._schedule_of(pos))
        pos.phase = Phase.TP_PENDING
        lag = pos.tp_submitted_at - (pos.entry_fill_epoch or pos.tp_submitted_at)
        self._log(
            f"TP RESTING {pos.asset} {pos.side} {qty:.0f} sh @ ${pos.tp_price:.2f} "
            f"({lag:.1f}s after fill | {pos.tp_queue_ahead:.0f} sh ahead of us, "
            f"{pos.tp_level_size:.0f} at our price | {pos.tp_secs_remaining_at_submit:.0f}s left)",
            "trade",
        )
        self._save_state()

    def _advance_tp(self, pos: Position, px, secs: float):
        st = self._order_status(pos.tp_order_id)
        self._record_tp_queue(pos, st, px)
        if st and st.filled > pos.tp_filled_qty:
            new_qty = st.filled - pos.tp_filled_qty
            pos.tp_filled_qty = st.filled
            pos.tp_fill_price = st.avg_price or pos.tp_price
            if not pos.tp_fill_epoch:
                pos.tp_fill_epoch = self.clock()
            pos.tp_fee_actual = fees.estimate_fee(
                pos.tp_filled_qty, pos.tp_fill_price,
                st.liquidity == "taker", self._schedule_of(pos),
            )
            pos.tp_fee_source = "estimate"
            actual, source = fees.actual_fee_for_order(pos.tp_order_id)
            if actual is not None:
                pos.tp_fee_actual = actual
                pos.tp_fee_source = source
            cand = self._entry_candidate(pos)
            if cand:
                cand.our_tp_filled = True
            self._log(
                f"TP FILL {pos.asset} {pos.side} {new_qty:.0f} sh @ "
                f"${pos.tp_fill_price:.3f} ({pos.tp_filled_qty:.0f}/{pos.tp_qty:.0f})",
                "profit",
            )

        if pos.open_qty <= 1e-9:
            self.tp_fills += 1
            self._close(pos, "tp_filled")
            return

        if st and st.status == "cancelled" and pos.open_qty > 0:
            # Somebody (or something) cancelled our resting sell — put it back.
            pos.phase = Phase.POSITION_OPEN
            pos.tp_order_id = ""
            self._log(f"{pos.asset}: TP order vanished — re-placing", "warn")
            return

        reason = self._stop_reason(pos, px, secs)
        if reason:
            self._emergency_exit(pos, px, reason)

    # ── emergency exit ───────────────────────────────────────────────────

    def _record_tp_queue(self, pos: Position, st, px):
        """
        Keep the two answers side by side:

          tp_price_reached         the market quoted or printed our price
          tp_queue_adjusted_fill   enough volume went through to reach US

        The gap between them is the whole question about this strategy, so it
        is measured rather than assumed away.
        """
        if getattr(px, "best_bid", 0.0) >= pos.tp_price - 1e-9 or \
                getattr(px, "last_trade_price", 0.0) >= pos.tp_price - 1e-9:
            if not pos.tp_price_reached:
                pos.tp_price_reached = True
                pos.tp_price_reached_at = self.clock()
                self.tp_price_reached_count += 1
        if st is None:
            return
        pos.tp_consumed = max(pos.tp_consumed, getattr(st, "consumed", 0.0))
        level_now = getattr(st, "level_size_last", 0.0)
        if level_now:
            pos.tp_level_size_min = min(pos.tp_level_size_min or level_now, level_now)
        if getattr(st, "optimistic_filled", False) and not pos.tp_price_reached:
            pos.tp_price_reached = True
            pos.tp_price_reached_at = self.clock()
        evidence = getattr(st, "fill_evidence", "")
        if evidence:
            pos.tp_fill_evidence = evidence
        if st.filled > 0:
            pos.tp_queue_adjusted_fill = True

    def _secs_left(self, pos: Position) -> float:
        try:
            return seconds_until(_parse_dt(pos.end_time_iso))
        except Exception:
            return 0.0

    def _stop_reason(self, pos: Position, px, secs: float) -> str:
        """Why we should bail out now, or '' to keep waiting for the TP."""
        bid = getattr(px, "best_bid", 0.0)
        if bid > 0 and bid <= pos.stop_price:
            return f"stop_price(${bid:.3f}<=${pos.stop_price:.2f})"

        # "The underlying crossed back through the strike" is only a fact if
        # it is measured against the reference the market resolves from.
        # Binance is a different series (measured +7 to +9 bps above the
        # Chainlink feed) and the market settles on a TWAP, so a proxy
        # crossing is a hint, not evidence. It is recorded either way, but it
        # does not force an exit unless the reference is official — or unless
        # S9099_ALLOW_PROXY_THRESHOLD_STOP is explicitly turned on.
        if self.stop_on_threshold_cross:
            favourable, source, is_official = self.settlement.distance_for_side(
                pos.asset, self._window_start_of(pos), pos.side
            )
            if favourable < 0:
                if is_official:
                    return f"reference_crossed_threshold({source})"
                if self.allow_proxy_threshold_stop:
                    return f"proxy_crossed_threshold({source})"
                if not pos.proxy_cross_seen:
                    pos.proxy_cross_seen = True
                    pos.proxy_cross_at = self.clock()
                    self._log(
                        f"{pos.asset} {pos.side}: PROXY reference crossed back "
                        f"({source}) — recorded, not acted on (no official reference)",
                        "warn",
                    )

        if self.stop_velocity_drop > 0 and bid > 0:
            hist = self._px_hist.get(pos.trade_id, [])
            cutoff = self.clock() - self.stop_velocity_window
            window = [p for ts, p in hist if ts >= cutoff]
            if window and (max(window) - bid) >= self.stop_velocity_drop:
                return f"adverse_velocity(-{max(window) - bid:.3f} in {self.stop_velocity_window:.0f}s)"

        if self.exit_before_expiry > 0 and secs <= self.exit_before_expiry:
            return f"expiry({secs:.0f}s_left)"
        if secs <= 0:
            return "market_expired"
        return ""

    def _emergency_exit(self, pos: Position, px, reason: str):
        """
        Cancel the resting take-profit FIRST, then sell into the bid.

        Selling before the cancel lands is how the same shares get sold twice,
        so a failed cancel aborts the exit and retries on the next tick.
        """
        if not pos.stop_reason:
            pos.stop_reason = reason
            pos.stop_epoch = self.clock()
            self._log(f"EMERGENCY EXIT {pos.asset} {pos.side}: {reason}", "warn")

        if pos.tp_order_id:
            if not self.broker.cancel(pos.tp_order_id):
                self._log(f"{pos.asset}: TP cancel failed — not selling yet", "error")
                return
            # The cancel may have raced a fill; take the final count.
            final = self._order_status(pos.tp_order_id)
            if final and final.filled > pos.tp_filled_qty:
                pos.tp_filled_qty = final.filled
                pos.tp_fill_price = final.avg_price or pos.tp_price
                pos.tp_fee_actual = fees.estimate_fee(
                    pos.tp_filled_qty, pos.tp_fill_price,
                    final.liquidity == "taker", self._schedule_of(pos),
                )
            pos.tp_order_id = ""

        pos.phase = Phase.EXIT_PENDING

        qty = pos.open_qty
        if qty <= 1e-9:
            self.tp_fills += 1
            self._close(pos, "tp_filled")
            return

        bid = getattr(px, "best_bid", 0.0)
        if bid <= 0 or qty < self.min_shares:
            pos.exit_reason = pos.exit_reason or "no_exit_liquidity"
            self._await_settlement(pos)
            self._log(
                f"{pos.asset}: cannot exit {qty:.0f} sh (bid ${bid:.3f}) — "
                f"holding to resolution",
                "warn",
            )
            return

        if pos.exit_attempts >= 5:
            pos.exit_reason = "exit_rejected"
            self._await_settlement(pos)
            return
        pos.exit_attempts += 1
        order_id = self._submit_once(
            pos, f"exit:{pos.exit_attempts}",
            lambda: self.broker.place_sell(pos.token_id, bid, qty, pos.market_id),
        )
        if not order_id:
            return
        pos.exit_order_id = order_id
        pos.exit_liquidity = "taker"
        self._log(f"EXIT ORDER {pos.asset} {qty:.0f} sh @ ${bid:.3f} ({reason})", "trade")
        self._save_state()

    def _advance_exit(self, pos: Position, px, secs: float):
        if pos.awaiting_settlement:
            self._queue_resolution(pos)   # idempotent; covers recovered positions
            return
        if not pos.exit_order_id:
            self._emergency_exit(pos, px, pos.stop_reason or "retry_exit")
            return
        st = self._order_status(pos.exit_order_id)
        if st and st.filled > pos.exit_qty:
            filled_now = st.filled
            pos.exit_price = st.avg_price or pos.exit_price
            pos.exit_qty = filled_now
            pos.exit_fee_actual = fees.estimate_fee(
                pos.exit_qty, pos.exit_price,
                st.liquidity != "maker", self._schedule_of(pos),
            )
            actual, _src = fees.actual_fee_for_order(pos.exit_order_id)
            if actual is not None:
                pos.exit_fee_actual = actual
        if pos.open_qty <= 1e-9:
            self.emergency_exits += 1
            self._close(pos, pos.stop_reason or "emergency_exit")
            return
        if secs <= -5:
            # Market is gone; whatever is left settles.
            self._await_settlement(pos)

    # ══════════════════════════════════════════════════════════════════
    #  Closing out + P&L
    # ══════════════════════════════════════════════════════════════════

    def _close(self, pos: Position, reason: str):
        """
        Finish a position. If shares are still held the trade cannot be priced
        until the market resolves, so it parks in the resolution queue instead
        of being written with a made-up exit price.
        """
        pos.exit_reason = reason
        if pos.open_qty > 1e-9:
            pos.awaiting_settlement = True
            pos.phase = Phase.EXIT_PENDING
            self._queue_resolution(pos)
            return
        self._finish(pos)

    def _finish(self, pos: Position):
        """Price the trade, write the row, release the capital."""
        if pos.phase == Phase.CLOSED:
            return
        held = pos.open_qty
        proceeds = (
            pos.tp_filled_qty * pos.tp_fill_price
            + pos.exit_qty * pos.exit_price
            + held * pos.settled_value
        )
        cost = pos.entry_filled_qty * pos.entry_fill_price
        pos.fees_total = round(
            pos.entry_fee_actual + pos.tp_fee_actual + pos.exit_fee_actual, 5
        )
        pos.gross_pnl = round(proceeds - cost, 4)
        pos.realized_pnl = round(pos.gross_pnl - pos.fees_total, 4)
        pos.realized_pnl_pct = round(pos.realized_pnl / cost * 100, 3) if cost else 0.0
        pos.closed_at = self.clock()
        pos.phase = Phase.CLOSED
        pos.bankroll_after = self.available_balance(max_age=0.0)

        if pos.entry_filled_qty > 0:
            self.realized_pnl += pos.realized_pnl
            if pos.realized_pnl > 0:
                self.wins += 1
                self.consecutive_losses = 0
            elif pos.realized_pnl < 0:
                self.losses += 1
                self.consecutive_losses += 1
                self.daily_loss += -pos.realized_pnl

        cand = self._entry_candidate(pos)
        if cand:
            cand.trade_id = pos.trade_id
            cand.traded = True
            cand.our_tp_filled = pos.tp_filled_qty > 0

        trade_9099_log.write(self._trade_row(pos))
        self._log(
            f"CLOSED {pos.asset} {pos.side} [{pos.exit_reason}] "
            f"entry {pos.entry_filled_qty:.0f}@${pos.entry_fill_price:.3f} "
            f"tp {pos.tp_filled_qty:.0f}@${pos.tp_fill_price:.3f} "
            f"fees ${pos.fees_total:.2f} -> P&L ${pos.realized_pnl:+.2f} "
            f"({pos.realized_pnl_pct:+.1f}%)",
            "profit" if pos.realized_pnl >= 0 else "warn",
        )
        self._cleanup(pos)

    def _cleanup(self, pos: Position):
        self.positions.pop(pos.market_id, None)
        self.cooldowns[pos.market_id] = self.clock()
        self._px_hist.pop(pos.trade_id, None)
        self.closed.append(pos)
        if len(self.closed) > 500:
            self.closed = self.closed[-500:]
        self._save_state()

    def _trade_row(self, pos: Position) -> dict:
        cand = self._entry_candidate(pos)
        targets = cand.targets if cand else {}

        def tsecs(p: float):
            t = targets.get(f"{p:.2f}")
            return t["secs"] if t else ""

        correct = ""
        if pos.market_result in ("Up", "Down"):
            correct = (pos.market_result == pos.side)
        return {
            "trade_id": pos.trade_id,
            "candidate_id": pos.candidate_id,
            "mode": pos.mode,
            "market_id": pos.market_id,
            "asset": pos.asset,
            "question": pos.question,
            "side": pos.side,
            "token_id": pos.token_id,
            "market_end_time": pos.end_time_iso,
            "signal_timestamp": _iso(pos.signal_epoch),
            "signal_secs_remaining": round(pos.signal_secs, 2),
            "entry_submitted_at": _iso(pos.entry_submitted_at),
            "entry_requested_price": pos.entry_price_req,
            "entry_requested_qty": pos.entry_qty_req,
            "entry_order_id": pos.entry_order_id,
            "entry_order_type": "GTC_LIMIT",
            "entry_liquidity": pos.entry_liquidity,
            "entry_fill_timestamp": _iso(pos.entry_fill_epoch),
            "entry_fill_price": pos.entry_fill_price,
            "entry_filled_qty": pos.entry_filled_qty,
            "entry_partial": 0 < pos.entry_filled_qty < pos.entry_qty_req,
            "entry_fee_estimated": pos.entry_fee_est,
            "entry_fee_actual": pos.entry_fee_actual,
            "entry_fee_source": pos.entry_fee_source,
            "entry_cost": round(pos.entry_filled_qty * pos.entry_fill_price, 4),
            "tp_submitted_at": _iso(pos.tp_submitted_at),
            "tp_price": pos.tp_price,
            "tp_qty": pos.tp_qty,
            "tp_order_id": pos.tp_order_id,
            "tp_liquidity": pos.tp_liquidity,
            "tp_fill_timestamp": _iso(pos.tp_fill_epoch),
            "tp_fill_price": pos.tp_fill_price,
            "tp_filled_qty": pos.tp_filled_qty,
            "tp_partial": 0 < pos.tp_filled_qty < pos.tp_qty,
            "tp_fee_estimated": pos.tp_fee_est,
            "tp_fee_actual": pos.tp_fee_actual,
            "tp_fee_source": pos.tp_fee_source,
            # ── queue evidence for the resting take-profit ──
            "tp_queue_ahead": pos.tp_queue_ahead,
            "tp_level_size_at_submit": pos.tp_level_size,
            "tp_level_size_min": pos.tp_level_size_min,
            "tp_bid_depth_at_submit": pos.tp_bid_depth_at_submit,
            "tp_secs_remaining_at_submit": round(pos.tp_secs_remaining_at_submit, 1),
            "tp_consumed_volume": round(pos.tp_consumed, 1),
            "tp_price_reached": pos.tp_price_reached,
            "tp_price_reached_at": _iso(pos.tp_price_reached_at),
            "tp_queue_adjusted_fill": pos.tp_queue_adjusted_fill,
            "tp_fill_evidence": pos.tp_fill_evidence,
            "stop_price": pos.stop_price,
            "stop_triggered_at": _iso(pos.stop_epoch),
            "stop_reason": pos.stop_reason,
            "exit_order_id": pos.exit_order_id,
            "exit_liquidity": pos.exit_liquidity,
            "final_exit_price": pos.exit_price,
            "final_exit_qty": pos.exit_qty,
            "exit_fee_actual": pos.exit_fee_actual,
            "exit_reason": pos.exit_reason,
            "gross_pnl": pos.gross_pnl,
            "fees_total": pos.fees_total,
            "realized_pnl": pos.realized_pnl,
            "realized_pnl_pct": pos.realized_pnl_pct,
            "closed_at": _iso(pos.closed_at),
            "hold_secs": round(pos.closed_at - pos.entry_fill_epoch, 2) if pos.entry_fill_epoch else "",
            "max_price_after_entry": pos.max_price_after,
            "min_price_after_entry": pos.min_price_after,
            "secs_to_95": tsecs(0.95),
            "secs_to_97": tsecs(0.97),
            "secs_to_98": tsecs(0.98),
            "secs_to_99": tsecs(0.99),
            "reached_99": "0.99" in targets,
            "liquidity_at_99": (targets.get("0.99") or {}).get("depth", 0),
            "tp_actually_filled": pos.tp_filled_qty > 0,
            "market_result": pos.market_result or "unknown",
            "prediction_correct": correct,
            "settled_value": pos.settled_value,
            "underlying_price": pos.underlying_price,
            "settlement_threshold": pos.threshold,
            "distance_from_threshold": round(pos.distance, 6),
            "spread_at_entry": round(pos.spread_at_entry, 4),
            "ask_depth_at_entry": pos.ask_depth_at_entry,
            "tp_depth_at_entry": pos.tp_depth_at_entry,
            "fee_rate_raw": pos.fee_rate_raw_bps,
            "economic_fee_rate": pos.fee_rate,
            "fee_exponent": pos.fee_exponent,
            "fee_taker_only": pos.fee_taker_only,
            "fee_schedule_source": pos.fee_schedule_source,
            "proxy_cross_seen": pos.proxy_cross_seen,
            "proxy_cross_at": _iso(pos.proxy_cross_at),
            **{k: v for k, v in pos.reference.items()},
            "bankroll_before": round(pos.bankroll_before, 2),
            "bankroll_after": round(pos.bankroll_after, 2),
        }

    # ══════════════════════════════════════════════════════════════════
    #  Resolution + candidate finalisation
    # ══════════════════════════════════════════════════════════════════

    def _await_settlement(self, pos: Position):
        """Park a position that can only be priced by the market result."""
        pos.awaiting_settlement = True
        pos.phase = Phase.EXIT_PENDING
        self._queue_resolution(pos)

    def _queue_resolution(self, obj):
        if not any(o is obj for _, o, _ in self._pending_resolution):
            self._pending_resolution.append(("pos" if isinstance(obj, Position) else "cand",
                                             obj, self.clock()))

    def _resolve(self, market_id: str) -> str:
        """Winning outcome for a market ('Up'/'Down'/''), cached per market."""
        cached = self._resolution_cache.get(market_id)
        if cached and cached[0]:
            return cached[0]
        now = self.clock()
        if cached and now - cached[1] < 15.0:
            return ""
        winner = ""
        try:
            fn = self._resolve_fn
            if fn is None:
                from polymarket_client import check_market_resolution as fn  # noqa: N813
                self._resolve_fn = fn
            result = fn(market_id) or {}
            if result.get("resolved"):
                winner = result.get("winner", "") or ""
        except Exception as e:
            log.debug(f"[9099] resolution lookup failed for {_short(market_id)}: {e}")
        self._resolution_cache[market_id] = (winner, now)
        return winner

    def _sweep_finished(self):
        """
        Housekeeping, once per tick:
          - settle positions that are waiting on a market result
          - close out and write candidate outcome rows for finished markets
        """
        now = self.clock()

        for entry in list(self._pending_resolution):
            kind, obj, first = entry
            winner = self._resolve(obj.market_id)
            expired = now - first > RESOLVE_MAX_WAIT
            if not winner and not expired:
                continue
            self._pending_resolution.remove(entry)
            if kind == "pos":
                obj.market_result = winner or "unknown"
                obj.settled_value = 1.0 if winner and winner == obj.side else 0.0
                if not winner:
                    obj.exit_reason = f"{obj.exit_reason}+unresolved"
                obj.awaiting_settlement = False
                self._finish(obj)
            else:
                obj.resolution = winner or "unknown"
                self._write_candidate_outcome(obj)

        # Candidates whose market has run out: stop tracking, record the result.
        for key, cand in list(self.candidates.items()):
            if cand.finalised:
                continue
            if seconds_until(cand.end_time) > -self.track_after_close:
                continue
            cand.finalised = True
            cand.closed_reason = "market_closed"
            self._settle_decision(cand, traded=cand.traded, trade_id=cand.trade_id)
            winner = self._resolve(cand.market_id)
            if winner:
                cand.resolution = winner
                self._write_candidate_outcome(cand)
            else:
                self._queue_resolution(cand)

    def _write_candidate_outcome(self, cand: Candidate):
        candidate_outcome_log.write(cand.outcome_row())
        self.candidates.pop((cand.market_id, cand.side, cand.level), None)

    # ══════════════════════════════════════════════════════════════════
    #  Market data helpers
    # ══════════════════════════════════════════════════════════════════

    def _order_status(self, order_id: str) -> OrderState | None:
        if not order_id:
            return None
        try:
            st = self.broker.status(order_id)
            self.api_errors = 0
            return st
        except Exception as e:
            self.api_errors += 1
            self._log(f"order status failed for {order_id}: {e}", "warn")
            return None

    def _window_start_of(self, pos: Position) -> float:
        """Epoch of the start of this position's 5-minute market window."""
        try:
            return _parse_dt(pos.end_time_iso).timestamp() - WINDOW_SECONDS
        except Exception:
            return 0.0

    def _full_book(self, token_id: str) -> dict:
        """
        Full order book for a token, cached briefly.

        The websocket feed keeps level 1 only, and the queue in front of a
        resting sell lives in the deeper levels, so this is a REST call on a
        hot path — hence the cache.
        """
        now = self.clock()
        cached = self._book_cache.get(token_id)
        if cached and now - cached[1] < DEPTH_CACHE_TTL:
            return cached[0]
        book = {"bids": [], "asks": []}
        try:
            fn = self._depth_fn
            if fn is None:
                from polymarket_client import fetch_full_orderbook as fn  # noqa: N813
                self._depth_fn = fn
            book = fn(token_id) or book
        except Exception:
            pass
        self._book_cache[token_id] = (book, now)
        return book

    def ask_queue_ahead(self, token_id: str, price: float) -> tuple:
        """
        (shares offered at or below `price`, shares offered exactly at `price`).

        Everything at or below our price trades before a sell we place there:
        cheaper offers get lifted first, and equal offers already resting have
        time priority. This is the number the old model ignored.
        """
        book = self._full_book(token_id)
        ahead = at_level = 0.0
        for entry in book.get("asks", []):
            try:
                p, sz = float(entry.get("price", 0)), float(entry.get("size", 0))
            except (TypeError, ValueError):
                continue
            if p <= price + 1e-9:
                ahead += sz
                if abs(p - price) < 1e-9:
                    at_level += sz
        return ahead, at_level

    def schedule_for(self, market_id: str, token_id: str) -> fees.Schedule:
        """This market's economic fee schedule (cached per market)."""
        sched = self._schedules.get(market_id)
        if sched is None:
            sched = fees.schedule_for_market(condition_id=market_id, token_id=token_id)
            self._schedules[market_id] = sched
        return sched

    def _depth_at(self, token_id: str, price: float) -> float:
        """
        Shares bid at or above `price` — how much of our exit could actually
        be absorbed. Uses the REST full book (the WS feed keeps level 1 only),
        cached briefly because it is a network call on a hot path.
        """
        now = self.clock()
        cached = self._depth_cache.get((token_id, round(price, 3)))
        if cached and now - cached[1] < DEPTH_CACHE_TTL:
            return cached[0]
        total = 0.0
        try:
            for level in self._full_book(token_id).get("bids", []):
                if float(level.get("price", 0)) >= price - 1e-9:
                    total += float(level.get("size", 0))
        except (TypeError, ValueError):
            total = 0.0
        self._depth_cache[(token_id, round(price, 3))] = (total, now)
        return total

    # ══════════════════════════════════════════════════════════════════
    #  Stats (dashboard + CLI)
    # ══════════════════════════════════════════════════════════════════

    def unrealized_pnl(self) -> float:
        """Mark open positions at the current bid, net of the entry fee."""
        total = 0.0
        for pos in self.positions.values():
            if pos.entry_filled_qty <= 0:
                continue
            px = self.feed.get_price(pos.token_id)
            bid = getattr(px, "best_bid", 0.0) or pos.entry_fill_price
            total += (bid - pos.entry_fill_price) * pos.open_qty - pos.entry_fee_actual
        return round(total, 2)

    def stats(self) -> dict:
        decided = self.wins + self.losses
        avg_return = (self.realized_pnl / decided) if decided else 0.0
        balance = self.available_balance()
        open_positions = [p for p in self.positions.values() if p.phase in ACTIVE_PHASES]
        return {
            "mode": self.mode,
            "enabled": self.enabled,
            "auto_trade": self.auto_trade,
            "kill_switch": self.kill_switch,
            "live_blocked_by": config.strategy_9099_block_reasons(),
            "bankroll_start": round(self._starting_bankroll, 2),
            "bankroll": round(balance + sum(p.entry_cost for p in open_positions), 2),
            "available_balance": round(balance, 2),
            "open_positions": len(open_positions),
            "positions": [p.dashboard_dict() for p in open_positions],
            "realized_pnl": round(self.realized_pnl, 2),
            "unrealized_pnl": self.unrealized_pnl(),
            "wins": self.wins,
            "losses": self.losses,
            "hit_rate": round(self.wins / decided * 100, 1) if decided else 0.0,
            "avg_return": round(avg_return, 3),
            "tp_fills": self.tp_fills,
            "tp_price_reached": self.tp_price_reached_count,
            "emergency_exits": self.emergency_exits,
            "queue_aware": getattr(self.broker, "queue_aware", True),
            "reference": {
                "official_available": self.settlement.official_reading(
                    self.assets[0] if self.assets else "BTC"
                ).available,
                "proxy_threshold_stop": self.allow_proxy_threshold_stop,
                "observe_thresholds": list(self.observe_thresholds),
            },
            "entry_timeouts": self.entry_timeouts,
            "trades_today": self.trades_today,
            "daily_loss": round(self.daily_loss, 2),
            "consecutive_losses": self.consecutive_losses,
            "candidates_seen": self.candidates_seen,
            "candidates_traded": self.candidates_traded,
            "candidates_rejected": self.candidates_rejected,
            "candidates_tracking": len(self.candidates),
            "rejection_reasons": dict(sorted(
                self.rejection_reasons.items(), key=lambda kv: -kv[1]
            )[:8]),
            "api_errors": self.api_errors,
            "params": self.params(),
        }

    def params(self) -> dict:
        """The live-editable knobs, for the dashboard."""
        return {
            "entry_price_min": self.entry_price_min,
            "entry_price_max": self.entry_price_max,
            "tp_price": self.tp_price,
            "stop_price": self.stop_price,
            "max_secs_remaining": self.max_secs_remaining,
            "min_secs_remaining": self.min_secs_remaining,
            "candidate_max_secs": self.candidate_max_secs,
            "max_spread": self.max_spread,
            "min_liquidity": self.min_liquidity,
            "min_tp_depth": self.min_tp_depth,
            "min_margin_pct": self.min_margin_pct,
            "size_mode": self.size_mode,
            "fixed_dollars": self.fixed_dollars,
            "max_position_percent": self.max_position_percent,
            "max_position_dollars": self.max_position_dollars,
            "exit_before_expiry": self.exit_before_expiry,
            "max_open_positions": self.max_open_positions,
            "max_daily_loss": self.max_daily_loss,
            "max_consecutive_losses": self.max_consecutive_losses,
            "min_balance": self.min_balance,
            "max_trades_per_day": self.max_trades_per_day,
            "entry_timeout": self.entry_timeout,
            "partial_fill_grace": self.partial_fill_grace,
        }

    _NUMERIC_PARAMS = {
        "entry_price_min", "entry_price_max", "tp_price", "stop_price",
        "max_secs_remaining", "min_secs_remaining", "candidate_max_secs",
        "max_spread",
        "min_liquidity", "min_tp_depth", "min_margin_pct", "fixed_dollars",
        "max_position_percent", "max_position_dollars", "exit_before_expiry",
        "max_daily_loss", "min_balance", "entry_timeout", "partial_fill_grace",
    }
    _INT_PARAMS = {"max_open_positions", "max_consecutive_losses", "max_trades_per_day"}

    def set_params(self, updates: dict) -> dict:
        """
        Apply dashboard edits. Unknown keys are ignored and the sanity
        relationships (tp > entry > stop) are enforced, so a fat-fingered box
        cannot create a strategy that sells below its own stop.
        """
        applied = {}
        for key, value in (updates or {}).items():
            try:
                if key in self._NUMERIC_PARAMS:
                    setattr(self, key, float(value))
                elif key in self._INT_PARAMS:
                    setattr(self, key, int(value))
                elif key == "size_mode" and value in ("fixed_dollars", "percent_bankroll"):
                    self.size_mode = value
                elif key in ("enabled", "auto_trade", "kill_switch"):
                    setattr(self, key, bool(value))
                else:
                    continue
                applied[key] = getattr(self, key)
            except (TypeError, ValueError):
                continue
        # Keep the ladder coherent.
        self.entry_price_max = max(self.entry_price_max, self.entry_price_min)
        self.tp_price = max(self.tp_price, self.entry_price_min + 0.01)
        self.stop_price = min(self.stop_price, self.entry_price_min - 0.01)
        self.min_secs_remaining = min(self.min_secs_remaining, self.max_secs_remaining)
        # A crossing that never becomes a candidate can never be traded, so
        # the tracking window has to cover the entry window.
        if self.candidate_max_secs < self.max_secs_remaining:
            self.candidate_max_secs = self.max_secs_remaining
            self._log(
                f"tracking window raised to {self.candidate_max_secs:.0f}s to cover "
                f"the {self.max_secs_remaining:.0f}s entry window"
            )
        if applied:
            self._log(f"params updated: {applied}")
        return applied

    # ══════════════════════════════════════════════════════════════════
    #  Restart recovery
    # ══════════════════════════════════════════════════════════════════

    def _save_state(self):
        """Persist open positions so a restart does not abandon them."""
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            payload = {
                "saved_at": self.clock(),
                "mode": self.mode,
                "day": self.day,
                "counters": {
                    "trades_today": self.trades_today,
                    "wins": self.wins,
                    "losses": self.losses,
                    "tp_fills": self.tp_fills,
                    "tp_price_reached_count": self.tp_price_reached_count,
                    "emergency_exits": self.emergency_exits,
                    "realized_pnl": self.realized_pnl,
                    "daily_loss": self.daily_loss,
                    "consecutive_losses": self.consecutive_losses,
                    "candidates_seen": self.candidates_seen,
                    "candidates_traded": self.candidates_traded,
                    "candidates_rejected": self.candidates_rejected,
                },
                "positions": [p.to_dict() for p in self.positions.values()],
                "traded_markets": sorted(self.traded_markets),
            }
            tmp = self.state_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, default=str)
            os.replace(tmp, self.state_file)
        except Exception as e:
            log.debug(f"[9099] state save failed: {e}")

    def recover(self) -> int:
        """
        Reload positions left open by a previous run and re-attach to their
        orders.  Called once at startup, before the first tick.

        Live: the order ids are still valid on the exchange, so polling picks
        the position back up exactly where it was.
        Paper: the simulated book is gone, so the orders are re-registered
        with the fills they had, and the position carries on from there.
        """
        if not os.path.exists(self.state_file):
            return 0
        try:
            with open(self.state_file, encoding="utf-8") as f:
                payload = json.load(f)
        except Exception as e:
            self._log(f"could not read saved state: {e}", "warn")
            return 0

        if payload.get("day") == self.day:
            c = payload.get("counters", {})
            for key, value in c.items():
                if hasattr(self, key):
                    setattr(self, key, value)

        restored = 0
        for raw in payload.get("positions", []):
            try:
                pos = Position.from_dict(raw)
            except Exception:
                continue
            if pos.phase in (Phase.CLOSED, Phase.IDLE):
                continue
            # A position saved in the other mode must never be resumed here:
            # paper fills cannot manage a live order, and vice versa.
            if pos.mode != self.mode:
                self._log(
                    f"skipping recovered {pos.mode} position {pos.trade_id} "
                    f"while running {self.mode}", "warn",
                )
                continue
            self.positions[pos.market_id] = pos
            self.traded_markets.add(pos.market_id)
            if isinstance(self.broker, PaperBroker):
                self._restore_paper_orders(pos)
            restored += 1
            self._log(
                f"RECOVERED {pos.asset} {pos.side} in {pos.phase} "
                f"({pos.entry_filled_qty:.0f} sh, tp {pos.tp_filled_qty:.0f} filled)",
                "warn",
            )
        for mid in payload.get("traded_markets", []):
            self.traded_markets.add(mid)
        if restored:
            self._log(f"recovered {restored} open position(s) from {self.state_file}")
        return restored

    def _restore_paper_orders(self, pos: Position):
        for oid, side, price, size, filled in (
            (pos.entry_order_id, "BUY", pos.entry_price_req, pos.entry_qty_req, pos.entry_filled_qty),
            (pos.tp_order_id, "SELL", pos.tp_price, pos.tp_qty, pos.tp_filled_qty),
            (pos.exit_order_id, "SELL", pos.exit_price, pos.exit_qty, pos.exit_qty),
        ):
            if not oid or size <= 0:
                continue
            self.broker.restore_order(OrderState(
                order_id=oid, side=side, token_id=pos.token_id,
                price=price, size=size, filled=filled,
                avg_price=price, created=pos.signal_epoch,
                status="partial" if 0 < filled < size else ("matched" if filled >= size else "live"),
            ))

    # ══════════════════════════════════════════════════════════════════
    #  Shutdown
    # ══════════════════════════════════════════════════════════════════

    def shutdown(self, cancel_open: bool = True):
        """
        Stop cleanly. Working entry orders are cancelled (an unfilled entry is
        worthless after we stop watching it); a resting take-profit is LEFT IN
        THE BOOK on purpose — it is the exit for shares we actually hold, and
        cancelling it would strand them.
        """
        self._log("shutting down")
        if cancel_open:
            for pos in list(self.positions.values()):
                if pos.phase == Phase.ENTRY_PENDING and pos.entry_order_id:
                    try:
                        self.broker.cancel(pos.entry_order_id)
                        self._log(f"cancelled working entry {pos.entry_order_id}")
                    except Exception:
                        pass
                elif pos.phase == Phase.TP_PENDING and pos.tp_order_id:
                    self._log(
                        f"leaving TP {pos.tp_order_id} resting @ ${pos.tp_price:.2f} "
                        f"for {pos.open_qty:.0f} held shares"
                    )
        self._save_state()


# ══════════════════════════════════════════════════════════════════════════
#  Module-level bits used above
# ══════════════════════════════════════════════════════════════════════════

RESOLVE_MAX_WAIT = 600.0     # give up waiting for a market result after 10 min
DEPTH_CACHE_TTL = 2.0        # seconds a full-book depth reading stays fresh


def _iso(epoch: float) -> str:
    if not epoch:
        return ""
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()
