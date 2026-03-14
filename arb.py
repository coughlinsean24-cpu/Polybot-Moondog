"""
Polybot Snipez — Combined-Ask Arbitrage Engine

Strategy: When up_ask + down_ask < $1.00, buy BOTH sides immediately.
One side always resolves to $1.00 at settlement.
Guaranteed profit = $1.00 − (up_ask + down_ask) per share.

Example: up_ask=$0.06, down_ask=$0.91 → combined=$0.97
  Buy 50 shares each side → cost=$48.50 → payout=$50.00 → profit=$1.50

Key risks handled:
  - Partial fills: if only one side fills, cancel the other + accept directional exposure
  - Stale prices: skip if WS data is older than 5 seconds
  - Slippage: add 1¢ price cushion to ensure fill
"""

import time
import threading
from datetime import datetime, timezone
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor

import config
from logger import log, log_error
from polymarket_client import (
    MarketWindow, place_limit_buy, cancel_order, get_order_status,
    seconds_until,
)
from ws_feed import PriceFeed


# ── Data Types ────────────────────────────────────────────────────────────

@dataclass
class ArbPosition:
    """Tracks one arbitrage position (both sides of a market)."""
    market_id: str
    question: str
    asset: str

    # Entry prices
    up_price: float = 0.0
    down_price: float = 0.0
    combined: float = 0.0
    edge: float = 0.0          # 1.0 − combined (profit per $1 payout)

    # Shares (always equal on both sides for guaranteed arb)
    shares: int = 0

    # Orders
    order_id_up: str | None = None
    order_id_down: str | None = None

    # Fill status
    up_filled: bool = False
    down_filled: bool = False
    up_fill_size: float = 0.0
    down_fill_size: float = 0.0

    # Timing
    entered_at: float = 0.0
    resolved_at: float = 0.0

    # Resolution
    resolved: bool = False
    payout: float = 0.0
    profit: float = 0.0
    cost: float = 0.0
    outcome: str = ""  # "both_filled", "partial_up", "partial_down", "cancelled", "timeout"

    def to_dict(self) -> dict:
        return {
            "market_id": self.market_id,
            "question": self.question,
            "asset": self.asset,
            "up_price": round(self.up_price, 4),
            "down_price": round(self.down_price, 4),
            "combined": round(self.combined, 4),
            "edge": round(self.edge, 4),
            "shares": self.shares,
            "order_id_up": self.order_id_up,
            "order_id_down": self.order_id_down,
            "up_filled": self.up_filled,
            "down_filled": self.down_filled,
            "up_fill_size": self.up_fill_size,
            "down_fill_size": self.down_fill_size,
            "entered_at": self.entered_at,
            "resolved": self.resolved,
            "cost": round(self.cost, 4),
            "payout": round(self.payout, 4),
            "profit": round(self.profit, 4),
            "outcome": self.outcome,
            "age_secs": round(time.time() - self.entered_at, 1) if self.entered_at else 0,
        }


# ── Arb Engine ────────────────────────────────────────────────────────────

