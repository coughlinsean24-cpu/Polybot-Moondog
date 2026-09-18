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

def test_tracking_window_cannot_be_smaller_than_the_entry_window(engine):
    """
    A crossing that never becomes a candidate can never be traded, so a
    tracking window below the entry window would silently cap entries.
    """
    engine.candidate_max_secs = 120
    engine.set_params({"max_secs_remaining": 150})
    assert engine.max_secs_remaining == 150
    assert engine.candidate_max_secs >= 150, "tracking must cover the entry window"

    # Raising tracking alone is fine — observing wider than we trade is the point.
    engine.set_params({"candidate_max_secs": 300})
    assert engine.candidate_max_secs == 300
    assert engine.max_secs_remaining == 150


def test_entries_are_possible_across_the_whole_window(engine, feed):
    """A 150s entry window really does take a crossing at 140s."""
    engine.set_params({"max_secs_remaining": 150, "min_secs_remaining": 10})
    market = market_ending_in(140)
    prime(feed, market)
    engine.on_tick([market])

    assert market.market_id in engine.positions, \
        "a crossing at 140s must be tradeable with a 150s window"
    pos = engine.positions[market.market_id]
    assert 130 < pos.signal_secs <= 150


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


def test_reference_margin_filter(engine):
    """
    The margin filter measures against the SETTLEMENT REFERENCE, and the
    distance handed to it is already signed for the side.
    """
    engine.min_margin_pct = 0.02   # 0.02% of 100k = $20
    base = dict(price=0.90, secs_remaining=30, spread=0.01, ask_depth=500,
                reference_price=100_000.0)

    ok, _ = engine.check_entry_conditions(favourable_distance=50.0, **base)
    assert ok

    ok, reason = engine.check_entry_conditions(favourable_distance=5.0, **base)
    assert not ok and "reference_margin" in reason

    # A side losing on the reference has a negative favourable distance.
    ok, reason = engine.check_entry_conditions(favourable_distance=-50.0, **base)
    assert not ok and "reference_margin" in reason

    # No reference at all is a rejection, never an assumed pass.
    ok, reason = engine.check_entry_conditions(
        price=0.90, secs_remaining=30, spread=0.01, ask_depth=500,
        favourable_distance=50.0, reference_price=0.0,
    )
    assert not ok and reason == "no_reference_price"

    # The older side-agnostic spelling still works for existing callers.
    ok, _ = engine.check_entry_conditions(
        price=0.90, secs_remaining=30, spread=0.01, ask_depth=500,
        signed_distance=50.0, underlying_price=100_000.0, side="Up",
    )
    assert ok
    ok, reason = engine.check_entry_conditions(
        price=0.90, secs_remaining=30, spread=0.01, ask_depth=500,
        signed_distance=50.0, underlying_price=100_000.0, side="Down",
    )
    assert not ok, "Down needs the reference BELOW the window open"


def test_margin_uses_the_window_twap_not_spot(engine, feed):
    """
    The market resolves on the TWAP over the window against the window's
    opening price. A price that ran up late has a much smaller TWAP margin
    than its spot margin — the old spot-vs-open reading flattered it.
    """
    from conftest import seed_reference
    market = market_ending_in(30)
    # Flat at 100000 for most of the window, then a late run to 100100.
    # (A price only earns TWAP weight for the time it actually holds, so the
    # spike has to persist for a tick to count at all.)
    seed_reference(engine, market, [100_000.0] * 8 + [100_100.0] * 2)
    window_start = market.end_time.timestamp() - 300
    stats = engine.settlement.window(market.asset, window_start)

    assert stats.open_price == 100_000.0
    assert stats.spot_distance == pytest.approx(100.0)
    assert 0 < stats.twap_distance < stats.spot_distance, \
        "the TWAP lags a late move; that is the number the market settles on"

    favourable, source, is_official = engine.settlement.distance_for_side(
        market.asset, window_start, "Up"
    )
    assert favourable == pytest.approx(stats.twap_distance)
    assert source == "binance_twap_proxy"
    assert is_official is False


# ══════════════════════════════════════════════════════════════════════
#  4. Position sizing
# ══════════════════════════════════════════════════════════════════════

def test_position_sizing_modes(engine):
    free = fees.Schedule(rate=0.0)
    engine.size_mode = "fixed_dollars"
    engine.fixed_dollars = 45.0
    engine.max_position_dollars = 1000
    assert engine.calculate_position_size(500, 0.90, schedule=free) == 50

    engine.size_mode = "percent_bankroll"
    engine.max_position_percent = 10
    assert engine.calculate_position_size(500, 0.90, schedule=free) == 55  # $50 / 0.90

    engine.max_position_percent = 100
    assert engine.calculate_position_size(500, 0.90, schedule=free) == 555

    # Never risks the whole account by accident: the dollar cap still binds.
    engine.max_position_dollars = 100
    assert engine.calculate_position_size(500, 0.90, schedule=free) == 111


def test_position_sizing_accounts_for_fees_and_depth(engine):
    engine.size_mode = "fixed_dollars"
    engine.fixed_dollars = 100.0
    engine.max_position_dollars = 1000
    crypto = fees.Schedule(rate=0.07, exponent=1.0, taker_only=True)

    no_fee = engine.calculate_position_size(500, 0.90, schedule=fees.Schedule(rate=0.0))
    with_fee = engine.calculate_position_size(500, 0.90, schedule=crypto)
    assert with_fee < no_fee, "the taker fee is part of the cost of a share"
    total = with_fee * 0.90 + fees.estimate_fee(with_fee, 0.90, True, crypto)
    assert total <= 100.0 + 1e-6, "sizing must not overspend the budget"

    # Depth caps the order so it is marketable in full.
    assert engine.calculate_position_size(
        500, 0.90, ask_depth=30, schedule=fees.Schedule(rate=0.0)) == 30

    # Below the exchange minimum is no trade at all.
    engine.fixed_dollars = 2.0
    assert engine.calculate_position_size(500, 0.90, schedule=crypto) == 0
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
#  Queue-aware take-profit — the market touching 99c is not a fill
# ══════════════════════════════════════════════════════════════════════

