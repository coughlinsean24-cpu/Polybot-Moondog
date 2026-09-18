"""How the two stops divide the work.

There are two exits on the way down and they are checked in order: the hard
price stop first, then the velocity rule (a drop of stop_velocity_drop from
the bid's high over stop_velocity_window seconds).

Because the price stop is checked first, a gap that skips past it was being
labelled stop_price even though the velocity rule would have exited on the
very same tick at the very same bid. Lowering the price stop does not change
that exit — it changes which rule takes the credit, and reserves the price
stop for a collapse the velocity rule sleeps through.
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import strategy_9099 as s9099  # noqa: E402
from test_strategy_9099 import _open_position  # noqa: E402

Phase = s9099.Phase


@pytest.fixture
def eng(engine):
    engine.stop_velocity_drop = 0.05
    engine.stop_velocity_window = 5.0
    return engine


def _gap_to(eng, market, bid):
    eng.feed.set(market.token_id_up, ask=round(bid + 0.01, 3), ask_size=500,
                 bid=bid, bid_size=500)
    eng.on_tick([market])


def test_a_gap_exits_at_the_same_bid_whichever_stop_is_set(eng):
    """
    The claim behind lowering the stop: on a gap, the exit price is the same
    either way. Only the reason string changes.
    """
    results = {}
    for stop in (0.80, 0.60):
        eng.positions.clear()
        eng.cooldowns.clear()
        eng._px_hist.clear()
        eng.stop_price = stop
        market, pos = _open_position(eng, eng.feed, secs=60,
                                     market_id=f"0xgap{int(stop * 100)}")
        assert pos.stop_price == stop, "stop is snapshotted at entry"

        _gap_to(eng, market, 0.73)          # 0.92 -> 0.73 in one tick
        assert pos.stop_reason, f"no exit at all with stop {stop}"
        results[stop] = (pos.stop_reason, pos.exit_price)

    assert "stop_price" in results[0.80][0]
    assert "adverse_velocity" in results[0.60][0], (
        "the velocity rule has to be the one that catches it"
    )
    assert results[0.80][1] == results[0.60][1], (
        f"the exit price moved: {results[0.80][1]} vs {results[0.60][1]}"
    )


def test_a_slow_grind_is_what_the_lower_stop_actually_costs(eng):
    """
    The honest downside, and the shape of decline it takes to get there.

    The velocity rule only sleeps through a slide slower than
    stop_velocity_drop per stop_velocity_window — under 1c/second at the
    current settings. Anything that slow rides past 0.80 to the new stop,
    which is a bigger loss than before. Anything faster is caught by the
    velocity rule long before either price stop matters.

    The clock is driven here rather than left to wall time: without that the
    whole grind lands inside one 5s window and reads as a single fast drop,
    which is the opposite of the case under test.
    """
    eng.stop_price = 0.60
    market, pos = _open_position(eng, eng.feed, secs=140)   # inside the 150s entry window

    # Take the clock over only once the position is open, seeded from real
    # time so the price history recorded during entry stays coherent with it.
    now = [time.time()]
    eng.clock = lambda: now[0]

    # 2c every 10s — a fifth of the speed the velocity rule reacts to.
    bid = 0.90
    while bid > 0.62 and not pos.stop_reason:
        now[0] += 10.0
        bid = round(bid - 0.02, 2)
        _gap_to(eng, market, bid)

    assert not pos.stop_reason, (
        f"2c per 10s should not trip a 5c-in-5s rule (stopped at {bid})"
    )
    assert bid < 0.80, "it rode straight past where the old stop would have been"

    now[0] += 10.0
    _gap_to(eng, market, 0.59)
    assert "stop_price" in pos.stop_reason
    assert pos.phase in (Phase.EXIT_PENDING, Phase.CLOSED)


def test_the_velocity_rule_still_catches_an_ordinary_fast_move(eng):
    """It is the working stop; the price stop is only the backstop."""
    eng.stop_price = 0.60
    market, pos = _open_position(eng, eng.feed, secs=60)

    _gap_to(eng, market, 0.84)      # 0.90 -> 0.84, a 6c drop
    assert "adverse_velocity" in pos.stop_reason
    assert pos.phase in (Phase.EXIT_PENDING, Phase.CLOSED)


def test_the_stop_a_position_was_opened_with_is_the_one_it_keeps(eng):
    """Changing the box mid-flight must not move an open position's stop."""
    eng.stop_price = 0.80
    market, pos = _open_position(eng, eng.feed, secs=120)
    assert pos.stop_price == 0.80

    eng.stop_price = 0.60
    assert pos.stop_price == 0.80, "an open position kept its entry-time stop"
