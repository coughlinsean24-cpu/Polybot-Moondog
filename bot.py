"""
Polybot Snipez — Main Bot
BTC 5-Minute Market Limit-Bid Bot for Polymarket

Strategy: Post low-ball limit BUY orders on BOTH the Up and Down sides of
BTC 5-minute markets at ~$0.05-$0.15 per share. When BTC is close to the
strike price near expiry, panicked traders sell into our bids. One side
always pays $1.00 at settlement. Profit = $1.00 × shares − total cost.

Example: 200 shares × $0.10 × 2 sides = $40 total.
If both fill → one side pays $200. Net +$160.
If one fills → $20 spent. Pays $200 or $0 (50/50 gamble at $0.10).

ALL price data comes EXCLUSIVELY from Polymarket's own CLOB orderbook.
No external BTC price feeds. No direction prediction.
"""

import time
import signal
import sys
import threading
from datetime import datetime, timezone, timedelta
from collections import defaultdict

import config
from logger import (
    log, log_signal, log_trade, log_fire_pair,
    log_outcome, log_cancel, log_error, log_heartbeat,
)
from polymarket_client import (
    MarketWindow, OrderBookSnapshot,
    get_clob_client, fetch_active_btc_markets,
    fetch_orderbook, fetch_orderbooks_parallel,
    place_limit_buy, cancel_order, get_order_status,
    seconds_until, get_adaptive_poll_interval,
)
from dashboard import (
    dash_state, dash_log, dash_market_update, dash_trade,
    dash_bids_posted, dash_fill, start_dashboard, stop_dashboard,
)
from ws_feed import PriceFeed
from data_recorder import recorder as data_rec


# ── Bot State ───────────────────────────────────────────────────────────────

class BidRecord:
    """Tracks a pair of limit-buy orders for one market."""
    def __init__(self, market_id: str, question: str):
        self.market_id = market_id
        self.question = question
        self.order_id_up: str | None = None
        self.order_id_down: str | None = None
        self.posted_at: float = 0.0     # epoch when bids were placed
        self.bid_price: float = 0.0     # price we bid
        self.tokens: int = 0            # shares per side

        # Fill tracking
        self.up_filled: bool = False
        self.down_filled: bool = False
        self.up_fill_size: float = 0.0
        self.down_fill_size: float = 0.0

        # Lifecycle
        self.cancelled: bool = False