def _queued_position(engine, feed, queue_ahead=2000.0, secs=30):
    """Open a position whose take-profit joins a queue of `queue_ahead` shares."""
    book = {"bids": [{"price": 0.97, "size": 500}],
            "asks": [{"price": 0.99, "size": queue_ahead}]}
    engine._depth_fn = lambda _t: book
    engine.broker.book_fn = lambda _t: book
    engine._book_cache.clear()
    market = market_ending_in(secs)
    prime(feed, market)
    engine.on_tick([market])
    engine.on_tick([market])
    return market, engine.positions[market.market_id]


def test_tp_does_not_fill_just_because_the_market_reaches_99(engine, feed):
    """
    2,000 shares are already offered at 99c. The market printing 99c says
    nothing about whether OUR order — at the back of that queue — traded.
    """
    market, pos = _queued_position(engine, feed, queue_ahead=2000.0)
    assert pos.tp_queue_ahead == 2000.0
    assert pos.tp_level_size == 2000.0

    # The level trades, but nowhere near enough to reach us.
    feed.set(market.token_id_up, ask=0.99, ask_size=2000, bid=0.98, bid_size=400)
    feed.trade(market.token_id_up, price=0.99, size=300, side="BUY")
    engine.on_tick([market])

    assert pos.tp_filled_qty == 0, "300 of 2000 ahead of us is not our fill"
    assert pos.phase == Phase.TP_PENDING
    assert pos.tp_price_reached is True, "the PRICE was reached..."
    assert pos.tp_queue_adjusted_fill is False, "...but we were not filled"
    assert pos.tp_consumed == 300


def test_tp_fills_once_the_queue_is_eaten_through(engine, feed):
    market, pos = _queued_position(engine, feed, queue_ahead=2000.0)
    qty = pos.tp_qty

    feed.set(market.token_id_up, ask=0.99, ask_size=2000, bid=0.98, bid_size=400)
    feed.trade(market.token_id_up, price=0.99, size=2000 + qty, side="BUY")
    engine.on_tick([market])

    assert pos.tp_filled_qty == qty
    assert pos.tp_queue_adjusted_fill is True
    assert pos.tp_fill_evidence == "queue_consumed"
    assert pos.phase == Phase.CLOSED
    assert pos.exit_reason == "tp_filled"


def test_tp_partially_fills_as_the_queue_drains(engine, feed):
    market, pos = _queued_position(engine, feed, queue_ahead=1000.0)
    qty = pos.tp_qty
    assert qty > 4, "need a few shares for a meaningful partial"

    feed.set(market.token_id_up, ask=0.99, ask_size=1000, bid=0.98, bid_size=400)
    feed.trade(market.token_id_up, price=0.99, size=1000 + qty / 2, side="BUY")
    engine.on_tick([market])

    assert pos.tp_filled_qty == pytest.approx(qty / 2)
    assert pos.phase == Phase.TP_PENDING
    assert pos.open_qty == pytest.approx(qty / 2)


def test_only_volume_at_or_below_our_price_counts(engine, feed):
    """Trades at 0.95 do not consume a queue sitting at 0.99... but they do
    consume the cheaper offers that are ahead of us."""
    book = {"bids": [{"price": 0.94, "size": 500}],
            "asks": [{"price": 0.95, "size": 100}, {"price": 0.99, "size": 400}]}
    engine._depth_fn = lambda _t: book
    engine.broker.book_fn = lambda _t: book
    engine._book_cache.clear()
    market = market_ending_in(30)
    prime(feed, market)
    engine.on_tick([market])
    engine.on_tick([market])
    pos = engine.positions[market.market_id]
    assert pos.tp_queue_ahead == 500.0, "0.95s and 0.99s are all ahead of us"

    # A print ABOVE our price would be impossible while we rest, so only
    # at-or-below volume is counted here.
    feed.set(market.token_id_up, ask=0.99, ask_size=400, bid=0.98, bid_size=50)
    feed.trade(market.token_id_up, price=0.95, size=100, side="BUY")
    engine.on_tick([market])
    assert pos.tp_filled_qty == 0
    assert pos.tp_consumed == 100


def test_a_crossed_book_is_not_a_fill(engine, feed):
    """
    best_bid at our price while the asks are still sitting there is a crossed
    book — the two sides of the feed are out of step. Filling on that would
    be the old optimism wearing a disguise.
    """
    market, pos = _queued_position(engine, feed, queue_ahead=2000.0)
    assert pos.tp_liquidity == "maker", "we rested"

    # Bid claims 0.99 but 2,000 shares are still offered at 0.99.
    feed.set(market.token_id_up, ask=0.99, ask_size=2000, bid=0.99, bid_size=800)
    engine.on_tick([market])
    assert pos.tp_filled_qty == 0, "a crossed quote is stale data, not an execution"
    assert pos.phase == Phase.TP_PENDING
    assert pos.tp_price_reached is True, "it is still recorded as the price being reached"

    # Once the level really clears, the ask moves up and we fill.
    feed.set(market.token_id_up, ask=1.00, ask_size=500, bid=0.99, bid_size=800)
    engine.on_tick([market])
    assert pos.tp_filled_qty == pos.tp_qty
    assert pos.tp_fill_evidence == "level_cleared"


