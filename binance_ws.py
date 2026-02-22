"""
Binance WebSocket Feed — Real-time multi-asset price updates via WebSocket.

Supports BTC, ETH, SOL, and XRP via a single combined WebSocket connection.
Each asset gets its own trade price, 5-min candle OHLC, distance-from-open,
and candle range tracking.

Usage:
    from binance_ws import BinanceFeed
    feed = BinanceFeed()          # Tracks all 4 assets
    feed.start()

    btc = feed.get("BTC")        # AssetState for BTC
    eth = feed.get("ETH")        # AssetState for ETH
    print(btc.price, btc.distance, btc.candle_range)

    # Legacy compat: feed.price / feed.distance etc. still map to BTC
    print(feed.price, feed.distance)

    feed.stop()
"""

import asyncio
import json
import threading
import time
from typing import Optional

import websockets

from logger import log


# ── Per-Asset State ─────────────────────────────────────────────────────────

class AssetState:
    """Live price + candle state for a single asset."""
    __slots__ = (
        "symbol", "price", "candle_open", "candle_high", "candle_low",
        "distance", "candle_range", "prior_candle_range",
        "_last_price_ts", "_last_candle_ts", "_trade_count", "_current_candle_start",
    )

    def __init__(self, symbol: str = ""):
        self.symbol: str = symbol
        self.price: float = 0.0
        self.candle_open: float = 0.0
        self.candle_high: float = 0.0
        self.candle_low: float = 0.0
        self.distance: float = 0.0
        self.candle_range: float = 0.0
        self.prior_candle_range: float = 0.0
        self._last_price_ts: float = 0.0
        self._last_candle_ts: float = 0.0
        self._trade_count: int = 0
        self._current_candle_start: int = 0

    def price_age(self) -> float:
        if self._last_price_ts == 0:
            return -1.0
        return time.time() - self._last_price_ts

    def candle_age(self) -> float:
        if self._last_candle_ts == 0:
            return -1.0
        return time.time() - self._last_candle_ts

    def stats(self) -> dict:
        return {
            "symbol": self.symbol,
            "price": self.price,
            "candle_open": self.candle_open,
            "candle_high": self.candle_high,
            "candle_low": self.candle_low,
            "distance": self.distance,
            "candle_range": self.candle_range,
            "prior_candle_range": self.prior_candle_range,
            "price_age": round(self.price_age(), 1) if self.price_age() >= 0 else -1,
            "trades": self._trade_count,
        }


# ── Binance stream config ──────────────────────────────────────────────────

# Assets to track: Binance symbol suffix is always "USDT"
ASSETS = {
    "BTC":  "btcusdt",
    "ETH":  "ethusdt",
    "SOL":  "solusdt",
    "XRP":  "xrpusdt",
}

def _build_combined_url() -> str:
    """Build Binance combined stream URL for all assets (trade + kline_5m)."""
    streams = []
    for sym in ASSETS.values():
        streams.append(f"{sym}@trade")
        streams.append(f"{sym}@kline_5m")
    return f"wss://stream.binance.com:9443/stream?streams={'/'.join(streams)}"


COMBINED_STREAM = _build_combined_url()
RECONNECT_DELAY = 2.0
MAX_RECONNECTS = 100
PING_INTERVAL = 20


# ── Multi-Asset Feed ────────────────────────────────────────────────────────

