#!/usr/bin/env python3
"""READ-ONLY probe of Polymarket US. Only HTTP GETs. Never places, changes or cancels anything.
Reads the key from env (GitHub Secrets) and prints/writes only aggregate, non-private facts:
status codes and market-type counts. No balances, positions, IDs or keys are ever output.
"""
import base64, collections, json, os, sys, time, urllib.parse, urllib.request, urllib.error

KEY_ID = os.environ.get("POLYMARKET_API_KEY_ID", "").strip()
SECRET = os.environ.get("POLYMARKET_API_KEY_SECRET", "").strip()
GATEWAY, API = "https://gateway.polymarket.us", "https://api.polymarket.us"
UA = {"User-Agent": "paper-prop-placer-probe/1", "Accept": "application/json"}
out = {"ran_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "keys_present": bool(KEY_ID and SECRET), "checks": {}}


def signer():
    from cryptography.hazmat.primitives.asymmetric import ed25519
    pk = ed25519.Ed25519PrivateKey.from_private_bytes(base64.b64decode(SECRET)[:32])
    def headers(method, path):
        ts = str(int(time.time() * 1000))
        sig = base64.b64encode(pk.sign(f"{ts}{method}{path}".encode())).decode()
        return {"X-PM-Access-Key": KEY_ID, "X-PM-Timestamp": ts, "X-PM-Signature": sig, **UA}
    return headers


def get(url, hdr=None):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=hdr or UA), timeout=30) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
        except Exception:
            body = {}
        return e.code, {"code": body.get("code") if isinstance(body, dict) else None}
    except Exception as e:
        return 0, {"error": type(e).__name__}


def types(obj):
    c = collections.Counter()
    def walk(x):
        if isinstance(x, dict):
            t = x.get("sportsMarketType") or x.get("marketType")
            if t and ("question" in x or "slug" in x):
                c[t] += 1
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(obj)
    return dict(c)


# 1. public: NBA events and which market types each game carries
st, d = get(f"{GATEWAY}/v2/leagues/nba/events?limit=50")
ev = (d or {}).get("events", []) if st == 200 else []
out["checks"]["public_nba_events"] = {"status": st, "events": len(ev), "market_types": types(ev),
                                      "max_markets_per_event": max([len(e.get("markets", [])) for e in ev] or [0])}
# 2. public: player reference + any prop-looking search results
st, d = get(f"{GATEWAY}/v1/sports/players?limit=1")
out["checks"]["public_sports_players"] = {"status": st}
for q in ("points", "rebounds", "assists", "player prop"):
    st, d = get(f"{GATEWAY}/v1/search?query={urllib.parse.quote(q)}")
    out["checks"][f"public_search_{q.replace(' ', '_')}"] = {"status": st, "market_types": types(d) if st == 200 else {}}

# 3. authenticated, read-only
if out["keys_present"]:
    try:
        h = signer()
    except Exception as e:
        out["checks"]["auth_setup"] = {"ok": False, "error": type(e).__name__}
        h = None
    if h:
        st, _ = get(f"{API}/v1/portfolio/positions", h("GET", "/v1/portfolio/positions"))
        out["checks"]["auth_portfolio_positions"] = {"status": st, "authenticated": st == 200}
        path, q = "/v1/markets", "?limit=200&active=true&closed=false"
        st, d = get(API + path + q, h("GET", path))          # signature over the path only (per docs)
        if st in (401, 403):
            st, d = get(API + path + q, h("GET", path + q))  # fall back to signing the query too
        out["checks"]["auth_markets"] = {"status": st, "market_types": types(d) if st == 200 else {}}
else:
    out["checks"]["auth"] = "skipped: secrets not available to this run"

print(json.dumps(out, indent=1))
json.dump(out, open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe_result.json"), "w"), indent=1)
