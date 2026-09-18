"""
WebSocket Price Feed — Real-time orderbook updates from Polymarket CLOB.

Instead of polling the REST API every 200-500ms, this module connects to
Polymarket's WebSocket endpoint and receives push updates in <10ms.

Usage:
    feed = PriceFeed()
    feed.start()                          # Runs in background thread
    feed.subscribe(token_id_up, token_id_down, market_id)
    ...
    price = feed.get_price(token_id_up)   # Returns (best_ask, best_ask_size, ts)
    feed.stop()
"""

import os
import asyncio
import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import websockets

from logger import log

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
RECONNECT_DELAY = 2.0     # seconds between reconnect attempts
PING_INTERVAL = 20        # seconds between WebSocket pings
MAX_RECONNECTS = 50        # give up after this many consecutive failures
SUB_BATCH_SIZE = 50        # max tokens per subscription message
STALE_THRESHOLD = 60.0     # seconds before a token's data is considered stale
RESUB_COOLDOWN = 30.0      # min seconds between re-sub attempts for the same token

# How far ahead of a market's close its tokens are worth chasing.
#
# A 5-min market an hour out has never traded, so it has no book and sends
# nothing — which the stale check could not tell apart from a subscription
# that had genuinely dropped. Every quiet far-future token was therefore
# re-subscribed every RESUB_COOLDOWN seconds, forever, and every re-sub pulls
# a full book snapshot back down the one shared socket. With four assets and
# an hour of lookahead that was ~90 pointless snapshots per cycle competing
# with the one market actually being traded.
#
# Silence from a market that has not started yet is not a fault, so it is not
# chased. 15 minutes is well beyond any strategy's tracking window.
RESUB_HORIZON = float(os.getenv("WS_RESUB_HORIZON", "900"))


@dataclass
class PriceTick:
    """One snapshot of price at a point in time."""
    ts: float           # time.time()
    ask: float
    bid: float
    ask_size: float = 0.0
    bid_size: float = 0.0


@dataclass
class TradePrint:
    """One executed trade reported by the CLOB (event_type=last_trade_price).

    `side` is the TAKER's side: BUY means someone lifted an ask, which is what
    consumes the queue in front of a resting sell order.
    """
    ts: float
    price: float
    size: float
    side: str          # "BUY" or "SELL"


# How many seconds of history to keep per token
PRICE_HISTORY_WINDOW = 120.0   # 2 minutes of ticks
PRICE_HISTORY_MAX = 600        # cap the deque length
TRADE_HISTORY_WINDOW = 400.0   # a whole 5-min market plus slack
TRADE_HISTORY_MAX = 4000       # cap the trade deque length


