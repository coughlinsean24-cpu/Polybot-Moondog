"""
Focused tests for the 90c -> 99c strategy.

One test per behaviour that would cost real money if it broke.
"""

import os
from datetime import datetime, timezone, timedelta

import pytest

import config
import fees
import strategy_9099 as s9099
from strategy_9099 import Phase, PaperBroker, LiveBroker, Strategy9099

from conftest import FakeUnderlying, RecordingBroker, market_ending_in


# Book that comfortably passes every market-quality filter.
GOOD_BOOK = dict(ask=0.90, ask_size=500, bid=0.89, bid_size=500)


def prime(feed, market, **up):
    """Set both sides of a market's book. `up` overrides the Up side."""
    book = dict(GOOD_BOOK)
    book.update(up)
    feed.set(market.token_id_up, **book)
    feed.set(market.token_id_down, ask=0.11, ask_size=500, bid=0.10, bid_size=500)


# ══════════════════════════════════════════════════════════════════════
#  1. Entry threshold detection
# ══════════════════════════════════════════════════════════════════════

def test_entry_threshold_detection(engine):
    base = dict(secs_remaining=30, spread=0.01, ask_depth=500,
                signed_distance=50.0, underlying_price=100_000.0)

    ok, reason = engine.check_entry_conditions(price=0.89, **base)
    assert not ok and "price_below_threshold" in reason

    ok, _ = engine.check_entry_conditions(price=0.90, **base)
    assert ok, "exactly at the threshold must qualify"

    ok, _ = engine.check_entry_conditions(price=0.93, **base)
    assert ok

    ok, reason = engine.check_entry_conditions(price=0.97, **base)
    assert not ok and "price_above_max" in reason

    # Configurable, not hardcoded.
    engine.entry_price_min = 0.85
    ok, _ = engine.check_entry_conditions(price=0.86, **base)
    assert ok


def test_entry_never_above_take_profit(engine):
    engine.entry_price_max = 0.999
    ok, reason = engine.check_entry_conditions(
        price=0.99, secs_remaining=30, spread=0.01, ask_depth=500,
        signed_distance=50.0, underlying_price=100_000.0,
    )
    assert not ok and "no_room_to_tp" in reason


# ══════════════════════════════════════════════════════════════════════
#  2. Time-to-expiry requirement
# ══════════════════════════════════════════════════════════════════════

def test_time_to_expiry_window(engine):
    base = dict(price=0.90, spread=0.01, ask_depth=500,
                signed_distance=50.0, underlying_price=100_000.0)
    engine.max_secs_remaining = 60
    engine.min_secs_remaining = 10

    ok, reason = engine.check_entry_conditions(secs_remaining=90, **base)
    assert not ok and "too_early" in reason

    ok, _ = engine.check_entry_conditions(secs_remaining=45, **base)
    assert ok

    ok, reason = engine.check_entry_conditions(secs_remaining=5, **base)
    assert not ok and "too_late" in reason

    # Every bucket we want to test later is reachable from config alone.
    for window in (90, 60, 45, 30, 20, 15, 10):
        engine.max_secs_remaining = window
        engine.min_secs_remaining = 0
        ok, _ = engine.check_entry_conditions(secs_remaining=window - 1, **base)
        assert ok, f"{window}s bucket should accept {window - 1}s"


# ══════════════════════════════════════════════════════════════════════
#  3. Liquidity / market-quality requirements
# ══════════════════════════════════════════════════════════════════════

def test_liquidity_and_quality_filters(engine):
    base = dict(price=0.90, secs_remaining=30,
                signed_distance=50.0, underlying_price=100_000.0)

    engine.min_liquidity = 50
    ok, reason = engine.check_entry_conditions(spread=0.01, ask_depth=20, **base)
    assert not ok and "thin_book" in reason

    ok, reason = engine.check_entry_conditions(spread=0.09, ask_depth=500, **base)
    assert not ok and "spread_too_wide" in reason

    engine.min_tp_depth = 100
    ok, reason = engine.check_entry_conditions(spread=0.01, ask_depth=500, tp_depth=10, **base)
    assert not ok and "thin_at_tp" in reason

    engine.min_tp_depth = 0
    ok, reason = engine.check_entry_conditions(
        spread=0.01, ask_depth=500, data_age=30, **base
    )
    assert not ok and "stale_data" in reason


