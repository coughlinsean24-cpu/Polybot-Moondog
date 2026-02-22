"""
Binance WebSocket Feed — Real-time BTC price updates via WebSocket.

Replaces the REST polling approach (every 2s) with a persistent WebSocket
connection that delivers price updates every ~100ms.

Usage:
    from binance_ws import BinanceFeed
    btc = BinanceFeed()
    btc.start()
    price = btc.price          # Latest BTC price (float)
    age   = btc.price_age()    # Seconds since last update
    btc.stop()
"""

import asyncio
import json
import threading
import time
from typing import Optional

import websockets

from logger import log

# Binance WebSocket stream for BTCUSDT trades (most real-time)
# Using individual mini-ticker — updates on every trade, much faster than klines
TRADE_STREAM = "wss://stream.binance.com:9443/ws/btcusdt@trade"
# Kline stream as fallback context (1m candles)
KLINE_STREAM = "wss://stream.binance.com:9443/ws/btcusdt@kline_5m"
# Combined stream for both
COMBINED_STREAM = "wss://stream.binance.com:9443/stream?streams=btcusdt@trade/btcusdt@kline_5m"

RECONNECT_DELAY = 2.0
MAX_RECONNECTS = 100
PING_INTERVAL = 20


class BinanceFeed:
    """
    Real-time BTC price feed via Binance WebSocket.
    
    Provides:
      - .price          — latest BTC/USDT trade price (~100ms updates)
      - .candle_open    — current 5-min candle open price
      - .candle_high    — current 5-min candle high
      - .candle_low     — current 5-min candle low
      - .distance       — |price - candle_open|
      - .price_age()    — seconds since last price update
      - .candle_range   — high - low of current 5-min candle
    """

    def __init__(self):
        self.price: float = 0.0
        self.candle_open: float = 0.0
        self.candle_high: float = 0.0
        self.candle_low: float = 0.0
        self.distance: float = 0.0
        self.candle_range: float = 0.0
        self.prior_candle_range: float = 0.0   # Previous 5-min candle range

        self._last_price_ts: float = 0.0
        self._last_candle_ts: float = 0.0
        self._trade_count: int = 0
        self._current_candle_start: int = 0    # Start time of current candle (ms)

        # Connection state
        self.connected: bool = False
        self.reconnect_count: int = 0
        self.messages_received: int = 0

        # Threading
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running: bool = False

    # ── Public API ──────────────────────────────────────────────────────

    def start(self):
        """Start the Binance WebSocket feed in a background thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="binance-ws"
        )
        self._thread.start()
        log.info("Binance WebSocket feed starting...")

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

    def price_age(self) -> float:
        """Seconds since last trade price update. -1 if never updated."""
        if self._last_price_ts == 0:
            return -1.0
        return time.time() - self._last_price_ts

    def candle_age(self) -> float:
        """Seconds since last candle update. -1 if never updated."""
        if self._last_candle_ts == 0:
            return -1.0
        return time.time() - self._last_candle_ts

    def stats(self) -> dict:
        """Return feed statistics."""
        return {
            "connected": self.connected,
            "price": self.price,
            "candle_open": self.candle_open,
            "distance": self.distance,
            "price_age": self.price_age(),
            "messages": self.messages_received,
            "trades": self._trade_count,
            "reconnects": self.reconnect_count,
        }

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
                    log.info("Binance WebSocket connected (trade + kline_5m)")

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
                # No message in 30s — Binance should send trades constantly
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
        """Route combined stream messages."""
        # Combined stream wraps in {"stream": "...", "data": {...}}
        stream = msg.get("stream", "")
        data = msg.get("data", msg)  # fallback to msg itself for direct streams

        if "trade" in stream or data.get("e") == "trade":
            self._handle_trade(data)
        elif "kline" in stream or data.get("e") == "kline":
            self._handle_kline(data)

    def _handle_trade(self, data: dict):
        """Process a trade event — gives us the latest BTC price."""
        try:
            price = float(data.get("p", 0))
            if price > 0:
                self.price = price
                self._last_price_ts = time.time()
                self._trade_count += 1

                # Update distance from candle open
                if self.candle_open > 0:
                    self.distance = abs(price - self.candle_open)
        except (ValueError, TypeError):
            pass

    def _handle_kline(self, data: dict):
        """Process a kline event — gives us candle OHLC."""
        try:
            k = data.get("k", {})
            candle_open = float(k.get("o", 0))
            candle_high = float(k.get("h", 0))
            candle_low = float(k.get("l", 0))
            candle_start = int(k.get("t", 0))   # Candle start time (ms)

            if candle_open > 0:
                # Detect new candle — save old range as prior
                if candle_start > 0 and candle_start != self._current_candle_start:
                    if self._current_candle_start > 0 and self.candle_range > 0:
                        self.prior_candle_range = self.candle_range
                    self._current_candle_start = candle_start

                self.candle_open = candle_open
                self.candle_high = candle_high
                self.candle_low = candle_low
                self.candle_range = round(candle_high - candle_low, 2)
                self._last_candle_ts = time.time()

                # Update distance
                if self.price > 0:
                    self.distance = abs(self.price - candle_open)
        except (ValueError, TypeError):
            pass