def test_strict_tape_mode_ignores_a_cleared_level(engine, feed, monkeypatch):
    """
    Most of the 99c level disappears by cancellation, not execution. The
    default infers a fill from a cleared level; strict mode demands prints.
    """
    monkeypatch.setattr(config, "S9099_REQUIRE_TAPE_EVIDENCE", True)
    market, pos = _queued_position(engine, feed, queue_ahead=3000.0)

    # The level vanishes with almost nothing printed.
    feed.set(market.token_id_up, ask=1.00, ask_size=100, bid=0.99, bid_size=900)
    feed.trade(market.token_id_up, price=0.99, size=40, side="BUY")
    engine.on_tick([market])
    assert pos.tp_filled_qty == 0, "strict mode wants the tape, not an inference"
    assert pos.tp_price_reached is True

    # Enough printed volume fills it in either mode.
    feed.trade(market.token_id_up, price=0.99, size=3000 + pos.tp_qty, side="BUY")
    engine.on_tick([market])
    assert pos.tp_filled_qty == pos.tp_qty
    assert pos.tp_fill_evidence == "queue_consumed"


def test_trade_through_counts_as_execution(engine, feed):
    """A print above our resting offer cannot happen unless we were taken."""
    market, pos = _queued_position(engine, feed, queue_ahead=5000.0)
    qty = pos.tp_qty

    feed.set(market.token_id_up, ask=1.00, ask_size=10, bid=0.98, bid_size=100)
    feed.trade(market.token_id_up, price=0.995, size=50, side="BUY")
    engine.on_tick([market])

    assert pos.tp_filled_qty == qty
    assert pos.tp_fill_evidence == "trade_through"


def test_queue_evidence_is_captured_at_submission(engine, feed, logs):
    market, pos = _queued_position(engine, feed, queue_ahead=3000.0, secs=45)
    assert pos.tp_qty > 0
    assert pos.tp_queue_ahead == 3000.0
    assert pos.tp_level_size == 3000.0
    assert pos.tp_submitted_at > 0
    assert 0 < pos.tp_secs_remaining_at_submit <= 45
    assert pos.tp_bid_depth_at_submit >= 0

    # Never filled: it closes at expiry, and the row says exactly why.
    feed.set(market.token_id_up, ask=0.99, ask_size=3000, bid=0.98, bid_size=200)
    feed.trade(market.token_id_up, price=0.99, size=500, side="BUY")
    engine.on_tick([market])
    market.end_time = datetime.now(timezone.utc) + timedelta(seconds=1)
    engine.on_tick([market])
    engine.on_tick([market])

    row = logs["trade"].rows[-1]
    assert row["tp_queue_ahead"] == 3000.0
    assert row["tp_level_size_at_submit"] == 3000.0
    assert row["tp_consumed_volume"] == 500.0
    assert row["tp_price_reached"] is True
    assert row["tp_queue_adjusted_fill"] is False
    assert row["tp_filled_qty"] == 0
    assert row["tp_secs_remaining_at_submit"] > 0


def test_optimistic_mode_still_available_for_comparison(engine, feed):
    """The old behaviour is switchable, so the two fill rates can be compared."""
    engine.broker.queue_aware = False
    market, pos = _queued_position(engine, feed, queue_ahead=5000.0)
    engine.broker.queue_aware = False

    feed.set(market.token_id_up, ask=0.99, ask_size=5000, bid=0.99, bid_size=pos.tp_qty)
    engine.on_tick([market])
    assert pos.tp_filled_qty == pos.tp_qty, \
        "optimistic mode fills on the price alone — which is the bias we are measuring"


def test_shadow_tp_records_both_answers_for_untraded_crossings(engine, feed, logs):
    """
    Candidates we never trade still answer the question both ways, which is
    what lets the dataset compare entry levels honestly.
    """
    book = {"bids": [{"price": 0.97, "size": 100}],
            "asks": [{"price": 0.99, "size": 4000}]}
    engine._depth_fn = lambda _t: book
    engine.broker.book_fn = lambda _t: book
    engine._book_cache.clear()
    engine.auto_trade = False           # observation only
    market = market_ending_in(100)
    prime(feed, market)
    engine.on_tick([market])

    cand = engine.candidates[(market.market_id, "Up", 0.90)]
    assert cand.shadow_queue_ahead == 4000.0
    assert cand.shadow_qty > 0

    # The price PRINTS at 99c, but only a trickle goes through it, and the
    # shadow order is behind 4,000 shares.
    feed.set(market.token_id_up, ask=0.99, ask_size=4000, bid=0.98, bid_size=200)
    feed.trade(market.token_id_up, price=0.99, size=100, side="BUY")
    engine.on_tick([market])

    assert cand.shadow_optimistic_filled is True, "the market printed our price"
    assert cand.shadow_queue_filled is False, "100 of 4000 ahead is not a fill"
    assert cand.shadow_consumed == 100.0

    past = datetime.now(timezone.utc) - timedelta(seconds=60)
    market.end_time = past
    for c in engine.candidates.values():
        c.end_time = past
    engine.on_tick([market])

    row = [r for r in logs["outcome"].rows if r["observe_level"] == 0.90][0]
    assert row["tp_price_reached"] is True
    assert row["tp_queue_adjusted_fill"] is False
    assert row["shadow_queue_ahead"] == 4000.0
    assert row["shadow_consumed"] == 100.0


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


def test_proxy_reference_crossing_does_not_force_an_exit(engine, feed):
    """
    Binance is a proxy: measured +7 to +9 bps off the Chainlink feed, on a
    market that settles on a TWAP. A proxy crossing is recorded, not acted on.
    """
    from conftest import seed_reference
    market, pos = _open_position(engine, feed)
    engine.stop_price = 0.10           # isolate the threshold rule
    engine.stop_velocity_drop = 0      # and the velocity rule
    seed_reference(engine, market, [100_000.0, 99_800.0, 99_700.0])

    feed.set(market.token_id_up, ask=0.88, ask_size=500, bid=0.87, bid_size=500)
    engine.on_tick([market])

    assert pos.stop_reason == "", "a proxy must not force an exit"
    assert pos.proxy_cross_seen is True, "but it must be recorded"
    assert pos.phase == Phase.TP_PENDING


