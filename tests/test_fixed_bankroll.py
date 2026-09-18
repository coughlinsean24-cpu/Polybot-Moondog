"""Pinned paper buying power.

Sizing is a percentage of the balance, so without a pin every loss shrinks
the next trade: the hundredth observation is taken at a different size from
the first, and a bad night ends collection altogether. Pinning holds buying
power at the starting bankroll while leaving the P&L alone.
"""

import os
import sys
from datetime import datetime, timezone, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import strategy_9099 as s9099  # noqa: E402
from conftest import market_ending_in  # noqa: E402
from test_strategy_9099 import _open_position, prime  # noqa: E402

Phase = s9099.Phase


@pytest.fixture
def eng(engine):
    engine.paper_fixed_bankroll = True
    engine.size_mode = "percent_bankroll"
    engine.max_position_percent = 100
    engine.max_position_dollars = 0
    return engine


def _settle(eng, market, pos, winner):
    eng._resolve_fn = lambda mid: {"resolved": True, "winner": winner}
    eng.exit_before_expiry = 3
    market.end_time = datetime.now(timezone.utc) + timedelta(seconds=1)
    pos.end_time_iso = market.end_time.isoformat()
    eng.feed.set(market.token_id_up, ask=0.99, ask_size=10, bid=0.0, bid_size=0)
    eng.on_tick([market])
    eng.on_tick([market])


def test_a_loss_leaves_buying_power_untouched(eng):
    start = eng.available_balance()
    market, pos = _open_position(eng, eng.feed, secs=60)
    _settle(eng, market, pos, "Down" if pos.side == "Up" else "Up")

    assert pos.phase == Phase.CLOSED
    assert eng.realized_pnl < 0, "this path is supposed to lose"
    assert eng.broker.balance == pytest.approx(start, abs=0.01), (
        "buying power moved with the P&L"
    )


def test_a_win_does_not_let_the_next_trade_grow_either(eng):
    """Pinned means pinned in both directions, or sizing still drifts."""
    start = eng.available_balance()
    market, pos = _open_position(eng, eng.feed, secs=60)
    eng.feed.set(market.token_id_up, ask=1.00, ask_size=10,
                 bid=0.99, bid_size=pos.tp_qty)
    eng.on_tick([market])

    assert pos.phase == Phase.CLOSED
    assert eng.realized_pnl > 0
    assert eng.broker.balance == pytest.approx(start, abs=0.01)


def test_pnl_is_still_the_real_pnl_across_a_losing_run(eng):
    """The pin must not launder losses: every one still lands in realized P&L."""
    start = eng.available_balance()
    expected = 0.0
    for i in range(4):
        market, pos = _open_position(eng, eng.feed, secs=60, market_id=f"0xm{i}")
        _settle(eng, market, pos, "Down" if pos.side == "Up" else "Up")
        assert pos.phase == Phase.CLOSED
        expected += pos.realized_pnl
        eng.cooldowns.clear()

    assert eng.losses == 4
    assert eng.realized_pnl == pytest.approx(expected, abs=0.01)
    assert eng.realized_pnl < 0
    assert eng.broker.balance == pytest.approx(start, abs=0.01)
    # Buying power held while the P&L fell, so the difference is exactly what
    # was injected — nothing has gone missing between the two.
    assert eng.bankroll_topups == pytest.approx(-eng.realized_pnl, abs=0.01)
    assert eng.stats()["equity"] == pytest.approx(start + eng.realized_pnl, abs=0.01)


def _stop_out(eng, market):
    """A partial loss (~11%), not a wipeout — the balance drops but survives."""
    eng.feed.set(market.token_id_up, ask=0.81, ask_size=500, bid=0.78, bid_size=500)
    eng.on_tick([market])
    eng.on_tick([market])


def test_every_trade_is_sized_the_same(eng):
    """
    The point of the whole thing: the sample does not drift.

    Uses partial losses deliberately. A total loss would be refilled by the
    wipeout path anyway, so it would pass with or without the pin and prove
    nothing; a string of ~11% losses is exactly the case that used to shrink
    each successive trade.
    """
    sizes = []
    for i in range(4):
        market, pos = _open_position(eng, eng.feed, secs=60, market_id=f"0xs{i}")
        sizes.append(pos.entry_filled_qty)
        _stop_out(eng, market)
        assert pos.phase == Phase.CLOSED
        eng.cooldowns.clear()

    assert eng.paper_resets == 0, "no wipeout — so the pin is what held the size"
    assert len(set(round(x) for x in sizes)) == 1, f"sizes drifted: {sizes}"


def test_a_losing_run_never_halts_collection(eng):
    """"Choke out and die overnight" — the thing this is for."""
    for i in range(8):
        market, pos = _open_position(eng, eng.feed, secs=60, market_id=f"0xr{i}")
        _stop_out(eng, market)
        eng.cooldowns.clear()

    assert eng.losses == 8
    assert eng.realized_pnl < 0
    assert eng.paper_resets == 0, "nothing had to be invented by the wipeout path"
    assert eng.broker.balance == pytest.approx(eng._starting_bankroll, abs=0.01)
    assert eng._not_trading_because() == [], "something halted the session"
    ok, reason = eng.check_risk_gates("0xnext")
    assert ok, f"gates closed after a losing run: {reason}"


def test_the_balance_never_moves_under_a_working_position(eng):
    """Its entry cost is spent from this same balance."""
    start = eng.available_balance()
    market, pos = _open_position(eng, eng.feed, secs=60)
    assert pos.phase in s9099.ACTIVE_PHASES
    spent = pos.entry_filled_qty * pos.entry_fill_price
    assert eng.broker.balance == pytest.approx(start - spent, abs=0.01)

    assert eng._pin_paper_bankroll() is False
    assert eng.broker.balance == pytest.approx(start - spent, abs=0.01)


def test_live_can_never_be_topped_up(eng, monkeypatch):
    """A real account cannot be refilled, whatever the checkbox says."""
    monkeypatch.setattr(type(eng), "is_live", property(lambda self: True))
    assert eng.paper_fixed_bankroll is True
    assert eng._fixed_bankroll_active() is False
    assert eng._pin_paper_bankroll() is False
    assert eng.stats()["paper_fixed_bankroll"] is False
    assert any("LIVE" in w for w in eng.config_warnings())


def test_off_by_default_in_the_lifecycle_fixture_means_the_old_invariant_holds(engine, feed):
    """The unpinned accounting is a separate mode and still has to add up."""
    assert engine.paper_fixed_bankroll is False
    start = engine.available_balance()
    market, pos = _open_position(engine, feed, secs=60)
    feed.set(market.token_id_up, ask=1.00, ask_size=10, bid=0.99, bid_size=pos.tp_qty)
    engine.on_tick([market])
    assert pos.phase == Phase.CLOSED
    assert engine.broker.balance == pytest.approx(start + engine.realized_pnl, abs=0.01)
    assert engine.bankroll_topups == 0.0


def test_the_toggle_is_editable_and_survives_a_reset(eng):
    assert eng.set_params({"paper_fixed_bankroll": False}) == {
        "paper_fixed_bankroll": False}
    assert eng.params()["paper_fixed_bankroll"] is False
    eng.reset_to_defaults()
    assert eng.params()["paper_fixed_bankroll"] is s9099.config.S9099_PAPER_FIXED_BANKROLL
