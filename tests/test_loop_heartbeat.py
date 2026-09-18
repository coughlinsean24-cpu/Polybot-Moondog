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
    ("socket dropped for good",
     "socketConnected=false; socketDownSince=Date.now()-45000; "
     "lastStateAt=Date.now()-45000;",
     "DISCONNECTED"),
    # Socket.IO reconnects in about a second; a blip that short is not an alarm.
    ("socket blipped, still inside the grace period",
     "socketConnected=false; socketDownSince=Date.now()-1000; "
     "lastStateAt=Date.now()-1000;",
     None),
    ("socket flapping", "socketConnected=true; lastStateAt=Date.now(); "
                        "lastTickAt=Date.now(); "
                        "reconnectTimes=[Date.now()-5000,Date.now()-3000,Date.now()-1000];",
     "CONNECTION UNSTABLE"),
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


# ── One dashboard per port ───────────────────────────────────────────────
# Werkzeug sets SO_REUSEADDR, and on Windows that lets a second process bind
# a port another is already listening on. Both accept connections, and the
# browser's Socket.IO session flaps between them.

def test_port_probe_detects_a_listener(monkeypatch):
    import socket as _socket
    import threading

    srv = _socket.socket()
    srv.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.listen(5)

    stop = threading.Event()

    def _accept():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
                conn.close()
            except Exception:
                pass

    t = threading.Thread(target=_accept, daemon=True)
    t.start()
    try:
        assert wd._port_is_taken(port) is True
    finally:
        stop.set()
        t.join(timeout=2)
        srv.close()

    # Closed again — and a free port must not read as taken, or every start
    # would refuse to run.
    assert wd._port_is_taken(port) is False


def test_start_refuses_rather_than_binding_a_second_listener(monkeypatch, capsys):
    """Two servers on one port is worse than not starting: the page would show
    one copy's numbers while the other holds the positions."""
    monkeypatch.setattr(wd, "_port_is_taken", lambda *a, **k: True)
    monkeypatch.setattr(wd, "PID_FILE", "/nonexistent/dashboard.pid")

    with pytest.raises(SystemExit) as exc:
        wd._kill_old_instances()
    assert exc.value.code == 1
    assert "ALREADY RUNNING" in capsys.readouterr().out


def test_a_free_port_starts_normally_and_records_the_pid(monkeypatch, tmp_path):
    pid_file = tmp_path / "dashboard.pid"
    monkeypatch.setattr(wd, "_port_is_taken", lambda *a, **k: False)
    monkeypatch.setattr(wd, "PID_FILE", str(pid_file))

    wd._kill_old_instances()        # must not raise, must not exit
    assert pid_file.read_text().strip() == str(os.getpid())


def test_a_stale_pid_file_does_not_kill_this_process(monkeypatch, tmp_path):
    """The pidfile says who to stop — never us."""
    pid_file = tmp_path / "dashboard.pid"
    pid_file.write_text(str(os.getpid()))
    killed = []
    monkeypatch.setattr(wd, "PID_FILE", str(pid_file))
    monkeypatch.setattr(wd, "_port_is_taken", lambda *a, **k: False)
    monkeypatch.setattr(wd, "_kill_pid", lambda pid: killed.append(pid) or True)

    wd._kill_old_instances()
    assert killed == []
