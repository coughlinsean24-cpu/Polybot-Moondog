"""
Polybot Snipez — Polymarket API Client Module
All price data comes EXCLUSIVELY from Polymarket's own CLOB.
No external BTC price feeds. The orderbook IS the signal.
"""

import json
import time
import requests
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs

import config
from logger import log, log_error


# ── Data Classes ────────────────────────────────────────────────────────────

@dataclass
class OrderBookSnapshot:
    """Snapshot of one side's orderbook."""
    best_ask: float = 0.0        # Best (lowest) ask price (0 = no data / error)
    best_ask_size: float = 0.0   # Tokens available at best ask
    best_bid: float = 0.0        # Best (highest) bid price
    best_bid_size: float = 0.0   # Tokens available at best bid
    timestamp: float = 0.0       # When this snapshot was taken
    valid: bool = False          # True if fetch succeeded


@dataclass
class MarketWindow:
    """Represents a single 5-minute Up/Down market window on Polymarket."""
    market_id: str               # Condition ID / market slug
    question: str                # Market question text
    token_id_up: str             # Token ID for the "Up" outcome
    token_id_down: str           # Token ID for the "Down" outcome
    end_time: datetime           # When this window closes
    asset: str = "BTC"           # Underlying asset (BTC, ETH, SOL, XRP, …)
    status: str = "active"       # Market status
    fired: bool = False          # Whether we've already placed orders
    order_ids: list = field(default_factory=list)  # Placed order IDs


# ── CLOB Client Singleton ──────────────────────────────────────────────────

_clob_client: Optional[ClobClient] = None


def get_clob_client() -> ClobClient:
    """Get or create the authenticated CLOB client."""
    global _clob_client
    if _clob_client is not None:
        return _clob_client

    creds = ApiCreds(
        api_key=config.POLYMARKET_API_KEY,
        api_secret=config.POLYMARKET_API_SECRET,
        api_passphrase=config.POLYMARKET_PASSPHRASE,
    )

    # Polymarket proxy wallet address (the on-exchange address that holds funds).
    # This is NOT the same as the EOA — it's the proxy contract created by Polymarket.
    proxy_address = config.POLYMARKET_PROXY_ADDRESS

    _clob_client = ClobClient(
        host=config.CLOB_URL,
        chain_id=config.CHAIN_ID,
        key=config.POLYMARKET_PRIVATE_KEY,
        creds=creds,
        signature_type=1,        # POLY_PROXY wallet mode
        funder=proxy_address,    # Proxy contract that holds the funds
        tick_size_ttl=120.0,
    )

    # Verify connectivity
    try:
        ok = _clob_client.get_ok()
        log.info(f"CLOB client connected: {ok}")
    except Exception as e:
        log_error("CLOB client health check", e)

    return _clob_client


# ── Market Discovery ────────────────────────────────────────────────────────

# 5-min Up/Down markets use a predictable event slug pattern:
#   {asset}-updown-5m-{unix_timestamp}
# where timestamp = start of the 5-minute window, at 300-second intervals.
# Assets: BTC, ETH, SOL, XRP (and potentially more in the future).
# The Gamma API filter params (tag, question_contains, etc.) are unreliable,
# but exact slug lookups work perfectly:
#   GET /events?slug={asset}-updown-5m-{ts}

WINDOW_SECONDS = 300  # 5-minute windows

# All 5-minute Up/Down market assets to scan
MARKET_ASSETS: list[str] = ["btc", "eth", "sol", "xrp"]


def _current_window_start() -> int:
    """Return the unix timestamp for the start of the current 5-min window."""
    now = int(time.time())
    return now - (now % WINDOW_SECONDS)


def _generate_window_timestamps(look_ahead: int = 10, look_behind: int = 1) -> list[int]:
    """
    Generate unix timestamps for nearby 5-min windows.
    look_behind=1 catches the currently-active window that started in the past.
    look_ahead=10 finds upcoming windows (~50 minutes ahead).
    """
    base = _current_window_start()
    timestamps = []
    for i in range(-look_behind, look_ahead + 1):
        timestamps.append(base + i * WINDOW_SECONDS)
    return timestamps


