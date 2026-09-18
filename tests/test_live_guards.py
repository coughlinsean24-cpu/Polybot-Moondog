"""Going live is where a wrong default costs real money.

The paper defaults size at 100% of bankroll with no dollar cap, which is
correct for paper: the bankroll is a fixed $500 and the point is a clean
sample. In live, "bankroll" is the real USDC balance, so the same settings
bet the whole wallet on one 5-minute market.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import strategy_9099 as s9099  # noqa: E402
import config  # noqa: E402


@pytest.fixture
def live(engine, monkeypatch):
    monkeypatch.setattr(type(engine), "is_live", property(lambda self: True))
    engine.live_max_position_dollars = 50.0
    return engine


def test_percent_of_a_fat_wallet_is_still_capped(live):
    """The exact trap: 100% sizing against a wallet holding more than the test."""
    live.size_mode = "percent_bankroll"
    live.max_position_percent = 100
    live.max_position_dollars = 0          # no ceiling from the dashboard

    shares = live.calculate_position_size(balance=5000.0, price=0.90)
    spend = shares * 0.90
    assert spend <= 50.0, f"${spend:.2f} of a $5000 wallet went into one trade"
    assert shares > 0, "but it still trades"


def test_the_dashboard_cannot_raise_the_live_cap(live):
    """Not in params()/set_params, so no box can widen it."""
    assert "live_max_position_dollars" not in live.params()
    assert live.set_params({"live_max_position_dollars": 5000}) == {}
    assert live.live_max_position_dollars == 50.0

    # And a max_position_dollars above the cap does not win.
    live.size_mode = "fixed_dollars"
    live.fixed_dollars = 5000
    live.max_position_dollars = 5000
    shares = live.calculate_position_size(balance=5000.0, price=0.90)
    assert shares * 0.90 <= 50.0


def test_a_tighter_dashboard_cap_still_wins(live):
    """The hard cap is a ceiling, not a floor — it must not raise anything."""
    live.size_mode = "fixed_dollars"
    live.fixed_dollars = 10
    live.max_position_dollars = 10
    shares = live.calculate_position_size(balance=5000.0, price=0.90)
    assert 5 <= shares * 0.90 <= 10.5


def test_paper_is_left_alone(engine):
    """Paper still sizes at the full bankroll — that is the point of paper."""
    assert engine.is_live is False
    engine.size_mode = "percent_bankroll"
    engine.max_position_percent = 100
    engine.max_position_dollars = 0
    shares = engine.calculate_position_size(balance=500.0, price=0.90)
    assert shares * 0.90 > 50.0, "the live cap leaked into paper"


def test_live_says_what_else_going_live_armed(live, monkeypatch):
    """
    MANUAL_ONLY=false is required for the live gate, and it also re-arms the
    arb engine — which places its own orders and is not covered by this
    strategy's toggles.
    """
    monkeypatch.setattr(config, "ARB_ENABLED", True)
    monkeypatch.setattr(config, "MANUAL_ONLY", False)
    monkeypatch.setattr(config, "TRADING_ENABLED", True)
    warnings = live.config_warnings()

    assert any("hard-capped at $50" in w for w in warnings)
    assert any("ARB_ENABLED" in w for w in warnings)
    assert any("limit-bid" in w for w in warnings)


def test_none_of_that_is_said_in_paper(engine, monkeypatch):
    monkeypatch.setattr(config, "ARB_ENABLED", True)
    assert not any("LIVE" in w for w in engine.config_warnings())


def test_the_live_gate_still_needs_all_four_keys(monkeypatch):
    """Nothing here loosened the ignition."""
    for flag, value in (("LIVE_TRADING", False), ("PAPER_TRADING", True),
                        ("TRADING_ENABLED", False), ("MANUAL_ONLY", True)):
        monkeypatch.setattr(config, "LIVE_TRADING", True)
        monkeypatch.setattr(config, "PAPER_TRADING", False)
        monkeypatch.setattr(config, "TRADING_ENABLED", True)
        monkeypatch.setattr(config, "MANUAL_ONLY", False)
        assert config.strategy_9099_is_live() is True, "all four open = live"

        monkeypatch.setattr(config, flag, value)
        assert config.strategy_9099_is_live() is False, f"{flag} alone must block"


def test_every_order_is_a_signed_limit_never_a_market_order():
    """The whole point of the question: no market-order path exists."""
    import inspect
    import polymarket_client as pc

    src = inspect.getsource(pc)
    assert 'orderType="GTC"' in src
    for banned in ("MarketOrderArgs", "create_market_order", '"FOK"', '"FAK"'):
        assert banned not in src, f"a market-order path appeared: {banned}"

    # And the entry never bids above the ask by more than the configured
    # slippage, which ships at zero.
    assert config.S9099_ENTRY_SLIPPAGE == 0.0
