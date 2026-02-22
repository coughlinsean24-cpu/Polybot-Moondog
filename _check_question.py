import requests, time, json
now = int(time.time())
base = now - (now % 300)
ts = base + 300  # next upcoming
slug = f"btc-updown-5m-{ts}"
resp = requests.get("https://gamma-api.polymarket.com/events", params={"slug": slug}, timeout=8)
data = resp.json()
if data and isinstance(data, list) and len(data) > 0:
    event = data[0]
    # Print all event-level keys
    print("EVENT KEYS:", list(event.keys()))
    # Print description/title
    print("TITLE:", event.get("title"))
    print("DESC:", event.get("description", "")[:200])
    
    m = event.get("markets", [{}])[0]
    print("\nMARKET KEYS:", list(m.keys()))
    print("QUESTION:", m.get("question"))
    print("DESCRIPTION:", m.get("description", "")[:300])
    
    # Check for anything with price/strike/target/threshold
    for key in sorted(m.keys()):
        val = m.get(key)
        if val and isinstance(val, str) and len(val) < 200:
            s = val.lower()
            if any(w in s for w in ["price", "strike", "target", "threshold", "above", "below", "$", "btc", "bitcoin"]):
                print(f"  {key}: {val}")
else:
    print("no data")
