"""
Polybot Snipez — Live Dashboard
Beautiful terminal dashboard using Rich for real-time bot monitoring.
"""

import json
import os
import re
import threading
import time
from datetime import datetime, timezone, timedelta
from collections import deque

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.align import Align
from rich import box

import config

# ── Dashboard State (thread-safe updates from bot) ──────────────────────────

class DashboardState:
    """Thread-safe state container for dashboard rendering."""

    def __init__(self):
        self._lock = threading.Lock()

        # Bot status
        self.mode = "PAPER" if config.PAPER_TRADING else "LIVE"
        self.status = "STARTING"
        self.start_time: datetime = datetime.now(timezone.utc)
        self.last_poll: datetime | None = None
        self.last_fire: datetime | None = None

        # Counters
        self.trades_today: int = 0
        self.markets_watched: int = 0
        self.markets_fired: int = 0
        self.signals_evaluated: int = 0
        self.signals_skipped: int = 0
        self.errors: int = 0

        # Active markets: list of dicts
        self.active_markets: list[dict] = []

        # Recent activity log: deque of (timestamp, message, style) tuples
        self.activity_log: deque = deque(maxlen=20)

        # Trade history for P&L: list of dicts
        self.trades: list[dict] = []

        # P&L tracking
        self.total_spent: float = 0.0
        self.total_payout: float = 0.0
        self.total_profit: float = 0.0

        # WebSocket feed stats
        self.ws_connected: bool = False
        self.ws_messages: int = 0
        self.ws_reconnects: int = 0

        # Data recorder stats
        self.rec_ticks: int = 0
        self.rec_signals: int = 0

    def update(self, **kwargs):
        """Thread-safe update of any state fields."""
        with self._lock:
            for k, v in kwargs.items():
                if hasattr(self, k):
                    setattr(self, k, v)

    def add_activity(self, message: str, style: str = "white"):
        """Add a line to the activity log."""
        with self._lock:
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            self.activity_log.appendleft((ts, message, style))

    def add_trade(self, trade: dict):
        """Add a trade record."""
        with self._lock:
            self.trades.append(trade)
            cost = trade.get("cost", 0)
            self.total_spent += cost

    def record_payout(self, amount: float):
        """Record a market payout."""
        with self._lock:
            self.total_payout += amount
            self.total_profit = self.total_payout - self.total_spent

    def set_active_markets(self, markets: list[dict]):
        """Replace the active markets list."""
        with self._lock:
            self.active_markets = markets

    def snapshot(self) -> dict:
        """Return a copy of all state for rendering."""
        with self._lock:
            return {
                "mode": self.mode,
                "status": self.status,
                "start_time": self.start_time,
                "last_poll": self.last_poll,
                "last_fire": self.last_fire,
                "trades_today": self.trades_today,
                "markets_watched": self.markets_watched,
                "markets_fired": self.markets_fired,
                "signals_evaluated": self.signals_evaluated,
                "signals_skipped": self.signals_skipped,
                "errors": self.errors,
                "active_markets": [dict(m) for m in self.active_markets],
                "activity_log": list(self.activity_log),
                "trades": list(self.trades),
                "total_spent": self.total_spent,
                "total_payout": self.total_payout,
                "total_profit": self.total_profit,
                "ws_connected": self.ws_connected,
                "ws_messages": self.ws_messages,
                "ws_reconnects": self.ws_reconnects,
                "rec_ticks": self.rec_ticks,
                "rec_signals": self.rec_signals,
            }


# Global dashboard state
dash_state = DashboardState()


# ── Rendering Functions ─────────────────────────────────────────────────────

