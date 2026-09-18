"""Market discovery must not be able to hold the trading loop.

fetch_active_markets() runs inside the one loop that also places, cancels and
manages orders.  A slow Gamma response used to stall all of it for as long as
the slowest request took, with nothing anywhere saying so.
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import polymarket_client as pc  # noqa: E402


class FakeMarket:
    """Just enough of MarketWindow for fetch_active_markets' own bookkeeping."""

    def __init__(self, slug, asset):
        self.slug = slug
        self.asset = asset


@pytest.fixture
def fast_deadline(monkeypatch):
    monkeypatch.setattr(pc, "MARKET_FETCH_DEADLINE", 0.5)
    monkeypatch.setattr(pc, "MARKET_FETCH_TIMEOUT", 10.0)


def test_hung_gamma_does_not_block_past_the_deadline(fast_deadline, monkeypatch):
    def _hang(*_a, **_kw):
        time.sleep(30)
        raise AssertionError("should never get here")

    monkeypatch.setattr(pc.requests, "get", _hang)

    started = time.time()
    markets = pc.fetch_active_markets(assets=["btc"])
    elapsed = time.time() - started

    assert markets == []
    # 0.5s deadline plus scheduling slack — nowhere near the 30s hang.
    assert elapsed < 5.0, f"discovery blocked for {elapsed:.1f}s"


def test_slow_responses_keep_whatever_came_back_in_time(fast_deadline, monkeypatch):
    """A short poll is fine: the fast slugs still land in the watch list."""
    calls = {"n": 0}

    class _Resp:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    def _mixed(url, params=None, timeout=None):
        calls["n"] += 1
        slug = (params or {}).get("slug", "")
        # Every slug but the first hangs past the deadline.
        if calls["n"] > 1:
            time.sleep(30)
        return _Resp([{"slug": slug}])

    seen = []

    def _parse(event, asset):
        seen.append(event)
        return FakeMarket(event["slug"], asset)

    monkeypatch.setattr(pc.requests, "get", _mixed)
    monkeypatch.setattr(pc, "_parse_event_to_market", _parse)

    started = time.time()
    markets = pc.fetch_active_markets(assets=["btc"])
    elapsed = time.time() - started

    assert elapsed < 5.0
    assert len(markets) == 1
    assert markets[0].slug.startswith("btc-updown-5m-")


def test_healthy_gamma_still_returns_every_slug(monkeypatch):
    class _Resp:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    def _ok(url, params=None, timeout=None):
        return _Resp([{"slug": (params or {}).get("slug", "")}])

    monkeypatch.setattr(pc.requests, "get", _ok)
    monkeypatch.setattr(
        pc, "_parse_event_to_market",
        lambda event, asset: FakeMarket(event["slug"], asset),
    )

    expected = len(pc._generate_window_timestamps(look_ahead=10, look_behind=1))
    markets = pc.fetch_active_markets(assets=["btc"])
    assert len(markets) == expected
