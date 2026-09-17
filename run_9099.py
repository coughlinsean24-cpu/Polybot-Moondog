"""
Polybot Snipez — 90c -> 99c strategy runner (headless)

Starts the same feeds the dashboard uses, points the 90/99 engine at the
live 5-minute crypto markets, and prints a status line as it goes.

    python run_9099.py                 # paper by default — no order is sent
    python run_9099.py --observe       # record candidates only, never trade
    python run_9099.py --check         # print the config and exit

Going live is not a flag here on purpose. It takes four values in .env
(LIVE_TRADING=true, PAPER_TRADING=false, TRADING_ENABLED=true,
MANUAL_ONLY=false); anything less and the engine stays in paper mode.
"""

import argparse
import signal
import sys
import time
from datetime import datetime, timezone

import config
from logger import log
from polymarket_client import fetch_active_markets, seconds_until
from ws_feed import PriceFeed
from binance_ws import BinanceFeed
from strategy_9099 import Strategy9099

# Only subscribe to books we are going to look at soon.
WATCH_HORIZON = 400.0        # seconds before close
DISCOVERY_INTERVAL = 30.0    # seconds between market sweeps
TICK_INTERVAL = 0.25         # seconds between strategy evaluations


class Runner:
    def __init__(self, observe_only: bool = False, status_secs: float = 15.0):
        self.feed = PriceFeed()
        self.underlying = BinanceFeed()
        self.strategy = Strategy9099(feed=self.feed, underlying=self.underlying)
        if observe_only:
            self.strategy.auto_trade = False
            log.info("[9099] OBSERVE ONLY — candidates are recorded, nothing is traded")
        self.watch: dict[str, object] = {}
        self.running = False
        self.status_secs = status_secs
        self._last_discovery = 0.0
        self._last_status = 0.0

    # ── market discovery ─────────────────────────────────────────────────
    def refresh_markets(self):
        assets = [a.lower() for a in self.strategy.assets] or None
        try:
            markets = fetch_active_markets(assets=assets)
        except Exception as e:
            log.error(f"[9099] market discovery failed: {e}")
            return
        added = 0
        for m in markets:
            secs = seconds_until(m.end_time)
            if secs > WATCH_HORIZON or secs < -60:
                continue
            if m.market_id not in self.watch:
                self.watch[m.market_id] = m
                self.feed.subscribe(m.token_id_up, m.token_id_down, m.market_id)
                added += 1
        dropped = [
            mid for mid, m in self.watch.items()
            if seconds_until(m.end_time) < -config.S9099_TRACK_AFTER_CLOSE - 60
        ]
        for mid in dropped:
            m = self.watch.pop(mid)
            self.feed.unsubscribe(m.token_id_up, m.token_id_down)
        if added or dropped:
            log.info(f"[9099] markets: +{added} -{len(dropped)} (watching {len(self.watch)})")

    # ── status ───────────────────────────────────────────────────────────
    def print_status(self):
        s = self.strategy.stats()
        nearest = min(
            (seconds_until(m.end_time) for m in self.watch.values()
             if seconds_until(m.end_time) > 0),
            default=-1,
        )
        line = (
            f"[{datetime.now(timezone.utc):%H:%M:%S}] {s['mode']} | "
            f"markets {len(self.watch)} (next close {nearest:.0f}s) | "
            f"crossings {s['candidates_seen']} | "
            f"@{s['entry_level']:.2f} {s['entry_level_seen']} "
            f"(traded {s['candidates_traded']}, rejected {s['candidates_rejected']}) | "
            f"open {s['open_positions']} | "
            f"TP fills {s['tp_fills']} | exits {s['emergency_exits']} | "
            f"W/L {s['wins']}/{s['losses']} | "
            f"P&L ${s['realized_pnl']:+.2f} | bal ${s['available_balance']:.2f}"
        )
        print(line, flush=True)
        if s["rejection_reasons"]:
            top = ", ".join(f"{k}={v}" for k, v in list(s["rejection_reasons"].items())[:4])
            print(f"           rejections: {top}", flush=True)

    # ── main loop ────────────────────────────────────────────────────────
    def start(self):
        self.running = True
        self.feed.start()
        self.underlying.start()
        log.info("[9099] waiting for the first book + candle updates...")
        time.sleep(3)

        recovered = self.strategy.recover()
        if recovered:
            print(f"  Recovered {recovered} open position(s) from the last run.")

        self.refresh_markets()
        self._last_discovery = time.time()

        while self.running:
            now = time.time()
            try:
                if now - self._last_discovery >= DISCOVERY_INTERVAL:
                    self.refresh_markets()
                    self._last_discovery = now

                self.strategy.on_tick(list(self.watch.values()))

                if now - self._last_status >= self.status_secs:
                    self.print_status()
                    self._last_status = now
            except Exception as e:
                log.error(f"[9099] loop error: {e}")
                time.sleep(1.0)
            time.sleep(TICK_INTERVAL)

    def stop(self, *_):
        if not self.running:
            return
        print("\n  Stopping...")
        self.running = False
        try:
            self.strategy.shutdown()
        finally:
            self.feed.stop()
            self.underlying.stop()
        self.print_status()
        print("  Stopped. Data is in data/candidates_*.csv, "
              "data/candidate_outcomes_*.csv and data/trades_9099_*.csv")


