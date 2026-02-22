"""
Scan Polymarket Gamma API for ALL short-expiry, low-liquidity markets.
We want binary (Yes/No) markets that:
  - Expire soon (within hours to days)
  - Have low liquidity (thin books = opportunity)
  - Are simple Yes/No (not multi-bracket)
"""
import requests, json, time
from datetime import datetime, timezone, timedelta

GAMMA = "https://gamma-api.polymarket.com"

# ── 1. Fetch recently-created, active, closing-soon events ────────────
def fetch_events(params, label):
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    try:
        r = requests.get(f"{GAMMA}/events", params=params, timeout=10)
        events = r.json()
        if not events:
            print("  (no results)")
            return []
        for ev in events[:15]:
            title = ev.get("title", "?")[:80]
            slug = ev.get("slug", "?")[:60]
            end = ev.get("endDate", "?")
            liquidity = ev.get("liquidity", "?")
            volume = ev.get("volume", "?")
            num_markets = len(ev.get("markets", []))
            print(f"  [{num_markets} mkts] liq=${liquidity}  vol=${volume}  end={end}")
            print(f"    title: {title}")
            print(f"    slug:  {slug}")
            # Show market-level detail for first few
            for m in ev.get("markets", [])[:3]:
                q = m.get("question","?")[:70]
                mliq = m.get("liquidity","?")
                mend = m.get("endDate","?")
                outcomes = m.get("outcomes","?")
                cid = m.get("conditionId","?")[:16]
                print(f"      mkt: {q}")
                print(f"           liq=${mliq} end={mend} outcomes={outcomes} cid={cid}..")
        return events
    except Exception as e:
        print(f"  ERROR: {e}")
        return []

def fetch_markets(params, label):
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    try:
        r = requests.get(f"{GAMMA}/markets", params=params, timeout=10)
        markets = r.json()
        if not markets:
            print("  (no results)")
            return []
        for m in markets[:20]:
            q = m.get("question","?")[:80]
            liq = m.get("liquidity","?")
            vol = m.get("volume","?")
            end = m.get("endDate","?")
            outcomes = m.get("outcomes","?")
            slug = m.get("slug","?")[:60] if m.get("slug") else ""
            event_slug = m.get("eventSlug","?")[:60] if m.get("eventSlug") else ""
            cid = m.get("conditionId","?")[:16]
            closed = m.get("closed", False)
            active = m.get("active", True)
            print(f"  liq=${liq}  vol=${vol}  end={end}  closed={closed}  active={active}")
            print(f"    Q: {q}")
            print(f"    outcomes: {outcomes}")
            print(f"    slug: {slug}")
            print(f"    eventSlug: {event_slug}")
        return markets
    except Exception as e:
        print(f"  ERROR: {e}")
        return []

# ── 2. Try various queries ────────────────────────────────────────────
now = datetime.now(timezone.utc)
today = now.strftime("%Y-%m-%d")
tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
next3 = (now + timedelta(days=3)).strftime("%Y-%m-%d")

print(f"Current UTC: {now.isoformat()}")
print(f"Today: {today}, Tomorrow: {tomorrow}, Next3: {next3}")

# --- Markets endpoint: active, sorted by end date, closing soon ---
fetch_markets({
    "active": "true",
    "closed": "false",
    "end_date_min": today,
    "end_date_max": next3,
    "order": "endDate",
    "ascending": "true",
    "limit": 50,
}, "Markets closing within 3 days (sorted by endDate)")

# --- Markets endpoint: active, low liquidity ---
fetch_markets({
    "active": "true",
    "closed": "false",
    "liquidity_min": 100,
    "liquidity_max": 5000,
    "order": "endDate",
    "ascending": "true",
    "limit": 50,
}, "Active markets with liquidity $100-$5000")

# --- Markets endpoint: recently created ---
fetch_markets({
    "active": "true",
    "closed": "false",
    "order": "startDate",
    "ascending": "false",
    "limit": 50,
}, "Most recently created active markets")

# --- Events: tag_slug for various categories ---
for tag in ["crypto", "sports", "politics", "science", "pop-culture", "business", "gaming"]:
    fetch_events({
        "tag_slug": tag,
        "active": "true",
        "closed": "false",
        "order": "endDate",
        "ascending": "true",
        "limit": 10,
    }, f"Events tag_slug={tag} closing soonest")

# --- Also look at 5-minute style markets beyond crypto ---
fetch_markets({
    "active": "true",
    "closed": "false",
    "order": "endDate",
    "ascending": "true",
    "limit": 100,
}, "ALL active markets closing soonest (limit 100)")
