"""
Polymarket BTC 5-Minute Scalper
───────────────────────────────
Separate strategy from the limit-bid approach.  This module monitors
Polymarket's Bitcoin 5-minute Up/Down markets and:

  1.  Waits for a dip (share price falls below a threshold).
  2.  Buys YES shares on the side that the BTC movement favours.
  3.  Holds for a quick profit or cuts at a stop-loss.
  4.  Sells before the window expires.

All parameters are live-editable from the dashboard's Scalper tab.

Usage (standalone test):
    from scalper import Scalper
    s = Scalper(btc_feed, ws_feed)
    s.start()
    ...
    s.stop()

The web_dashboard.py integrates this via Engine.scalper.
"""

import time
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from collections import deque
from enum import Enum

from logger import log
import config
from polymarket_client import (
    MarketWindow, fetch_active_btc_markets,
    place_limit_buy, cancel_order, get_order_status,
    seconds_until, get_clob_client,
)
from ws_feed import PriceFeed
from binance_ws import BinanceFeed


# ══════════════════════════════════════════════════════════════════════════════
#  CONFIGURATION (live-editable from the dashboard)
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class ScalpConfig:
    """All scalper tunables — changed live from the UI."""
    trade_size_usd: float = 2.50       # Dollar amount per scalp trade
    entry_ask_max: float = 0.50        # Max ask price to consider entry (hard ceiling)
    profit_target: float = 0.04        # Sell when price rises this much above entry ($0.04 = 4¢)
    stop_loss: float = 0.05            # Cut position when price drops this much below entry
    stop_loss_pct: float = 0.20        # % stop-loss (0.20 = 20% of entry price)
    profit_target_pct: float = 0.25    # % profit target (0.25 = 25% of entry price)
    use_pct_exits: bool = True         # Use percentage-based exits instead of absolute
    cooldown_secs: float = 15.0        # Seconds to wait after exiting before re-entering
    exit_before_close: int = 30        # Force exit this many seconds before market close
    max_open_positions: int = 2        # Max simultaneous scalp positions
    enabled: bool = False              # Master toggle


class ScalpState(Enum):
    """State for each tracked position."""
    WATCHING = "watching"        # Monitoring, looking for entry
    BUYING = "buying"            # Buy order placed, waiting for fill
    SETTLING = "settling"        # Buy filled, waiting for token settlement
    HOLDING = "holding"          # Position open, monitoring for exit
    SELLING = "selling"          # Sell order placed, waiting for fill
    EXITED = "exited"            # Position closed
    COOLDOWN = "cooldown"        # Waiting before next entry


@dataclass
class ScalpPosition:
    """One active scalp position."""
    market_id: str
    question: str
    token_id: str                     # Which token we bought (Up or Down)
    side: str                          # "UP" or "DOWN"
    entry_price: float = 0.0          # Price we bought at
    entry_time: float = 0.0
    size: int = 0                      # Number of shares bought
    order_id_buy: str = ""            # Buy order ID
    order_id_sell: str = ""           # Sell order ID
    state: ScalpState = ScalpState.WATCHING
    current_ask: float = 0.0          # Latest ask price from WS
    current_bid: float = 0.0          # Latest bid price from WS
    pnl: float = 0.0                  # Unrealised P&L
    exit_price: float = 0.0           # Price we sold at (0 if still open)
    exit_time: float = 0.0
    cooldown_until: float = 0.0
    end_time: Optional[datetime] = None   # Market close time
    error: str = ""
    sell_retry_after: float = 0.0         # Don't retry sell before this time
    sell_attempts: int = 0                 # Number of failed sell attempts
    fill_detected_time: float = 0.0        # When buy fill was first detected
    tokens_confirmed: bool = False         # Whether conditional tokens are in wallet
    original_size: int = 0                 # Shares we paid for (before fee reduction)
    _last_force_exit_log: float = 0.0     # Throttle force-exit log messages
    _last_profit_log: float = 0.0         # Throttle profit target log messages
    _last_stoploss_log: float = 0.0       # Throttle stop-loss log messages

    def to_dict(self) -> dict:
        return {
            "market_id": self.market_id,
            "question": self.question,
            "side": self.side,
            "state": self.state.value,
            "entry_price": round(self.entry_price, 4),
            "size": self.size,
            "current_bid": round(self.current_bid, 4),
            "current_ask": round(self.current_ask, 4),
            "pnl": round(self.pnl, 4),
            "exit_price": round(self.exit_price, 4),
            "secs_left": max(0, seconds_until(self.end_time)) if self.end_time else 0,
            "error": self.error,
        }


# ══════════════════════════════════════════════════════════════════════════════
#  SELL ORDER HELPER
# ══════════════════════════════════════════════════════════════════════════════

def check_token_balance(token_id: str) -> int:
    """
    Check how many conditional tokens we hold for a given token_id.
    Returns the RAW balance (6-decimal units), or 0 on error.
    To get human-readable shares: raw_balance / 1_000_000
    """
    from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

    if config.PAPER_TRADING:
        return 999_000_000  # Assume we have them in paper mode

    try:
        client = get_clob_client()
        params = BalanceAllowanceParams(
            asset_type=AssetType.CONDITIONAL,
            token_id=token_id,
        )
        result = client.get_balance_allowance(params)
        balance = int(result.get("balance", 0))
        return balance
    except Exception as e:
        log.warning(f"check_token_balance error: {e}")
        return 0