def _parse_event_to_market(event: dict, asset: str = "BTC") -> Optional[MarketWindow]:
    """Parse a Gamma API event JSON into a MarketWindow object."""
    event_markets = event.get("markets", [])
    if not event_markets:
        return None

    m = event_markets[0]  # Each 5-min event has exactly 1 market

    # Parse outcomes — stored as JSON string: '["Up", "Down"]'
    try:
        outcomes_raw = m.get("outcomes", "[]")
        outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
    except (json.JSONDecodeError, TypeError):
        outcomes = []

    # Parse token IDs — stored as JSON string: '["token_up", "token_down"]'
    try:
        tokens_raw = m.get("clobTokenIds", "[]")
        tokens = json.loads(tokens_raw) if isinstance(tokens_raw, str) else tokens_raw
    except (json.JSONDecodeError, TypeError):
        tokens = []

    if len(outcomes) < 2 or len(tokens) < 2:
        return None

    # Map token IDs: index 0 = "Up", index 1 = "Down"
    token_id_up = None
    token_id_down = None
    for i, outcome in enumerate(outcomes):
        outcome_lower = outcome.lower()
        if "up" in outcome_lower:
            token_id_up = tokens[i]
        elif "down" in outcome_lower:
            token_id_down = tokens[i]

    # Fallback: first = Up, second = Down (matches Polymarket convention)
    if not token_id_up or not token_id_down:
        token_id_up = tokens[0]
        token_id_down = tokens[1]

    # Parse end time from the market (this is the END of the 5-min window)
    end_time_str = m.get("endDate") or event.get("endDate", "")
    if not end_time_str:
        return None
    try:
        end_time = datetime.fromisoformat(end_time_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None

    # Skip closed/inactive markets
    if m.get("closed", False):
        return None

    market_id = m.get("conditionId", "")
    if not market_id:
        return None

    return MarketWindow(
        market_id=market_id,
        question=m.get("question", event.get("title", "")),
        token_id_up=token_id_up,
        token_id_down=token_id_down,
        end_time=end_time,
        asset=asset.upper(),
        status="active" if m.get("active", True) else "inactive",
    )


def fetch_active_btc_markets() -> list[MarketWindow]:
    """
    Fetch active BTC 5-minute Up/Down markets using slug-based discovery.
    Kept for backward compat (used by scalper).
    """
    return fetch_active_markets(assets=["btc"])


def fetch_active_markets(assets: list[str] | None = None) -> list[MarketWindow]:
    """
    Fetch active 5-minute Up/Down markets for one or more assets.

    Generates predictable event slugs for current + upcoming 5-min windows
    and queries the Gamma API by exact slug.  Supports BTC, ETH, SOL, XRP
    and any future asset that follows the {asset}-updown-5m-{ts} pattern.

    Args:
        assets: list of asset tickers to scan (lowercase).
                Defaults to MARKET_ASSETS (all known assets).

    Source: GET https://gamma-api.polymarket.com/events?slug={asset}-updown-5m-{ts}
    """
    if assets is None:
        assets = MARKET_ASSETS

    markets: list[MarketWindow] = []
    timestamps = _generate_window_timestamps(look_ahead=10, look_behind=1)
    url = f"{config.GAMMA_URL}/events"

    def _fetch_slug(asset_ts: tuple[str, int]) -> Optional[MarketWindow]:
        asset, ts = asset_ts
        slug = f"{asset}-updown-5m-{ts}"
        try:
            resp = requests.get(url, params={"slug": slug}, timeout=8)
            if resp.status_code != 200:
                return None
            data = resp.json()
            events = data if isinstance(data, list) else [data]
            for event in events:
                if not isinstance(event, dict):
                    continue
                return _parse_event_to_market(event, asset=asset)
        except Exception:
            return None
        return None

    # Build (asset, timestamp) pairs for all assets × all windows
    tasks: list[tuple[str, int]] = [
        (a, ts) for a in assets for ts in timestamps
    ]

    # Fetch all slugs in parallel
    with ThreadPoolExecutor(max_workers=min(24, len(tasks))) as pool:
        futures = {pool.submit(_fetch_slug, task): task for task in tasks}
        for future in as_completed(futures):
            result = future.result()
            if result:
                markets.append(result)

    asset_counts = {}
    for m in markets:
        asset_counts[m.asset] = asset_counts.get(m.asset, 0) + 1
    log.debug(
        f"Fetched {len(markets)} active 5-min markets "
        f"(scanned {len(tasks)} slugs across {len(assets)} assets): {asset_counts}"
    )
    return markets


# ── Orderbook Reading (Polymarket CLOB Only) ────────────────────────────────

def fetch_orderbook(token_id: str) -> OrderBookSnapshot:
    """
    Fetch the orderbook for a single token from Polymarket's CLOB.
    This is the ONLY price source. No external feeds.
    
    Source: GET https://clob.polymarket.com/book?token_id={token_id}
    """
    snapshot = OrderBookSnapshot(timestamp=time.time())

    try:
        client = get_clob_client()
        book = client.get_order_book(token_id)

        # Parse asks (we want the best/lowest ask)
        if book.asks:
            best_ask_entry = book.asks[0]  # Already sorted, lowest first
            snapshot.best_ask = float(best_ask_entry.price)
            snapshot.best_ask_size = float(best_ask_entry.size)

        # Parse bids (we want the best/highest bid)
        if book.bids:
            best_bid_entry = book.bids[0]  # Already sorted, highest first
            snapshot.best_bid = float(best_bid_entry.price)
            snapshot.best_bid_size = float(best_bid_entry.size)

        snapshot.valid = True  # Mark as successful fetch

    except Exception as e:
        log_error(f"fetch_orderbook({token_id})", e)
        # snapshot.valid stays False, prices stay 0.0

    return snapshot


def fetch_full_orderbook(token_id: str) -> dict:
    """Fetch the FULL orderbook for a token — all bid and ask levels.
    Returns dict with 'bids': [{price, size}, ...], 'asks': [{price, size}, ...],
    'timestamp': float, 'valid': bool.
    Used for post-close monitoring to detect fills at every price level."""
    result = {"bids": [], "asks": [], "timestamp": time.time(), "valid": False}
    try:
        client = get_clob_client()
        book = client.get_order_book(token_id)
        if book.bids:
            result["bids"] = [{"price": float(b.price), "size": float(b.size)} for b in book.bids]
        if book.asks:
            result["asks"] = [{"price": float(a.price), "size": float(a.size)} for a in book.asks]
        result["valid"] = True
    except Exception as e:
        log_error(f"fetch_full_orderbook({token_id})", e)
    return result


def fetch_orderbooks_parallel(token_pairs: list[tuple[str, str]]) -> dict[str, tuple[OrderBookSnapshot, OrderBookSnapshot]]:
    """
    Fetch orderbooks for multiple markets in parallel.
    Each pair is (token_id_up, token_id_down).
    Returns dict: token_id_up -> (book_up, book_down)
    """
    results: dict[str, tuple[OrderBookSnapshot, OrderBookSnapshot]] = {}

    if not token_pairs:
        return results

    # Flatten to individual token fetch tasks: (key, token_id, side_label)
    tasks = []
    for up_id, down_id in token_pairs:
        tasks.append((up_id, up_id, "up"))
        tasks.append((up_id, down_id, "down"))

    fetched: dict[str, OrderBookSnapshot] = {}

    def _fetch_one(token_id: str) -> tuple[str, OrderBookSnapshot]:
        return (token_id, fetch_orderbook(token_id))

    all_token_ids = list({t[1] for t in tasks})  # unique token IDs
    with ThreadPoolExecutor(max_workers=min(12, len(all_token_ids))) as pool:
        futures = {pool.submit(_fetch_one, tid): tid for tid in all_token_ids}
        for future in as_completed(futures):
            try:
                tid, book = future.result()
                fetched[tid] = book
            except Exception as e:
                tid = futures[future]
                log_error(f"parallel_orderbook({tid})", e)
                fetched[tid] = OrderBookSnapshot(timestamp=time.time())

    # Reassemble into pairs
    for up_id, down_id in token_pairs:
        book_up = fetched.get(up_id, OrderBookSnapshot(timestamp=time.time()))
        book_down = fetched.get(down_id, OrderBookSnapshot(timestamp=time.time()))
        results[up_id] = (book_up, book_down)

    return results


def get_best_ask_price(token_id: str) -> Optional[float]:
    """
    Quick fetch of just the best ask price for a token.
    Source: GET https://clob.polymarket.com/price?token_id={token_id}&side=buy
    """
    try:
        client = get_clob_client()
        price = client.get_price(token_id, "buy")
        return float(price) if price else None
    except Exception as e:
        log_error(f"get_best_ask_price({token_id})", e)
        return None


# ── Order Placement ─────────────────────────────────────────────────────────

def place_limit_buy(token_id: str, price: float, size: int,
                    market_id: str = "") -> Optional[str]:
    """
    Place a limit BUY order on Polymarket's CLOB.
    
    Args:
        token_id: The token to buy (Up or Down)
        price: Limit price (match the current best ask exactly)
        size: Number of tokens to buy
        market_id: For logging purposes
    
    Returns:
        order_id if successful, None if failed
    """
    if config.PAPER_TRADING:
        # Simulate order placement
        fake_id = f"paper_{int(time.time())}_{token_id[:8]}"
        log.info(
            f"[PAPER ORDER] BUY {size} tokens of {token_id[:16]}... "
            f"@ ${price:.4f} (market={market_id})"
        )
        return fake_id

    if not config.TRADING_ENABLED:
        log.warning("Trading is disabled. Order not placed.")
        return None

    try:
        client = get_clob_client()

        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=float(size),
            side="BUY",
        )

        # Create the signed order and post it
        signed_order = client.create_order(order_args)
        response = client.post_order(signed_order, orderType="GTC")

        order_id = response.get("orderID") or response.get("id", "unknown")
        log.info(
            f"[LIVE ORDER] BUY {size} tokens of {token_id[:16]}... "
            f"@ ${price:.4f} → order_id={order_id}"
        )
        return order_id

    except Exception as e:
        log_error(f"place_limit_buy(token={token_id}, price={price}, size={size})", e)
        global last_order_error
        last_order_error = str(e)
        return None