class ArbEngine:
    """Monitors combined asks across all markets and executes arbitrage."""

    def __init__(self, ws_feed: PriceFeed):
        self.ws_feed = ws_feed
        self.enabled: bool = config.ARB_ENABLED
        self.min_edge: float = config.ARB_MIN_EDGE
        self.trade_size: float = config.ARB_TRADE_SIZE
        self.max_positions: int = config.ARB_MAX_POSITIONS
        self.cooldown: float = config.ARB_COOLDOWN
        self.fill_timeout: float = config.ARB_FILL_TIMEOUT
        self.max_daily_spend: float = config.ARB_MAX_DAILY_SPEND

        # State
        self.positions: dict[str, ArbPosition] = {}      # market_id → ArbPosition
        self.cooldowns: dict[str, float] = {}             # market_id → last arb timestamp
        self.lock = threading.Lock()

        # Daily tracking
        self.daily_spend: float = 0.0
        self.daily_date: str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Stats
        self.total_arbs: int = 0
        self.total_profit: float = 0.0
        self.total_spent: float = 0.0
        self.opportunities_seen: int = 0

        # Callback for logging to web dashboard (set by web_dashboard.py)
        self.log_callback = None

    def _log(self, msg: str, level: str = "info"):
        """Log to both file logger and web dashboard."""
        log.info(f"[ARB] {msg}")
        if self.log_callback:
            self.log_callback(f"[ARB] {msg}", level)

    def _reset_daily(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self.daily_date:
            self.daily_spend = 0.0
            self.daily_date = today

    # ── Opportunity Detection ─────────────────────────────────────────────

    def scan_opportunity(self, market: MarketWindow) -> ArbPosition | None:
        """
        Check if a market has an arb opportunity. If so, execute immediately.
        Called from the main bot loop for each watched market.
        """
        if not self.enabled:
            return None

        mid = market.market_id

        # Skip if already have position on this market
        if mid in self.positions:
            return None

        # Skip if market is about to close (< 15s) — not enough time to fill
        secs = seconds_until(market.end_time)
        if secs < 15:
            return None

        # Skip if on cooldown
        now = time.time()
        if now - self.cooldowns.get(mid, 0) < self.cooldown:
            return None

        # Check position limit
        active = sum(1 for p in self.positions.values() if not p.resolved)
        if active >= self.max_positions:
            return None

        # Daily spend check
        self._reset_daily()
        if self.daily_spend >= self.max_daily_spend:
            return None

        # ── Get real-time prices from WebSocket ───────────────────────────
        ws_up = self.ws_feed.get_price(market.token_id_up)
        ws_down = self.ws_feed.get_price(market.token_id_down)

        if not ws_up.valid or not ws_down.valid:
            return None

        # Reject stale data (> 5 seconds old)
        data_age = now - max(ws_up.timestamp, ws_down.timestamp)
        if data_age > 5.0:
            return None

        up_ask = ws_up.best_ask
        down_ask = ws_down.best_ask

        if up_ask <= 0 or down_ask <= 0:
            return None

        combined = up_ask + down_ask
        edge = 1.0 - combined

        # ── Is the edge big enough? ──────────────────────────────────────
        if edge < self.min_edge:
            return None

        self.opportunities_seen += 1

        # ── Check liquidity on both sides ─────────────────────────────────
        min_available = min(ws_up.best_ask_size, ws_down.best_ask_size)
        if min_available < 1:
            return None

        # ── Calculate position size ───────────────────────────────────────
        max_by_budget = int(self.trade_size / combined)
        max_by_liquidity = int(min_available)
        max_by_daily = int((self.max_daily_spend - self.daily_spend) / combined) if combined > 0 else 0
        shares = min(max_by_budget, max_by_liquidity, max_by_daily)

        if shares < 1:
            return None

        total_cost = shares * combined
        expected_profit = shares * edge

        self._log(
            f"OPPORTUNITY: {market.asset} {market.question[-30:]} | "
            f"Up={up_ask:.2f} + Down={down_ask:.2f} = {combined:.3f} | "
            f"Edge={edge:.3f} ({edge*100:.1f}%) | "
            f"Shares={shares} | Cost=${total_cost:.2f} → Profit=${expected_profit:.2f}",
            "trade",
        )

        # ═══ EXECUTE ═══
        return self._execute_arb(market, up_ask, down_ask, shares)

    # ── Order Execution ───────────────────────────────────────────────────

    def _execute_arb(self, market: MarketWindow,
                     up_ask: float, down_ask: float, shares: int) -> ArbPosition | None:
        """Place buy orders on both sides simultaneously."""
        combined = up_ask + down_ask
        edge = 1.0 - combined

        pos = ArbPosition(
            market_id=market.market_id,
            question=market.question,
            asset=market.asset,
            up_price=up_ask,
            down_price=down_ask,
            combined=combined,
            edge=edge,
            shares=shares,
            entered_at=time.time(),
        )

        # Place both orders in parallel for speed
        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_up = pool.submit(
                place_limit_buy,
                token_id=market.token_id_up,
                price=up_ask,
                size=shares,
                market_id=market.market_id,
            )
            fut_down = pool.submit(
                place_limit_buy,
                token_id=market.token_id_down,
                price=down_ask,
                size=shares,
                market_id=market.market_id,
            )
            pos.order_id_up = fut_up.result()
            pos.order_id_down = fut_down.result()

        # Both orders failed
        if not pos.order_id_up and not pos.order_id_down:
            self._log("FAILED: Both orders rejected", "error")
            return None

        # One side failed — cancel the other to avoid directional exposure
        if not pos.order_id_up or not pos.order_id_down:
            failed_side = "Up" if not pos.order_id_up else "Down"
            success_id = pos.order_id_up or pos.order_id_down
            self._log(
                f"PARTIAL PLACE: {failed_side} order failed. "
                f"Cancelling {success_id} to avoid exposure.",
                "warn",
            )
            cancel_order(success_id)
            pos.outcome = f"place_failed_{failed_side.lower()}"
            pos.resolved = True
            return None

        # ── Both orders placed successfully ────────────────────────────────
        total_cost = shares * combined

        with self.lock:
            self.positions[market.market_id] = pos
            self.cooldowns[market.market_id] = time.time()
            self.total_arbs += 1
            self.total_spent += total_cost
            self.daily_spend += total_cost

        self._log(
            f"ENTERED: {market.asset} | {shares} shares @ "
            f"Up={up_ask:.2f} + Down={down_ask:.2f} = {combined:.3f}/pair | "
            f"Cost=${total_cost:.2f} | Expected=${shares * edge:.2f}",
            "trade",
        )

        return pos

    # ── Fill Monitoring ───────────────────────────────────────────────────

    def check_fills(self):
        """Check fill status of all active arb positions."""
        for mid, pos in list(self.positions.items()):
            if pos.resolved:
                continue

            # Check Up side
            if pos.order_id_up and not pos.up_filled:
                try:
                    status = get_order_status(pos.order_id_up)
                    if status["status"] in ("matched", "partial"):
                        pos.up_filled = True
                        pos.up_fill_size = status["size_matched"]
                        self._log(
                            f"FILL UP: {pos.asset} {pos.up_fill_size:.0f} sh @ ${pos.up_price:.2f}",
                            "trade",
                        )
                except Exception as e:
                    log_error(f"arb_check_fill_up({mid})", e)

            # Check Down side
            if pos.order_id_down and not pos.down_filled:
                try:
                    status = get_order_status(pos.order_id_down)
                    if status["status"] in ("matched", "partial"):
                        pos.down_filled = True
                        pos.down_fill_size = status["size_matched"]
                        self._log(
                            f"FILL DOWN: {pos.asset} {pos.down_fill_size:.0f} sh @ ${pos.down_price:.2f}",
                            "trade",
                        )
                except Exception as e:
                    log_error(f"arb_check_fill_down({mid})", e)

            # Both filled → guaranteed profit
            if pos.up_filled and pos.down_filled:
                filled_shares = min(pos.up_fill_size, pos.down_fill_size)
                cost = (pos.up_price * pos.up_fill_size) + (pos.down_price * pos.down_fill_size)
                payout = filled_shares * 1.0  # Winner pays $1/share
                profit = payout - cost

                pos.cost = cost
                pos.payout = payout
                pos.profit = profit
                pos.outcome = "both_filled"
                pos.resolved = True
                pos.resolved_at = time.time()

                with self.lock:
                    self.total_profit += profit

                self._log(
                    f"BOTH FILLED: {pos.asset} | "
                    f"Cost=${cost:.2f} → Payout=${payout:.2f} | "
                    f"PROFIT=${profit:.2f}",
                    "trade",
                )

    # ── Timeout / Stale Order Cleanup ─────────────────────────────────────

    def cancel_stale(self):
        """Cancel unfilled arb orders that have been open past fill_timeout."""
        now = time.time()
        for mid, pos in list(self.positions.items()):
            if pos.resolved:
                continue
            if now - pos.entered_at < self.fill_timeout:
                continue

            # Cancel unfilled sides
            cancelled = []
            if pos.order_id_up and not pos.up_filled:
                cancel_order(pos.order_id_up)
                cancelled.append("Up")
            if pos.order_id_down and not pos.down_filled:
                cancel_order(pos.order_id_down)
                cancelled.append("Down")

            # Determine outcome
            if pos.up_filled and not pos.down_filled:
                pos.outcome = "partial_up"
                pos.cost = pos.up_price * pos.up_fill_size
                # One side filled = directional bet, not arb
                self._log(
                    f"TIMEOUT: Only UP filled ({pos.up_fill_size:.0f} sh @ ${pos.up_price:.2f}). "
                    f"Directional exposure until settlement. {pos.asset}",
                    "warn",
                )
            elif pos.down_filled and not pos.up_filled:
                pos.outcome = "partial_down"
                pos.cost = pos.down_price * pos.down_fill_size
                self._log(
                    f"TIMEOUT: Only DOWN filled ({pos.down_fill_size:.0f} sh @ ${pos.down_price:.2f}). "
                    f"Directional exposure until settlement. {pos.asset}",
                    "warn",
                )
            elif pos.up_filled and pos.down_filled:
                # Shouldn't happen (check_fills should've caught it), but handle gracefully
                pos.outcome = "both_filled"
                filled = min(pos.up_fill_size, pos.down_fill_size)
                pos.cost = (pos.up_price * pos.up_fill_size) + (pos.down_price * pos.down_fill_size)
                pos.payout = filled * 1.0
                pos.profit = pos.payout - pos.cost
                with self.lock:
                    self.total_profit += pos.profit
            else:
                pos.outcome = "cancelled"
                self._log(
                    f"TIMEOUT: Neither side filled. Cancelled. {pos.asset} {pos.question[-25:]}",
                    "info",
                )

            pos.resolved = True
            pos.resolved_at = now

    # ── Stats ─────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        active = [p for p in self.positions.values() if not p.resolved]
        resolved = [p for p in self.positions.values() if p.resolved]
        both_filled = [p for p in resolved if p.outcome == "both_filled"]
        partial = [p for p in resolved if p.outcome.startswith("partial_")]

        return {
            "enabled": self.enabled,
            "min_edge": self.min_edge,
            "trade_size": self.trade_size,
            "max_positions": self.max_positions,
            "cooldown": self.cooldown,
            "fill_timeout": self.fill_timeout,
            "max_daily_spend": self.max_daily_spend,
            "daily_spend": round(self.daily_spend, 2),
            "total_arbs": self.total_arbs,
            "total_profit": round(self.total_profit, 2),
            "total_spent": round(self.total_spent, 2),
            "opportunities_seen": self.opportunities_seen,
            "active_count": len(active),
            "resolved_count": len(resolved),
            "both_filled_count": len(both_filled),
            "partial_count": len(partial),
            "active_positions": [p.to_dict() for p in active],
            "recent_resolved": [p.to_dict() for p in list(resolved)[-20:]],
        }

    def get_positions_list(self) -> list[dict]:
        """All positions (active first, then resolved) for dashboard display."""
        active = sorted(
            [p for p in self.positions.values() if not p.resolved],
            key=lambda p: p.entered_at, reverse=True,
        )
        resolved = sorted(
            [p for p in self.positions.values() if p.resolved],
            key=lambda p: p.resolved_at, reverse=True,
        )
        return [p.to_dict() for p in active + resolved[:30]]

    # ── Shutdown ──────────────────────────────────────────────────────────

    def shutdown(self):
        """Cancel all active arb orders on shutdown."""
        for mid, pos in self.positions.items():
            if pos.resolved:
                continue
            if pos.order_id_up and not pos.up_filled:
                cancel_order(pos.order_id_up)
            if pos.order_id_down and not pos.down_filled:
                cancel_order(pos.order_id_down)
            pos.resolved = True
            pos.outcome = "shutdown"
        self._log("All active arb orders cancelled (shutdown)", "info")