def raw_balance_to_shares(raw_balance: int) -> int:
    """Convert raw token balance (6 decimals) to whole shares."""
    return int(raw_balance / 1_000_000)


def place_limit_sell(token_id: str, price: float, size: int,
                     market_id: str = "") -> Optional[str]:
    """
    Place a limit SELL order on Polymarket's CLOB.
    Mirror of place_limit_buy but with side='SELL'.
    Verifies we actually hold the tokens before attempting.
    Retries up to 3 times with re-approval on allowance errors.
    """
    from py_clob_client.clob_types import OrderArgs, BalanceAllowanceParams, AssetType

    if config.PAPER_TRADING:
        fake_id = f"paper_sell_{int(time.time())}_{token_id[:8]}"
        log.info(
            f"[PAPER SELL] SELL {size} tokens of {token_id[:16]}... "
            f"@ ${price:.4f} (market={market_id})"
        )
        return fake_id

    if not config.TRADING_ENABLED:
        log.warning("Trading is disabled. Sell order not placed.")
        return None

    # Polymarket CLOB minimum order size = 5 shares
    MIN_ORDER_SIZE = 5
    if size < MIN_ORDER_SIZE:
        log.warning(
            f"[SELL SKIP] Size ({size}) below Polymarket minimum ({MIN_ORDER_SIZE}). "
            f"Cannot sell — shares stranded."
        )
        return None

    MAX_SELL_RETRIES = 3

    for attempt in range(1, MAX_SELL_RETRIES + 1):
        try:
            client = get_clob_client()

            # ── Verify we actually hold the tokens before selling ──
            try:
                params = BalanceAllowanceParams(
                    asset_type=AssetType.CONDITIONAL,
                    token_id=token_id,
                )
                bal_result = client.get_balance_allowance(params)
                raw_balance = int(bal_result.get("balance", 0))
                actual_shares = raw_balance_to_shares(raw_balance)
                if actual_shares < 1:
                    log.warning(
                        f"[SELL SKIP] Token balance={raw_balance} raw ({actual_shares} shares) — "
                        f"tokens not yet settled, will retry"
                    )
                    if attempt < MAX_SELL_RETRIES:
                        import time as _t; _t.sleep(2)
                        continue
                    return None
                if actual_shares < size:
                    log.info(
                        f"[SELL] Adjusting size {size}→{actual_shares} shares "
                        f"(raw balance={raw_balance}, fees reduced tokens)"
                    )
                    size = actual_shares
                log.info(f"[SELL] Token balance confirmed: {actual_shares} shares (raw={raw_balance})")
            except Exception as be:
                log.warning(f"Balance check error (proceeding anyway): {be}")

            # ── Update balance allowance so the CLOB recognises our tokens ──
            # This is CRITICAL — without it, sell orders fail with
            # "not enough balance / allowance" even when we hold the tokens.
            # We do this EVERY attempt to ensure the allowance is refreshed.
            try:
                client.update_balance_allowance(
                    BalanceAllowanceParams(
                        asset_type=AssetType.CONDITIONAL,
                        token_id=token_id,
                    )
                )
                log.info(f"[SELL] Allowance updated for token {token_id[:16]}...")
            except Exception as ae:
                log.warning(f"update_balance_allowance failed (attempt {attempt}): {ae}")
                # If allowance update fails, wait and retry
                if attempt < MAX_SELL_RETRIES:
                    import time as _t; _t.sleep(2)
                    continue

            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=float(size),
                side="SELL",
            )
            signed_order = client.create_order(order_args)
            response = client.post_order(signed_order, orderType="GTC")
            order_id = response.get("orderID") or response.get("id", "unknown")
            log.info(
                f"[LIVE SELL] SELL {size} tokens of {token_id[:16]}... "
                f"@ ${price:.4f} → order_id={order_id}"
            )
            return order_id

        except Exception as e:
            error_str = str(e)
            log.error(f"place_limit_sell error (attempt {attempt}/{MAX_SELL_RETRIES}): {e}")

            # If it's a balance/allowance error, retry with fresh allowance
            if "not enough balance" in error_str.lower() or "allowance" in error_str.lower():
                if attempt < MAX_SELL_RETRIES:
                    log.info(f"[SELL] Balance/allowance error — refreshing and retrying in 2s...")
                    try:
                        client = get_clob_client()
                        client.update_balance_allowance(
                            BalanceAllowanceParams(
                                asset_type=AssetType.CONDITIONAL,
                                token_id=token_id,
                            )
                        )
                    except Exception:
                        pass
                    import time as _t; _t.sleep(2)
                    continue

            # Non-retryable error or max retries reached
            return None

    log.error(f"place_limit_sell: all {MAX_SELL_RETRIES} attempts failed")
    return None


# ══════════════════════════════════════════════════════════════════════════════
#  SCALPER ENGINE
# ══════════════════════════════════════════════════════════════════════════════

