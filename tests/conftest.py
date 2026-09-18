"""Shared fixtures for the 90/99 strategy tests.

Nothing here touches the network, the CLOB, or the real data/ directory:
the feeds are hand-driven, the broker is simulated, and the CSV logs are
swapped for in-memory recorders.
"""

import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import strategy_9099 as s9099  # noqa: E402


# ── Fake market data ─────────────────────────────────────────────────────

@dataclass
class FakePrice:
    best_ask: float = 0.0
    best_ask_size: float = 0.0
    best_bid: float = 0.0
    best_bid_size: float = 0.0
    timestamp: float = field(default_factory=time.time)
    valid: bool = True
    # Executed tape — what the queue model actually reads.
    trades: list = field(default_factory=list)   # (ts, price, size, side)
    last_trade_price: float = 0.0

    def volume_since(self, since_ts, side=None, max_price=None, min_price=None):
        total = 0.0
        for ts, price, size, tside in self.trades:
            if ts < since_ts:
                continue
            if side and tside != side:
                continue
            if max_price is not None and price > max_price + 1e-9:
                continue
            if min_price is not None and price < min_price - 1e-9:
                continue
            total += size
        return total

    def max_trade_price_since(self, since_ts):
        prices = [p for ts, p, _s, _sd in self.trades if ts >= since_ts]
        return max(prices) if prices else 0.0


class FakeFeed:
    """Hand-driven stand-in for ws_feed.PriceFeed."""

    def __init__(self):
        self.prices: dict[str, FakePrice] = {}

    def set(self, token_id, ask=0.0, ask_size=0.0, bid=0.0, bid_size=0.0, age=0.0):
        prev = self.prices.get(token_id)
        self.prices[token_id] = FakePrice(
            best_ask=ask, best_ask_size=ask_size,
            best_bid=bid, best_bid_size=bid_size,
            timestamp=time.time() - age, valid=True,
            trades=list(prev.trades) if prev else [],
            last_trade_price=prev.last_trade_price if prev else 0.0,
        )

    def trade(self, token_id, price, size, side="BUY", ts=None):
        """Append an executed print — this is what consumes a resting queue."""
        px = self.prices.get(token_id)
        if px is None:
            self.set(token_id)
            px = self.prices[token_id]
        px.trades.append((time.time() if ts is None else ts, price, size, side))
        px.last_trade_price = price

    def get_price(self, token_id):
        return self.prices.get(token_id, FakePrice(valid=False))


@dataclass
class FakeAsset:
    price: float = 100_000.0
    candle_open: float = 99_950.0   # spot $50 above the threshold -> "Up" winning

    def price_age(self):
        return 0.0


class FakeUnderlying:
    def __init__(self, asset=None):
        self.asset = asset or FakeAsset()

    def get(self, _name):
        return self.asset


@dataclass
class FakeMarket:
    market_id: str = "0xmarket"
    question: str = "Bitcoin Up or Down - 1:00PM-1:05PM ET"
    token_id_up: str = "tok-up"
    token_id_down: str = "tok-down"
    end_time: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc) + timedelta(seconds=30)
    )
    asset: str = "BTC"


def market_ending_in(secs: float, market_id: str = "0xmarket") -> FakeMarket:
    return FakeMarket(
        market_id=market_id,
        end_time=datetime.now(timezone.utc) + timedelta(seconds=secs),
    )


# ── Recording broker (for call-ordering and duplicate checks) ────────────

# A book with nothing resting at the take-profit: the queue is empty, so
# queue-aware and optimistic agree and the older tests still describe the
# same behaviour. Tests about queueing set their own depth.
EMPTY_TP_BOOK = {"bids": [{"price": 0.99, "size": 400}], "asks": []}


class RecordingBroker(s9099.PaperBroker):
    """PaperBroker that also keeps an ordered log of every call."""

    def __init__(self, feed, balance=500.0, book_fn=None):
        super().__init__(feed, starting_balance=balance,
                         book_fn=book_fn or (lambda _t: EMPTY_TP_BOOK))
        self.calls: list[tuple] = []
        self.fail_sells = 0

    def place_buy(self, token_id, price, size, market_id=""):
        self.calls.append(("buy", token_id, price, size))
        return super().place_buy(token_id, price, size, market_id)

    def place_sell(self, token_id, price, size, market_id=""):
        self.calls.append(("sell", token_id, price, size))
        if self.fail_sells > 0:
            self.fail_sells -= 1
            return None
        return super().place_sell(token_id, price, size, market_id)

    def cancel(self, order_id):
        self.calls.append(("cancel", order_id))
        return super().cancel(order_id)

    def kinds(self):
        return [c[0] for c in self.calls]


# ── In-memory CSV logs ───────────────────────────────────────────────────

class RecordingLog:
    def __init__(self):
        self.rows: list[dict] = []

    def write(self, row: dict):
        self.rows.append(row)


@pytest.fixture
def logs(monkeypatch):
    """Swap the three CSV logs for in-memory recorders."""
    rec = {
        "candidate": RecordingLog(),
        "outcome": RecordingLog(),
        "trade": RecordingLog(),
    }
    monkeypatch.setattr(s9099, "candidate_log", rec["candidate"])
    monkeypatch.setattr(s9099, "candidate_outcome_log", rec["outcome"])
    monkeypatch.setattr(s9099, "trade_9099_log", rec["trade"])
    return rec


@pytest.fixture
def feed():
    return FakeFeed()


@pytest.fixture
def engine(feed, logs, tmp_path):
    """
    Engine wired to fakes only.

    depth_fn / resolve_fn are injected so nothing in a test can reach the
    network through the lazy-import fallbacks.
    """
    broker = RecordingBroker(feed)
    eng = s9099.Strategy9099(
        feed=feed,
        underlying=FakeUnderlying(),
        broker=broker,
        depth_fn=lambda token_id: dict(EMPTY_TP_BOOK),
        resolve_fn=lambda market_id: {"resolved": True, "winner": "Up"},
        state_file=str(tmp_path / "state.json"),
    )
    # The lifecycle tests are about the lifecycle, not about whichever
    # defaults happen to ship. Pin the ones they depend on so a deliberate
    # default change shows up as a real failure, not a dozen false ones.
    eng.min_margin_pct = 0.0
    eng.size_mode = "fixed_dollars"
    eng.fixed_dollars = 25.0
    eng.max_position_dollars = 100.0
    eng.max_daily_loss = 50.0
    eng.max_consecutive_losses = 3
    eng.max_trades_per_day = 40
    eng.entry_price_max = 0.96
    # These tests drive a stop-out by dropping the bid to 0.78, so they need
    # the stop above it. It is pinned rather than left to the default, which
    # is a tuning knob and moves. Note pos.stop_price is snapshotted at entry,
    # so this has to be set before the position opens, not after.
    eng.stop_price = 0.80
    # These tests are about the accounting that runs when the balance moves
    # with the P&L. Pinned buying power is a separate mode with its own
    # invariant (see test_fixed_bankroll.py), so it is off here rather than
    # quietly changing what every balance assertion means.
    eng.paper_fixed_bankroll = False
    return eng


def seed_reference(engine, market, prices, dt=1.0):
    """
    Feed a price path into a market's settlement window.

    The margin filter now compares the window TWAP against the window's
    opening price (the statistic the market actually resolves on), so a
    single flat sample means zero distance — the path has to move.
    """
    window_start = market.end_time.timestamp() - 300
    now = time.time() - dt * len(prices)
    for price in prices:
        engine.settlement.binance_feed.asset.price = price
        engine.settlement.observe(market.asset, window_start, now)
        now += dt
    return window_start