class BinanceFeed:
    """
    Real-time multi-asset price feed via Binance WebSocket.

    Tracks BTC, ETH, SOL, XRP simultaneously through one combined stream.

    Access per-asset state via:
      feed.get("BTC")  → AssetState
      feed.get("ETH")  → AssetState

    Legacy BTC-compat properties (.price, .distance, etc.) still work and
    map to the BTC asset so existing code doesn't break.
    """

    def __init__(self):
        # Per-asset state
        self._assets: dict[str, AssetState] = {}
        for name in ASSETS:
            self._assets[name] = AssetState(symbol=name)

        # Reverse lookup: "btcusdt" → "BTC"
        self._sym_to_asset: dict[str, str] = {v: k for k, v in ASSETS.items()}

        # Connection state
        self.connected: bool = False
        self.reconnect_count: int = 0
        self.messages_received: int = 0

        # Threading
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws = None
        self._running: bool = False

    # ── Public API ──────────────────────────────────────────────────────

    def get(self, asset: str) -> AssetState:
        """Get the AssetState for a given asset (e.g. "BTC", "ETH").
        Returns an empty AssetState if asset is unknown."""
        return self._assets.get(asset.upper(), AssetState(symbol=asset.upper()))

    def start(self):
        """Start the multi-asset WebSocket feed in a background thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="binance-ws"
        )
        self._thread.start()
        log.info(f"Binance WebSocket feed starting (assets: {', '.join(ASSETS.keys())})...")

    def stop(self):
        """Stop the feed gracefully."""
        self._running = False
        if self._ws and self._loop and self._loop.is_running():
            async def _close():
                try:
                    if self._ws:
                        await self._ws.close()
                except Exception:
                    pass
            future = asyncio.run_coroutine_threadsafe(_close(), self._loop)
            try:
                future.result(timeout=3)
            except Exception:
                pass

        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)

        if self._thread:
            self._thread.join(timeout=5)

        self.connected = False
        log.info("Binance WebSocket feed stopped")

    def stats(self) -> dict:
        """Return aggregate feed statistics."""
        return {
            "connected": self.connected,
            "messages": self.messages_received,
            "reconnects": self.reconnect_count,
            "assets": {name: st.stats() for name, st in self._assets.items()},
            # Legacy BTC fields for backward compat
            "price": self._assets["BTC"].price,
            "candle_open": self._assets["BTC"].candle_open,
            "distance": self._assets["BTC"].distance,
            "price_age": self._assets["BTC"].price_age(),
            "trades": self._assets["BTC"]._trade_count,
        }

    # ── Legacy BTC-compat properties ────────────────────────────────────
    # These let old code that does `engine.btc_feed.price` keep working.

    @property
    def price(self) -> float:
        return self._assets["BTC"].price

    @price.setter
    def price(self, v: float):
        self._assets["BTC"].price = v

    @property
    def candle_open(self) -> float:
        return self._assets["BTC"].candle_open

    @candle_open.setter
    def candle_open(self, v: float):
        self._assets["BTC"].candle_open = v

    @property
    def candle_high(self) -> float:
        return self._assets["BTC"].candle_high

    @candle_high.setter
    def candle_high(self, v: float):
        self._assets["BTC"].candle_high = v

    @property
    def candle_low(self) -> float:
        return self._assets["BTC"].candle_low

    @candle_low.setter
    def candle_low(self, v: float):
        self._assets["BTC"].candle_low = v

    @property
    def distance(self) -> float:
        return self._assets["BTC"].distance

    @distance.setter
    def distance(self, v: float):
        self._assets["BTC"].distance = v

    @property
    def candle_range(self) -> float:
        return self._assets["BTC"].candle_range

    @candle_range.setter
    def candle_range(self, v: float):
        self._assets["BTC"].candle_range = v

    @property
    def prior_candle_range(self) -> float:
        return self._assets["BTC"].prior_candle_range

    @prior_candle_range.setter
    def prior_candle_range(self, v: float):
        self._assets["BTC"].prior_candle_range = v

    def price_age(self) -> float:
        """Seconds since last BTC trade price update (legacy compat)."""
        return self._assets["BTC"].price_age()

    def candle_age(self) -> float:
        """Seconds since last BTC candle update (legacy compat)."""
        return self._assets["BTC"].candle_age()

    # ── Internal: Event Loop + Connection ───────────────────────────────

    def _run_loop(self):
        """Entry point for the background thread — auto-restarts on crash."""
        while self._running:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            try:
                self._loop.run_until_complete(self._connect_loop())
            except Exception as e:
                log.error(f"Binance WS loop crashed: {e}")
            finally:
                try:
                    self._loop.close()
                except Exception:
                    pass
                self.connected = False

            if self._running:
                log.warning("Binance WS loop ended, restarting in 5s...")
                time.sleep(5)

    async def _connect_loop(self):
        """Reconnection loop."""
        consecutive_failures = 0

        while self._running and consecutive_failures < MAX_RECONNECTS:
            try:
                async with websockets.connect(
                    COMBINED_STREAM,
                    ping_interval=PING_INTERVAL,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    self.connected = True
                    consecutive_failures = 0
                    log.info(f"Binance WebSocket connected ({len(ASSETS)} assets: trade + kline_5m)")

                    await self._receive_loop(ws)

            except websockets.ConnectionClosed as e:
                log.warning(f"Binance WS closed: {e.code} {e.reason}")
            except Exception as e:
                log.error(f"Binance WS error: {e}")

            self.connected = False
            self._ws = None
            consecutive_failures += 1
            self.reconnect_count += 1

            if self._running:
                delay = min(RECONNECT_DELAY * consecutive_failures, 30)
                log.info(f"Binance WS reconnecting in {delay:.0f}s...")
                await asyncio.sleep(delay)

        if consecutive_failures >= MAX_RECONNECTS:
            log.error(f"Binance WS gave up after {MAX_RECONNECTS} failures")

    async def _receive_loop(self, ws):
        """Process incoming messages."""
        while self._running:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=30)
            except asyncio.TimeoutError:
                log.warning("Binance WS: no message in 30s, connection may be stale")
                continue

            self.messages_received += 1

            try:
                msg = json.loads(raw)
                self._process_message(msg)
            except json.JSONDecodeError:
                pass
            except Exception as e:
                log.debug(f"Binance WS parse error: {e}")

    def _process_message(self, msg: dict):
        """Route combined stream messages to the correct asset handler."""
        # Combined stream wraps in {"stream": "btcusdt@trade", "data": {...}}
        stream = msg.get("stream", "")
        data = msg.get("data", msg)

        # Extract the Binance symbol from the stream name
        sym = stream.split("@")[0] if "@" in stream else ""
        asset_name = self._sym_to_asset.get(sym)

        if not asset_name:
            # Try to match from data event itself
            s_lower = data.get("s", "").lower()
            asset_name = self._sym_to_asset.get(s_lower)

        if not asset_name:
            return  # Unknown asset, skip

        asset = self._assets[asset_name]

        if "trade" in stream or data.get("e") == "trade":
            self._handle_trade(asset, data)
        elif "kline" in stream or data.get("e") == "kline":
            self._handle_kline(asset, data)

    def _handle_trade(self, asset: AssetState, data: dict):
        """Process a trade event — gives us the latest price for an asset."""
        try:
            price = float(data.get("p", 0))
            if price > 0:
                asset.price = price
                asset._last_price_ts = time.time()
                asset._trade_count += 1

                # Update distance from candle open
                if asset.candle_open > 0:
                    asset.distance = abs(price - asset.candle_open)
        except (ValueError, TypeError):
            pass

    def _handle_kline(self, asset: AssetState, data: dict):
        """Process a kline event — gives us candle OHLC for an asset."""
        try:
            k = data.get("k", {})
            candle_open = float(k.get("o", 0))
            candle_high = float(k.get("h", 0))
            candle_low = float(k.get("l", 0))
            candle_start = int(k.get("t", 0))   # Candle start time (ms)

            if candle_open > 0:
                # Detect new candle — save old range as prior
                if candle_start > 0 and candle_start != asset._current_candle_start:
                    if asset._current_candle_start > 0 and asset.candle_range > 0:
                        asset.prior_candle_range = asset.candle_range
                    asset._current_candle_start = candle_start

                asset.candle_open = candle_open
                asset.candle_high = candle_high
                asset.candle_low = candle_low
                asset.candle_range = round(candle_high - candle_low, 8)  # 8 dp for small-price assets
                asset._last_candle_ts = time.time()

                # Update distance
                if asset.price > 0:
                    asset.distance = abs(asset.price - candle_open)
        except (ValueError, TypeError):
            pass