def test_official_reference_crossing_does_force_an_exit(engine, feed, monkeypatch):
    """With the real reference available, a crossing is evidence, so we act."""
    from conftest import seed_reference
    import settlement_ref
    market, pos = _open_position(engine, feed)
    engine.stop_price = 0.10
    engine.stop_velocity_drop = 0
    seed_reference(engine, market, [100_000.0, 100_000.0])

    # The official stream says the reference is now BELOW the window open.
    monkeypatch.setattr(
        engine.settlement, "official_reading",
        lambda asset: settlement_ref.Reading(
            asset=asset, available=True, value=99_500.0,
            source="chainlink_twap_60s_stream", is_official=True,
        ),
    )
    feed.set(market.token_id_up, ask=0.88, ask_size=500, bid=0.87, bid_size=500)
    engine.on_tick([market])
    assert "reference_crossed_threshold" in pos.stop_reason


def test_proxy_crossing_can_be_opted_into(engine, feed):
    """The proxy stop is available, but only by explicit opt-in."""
    from conftest import seed_reference
    market, pos = _open_position(engine, feed)
    engine.stop_price = 0.10
    engine.stop_velocity_drop = 0
    engine.allow_proxy_threshold_stop = True
    seed_reference(engine, market, [100_000.0, 99_800.0, 99_700.0])

    feed.set(market.token_id_up, ask=0.88, ask_size=500, bid=0.87, bid_size=500)
    engine.on_tick([market])
    assert "proxy_crossed_threshold" in pos.stop_reason


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
    """A TP that rested and was later hit is a MAKER fill — it costs nothing."""
    market, pos = _open_position(engine, feed)
    qty = pos.tp_qty
    assert pos.tp_price > 0
    feed.set(market.token_id_up, ask=1.00, ask_size=10, bid=0.99, bid_size=qty)
    engine.on_tick([market])

    gross = qty * (pos.tp_fill_price - pos.entry_fill_price)
    assert pos.gross_pnl == pytest.approx(gross, abs=1e-6)
    assert pos.tp_fill_evidence == "level_cleared", \
        "the 99c asks went away and demand was still there"
    assert pos.tp_liquidity == "maker"
    assert pos.tp_fee_actual == 0.0, "we rested; being hit does not make us the taker"
    assert pos.fees_total == pytest.approx(pos.entry_fee_actual, abs=1e-9)
    assert pos.realized_pnl == pytest.approx(gross - pos.fees_total, abs=1e-4)
    assert pos.realized_pnl < pos.gross_pnl
    assert pos.realized_pnl_pct == pytest.approx(
        pos.realized_pnl / (qty * pos.entry_fill_price) * 100, abs=1e-3
    )
    assert engine.realized_pnl == pytest.approx(pos.realized_pnl, abs=1e-6)


def test_a_tp_that_crosses_on_submission_is_a_taker_fill(engine, feed):
    """Placing into a bid that is already at our price makes us the taker."""
    market = market_ending_in(30)
    prime(feed, market)
    engine.on_tick([market])                      # entry submitted
    # By the time the entry fills, the bid is already up at the TP price.
    feed.set(market.token_id_up, ask=0.90, ask_size=500, bid=0.99, bid_size=500)
    engine.on_tick([market])                      # fill + TP, crossing
    pos = engine.positions.get(market.market_id) or engine.closed[-1]

    assert pos.tp_liquidity == "taker"
    assert pos.tp_fee_actual > 0
    assert pos.tp_fee_actual == pytest.approx(
        fees.estimate_fee(pos.tp_filled_qty, pos.tp_price, True,
                          fees.Schedule(rate=0.07)), abs=1e-6)


def test_a_rested_tp_filled_by_the_tape_pays_no_fee(engine, feed):
    """A genuine maker fill — queue eaten by trade prints — is free."""
    market, pos = _open_position(engine, feed)
    qty = pos.tp_qty
    # No bid at our price: we rest. Then the tape eats through our level.
    feed.set(market.token_id_up, ask=0.99, ask_size=qty, bid=0.97, bid_size=200)
    feed.trade(market.token_id_up, price=0.99, size=qty, side="BUY")
    engine.on_tick([market])

    assert pos.phase == Phase.CLOSED
    assert pos.tp_fill_evidence == "queue_consumed"
    assert pos.tp_fee_actual == 0.0, "a resting maker fill costs nothing"
    assert pos.fees_total == pytest.approx(pos.entry_fee_actual, abs=1e-9)
    assert pos.realized_pnl > 0


def test_loss_counters_and_daily_loss(engine, feed):
    market, pos = _open_position(engine, feed)
    feed.set(market.token_id_up, ask=0.81, ask_size=500, bid=0.78, bid_size=500)
    engine.on_tick([market])
    engine.on_tick([market])

    assert pos.realized_pnl < 0
    assert engine.losses == 1
    assert engine.daily_loss == pytest.approx(-pos.realized_pnl, abs=1e-6)

    engine.paper_auto_reset = False    # testing the brake, not the recovery
    engine.max_daily_loss = 0.01
    ok, reason = engine.check_risk_gates()
    assert not ok and "max_daily_loss" in reason


# ══════════════════════════════════════════════════════════════════════
#  19. Fees
# ══════════════════════════════════════════════════════════════════════