class BotState:
    """Tracks all bot runtime state."""

    def __init__(self):
        # Markets currently being watched
        self.watch_list: dict[str, MarketWindow] = {}

        # Markets where we have already posted bids (market_id → BidRecord)
        self.bids_posted: dict[str, BidRecord] = {}

        # Daily bid counter (resets at midnight UTC)
        self.bids_today: int = 0
        self.bid_date: str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Cancellation timers
        self.cancel_timers: dict[str, threading.Timer] = {}

        # Running flag
        self.running: bool = True

        # WebSocket price feed
        self.ws_feed: PriceFeed = PriceFeed()

    def reset_daily_counter(self):
        """Reset daily bid count if date has changed."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self.bid_date:
            log.info(f"New day: {today}. Resetting daily bid counter (was {self.bids_today}).")
            self.bids_today = 0
            self.bid_date = today


state = BotState()


# ── Signal Handling ─────────────────────────────────────────────────────────

def shutdown_handler(signum, frame):
    """Graceful shutdown on Ctrl+C."""
    log.info("Shutdown signal received. Cleaning up...")
    state.running = False
    dash_state.update(status="STOPPED")
    dash_log("Shutdown signal received -- cleaning up...", style="bold red")

    # Flush & stop data recorder
    data_rec.flush()

    # Stop the WebSocket feed
    state.ws_feed.stop()

    # Stop the dashboard
    stop_dashboard()

    # Cancel all pending timers
    for timer in state.cancel_timers.values():
        timer.cancel()

    # Cancel unfilled orders for all active bids
    for mid, bid in state.bids_posted.items():
        for oid in [bid.order_id_up, bid.order_id_down]:
            if oid and not bid.cancelled:
                cancel_order(oid)
                log_cancel(mid, oid, "shutdown")

    log.info("Shutdown complete.")
    sys.exit(0)


signal.signal(signal.SIGINT, shutdown_handler)
signal.signal(signal.SIGTERM, shutdown_handler)


# ── Core Logic: Post Limit Bids on Both Sides ──────────────────────────────

def post_bids(market: MarketWindow) -> bool:
    """
    Post low-ball limit BUY orders on BOTH Up and Down sides.

    Called once per market when it enters the bid window.
    Returns True if bids were successfully posted, False otherwise.
    """

    # ── Already posted? ────────────────────────────────────────────────────
    if market.market_id in state.bids_posted:
        return False

    # ── Time check ─────────────────────────────────────────────────────────
    secs = seconds_until(market.end_time)

    if secs > config.BID_WINDOW_OPEN:
        return False  # Too early
    if secs < config.BID_WINDOW_CLOSE:
        log.debug(f"Too late to post bids for {market.market_id} ({secs:.0f}s left)")
        return False

    # ── Daily limit check ──────────────────────────────────────────────────
    if config.MAX_BIDS_PER_DAY > 0 and state.bids_today >= config.MAX_BIDS_PER_DAY:
        log.debug(f"Daily bid limit reached ({state.bids_today})")
        return False

    # ── Risk check ─────────────────────────────────────────────────────────
    tokens = config.TOKENS_PER_SIDE
    price = config.BID_PRICE
    total_cost = price * tokens * 2  # both sides
    if total_cost > config.MAX_RISK_PER_MARKET:
        # Scale down tokens to fit risk limit
        tokens = int(config.MAX_RISK_PER_MARKET / (price * 2))
        if tokens < 10:
            log.warning(f"Risk limit prevents bidding on {market.market_id}")
            return False
        total_cost = price * tokens * 2
        log.info(f"Reduced tokens to {tokens}/side to fit ${config.MAX_RISK_PER_MARKET} risk cap")

    # ═══════════════════════════════════════════════════════════════════════
    # POST BIDS ON BOTH SIDES
    # ═══════════════════════════════════════════════════════════════════════

    dash_state.update(status="BIDDING")

    log.info(
        f"POSTING BIDS: {market.question}  "
        f"${price} x {tokens} shares on EACH side  "
        f"(total cost if both fill: ${total_cost:.2f})  "
        f"[{secs:.0f}s remaining]"
    )

    bid = BidRecord(market.market_id, market.question)
    bid.bid_price = price
    bid.tokens = tokens
    bid.posted_at = time.time()

    # Place limit BUY on Up side
    bid.order_id_up = place_limit_buy(
        token_id=market.token_id_up,
        price=price,
        size=tokens,
        market_id=market.market_id,
    )

    # Place limit BUY on Down side
    bid.order_id_down = place_limit_buy(
        token_id=market.token_id_down,
        price=price,
        size=tokens,
        market_id=market.market_id,
    )

    if not bid.order_id_up and not bid.order_id_down:
        log.error(f"BOTH bid placements failed for {market.market_id}")
        dash_state.update(status="RUNNING")
        return False

    # Record in state
    state.bids_posted[market.market_id] = bid
    market.fired = True
    state.bids_today += 1

    # Log to files
    if bid.order_id_up:
        log_trade(market.market_id, "UP", market.token_id_up,
                  price, tokens, bid.order_id_up,
                  secs, paper=config.PAPER_TRADING)
        dash_trade(market.question, "UP", price, tokens)

    if bid.order_id_down:
        log_trade(market.market_id, "DOWN", market.token_id_down,
                  price, tokens, bid.order_id_down,
                  secs, paper=config.PAPER_TRADING)
        dash_trade(market.question, "DOWN", price, tokens)

    # Dashboard bid-posting event
    dash_bids_posted(market.market_id, price, tokens)

    # Schedule cancellation of unfilled orders after market close
    schedule_cancel(market)

    dash_log(
        f"BIDS POSTED: {market.question}  ${price} x {tokens}/side  "
        f"[{secs:.0f}s left]",
        style="bold green",
    )

    dash_state.update(status="RUNNING")
    return True


def check_fills():
    """
    Check all posted bids for fill status.
    Called every cycle to detect fills as fast as possible.
    """
    for mid, bid in list(state.bids_posted.items()):
        if bid.cancelled:
            continue

        # Check Up side
        if bid.order_id_up and not bid.up_filled:
            status = get_order_status(bid.order_id_up)
            if status["status"] in ("matched", "partial"):
                bid.up_filled = True
                bid.up_fill_size = status["size_matched"]
                fill_cost = bid.bid_price * bid.up_fill_size
                log.info(
                    f"FILL: UP side of {bid.question}  "
                    f"{bid.up_fill_size:.0f} shares @ ${bid.bid_price}  "
                    f"(cost: ${fill_cost:.2f})"
                )
                dash_log(
                    f"FILLED: UP {bid.up_fill_size:.0f} sh @ ${bid.bid_price}  "
                    f"{bid.question}",
                    style="bold green",
                )

        # Check Down side
        if bid.order_id_down and not bid.down_filled:
            status = get_order_status(bid.order_id_down)
            if status["status"] in ("matched", "partial"):
                bid.down_filled = True
                bid.down_fill_size = status["size_matched"]
                fill_cost = bid.bid_price * bid.down_fill_size
                log.info(
                    f"FILL: DOWN side of {bid.question}  "
                    f"{bid.down_fill_size:.0f} shares @ ${bid.bid_price}  "
                    f"(cost: ${fill_cost:.2f})"
                )
                dash_log(
                    f"FILLED: DOWN {bid.down_fill_size:.0f} sh @ ${bid.bid_price}  "
                    f"{bid.question}",
                    style="bold green",
                )

        # If both filled — guaranteed profit!
        if bid.up_filled and bid.down_filled:
            total_cost = (bid.bid_price * bid.up_fill_size) + (bid.bid_price * bid.down_fill_size)
            payout = max(bid.up_fill_size, bid.down_fill_size) * 1.0
            profit = payout - total_cost
            log.info(
                f"BOTH SIDES FILLED: {bid.question}  "
                f"Cost=${total_cost:.2f}  Payout=${payout:.2f}  "
                f"PROFIT=${profit:.2f}"
            )
            dash_log(
                f"BOTH FILLED! {bid.question}  "
                f"Profit: ${profit:.2f}",
                style="bold yellow",
            )
            dash_state.record_payout(payout)


# ── Post-Market Cancellation ────────────────────────────────────────────────

def schedule_cancel(market: MarketWindow):
    """Schedule cancellation of unfilled orders after market.end_time + CANCEL_DELAY."""
    secs_until_cancel = seconds_until(market.end_time) + config.CANCEL_DELAY_AFTER_CLOSE

    if secs_until_cancel < 0:
        secs_until_cancel = 5  # Market already closed, cancel soon

    def do_cancel():
        bid = state.bids_posted.get(market.market_id)
        if not bid or bid.cancelled:
            return

        cancelled_count = 0
        # Cancel Up if not filled
        if bid.order_id_up and not bid.up_filled:
            cancel_order(bid.order_id_up)
            log_cancel(market.market_id, bid.order_id_up, "post_close_cleanup")
            cancelled_count += 1
        # Cancel Down if not filled
        if bid.order_id_down and not bid.down_filled:
            cancel_order(bid.order_id_down)
            log_cancel(market.market_id, bid.order_id_down, "post_close_cleanup")
            cancelled_count += 1

        bid.cancelled = True

        # Summary
        fills = []
        if bid.up_filled:
            fills.append(f"UP={bid.up_fill_size:.0f}")
        if bid.down_filled:
            fills.append(f"DOWN={bid.down_fill_size:.0f}")
        fill_str = ", ".join(fills) if fills else "NONE"

        log_outcome(market.market_id, "post_close",
                    f"cancelled {cancelled_count} unfilled, fills: {fill_str}")
        dash_log(
            f"Cancelled {cancelled_count} unfilled | Fills: {fill_str} | {market.question}",
            style="yellow" if fills else "dim",
        )

    timer = threading.Timer(secs_until_cancel, do_cancel)
    timer.daemon = True
    timer.start()
    state.cancel_timers[market.market_id] = timer

    log.debug(
        f"Scheduled cancel for market={market.market_id} "
        f"in {secs_until_cancel:.0f}s"
    )


# ── Market Discovery Loop ──────────────────────────────────────────────────

def refresh_watch_list():
    """Fetch active BTC 5-min markets and update the watch list."""
    markets = fetch_active_btc_markets()
    now = datetime.now(timezone.utc)

    added = 0
    for m in markets:
        # Only watch markets that haven't expired yet
        if m.end_time > now + timedelta(seconds=config.BID_WINDOW_CLOSE):
            if m.market_id not in state.watch_list:
                state.watch_list[m.market_id] = m
                # Subscribe new market to WebSocket feed for price monitoring
                state.ws_feed.subscribe(m.token_id_up, m.token_id_down, m.market_id)
                # Record market metadata to CSV
                data_rec.record_market(
                    market_id=m.market_id,
                    question=m.question,
                    token_id_up=m.token_id_up,
                    token_id_down=m.token_id_down,
                    end_time=m.end_time,
                )
                added += 1

    # Remove expired markets from watch list
    expired = [
        mid for mid, mkt in state.watch_list.items()
        if seconds_until(mkt.end_time) < -config.CANCEL_DELAY_AFTER_CLOSE - 30
    ]
    for mid in expired:
        mkt = state.watch_list[mid]
        state.ws_feed.unsubscribe(mkt.token_id_up, mkt.token_id_down)
        del state.watch_list[mid]

    # ── Update dashboard with market details (PRESERVE existing prices) ──
    existing_prices = {}
    for dm in dash_state.active_markets:
        mid_key = dm.get("market_id")
        if mid_key:
            existing_prices[mid_key] = {
                "up_ask": dm.get("up_ask", 0),
                "down_ask": dm.get("down_ask", 0),
                "combined": dm.get("combined", 0),
                "price_ts": dm.get("price_ts", 0),
            }

    dash_markets = []
    for mid, mkt in state.watch_list.items():
        secs = seconds_until(mkt.end_time)
        bid = state.bids_posted.get(mid)

        if bid and bid.up_filled and bid.down_filled:
            status = "both_filled"
        elif bid and (bid.up_filled or bid.down_filled):
            status = "partial_fill"
        elif bid:
            status = "bids_live"
        elif secs <= config.BID_WINDOW_OPEN and secs >= config.BID_WINDOW_CLOSE:
            status = "in_range"
        elif secs < 0:
            status = "expired"
        else:
            status = "watching"

        prev = existing_prices.get(mid, {})
        dash_markets.append({
            "market_id": mid,
            "question": mkt.question,
            "end_time": mkt.end_time,
            "secs_remaining": secs,
            "up_ask": prev.get("up_ask", 0),
            "down_ask": prev.get("down_ask", 0),
            "combined": prev.get("combined", 0),
            "price_ts": prev.get("price_ts", 0),
            "status": status,
            "bid_price": bid.bid_price if bid else 0,
            "up_filled": bid.up_filled if bid else False,
            "down_filled": bid.down_filled if bid else False,
        })
    dash_market_update(dash_markets)
    dash_state.update(
        markets_watched=len(state.watch_list),
        last_poll=datetime.now(timezone.utc),
    )

    if added > 0 or expired:
        log.info(
            f"Watch list updated: +{added} new, -{len(expired)} expired, "
            f"{len(state.watch_list)} total active"
        )
        dash_log(
            f"Markets: +{added} new, -{len(expired)} expired, "
            f"{len(state.watch_list)} active",
            style="cyan",
        )


# ── Main Polling Loop ──────────────────────────────────────────────────────

def run_bot():
    """Main bot loop — posts limit bids and monitors for fills."""

    # Validate configuration
    errors = config.validate_config()
    if errors and not config.PAPER_TRADING:
        log.error("Configuration errors (required for live trading):")
        for e in errors:
            log.error(f"  - {e}")
        log.error("Fix these in .env and restart. Exiting.")
        sys.exit(1)

    # Print config summary
    config.print_config_summary()

    mode = "PAPER TRADING" if config.PAPER_TRADING else "LIVE TRADING"
    cost_per_market = config.BID_PRICE * config.TOKENS_PER_SIDE * 2
    log.info(f"=== Polybot Snipez starting in {mode} mode ===")
    log.info(f"Strategy: Limit BID ${config.BID_PRICE} x {config.TOKENS_PER_SIDE}/side on both Up & Down")
    log.info(f"Max cost per market (if both fill): ${cost_per_market:.2f}")
    log.info(f"All prices sourced from Polymarket CLOB -- no external feeds")

    if config.PAPER_TRADING:
        log.info("PAPER TRADING enabled -- no real orders will be placed")
    else:
        profit_both = (config.TOKENS_PER_SIDE * 1.0) - cost_per_market
        log.warning(f"LIVE TRADING -- real money at risk!")
        log.info(f"  Bid price:       ${config.BID_PRICE}")
        log.info(f"  Shares/side:     {config.TOKENS_PER_SIDE}")
        log.info(f"  Cost if both:    ${cost_per_market:.2f}")
        log.info(f"  Profit if both:  ${profit_both:.2f}")
        daily = config.MAX_BIDS_PER_DAY if config.MAX_BIDS_PER_DAY > 0 else "Unlimited"
        log.info(f"  Bids per day:    {daily}")
        try:
            confirm = input("\n  Type YES to start live trading: ").strip()
            if confirm != "YES":
                log.info("Live trading not confirmed. Exiting.")
                sys.exit(0)
        except EOFError:
            pass  # Non-interactive — skip confirmation

    # Initialize CLOB client
    try:
        client = get_clob_client()
        log.info("Polymarket CLOB client initialized successfully")
    except Exception as e:
        log_error("Failed to initialize CLOB client", e)
        if not config.PAPER_TRADING:
            sys.exit(1)

    # Live mode: verify API auth
    if not config.PAPER_TRADING:
        try:
            orders = client.get_orders()
            log.info(f"API auth verified -- {len(orders) if orders else 0} existing open orders")
        except Exception as e:
            log.error(f"API auth FAILED: {e}")
            log.error("Check your API key/secret/passphrase in .env. Exiting.")
            sys.exit(1)

    # ── Start data recorder ────────────────────────────────────────────────
    data_rec.start()
    log.info("Data recorder started")

    # ── Start WebSocket price feed ────────────────────────────────────────
    state.ws_feed.start()
    dash_log("WebSocket price feed started", style="bold green")

    # ── Start the live dashboard ───────────────────────────────────────────
    start_dashboard()
    dash_state.update(status="RUNNING")
    dash_log(f"Bot started in {mode} mode", style="bold green")
    dash_log(
        f"Strategy: BID ${config.BID_PRICE} x {config.TOKENS_PER_SIDE}/side",
        style="cyan",
    )

    last_market_refresh = 0
    last_heartbeat = 0
    last_fill_check = 0
    last_http_fallback = 0
    heartbeat_interval = 300
    fill_check_interval = 3.0   # Check fills every 3 seconds
    http_fallback_interval = 5.0

    log.info("Entering main loop...")

    while state.running:
        try:
            now_ts = time.time()
            state.reset_daily_counter()

            # ── Refresh market list every MARKET_POLL_INTERVAL ─────────────
            if now_ts - last_market_refresh >= config.MARKET_POLL_INTERVAL:
                dash_state.update(status="POLLING")
                refresh_watch_list()
                last_market_refresh = now_ts
                dash_state.update(status="RUNNING")

            # ── Heartbeat logging ──────────────────────────────────────────
            if now_ts - last_heartbeat >= heartbeat_interval:
                ws_stats = state.ws_feed.stats()
                active_bids = sum(1 for b in state.bids_posted.values() if not b.cancelled)
                fills = sum(1 for b in state.bids_posted.values()
                            if b.up_filled or b.down_filled)
                log_heartbeat(
                    active_markets=len(state.watch_list),
                    fired_today=state.bids_today,
                    paper=config.PAPER_TRADING,
                )
                dash_log(
                    f"Heartbeat: {len(state.watch_list)} mkts, "
                    f"{active_bids} active bids, {fills} fills | "
                    f"WS: {'ON' if ws_stats['connected'] else 'OFF'}, "
                    f"{ws_stats['messages']} msgs",
                    style="dim",
                )
                last_heartbeat = now_ts

            # ── Post bids on markets entering the window ───────────────────
            for market_id, market in list(state.watch_list.items()):
                if not state.running:
                    break
                if market_id in state.bids_posted:
                    continue  # Already have bids on this one
                if not config.TRADING_ENABLED:
                    continue

                secs = seconds_until(market.end_time)
                if config.BID_WINDOW_CLOSE <= secs <= config.BID_WINDOW_OPEN:
                    posted = post_bids(market)
                    if posted:
                        dash_state.update(
                            markets_fired=dash_state.markets_fired + 1,
                            signals_evaluated=dash_state.signals_evaluated + 1,
                        )

            # ── Check for fills on posted bids ─────────────────────────────
            if now_ts - last_fill_check >= fill_check_interval:
                check_fills()
                last_fill_check = now_ts

            # ── Update dashboard prices (WS or HTTP fallback) ─────────────
            markets_in_range = []
            for market_id, market in state.watch_list.items():
                secs = seconds_until(market.end_time)
                if secs <= 0 or secs > 300:
                    continue
                markets_in_range.append((market_id, market))

            # HTTP fallback for markets without WS data
            ws_connected = state.ws_feed.connected
            markets_needing_http = [
                (mid, mkt) for mid, mkt in markets_in_range
                if not state.ws_feed.has_valid_prices(mkt.token_id_up, mkt.token_id_down)
            ]
            need_http = bool(markets_needing_http) or (
                not ws_connected and markets_in_range and
                now_ts - last_http_fallback >= http_fallback_interval
            )

            if need_http:
                http_targets = markets_in_range if not ws_connected else markets_needing_http
                if http_targets:
                    token_pairs = [
                        (mkt.token_id_up, mkt.token_id_down)
                        for _, mkt in http_targets
                    ]
                    prefetched = fetch_orderbooks_parallel(token_pairs)
                    last_http_fallback = now_ts
                else:
                    prefetched = {}
            else:
                prefetched = {}

            # Push prices to dashboard
            for _, mkt in markets_in_range:
                ws_up = state.ws_feed.get_price(mkt.token_id_up)
                ws_down = state.ws_feed.get_price(mkt.token_id_down)

                if ws_up.valid and ws_down.valid:
                    ask_up = ws_up.best_ask
                    ask_down = ws_down.best_ask
                    pts = max(ws_up.timestamp, ws_down.timestamp)
                elif mkt.token_id_up in prefetched:
                    bk_up, bk_down = prefetched[mkt.token_id_up]
                    if bk_up.valid and bk_down.valid:
                        ask_up = bk_up.best_ask
                        ask_down = bk_down.best_ask
                        pts = max(bk_up.timestamp, bk_down.timestamp)
                    else:
                        continue
                else:
                    continue

                for dm in dash_state.active_markets:
                    if dm.get("market_id") == mkt.market_id:
                        dm["up_ask"] = ask_up
                        dm["down_ask"] = ask_down
                        dm["combined"] = ask_up + ask_down
                        dm["price_ts"] = pts
                        break

            # ── Record ticks for active markets ────────────────────────────
            for _, mkt in markets_in_range:
                secs = seconds_until(mkt.end_time)
                ws_up = state.ws_feed.get_price(mkt.token_id_up)
                ws_down = state.ws_feed.get_price(mkt.token_id_down)
                if ws_up.valid and ws_down.valid:
                    data_rec.record_tick(
                        market_id=mkt.market_id,
                        market_name=mkt.question,
                        secs_remaining=secs,
                        up_ask=ws_up.best_ask, up_ask_size=ws_up.best_ask_size,
                        up_bid=ws_up.best_bid, up_bid_size=ws_up.best_bid_size,
                        down_ask=ws_down.best_ask, down_ask_size=ws_down.best_ask_size,
                        down_bid=ws_down.best_bid, down_bid_size=ws_down.best_bid_size,
                        source="ws",
                        fired=mkt.market_id in state.bids_posted,
                    )

            # ── Flush data recorder ────────────────────────────────────────
            data_rec.flush()

            # ── Sync counters to dashboard ─────────────────────────────────
            dash_state.update(trades_today=state.bids_today)

            # Sync WS feed stats
            ws_s = state.ws_feed.stats()
            dash_state.update(
                ws_connected=ws_s["connected"],
                ws_messages=ws_s["messages"],
                ws_reconnects=ws_s["reconnects"],
            )

            # Sync data recorder stats
            rec_s = data_rec.stats()
            dash_state.update(
                rec_ticks=rec_s["ticks"],
                rec_signals=rec_s["signals"],
            )

            # ── Sleep ──────────────────────────────────────────────────────
            has_active = any(
                config.BID_WINDOW_CLOSE <= seconds_until(mkt.end_time) <= config.BID_WINDOW_OPEN
                for mkt in state.watch_list.values()
            )
            sleep_time = 0.5 if has_active else 1.0
            time.sleep(sleep_time)

        except KeyboardInterrupt:
            shutdown_handler(None, None)
        except Exception as e:
            log_error("main_loop", e)
            dash_state.update(status="ERROR", errors=dash_state.errors + 1)
            dash_log(f"Error: {e}", style="bold red")
            time.sleep(5)


# ── Entry Point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    print(r"""
    +===================================================+
    |          POLYBOT SNIPEZ -- BTC BID BOT            |
    |    Polymarket 5-Min Limit Bid Strategy            |
    |                                                   |
    |  Posts low-ball bids on BOTH sides, waits for     |
    |  fills. One side always pays $1.00.               |
    +===================================================+
    """)
    run_bot()
