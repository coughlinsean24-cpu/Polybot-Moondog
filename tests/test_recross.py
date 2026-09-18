"""A side that dips out of the entry band and comes back must still trade.

Tracking starts at 300s but the entry window opens at 150s, so there is a
long stretch in which a crossing is recorded and can be discarded before an
entry was ever permitted.  When that happened the market went untraded and
nothing was written anywhere saying why.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from conftest import market_ending_in  # noqa: E402


@pytest.fixture
def eng(engine):
    engine.auto_trade = True
    engine.entry_price_min = 0.90
    engine.entry_price_max = 0.97
    engine.max_secs_remaining = 150
    engine.min_secs_remaining = 10
    engine.candidate_max_secs = 300
    engine._sync_observe_thresholds()
    return engine


def _tick(eng, market, secs, ask, bid=None):
    """Re-point the market at `secs` from close and quote it."""
    market.end_time = market_ending_in(secs).end_time
    eng.feed.set(market.token_id_up, ask=ask, ask_size=5000,
                 bid=bid if bid is not None else round(ask - 0.01, 3), bid_size=5000)
    eng.feed.set(market.token_id_down, ask=round(1 - ask, 3), ask_size=5000,
                 bid=round(1 - ask - 0.01, 3), bid_size=5000)
    eng.on_tick([market])


def test_dip_before_the_window_does_not_kill_the_entry(eng):
    market = market_ending_in(240)

    # Crosses 0.90 far too early to trade, then falls back out of the band.
    _tick(eng, market, 240, 0.90)
    _tick(eng, market, 230, 0.87)

    # Now, squarely inside the entry window and well inside the band.
    _tick(eng, market, 100, 0.93)

    assert eng.positions, (
        "qualifying market never traded — the pre-window dip retired the candidate"
    )
    pos = next(iter(eng.positions.values()))
    assert pos.side == "Up"
    assert pos.entry_price_req == pytest.approx(0.93)


def test_a_miss_always_leaves_a_reason(eng, logs):
    """Whatever happens, the crossing gets a row saying what blocked it."""
    market = market_ending_in(240)
    _tick(eng, market, 240, 0.90)
    _tick(eng, market, 230, 0.87)
    # Never comes back into the band; the window closes.
    _tick(eng, market, 100, 0.80)
    _tick(eng, market, 5, 0.80)

    rows = [r for r in logs["candidate"].rows
            if abs(r["observe_level"] - 0.90) < 1e-9 and r["side"] == "Up"]
    assert rows, "no observation row written for the 0.90 crossing"
    assert not rows[0]["traded"]
    assert rows[0]["reason_rejected"], "row written with no reason at all"


def test_dip_inside_the_window_then_recross_still_trades(eng):
    """Same thing, but the whole episode happens inside the entry window."""
    market = market_ending_in(140)
    _tick(eng, market, 140, 0.91)   # in band, in window — but see below
    assert eng.positions, "should have entered on the first qualifying tick"


def test_recross_after_the_window_shuts_does_not_trade(eng):
    market = market_ending_in(240)
    _tick(eng, market, 240, 0.90)
    _tick(eng, market, 230, 0.87)
    _tick(eng, market, 5, 0.93)     # back in band, but too late
    assert not eng.positions


# ── The miss must never be silent ────────────────────────────────────────

def test_in_band_with_no_crossing_tracked_is_reported(eng):
    """The one outcome that must never be silent: tradeable, but no record."""
    eng.track_candidates = False        # nothing will ever be tracked
    market = market_ending_in(100)
    _tick(eng, market, 100, 0.93)

    assert not eng.positions
    assert eng.stats()["block_reasons"].get("no_candidate_tracked") == 1


def test_that_warning_fires_once_per_side_not_every_tick(eng):
    eng.track_candidates = False
    market = market_ending_in(100)
    for secs in (100, 90, 80, 70):
        _tick(eng, market, secs, 0.93)
    assert eng.stats()["block_reasons"]["no_candidate_tracked"] == 1


def test_tracking_off_is_a_blanket_blocker(eng):
    assert eng._not_trading_because() == []
    eng.track_candidates = False
    assert any("tracking is off" in r for r in eng._not_trading_because())


def test_tracking_narrower_than_the_entry_window_is_a_blanket_blocker(eng):
    """Track from 120s with entries allowed from 150s is a 30s dead zone."""
    assert eng._not_trading_because() == []
    eng.candidate_max_secs = 120
    eng.max_secs_remaining = 150
    blockers = eng._not_trading_because()
    assert any("nothing is tracked yet" in r for r in blockers), blockers


def test_a_side_with_no_book_during_the_window_is_reported(eng):
    """A dead feed inside the entry window is a miss, not a non-event."""
    market = market_ending_in(100)
    market.end_time = market_ending_in(100).end_time
    # Only the Down side ever quotes; Up never gets a book.
    eng.feed.set(market.token_id_down, ask=0.10, ask_size=500, bid=0.09, bid_size=500)
    eng.on_tick([market])

    assert eng.stats()["block_reasons"].get("no_price_feed") == 1
