"""Analyze all orders from trades_2026-02-20.jsonl for fill status."""
import json, os, time, sys
from dotenv import load_dotenv
load_dotenv()

# Load trade records
with open("logs/trades_2026-02-20.jsonl") as f:
    lines = [json.loads(l) for l in f if l.strip()]

# Group by market
markets = {}
for l in lines:
    mid = l["market_id"]
    if mid not in markets:
        markets[mid] = []
    markets[mid].append(l)

print(f"Total markets: {len(markets)}, Total orders: {len(lines)}")

# Query CLOB for each order
from py_clob_client.client import ClobClient
client = ClobClient(
    host="https://clob.polymarket.com",
    key=os.getenv("POLYMARKET_PRIVATE_KEY"),
    chain_id=137,
    signature_type=1,
    funder=os.getenv("POLYMARKET_PROXY_ADDRESS"),
)

results = []
for i, (mid, orders) in enumerate(
    sorted(markets.items(), key=lambda x: x[1][0]["timestamp"])
):
    r = {
        "num": i + 1,
        "mid": mid,
        "time": orders[0]["timestamp"][:19],
        "price": orders[0]["price"],
        "size": orders[0]["size"],
        "secs_left": orders[0].get("secs_remaining", "?"),
        "up_fill": 0,
        "down_fill": 0,
        "up_status": "?",
        "down_status": "?",
        "up_oid": "",
        "down_oid": "",
    }
    for o in orders:
        try:
            resp = client.get_order(o["order_id"])
            status = resp.get("status", "?")
            size_matched = float(resp.get("size_matched", 0))
        except Exception as e:
            status = f"ERR:{e}"
            size_matched = 0

        if o["side"] == "UP":
            r["up_status"] = status
            r["up_fill"] = size_matched
            r["up_oid"] = o["order_id"]
        else:
            r["down_status"] = status
            r["down_fill"] = size_matched
            r["down_oid"] = o["order_id"]
        time.sleep(0.12)

    results.append(r)
    if (i + 1) % 10 == 0:
        print(f"  ...checked {i+1}/{len(markets)} markets", file=sys.stderr)

# Categorize
both_fill = [r for r in results if r["up_fill"] > 0 and r["down_fill"] > 0]
up_only = [r for r in results if r["up_fill"] > 0 and r["down_fill"] == 0]
down_only = [r for r in results if r["up_fill"] == 0 and r["down_fill"] > 0]
no_fill = [r for r in results if r["up_fill"] == 0 and r["down_fill"] == 0]

print(f"\n{'='*60}")
print(f"RESULTS SUMMARY")
print(f"{'='*60}")
print(f"Total markets bid on:     {len(results)}")
print(f"BOTH filled (guaranteed): {len(both_fill)}")
print(f"UP only filled:           {len(up_only)}")
print(f"DOWN only filled:         {len(down_only)}")
print(f"No fills:                 {len(no_fill)}")

if both_fill:
    print(f"\n{'='*60}")
    print(f"BOTH-FILL WINNERS (guaranteed profit)")
    print(f"{'='*60}")
    for r in both_fill:
        cost = r["price"] * (r["up_fill"] + r["down_fill"])
        payout = max(r["up_fill"], r["down_fill"])
        profit = payout - cost
        print(f"\n  Market #{r['num']} | {r['time']} UTC")
        print(f"    Bid price: ${r['price']}  |  Size: {r['size']}  |  Secs left: {r['secs_left']}")
        print(f"    UP filled:   {r['up_fill']:.0f} shares ({r['up_status']})")
        print(f"    DOWN filled: {r['down_fill']:.0f} shares ({r['down_status']})")
        print(f"    Cost: ${cost:.2f}  |  Payout: ${payout:.2f}  |  PROFIT: ${profit:.2f}")

if up_only:
    print(f"\n{'='*60}")
    print(f"SINGLE-FILL: UP ONLY")
    print(f"{'='*60}")
    for r in up_only:
        cost = r["price"] * r["up_fill"]
        print(f"  #{r['num']} | {r['time']} | price=${r['price']} | UP filled={r['up_fill']:.0f} | cost=${cost:.2f} | secs_left={r['secs_left']}")

if down_only:
    print(f"\n{'='*60}")
    print(f"SINGLE-FILL: DOWN ONLY")
    print(f"{'='*60}")
    for r in down_only:
        cost = r["price"] * r["down_fill"]
        print(f"  #{r['num']} | {r['time']} | price=${r['price']} | DOWN filled={r['down_fill']:.0f} | cost=${cost:.2f} | secs_left={r['secs_left']}")

print(f"\n{'='*60}")
print(f"NO FILLS: {len(no_fill)} markets")
print(f"{'='*60}")

# Financials
total_spent = sum(r["price"] * (r["up_fill"] + r["down_fill"]) for r in results)
both_payout = sum(max(r["up_fill"], r["down_fill"]) for r in both_fill)
single_risk = sum(r["price"] * (r["up_fill"] + r["down_fill"]) for r in up_only + down_only)

# For single fills: they win if BTC goes the direction they bet, lose if not
# Best case: all singles win → payout = sum of single fill amounts
single_best_payout = sum(r["up_fill"] for r in up_only) + sum(r["down_fill"] for r in down_only)

print(f"\n{'='*60}")
print(f"FINANCIAL SUMMARY")
print(f"{'='*60}")
print(f"Total spent (all fills):       ${total_spent:.2f}")
print(f"Both-fill guaranteed payout:   ${both_payout:.2f}")
print(f"Both-fill guaranteed profit:   ${both_payout - sum(r['price']*(r['up_fill']+r['down_fill']) for r in both_fill):.2f}")
print(f"Single-fill at-risk spend:     ${single_risk:.2f}")
print(f"Single-fill best-case payout:  ${single_best_payout:.2f}")
print(f"Worst case net P&L:            ${both_payout - total_spent:.2f}")
print(f"Best case net P&L:             ${both_payout + single_best_payout - total_spent:.2f}")

# Save full results
with open("logs/fill_analysis_2026-02-20.json", "w") as f:
    json.dump(results, f, indent=2)
print(f"\nFull results saved to logs/fill_analysis_2026-02-20.json")
