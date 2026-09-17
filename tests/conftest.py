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


class FakeFeed:
    """Hand-driven stand-in for ws_feed.PriceFeed."""

    def __init__(self):
        self.prices: dict[str, FakePrice] = {}

    def set(self, token_id, ask=0.0, ask_size=0.0, bid=0.0, bid_size=0.0, age=0.0):
        self.prices[token_id] = FakePrice(
            best_ask=ask, best_ask_size=ask_size,
            best_bid=bid, best_bid_size=bid_size,
            timestamp=time.time() - age, valid=True,
        )

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

class RecordingBroker(s9099.PaperBroker):
    """PaperBroker that also keeps an ordered log of every call."""

    def __init__(self, feed, balance=500.0):
        super().__init__(feed, starting_balance=balance)
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
        depth_fn=lambda token_id: {"bids": [{"price": 0.99, "size": 400}],
                                   "asks": [{"price": 1.00, "size": 400}]},
        resolve_fn=lambda market_id: {"resolved": True, "winner": "Up"},
        state_file=str(tmp_path / "state.json"),
    )
    eng.min_margin_pct = 0.02
    return eng