@dataclass
class LivePrice:
    """Real-time price state for one token (one side of a market)."""
    best_ask: float = 0.0
    best_ask_size: float = 0.0
    best_bid: float = 0.0
    best_bid_size: float = 0.0
    timestamp: float = 0.0        # time.time() of last update
    update_count: int = 0
    valid: bool = False            # True once we've received at least one update
    last_resub: float = 0.0       # time.time() of last re-subscription attempt
    resub_count: int = 0          # how many times we've re-subscribed this token

    # Rolling price history (newest at right)
    history: deque = field(default_factory=lambda: deque(maxlen=PRICE_HISTORY_MAX))

    # Executed trades (newest at right) — the only honest way to know whether
    # a resting order's queue has actually been eaten through.
    trades: deque = field(default_factory=lambda: deque(maxlen=TRADE_HISTORY_MAX))
    last_trade_price: float = 0.0
    last_trade_size: float = 0.0
    last_trade_side: str = ""
    last_trade_ts: float = 0.0
    trade_count: int = 0
    taker_buy_volume: float = 0.0    # cumulative shares lifted from the asks
    taker_sell_volume: float = 0.0   # cumulative shares hit into the bids

    def record_tick(self, ts: float):
        """Append current best_ask/bid to history and prune old ticks."""
        if self.best_ask <= 0:
            return
        self.history.append(PriceTick(
            ts=ts,
            ask=self.best_ask,
            bid=self.best_bid,
            ask_size=self.best_ask_size,
            bid_size=self.best_bid_size,
        ))
        # Prune ticks older than window
        cutoff = ts - PRICE_HISTORY_WINDOW
        while self.history and self.history[0].ts < cutoff:
            self.history.popleft()

    def record_trade(self, price: float, size: float, side: str, ts: float):
        """Store one executed trade print and prune anything too old."""
        if price <= 0 or size <= 0:
            return
        side = (side or "").upper()
        self.trades.append(TradePrint(ts=ts, price=price, size=size, side=side))
        self.last_trade_price = price
        self.last_trade_size = size
        self.last_trade_side = side
        self.last_trade_ts = ts
        self.trade_count += 1
        if side == "BUY":
            self.taker_buy_volume += size
        elif side == "SELL":
            self.taker_sell_volume += size
        cutoff = ts - TRADE_HISTORY_WINDOW
        while self.trades and self.trades[0].ts < cutoff:
            self.trades.popleft()

    def volume_since(self, since_ts: float, side: str | None = None,
                     max_price: float | None = None,
                     min_price: float | None = None) -> float:
        """
        Shares traded since `since_ts`, optionally filtered.

        For a resting SELL at price P, the queue in front of it is eaten by
        taker BUYs at prices at or below P (better-priced asks fill first),
        so that is volume_since(t0, side="BUY", max_price=P).
        """
        total = 0.0
        for t in reversed(self.trades):
            if t.ts < since_ts:
                break
            if side and t.side != side:
                continue
            if max_price is not None and t.price > max_price + 1e-9:
                continue
            if min_price is not None and t.price < min_price - 1e-9:
                continue
            total += t.size
        return total

    def max_trade_price_since(self, since_ts: float) -> float:
        """Highest executed price since `since_ts` (0 if nothing traded)."""
        best = 0.0
        for t in reversed(self.trades):
            if t.ts < since_ts:
                break
            best = max(best, t.price)
        return best

    # ── Derived signals ─────────────────────────────────────────────

    def ask_range(self, secs: float = 60.0) -> tuple[float, float, float]:
        """Return (min_ask, max_ask, current_ask) over the last `secs` seconds."""
        if not self.history:
            return self.best_ask, self.best_ask, self.best_ask
        cutoff = time.time() - secs
        asks = [t.ask for t in self.history if t.ts >= cutoff and t.ask > 0]
        if not asks:
            return self.best_ask, self.best_ask, self.best_ask
        return min(asks), max(asks), self.best_ask

    def bid_range(self, secs: float = 60.0) -> tuple[float, float, float]:
        """Return (min_bid, max_bid, current_bid) over the last `secs` seconds."""
        if not self.history:
            return self.best_bid, self.best_bid, self.best_bid
        cutoff = time.time() - secs
        bids = [t.bid for t in self.history if t.ts >= cutoff and t.bid > 0]
        if not bids:
            return self.best_bid, self.best_bid, self.best_bid
        return min(bids), max(bids), self.best_bid

    def ask_momentum(self, secs: float = 15.0) -> float:
        """
        Price momentum of ask over last `secs` seconds.
        Positive = ask is rising (price moving up), Negative = ask is falling (dip).
        Returns 0.0 if not enough data.
        """
        if len(self.history) < 2:
            return 0.0
        cutoff = time.time() - secs
        recent = [t for t in self.history if t.ts >= cutoff and t.ask > 0]
        if len(recent) < 2:
            return 0.0
        return recent[-1].ask - recent[0].ask

    def is_near_low(self, secs: float = 60.0, pct: float = 0.25) -> bool:
        """
        True if current ask is in the bottom `pct` (25%) of its recent range.
        This means the price is 'low' relative to where it's been.
        """
        lo, hi, cur = self.ask_range(secs)
        spread = hi - lo
        if spread < 0.005:  # Less than half a cent range — too tight to judge
            return False
        position = (cur - lo) / spread  # 0.0 = at the low, 1.0 = at the high
        return position <= pct

    def flow_score(self, secs: float = 30.0) -> float:
        """
        Combined flow score: how much buying pressure vs selling pressure.
        Looks at bid_size vs ask_size trend over recent ticks.
        Positive = more buying flow, Negative = more selling flow.
        Range roughly -1.0 to +1.0.
        """
        if len(self.history) < 3:
            return 0.0
        cutoff = time.time() - secs
        recent = [t for t in self.history if t.ts >= cutoff]
        if len(recent) < 3:
            return 0.0

        # Compare bid_size vs ask_size — bigger bid sizes = buying pressure
        total_bid = sum(t.bid_size for t in recent)
        total_ask = sum(t.ask_size for t in recent)
        total = total_bid + total_ask
        if total == 0:
            return 0.0
        # Normalize to -1..+1:  all bids = +1, all asks = -1
        return (total_bid - total_ask) / total