class Scalper:
    """
    Autonomous scalping engine for Polymarket BTC 5-minute markets.
    Runs in its own thread, shares the BinanceFeed and PriceFeed
    from the main Engine.
    """

    def __init__(self, btc_feed: BinanceFeed, ws_feed: PriceFeed):
        self.btc_feed = btc_feed
        self.ws_feed = ws_feed
        self.cfg = ScalpConfig()

        # State
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self.positions: dict[str, ScalpPosition] = {}   # market_id → position
        self.history: deque = deque(maxlen=100)          # Completed trades
        self.activity_log: deque = deque(maxlen=50)

        # Stats
        self.total_trades: int = 0
        self.winning_trades: int = 0
        self.losing_trades: int = 0
        self.total_pnl: float = 0.0
        self.total_spent: float = 0.0
        self.total_revenue: float = 0.0
        self.daily_trades: int = 0
        self.daily_date: str = ""

        # Market tracking
        self.known_markets: dict[str, MarketWindow] = {}
        self.traded_markets: set[str] = set()  # Markets we already traded — no re-entry
        self.limit_bid_markets: set[str] = set()  # Markets with active limit bids (set by engine)

        # Control
        self._user_stopped = False  # True if user explicitly stopped from dashboard

        # SocketIO reference (set by web_dashboard after creation)
        self._socketio = None

    # ── Logging ──────────────────────────────────────────────────────────

    def add_log(self, msg: str, level: str = "info"):
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        entry = {"ts": ts, "msg": msg, "level": level}
        self.activity_log.appendleft(entry)
        log.info(f"[SCALPER] {msg}")
        if self._socketio:
            self._socketio.emit("scalper_log", entry)

    # ── Start / Stop ─────────────────────────────────────────────────────

    def start(self):
        if self.running:
            return
        self.running = True
        self.cfg.enabled = True
        self._user_stopped = False
        self._thread = threading.Thread(target=self._loop, daemon=True, name="scalper")
        self._thread.start()
        self.add_log("Scalper STARTED", "info")

    def stop(self, user_requested: bool = False):
        self.running = False
        self.cfg.enabled = False
        if user_requested:
            self._user_stopped = True
        self.add_log("Scalper STOPPED" + (" (by user)" if user_requested else ""), "warn")

    # ── Main Loop ────────────────────────────────────────────────────────

    def _loop(self):
        """Main scalper loop — runs every ~0.5s."""
        last_market_refresh = 0
        last_position_check = 0

        while self.running:
            try:
                now = time.time()

                # Reset daily counter
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                if today != self.daily_date:
                    self.daily_trades = 0
                    self.daily_date = today

                # Refresh markets every 8 seconds
                if now - last_market_refresh >= 8.0:
                    self._refresh_markets()
                    last_market_refresh = now

                # Check positions & look for entries every 1s
                if now - last_position_check >= 1.0:
                    self._scan_for_entries()
                    self._manage_positions()
                    last_position_check = now

                time.sleep(0.5)

            except Exception as e:
                self.add_log(f"Scalper loop error: {e}", "error")
                time.sleep(2)

        self.add_log("Scalper loop exited", "warn")

    # ── Market Discovery ─────────────────────────────────────────────────

    def _refresh_markets(self):
        """Fetch active BTC 5-min markets from Polymarket."""
        try:
            markets = fetch_active_btc_markets()
            for m in markets:
                if m.market_id not in self.known_markets:
                    self.known_markets[m.market_id] = m
                    # Subscribe WS feed to get live prices
                    self.ws_feed.subscribe(
                        m.token_id_up, m.token_id_down, m.market_id
                    )
                else:
                    # Update end_time etc.
                    self.known_markets[m.market_id] = m

            # Prune expired markets (closed > 5 min ago)
            to_remove = []
            for mid, m in self.known_markets.items():
                secs = seconds_until(m.end_time)
                if secs < -300:
                    to_remove.append(mid)
            for mid in to_remove:
                del self.known_markets[mid]
                self.traded_markets.discard(mid)  # Clean up traded set

        except Exception as e:
            self.add_log(f"Market refresh error: {e}", "error")

    # ── Entry Scanner ────────────────────────────────────────────────────

    def _scan_for_entries(self):
        """
        Compression / Indecision strategy — enter when BTC is hugging its
        candle open, the outcome is maximally uncertain, and tail contracts
        are cheap. Timing: primary window T-20s to T-2s before close.
        """
        if not self.cfg.enabled:
            return

        # Count open positions
        open_count = sum(
            1 for p in self.positions.values()
            if p.state in (ScalpState.BUYING, ScalpState.SETTLING, ScalpState.HOLDING, ScalpState.SELLING)
        )
        if open_count >= self.cfg.max_open_positions:
            return

        now_t = time.time()

        # ── Near-close activity logging — log every 5s when <60s remain ──
        do_close_log = (now_t - getattr(self, '_last_close_log', 0)) >= 5
        # Standard debug every 30s
        do_debug = (now_t - getattr(self, '_last_scan_debug', 0)) >= 30
        if do_debug:
            self._last_scan_debug = now_t

        for market_id, market in list(self.known_markets.items()):
            if market_id in self.positions:
                continue
            if market_id in self.traded_markets:
                continue
            if market_id in self.limit_bid_markets:
                continue  # Limit bid strategy already has bids on this market

            secs = seconds_until(market.end_time)

            # ═══ TIMING WINDOW ═══
            # Primary:   T-45s to T-5s  (sweet spot for compression entry)
            # Skip:      Too early (>45s) or too late (<5s for fill)
            if secs > 45 or secs < 5:
                continue

            # ═══ NEAR-CLOSE LOGGING ═══
            # Always log what we're evaluating in the last 60 seconds
            if secs <= 60 and do_close_log:
                self._last_close_log = now_t
                self._log_market_eval(market, secs)

            # ═══ EVALUATE ENTRY ═══
            result = self._evaluate_compression(market, secs)
            if not result:
                continue

            side, token_id, score, reasoning = result

            # Get live price for entry
            lp = self.ws_feed.get_price(token_id)
            if not lp or not lp.valid or lp.best_ask <= 0:
                continue

            best_ask = lp.best_ask
            best_bid = lp.best_bid

            # Price filter — only buy cheap tail contracts
            if best_ask > self.cfg.entry_ask_max or best_ask < 0.03:
                continue

            # Need valid spread
            if best_bid <= 0 or best_ask <= best_bid:
                continue
            spread = best_ask - best_bid
            if spread > 0.08:
                continue  # Too illiquid

            # Smart pricing
            if spread >= 0.03:
                buy_price = round(best_bid + 0.01, 2)
            elif spread >= 0.01:
                buy_price = best_ask
            else:
                buy_price = best_ask

            # Min 7 shares (fees eat ~2, Polymarket min sell = 5)
            shares = int(self.cfg.trade_size_usd / buy_price)
            if shares < 7:
                shares = 7

            self.add_log(
                f"SIGNAL: {side} score={score:.1f} T-{secs:.0f}s "
                f"ask=${best_ask:.3f} | {reasoning}",
                "info"
            )
            self._enter_position(market, side, token_id, buy_price, shares)

    def _log_market_eval(self, market, secs: float):
        """Log detailed evaluation of a market near close for visibility."""
        bf = self.btc_feed
        if not bf or bf.price <= 0:
            return

        btc_move = bf.price - bf.candle_open if bf.candle_open > 0 else 0
        dist_pct = (bf.distance / bf.price * 100) if bf.price > 0 else 99

        lp_up = self.ws_feed.get_price(market.token_id_up)
        lp_dn = self.ws_feed.get_price(market.token_id_down)

        up_ask = lp_up.best_ask if lp_up and lp_up.valid else 0
        dn_ask = lp_dn.best_ask if lp_dn and lp_dn.valid else 0

        self.add_log(
            f"T-{secs:.0f}s EVAL: {market.question[:35]} | "
            f"BTC {btc_move:+.1f} ({dist_pct:.3f}%) range=${bf.candle_range:.1f} "
            f"prior=${bf.prior_candle_range:.1f} | "
            f"UP=${up_ask:.3f} DN=${dn_ask:.3f}",
            "info"
        )

    def _evaluate_compression(self, market, secs: float):
        """
        Compression / Indecision entry scoring.

        Core idea: When BTC price is hugging the candle open near market
        close, the outcome is a coin flip. Both tail contracts are cheap.
        Buy the cheaper side — if it wins, shares pay $1.00.

        Returns (side, token_id, score, reasoning) or None.
        """
        bf = self.btc_feed
        if not bf or bf.price <= 0 or bf.candle_open <= 0:
            return None

        btc_move = bf.price - bf.candle_open
        abs_move = abs(btc_move)
        dist_pct = abs_move / bf.price  # % distance from open

        # ═══════════════════════════════════════════════════════════════
        #  DO NOT ENTER GUARDS — hard blocks, any one kills the trade
        # ═══════════════════════════════════════════════════════════════

        # Guard 1: BTC too far from open (>0.06% = directional, not indecision)
        if dist_pct > 0.0006:
            return None

        # Guard 2: Large current candle (momentum, not compression)
        if bf.candle_range > 40:
            return None

        # Guard 3: Check both sides have price data
        lp_up = self.ws_feed.get_price(market.token_id_up)
        lp_dn = self.ws_feed.get_price(market.token_id_down)
        if not lp_up or not lp_up.valid or not lp_dn or not lp_dn.valid:
            return None
        if lp_up.best_ask <= 0 or lp_dn.best_ask <= 0:
            return None

        up_ask = lp_up.best_ask
        dn_ask = lp_dn.best_ask

        # Guard 4: Either tail price too expensive (>0.65 = market already decided)
        if up_ask > 0.65 or dn_ask > 0.65:
            return None

        # Guard 5: Combined ask too high (>1.15 = too much vig, bad pricing)
        combined = up_ask + dn_ask
        if combined > 1.15:
            return None

        # Guard 6: Momentum accelerating (price running away from open)
        # Check if BTC has been moving AWAY from open in last 10s
        if bf.candle_range > 0 and bf.prior_candle_range > 0:
            range_expansion = bf.candle_range / bf.prior_candle_range
            if range_expansion > 1.5 and abs_move > 20:
                return None  # Range blowing out = volatility, skip

        # ═══════════════════════════════════════════════════════════════
        #  PICK SIDE — buy the cheaper (more uncertain) tail
        # ═══════════════════════════════════════════════════════════════

        # Primary: buy the cheaper side (higher uncertainty = higher payoff)
        if up_ask < dn_ask:
            side, token_id = "UP", market.token_id_up
            our_ask, other_ask = up_ask, dn_ask
            lp = lp_up
        elif dn_ask < up_ask:
            side, token_id = "DOWN", market.token_id_down
            our_ask, other_ask = dn_ask, up_ask
            lp = lp_dn
        else:
            # Same price — use BTC direction as tiebreaker
            if btc_move >= 0:
                side, token_id = "UP", market.token_id_up
                our_ask, other_ask = up_ask, dn_ask
                lp = lp_up
            else:
                side, token_id = "DOWN", market.token_id_down
                our_ask, other_ask = dn_ask, up_ask
                lp = lp_dn

        # ═══════════════════════════════════════════════════════════════
        #  SCORING — how good is this compression setup?
        # ═══════════════════════════════════════════════════════════════
        score = 0.0
        reasons = []

        # S1: BTC compression quality (distance from open as % of price)
        # Elite: <0.01% (+3.0), Strong: <0.02% (+2.0), OK: <0.04% (+1.0)
        if dist_pct < 0.0001:
            score += 3.0
            reasons.append("elite_compress")
        elif dist_pct < 0.0002:
            score += 2.0
            reasons.append("strong_compress")
        elif dist_pct < 0.0004:
            score += 1.0
            reasons.append("ok_compress")
        else:
            score += 0.3
            reasons.append("marginal_compress")

        # S2: Tail contract cheapness (our side)
        # $0.05=+3.0, $0.15=+2.0, $0.30=+1.0, $0.45=0, $0.50+=-1.0
        cheapness = max(min((0.45 - our_ask) * 6.0, 3.0), -2.0)
        score += cheapness
        if cheapness >= 2.0:
            reasons.append(f"very_cheap@{our_ask:.2f}")
        elif cheapness >= 1.0:
            reasons.append(f"cheap@{our_ask:.2f}")

        # S3: Price symmetry (both sides similarly priced = max uncertainty)
        # |up-dn| < 0.05 = perfect uncertainty (+1.5)
        ask_diff = abs(up_ask - dn_ask)
        if ask_diff <= 0.05:
            score += 1.5
            reasons.append("symmetric")
        elif ask_diff <= 0.10:
            score += 0.8
            reasons.append("semi_symmetric")
        elif ask_diff <= 0.20:
            score += 0.2
        else:
            score -= 0.5  # Market has already picked a side

        # S4: Combined ask discount (< 1.0 = FREE MONEY, < 1.05 = great)
        if combined < 1.0:
            score += 2.5
            reasons.append(f"arb_combined={combined:.2f}")
        elif combined < 1.03:
            score += 1.5
            reasons.append(f"tight_vig={combined:.2f}")
        elif combined < 1.08:
            score += 0.5

        # S5: Candle compression (current range vs prior range)
        if bf.prior_candle_range > 0:
            compression_ratio = bf.candle_range / bf.prior_candle_range
            if compression_ratio < 0.3:
                score += 1.5
                reasons.append("extreme_candle_compress")
            elif compression_ratio < 0.5:
                score += 1.0
                reasons.append("candle_compressing")
            elif compression_ratio < 0.8:
                score += 0.3

        # S6: Timing bonus (later = better, price has formed)
        if secs <= 15:
            score += 1.0
            reasons.append(f"T-{secs:.0f}s_prime")
        elif secs <= 25:
            score += 0.5
            reasons.append(f"T-{secs:.0f}s_good")

        # S7: Flow confirmation (order book supporting our side)
        flow = lp.flow_score(secs=20.0)
        if flow > 0.3:
            score += 0.8
            reasons.append("flow_confirm")
        elif flow < -0.5:
            score -= 0.5

        # S8: Minimum price history
        if len(lp.history) < 3:
            score -= 5.0

        # ═══════════════════════════════════════════════════════════════
        #  THRESHOLD — need minimum score to enter
        # ═══════════════════════════════════════════════════════════════
        MIN_COMPRESSION_SCORE = 4.0
        if score < MIN_COMPRESSION_SCORE:
            return None

        reasoning = " ".join(reasons[:4])  # Cap at 4 reasons for log readability
        return side, token_id, score, reasoning

    # ── Position Management ──────────────────────────────────────────────

    def _enter_position(self, market: MarketWindow, side: str, token_id: str,
                        ask_price: float, shares: int):
        """Place a buy order to enter a scalp position."""
        self.add_log(
            f"ENTRY: {side} {shares} shares @ ${ask_price:.3f} on {market.question[:40]}",
            "trade"
        )

        order_id = place_limit_buy(
            token_id=token_id,
            price=ask_price,
            size=shares,
            market_id=market.market_id,
        )

        if not order_id:
            self.add_log(f"Buy order FAILED for {side}", "error")
            return

        pos = ScalpPosition(
            market_id=market.market_id,
            question=market.question,
            token_id=token_id,
            side=side,
            entry_price=ask_price,
            entry_time=time.time(),
            size=shares,
            original_size=shares,
            order_id_buy=order_id,
            state=ScalpState.BUYING,
            end_time=market.end_time,
        )
        self.positions[market.market_id] = pos
        self.total_spent += ask_price * shares

    def _manage_positions(self):
        """Check all open positions for fills, profit targets, stop losses."""
        to_remove = []

        for market_id, pos in list(self.positions.items()):
            try:
                secs = seconds_until(pos.end_time) if pos.end_time else 999

                if pos.state == ScalpState.BUYING:
                    self._check_buy_fill(pos)

                elif pos.state == ScalpState.SETTLING:
                    self._check_settlement(pos)

                elif pos.state == ScalpState.HOLDING:
                    self._check_exit_conditions(pos, secs)

                elif pos.state == ScalpState.SELLING:
                    self._check_sell_fill(pos)

                elif pos.state == ScalpState.COOLDOWN:
                    if time.time() >= pos.cooldown_until:
                        to_remove.append(market_id)

                elif pos.state == ScalpState.EXITED:
                    to_remove.append(market_id)

                # Update live prices
                if pos.state in (ScalpState.HOLDING, ScalpState.BUYING, ScalpState.SETTLING):
                    lp = self.ws_feed.get_price(pos.token_id)
                    if lp and lp.valid:
                        pos.current_ask = lp.best_ask
                        pos.current_bid = lp.best_bid
                        if pos.state in (ScalpState.HOLDING, ScalpState.SETTLING) and pos.entry_price > 0:
                            pos.pnl = (lp.best_bid - pos.entry_price) * pos.size

            except Exception as e:
                self.add_log(f"Position manage error ({market_id}): {e}", "error")

        for mid in to_remove:
            if mid in self.positions:
                self.history.appendleft(self.positions[mid].to_dict())
                del self.positions[mid]

    def _check_buy_fill(self, pos: ScalpPosition):
        """Check if our buy order has been filled."""
        if config.PAPER_TRADING:
            # Paper mode: assume fill after 2 seconds
            if time.time() - pos.entry_time > 2.0:
                pos.state = ScalpState.HOLDING
                pos.tokens_confirmed = True
                self.add_log(
                    f"[PAPER] BUY FILLED: {pos.side} {pos.size} @ ${pos.entry_price:.3f}",
                    "fill"
                )
            return

        # Live mode: check order status
        try:
            status = get_order_status(pos.order_id_buy)
            matched = float(status.get("size_matched", 0))
            order_status = status.get("status", "").upper()

            if matched >= pos.size or order_status == "MATCHED":
                if matched > 0 and matched != pos.size:
                    pos.size = int(matched)  # Partial fill
                pos.fill_detected_time = time.time()
                pos.state = ScalpState.SETTLING
                self.add_log(
                    f"BUY FILLED: {pos.side} {pos.size} @ ${pos.entry_price:.3f} — waiting for settlement",
                    "fill"
                )
            elif order_status in ("CANCELLED", "EXPIRED"):
                pos.state = ScalpState.EXITED
                pos.error = f"Buy order {order_status}"
                cost = pos.entry_price * pos.size
                self.total_spent -= cost  # Refund since not filled
                self.add_log(f"Buy order {order_status} for {pos.side}", "warn")

            # Timeout — cancel if not filled within 15s
            elif time.time() - pos.entry_time > 15:
                cancel_order(pos.order_id_buy)
                pos.state = ScalpState.EXITED
                pos.error = "Buy timeout"
                cost = pos.entry_price * pos.size
                self.total_spent -= cost
                self.add_log(f"Buy TIMEOUT, cancelled for {pos.side}", "warn")

        except Exception as e:
            self.add_log(f"Buy fill check error: {e}", "error")

    def _check_settlement(self, pos: ScalpPosition):
        """Wait for conditional tokens to appear in wallet after buy fill."""
        if config.PAPER_TRADING:
            pos.state = ScalpState.HOLDING
            pos.tokens_confirmed = True
            return

        elapsed = time.time() - pos.fill_detected_time

        # Check token balance (returns raw 6-decimal units)
        raw_balance = check_token_balance(pos.token_id)
        actual_shares = raw_balance_to_shares(raw_balance)
        if actual_shares >= 1:  # We have at least 1 whole share
            # Adjust pos.size to what we actually received (fees reduce tokens)
            if actual_shares < pos.size:
                log.info(
                    f"[SETTLE] Received {actual_shares} shares (requested {pos.size}) — "
                    f"fees reduced tokens. Adjusting position size."
                )
                pos.size = actual_shares
            pos.state = ScalpState.HOLDING
            pos.tokens_confirmed = True
            self.add_log(
                f"TOKENS CONFIRMED: {pos.side} balance={actual_shares} shares "
                f"(raw={raw_balance}, took {elapsed:.1f}s)",
                "fill"
            )
            return

        # Log progress every 5 seconds
        if int(elapsed) % 5 == 0 and int(elapsed) > 0:
            self.add_log(
                f"Waiting for settlement: {pos.side} balance={actual_shares}/{pos.size} shares ({elapsed:.0f}s)",
                "info"
            )

        # Timeout after 60 seconds — force to HOLDING anyway (tokens might be
        # visible to the exchange but not the balance API yet)
        if elapsed > 60:
            pos.state = ScalpState.HOLDING
            pos.tokens_confirmed = False
            self.add_log(
                f"Settlement TIMEOUT ({elapsed:.0f}s) — proceeding with sell anyway",
                "warn"
            )

    def _check_exit_conditions(self, pos: ScalpPosition, secs_left: float):
        """Determine if we should sell our position."""
        # Force exit near market close
        if secs_left <= self.cfg.exit_before_close:
            # Only log once per 10 seconds during force-exit attempts
            now_fe = time.time()
            last_fe = getattr(pos, '_last_force_exit_log', 0)
            if now_fe - last_fe >= 10:
                pos._last_force_exit_log = now_fe
                self.add_log(
                    f"FORCE EXIT (close in {secs_left:.0f}s): {pos.side}",
                    "warn"
                )
            self._sell_position(pos, reason="close")
            return

        # Check using bid price (what we can actually sell at)
        if pos.current_bid <= 0 or pos.entry_price <= 0:
            return

        price_diff = pos.current_bid - pos.entry_price
        pct_change = price_diff / pos.entry_price  # e.g. +0.25 = +25%

        # Calculate effective thresholds (percentage OR absolute, whichever gives more room)
        if self.cfg.use_pct_exits:
            # Percentage-based: scale with entry price
            # e.g. entry=$0.35 × 25% = $0.0875 profit target
            #      entry=$0.35 × 20% = $0.07 stop loss
            effective_profit = max(
                pos.entry_price * self.cfg.profit_target_pct,
                self.cfg.profit_target  # Never below absolute minimum
            )
            effective_stop = max(
                pos.entry_price * self.cfg.stop_loss_pct,
                self.cfg.stop_loss  # Never below absolute minimum
            )
        else:
            effective_profit = self.cfg.profit_target
            effective_stop = self.cfg.stop_loss

        # Profit target hit
        if price_diff >= effective_profit:
            # Only log once per 10 seconds during sell retries
            now_pt = time.time()
            last_pt = getattr(pos, '_last_profit_log', 0)
            if now_pt - last_pt >= 10:
                pos._last_profit_log = now_pt
                self.add_log(
                    f"PROFIT TARGET: {pos.side} +${price_diff:.3f}/share ({pct_change:+.0%}) "
                    f"(bid=${pos.current_bid:.3f}, entry=${pos.entry_price:.3f}, "
                    f"target=${effective_profit:.3f})",
                    "profit"
                )
            self._sell_position(pos, reason="profit")
            return

        # Stop loss hit
        if price_diff <= -effective_stop:
            # Only log once per 10 seconds during sell retries
            now_sl = time.time()
            last_sl = getattr(pos, '_last_stoploss_log', 0)
            if now_sl - last_sl >= 10:
                pos._last_stoploss_log = now_sl
                self.add_log(
                    f"STOP LOSS: {pos.side} -${abs(price_diff):.3f}/share ({pct_change:+.0%}) "
                    f"(bid=${pos.current_bid:.3f}, entry=${pos.entry_price:.3f}, "
                    f"stop=${effective_stop:.3f})",
                    "error"
                )
            self._sell_position(pos, reason="stop_loss")
            return

    def _sell_position(self, pos: ScalpPosition, reason: str = "manual"):
        """Place a sell order to exit the position."""
        # Throttle retries — don't spam sell attempts
        if time.time() < pos.sell_retry_after:
            return

        # Determine sell price — use current bid for fast fill
        sell_price = pos.current_bid if pos.current_bid > 0 else pos.entry_price

        # For force-exit near close, sell aggressively at lower price
        if reason == "close":
            sell_price = max(sell_price - 0.02, 0.01)  # Drop 2¢ for fast fill

        order_id = place_limit_sell(
            token_id=pos.token_id,
            price=sell_price,
            size=pos.size,
            market_id=pos.market_id,
        )

        if order_id:
            pos.order_id_sell = order_id
            pos.state = ScalpState.SELLING
            pos.exit_price = sell_price
            pos.sell_attempts = 0
            self.add_log(
                f"SELL placed: {pos.side} {pos.size} @ ${sell_price:.3f} ({reason})",
                "trade"
            )
        else:
            pos.sell_attempts += 1
            # Exponential backoff: 3s, 6s, 12s, max 30s
            delay = min(3 * (2 ** (pos.sell_attempts - 1)), 30)
            pos.sell_retry_after = time.time() + delay
            pos.error = f"Sell failed (attempt {pos.sell_attempts}, retry in {delay}s)"
            self.add_log(
                f"SELL FAILED for {pos.side} ({reason}), attempt {pos.sell_attempts}, retry in {delay}s",
                "error"
            )

    def _check_sell_fill(self, pos: ScalpPosition):
        """Check if our sell order has been filled."""
        if config.PAPER_TRADING:
            # Paper mode: assume fill after 2 seconds
            if time.time() - pos.exit_time > 0 or (
                pos.exit_price > 0 and time.time() - pos.entry_time > 5
            ):
                self._finalize_exit(pos)
            return

        # Live mode
        try:
            status = get_order_status(pos.order_id_sell)
            matched = float(status.get("size_matched", 0))
            order_status = status.get("status", "").upper()

            if matched >= pos.size or order_status == "MATCHED":
                self._finalize_exit(pos)
            elif order_status in ("CANCELLED", "EXPIRED"):
                # Sell cancelled — still holding, try again
                pos.state = ScalpState.HOLDING
                pos.order_id_sell = ""
                self.add_log(f"Sell order {order_status} — retrying", "warn")
            elif time.time() - (pos.exit_time or pos.entry_time) > 20:
                # Sell timeout — lower price and retry
                cancel_order(pos.order_id_sell)
                pos.state = ScalpState.HOLDING
                pos.order_id_sell = ""
                self.add_log("Sell timeout — will retry at lower price", "warn")

        except Exception as e:
            self.add_log(f"Sell fill check error: {e}", "error")

    def _finalize_exit(self, pos: ScalpPosition):
        """Record the completed trade and enter cooldown."""
        pos.exit_time = time.time()

        # Use original_size for TRUE cost (fees reduce received tokens)
        orig = pos.original_size if pos.original_size > 0 else pos.size
        cost = pos.entry_price * orig        # What we actually PAID
        revenue = pos.exit_price * pos.size  # What we actually GOT (fewer shares)
        pnl = revenue - cost
        pos.pnl = pnl

        fee_shares = orig - pos.size
        fee_note = f", fees ate {fee_shares} shares" if fee_shares > 0 else ""

        self.total_trades += 1
        self.daily_trades += 1
        self.total_revenue += revenue
        self.total_pnl += pnl

        if pnl >= 0:
            self.winning_trades += 1
            self.add_log(
                f"SCALP WIN: {pos.side} +${pnl:.2f} "
                f"(${pos.entry_price:.3f} → ${pos.exit_price:.3f}, "
                f"bought {orig} sold {pos.size}{fee_note})",
                "profit"
            )
        else:
            self.losing_trades += 1
            self.add_log(
                f"SCALP LOSS: {pos.side} -${abs(pnl):.2f} "
                f"(${pos.entry_price:.3f} → ${pos.exit_price:.3f}, "
                f"bought {orig} sold {pos.size}{fee_note})",
                "error"
            )

        # Enter cooldown — keep in self.positions to block re-entry during cooldown
        pos.state = ScalpState.COOLDOWN
        pos.cooldown_until = time.time() + self.cfg.cooldown_secs

        # Also mark this market as traded — permanent block for this window
        self.traded_markets.add(pos.market_id)

    # ── Stats ────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        """Return scalper stats for the dashboard."""
        win_rate = (
            round(self.winning_trades / self.total_trades * 100, 1)
            if self.total_trades > 0 else 0.0
        )
        positions_list = [p.to_dict() for p in self.positions.values()]
        history_list = list(self.history)[:20]

        return {
            "enabled": self.cfg.enabled,
            "running": self.running,
            "trade_size_usd": self.cfg.trade_size_usd,
            "entry_ask_max": self.cfg.entry_ask_max,
            "profit_target": self.cfg.profit_target,
            "stop_loss": self.cfg.stop_loss,
            "stop_loss_pct": self.cfg.stop_loss_pct,
            "profit_target_pct": self.cfg.profit_target_pct,
            "use_pct_exits": self.cfg.use_pct_exits,
            "cooldown_secs": self.cfg.cooldown_secs,
            "exit_before_close": self.cfg.exit_before_close,
            "max_open_positions": self.cfg.max_open_positions,
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "win_rate": win_rate,
            "total_pnl": round(self.total_pnl, 2),
            "total_spent": round(self.total_spent, 2),
            "total_revenue": round(self.total_revenue, 2),
            "daily_trades": self.daily_trades,
            "positions": positions_list,
            "history": history_list,
            "activity": list(self.activity_log)[:30],
            "markets_tracked": len(self.known_markets),
        }

    def update_config(self, data: dict) -> list:
        """
        Update scalper config from dashboard.
        Returns list of change descriptions.
        """
        changes = []

        if "trade_size_usd" in data:
            val = float(data["trade_size_usd"])
            if 0.5 <= val <= 100:
                if val != self.cfg.trade_size_usd:
                    changes.append(f"Trade Size ${self.cfg.trade_size_usd:.2f}→${val:.2f}")
                self.cfg.trade_size_usd = val

        if "entry_ask_max" in data:
            val = float(data["entry_ask_max"])
            if 0.10 <= val <= 0.90:
                if val != self.cfg.entry_ask_max:
                    changes.append(f"Max Entry ${self.cfg.entry_ask_max:.2f}→${val:.2f}")
                self.cfg.entry_ask_max = val

        if "profit_target" in data:
            val = float(data["profit_target"])
            if 0.005 <= val <= 0.50:
                if val != self.cfg.profit_target:
                    changes.append(f"Profit ${self.cfg.profit_target:.3f}→${val:.3f}")
                self.cfg.profit_target = val

        if "stop_loss" in data:
            val = float(data["stop_loss"])
            if 0.005 <= val <= 0.50:
                if val != self.cfg.stop_loss:
                    changes.append(f"Stop ${self.cfg.stop_loss:.3f}→${val:.3f}")
                self.cfg.stop_loss = val

        if "cooldown_secs" in data:
            val = float(data["cooldown_secs"])
            if 0 <= val <= 300:
                if val != self.cfg.cooldown_secs:
                    changes.append(f"Cooldown {self.cfg.cooldown_secs:.0f}s→{val:.0f}s")
                self.cfg.cooldown_secs = val

        if "exit_before_close" in data:
            val = int(data["exit_before_close"])
            if 5 <= val <= 120:
                if val != self.cfg.exit_before_close:
                    changes.append(f"Exit Before {self.cfg.exit_before_close}s→{val}s")
                self.cfg.exit_before_close = val

        if "max_open_positions" in data:
            val = int(data["max_open_positions"])
            if 1 <= val <= 10:
                if val != self.cfg.max_open_positions:
                    changes.append(f"Max Pos {self.cfg.max_open_positions}→{val}")
                self.cfg.max_open_positions = val

        if "stop_loss_pct" in data:
            val = float(data["stop_loss_pct"])
            if 0.05 <= val <= 0.80:
                if val != self.cfg.stop_loss_pct:
                    changes.append(f"Stop% {self.cfg.stop_loss_pct*100:.0f}%→{val*100:.0f}%")
                self.cfg.stop_loss_pct = val

        if "profit_target_pct" in data:
            val = float(data["profit_target_pct"])
            if 0.05 <= val <= 0.80:
                if val != self.cfg.profit_target_pct:
                    changes.append(f"Profit% {self.cfg.profit_target_pct*100:.0f}%→{val*100:.0f}%")
                self.cfg.profit_target_pct = val

        if "use_pct_exits" in data:
            val = bool(data["use_pct_exits"])
            if val != self.cfg.use_pct_exits:
                changes.append(f"Pct Exits {'ON' if val else 'OFF'}")
            self.cfg.use_pct_exits = val

        return changes