def test_underlying_margin_filter(engine):
    """Do not buy 'Up' when spot is sitting on the wrong side of the strike."""
    engine.min_margin_pct = 0.02   # 0.02% of 100k = $20
    base = dict(price=0.90, secs_remaining=30, spread=0.01, ask_depth=500,
                underlying_price=100_000.0)

    ok, _ = engine.check_entry_conditions(signed_distance=50.0, side="Up", **base)
    assert ok

    ok, reason = engine.check_entry_conditions(signed_distance=5.0, side="Up", **base)
    assert not ok and "underlying_margin" in reason

    ok, reason = engine.check_entry_conditions(signed_distance=50.0, side="Down", **base)
    assert not ok and "underlying_margin" in reason, "Down needs spot BELOW the open"

    ok, _ = engine.check_entry_conditions(signed_distance=-50.0, side="Down", **base)
    assert ok


# ══════════════════════════════════════════════════════════════════════
#  4. Position sizing
# ══════════════════════════════════════════════════════════════════════

def test_position_sizing_modes(engine):
    engine.size_mode = "fixed_dollars"
    engine.fixed_dollars = 45.0
    engine.max_position_dollars = 1000
    # Zero fee rate -> pure budget / price
    assert engine.calculate_position_size(500, 0.90, fee_rate=0.0) == 50

    engine.size_mode = "percent_bankroll"
    engine.max_position_percent = 10
    assert engine.calculate_position_size(500, 0.90, fee_rate=0.0) == 55  # $50 / 0.90

    engine.max_position_percent = 100
    assert engine.calculate_position_size(500, 0.90, fee_rate=0.0) == 555

    # Never risks the whole account by accident: the dollar cap still binds.
    engine.max_position_dollars = 100
    assert engine.calculate_position_size(500, 0.90, fee_rate=0.0) == 111


def test_position_sizing_accounts_for_fees_and_depth(engine):
    engine.size_mode = "fixed_dollars"
    engine.fixed_dollars = 100.0
    engine.max_position_dollars = 1000

    no_fee = engine.calculate_position_size(500, 0.90, fee_rate=0.0)
    with_fee = engine.calculate_position_size(500, 0.90, fee_rate=0.10)
    assert with_fee < no_fee, "the taker fee is part of the cost of a share"
    total = with_fee * 0.90 + fees.taker_fee(with_fee, 0.90, 0.10)
    assert total <= 100.0 + 1e-6, "sizing must not overspend the budget"

    # Depth caps the order so it is marketable in full.
    assert engine.calculate_position_size(500, 0.90, ask_depth=30, fee_rate=0.0) == 30

    # Below the exchange minimum is no trade at all.
    engine.fixed_dollars = 2.0
    assert engine.calculate_position_size(500, 0.90, fee_rate=0.0) == 0
    assert engine.calculate_position_size(0, 0.90) == 0


# ══════════════════════════════════════════════════════════════════════
#  5/7/8. Entry fill -> immediate take-profit for the FILLED quantity
# ══════════════════════════════════════════════════════════════════════

def test_full_entry_fill_places_tp_immediately(engine, feed):
    market = market_ending_in(30)
    prime(feed, market)

    engine.on_tick([market])                 # candidate -> entry submitted
    pos = engine.positions[market.market_id]
    assert pos.phase == Phase.ENTRY_PENDING
    assert pos.entry_qty_req > 0

    engine.on_tick([market])                 # fill -> TP resting, same tick
    assert pos.phase == Phase.TP_PENDING
    assert pos.entry_filled_qty == pos.entry_qty_req
    assert pos.tp_qty == pos.entry_filled_qty
    assert pos.tp_price == engine.tp_price
    assert pos.tp_order_id
    assert pos.tp_submitted_at >= pos.entry_fill_epoch


def test_partial_entry_fill_sizes_tp_to_actual_fill(engine, feed):
    market = market_ending_in(30)
    # Only 20 shares available at the ask -> a partial fill.
    prime(feed, market, ask_size=20)
    engine.min_liquidity = 10
    engine.partial_fill_grace = 0.0    # take what we got and rest the TP now
    engine.fixed_dollars = 100.0
    engine.size_mode = "fixed_dollars"

    engine.on_tick([market])
    pos = engine.positions[market.market_id]
    requested = pos.entry_qty_req

    # Depth capping means the order itself is already 20; widen it by hand to
    # force a genuine partial fill against a 20-share book.
    pos.entry_qty_req = 60
    engine.broker.orders[pos.entry_order_id].size = 60

    engine.on_tick([market])
    assert pos.entry_filled_qty == 20
    assert pos.entry_filled_qty < 60
    assert pos.tp_qty == 20, "TP must use the ACTUAL filled quantity"
    assert pos.phase == Phase.TP_PENDING
    assert requested == 20  # depth-capped sizing, before the manual widening