def _make_header() -> Panel:
    """Render the top header banner."""
    snap = dash_state.snapshot()
    mode = snap["mode"]
    status = snap["status"]

    if mode == "LIVE":
        mode_style = "bold red"
        mode_icon = "● LIVE"
    else:
        mode_style = "bold yellow"
        mode_icon = "◌ PAPER"

    status_colors = {
        "STARTING": "yellow",
        "RUNNING": "green",
        "POLLING": "cyan",
        "FIRING": "bold red",
        "STOPPED": "red",
        "ERROR": "bold red",
    }
    status_style = status_colors.get(status, "white")

    title = Text()
    title.append("  ⚡ POLYBOT SNIPEZ ", style="bold white")
    title.append("─── ", style="dim")
    title.append(f"{mode_icon} ", style=mode_style)
    title.append("─── ", style="dim")
    title.append(f"Status: {status}", style=status_style)

    # Uptime
    uptime = datetime.now(timezone.utc) - snap["start_time"]
    hours, remainder = divmod(int(uptime.total_seconds()), 3600)
    minutes, seconds = divmod(remainder, 60)
    title.append(f"  │  Uptime: {hours:02d}:{minutes:02d}:{seconds:02d}", style="dim")

    return Panel(
        Align.left(title),
        box=box.DOUBLE_EDGE,
        style="bright_blue",
        height=3,
    )


def _make_stats_panel() -> Panel:
    """Render the statistics overview panel."""
    snap = dash_state.snapshot()

    grid = Table(show_header=False, box=None, padding=(0, 2), expand=True)
    grid.add_column("label", style="dim", ratio=1)
    grid.add_column("value", style="bold", ratio=1)
    grid.add_column("label2", style="dim", ratio=1)
    grid.add_column("value2", style="bold", ratio=1)

    # Row 1
    grid.add_row(
        "Markets Watching",
        str(snap["markets_watched"]),
        "Bids Today",
        f"{snap['trades_today']} / {config.MAX_BIDS_PER_DAY if config.MAX_BIDS_PER_DAY > 0 else 'oo'}",
    )

    # Row 2
    grid.add_row(
        "Bids Posted",
        str(snap["markets_fired"]),
        "Markets Evaluated",
        str(snap["signals_evaluated"]),
    )

    # Row 3
    last_poll = snap["last_poll"]
    if last_poll:
        ago = (datetime.now(timezone.utc) - last_poll).total_seconds()
        poll_str = f"{ago:.0f}s ago"
    else:
        poll_str = "—"

    last_fire = snap["last_fire"]
    fire_str = last_fire.strftime("%H:%M:%S") if last_fire else "—"

    grid.add_row(
        "Last Poll",
        poll_str,
        "Last Fire",
        fire_str,
    )

    # Row 4
    ws_on = snap.get("ws_connected", False)
    ws_msgs = snap.get("ws_messages", 0)
    ws_str = Text()
    if ws_on:
        ws_str.append("● ON", style="bold green")
        ws_str.append(f"  ({ws_msgs} msgs)", style="dim")
    else:
        ws_str.append("○ OFF", style="bold red")
        reconn = snap.get("ws_reconnects", 0)
        if reconn:
            ws_str.append(f"  (reconn: {reconn})", style="dim")

    grid.add_row(
        "WebSocket Feed",
        ws_str,
        "Errors",
        Text(str(snap["errors"]), style="red" if snap["errors"] > 0 else "green"),
    )

    # Row 5
    rec_ticks = snap.get("rec_ticks", 0)
    rec_signals = snap.get("rec_signals", 0)
    rec_str = Text()
    rec_str.append(f"{rec_ticks:,}", style="green")
    rec_str.append(" ticks", style="dim")
    if rec_signals > 0:
        rec_str.append(f"  ({rec_signals} signals)", style="bold yellow")

    grid.add_row(
        "Fills Detected",
        str(snap["signals_skipped"]),
        "Data Saved",
        rec_str,
    )

    return Panel(
        grid,
        title="[bold cyan]📊 Stats[/]",
        border_style="cyan",
        box=box.ROUNDED,
    )


