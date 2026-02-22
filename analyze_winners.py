"""
Analyze winning trades from the Feb 19-20 trading session.
Parses bot_2026-02-20.log for all ORDER-LIVE entries, 
then checks CLOB API for fills + Polymarket results.
"""
import json, os, re, time
from collections import defaultdict
from dotenv import load_dotenv
load_dotenv()

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds

creds = ApiCreds(
    api_key=os.getenv("POLYMARKET_API_KEY"),
    api_secret=os.getenv("POLYMARKET_API_SECRET"),
    api_passphrase=os.getenv("POLYMARKET_PASSPHRASE"),
)
client = ClobClient(
    host="https://clob.polymarket.com",
    key=os.getenv("POLYMARKET_PRIVATE_KEY"),
    chain_id=137,
    creds=creds,
    signature_type=1,
    funder=os.getenv("POLYMARKET_PROXY_ADDRESS"),
)

# Parse all ORDER-LIVE lines from bot_2026-02-20.log
# Format: [ORDER-LIVE] market=XXX side=UP price=0.0500 size=101 order_id=0xXXX secs_remaining=179
order_pattern = re.compile(
    r"\[ORDER-LIVE\]\s+market=(\S+)\s+side=(\w+)\s+price=(\S+)\s+size=(\d+)\s+order_id=(\S+)\s+secs_remaining=(\S+)"
)

# Also parse CANCEL lines
cancel_pattern = re.compile(
    r"\[CANCEL\]\s+market=(\S+)\s+order_id=(\S+)\s+reason=(\S+)"
)

orders = []
cancels = {}
timestamp_pattern = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")

with open("logs/bot_2026-02-20.log", "r") as f:
    for line in f:
        ts_match = timestamp_pattern.search(line)
        ts = ts_match.group(1) if ts_match else "?"
        
        m = order_pattern.search(line)
        if m:
            orders.append({
                "timestamp": ts,
                "market_id": m.group(1),
                "side": m.group(2),
                "price": float(m.group(3)),
                "size": int(m.group(4)),
                "order_id": m.group(5),
                "secs_remaining": float(m.group(6)),
            })
        
        cm = cancel_pattern.search(line)
        if cm:
            cancels[cm.group(2)] = {"market": cm.group(1), "reason": cm.group(3)}

print(f"Total orders found: {len(orders)}")
print(f"Total cancels found: {len(cancels)}")

# Group by market
markets = defaultdict(list)
for o in orders:
    markets[o["market_id"]].append(o)

print(f"Total unique markets: {len(markets)}")
print()

# Check each order via CLOB API
results = []
for idx, (mid, morders) in enumerate(sorted(markets.items(), key=lambda x: x[1][0]["timestamp"]), 1):
    market_result = {
        "num": idx,
        "market_id": mid,
        "timestamp": morders[0]["timestamp"],
        "price": morders[0]["price"],
        "size": morders[0]["size"],
        "secs_remaining": morders[0]["secs_remaining"],
        "orders": {},
    }
    
    for o in morders:
        try:
            resp = client.get_order(o["order_id"])
            if resp is None:
                # Order may have been purged, check if cancelled
                was_cancelled = o["order_id"] in cancels
                market_result["orders"][o["side"]] = {
                    "order_id": o["order_id"],
                    "status": "CANCELLED" if was_cancelled else "PURGED/UNKNOWN",
                    "size_matched": 0,
                    "cancelled": was_cancelled,
                    "cancel_reason": cancels.get(o["order_id"], {}).get("reason", ""),
                }
            else:
                status = resp.get("status", "?")
                matched = float(resp.get("size_matched", 0))
                market_result["orders"][o["side"]] = {
                    "order_id": o["order_id"],
                    "status": status,
                    "size_matched": matched,
                    "cancelled": o["order_id"] in cancels,
                    "cancel_reason": cancels.get(o["order_id"], {}).get("reason", ""),
                }
        except Exception as e:
            market_result["orders"][o["side"]] = {
                "order_id": o["order_id"],
                "status": f"ERR:{e}",
                "size_matched": 0,
                "cancelled": o["order_id"] in cancels,
            }
        time.sleep(0.12)
    
    results.append(market_result)

# Categorize
both_fills = []
single_fills = []
no_fills = []
cancelled_before_fill = []

for r in results:
    up = r["orders"].get("UP", {})
    dn = r["orders"].get("DOWN", {})
    up_matched = up.get("size_matched", 0)
    dn_matched = dn.get("size_matched", 0)
    
    if up_matched > 0 and dn_matched > 0:
        both_fills.append(r)
    elif up_matched > 0 or dn_matched > 0:
        single_fills.append(r)
    elif up.get("cancelled") or dn.get("cancelled"):
        cancelled_before_fill.append(r)
    else:
        no_fills.append(r)