def test_entry_is_abandoned_if_the_book_runs_away_first(engine, feed):
    """Qualifying at 90c is no licence to lift a 98c offer a tick later."""
    market = market_ending_in(30)
    prime(feed, market)
    engine.entry_price_max = 0.92

    real_size = engine.calculate_position_size

    def move_the_book(*args, **kwargs):
        # The ask jumps the instant we go to size the order.
        feed.set(market.token_id_up, ask=0.98, ask_size=500, bid=0.97, bid_size=500)
        return real_size(*args, **kwargs)

    # Qualification happens on the 0.90 book; submission sees 0.98.
    engine._evaluate_candidate = lambda *a, **k: (True, "all_checks_passed")
    engine.calculate_position_size = move_the_book
    feed.set(market.token_id_up, ask=0.98, ask_size=500, bid=0.97, bid_size=500)

    engine.on_tick([market])
    assert not [c for c in engine.broker.calls if c[0] == "buy"]
    assert market.market_id not in engine.positions


def test_entry_that_never_fills_is_cancelled(engine, feed):
    market = market_ending_in(30)
    prime(feed, market)
    engine.entry_timeout = 0.0        # expire immediately

    engine.on_tick([market])
    pos = engine.positions[market.market_id]
    # Book moves away before anything fills.
    feed.set(market.token_id_up, ask=0.95, ask_size=500, bid=0.94, bid_size=500)

    engine.on_tick([market])
    assert pos.phase == Phase.CLOSED
    assert pos.exit_reason == "entry_never_filled"
    assert pos.entry_filled_qty == 0
    assert engine.entry_timeouts == 1
    assert ("cancel", pos.entry_order_id) in engine.broker.calls


# ══════════════════════════════════════════════════════════════════════
#  9/10. Take-profit fills
# ══════════════════════════════════════════════════════════════════════

def _open_position(engine, feed, secs=30, market_id="0xmarket"):
    market = market_ending_in(secs, market_id)
    prime(feed, market)
    engine.on_tick([market])
    engine.on_tick([market])
    return market, engine.positions[market.market_id]


def test_partial_tp_fill_keeps_position_open(engine, feed):
    market, pos = _open_position(engine, feed)
    qty = pos.tp_qty

    # Only part of our resting sell gets lifted.
    feed.set(market.token_id_up, ask=1.00, ask_size=10, bid=0.99, bid_size=qty / 2)
    engine.on_tick([market])

    assert pos.tp_filled_qty == pytest.approx(qty / 2)
    assert pos.phase == Phase.TP_PENDING
    assert pos.open_qty == pytest.approx(qty / 2)
    assert engine.tp_fills == 0


def test_full_tp_fill_closes_with_profit(engine, feed, logs):
    market, pos = _open_position(engine, feed)
    qty = pos.tp_qty

    feed.set(market.token_id_up, ask=1.00, ask_size=10, bid=0.99, bid_size=qty)
    engine.on_tick([market])

    assert pos.phase == Phase.CLOSED
    assert pos.exit_reason == "tp_filled"
    assert pos.tp_filled_qty == qty
    assert engine.tp_fills == 1
    assert engine.wins == 1
    assert pos.realized_pnl > 0
    assert market.market_id not in engine.positions, "capital must be released"


# ══════════════════════════════════════════════════════════════════════
#  11/12. Emergency exit
# ══════════════════════════════════════════════════════════════════════

def test_stop_price_triggers_emergency_exit(engine, feed):
    market, pos = _open_position(engine, feed)
    engine.stop_price = 0.80

    # Reversal: the bid collapses through the stop.
    feed.set(market.token_id_up, ask=0.81, ask_size=500, bid=0.78, bid_size=500)
    engine.on_tick([market])

    assert "stop_price" in pos.stop_reason
    assert pos.phase in (Phase.EXIT_PENDING, Phase.CLOSED)

    engine.on_tick([market])
    assert pos.phase == Phase.CLOSED
    assert engine.emergency_exits == 1
    assert pos.realized_pnl < 0
    assert engine.consecutive_losses == 1


