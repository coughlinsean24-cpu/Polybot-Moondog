"""The shared WebSocket should carry the markets being traded, not every
market that exists.

Four assets at an hour of lookahead was ~96 subscribed tokens, ~90 of them
for markets that would not trade for up to an hour. None of those ever send
anything, so every one looked permanently "stale" and was re-subscribed every
30s forever — and every re-subscribe pulls a full book snapshot back down the
one socket the live market is sharing.
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import polymarket_client as pc  # noqa: E402
import ws_feed  # noqa: E402


@pytest.fixture
def feed():
    return ws_feed.PriceFeed()


def _sub(feed, up, down, secs_to_close, market_id="m"):
    feed.subscribe(up, down, market_id, end_epoch=time.time() + secs_to_close)


def test_a_market_that_has_not_started_is_not_chased(feed):
    """Silence from an hour away is not a fault."""
    _sub(feed, "far-up", "far-down", 3600, "far")
    assert feed.in_resub_horizon("far-up") is False
    assert feed.get_stale_tokens(resub_eligible_only=True) == []
    # It is still reported as stale for diagnostics — just not chased.
    assert set(feed.get_stale_tokens()) == {"far-up", "far-down"}


def test_a_market_about_to_close_is_chased(feed):
    _sub(feed, "near-up", "near-down", 60, "near")
    assert feed.in_resub_horizon("near-up") is True
    assert set(feed.get_stale_tokens(resub_eligible_only=True)) == {
        "near-up", "near-down"}


def test_an_unknown_close_time_is_still_chased(feed):
    """Not being told about a token is no reason to stop maintaining it."""
    feed.subscribe("mystery-up", "mystery-down", "m")
    assert feed.in_resub_horizon("mystery-up") is True
    assert "mystery-up" in feed.get_stale_tokens(resub_eligible_only=True)


def test_unsubscribe_forgets_the_close_time(feed):
    _sub(feed, "u", "d", 60, "m")
    feed.unsubscribe("u", "d")
    assert feed._token_end == {}


def test_a_change_with_no_top_of_book_does_not_refresh_the_clock(feed):
    """
    A price that has stopped moving used to report itself as healthy: the
    timestamp was stamped on every change, so the stale check never fired and
    the re-subscribe that would have refilled the book never ran.
    """
    feed.subscribe("tok", "tok2", "m")
    now = time.time()
    feed._handle_single_message(
        {"event_type": "book", "asset_id": "tok",
         "asks": [{"price": "0.51", "size": "100"}],
         "bids": [{"price": "0.50", "size": "100"}]},
        now - 100,
    )
    px = feed.get_price("tok")
    assert px.best_ask == 0.51
    assert px.timestamp == now - 100

    # A change carrying neither side must leave the clock where it was.
    feed._handle_single_message(
        {"event_type": "price_change",
         "price_changes": [{"asset_id": "tok", "size": "5", "side": "SELL"}]},
        now,
    )
    assert feed.get_price("tok").timestamp == now - 100, "stamped fresh on nothing"
    assert "tok" in feed.get_stale_tokens(), "so the stall is now visible"

    # A change that does carry one is applied as before.
    feed._handle_single_message(
        {"event_type": "price_change",
         "price_changes": [{"asset_id": "tok", "best_ask": "0.62"}]},
        now,
    )
    px = feed.get_price("tok")
    assert px.best_ask == 0.62
    assert px.timestamp == now


def test_lookahead_is_no_wider_than_any_strategy_needs():
    """Every extra window is two more subscriptions on the shared socket."""
    import config
    assert pc.MARKET_LOOK_AHEAD * 300 >= config.S9099_CANDIDATE_MAX_SECS, (
        "discovery must reach further ahead than the tracking window"
    )
    assert pc.MARKET_LOOK_AHEAD <= 6, "an hour of lookahead is what caused this"


def test_assets_and_lookahead_are_overridable(monkeypatch):
    """Nothing here is a one-way door."""
    import importlib
    monkeypatch.setenv("MARKET_ASSETS", "btc")
    monkeypatch.setenv("MARKET_LOOK_AHEAD", "5")
    reloaded = importlib.reload(pc)
    try:
        assert reloaded.MARKET_ASSETS == ["btc"]
        assert reloaded.MARKET_LOOK_AHEAD == 5
    finally:
        monkeypatch.delenv("MARKET_ASSETS")
        monkeypatch.delenv("MARKET_LOOK_AHEAD")
        importlib.reload(pc)


# ── Which assets get discovered at all ───────────────────────────────────

import web_dashboard as wd  # noqa: E402


class _StubStrategy:
    def __init__(self, assets):
        self.assets = list(assets)


@pytest.fixture
def focus(monkeypatch):
    """engine.s9099 is None until init_9099() runs, which needs live feeds."""
    monkeypatch.delenv("MARKET_ASSETS", raising=False)
    monkeypatch.setattr(wd.engine, "s9099", _StubStrategy(["BTC"]), raising=False)
    monkeypatch.setattr(wd.engine, "_last_discovery_assets", None, raising=False)
    return wd


def test_focus_follows_the_running_strategy_when_nothing_else_trades(focus, monkeypatch):
    monkeypatch.setattr(wd.config, "MANUAL_ONLY", True)
    assert wd.discovery_assets() == ["btc"]


def test_every_asset_comes_back_when_the_other_automation_is_on(focus, monkeypatch):
    monkeypatch.setattr(wd.config, "MANUAL_ONLY", False)
    monkeypatch.setattr(wd.config, "TRADING_ENABLED", True)
    assert wd.discovery_assets() == list(wd._pm.MARKET_ASSETS)


def test_an_explicit_env_setting_always_wins(focus, monkeypatch):
    monkeypatch.setenv("MARKET_ASSETS", "btc,eth")
    monkeypatch.setattr(wd.config, "MANUAL_ONLY", True)
    assert wd.discovery_assets() == list(wd._pm.MARKET_ASSETS)


def test_an_empty_strategy_asset_list_means_all_not_none(focus, monkeypatch):
    """Blank means every asset — never zero markets and a silent dead bot."""
    monkeypatch.setattr(wd.config, "MANUAL_ONLY", True)
    wd.engine.s9099.assets = []
    assert wd.discovery_assets() == list(wd._pm.MARKET_ASSETS)