def test_economic_fee_formula_matches_the_published_schedule():
    """
    fee = size * rate * (p * (1 - p)) ** exponent, USDC, TAKERS ONLY.

    Every 5-minute crypto market reports feeSchedule
    {rate: 0.07, exponent: 1, takerOnly: true} — the same 0.07 as the
    published crypto category rate.
    """
    crypto = fees.Schedule(rate=0.07, exponent=1.0, taker_only=True)

    assert fees.estimate_fee(100, 0.50, True, crypto) == pytest.approx(1.75)
    assert fees.estimate_fee(100, 0.90, True, crypto) == pytest.approx(0.63)
    assert fees.estimate_fee(100, 0.10, True, crypto) == pytest.approx(0.63), \
        "symmetric around 50c"
    assert fees.estimate_fee(100, 0.99, True, crypto) == pytest.approx(0.0693)
    assert fees.estimate_fee(556, 0.90, True, crypto) == pytest.approx(3.5028, abs=1e-3)

    # Makers are not charged.
    assert fees.estimate_fee(556, 0.99, False, crypto) == 0.0
    assert fees.maker_fee(100, 0.90, crypto) == 0.0
    assert fees.taker_fee(100, 0.90, crypto) == pytest.approx(0.63)

    # Degenerate inputs cost nothing rather than raising.
    assert fees.estimate_fee(0, 0.90, True, crypto) == 0.0
    assert fees.estimate_fee(100, 1.0, True, crypto) == 0.0
    assert fees.estimate_fee(0.0001, 0.99, True, crypto) == 0.0


def test_signing_parameter_is_not_the_economic_rate():
    """
    The /fee-rate base_fee (1000 bps) is the order's feeRateBps — a ceiling
    carried in the signature. Reading it as an economic 0.10 rate overstated
    every fee by ~43%.
    """
    signing_as_rate = 1000 / 10_000.0
    economic = fees.default_schedule()
    assert economic.rate == pytest.approx(0.07)
    assert signing_as_rate > economic.rate

    wrong = 27 * signing_as_rate * 0.9 * 0.1
    right = fees.estimate_fee(27, 0.90, True, economic)
    assert wrong == pytest.approx(0.243, abs=1e-3)
    assert right == pytest.approx(0.1701, abs=1e-4)
    assert right < wrong

    # The giveaway: the MAKER side reports the same 1000 bps on a market
    # whose schedule charges makers nothing.
    assert economic.taker_only is True
    assert fees.estimate_fee(27, 0.99, False, economic) == 0.0