def test_tp_is_cancelled_before_the_exit_sell(engine, feed):
    market, pos = _open_position(engine, feed)
    tp_order = pos.tp_order_id
    engine.broker.calls.clear()

    feed.set(market.token_id_up, ask=0.81, ask_size=500, bid=0.78, bid_size=500)
    engine.on_tick([market])

    kinds = engine.broker.kinds()
    assert "cancel" in kinds, "the resting TP must be cancelled"
    assert ("cancel", tp_order) in engine.broker.calls
    assert kinds.index("cancel") < kinds.index("sell"), \
        "cancelling AFTER the exit sell would double-sell the same shares"


def test_failed_tp_cancel_blocks_the_exit_sell(engine, feed, monkeypatch):
    """If we cannot cancel the TP we must not sell — retry instead."""
    market, pos = _open_position(engine, feed)
    monkeypatch.setattr(engine.broker, "cancel", lambda oid: False)
    engine.broker.calls.clear()

    feed.set(market.token_id_up, ask=0.81, ask_size=500, bid=0.78, bid_size=500)
    engine.on_tick([market])

    assert "sell" not in engine.broker.kinds()
    assert pos.open_qty > 0


def test_underlying_crossing_back_triggers_exit(engine, feed):
    market, pos = _open_position(engine, feed)
    engine.stop_price = 0.10          # isolate the threshold rule
    engine.underlying.asset.price = 99_900.0   # now BELOW the candle open

    feed.set(market.token_id_up, ask=0.88, ask_size=500, bid=0.87, bid_size=500)
    engine.on_tick([market])
    assert pos.stop_reason == "underlying_crossed_threshold"


# ══════════════════════════════════════════════════════════════════════
#  13. Duplicate-order prevention
# ══════════════════════════════════════════════════════════════════════

def test_duplicate_market_updates_place_one_order(engine, feed):
    market = market_ending_in(30)
    prime(feed, market)

    for _ in range(10):               # the same update, fired ten times
        engine.on_tick([market])

    buys = [c for c in engine.broker.calls if c[0] == "buy"]
    sells = [c for c in engine.broker.calls if c[0] == "sell"]
    assert len(buys) == 1, f"expected exactly one entry, got {len(buys)}"
    assert len(sells) == 1, f"expected exactly one take-profit, got {len(sells)}"


def test_submit_once_suppresses_a_repeated_intent(engine, feed):
    market, pos = _open_position(engine, feed)
    calls = []
    first = engine._submit_once(pos, "probe", lambda: calls.append(1) or "id-1")
    second = engine._submit_once(pos, "probe", lambda: calls.append(2) or "id-2")
    assert first == "id-1" and second is None
    assert calls == [1]


def test_one_position_per_market(engine, feed):
    market, pos = _open_position(engine, feed)
    # Close it out, then let the same market qualify again.
    feed.set(market.token_id_up, ask=1.00, ask_size=10, bid=0.99, bid_size=pos.tp_qty)
    engine.on_tick([market])
    assert pos.phase == Phase.CLOSED

    prime(feed, market)
    engine.market_cooldown = 0
    engine.on_tick([market])
    assert market.market_id not in engine.positions
    ok, reason = engine.check_risk_gates(market.market_id)
    assert not ok and reason == "already_traded_this_market"


# ══════════════════════════════════════════════════════════════════════
#  14. Market expiration
# ══════════════════════════════════════════════════════════════════════

def test_no_entry_after_expiration(engine, feed):
    market = market_ending_in(-1)     # already closed
    prime(feed, market)
    engine.on_tick([market])
    assert not engine.positions
    assert not [c for c in engine.broker.calls if c[0] == "buy"]


def test_open_position_exits_before_expiry(engine, feed):
    engine.exit_before_expiry = 3
    market, pos = _open_position(engine, feed, secs=30)

    # Time-travel the market to T-2s.
    market.end_time = datetime.now(timezone.utc) + timedelta(seconds=2)
    engine.on_tick([market])
    assert "expiry" in pos.stop_reason