# Accessible from web_dashboard for surfacing errors
last_order_error: str = ""


# ══════════════════════════════════════════════════════════════════════════════
#  SELL SIDE
#  Selling a conditional token needs two things the buy side does not: the
#  tokens must have settled into the wallet, and the CLOB's balance allowance
#  must be refreshed or the order is rejected for insufficient balance even
#  when the tokens are held.  Both are handled below.
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


def get_usdc_balance() -> float:
    """
    Available USDC collateral on the exchange, in dollars.

    This is buying power — what the CLOB will let us spend right now — not
    the wallet's total balance.  In paper mode it returns the configured
    paper bankroll so sizing maths behaves identically in both modes.
    Returns 0.0 on error, which every caller must treat as "cannot size".
    """
    from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

    if config.PAPER_TRADING:
        return float(config.S9099_PAPER_BANKROLL)

    try:
        client = get_clob_client()
        result = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        # USDC is 6-decimal, same convention as conditional tokens.
        return int(result.get("balance", 0)) / 1_000_000
    except Exception as e:
        log_error("get_usdc_balance", e)
        return 0.0


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



def cancel_order(order_id: str) -> bool:
    """Cancel a specific order by ID."""
    if config.PAPER_TRADING:
        log.info(f"[PAPER CANCEL] order_id={order_id}")
        return True

    try:
        client = get_clob_client()
        client.cancel(order_id)
        log.info(f"[CANCEL] order_id={order_id}")
        return True
    except Exception as e:
        log_error(f"cancel_order({order_id})", e)
        return False