def _make_pnl_panel() -> Panel:
    """Render the P&L tracking panel."""
    snap = dash_state.snapshot()

    spent = snap["total_spent"]
    payout = snap["total_payout"]
    profit = snap["total_profit"]

    grid = Table(show_header=False, box=None, padding=(0, 2), expand=True)
    grid.add_column("label", style="dim", ratio=1)
    grid.add_column("value", ratio=1)

    # Spent
    grid.add_row("Total Spent", Text(f"${spent:.2f}", style="yellow"))

    # Payout
    grid.add_row("Total Payout", Text(f"${payout:.2f}", style="green" if payout > 0 else "dim"))

    # P&L
    if profit > 0:
        pnl_style = "bold green"
        pnl_prefix = "+"
    elif profit < 0:
        pnl_style = "bold red"
        pnl_prefix = ""
    else:
        pnl_style = "dim"
        pnl_prefix = ""

    grid.add_row("Net P&L", Text(f"{pnl_prefix}${profit:.2f}", style=pnl_style))

    # ROI
    roi = (profit / spent * 100) if spent > 0 else 0
    roi_style = "green" if roi > 0 else ("red" if roi < 0 else "dim")
    grid.add_row("ROI", Text(f"{roi:+.1f}%", style=roi_style))

    # Config info
    cost_both = config.BID_PRICE * config.TOKENS_PER_SIDE * 2
    grid.add_row("", Text(""))
    grid.add_row("Bid Price", Text(f"${config.BID_PRICE}", style="dim"))
    grid.add_row("Tokens/Side", Text(f"{config.TOKENS_PER_SIDE}", style="dim"))
    grid.add_row("Cost if Both Fill", Text(f"${cost_both:.2f}", style="dim"))

    return Panel(
        grid,
        title="[bold green]💰 P&L[/]",
        border_style="green",
        box=box.ROUNDED,
    )


def _make_markets_panel() -> Panel:
    """Render the active markets table."""
    snap = dash_state.snapshot()

    table = Table(
        box=box.SIMPLE_HEAVY,
        show_edge=False,
        expand=True,
        row_styles=["", "dim"],
    )
    table.add_column("Market", style="white", max_width=24, no_wrap=True)
    table.add_column("Closes In", justify="right", style="cyan", width=9)
    table.add_column("Up Ask", justify="right", width=8)
    table.add_column("Down Ask", justify="right", width=8)
    table.add_column("Combined", justify="right", width=9)
    table.add_column("Age", justify="right", width=5)
    table.add_column("Bid", justify="center", width=12)

    markets = snap["active_markets"]

    if not markets:
        return Panel(
            Align.center(Text("No active markets in window", style="dim italic")),
            title="[bold yellow]🎯 Active Markets[/]",
            border_style="yellow",
            box=box.ROUNDED,
            height=8,
        )

    # Compute live secs_remaining from end_time (not stale cached values)
    now = datetime.now(timezone.utc)
    for m in markets:
        et = m.get("end_time")
        if et:
            m["secs_remaining"] = (et - now).total_seconds()

    # Sort by time remaining
    markets.sort(key=lambda m: m.get("secs_remaining", 9999))

    render_now = time.time()

    for m in markets[:12]:  # Show up to 12
        secs = m.get("secs_remaining", 0)
        up_ask = m.get("up_ask", 0)
        down_ask = m.get("down_ask", 0)
        combined = m.get("combined", 0)
        status = m.get("status", "watching")
        question = m.get("question", m.get("market_id", "—"))

        # Extract just the time range (e.g., "2:20PM-2:25PM ET") for compact display
        _time_match = re.search(r'(\d{1,2}:\d{2}[AP]M\s*-\s*\d{1,2}:\d{2}[AP]M\s*ET)', question)
        if _time_match:
            question = _time_match.group(1)
        elif len(question) > 23:
            question = "…" + question[-22:]

        # Time styling
        if secs <= 30:
            time_style = "bold red"
        elif secs <= 60:
            time_style = "bold yellow"
        elif secs <= 180:
            time_style = "cyan"
        else:
            time_style = "dim"

        mins, sec = divmod(int(secs), 60)
        time_str = f"{mins}:{sec:02d}" if mins > 0 else f"{sec}s"

        # Combined price styling
        if combined > 0 and combined < 0.50:
            combined_style = "bold green"
        elif combined > 0 and combined < 1.00:
            combined_style = "yellow"
        else:
            combined_style = "dim"

        # Price age — how fresh the orderbook data is
        price_ts = m.get("price_ts", 0)
        if price_ts > 0:
            age = render_now - price_ts
            if age < 1.0:
                age_str = f"{age:.1f}s"
                age_style = "bold green"
            elif age < 3.0:
                age_str = f"{age:.1f}s"
                age_style = "green"
            elif age < 10.0:
                age_str = f"{age:.0f}s"
                age_style = "yellow"
            else:
                age_str = f"{age:.0f}s"
                age_style = "red"
        else:
            age_str = "—"
            age_style = "dim"

        # Status styling
        status_map = {
            "watching": ("WATCH", "dim"),
            "in_range": ("RANGE", "yellow"),
            "bids_live": ("BIDS LIVE", "bold cyan"),
            "partial_fill": ("PARTIAL", "bold yellow"),
            "both_filled": ("BOTH FILL", "bold green"),
            "expired": ("DONE", "dim"),
        }
        status_text, status_style = status_map.get(status, (status.upper(), "white"))

        table.add_row(
            question,
            Text(time_str, style=time_style),
            f"${up_ask:.3f}" if up_ask > 0 else "—",
            f"${down_ask:.3f}" if down_ask > 0 else "—",
            Text(f"${combined:.3f}" if combined > 0 else "—", style=combined_style),
            Text(age_str, style=age_style),
            Text(status_text, style=status_style),
        )

    return Panel(
        table,
        title=f"[bold yellow]🎯 Active Markets ({len(markets)})[/]",
        border_style="yellow",
        box=box.ROUNDED,
    )