def test_held_shares_settle_from_the_market_result(engine, feed, logs):
    """No bid to sell into: the trade is priced from the resolution, not guessed."""
    market, pos = _open_position(engine, feed)
    engine.stop_price = 0.10
    engine.exit_before_expiry = 3
    resolved = {"yet": False}
    engine._resolve_fn = lambda mid: (
        {"resolved": True, "winner": "Up"} if resolved["yet"] else {"resolved": False}
    )
    market.end_time = datetime.now(timezone.utc) + timedelta(seconds=1)
    feed.set(market.token_id_up, ask=0.95, ask_size=100, bid=0.0, bid_size=0)

    engine.on_tick([market])
    assert pos.awaiting_settlement is True, "shares we cannot sell wait for the result"
    assert pos.phase == Phase.EXIT_PENDING
    assert not logs["trade"].rows, "nothing is written until the trade can be priced"

    resolved["yet"] = True
    engine._resolution_cache.clear()
    engine.on_tick([market])
    assert pos.phase == Phase.CLOSED
    assert pos.market_result == "Up"
    assert pos.settled_value == 1.0
    assert pos.realized_pnl > 0
    assert logs["trade"].rows[-1]["settled_value"] == 1.0


# ══════════════════════════════════════════════════════════════════════
#  15. Restart / recovery
# ══════════════════════════════════════════════════════════════════════

def test_restart_recovers_open_position(engine, feed, logs, tmp_path):
    market, pos = _open_position(engine, feed)
    assert os.path.exists(engine.state_file)

    # A brand-new engine, as if the process had been restarted.
    broker2 = RecordingBroker(feed)
    engine2 = Strategy9099(
        feed=feed, underlying=FakeUnderlying(), broker=broker2,
        depth_fn=lambda t: {"bids": [], "asks": []},
        resolve_fn=lambda m: {"resolved": False},
        state_file=engine.state_file,
    )
    assert engine2.recover() == 1

    restored = engine2.positions[market.market_id]
    assert restored.phase == Phase.TP_PENDING
    assert restored.tp_qty == pos.tp_qty
    assert restored.tp_order_id == pos.tp_order_id
    assert market.market_id in engine2.traded_markets

    # It keeps managing the position instead of opening a second one.
    feed.set(market.token_id_up, ask=1.00, ask_size=10, bid=0.99, bid_size=pos.tp_qty)
    engine2.on_tick([market])
    assert restored.phase == Phase.CLOSED
    assert not [c for c in broker2.calls if c[0] == "buy"]


def test_recovery_refuses_a_position_from_the_other_mode(engine, feed):
    market, pos = _open_position(engine, feed)
    pos.mode = "LIVE"          # saved by a live run
    engine._save_state()

    engine2 = Strategy9099(
        feed=feed, underlying=FakeUnderlying(), broker=RecordingBroker(feed),
        depth_fn=lambda t: {"bids": [], "asks": []},
        resolve_fn=lambda m: {"resolved": False},
        state_file=engine.state_file,
    )
    assert engine2.recover() == 0
    assert not engine2.positions


# ══════════════════════════════════════════════════════════════════════
#  16/17. Paper vs live
# ══════════════════════════════════════════════════════════════════════

def test_paper_mode_cannot_reach_the_order_api(engine, feed, monkeypatch):
    import polymarket_client as pmc

    def boom(*a, **k):
        raise AssertionError("paper mode must never call the live order API")

    monkeypatch.setattr(pmc, "place_limit_buy", boom)
    monkeypatch.setattr(pmc, "place_limit_sell", boom)
    monkeypatch.setattr(pmc, "cancel_order", boom)

    assert isinstance(engine.broker, PaperBroker)
    assert engine.mode == "PAPER"

    market, pos = _open_position(engine, feed)
    feed.set(market.token_id_up, ask=1.00, ask_size=10, bid=0.99, bid_size=pos.tp_qty)
    engine.on_tick([market])
    assert pos.phase == Phase.CLOSED     # full round trip, nothing real sent


def test_live_requires_explicit_activation(monkeypatch, feed):
    # Default config: paper. make_broker must fail closed.
    assert config.strategy_9099_is_live() is False
    assert isinstance(s9099.make_broker(feed), PaperBroker)

    with pytest.raises(RuntimeError):
        LiveBroker()

    # Every gate open except one -> still paper.
    monkeypatch.setattr(config, "LIVE_TRADING", True)
    monkeypatch.setattr(config, "PAPER_TRADING", False)
    monkeypatch.setattr(config, "TRADING_ENABLED", True)
    monkeypatch.setattr(config, "MANUAL_ONLY", True)
    assert config.strategy_9099_is_live() is False
    assert isinstance(s9099.make_broker(feed), PaperBroker)

    monkeypatch.setattr(config, "LIVE_TRADING", False)
    monkeypatch.setattr(config, "MANUAL_ONLY", False)
    assert config.strategy_9099_is_live() is False

    # All four, explicitly.
    monkeypatch.setattr(config, "LIVE_TRADING", True)
    assert config.strategy_9099_is_live() is True
    assert config.strategy_9099_mode() == "LIVE"