def cancel_all_orders() -> bool:
    """Cancel all open orders."""
    if config.PAPER_TRADING:
        log.info("[PAPER CANCEL ALL]")
        return True

    try:
        client = get_clob_client()
        client.cancel_all()
        log.info("[CANCEL ALL] All open orders cancelled")
        return True
    except Exception as e:
        log_error("cancel_all_orders", e)
        return False


def get_order_status(order_id: str) -> dict:
    """
    Check the status of a specific order.
    Returns dict with keys: status, size_matched, price, side, token_id
    Possible statuses: 'live', 'matched', 'cancelled', 'unknown'
    For paper orders, simulates random fills.
    """
    if config.PAPER_TRADING:
        import random
        # Paper mode: 30% chance a paper order gets filled after being posted
        filled = random.random() < 0.30
        return {
            "status": "matched" if filled else "live",
            "size_matched": 200.0 if filled else 0.0,
            "original_size": 200.0,
            "price": 0.10,
            "side": "BUY",
            "token_id": "",
        }

    try:
        client = get_clob_client()
        order = client.get_order(order_id)
        if not order:
            return {"status": "unknown", "size_matched": 0.0, "original_size": 0.0}

        # py-clob-client returns order dict with these fields
        size_matched = float(order.get("size_matched", 0) or 0)
        original_size = float(order.get("original_size", 0) or order.get("size", 0) or 0)
        status = order.get("status", "unknown")

        # Normalize status names
        if status in ("MATCHED", "matched"):
            status = "matched"
        elif status in ("LIVE", "live", "OPEN", "open"):
            status = "live"
        elif status in ("CANCELLED", "cancelled", "CANCELED", "canceled"):
            status = "cancelled"
        else:
            # If fully filled, mark as matched
            if original_size > 0 and size_matched >= original_size:
                status = "matched"
            elif size_matched > 0:
                status = "partial"
            else:
                status = status.lower() if status else "unknown"

        return {
            "status": status,
            "size_matched": size_matched,
            "original_size": original_size,
            "price": float(order.get("price", 0) or 0),
            "side": order.get("side", "BUY"),
            "token_id": order.get("asset_id", ""),
        }
    except Exception as e:
        log_error(f"get_order_status({order_id})", e)
        return {"status": "unknown", "size_matched": 0.0, "original_size": 0.0}