def _make_activity_panel() -> Panel:
    """Render the recent activity log."""
    snap = dash_state.snapshot()

    text = Text()
    entries = snap["activity_log"]

    if not entries:
        text.append("  Waiting for activity...", style="dim italic")
    else:
        for i, (ts, msg, style) in enumerate(entries):
            if i > 0:
                text.append("\n")
            text.append(f"  {ts} ", style="dim")
            text.append(msg, style=style)

    return Panel(
        text,
        title="[bold magenta]📋 Activity Log[/]",
        border_style="magenta",
        box=box.ROUNDED,
    )


def _make_trades_panel() -> Panel:
    """Render the recent trades table."""
    snap = dash_state.snapshot()

    table = Table(
        box=box.SIMPLE,
        show_edge=False,
        expand=True,
    )
    table.add_column("Time", style="dim", width=8)
    table.add_column("Market", max_width=20, no_wrap=True)
    table.add_column("Side", justify="center", width=6)
    table.add_column("Price", justify="right", width=7)
    table.add_column("Tokens", justify="right", width=7)
    table.add_column("Cost", justify="right", width=8)
    table.add_column("Result", justify="center", width=8)

    trades = snap["trades"]

    if not trades:
        return Panel(
            Align.center(Text("No trades yet", style="dim italic")),
            title="[bold red]🔥 Recent Trades[/]",
            border_style="red",
            box=box.ROUNDED,
            height=6,
        )

    # Show most recent first, limit to 10
    for t in reversed(trades[-10:]):
        ts = t.get("time", "—")
        market = t.get("market", "—")
        if len(market) > 19:
            market = market[:16] + "..."
        side = t.get("side", "—")
        side_style = "cyan" if side == "UP" else "magenta"
        price = t.get("price", 0)
        tokens = t.get("tokens", 0)
        cost = t.get("cost", 0)
        result = t.get("result", "pending")

        result_map = {
            "pending": ("⏳ PEND", "yellow"),
            "won": ("✓ WON", "bold green"),
            "lost": ("✗ LOST", "red"),
            "cancelled": ("⊘ CANC", "dim"),
        }
        result_text, result_style = result_map.get(result, (result, "white"))

        table.add_row(
            ts,
            market,
            Text(side, style=side_style),
            f"${price:.3f}",
            str(tokens),
            f"${cost:.2f}",
            Text(result_text, style=result_style),
        )

    return Panel(
        table,
        title=f"[bold red]🔥 Recent Trades ({len(trades)})[/]",
        border_style="red",
        box=box.ROUNDED,
    )


