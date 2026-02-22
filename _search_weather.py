"""Quick script to find weather markets on Polymarket."""
import requests, json

# Search for weather-tagged events
print("=== Searching weather tag ===")
r = requests.get("https://gamma-api.polymarket.com/events", 
                  params={"tag": "weather", "closed": "false", "limit": "50"}, timeout=10)
events = r.json()
print(f"Found {len(events)} weather events")
for e in events[:20]:
    slug = e.get("slug", "?")
    title = e.get("title", "?")
    end = e.get("endDate", "?")
    markets = e.get("markets", [])
    print(f"\n  slug: {slug}")
    print(f"  title: {title}")
    print(f"  endDate: {end}")
    print(f"  markets: {len(markets)}")
    for m in markets[:3]:
        q = m.get("question", "?")
        liq = m.get("liquidity", "?")
        vol = m.get("volume", "?")
        closed = m.get("closed", "?")
        outcomes = m.get("outcomes", "?")
        tokens = m.get("clobTokenIds", "?")
        end_m = m.get("endDate", "?")
        print(f"    Q: {q}")
        print(f"    liquidity={liq}  volume={vol}  closed={closed}")
        print(f"    outcomes={outcomes}  endDate={end_m}")

# Also search by text
print("\n\n=== Searching 'temperature' in title ===")
r2 = requests.get("https://gamma-api.polymarket.com/events",
                   params={"closed": "false", "limit": "50", "title": "temperature"}, timeout=10)
events2 = r2.json()
print(f"Found {len(events2)} temperature events")
for e in events2[:10]:
    slug = e.get("slug", "?")
    title = e.get("title", "?")
    end = e.get("endDate", "?")
    markets = e.get("markets", [])
    print(f"\n  slug: {slug}")
    print(f"  title: {title}")
    print(f"  endDate: {end}")
    print(f"  markets: {len(markets)}")
    for m in markets[:2]:
        q = m.get("question", "?")
        liq = m.get("liquidity", "?")
        print(f"    Q: {q}  liq={liq}")

# Also try searching for short-timeframe weather patterns
print("\n\n=== Searching for weather-5m or weather-1h patterns ===")
import time
now = int(time.time())
base = now - (now % 300)
for offset in range(0, 6):
    ts = base + offset * 300
    for tag in ["weather", "temp", "nyc-temp", "temperature"]:
        slug = f"{tag}-5m-{ts}"
        r3 = requests.get("https://gamma-api.polymarket.com/events",
                          params={"slug": slug}, timeout=5)
        data = r3.json()
        if data and isinstance(data, list) and len(data) > 0:
            print(f"  HIT: {slug} -> {data[0].get('title','?')}")
        elif data and isinstance(data, dict) and data.get("title"):
            print(f"  HIT: {slug} -> {data.get('title','?')}")
