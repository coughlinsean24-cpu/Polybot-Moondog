"""The dashboard has to be able to say "this screen is frozen".

Every number on the page is a snapshot pushed by the trading loop.  When that
loop stalls, the page keeps showing the last snapshot and a stalled bot is
indistinguishable from a quiet market.  These cover the server half: the
heartbeat the page reads to tell those two apart.
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import web_dashboard as wd  # noqa: E402


@pytest.fixture
def eng():
    e = wd.engine
    saved = (e.loop_beat, e.loop_phase, e.loop_cycles, e.loop_last_error)
    yield e
    (e.loop_beat, e.loop_phase, e.loop_cycles, e.loop_last_error) = saved


def test_loop_age_is_negative_before_the_first_cycle(eng):
    eng.loop_beat = 0.0
    assert eng.loop_age() == -1.0


def test_loop_age_grows_with_the_stall(eng):
    eng.loop_beat = time.time() - 45
    assert 44 <= eng.loop_age() <= 46


def test_beat_records_the_phase(eng):
    eng.beat("refresh_markets")
    assert eng.loop_phase == "refresh_markets"
    assert eng.loop_phase_since == pytest.approx(time.time(), abs=1.0)


def test_push_state_carries_the_heartbeat(eng, monkeypatch):
    sent = {}
    monkeypatch.setattr(wd.socketio, "emit",
                        lambda event, data=None, **kw: sent.setdefault(event, data))
    eng.loop_beat = time.time() - 12
    eng.beat("s9099")
    eng.loop_last_error = "s9099: boom"

    wd.push_state()

    state = sent["state"]
    assert 11 <= state["loop_age"] <= 13
    assert state["loop_phase"] == "s9099"
    assert state["loop_error"] == "s9099: boom"


def test_stale_threshold_leaves_room_for_a_normal_cycle():
    """The loop sleeps up to 1.0s and pushes every 1.5s; don't cry wolf."""
    assert wd.LOOP_STALE_SECS > 1.0 + 1.5
    assert wd.LOOP_SLOW_SECS >= 1.0 + 1.5


def test_page_is_told_the_stale_threshold():
    """The banner's timer runs in the browser, so the number has to reach it."""
    html = wd.app.test_client().get("/").get_data(as_text=True)
    assert "stale-banner" in html
    assert f"const LOOP_STALE_SECS = {wd.LOOP_STALE_SECS}" in html


# ── The banner's own logic ───────────────────────────────────────────────
# It runs in the browser, so this drives the real rendered function under node
# and checks it names the right failure.  Skipped where node is not installed.

_BANNER_CASES = [
    ("healthy", "socketConnected=true; botRunning=true; "
                "lastStateAt=Date.now(); lastTickAt=Date.now(); loopError='';",
     None),
    ("socket dropped", "socketConnected=false; lastStateAt=Date.now()-45000;",
     "DISCONNECTED"),
    ("loop wedged, tick thread alive",
     "socketConnected=true; lastStateAt=Date.now()-45000; lastTickAt=Date.now(); "
     "loopAge=45; loopPhase='refresh_markets'; loopError='';",
     "BOT LOOP STALLED"),
    ("nothing arriving at all",
     "socketConnected=true; lastStateAt=Date.now()-45000; lastTickAt=Date.now()-45000;",
     "NO DATA FROM BOT"),
    ("bot stopped on purpose",
     "botRunning=false; socketConnected=true; lastStateAt=Date.now()-45000; "
     "lastTickAt=Date.now();",
     None),
]


def _run_banner(setup: str) -> str:
    import json
    import re
    import shutil
    import subprocess
    import tempfile

    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")

    html = wd.app.test_client().get("/").get_data(as_text=True)
    m = re.search(
        r"const LOOP_STALE_SECS = .*?\nsetInterval\(updateStaleBanner, 1000\);",
        html, re.S,
    )
    assert m, "stale-banner script block not found in the rendered page"

    harness = (
        "let botRunning=true, socketConnected=true;\n"
        "const el={style:{},textContent:''};\n"
        "const document={getElementById:()=>el};\n"
        "function setInterval(){}\n"
        + m.group(0) + "\n"
        + setup + "\n"
        "updateStaleBanner();\n"
        "console.log(JSON.stringify("
        "el.style.display==='none' ? null : el.textContent));\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(harness)
        path = fh.name
    try:
        out = subprocess.run([node, path], capture_output=True, text=True,
                             timeout=30, check=True)
    finally:
        os.unlink(path)
    return json.loads(out.stdout.strip())


@pytest.mark.parametrize("name,setup,expected", _BANNER_CASES,
                         ids=[c[0] for c in _BANNER_CASES])
def test_banner_names_the_right_failure(name, setup, expected):
    msg = _run_banner(setup)
    if expected is None:
        assert msg is None, f"{name}: unexpected banner {msg!r}"
    else:
        assert msg and expected in msg, f"{name}: got {msg!r}"