class PriceFeed:
    """
    Manages a WebSocket connection to Polymarket's CLOB and maintains
    a live in-memory price cache keyed by token_id (asset_id).
    """

    def __init__(self):
        # token_id → LivePrice
        self._prices: dict[str, LivePrice] = {}

        # token_id → market_id (for dashboard updates)
        self._token_to_market: dict[str, str] = {}
        # token_id -> market close epoch, for the resub horizon only.
        self._token_end: dict[str, float] = {}

        # Set of asset_ids currently subscribed
        self._subscribed: set[str] = set()

        # Tokens waiting to be subscribed on next cycle
        self._pending_subs: list[str] = []

        # Threading
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        self._lock = threading.Lock()

        # Stats
        self.connected = False
        self.messages_received = 0
        self.reconnect_count = 0
        self.last_message_time = 0.0

    # ── Public API ──────────────────────────────────────────────────────

    def start(self):
        """Start the WebSocket feed in a background thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="ws-feed")
        self._thread.start()
        log.info("WebSocket price feed started")

    def stop(self):
        """Stop the feed gracefully."""
        self._running = False

        # Close the WebSocket from outside the event loop
        if self._ws and self._loop and self._loop.is_running():
            async def _close():
                try:
                    if self._ws:
                        await self._ws.close()
                except Exception:
                    pass
            import asyncio
            future = asyncio.run_coroutine_threadsafe(_close(), self._loop)
            try:
                future.result(timeout=3)
            except Exception:
                pass

        # Stop the event loop
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)

        if self._thread:
            self._thread.join(timeout=5)

        self.connected = False
        log.info("WebSocket price feed stopped")

    def subscribe(self, token_id_up: str, token_id_down: str, market_id: str,
                  end_epoch: float | None = None):
        """
        Subscribe to price updates for a market's Up and Down tokens.

        `end_epoch` is when the market closes. It is optional and only used to
        decide whether a silent token is worth chasing (see RESUB_HORIZON) —
        prices are handled identically either way.
        """
        with self._lock:
            for tid in (token_id_up, token_id_down):
                if end_epoch is not None:
                    self._token_end[tid] = end_epoch
                if tid not in self._subscribed:
                    self._pending_subs.append(tid)
                    self._subscribed.add(tid)
                    self._token_to_market[tid] = market_id
                    # Initialize price cache
                    if tid not in self._prices:
                        self._prices[tid] = LivePrice()

    def unsubscribe(self, token_id_up: str, token_id_down: str):
        """Remove tokens from subscription (cleanup for expired markets)."""
        with self._lock:
            for tid in (token_id_up, token_id_down):
                self._subscribed.discard(tid)
                self._prices.pop(tid, None)
                self._token_to_market.pop(tid, None)
                self._token_end.pop(tid, None)

    def get_price(self, token_id: str) -> LivePrice:
        """Get the latest price for a token. Returns LivePrice (check .valid)."""
        return self._prices.get(token_id, LivePrice())

    def get_best_ask(self, token_id: str) -> tuple[float, float, float]:
        """Returns (best_ask, best_ask_size, timestamp) for convenience."""
        p = self._prices.get(token_id, LivePrice())
        return p.best_ask, p.best_ask_size, p.timestamp

    def has_valid_prices(self, token_id_up: str, token_id_down: str) -> bool:
        """True if we have received at least one update for both sides."""
        up = self._prices.get(token_id_up, LivePrice())
        down = self._prices.get(token_id_down, LivePrice())
        return up.valid and down.valid

    def in_resub_horizon(self, token_id: str) -> bool:
        """
        Is this token close enough to its market's close to be worth chasing?

        Unknown close time means yes — a token nothing told us about is not
        something to quietly stop maintaining.
        """
        end = self._token_end.get(token_id)
        if end is None:
            return True
        return (end - time.time()) <= RESUB_HORIZON

    def get_stale_tokens(self, resub_eligible_only: bool = False) -> list[str]:
        """Return token_ids that are subscribed but haven't received data recently.
        If resub_eligible_only=True, only return tokens whose cooldown has expired
        AND whose market is near enough for the silence to mean anything."""
        now = time.time()
        stale = []
        for tid in self._subscribed:
            p = self._prices.get(tid)
            if p is None or not p.valid or (now - p.timestamp > STALE_THRESHOLD):
                if resub_eligible_only:
                    # A market that has not started trading yet is silent
                    # because there is nothing to say, not because the
                    # subscription dropped. Chasing it costs a full book
                    # snapshot on the socket the live market shares.
                    if not self.in_resub_horizon(tid):
                        continue
                    # Fixed cooldown — no exponential backoff (was causing stale tokens to become unrecoverable)
                    if p is not None and now - p.last_resub < RESUB_COOLDOWN:
                        continue
                stale.append(tid)
        return stale

    def force_resubscribe(self, token_ids: list[str] | None = None):
        """Queue tokens for re-subscription. If None, re-sub all subscribed."""
        with self._lock:
            targets = token_ids if token_ids else list(self._subscribed)
            for tid in targets:
                if tid not in self._pending_subs:
                    self._pending_subs.append(tid)
            log.info(f"WS force re-subscribe queued: {len(targets)} tokens")

    # ── Internal: Event Loop + Connection ───────────────────────────────

    def _run_loop(self):
        """Entry point for the background thread — auto-restarts on crash."""
        while self._running:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            try:
                self._loop.run_until_complete(self._connect_loop())
            except Exception as e:
                log.error(f"WS feed loop crashed: {e}")
            finally:
                try:
                    self._loop.close()
                except Exception:
                    pass
                self.connected = False

            if self._running:
                log.warning("WS feed loop ended, restarting in 5s...")
                time.sleep(5)

    async def _connect_loop(self):
        """Reconnection loop — keeps trying to stay connected."""
        consecutive_failures = 0

        while self._running and consecutive_failures < MAX_RECONNECTS:
            try:
                async with websockets.connect(
                    WS_URL,
                    ping_interval=PING_INTERVAL,
                    ping_timeout=10,
                    close_timeout=5,
                    max_size=2**22,  # 4MB — book snapshots can be large
                ) as ws:
                    self._ws = ws
                    self.connected = True
                    consecutive_failures = 0
                    log.info(f"WebSocket connected to {WS_URL}")

                    # Re-subscribe all known tokens in batches
                    if self._subscribed:
                        all_tokens = list(self._subscribed)
                        for i in range(0, len(all_tokens), SUB_BATCH_SIZE):
                            batch = all_tokens[i:i + SUB_BATCH_SIZE]
                            await self._send_subscription(batch)
                            if i + SUB_BATCH_SIZE < len(all_tokens):
                                await asyncio.sleep(0.3)  # small delay between batches

                    # Main receive loop
                    await self._receive_loop(ws)

            except websockets.ConnectionClosed as e:
                log.warning(f"WebSocket closed: {e.code} {e.reason}")
            except Exception as e:
                log.error(f"WebSocket error: {e}")

            self.connected = False
            self._ws = None
            consecutive_failures += 1
            self.reconnect_count += 1

            if self._running:
                delay = min(RECONNECT_DELAY * consecutive_failures, 30)
                log.info(f"Reconnecting in {delay:.0f}s... (attempt {consecutive_failures})")
                await asyncio.sleep(delay)

        if consecutive_failures >= MAX_RECONNECTS:
            log.error(f"WebSocket gave up after {MAX_RECONNECTS} consecutive failures")

    async def _receive_loop(self, ws):
        """Process incoming WebSocket messages."""
        last_stale_check = time.time()

        while self._running:
            # Check for pending subscriptions
            with self._lock:
                if self._pending_subs:
                    pending = self._pending_subs.copy()
                    self._pending_subs.clear()
                else:
                    pending = None

            if pending:
                # Send in batches to avoid overwhelming the server
                for i in range(0, len(pending), SUB_BATCH_SIZE):
                    batch = pending[i:i + SUB_BATCH_SIZE]
                    await self._send_subscription(batch)
                    if i + SUB_BATCH_SIZE < len(pending):
                        await asyncio.sleep(0.2)

            # Periodically check for stale tokens and re-subscribe them
            now = time.time()
            if now - last_stale_check >= 15:
                stale = self.get_stale_tokens(resub_eligible_only=True)
                if stale:
                    log.info(f"WS re-subscribing {len(stale)} stale tokens (of {len(self.get_stale_tokens())} total stale)")
                    for i in range(0, len(stale), SUB_BATCH_SIZE):
                        batch = stale[i:i + SUB_BATCH_SIZE]
                        await self._send_subscription(batch)
                        # Track re-sub attempts with backoff
                        for tid in batch:
                            p = self._prices.get(tid)
                            if p:
                                p.last_resub = now
                                p.resub_count += 1
                        if i + SUB_BATCH_SIZE < len(stale):
                            await asyncio.sleep(0.2)
                last_stale_check = now

            # Use a short timeout so we check for new subscriptions quickly.
            recv_timeout = 1.0 if self._subscribed else 0.2
            try:
                msg_raw = await asyncio.wait_for(ws.recv(), timeout=recv_timeout)
            except asyncio.TimeoutError:
                continue

            self.messages_received += 1
            self.last_message_time = time.time()

            try:
                msg = json.loads(msg_raw)
                self._process_message(msg)
            except json.JSONDecodeError:
                pass
            except Exception as e:
                log.debug(f"WS message parse error: {e}")

    async def _send_subscription(self, asset_ids: list[str]):
        """Send subscription request for given asset IDs."""
        if not self._ws:
            return
        # Polymarket expects each subscription for up to ~50 tokens
        sub_msg = {
            "auth": {},
            "type": "market",
            "assets_ids": asset_ids,
        }
        try:
            await self._ws.send(json.dumps(sub_msg))
            log.info(f"WS subscribed to {len(asset_ids)} tokens: {[t[:8]+'...' for t in asset_ids[:3]]}{'...' if len(asset_ids) > 3 else ''}")
        except Exception as e:
            log.error(f"WS subscription send failed: {e}")

    # ── Message Processing ──────────────────────────────────────────────

    def _process_message(self, msg):
        """Parse incoming WS message and update price cache."""
        now = time.time()

        # Messages can be a list (initial book snapshot) or dict
        if isinstance(msg, list):
            for item in msg:
                if isinstance(item, dict):
                    self._handle_single_message(item, now)
        elif isinstance(msg, dict):
            self._handle_single_message(msg, now)

    def _handle_single_message(self, msg: dict, now: float):
        """Process a single message dict."""
        event_type = msg.get("event_type", msg.get("type", ""))

        if event_type == "book":
            # Full orderbook snapshot — extract best ask/bid
            asset_id = msg.get("asset_id", "")
            if asset_id not in self._prices:
                return

            price = self._prices[asset_id]
            asks = msg.get("asks", [])
            bids = msg.get("bids", [])

            if asks:
                # Asks may be sorted descending — find the LOWEST ask (best)
                best_ask_entry = min(asks, key=lambda a: float(a.get("price", 999)))
                price.best_ask = float(best_ask_entry.get("price", 0))
                price.best_ask_size = float(best_ask_entry.get("size", 0))
            if bids:
                # Bids may be sorted descending — find the HIGHEST bid (best)
                best_bid_entry = max(bids, key=lambda b: float(b.get("price", 0)))
                price.best_bid = float(best_bid_entry.get("price", 0))
                price.best_bid_size = float(best_bid_entry.get("size", 0))

            price.timestamp = now
            price.update_count += 1
            price.valid = True
            price.resub_count = 0      # Reset backoff on successful data
            price.record_tick(now)

        elif event_type == "price_change":
            # Incremental price update — has best_bid/best_ask directly
            changes = msg.get("price_changes", [])
            for change in changes:
                asset_id = change.get("asset_id", "")
                if asset_id not in self._prices:
                    continue

                price = self._prices[asset_id]

                best_ask = change.get("best_ask")
                best_bid = change.get("best_bid")

                # A change that carries neither side tells us nothing about
                # the top of book. Stamping it fresh anyway made a token whose
                # quoted price had stopped moving look perfectly healthy, so
                # the stale check never fired and the resubscribe that would
                # have refilled the book never ran — a stuck price that
                # reported itself as live.
                if best_ask is None and best_bid is None:
                    continue

                if best_ask is not None:
                    price.best_ask = float(best_ask)
                if best_bid is not None:
                    price.best_bid = float(best_bid)

                # Size info from the change event
                size = change.get("size")
                side = change.get("side", "")
                if size is not None:
                    if side == "SELL" or side == "sell":
                        price.best_ask_size = float(size)
                    elif side == "BUY" or side == "buy":
                        price.best_bid_size = float(size)
                    else:
                        # Ambiguous — update ask size as before (conservative)
                        price.best_ask_size = float(size)

                price.timestamp = now
                price.update_count += 1
                price.valid = True
                price.resub_count = 0      # Reset backoff on successful data
                price.record_tick(now)

        elif event_type == "last_trade_price":
            # An executed trade. Carries price, size and the taker's side, so
            # it is what tells us whether a resting order's queue was consumed.
            asset_id = msg.get("asset_id", "")
            if asset_id in self._prices:
                price = self._prices[asset_id]
                price.timestamp = now
                price.update_count += 1
                try:
                    price.record_trade(
                        price=float(msg.get("price", 0) or 0),
                        size=float(msg.get("size", 0) or 0),
                        side=str(msg.get("side", "")),
                        ts=now,
                    )
                except (TypeError, ValueError):
                    pass
                # We don't override ask/bid from trade price

        elif event_type == "tick_size_change":
            pass  # Ignore tick size changes

    # ── Diagnostics ─────────────────────────────────────────────────────

    def stats(self) -> dict:
        """Return feed statistics."""
        now = time.time()
        all_stale = self.get_stale_tokens()
        never_received = [t for t in all_stale if not self._prices.get(t, LivePrice()).valid]
        idle = [t for t in all_stale if self._prices.get(t, LivePrice()).valid]
        active = sum(1 for p in self._prices.values() if p.valid and (now - p.timestamp <= STALE_THRESHOLD))
        return {
            "connected": self.connected,
            "messages": self.messages_received,
            "reconnects": self.reconnect_count,
            "subscribed_tokens": len(self._subscribed),
            "active_tokens": active,       # received data recently (within threshold)
            "valid_tokens": sum(1 for p in self._prices.values() if p.valid),  # ever received data
            "stale_tokens": len(all_stale),
            "idle_tokens": len(idle),       # had data before but quiet now
            "never_received": len(never_received),  # never got any data
            "last_msg_age": now - self.last_message_time if self.last_message_time else -1,
        }
