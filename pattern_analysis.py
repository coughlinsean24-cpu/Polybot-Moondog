"""
Pattern Analysis: Wins vs Losses
Compares the 2 winning both-fill markets (Feb 19 11:05PM, 11:10PM ET)
against the 30 single-fill losers to find exploitable patterns.

Pulls BTC kline data from Binance to check price action context.
"""
import json
import time
import statistics
from datetime import datetime, timezone, timedelta
import requests

# ── Load existing analysis data ─────────────────────────────────────
with open("logs/final_pnl_analysis.json") as f:
    pnl = json.load(f)

losers = pnl["losers"]  # 30 single-fill losers

# ── Winning markets (from check_two_markets analysis) ───────────────
# These were BOTH-FILL wins: both UP and DOWN got filled at $0.02
winners = [
    {
        "market": "Feb 19, 11:05PM-11:10PM ET",
        "condition_id": "0x1421e2a3187c95984acd02d8a61c7995f1b8fc94f6eaee1093df74b82d77c5b2",
        "timestamp": "2026-02-19 23:08:02",  # UTC (order placed)
        "market_open_utc": "2026-02-20 04:05:00",   # 11:05PM ET = 04:05 UTC
        "market_close_utc": "2026-02-20 04:10:00",
        "price": 0.02,
        "size": 200,
        "secs_remaining": 119,
        "both_filled": True,
        "resolution": "Down",
        "cost": 8.00,
        "payout": 200.00,
        "profit": 192.00,
    },
    {
        "market": "Feb 19, 11:10PM-11:15PM ET",
        "condition_id": "0x8772be0c491347869fa8f714c3a2fa679fa0259390c71da18aa3d46c14cf81d6",
        "timestamp": "2026-02-19 23:14:57",  # UTC (order placed)
        "market_open_utc": "2026-02-20 04:10:00",   # 11:10PM ET = 04:10 UTC
        "market_close_utc": "2026-02-20 04:15:00",
        "price": 0.02,
        "size": 200,
        "secs_remaining": 4,
        "both_filled": True,
        "resolution": "Up",
        "cost": 8.00,
        "payout": 200.00,
        "profit": 192.00,
    },
]