def get_open_orders() -> list[dict]:
    """Fetch all currently open orders from the CLOB."""
    if config.PAPER_TRADING:
        return []
    try:
        client = get_clob_client()
        orders = client.get_orders()
        return orders if orders else []
    except Exception as e:
        log_error("get_open_orders", e)
        return []


# ── Utility ─────────────────────────────────────────────────────────────────

def seconds_until(end_time: datetime) -> float:
    """Calculate seconds remaining until end_time."""
    now = datetime.now(timezone.utc)
    delta = (end_time - now).total_seconds()
    return delta


def check_market_resolution(condition_id: str) -> dict:
    """Query the CLOB for market resolution status.

    Returns a dict with:
        resolved: bool — True if the market has settled
        winner:   str  — "Up", "Down", or "unknown"
        tokens:   list — raw token data from CLOB
    """
    try:
        client = get_clob_client()
        market_data = client.get_market(condition_id)
        tokens = market_data.get("tokens", [])
        winner = "unknown"
        resolved = False
        for tok in tokens:
            if tok.get("winner") is True:
                resolved = True
                outcome = tok.get("outcome", "").strip()
                if outcome.lower() in ("up", "yes"):
                    winner = "Up"
                elif outcome.lower() in ("down", "no"):
                    winner = "Down"
                else:
                    winner = outcome
                break
        return {"resolved": resolved, "winner": winner, "tokens": tokens}
    except Exception as e:
        log_error(f"check_market_resolution({condition_id})", e)
        return {"resolved": False, "winner": "unknown", "tokens": []}


def get_adaptive_poll_interval(secs_remaining: float) -> float:
    """
    Adaptive polling frequency per Section 4.4 of spec:
    <=0s:    don't poll (return -1) — market already closed
    >300s:   don't poll (return -1) — too far out
    300-180s: every 10 seconds (warming up)
    180-30s:  every 3 seconds (active window)
    <30s:     every 1 second (final window)
    """
    if secs_remaining <= 0:
        return -1  # Market already expired
    elif secs_remaining > 300:
        return -1  # Don't poll yet
    elif secs_remaining > 180:
        return 10.0
    elif secs_remaining > 30:
        return 3.0
    else:
        return 1.0


# ── Market Scanner ──────────────────────────────────────────────────────────
# Queries the Gamma API broadly for active binary markets across all categories,
# returning a standardized list for the dashboard scanner tab.

@dataclass
class ScannedMarket:
    """A market discovered by the scanner — not necessarily Up/Down crypto."""
    market_id: str                # conditionId
    question: str                 # Full question text
    token_id_yes: str             # Token for outcome 1 (Up / Yes / Over / Team A)
    token_id_no: str              # Token for outcome 2 (Down / No / Under / Team B)
    outcome_yes: str              # Label for outcome 1
    outcome_no: str               # Label for outcome 2
    end_time: datetime            # Market close time
    liquidity: float = 0.0       # Reported liquidity (USD)
    volume: float = 0.0          # Reported volume (USD)
    category: str = "other"      # crypto-5m, crypto-15m, sports, esports, etc.
    slug: str = ""               # Event slug
    best_ask_yes: float = 0.0    # Best ask for outcome 1 (fetched separately)
    best_ask_no: float = 0.0     # Best ask for outcome 2 (fetched separately)