def test_fee_schedule_is_read_live_and_cached(monkeypatch):
    fees.clear_cache()
    calls = []

    class Resp:
        status_code = 200

        @staticmethod
        def json():
            return [{"feeSchedule": {"rate": 0.07, "exponent": 1,
                                     "takerOnly": True, "rebateRate": 0.2},
                     "feesEnabled": True}]

    def fake_get(url, params=None, timeout=None):
        calls.append(params)
        return Resp()

    monkeypatch.setattr(fees.requests, "get", fake_get)
    sched = fees.schedule_for_market(condition_id="0xabc")
    assert sched.rate == pytest.approx(0.07)
    assert sched.taker_only is True
    assert sched.source == "gamma"
    fees.schedule_for_market(condition_id="0xabc")
    assert len(calls) == 1, "the schedule should be cached per market"

    # A dead endpoint falls back to the configured default, never to zero.
    fees.clear_cache()
    monkeypatch.setattr(fees.requests, "get",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    fallback = fees.schedule_for_market(condition_id="0xdead")
    assert fallback.rate == fees.default_schedule().rate > 0
    assert fallback.source == "default"


def test_fees_disabled_market_is_respected(monkeypatch):
    fees.clear_cache()

    class Resp:
        status_code = 200

        @staticmethod
        def json():
            return [{"feesEnabled": False, "feeSchedule": {"rate": 0.07}}]

    monkeypatch.setattr(fees.requests, "get", lambda *a, **k: Resp())
    sched = fees.schedule_for_market(condition_id="0xfree")
    assert sched.rate == 0.0
    assert fees.estimate_fee(100, 0.90, True, sched) == 0.0


def test_actual_fee_overrides_the_estimate(monkeypatch):
    """Whatever the exchange says it charged wins over any formula."""
    monkeypatch.setattr(config, "PAPER_TRADING", False)
    monkeypatch.setattr(config, "S9099_USE_ACTUAL_FEES", True)

    fee, source = fees.actual_fee_for_order(
        "abc", trades_fn=lambda oid: [{"fee": "0.21"}, {"fee": 0.04}]
    )
    assert fee == pytest.approx(0.25)
    assert source == "clob_trades"

    # No fee field, no trades, or paper mode -> no actual number, and a reason.
    fee, source = fees.actual_fee_for_order("abc", trades_fn=lambda oid: [{"price": 0.9}])
    assert fee is None and source == "no_fee_field_in_trades"
    fee, source = fees.actual_fee_for_order("abc", trades_fn=lambda oid: [])
    assert fee is None and source == "no_trades_reported"
    monkeypatch.setattr(config, "PAPER_TRADING", True)
    fee, source = fees.actual_fee_for_order("abc", trades_fn=lambda oid: [{"fee": 1}])
    assert fee is None and source == "paper_mode"


def test_fee_columns_separate_raw_economic_estimated_and_actual(engine, feed, logs):
    market, pos = _open_position(engine, feed)
    feed.set(market.token_id_up, ask=1.00, ask_size=10, bid=0.99, bid_size=pos.tp_qty)
    engine.on_tick([market])

    row = logs["trade"].rows[-1]
    assert row["fee_rate_raw"] > 0, "the signing parameter is recorded..."
    assert row["economic_fee_rate"] == pytest.approx(0.07), "...separately from the real rate"
    assert row["fee_rate_raw"] / 10_000.0 != row["economic_fee_rate"]
    assert row["entry_fee_estimated"] > 0
    assert row["entry_fee_actual"] > 0
    assert row["entry_fee_source"] == "estimate", "paper mode has nothing to read back"
    assert row["fee_taker_only"] is True
    assert row["fee_exponent"] == 1.0
    assert "fee_schedule_source" in row


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
    engine.max_secs_remaining = 60     # explicit: this test is about rejections
    market = market_ending_in(100)     # outside that entry window
    prime(feed, market)

    engine.on_tick([market])
    # One record per observation threshold the price has crossed.
    crossed = [lv for lv in engine.observe_thresholds if lv <= 0.90]
    assert engine.candidates_seen == len(crossed)
    assert not engine.positions

    # Window closes without an entry -> the tradeable level writes its verdict.
    market.end_time = datetime.now(timezone.utc) + timedelta(seconds=2)
    engine.on_tick([market])

    rows = [r for r in logs["candidate"].rows if r["observe_level"] == engine.entry_price_min]
    assert len(rows) == 1
    row = rows[0]
    assert row["qualified"] is False
    assert row["traded"] is False
    assert "too_early" in row["reason_rejected"] or "too_late" in row["reason_rejected"]
    for field in ("market_id", "asset", "market_start_time", "market_end_time",
                  "secs_remaining", "side", "observe_level", "side_price",
                  "opposite_price", "bid", "ask", "spread", "bid_depth",
                  "ask_depth", "available_liquidity", "binance_price",
                  "binance_window_twap", "official_reference_available",
                  "official_reference_source", "distance_is_official"):
        assert field in row, f"candidate row is missing {field}"
    # The official reference is unavailable here, and says so rather than
    # quietly substituting Binance.
    assert row["official_reference_available"] is False
    assert row["official_resolution_reference"] == ""
    assert row["distance_is_official"] is False
    assert row["distance_source"] == "binance_twap_proxy"


def test_candidate_tracks_what_happened_after_the_trigger(engine, feed, logs):
    """The whole point: what a 90c contract did next, whether we traded it or not."""
    engine.max_secs_remaining = 60     # keep this one purely observational
    market = market_ending_in(100)     # deliberately outside the entry window
    prime(feed, market)
    engine.on_tick([market])
    cand = engine.candidates[(market.market_id, "Up", 0.90)]

    for bid in (0.93, 0.95, 0.97, 0.98, 0.99, 0.96):
        feed.set(market.token_id_up, ask=bid + 0.01, ask_size=100, bid=bid, bid_size=250)
        engine.on_tick([market])

    assert cand.max_price == 0.99
    assert "0.95" in cand.targets and "0.99" in cand.targets
    assert cand.targets["0.99"]["depth"] == 250

    past = datetime.now(timezone.utc) - timedelta(seconds=60)
    market.end_time = past
    for c in engine.candidates.values():
        c.end_time = past
    engine.on_tick([market])

    rows = {r["observe_level"]: r for r in logs["outcome"].rows}
    assert 0.90 in rows, "the traded level must be recorded"
    outcome = rows[0.90]
    assert outcome["reached_99"] is True
    assert outcome["our_tp_filled"] is False, \
        "the market printed 99c but WE never had an order there"
    assert outcome["resolution"] == "Up"
    assert outcome["prediction_correct"] is True
    assert outcome["secs_to_99"] != ""


def test_every_observed_threshold_gets_its_own_record(engine, feed, logs):
    """
    85 / 88 / 90 / 92 / 95 are measured independently, each from its own
    moment, so the data can say which entry level is actually best.
    """
    engine.auto_trade = False          # observation only
    market = market_ending_in(100)
    feed.set(market.token_id_down, ask=0.11, ask_size=500, bid=0.10, bid_size=500)

    # A side walking up through every threshold in turn.
    for ask in (0.86, 0.89, 0.91, 0.93, 0.96):
        feed.set(market.token_id_up, ask=ask, ask_size=500, bid=ask - 0.01, bid_size=500)
        engine.on_tick([market])

    levels = sorted(lv for (_m, side, lv) in engine.candidates if side == "Up")
    assert levels == [0.85, 0.88, 0.90, 0.92, 0.95]

    # Each records the seconds remaining and price at ITS OWN crossing.
    triggers = {lv: engine.candidates[(market.market_id, "Up", lv)].trigger_price
                for lv in levels}
    assert triggers[0.85] == 0.86 and triggers[0.95] == 0.96
    assert triggers[0.85] < triggers[0.95]

    # Only the configured entry level is allowed to trade.
    assert engine.entry_price_min == 0.90
    for lv in levels:
        cand = engine.candidates[(market.market_id, "Up", lv)]
        assert cand.traded is False

    past = datetime.now(timezone.utc) - timedelta(seconds=60)
    market.end_time = past
    for c in engine.candidates.values():
        c.end_time = past
    engine.on_tick([market])
    written = {r["observe_level"] for r in logs["outcome"].rows}
    assert written == set(levels), "every level writes its own outcome row"


def test_unresolvable_market_still_writes_its_row(engine, feed, logs, monkeypatch):
    """A resolution lookup that never answers must not swallow the data."""
    market = market_ending_in(100)
    prime(feed, market)
    engine._resolve_fn = lambda mid: {"resolved": False}
    engine.on_tick([market])

    past = datetime.now(timezone.utc) - timedelta(seconds=60)
    market.end_time = past
    for c in engine.candidates.values():
        c.end_time = past
    engine.on_tick([market])
    assert not logs["outcome"].rows, "still waiting on the result"

    # Give up after the maximum wait and record what we do know.
    monkeypatch.setattr(s9099, "RESOLVE_MAX_WAIT", 0.0)
    engine._resolution_cache.clear()
    engine.on_tick([market])
    assert logs["outcome"].rows
    assert all(r["resolution"] == "unknown" for r in logs["outcome"].rows)
    assert not engine._pending_resolution


# ══════════════════════════════════════════════════════════════════════
#  Safety rails
# ══════════════════════════════════════════════════════════════════════

def test_all_blocking_reasons_are_recorded_not_just_the_last(engine, feed, logs):
    """
    A candidate is re-evaluated every tick and the price usually leaves the
    band before the window shuts, so the LAST reason is nearly always
    price_below_threshold — which hid the real blocker.
    """
    engine.min_liquidity = 400          # the real blocker
    market = market_ending_in(100)
    prime(feed, market, ask=0.90, ask_size=100, bid=0.89)

    engine.on_tick([market])            # in the band, but the book is thin
    cand = engine.candidates[(market.market_id, "Up", 0.90)]
    assert "thin_book" in cand.reason_first

    # Price drops out of the band: the decision closes on price, as before.
    feed.set(market.token_id_up, ask=0.85, ask_size=100, bid=0.84, bid_size=500)
    engine.on_tick([market])

    row = [r for r in logs["candidate"].rows if r["observe_level"] == 0.90][0]
    assert "price_below_threshold" in row["reason_rejected"], "last reason, as before"
    assert "thin_book" in row["reason_first_block"], "...but the real one is kept"
    assert "thin_book" in row["reasons_all"]
    assert "thin_book" in engine.stats()["block_reasons"]


def test_not_trading_because_names_blanket_blockers(engine):
    """A market can read QUALIFIES while one of these silently stops everything."""
    assert engine._not_trading_because() == []

    engine.auto_trade = False
    assert any("Trade candidates" in r for r in engine._not_trading_because())

    engine.auto_trade = True
    engine.kill_switch = True
    assert any("kill switch" in r for r in engine._not_trading_because())

    engine.kill_switch = False
    engine.entry_price_min = 87.0       # the cents mistake
    assert any("not a per-share price" in r for r in engine._not_trading_because())

    engine.entry_price_min = 0.90
    engine.paper_auto_reset = False    # testing the brake, not the recovery
    engine.min_balance = 10_000
    assert any("below minimum" in r for r in engine._not_trading_because())

    # A dead underlying feed blocks every entry while a margin is required.
    engine.min_balance = 0
    engine.min_margin_pct = 0.02
    engine.settlement.binance_feed = None
    assert any("reference price" in r for r in engine._not_trading_because())
    assert engine.stats()["not_trading_because"]


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
    engine.paper_auto_reset = False    # testing the brake, not the recovery
    engine.consecutive_losses = 5
    ok, reason = engine.check_risk_gates()
    assert not ok and "max_consecutive_losses" in reason

    engine.consecutive_losses = 0
    engine.min_balance = 10_000
    ok, reason = engine.check_risk_gates()
    assert not ok and "below_min_balance" in reason


def test_paper_session_recovers_from_a_wipeout(engine, feed):
    """
    Paper is for collecting data. A drained bankroll or a tripped loss brake
    used to end the session silently — and the consecutive-loss brake is a
    genuine dead end, since it only clears on a win that can no longer happen.
    """
    engine.broker.balance = 1.0          # wiped out
    engine.consecutive_losses = 9
    engine.daily_loss = 9_999.0
    engine.realized_pnl = -512.34        # the record so far
    engine.wins, engine.losses = 3, 12
    engine._balance_cache = (1.0, 0.0)

    ok, reason = engine.check_risk_gates()
    assert ok, f"paper should have recovered, got: {reason}"
    assert engine.paper_resets == 1
    assert engine.available_balance() == 500.0
    assert engine.consecutive_losses == 0
    assert engine.daily_loss == 0.0

    # The record is kept — the reset count is itself the finding.
    assert engine.realized_pnl == pytest.approx(-512.34)
    assert (engine.wins, engine.losses) == (3, 12)
    assert engine.stats()["paper_resets"] == 1


def test_recovery_never_happens_mid_position(engine, feed):
    """Refilling while a position is working would corrupt its accounting."""
    market, pos = _open_position(engine, feed)
    engine.broker.balance = 1.0
    engine._balance_cache = (1.0, 0.0)

    engine.check_risk_gates()
    assert engine.paper_resets == 0, "not while shares are still held"

    # Once it closes, the next check recovers.
    feed.set(market.token_id_up, ask=1.00, ask_size=10, bid=0.99, bid_size=pos.tp_qty)
    engine.on_tick([market])
    assert pos.phase == Phase.CLOSED
    engine.broker.balance = 1.0
    engine._balance_cache = (1.0, 0.0)
    engine.check_risk_gates()
    assert engine.paper_resets == 1


def test_live_mode_never_refills_itself(engine, monkeypatch):
    """There is no topping up a real account."""
    engine.broker.balance = 1.0
    engine._balance_cache = (1.0, 0.0)
    monkeypatch.setattr(type(engine.broker), "name", "LIVE")
    assert engine.is_live is True
    assert engine._paper_recover() is False
    assert engine.paper_resets == 0

    # And with the switch off, paper behaves like live.
    monkeypatch.setattr(type(engine.broker), "name", "PAPER")
    engine.paper_auto_reset = False
    assert engine._paper_recover() is False
    ok, reason = engine.check_risk_gates()
    assert not ok and "below_min_balance" in reason


def test_closed_trades_are_reported_for_the_dashboard(engine, feed):
    """A finished trade has to be visible somewhere, win or lose."""
    assert engine.stats()["closed_trades"] == []

    # A take-profit that fills.
    m1 = market_ending_in(60, "0xwin")
    prime(feed, m1)
    engine.on_tick([m1])
    engine.on_tick([m1])
    won = engine.positions["0xwin"]
    feed.set(m1.token_id_up, ask=1.00, ask_size=10, bid=0.99, bid_size=won.tp_qty)
    engine.on_tick([m1])

    # A stop-out.
    m2 = market_ending_in(60, "0xloss")
    prime(feed, m2)
    engine.on_tick([m2])
    engine.on_tick([m2])
    feed.set(m2.token_id_up, ask=0.81, ask_size=500, bid=0.78, bid_size=500)
    engine.on_tick([m2])
    engine.on_tick([m2])

    trades = engine.stats()["closed_trades"]
    assert len(trades) == 2
    assert engine.stats()["closed_count"] == 2
    assert trades[0]["trade_id"] != trades[1]["trade_id"]
    # Newest first.
    assert trades[0]["closed_at"] >= trades[1]["closed_at"]

    loss = [t for t in trades if t["pnl"] < 0][0]
    win = [t for t in trades if t["pnl"] > 0][0]
    assert win["exit_reason"] == "tp_filled"
    assert win["tp_evidence"], "the dashboard shows WHY we believe it filled"
    assert win["exit_price"] == pytest.approx(engine.tp_price)
    assert "stop_price" in loss["exit_reason"]
    assert loss["fees"] > 0
    for field in ("closed_hhmm", "asset", "side", "shares", "entry_price",
                  "exit_price", "tp_queue_ahead", "hold_secs", "pnl_pct", "mode"):
        assert field in win, f"closed trade row is missing {field}"


def test_an_unfilled_entry_still_appears(engine, feed):
    """An entry that never filled is a result, and its absence was confusing."""
    engine.entry_timeout = 0.0
    market = market_ending_in(60)
    prime(feed, market)
    engine.on_tick([market])
    feed.set(market.token_id_up, ask=0.95, ask_size=500, bid=0.94, bid_size=500)
    engine.on_tick([market])

    trades = engine.stats()["closed_trades"]
    assert len(trades) == 1
    assert trades[0]["exit_reason"] == "entry_never_filled"
    assert trades[0]["shares"] == 0
    assert trades[0]["pnl"] == 0


def test_max_open_positions_is_enforced(engine, feed):
    engine.max_open_positions = 1
    m1, _ = _open_position(engine, feed, market_id="0xone")

    m2 = market_ending_in(30, "0xtwo")
    prime(feed, m2)
    engine.on_tick([m2])
    assert "0xtwo" not in engine.positions


def test_prices_must_be_per_share_not_cents(engine):
    """
    Typing 87 for 87 cents puts the entry threshold at $87 — unreachable, so
    the strategy would go silently dead. It is rejected with a reason instead.
    """
    before = engine.entry_price_min
    applied = engine.set_params({"entry_price_min": 87, "entry_price_max": 97,
                                 "tp_price": 99})
    assert applied == {}, "none of those are per-share prices"
    assert engine.entry_price_min == before, "the old value stands"
    assert len(engine.last_param_errors) == 3
    assert "between 0 and 1" in engine.last_param_errors[0]
    assert engine.stats()["param_errors"], "the UI can see what was wrong"

    # The decimal form is accepted.
    applied = engine.set_params({"entry_price_min": 0.87})
    assert applied["entry_price_min"] == 0.87
    assert engine.last_param_errors == []

    # So are the other ends of the range.
    assert engine.set_params({"stop_price": 0}) == {}
    assert engine.set_params({"tp_price": 1.0}) == {}


def test_changing_the_entry_level_keeps_it_tradeable(engine, feed):
    """
    Only crossings recorded AT the entry threshold can be traded. Moving the
    entry price to a level we were not recording used to kill trading with no
    error and no rejection reason.
    """
    engine.set_params({"entry_price_min": 0.91})
    assert 0.91 in engine.observe_thresholds, \
        "the entry level must be one of the levels we record"

    market = market_ending_in(30)
    prime(feed, market, ask=0.91, ask_size=500, bid=0.90)
    engine.on_tick([market])

    assert (market.market_id, "Up", 0.91) in engine.candidates
    assert market.market_id in engine.positions, "a 0.91 entry must actually trade"

    # The standard observation levels are still recorded alongside it.
    for level in (0.85, 0.88, 0.90):
        assert level in engine.observe_thresholds


def test_assets_can_be_narrowed_to_one_market(engine, feed):
    """Focusing on BTC alone should be a parameter, not a restart."""
    applied = engine.set_params({"assets": "BTC"})
    assert applied["assets"] == ["BTC"]

    btc = market_ending_in(30, "0xbtc")
    btc.asset = "BTC"
    eth = market_ending_in(30, "0xeth")
    eth.asset = "ETH"
    prime(feed, btc)
    prime(feed, eth)

    engine.on_tick([btc, eth])
    assert "0xbtc" in engine.positions
    assert "0xeth" not in engine.positions
    assert not any(key[0] == "0xeth" for key in engine.candidates), \
        "a filtered-out asset is not even recorded"

    # Case and spacing are forgiving; nonsense is refused with a reason.
    assert engine.set_params({"assets": " btc , eth "})["assets"] == ["BTC", "ETH"]
    assert engine.set_params({"assets": "DOGE"}) == {}
    assert "unknown asset" in engine.last_param_errors[0]


def test_size_preview_names_the_binding_cap(engine):
    """Three caps can bind; when the smallest is not the one you changed,
    the size does not move and it looks broken."""
    engine.size_mode = "percent_bankroll"
    engine.max_position_percent = 100
    engine.max_position_dollars = 100        # the real limit
    preview = engine.explain_size(price=0.90)
    assert "max $100.00/position" in preview["binding_cap"]
    assert preview["shares"] == engine.calculate_position_size(
        engine.available_balance(), 0.90)

    # Lift the per-position cap and the bankroll percentage takes over.
    engine.max_position_dollars = 1000
    preview = engine.explain_size(price=0.90)
    assert "100% of" in preview["binding_cap"]
    assert preview["cost"] > 400, "the full bankroll is now usable"

    # Too small to trade says so rather than reporting a silent zero.
    engine.size_mode = "fixed_dollars"
    engine.fixed_dollars = 1.0
    preview = engine.explain_size(price=0.90)
    assert preview["shares"] == 0
    assert "no trade" in preview["note"]


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