def _make_config_bar() -> Panel:
    """Render a compact config/footer bar."""
    snap = dash_state.snapshot()

    text = Text()
    text.append("  Bid: ", style="dim")
    text.append(f"${config.BID_PRICE} x {config.TOKENS_PER_SIDE}/side", style="white")
    text.append("  |  ", style="dim")
    text.append("Window: ", style="dim")
    text.append(f"{config.BID_WINDOW_CLOSE}s-{config.BID_WINDOW_OPEN}s", style="white")
    text.append("  |  ", style="dim")
    text.append("Risk/Mkt: ", style="dim")
    text.append(f"${config.MAX_RISK_PER_MARKET}", style="white")
    text.append("  |  ", style="dim")
    now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    text.append(f"  {now}", style="dim")

    return Panel(
        text,
        box=box.HORIZONTALS,
        style="dim",
        height=3,
    )


def build_dashboard() -> Layout:
    """Build the complete dashboard layout."""
    layout = Layout()

    # Main vertical split
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body", ratio=1),
        Layout(name="footer", size=3),
    )

    # Body: left panel (stats + P&L) | right panel (markets + activity)
    layout["body"].split_row(
        Layout(name="left", ratio=2),
        Layout(name="right", ratio=3),
    )

    # Left: stats on top, P&L + trades on bottom
    layout["left"].split_column(
        Layout(name="stats", ratio=2),
        Layout(name="pnl", ratio=2),
        Layout(name="trades", ratio=3),
    )

    # Right: markets on top, activity log on bottom
    layout["right"].split_column(
        Layout(name="markets", ratio=3),
        Layout(name="activity", ratio=2),
    )

    # Render all panels
    layout["header"].update(_make_header())
    layout["stats"].update(_make_stats_panel())
    layout["pnl"].update(_make_pnl_panel())
    layout["trades"].update(_make_trades_panel())
    layout["markets"].update(_make_markets_panel())
    layout["activity"].update(_make_activity_panel())
    layout["footer"].update(_make_config_bar())

    return layout


# ── Dashboard Runner (threaded) ─────────────────────────────────────────────

_live: Live | None = None
_dashboard_thread: threading.Thread | None = None
_dashboard_running = False


def start_dashboard():
    """Start the live dashboard in a background thread."""
    global _live, _dashboard_thread, _dashboard_running

    console = Console()
    _live = Live(
        build_dashboard(),
        console=console,
        refresh_per_second=4,
        screen=True,
    )

    _dashboard_running = True

    def _run():
        with _live:
            while _dashboard_running:
                try:
                    _live.update(build_dashboard())
                except Exception:
                    pass
                time.sleep(0.25)

    _dashboard_thread = threading.Thread(target=_run, daemon=True)
    _dashboard_thread.start()


def stop_dashboard():
    """Stop the dashboard."""
    global _dashboard_running
    _dashboard_running = False


# ── Convenience helpers for bot integration ─────────────────────────────────

def dash_log(message: str, style: str = "white"):
    """Log a message to the dashboard activity feed."""
    dash_state.add_activity(message, style)


def dash_market_update(markets_data: list[dict]):
    """Update the active markets panel."""
    dash_state.set_active_markets(markets_data)


def dash_trade(market: str, side: str, price: float, tokens: int):
    """Record a trade on the dashboard."""
    dash_state.add_trade({
        "time": datetime.now(timezone.utc).strftime("%H:%M:%S"),
        "market": market,
        "side": side,
        "price": price,
        "tokens": tokens,
        "cost": round(price * tokens, 4),
        "result": "pending",
    })


def dash_bids_posted(market_id: str, bid_price: float, tokens: int):
    """Record a bid-posting event (both sides)."""
    cost = bid_price * tokens * 2
    profit_if_both = (tokens * 1.0) - cost
    dash_state.update(
        last_fire=datetime.now(timezone.utc),
    )
    dash_log(
        f"BIDS POSTED {market_id[:20]}  ${bid_price} x {tokens}/side  "
        f"Cost=${cost:.2f}  Profit=${profit_if_both:.2f}",
        style="bold cyan",
    )


def dash_fill(market_id: str, side: str, price: float, tokens: int):
    """Record a fill event."""
    dash_log(
        f"FILL {side.upper()} {market_id[:20]}  ${price:.3f} x {tokens}",
        style="bold green",
    )