print("=" * 80)
print(f"BOTH FILLS (guaranteed profit): {len(both_fills)}")
print("=" * 80)
for r in both_fills:
    up = r["orders"]["UP"]
    dn = r["orders"]["DOWN"]
    cost = (up["size_matched"] * r["price"]) + (dn["size_matched"] * r["price"])
    # One side pays out $1/share, so min payout = min(up_matched, dn_matched)
    payout = min(up["size_matched"], dn["size_matched"])
    profit = payout - cost
    print(f"  Market #{r['num']} | {r['timestamp']} | price=${r['price']:.2f} size={r['size']}")
    print(f"    secs_remaining={r['secs_remaining']} | market={r['market_id'][:24]}...")
    print(f"    UP  filled: {up['size_matched']:.0f} ({up['status']})")
    print(f"    DOWN filled: {dn['size_matched']:.0f} ({dn['status']})")
    print(f"    Cost: ${cost:.2f} | Payout: ${payout:.2f} | Profit: ${profit:.2f}")
    print()

print("=" * 80)
print(f"SINGLE FILLS (need resolution): {len(single_fills)}")
print("=" * 80)
for r in single_fills:
    up = r["orders"].get("UP", {})
    dn = r["orders"].get("DOWN", {})
    up_m = up.get("size_matched", 0)
    dn_m = dn.get("size_matched", 0)
    filled_side = "UP" if up_m > 0 else "DOWN"
    filled_amt = max(up_m, dn_m)
    cost = filled_amt * r["price"]
    print(f"  Market #{r['num']} | {r['timestamp']} | price=${r['price']:.2f} size={r['size']}")
    print(f"    secs_remaining={r['secs_remaining']} | market={r['market_id'][:24]}...")
    print(f"    Filled side: {filled_side} = {filled_amt:.0f} shares | Cost: ${cost:.2f}")
    print(f"    UP: {up_m:.0f}({up.get('status','?')}) | DOWN: {dn_m:.0f}({dn.get('status','?')})")
    print()

print("=" * 80)
print(f"NO FILLS: {len(no_fills)}")
print("=" * 80)
for r in no_fills:
    up = r["orders"].get("UP", {})
    dn = r["orders"].get("DOWN", {})
    print(f"  Market #{r['num']} | {r['timestamp']} | price=${r['price']:.2f} size={r['size']} secs_left={r['secs_remaining']}")
    print(f"    UP: {up.get('status','?')} | DOWN: {dn.get('status','?')}")

print()
print("=" * 80)
print(f"CANCELLED BEFORE FILL: {len(cancelled_before_fill)}")
print("=" * 80)
for r in cancelled_before_fill:
    up = r["orders"].get("UP", {})
    dn = r["orders"].get("DOWN", {})
    print(f"  Market #{r['num']} | {r['timestamp']} | price=${r['price']:.2f} size={r['size']} secs_left={r['secs_remaining']}")
    print(f"    UP: {up.get('status','?')} cancel_reason={up.get('cancel_reason','')} | DOWN: {dn.get('status','?')} cancel_reason={dn.get('cancel_reason','')}")

# Summary
print()
print("=" * 80)
print("SUMMARY")
print("=" * 80)
total_spent = 0
total_payout = 0
for r in both_fills:
    up_m = r["orders"]["UP"]["size_matched"]
    dn_m = r["orders"]["DOWN"]["size_matched"]
    total_spent += (up_m + dn_m) * r["price"]
    total_payout += min(up_m, dn_m)

for r in single_fills:
    up_m = r["orders"].get("UP", {}).get("size_matched", 0)
    dn_m = r["orders"].get("DOWN", {}).get("size_matched", 0)
    filled = max(up_m, dn_m)
    total_spent += filled * r["price"]
    # Single fills: payout depends on resolution (either $1/share or $0)
    # We don't know resolution yet, so show best/worst case
    
print(f"Both-fill markets: {len(both_fills)} → guaranteed profit from these")
print(f"Single-fill markets: {len(single_fills)} → depends on resolution")  
print(f"No-fill markets: {len(no_fills)}")
print(f"Cancelled: {len(cancelled_before_fill)}")

# Save full results
with open("logs/winner_analysis.json", "w") as f:
    json.dump(results, f, indent=2, default=str)
print("\nFull results saved to logs/winner_analysis.json")
