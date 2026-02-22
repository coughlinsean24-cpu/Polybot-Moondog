"""
Focus on finding ALL binary Up/Down or Over/Under short-expiry markets.
These are the ones most compatible with our existing strategy.
"""
import requests, json
from datetime import datetime, timezone, timedelta

GAMMA = "https://gamma-api.polymarket.com"

def fetch_markets(params, label):
    print(f"\n{'='*70}")
    print(f"  {label}")
    print(f"{'='*70}")
    try:
        r = requests.get(f"{GAMMA}/markets", params=params, timeout=10)
        markets = r.json()
        if not markets:
            print("  (no results)")
            return []
        # Categorize
        categories = {}
        for m in markets:
            q = m.get("question","")
            slug = m.get("slug","")
            outcomes = m.get("outcomes","")
            liq = m.get("liquidity",0)
            end = m.get("endDate","")
            
            # Categorize by slug prefix
            parts = slug.split("-")
            if "updown" in slug:
                # crypto up/down - get asset and timeframe
                asset = parts[0] if parts else "?"
                tf = "5m" if "5m" in slug else "15m" if "15m" in slug else "1h" if "1h" in slug else "?"
                cat = f"{asset}-updown-{tf}"
            elif "lol-" in slug or "dota2-" in slug or "csgo-" in slug or "val-" in slug:
                cat = "esports"
            elif "nba-" in slug or "nfl-" in slug or "nhl-" in slug or "mlb-" in slug:
                cat = "us-sports"
            elif "epl-" in slug or "laliga-" in slug or "seriea-" in slug or "ligue1-" in slug:
                cat = "soccer"
            elif "temperature" in slug or "weather" in slug or "snow" in slug:
                cat = "weather"
            elif "genesis" in slug or "pga" in slug or "golf" in slug:
                cat = "golf"
            else:
                cat = "other"
            
            if cat not in categories:
                categories[cat] = []
            categories[cat].append({
                "q": q[:80],
                "slug": slug[:70],
                "liq": liq,
                "end": end,
                "outcomes": outcomes
            })
        
        for cat, items in sorted(categories.items()):
            print(f"\n  --- {cat} ({len(items)} markets) ---")
            for it in items[:5]:
                print(f"    liq=${it['liq']}  end={it['end']}")
                print(f"      {it['q']}")
                print(f"      outcomes={it['outcomes']}")
                print(f"      slug={it['slug']}")
            if len(items) > 5:
                print(f"    ... and {len(items)-5} more")
        
        return markets
    except Exception as e:
        print(f"  ERROR: {e}")
        return []

# Get ALL markets closing within 24 hours
now = datetime.now(timezone.utc)
tomorrow = (now + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
today = now.strftime("%Y-%m-%dT%H:%M:%SZ")

# First: all markets sorted by end date, upcoming
fetch_markets({
    "active": "true",
    "closed": "false",
    "order": "startDate",
    "ascending": "false",
    "limit": 200,
}, "200 most recently created markets (categorized)")

# Now specifically look for updown patterns with different timeframes
print("\n\n" + "="*70)
print("  SEARCHING FOR UPDOWN SLUG PATTERNS")
print("="*70)

# Try various assets and timeframes
import time
now_ts = int(now.timestamp())
# Round to nearest 5min
base = now_ts - (now_ts % 300)

assets_to_try = ["btc", "eth", "sol", "xrp", "doge", "bnb", "ada", "avax", "matic", "link", "dot", "atom"]
timeframes = ["1m", "5m", "15m", "30m", "1h"]

found_patterns = set()

for asset in assets_to_try:
    for tf in timeframes:
        slug = f"{asset}-updown-{tf}-{base}"
        try:
            r = requests.get(f"{GAMMA}/markets", params={"slug": slug}, timeout=5)
            data = r.json()
            if data:
                m = data[0]
                liq = m.get("liquidity", "?")
                end = m.get("endDate", "?")
                q = m.get("question", "?")[:80]
                found_patterns.add(f"{asset}-{tf}")
                print(f"  FOUND: {slug}")
                print(f"    liq=${liq}  end={end}")
                print(f"    Q: {q}")
        except:
            pass

# Try offset timestamps for 1m markets
print("\n  --- Trying 1-minute intervals ---")
for asset in ["btc", "eth", "sol", "xrp"]:
    for offset in range(0, 10):
        ts = base + (offset * 60)
        slug = f"{asset}-updown-1m-{ts}"
        try:
            r = requests.get(f"{GAMMA}/markets", params={"slug": slug}, timeout=5)
            data = r.json()
            if data:
                m = data[0]
                liq = m.get("liquidity", "?")
                q = m.get("question", "?")[:80]
                print(f"  FOUND 1m: {slug}  liq=${liq}")
                print(f"    Q: {q}")
                found_patterns.add(f"{asset}-1m")
                break
        except:
            pass

# Try 30m intervals
print("\n  --- Trying 30-minute intervals ---")
base30 = now_ts - (now_ts % 1800)
for asset in ["btc", "eth", "sol", "xrp"]:
    slug = f"{asset}-updown-30m-{base30}"
    try:
        r = requests.get(f"{GAMMA}/markets", params={"slug": slug}, timeout=5)
        data = r.json()
        if data:
            m = data[0]
            liq = m.get("liquidity", "?")
            q = m.get("question", "?")[:80]
            print(f"  FOUND 30m: {slug}  liq=${liq}")
            found_patterns.add(f"{asset}-30m")
    except:
        pass

# Try 1h intervals
print("\n  --- Trying 1-hour intervals ---")
base1h = now_ts - (now_ts % 3600)
for asset in ["btc", "eth", "sol", "xrp"]:
    slug = f"{asset}-updown-1h-{base1h}"
    try:
        r = requests.get(f"{GAMMA}/markets", params={"slug": slug}, timeout=5)
        data = r.json()
        if data:
            m = data[0]
            liq = m.get("liquidity", "?")
            q = m.get("question", "?")[:80]
            print(f"  FOUND 1h: {slug}  liq=${liq}")
            found_patterns.add(f"{asset}-1h")
    except:
        pass

print(f"\n\nSUMMARY - Found patterns: {sorted(found_patterns)}")

# Now look at esports closer
print("\n\n" + "="*70)
print("  ESPORTS MARKETS DETAIL")
print("="*70)
for tag in ["esports", "league-of-legends", "dota", "csgo", "valorant"]:
    try:
        r = requests.get(f"{GAMMA}/events", params={
            "tag_slug": tag,
            "active": "true",
            "closed": "false",
            "limit": 10,
            "order": "endDate",
            "ascending": "true",
        }, timeout=5)
        events = r.json()
        if events:
            print(f"\n  tag_slug={tag}: {len(events)} events")
            for ev in events[:3]:
                title = ev.get("title","?")[:80]
                markets = ev.get("markets", [])
                print(f"    {title} ({len(markets)} mkts)")
                for m in markets[:3]:
                    q = m.get("question","?")[:70]
                    liq = m.get("liquidity","?")
                    end = m.get("endDate","?")
                    out = m.get("outcomes","?")
                    print(f"      liq=${liq} end={end} out={out}")
                    print(f"        {q}")
    except:
        pass