def banner():
    mode = config.strategy_9099_mode()
    print()
    if mode == "LIVE":
        print("  " + "!" * 62)
        print("  !!  LIVE TRADING — REAL ORDERS, REAL MONEY                  !!")
        print("  " + "!" * 62)
    else:
        print("  " + "=" * 62)
        print("  ==  PAPER MODE — simulated fills, no order leaves this box  ==")
        print("  " + "=" * 62)
        for reason in config.strategy_9099_block_reasons():
            print(f"        paper because: {reason}")
    print(f"""
  90c -> 99c strategy
    entry           >= ${config.S9099_ENTRY_PRICE_MIN:.2f}  (up to ${config.S9099_ENTRY_PRICE_MAX:.2f})
    take profit      = ${config.S9099_TAKE_PROFIT_PRICE:.2f}  (resting limit sell)
    stop             = ${config.S9099_STOP_PRICE:.2f}
    entry window     = {config.S9099_MIN_SECS_REMAINING:.0f}s to {config.S9099_MAX_SECS_REMAINING:.0f}s before close
    sizing           = {config.S9099_POSITION_SIZE_MODE} """
          + (f"${config.S9099_FIXED_DOLLARS:.2f}/trade"
             if config.S9099_POSITION_SIZE_MODE == "fixed_dollars"
             else f"{config.S9099_MAX_POSITION_PERCENT:.0f}% of bankroll")
          + f""" (cap ${config.S9099_MAX_POSITION_DOLLARS:.2f})
    assets           = {', '.join(config.S9099_ASSETS)}
    max open         = {config.S9099_MAX_OPEN_POSITIONS}   max daily loss ${config.S9099_MAX_DAILY_LOSS:.2f}
""")


def main():
    parser = argparse.ArgumentParser(description="Run the 90c -> 99c strategy")
    parser.add_argument("--observe", action="store_true",
                        help="record candidates but never place an order")
    parser.add_argument("--check", action="store_true",
                        help="print the configuration and exit")
    parser.add_argument("--status-secs", type=float, default=15.0,
                        help="seconds between status lines (default 15)")
    args = parser.parse_args()

    banner()

    errors = config.validate_strategy_9099()
    if errors:
        print("  Configuration errors:")
        for e in errors:
            print(f"    - {e}")
        return 1
    if config.strategy_9099_is_live():
        cred_errors = config.validate_config()
        if cred_errors:
            print("  Cannot trade live — credentials incomplete:")
            for e in cred_errors:
                print(f"    - {e}")
            return 1
    if args.check:
        print("  Config OK.")
        return 0

    runner = Runner(observe_only=args.observe, status_secs=args.status_secs)
    signal.signal(signal.SIGINT, runner.stop)
    signal.signal(signal.SIGTERM, runner.stop)
    try:
        runner.start()
    except KeyboardInterrupt:
        runner.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
