"""
Final analysis: Check all filled orders against market resolution via CLOB get_market().
Determines winners/losers and calculates exact P&L.
"""
import json, os, time
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

# Load analysis
with open("logs/winner_analysis.json") as f:
    results = json.load(f)

# Get single fills
single_fills = []
for r in results:
    up = r["orders"].get("UP", {})
    dn = r["orders"].get("DOWN", {})
    up_m = up.get("size_matched", 0)
    dn_m = dn.get("size_matched", 0)
    if (up_m > 0) != (dn_m > 0):
        single_fills.append(r)

print(f"Analyzing {len(single_fills)} single-fill markets...\n")

winners = []
losers = []
errors = []

for r in single_fills:
    up = r["orders"].get("UP", {})
    dn = r["orders"].get("DOWN", {})
    up_m = up.get("size_matched", 0)
    dn_m = dn.get("size_matched", 0)
    filled_side = "UP" if up_m > 0 else "DOWN"
    filled_outcome = "Up" if filled_side == "UP" else "Down"
    filled_amt = max(up_m, dn_m)
    cost = filled_amt * r["price"]
    
    mid = r["market_id"]
    
    try:
        mkt = client.get_market(mid)
        tokens = mkt.get("tokens", [])
        question = mkt.get("question", "")
        slug = mkt.get("market_slug", "")
        closed = mkt.get("closed", False)
        
        # Find winning outcome
        winning_outcome = None
        for t in tokens:
            if t.get("winner", False):
                winning_outcome = t.get("outcome", "?")
                break
        
        won = (winning_outcome == filled_outcome)
        
        entry = {
            "num": r["num"],
            "timestamp": r["timestamp"],
            "price": r["price"],
            "size": r["size"],
            "secs_remaining": r["secs_remaining"],
            "filled_side": filled_side,
            "filled_amt": filled_amt,
            "cost": cost,
            "question": question,
            "slug": slug,
            "winning_outcome": winning_outcome,
            "won": won,
            "payout": filled_amt if won else 0,
            "profit": (filled_amt - cost) if won else -cost,
        }
        
        if won:
            winners.append(entry)
        else:
            losers.append(entry)
            
    except Exception as e:
        errors.append({"num": r["num"], "error": str(e), "filled_side": filled_side, "filled_amt": filled_amt, "cost": cost})
    
    time.sleep(0.15)

# Print WINNERS
print("=" * 90)
print(f"  WINNERS ({len(winners)}) — Filled side matched resolution")
print("=" * 90)
total_won_cost = 0
total_won_payout = 0
for w in winners:
    total_won_cost += w["cost"]
    total_won_payout += w["payout"]
    print(f"  #{w['num']:>2} | {w['timestamp']} | {w['filled_side']:>4} filled {w['filled_amt']:>5.0f} @ ${w['price']:.2f} | secs_left={w['secs_remaining']:>5.0f}")
    print(f"       Cost: ${w['cost']:>7.2f} → Payout: ${w['payout']:>7.2f} → Profit: ${w['profit']:>+8.2f}")
    print(f"       {w['question']}")
    print(f"       Resolution: {w['winning_outcome']} won ✓")
    print()

# Print LOSERS
print("=" * 90)
print(f"  LOSERS ({len(losers)}) — Filled side did NOT match resolution")
print("=" * 90)
total_lost = 0
for l in losers:
    total_lost += l["cost"]
    print(f"  #{l['num']:>2} | {l['timestamp']} | {l['filled_side']:>4} filled {l['filled_amt']:>5.0f} @ ${l['price']:.2f} | secs_left={l['secs_remaining']:>5.0f}")
    print(f"       Cost: ${l['cost']:>7.2f} → Payout: $   0.00 → Loss: ${-l['cost']:>+8.2f}")
    print(f"       {l['question']}")
    print(f"       Resolution: {l['winning_outcome']} won (we had {l['filled_side']}) ✗")
    print()

if errors:
    print(f"\nERRORS ({len(errors)}):")
    for e in errors:
        print(f"  #{e['num']} | {e['error'][:60]}")

# SUMMARY
print()
print("=" * 90)
print("  FINAL P&L SUMMARY")
print("=" * 90)
print(f"  Total markets with fills:    {len(single_fills)}")
print(f"  Winners:                     {len(winners)}")
print(f"  Losers:                      {len(losers)}")
print(f"  Win rate:                    {len(winners)/(len(winners)+len(losers))*100:.1f}%")
print()
print(f"  Winner cost:                 ${total_won_cost:>8.2f}")
print(f"  Winner payout:               ${total_won_payout:>8.2f}")
print(f"  Winner profit:               ${total_won_payout - total_won_cost:>+8.2f}")
print()
print(f"  Loser cost (total loss):     ${total_lost:>8.2f}")
print()
net = (total_won_payout - total_won_cost) - total_lost
print(f"  ═══════════════════════════════")
print(f"  NET PROFIT/LOSS:             ${net:>+8.2f}")
print(f"  ═══════════════════════════════")
print()

# Common conditions of winners
if winners:
    print("=" * 90)
    print("  WINNING CONDITIONS ANALYSIS")
    print("=" * 90)
    up_wins = [w for w in winners if w["filled_side"] == "UP"]
    dn_wins = [w for w in winners if w["filled_side"] == "DOWN"]
    secs = [w["secs_remaining"] for w in winners]
    print(f"  UP wins: {len(up_wins)} | DOWN wins: {len(dn_wins)}")
    print(f"  Secs remaining range: {min(secs):.0f} - {max(secs):.0f}")
    print(f"  Avg secs remaining: {sum(secs)/len(secs):.1f}")
    print(f"  All at price: ${winners[0]['price']:.2f}")
    print(f"  All at size: {winners[0]['size']}")
    sizes = [w["filled_amt"] for w in winners]
    print(f"  Avg fill size: {sum(sizes)/len(sizes):.0f}")
    
    # Time distribution
    hours = {}
    for w in winners:
        h = w["timestamp"].split(" ")[1].split(":")[0]
        hours[h] = hours.get(h, 0) + 1
    print(f"\n  Wins by hour (UTC):")
    for h in sorted(hours.keys()):
        print(f"    {h}:00 UTC → {hours[h]} wins")

# Save final results
output = {
    "winners": winners,
    "losers": losers,
    "summary": {
        "total_fills": len(single_fills),
        "winners": len(winners),
        "losers": len(losers),
        "win_rate": len(winners)/(len(winners)+len(losers))*100 if (winners or losers) else 0,
        "winner_cost": total_won_cost,
        "winner_payout": total_won_payout,
        "loser_cost": total_lost,
        "net_profit": net,
    }
}
with open("logs/final_pnl_analysis.json", "w") as f:
    json.dump(output, f, indent=2, default=str)
print("\nSaved to logs/final_pnl_analysis.json")