def test_block_reasons_name_every_closed_gate():
    reasons = config.strategy_9099_block_reasons()
    assert "LIVE_TRADING is false" in reasons
    assert "PAPER_TRADING is true" in reasons


# ══════════════════════════════════════════════════════════════════════
#  18. P&L
# ══════════════════════════════════════════════════════════════════════

def test_pnl_is_net_of_fees(engine, feed):
    market, pos = _open_position(engine, feed)
    qty = pos.tp_qty
    feed.set(market.token_id_up, ask=1.00, ask_size=10, bid=0.99, bid_size=qty)
    engine.on_tick([market])

    gross = qty * (pos.tp_fill_price - pos.entry_fill_price)
    assert pos.gross_pnl == pytest.approx(gross, abs=1e-6)
    assert pos.fees_total == pytest.approx(pos.entry_fee_actual, abs=1e-9), \
        "the resting TP is a maker fill and costs nothing"
    assert pos.realized_pnl == pytest.approx(gross - pos.fees_total, abs=1e-6)
    assert pos.realized_pnl < pos.gross_pnl
    assert pos.realized_pnl_pct == pytest.approx(
        pos.realized_pnl / (qty * pos.entry_fill_price) * 100, abs=1e-3
    )
    assert engine.realized_pnl == pytest.approx(pos.realized_pnl, abs=1e-6)


def test_loss_counters_and_daily_loss(engine, feed):
    market, pos = _open_position(engine, feed)
    feed.set(market.token_id_up, ask=0.81, ask_size=500, bid=0.78, bid_size=500)
    engine.on_tick([market])
    engine.on_tick([market])

    assert pos.realized_pnl < 0
    assert engine.losses == 1
    assert engine.daily_loss == pytest.approx(-pos.realized_pnl, abs=1e-6)

    engine.max_daily_loss = 0.01
    ok, reason = engine.check_risk_gates()
    assert not ok and "max_daily_loss" in reason


# ══════════════════════════════════════════════════════════════════════
#  19. Fees
# ══════════════════════════════════════════════════════════════════════

def test_fee_formula_matches_polymarket():
    # fee = shares * rate * price * (1 - price), USDC, takers only.
    assert fees.taker_fee(100, 0.50, 0.10) == pytest.approx(2.50)
    assert fees.taker_fee(100, 0.90, 0.10) == pytest.approx(0.90)
    assert fees.taker_fee(100, 0.10, 0.10) == pytest.approx(0.90), "symmetric around 50c"
    assert fees.taker_fee(100, 0.99, 0.10) == pytest.approx(0.099)
    assert fees.taker_fee(556, 0.90, 0.10) == pytest.approx(5.004, abs=1e-3)

    # Makers are not charged.
    assert fees.maker_fee(556, 0.99, 0.10) == 0.0
    assert fees.fee_for_fill(100, 0.90, is_taker=False, rate=0.10) == 0.0
    assert fees.fee_for_fill(100, 0.90, is_taker=True, rate=0.10) == pytest.approx(0.90)

    # Degenerate inputs cost nothing rather than raising.
    assert fees.taker_fee(0, 0.90, 0.10) == 0.0
    assert fees.taker_fee(100, 1.0, 0.10) == 0.0
    # Dust rounds away exactly like the exchange does.
    assert fees.taker_fee(0.0001, 0.99, 0.10) == 0.0


