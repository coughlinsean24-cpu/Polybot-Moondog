"""Deep dive into Polymarket weather markets."""
import requests, json

GAMMA = "https://gamma-api.polymarket.com"

# tag_slug=weather worked — get ALL of them with higher limit
print("=== tag_slug=weather (all, limit=200) ===")
r = requests.get(f"{GAMMA}/events", 
                 params={"tag_slug": "weather", "closed": "false", "limit": "200"}, timeout=15)
events = r.json()
print(f"Total: {len(events)} open weather events\n")

for e in events:
    title = e.get("title", "?")
    slug = e.get("slug", "?")
    end = e.get("endDate", "?")
    markets = e.get("markets", [])
    
    # Filter: look for short-timeframe and/or low-liquidity
    for m in markets:
        if m.get("closed", False):
            continue
        question = m.get("question", "?")
        liq = m.get("liquidity", 0)
        vol = m.get("volume", 0)
        end_m = m.get("endDate", "?")
        outcomes = m.get("outcomes", "[]")
        cond_id = m.get("conditionId", "?")
        
        try:
            liq_f = float(liq) if liq else 0
        except:
            liq_f = 0
        
        print(f"  Q: {question}")
        print(f"    slug={slug}  condId={cond_id[:16]}...")
        print(f"    liq=${liq_f:,.0f}  vol=${float(vol or 0):,.0f}  end={end_m}")
        print(f"    outcomes={outcomes}")
        print()

# Also check if there are short-term weather markets (daily, hourly)
print("\n=== Checking for daily/hourly weather slug patterns ===")
import time
from datetime import datetime, timezone, timedelta

now = datetime.now(timezone.utc)
# Try date-based slugs
for days_offset in range(0, 5):
    d = now + timedelta(days=days_offset)
    date_str = d.strftime("%Y-%m-%d")
    date_compact = d.strftime("%Y%m%d")
    
    for pattern in [
        f"weather-{date_str}",
        f"temperature-{date_str}",
        f"nyc-weather-{date_str}",
        f"nyc-temperature-{date_str}",
        f"chicago-temperature-{date_str}",
        f"high-temp-{date_str}",
        f"daily-temp-{date_str}",
        f"weather-{date_compact}",
    ]:
        try:
            r = requests.get(f"{GAMMA}/events", params={"slug": pattern}, timeout=5)
            data = r.json()
            if data and isinstance(data, list) and len(data) > 0:
                print(f"  HIT: {pattern} -> {data[0].get('title','?')}")
            elif data and isinstance(data, dict) and data.get("title"):
                print(f"  HIT: {pattern} -> {data.get('title','?')}")
        except:
            pass

print("\n=== Checking tag_slug variations ===")
for tag in ["weather", "climate", "temperature", "sports-weather", "daily-weather"]:
    try:
        r = requests.get(f"{GAMMA}/events", 
                        params={"tag_slug": tag, "closed": "false", "limit": "5"}, timeout=8)
        data = r.json()
        if data and len(data) > 0:
            print(f"\n  tag_slug={tag}: {len(data)} results")
            for e in data[:3]:
                print(f"    {e.get('title','?')[:70]}  slug={e.get('slug','?')[:50]}")
    except:
        pass