# ── Helper: Fetch BTC kline from Binance ────────────────────────────
def get_btc_kline(start_utc_str, minutes_before=10, minutes_after=5):
    """Get 1-min BTC klines around a timestamp."""
    dt = datetime.strptime(start_utc_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    start_ms = int((dt - timedelta(minutes=minutes_before)).timestamp() * 1000)
    end_ms = int((dt + timedelta(minutes=minutes_after)).timestamp() * 1000)
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": "BTCUSDT", "interval": "1m", "startTime": start_ms, "endTime": end_ms, "limit": 30}
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        return r.json()  # Each: [openTime, open, high, low, close, volume, ...]
    except Exception as e:
        print(f"  Binance error: {e}")
        return []

def get_5min_kline(open_utc_str):
    """Get the single 5-min kline that covers the market window."""
    dt = datetime.strptime(open_utc_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    start_ms = int(dt.timestamp() * 1000)
    end_ms = start_ms + 5 * 60 * 1000
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": "BTCUSDT", "interval": "5m", "startTime": start_ms, "endTime": end_ms, "limit": 2}
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        if data:
            k = data[0]
            return {"open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
                    "close": float(k[4]), "volume": float(k[5])}
    except Exception as e:
        print(f"  Binance error: {e}")
    return None

def get_prior_5min_kline(open_utc_str):
    """Get the 5-min kline BEFORE the market window (prior candle)."""
    dt = datetime.strptime(open_utc_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    start_ms = int((dt - timedelta(minutes=5)).timestamp() * 1000)
    end_ms = int(dt.timestamp() * 1000)
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": "BTCUSDT", "interval": "5m", "startTime": start_ms, "endTime": end_ms, "limit": 2}
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        if data:
            k = data[0]
            return {"open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
                    "close": float(k[4]), "volume": float(k[5])}
    except Exception as e:
        print(f"  Binance error: {e}")
    return None


def slug_to_open_utc(slug):
    """Extract market open time from slug like 'btc-updown-5m-1771567200'."""
    parts = slug.split("-")
    ts = int(parts[-1])
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ══════════════════════════════════════════════════════════════════════
#  ANALYSIS
# ══════════════════════════════════════════════════════════════════════

print("=" * 80)
print("  PATTERN ANALYSIS: WINS vs LOSSES")
print("=" * 80)
print()

# ── 1. WINNING MARKETS ──────────────────────────────────────────────
print("━" * 80)
print("  WINNING MARKETS (both-fill, $0.02 per side)")
print("━" * 80)

win_klines = []
win_ranges = []
win_prior_moves = []

for w in winners:
    print(f"\n  {w['market']}")
    print(f"    Orders placed: {w['timestamp']} UTC, secs_remaining={w['secs_remaining']}")
    print(f"    Price: ${w['price']}, Size: {w['size']}/side, Cost: ${w['cost']}")
    print(f"    Resolution: {w['resolution']} won → P&L: +${w['profit']:.2f}")

    # Get BTC price during market window
    kline = get_5min_kline(w["market_open_utc"])
    prior = get_prior_5min_kline(w["market_open_utc"])
    time.sleep(0.2)

    if kline:
        rng = kline["high"] - kline["low"]
        move = kline["close"] - kline["open"]
        pct = abs(move) / kline["open"] * 100
        win_klines.append(kline)
        win_ranges.append(rng)
        print(f"    BTC candle: open={kline['open']:.2f} high={kline['high']:.2f} low={kline['low']:.2f} close={kline['close']:.2f}")
        print(f"    Range: ${rng:.2f} | Move: {'+' if move > 0 else ''}{move:.2f} ({pct:.4f}%)")
        print(f"    Volume: {kline['volume']:.2f} BTC")

    if prior:
        prior_move = prior["close"] - prior["open"]
        prior_rng = prior["high"] - prior["low"]
        win_prior_moves.append(prior_move)
        print(f"    Prior candle: open={prior['open']:.2f} close={prior['close']:.2f} range=${prior_rng:.2f} move={'+' if prior_move > 0 else ''}{prior_move:.2f}")


# ── 2. LOSING MARKETS ──────────────────────────────────────────────
print()
print("━" * 80)
print("  LOSING MARKETS (single-fill, 30 trades)")
print("━" * 80)

loss_klines = []
loss_ranges = []
loss_prior_moves = []
loss_filled_sides = {"UP": 0, "DOWN": 0}
loss_secs = []
loss_hours = {}

for i, l in enumerate(losers):
    loss_filled_sides[l["filled_side"]] += 1
    loss_secs.append(l["secs_remaining"])

    # Parse hour from the market slug
    open_utc = slug_to_open_utc(l["slug"])
    dt = datetime.strptime(open_utc, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    et = dt - timedelta(hours=5)  # UTC to ET
    h = et.strftime("%I:%M%p")
    hour_key = et.strftime("%H")
    loss_hours[hour_key] = loss_hours.get(hour_key, 0) + 1

    # Fetch BTC klines (throttled)
    kline = get_5min_kline(open_utc)
    prior = get_prior_5min_kline(open_utc)
    time.sleep(0.15)

    if kline:
        rng = kline["high"] - kline["low"]
        move = kline["close"] - kline["open"]
        loss_klines.append(kline)
        loss_ranges.append(rng)
        direction = "UP" if move > 0 else "DOWN"

        # Check: did the filled side match BTC direction?
        filled_matched_btc = (l["filled_side"] == direction)

        if i < 5:  # Print first 5 for sample
            pct = abs(move) / kline['open'] * 100
            print(f"\n  #{l['num']} | {l['question']}")
            print(f"    Filled: {l['filled_side']} | BTC moved: {direction} ({'+' if move > 0 else ''}{move:.2f}, {pct:.4f}%)")
            print(f"    Range: ${rng:.2f} | secs_left: {l['secs_remaining']}")
            print(f"    Filled side matched BTC? {'YES ✓' if filled_matched_btc else 'NO ✗'}")

    if prior:
        prior_move = prior["close"] - prior["open"]
        loss_prior_moves.append(prior_move)

if len(losers) > 5:
    print(f"\n  ... ({len(losers) - 5} more losers omitted for brevity)")


# ══════════════════════════════════════════════════════════════════════
#  PATTERN COMPARISON
# ══════════════════════════════════════════════════════════════════════
print()
print("=" * 80)
print("  PATTERN COMPARISON")
print("=" * 80)

# ── A. Fill Pattern ──
print(f"""
  ┌─ FILL PATTERN ─────────────────────────────────────────────────┐
  │  WINNERS: Both UP and DOWN filled (2/2 = 100% both-fill)      │
  │  LOSERS:  Only ONE side filled (30/30 = 100% single-fill)     │
  │                                                                │
  │  KEY INSIGHT: Both-fill = guaranteed profit at $0.02           │
  │  Single-fill = always a loss (adverse selection)               │
  └────────────────────────────────────────────────────────────────┘""")

# ── B. Adverse Selection ──
print(f"""
  ┌─ ADVERSE SELECTION ────────────────────────────────────────────┐
  │  Loser filled sides: UP={loss_filled_sides['UP']}, DOWN={loss_filled_sides['DOWN']}                          │
  │                                                                │
  │  When only one side fills, it's because someone KNOWS the      │
  │  direction and is selling the losing side to you.              │
  └────────────────────────────────────────────────────────────────┘""")

# ── C. BTC Price Range ──
if win_ranges and loss_ranges:
    avg_win_range = statistics.mean(win_ranges)
    avg_loss_range = statistics.mean(loss_ranges)
    med_win_range = statistics.median(win_ranges)
    med_loss_range = statistics.median(loss_ranges)
    print(f"""
  ┌─ BTC 5-MIN CANDLE RANGE ──────────────────────────────────────┐
  │  WINNERS avg range:   ${avg_win_range:>10.2f}  (median: ${med_win_range:.2f})           │
  │  LOSERS  avg range:   ${avg_loss_range:>10.2f}  (median: ${med_loss_range:.2f})           │
  │                                                                │
  │  Larger range = more volatile = easier for informed traders    │
  │  to pick the winning side before your order fills.             │
  └────────────────────────────────────────────────────────────────┘""")

# ── D. Time of Day ──
print(f"""
  ┌─ TIME OF DAY (ET) ─────────────────────────────────────────────┐
  │  WINNERS: 11:05PM and 11:10PM ET (late night, 2 markets)      │
  │  LOSERS by hour (ET):                                          │""")
for h in sorted(loss_hours.keys()):
    et_hour = (int(h) - 5) % 24
    am_pm = "AM" if et_hour < 12 else "PM"
    display_h = et_hour if et_hour <= 12 else et_hour - 12
    if display_h == 0:
        display_h = 12
    bar = "█" * loss_hours[h]
    print(f"  │    {display_h:>2}{am_pm}: {loss_hours[h]:>2} losses  {bar:<20}│")
print(f"  └────────────────────────────────────────────────────────────────┘")

# ── E. Seconds Remaining ──
avg_secs = statistics.mean(loss_secs)
print(f"""
  ┌─ SECONDS REMAINING WHEN FILLED ────────────────────────────────┐
  │  WINNERS: secs_remaining = 119, 4                              │
  │  LOSERS:  avg = {avg_secs:.0f}s, min = {min(loss_secs):.0f}s, max = {max(loss_secs):.0f}s               │
  │                                                                │
  │  No clear time-of-fill pattern — losers fill across all times  │
  └────────────────────────────────────────────────────────────────┘""")

# ── F. Prior Candle Direction ──
if win_prior_moves and loss_prior_moves:
    loss_prior_up = sum(1 for m in loss_prior_moves if m > 0)
    loss_prior_dn = sum(1 for m in loss_prior_moves if m <= 0)
    print(f"""
  ┌─ PRIOR 5-MIN CANDLE (momentum before market) ─────────────────┐
  │  WINNERS prior candle moves: {', '.join(f'{m:+.2f}' for m in win_prior_moves):<30}│
  │  LOSERS  prior candle: {loss_prior_up} up, {loss_prior_dn} down                          │
  └────────────────────────────────────────────────────────────────┘""")


# ══════════════════════════════════════════════════════════════════════
#  DETAILED LOSS CORRELATION: filled side vs actual BTC move
# ══════════════════════════════════════════════════════════════════════
print()
print("=" * 80)
print("  FILLED SIDE vs ACTUAL BTC MOVE (per loser)")
print("=" * 80)

matched_count = 0
opposed_count = 0
detail_rows = []

for i, l in enumerate(losers):
    if i < len(loss_klines):
        k = loss_klines[i]
        move = k["close"] - k["open"]
        btc_dir = "UP" if move > 0 else "DOWN"
        filled = l["filled_side"]
        matched = filled == btc_dir
        if matched:
            matched_count += 1
        else:
            opposed_count += 1
        detail_rows.append({
            "num": l["num"],
            "filled": filled,
            "btc_dir": btc_dir,
            "btc_move": move,
            "matched": matched,
            "secs": l["secs_remaining"],
        })

print(f"\n  Filled side MATCHED BTC direction:  {matched_count} / {len(detail_rows)}")
print(f"  Filled side OPPOSED BTC direction:  {opposed_count} / {len(detail_rows)}")
print()

if detail_rows:
    # Show all rows
    print(f"  {'#':>4} | {'Filled':>6} | {'BTC':>5} | {'Move':>10} | {'Match':>5} | Secs")
    print(f"  {'─'*4}─┼─{'─'*6}─┼─{'─'*5}─┼─{'─'*10}─┼─{'─'*5}─┼─{'─'*5}")
    for r in detail_rows:
        mark = "  ✓" if r["matched"] else "  ✗"
        print(f"  {r['num']:>4} | {r['filled']:>6} | {r['btc_dir']:>5} | {r['btc_move']:>+10.2f} | {mark:>5} | {r['secs']:.0f}s")


# ══════════════════════════════════════════════════════════════════════
#  CONCLUSIONS
# ══════════════════════════════════════════════════════════════════════
print()
print("=" * 80)
print("  CONCLUSIONS & ACTIONABLE PATTERNS")
print("=" * 80)

print(f"""
  1. BOTH-FILL IS THE ONLY WAY TO WIN
     ─────────────────────────────────
     Winners: 2 markets, both had BOTH sides filled → guaranteed $192 profit each
     Losers:  30 markets, ALL were single-fill → guaranteed loss every time
     
     At $0.02/share, if both sides fill you pay $0.04 total and get $1.00 back.
     If only one side fills, you pay $0.02 and get $0.00 back = 100% loss.

  2. ADVERSE SELECTION IS THE CORE PROBLEM
     ─────────────────────────────────────
     When BTC has a clear trend, informed traders sell you the LOSING side.
     The winning side's orderbook gets swept by informed buyers → your bid
     doesn't fill on the winning side because someone else got there first.
     
     Filled UP {loss_filled_sides['UP']}x, DOWN {loss_filled_sides['DOWN']}x — roughly even, meaning the
     direction doesn't matter. What matters is that only ONE side fills.

  3. FILLED SIDE vs BTC DIRECTION
     ────────────────────────────
     Matched (filled = BTC dir):  {matched_count}/{len(detail_rows)} ({matched_count/len(detail_rows)*100:.0f}%)
     Opposed (filled ≠ BTC dir): {opposed_count}/{len(detail_rows)} ({opposed_count/len(detail_rows)*100:.0f}%)
     
     {'STRONG PATTERN: You are consistently getting filled on the LOSING side.' if opposed_count > matched_count * 1.5 else 'Mixed results — adverse selection is probabilistic, not deterministic.'}

  4. POTENTIAL STRATEGIES
     ────────────────────
     a) ONLY trade when orderbook depth is thick on BOTH sides
        (suggests no one has information edge yet)
     
     b) Avoid markets where one side's ask is much lower than the other
        (signals informed flow on that side)
     
     c) Consider time-of-day: your wins were at 11:05-11:15PM ET
        Late-night low-volume periods may have less informed trading
     
     d) Consider BTC volatility: if prior candle had big move,
        skip that market (momentum traders will adversely select you)
     
     e) The REAL edge: you need BOTH sides to fill. Consider:
        - Placing at higher price ($0.03-0.05) to increase fill rate
        - But: higher price = lower profit per win
        - Sweet spot: price where both-fill rate > breakeven
        
  5. BREAKEVEN MATH
     ──────────────
     At $0.02: need both-fill rate > 4.1% to break even
       (1 win = +$192, 1 loss = -$8, so 192/8 = 24:1, need 1/25 = 4%)
     At $0.05: need both-fill rate > 11.1%  
       (1 win = +$140, 1 loss = -$10, so 140/10 = 14:1, need 1/15 = 6.7%)
     At $0.10: need both-fill rate > 25%
       (1 win = +$80, 1 loss = -$20, so 80/20 = 4:1, need 1/5 = 20%)
     
     Your observed both-fill rate: 2/{2 + len(losers)} = {2/(2+len(losers))*100:.1f}%
     At $0.02, breakeven needs >4.1% → you had {2/(2+len(losers))*100:.1f}% → {'PROFITABLE ✓' if 2/(2+len(losers))*100 > 4.1 else 'NOT YET PROFITABLE ✗'}
""")

# Save analysis
output = {
    "winners": winners,
    "loser_count": len(losers),
    "loser_filled_sides": loss_filled_sides,
    "avg_loss_secs_remaining": avg_secs,
    "btc_analysis": {
        "win_ranges": win_ranges,
        "loss_ranges": loss_ranges,
        "avg_win_range": statistics.mean(win_ranges) if win_ranges else 0,
        "avg_loss_range": statistics.mean(loss_ranges) if loss_ranges else 0,
        "filled_matched_btc_count": matched_count,
        "filled_opposed_btc_count": opposed_count,
    },
    "both_fill_rate": 2 / (2 + len(losers)) * 100,
    "breakeven_rate_at_002": 4.1,
    "conclusion": "Both-fill is the only profitable outcome. Adverse selection causes single-fills to always lose.",
}
with open("logs/pattern_analysis.json", "w") as f:
    json.dump(output, f, indent=2, default=str)
print("  Saved to logs/pattern_analysis.json")
