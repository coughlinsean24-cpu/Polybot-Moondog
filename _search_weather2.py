"""Search for actual weather/temperature markets on Polymarket."""
import requests, json, time

GAMMA = "https://gamma-api.polymarket.com"

# Try multiple search approaches
searches = [
    {"tag": "weather"},
    {"tag_slug": "weather"},
    {"slug_contains": "weather"},
    {"slug_contains": "temperature"},
    {"slug_contains": "temp-"},
    {"slug_contains": "nyc"},
    {"slug_contains": "chicago"},
    {"slug_contains": "forecast"},
    {"slug_contains": "rain"},
    {"slug_contains": "snow"},
    {"slug_contains": "fahrenheit"},
    {"slug_contains": "celsius"},
    {"slug_contains": "degree"},
]

for params in searches:
    try:
        p = {**params, "closed": "false", "limit": "10"}
        r = requests.get(f"{GAMMA}/events", params=p, timeout=8)
        data = r.json()
        if data and len(data) > 0:
            titles = [e.get("title", "?")[:80] for e in data[:5]]
            slugs = [e.get("slug", "?")[:60] for e in data[:5]]
            print(f"\n--- {params} -> {len(data)} results ---")
            for i, (t, s) in enumerate(zip(titles, slugs)):
                print(f"  [{i}] {s}")
                print(f"      {t}")
    except Exception as ex:
        print(f"  {params} ERROR: {ex}")

# Try searching markets directly (not events)
print("\n\n=== Searching markets directly ===")
for q in ["temperature", "weather", "degrees", "high temperature", "snow"]:
    try:
        r = requests.get(f"{GAMMA}/markets", 
                        params={"closed": "false", "limit": "10", "title": q}, timeout=8)
        data = r.json()
        if data and len(data) > 0:
            print(f"\n--- markets title='{q}' -> {len(data)} results ---")
            for m in data[:5]:
                question = m.get("question", "?")[:80]
                slug = m.get("slug", "?")[:50]
                liq = m.get("liquidity", "?")
                end = m.get("endDate", "?")
                closed = m.get("closed", False)
                print(f"  Q: {question}")
                print(f"    slug={slug}  liq={liq}  end={end}  closed={closed}")
    except Exception as ex:
        print(f"  markets '{q}' ERROR: {ex}")

# Try text search
print("\n\n=== Text search ===")
for q in ["temperature", "weather high", "NYC temperature", "degrees fahrenheit"]:
    try:
        r = requests.get(f"{GAMMA}/events", 
                        params={"closed": "false", "limit": "10", "q": q}, timeout=8)
        data = r.json()
        if data and len(data) > 0:
            print(f"\n--- q='{q}' -> {len(data)} results ---")
            for e in data[:5]:
                title = e.get("title", "?")[:80]
                slug = e.get("slug", "?")[:60]
                end = e.get("endDate", "?")
                mkts = e.get("markets", [])
                print(f"  {slug}")
                print(f"    {title}  end={end}  markets={len(mkts)}")
    except Exception as ex:
        print(f"  q='{q}' ERROR: {ex}")