def _classify_market(slug: str, question: str) -> str:
    """Classify a market into a category based on slug and question text."""
    q = question.lower()
    s = slug.lower() if slug else ""
    if "updown-5m-" in s:
        return "crypto-5m"
    if "updown-15m-" in s:
        return "crypto-15m"
    if any(x in s for x in ["nba", "nhl", "nfl", "mlb", "mls", "epl", "ucl", "afl"]):
        return "sports"
    if any(x in s for x in ["esport", "league-of-legends", "dota", "csgo", "valorant", "lol"]):
        return "esports"
    if any(x in q for x in ["over", "under", "total"]) and any(x in q for x in ["kills", "goals", "points", "runs"]):
        return "over-under"
    if any(x in q for x in ["temperature", "weather", "snow", "rain", "precipitation"]):
        return "weather"
    return "other"


def scan_active_markets(
    max_hours_to_expiry: float = 24.0,
    min_liquidity: float = 0.0,
    max_liquidity: float = 999_999.0,
    limit: int = 200,
) -> list[ScannedMarket]:
    """
    Scan the Gamma API for ALL active binary (2-outcome) markets.

    Fetches recently-created active markets and filters client-side for:
    - Binary outcomes only (exactly 2 outcomes)
    - Within time-to-expiry range
    - Within liquidity range

    Returns a list of ScannedMarket objects sorted by time to expiry.
    """
    url = f"{config.GAMMA_URL}/markets"
    now = datetime.now(timezone.utc)
    results: list[ScannedMarket] = []
    seen_ids: set[str] = set()

    def _parse_market(m: dict) -> ScannedMarket | None:
        """Parse a raw Gamma market dict into a ScannedMarket."""
        cid = m.get("conditionId", "")
        if not cid or cid in seen_ids:
            return None
        if m.get("closed", False):
            return None

        # Must be binary (exactly 2 outcomes)
        try:
            outcomes_raw = m.get("outcomes", "[]")
            outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
        except (json.JSONDecodeError, TypeError):
            return None
        try:
            tokens_raw = m.get("clobTokenIds", "[]")
            tokens = json.loads(tokens_raw) if isinstance(tokens_raw, str) else tokens_raw
        except (json.JSONDecodeError, TypeError):
            return None

        if len(outcomes) != 2 or len(tokens) != 2:
            return None

        # Parse end time
        end_str = m.get("endDate", "")
        if not end_str:
            return None
        try:
            end_time = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None

        # Time filter
        secs_left = (end_time - now).total_seconds()
        if secs_left < 0 or secs_left > max_hours_to_expiry * 3600:
            return None

        # Liquidity filter (client-side since API params are unreliable)
        liq = float(m.get("liquidity", 0) or 0)
        vol = float(m.get("volume", 0) or 0)
        if liq < min_liquidity or liq > max_liquidity:
            return None

        slug = m.get("slug", "") or m.get("groupItemTitle", "") or ""
        question = m.get("question", "")
        category = _classify_market(slug, question)

        seen_ids.add(cid)
        return ScannedMarket(
            market_id=cid,
            question=question,
            token_id_yes=tokens[0],
            token_id_no=tokens[1],
            outcome_yes=outcomes[0],
            outcome_no=outcomes[1],
            end_time=end_time,
            liquidity=liq,
            volume=vol,
            category=category,
            slug=slug,
        )

    # Query 1: Most recently created active markets (catches new short-lived ones)
    def _fetch_batch(params: dict) -> list[dict]:
        try:
            resp = requests.get(url, params=params, timeout=10)
            if resp.status_code != 200:
                return []
            data = resp.json()
            return data if isinstance(data, list) else [data]
        except Exception as e:
            log_error("scan_active_markets", e)
            return []

    queries = [
        {"active": "true", "closed": "false", "order": "startDate",
         "ascending": "false", "limit": str(limit)},
        {"active": "true", "closed": "false", "order": "endDate",
         "ascending": "true", "limit": str(limit)},
    ]

    # Fetch both queries in parallel
    all_raw: list[dict] = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_fetch_batch, q) for q in queries]
        for f in as_completed(futures):
            all_raw.extend(f.result())

    for m in all_raw:
        parsed = _parse_market(m)
        if parsed:
            results.append(parsed)

    # Sort by time to expiry (soonest first)
    results.sort(key=lambda m: m.end_time)

    log.debug(
        f"[SCANNER] Found {len(results)} binary markets "
        f"(scanned {len(all_raw)} raw markets, "
        f"max_hours={max_hours_to_expiry}, liq={min_liquidity}-{max_liquidity})"
    )
    return results