def test_fee_rate_is_read_live_and_cached(monkeypatch):
    fees.clear_cache()
    calls = []

    class Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"base_fee": 1000}

    def fake_get(url, params=None, timeout=None):
        calls.append(params["token_id"])
        return Resp()

    monkeypatch.setattr(fees.requests, "get", fake_get)
    assert fees.fee_rate_for_token("tok") == pytest.approx(0.10)
    assert fees.fee_rate_for_token("tok") == pytest.approx(0.10)
    assert len(calls) == 1, "the rate should be cached per token"

    # A dead endpoint falls back to the configured default, never to zero.
    fees.clear_cache()
    monkeypatch.setattr(fees.requests, "get",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    assert fees.fee_rate_for_token("tok2") == fees.default_rate()
    assert fees.default_rate() > 0


def test_estimated_and_actual_fees_are_recorded_separately(engine, feed, logs):
    market, pos = _open_position(engine, feed)
    feed.set(market.token_id_up, ask=1.00, ask_size=10, bid=0.99, bid_size=pos.tp_qty)
    engine.on_tick([market])

    row = logs["trade"].rows[-1]
    assert row["entry_fee_estimated"] > 0
    assert row["entry_fee_actual"] > 0
    assert row["tp_fee_estimated"] == 0.0 and row["tp_fee_actual"] == 0.0
    assert row["fee_rate"] > 0


# ══════════════════════════════════════════════════════════════════════
#  20. Data collection
# ══════════════════════════════════════════════════════════════════════

def test_trade_row_agrees_with_the_candidate_record(engine, feed, logs):
    """A 99c print that fills our own TP must show up as reached_99."""
    market, pos = _open_position(engine, feed)
    feed.set(market.token_id_up, ask=1.00, ask_size=10, bid=0.99, bid_size=pos.tp_qty)
    engine.on_tick([market])

    row = logs["trade"].rows[-1]
    assert row["tp_actually_filled"] is True
    assert row["reached_99"] is True, \
        "the tick that filled our 99c sell is the tick that printed 99c"
    assert row["liquidity_at_99"] > 0


def test_trade_row_has_the_full_lifecycle(engine, feed, logs):
    market, pos = _open_position(engine, feed)
    feed.set(market.token_id_up, ask=1.00, ask_size=10, bid=0.99, bid_size=pos.tp_qty)
    engine.on_tick([market])

    assert len(logs["trade"].rows) == 1
    row = logs["trade"].rows[0]
    for field in (
        "trade_id", "candidate_id", "mode", "market_id", "asset", "side",
        "signal_timestamp", "entry_submitted_at", "entry_requested_price",
        "entry_requested_qty", "entry_fill_timestamp", "entry_fill_price",
        "entry_filled_qty", "entry_liquidity", "tp_submitted_at", "tp_price",
        "tp_qty", "tp_fill_timestamp", "tp_fill_price", "tp_filled_qty",
        "tp_liquidity", "stop_price", "exit_reason", "realized_pnl",
        "realized_pnl_pct", "fees_total", "max_price_after_entry",
        "min_price_after_entry", "reached_99", "tp_actually_filled",
        "underlying_price", "settlement_threshold", "distance_from_threshold",
    ):
        assert field in row, f"trade row is missing {field}"
    assert row["mode"] == "PAPER"
    assert row["tp_actually_filled"] is True
    assert row["entry_liquidity"] == "taker"
    assert row["tp_liquidity"] == "maker"


def test_rejected_candidates_are_recorded_with_a_reason(engine, feed, logs):
    market = market_ending_in(100)     # outside the 60s entry window
    prime(feed, market)

    engine.on_tick([market])
    assert engine.candidates_seen == 1
    assert not engine.positions

    # Window closes without an entry -> the decision is written once.
    market.end_time = datetime.now(timezone.utc) + timedelta(seconds=2)
    engine.on_tick([market])

    rows = logs["candidate"].rows
    assert len(rows) == 1
    row = rows[0]
    assert row["qualified"] is False
    assert row["traded"] is False
    assert "too_early" in row["reason_rejected"] or "too_late" in row["reason_rejected"]
    for field in ("market_id", "asset", "market_start_time", "market_end_time",
                  "secs_remaining", "side", "side_price", "opposite_price",
                  "bid", "ask", "spread", "bid_depth", "ask_depth",
                  "available_liquidity", "underlying_price",
                  "settlement_threshold", "distance_from_threshold"):
        assert field in row, f"candidate row is missing {field}"


def test_candidate_tracks_what_happened_after_the_trigger(engine, feed, logs):
    """The whole point: what a 90c contract did next, whether we traded it or not."""
    market = market_ending_in(100)     # deliberately not tradeable
    prime(feed, market)
    engine.on_tick([market])
    cand = engine.candidates[(market.market_id, "Up")]

    for bid in (0.93, 0.95, 0.97, 0.98, 0.99, 0.96):
        feed.set(market.token_id_up, ask=bid + 0.01, ask_size=100, bid=bid, bid_size=250)
        engine.on_tick([market])

    assert cand.max_price == 0.99
    assert "0.95" in cand.targets and "0.99" in cand.targets
    assert cand.targets["0.99"]["depth"] == 250

    # Time passes: the market closes. A candidate holds its own end time, so
    # both move together (they are the same instant in the real world).
    past = datetime.now(timezone.utc) - timedelta(seconds=60)
    market.end_time = past
    cand.end_time = past
    engine.on_tick([market])

    outcome = logs["outcome"].rows[-1]
    assert outcome["reached_99"] is True
    assert outcome["our_tp_filled"] is False, \
        "the market printed 99c but WE never had an order there"
    assert outcome["resolution"] == "Up"
    assert outcome["prediction_correct"] is True
    assert outcome["secs_to_99"] != ""


def test_unresolvable_market_still_writes_its_row(engine, feed, logs, monkeypatch):
    """A resolution lookup that never answers must not swallow the data."""
    market = market_ending_in(100)
    prime(feed, market)
    engine._resolve_fn = lambda mid: {"resolved": False}
    engine.on_tick([market])
    cand = engine.candidates[(market.market_id, "Up")]

    past = datetime.now(timezone.utc) - timedelta(seconds=60)
    market.end_time = past
    cand.end_time = past
    engine.on_tick([market])
    assert not logs["outcome"].rows, "still waiting on the result"

    # Give up after the maximum wait and record what we do know.
    monkeypatch.setattr(s9099, "RESOLVE_MAX_WAIT", 0.0)
    engine._resolution_cache.clear()
    engine.on_tick([market])
    assert len(logs["outcome"].rows) == 1
    assert logs["outcome"].rows[0]["resolution"] == "unknown"
    assert not engine._pending_resolution


# ══════════════════════════════════════════════════════════════════════
#  Safety rails
# ══════════════════════════════════════════════════════════════════════

def test_kill_switch_and_gates_block_new_entries(engine, feed):
    engine.kill_switch = True
    market = market_ending_in(30)
    prime(feed, market)
    engine.on_tick([market])
    assert not engine.positions
    assert not [c for c in engine.broker.calls if c[0] == "buy"]

    engine.kill_switch = False
    engine.auto_trade = False
    engine.on_tick([market])
    assert not engine.positions

    engine.auto_trade = True
    engine.api_errors = 99
    ok, reason = engine.check_risk_gates()
    assert not ok and "api_unhealthy" in reason

    engine.api_errors = 0
    engine.consecutive_losses = 5
    ok, reason = engine.check_risk_gates()
    assert not ok and "max_consecutive_losses" in reason

    engine.consecutive_losses = 0
    engine.min_balance = 10_000
    ok, reason = engine.check_risk_gates()
    assert not ok and "below_min_balance" in reason


def test_max_open_positions_is_enforced(engine, feed):
    engine.max_open_positions = 1
    m1, _ = _open_position(engine, feed, market_id="0xone")

    m2 = market_ending_in(30, "0xtwo")
    prime(feed, m2)
    engine.on_tick([m2])
    assert "0xtwo" not in engine.positions


def test_set_params_keeps_the_price_ladder_coherent(engine):
    engine.set_params({"entry_price_min": 0.94, "tp_price": 0.90, "stop_price": 0.96})
    assert engine.tp_price > engine.entry_price_min
    assert engine.stop_price < engine.entry_price_min

    engine.set_params({"entry_price_min": "nonsense", "bogus_key": 1})
    assert engine.entry_price_min == 0.94

    applied = engine.set_params({"max_secs_remaining": 30, "size_mode": "percent_bankroll"})
    assert applied["max_secs_remaining"] == 30.0
    assert engine.size_mode == "percent_bankroll"


def test_shutdown_cancels_working_entries_but_leaves_the_tp_resting(engine, feed):
    market, pos = _open_position(engine, feed)
    engine.broker.calls.clear()
    engine.shutdown()
    assert not [c for c in engine.broker.calls if c[0] == "cancel"], \
        "a resting TP protects shares we hold — never cancel it on shutdown"

    m2 = market_ending_in(30, "0xtwo")
    prime(feed, m2)
    engine.max_open_positions = 5
    engine.on_tick([m2])
    pos2 = engine.positions["0xtwo"]
    engine.broker.calls.clear()
    engine.shutdown()
    assert ("cancel", pos2.entry_order_id) in engine.broker.calls
