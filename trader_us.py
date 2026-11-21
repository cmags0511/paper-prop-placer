#!/usr/bin/env python3
"""Polymarket US trader: NBA same-game 3-leg parlays on the exchange your US account actually trades.

Same strategy as the paper trader (prop tracker model -> best 3-leg parlay per game, every listed parlay is bet, dropped
ones stay on the record), but legs are priced from Polymarket US "at least N" player markets.

MODES (nothing is ever ordered unless LIVE=1 is set, from the repo variable LIVE_TRADING):
  paper : ledger only. Paper price = product of leg asks, less fee_friction.
  shadow: (keys present, LIVE unset) also creates the real combo and PREVIEWS a real order to log the executable price.
          No order is placed.
  live  : (LIVE=1 and keys present) places an IMMEDIATE-OR-CANCEL limit buy on the combo at the highest price that still
          meets live.min_ev, within live caps. It can only fill at or below that price.
"""
import argparse, base64, itertools, json, math, os, sys, time, datetime as dt
import urllib.request, urllib.error
from zoneinfo import ZoneInfo
import trader as T

ET = ZoneInfo("America/New_York")
HERE = os.path.dirname(os.path.abspath(__file__))
GATEWAY, API = "https://gateway.polymarket.us", "https://api.polymarket.us"
ESPN = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/"
UA = {"User-Agent": "paper-prop-placer/2", "Accept": "application/json"}


# ------------------------------------------------------------------ US market data (public)
def qv(q):
    try:
        return float(q["value"])
    except Exception:
        return None


def us_markets(cfg, now):
    """{(name, stat, N): market} for upcoming NBA games; 'Yes' = player reaches at least N"""
    ev = T.get(f"{GATEWAY}/v2/leagues/nba/events?limit=50")["events"]
    horizon = now + dt.timedelta(hours=cfg["horizon_hours"])
    out = {}
    for e in ev:
        st = T.iso(e.get("startDate") or e["startTime"])
        if e.get("live") or e.get("ended") or st <= now or st > horizon:
            continue
        try:
            d = T.get(f"{GATEWAY}/v1/events/slug/{e['slug']}")["event"]
        except Exception:
            continue
        for m in d.get("markets", []):
            stat = cfg["markets"].get(m.get("sportsMarketType"))
            if not stat or not m.get("active") or m.get("closed") or not m.get("comboEnabled"):
                continue
            mm = __import__("re").match(r"Will (.+?) record at least (\d+) ", m.get("question", ""))
            if not mm:
                continue
            out[(T.norm(mm.group(1)), stat, int(mm.group(2)))] = {
                "id": m["id"], "slug": m["slug"], "q": m["question"], "start": d.get("startTime") or e["startDate"],
                "bid": qv(m.get("bestBidQuote")), "ask": qv(m.get("bestAskQuote")), "tick": m.get("orderPriceMinTickSize")}
    return out


def price_legs(pool, idx, cfg):
    legs, st = [], {"matched": 0, "priced": 0}
    for c in pool:
        if c["line"] % 1 != .5:
            continue                                   # whole-number lines can push; US markets are "at least N"
        m = idx.get((T.norm(c["name"]), c["market"], int(c["line"]) + 1))
        if not m or abs((T.iso(m["start"]) - T.iso(c["t"])).total_seconds()) > 6 * 3600:
            continue
        st["matched"] += 1
        yb, ya = m["bid"], m["ask"]
        if yb is None or ya is None or ya - yb > cfg["max_spread"]:
            continue
        ask, bid = (ya, yb) if c["side"] == "over" else (1 - yb, 1 - ya)
        if f'{c["market"]}:{c["side"]}' in cfg.get("block_legs", []):
            continue                                   # e.g. assist unders: the model was far too confident on these
        mid = (ask + bid) / 2
        pb = cfg.get("market_blend", 0) * mid + (1 - cfg.get("market_blend", 0)) * c["p"]   # shrink model toward the market
        if c["p"] <= ask or not cfg["leg_min_edge"] <= pb - ask <= cfg["leg_max_edge"]:
            continue                                   # need an edge on BOTH the model and the blend
        st["priced"] += 1
        legs.append(dict(c, p_model=c["p"], p=round(pb, 4), market_id=m["id"], slug=m["slug"], N=int(c["line"]) + 1, ask=round(ask, 4), bid=round(bid, 4),
                         dec=1 / ask, pm_q=m["q"]))
    return legs, st


# ------------------------------------------------------------------ settlement from ESPN box scores
def espn_games(dates):
    g = {}
    for d in dates:
        try:
            sb = T.get(ESPN + "scoreboard?dates=" + d)
        except Exception:
            continue
        for e in sb["events"]:
            c = e["competitions"][0]["competitors"]
            g["|".join(sorted(x["team"]["abbreviation"] for x in c))] = {"id": e["id"], "state": e["status"]["type"]["state"]}
    return g


def espn_box(eid):
    d = T.get(ESPN + "summary?event=" + eid)
    out = {}
    for t in d["boxscore"]["players"]:
        st = t["statistics"][0]
        lb = st["labels"]
        arr = []
        for a in st["athletes"]:
            v = a.get("stats") or []
            if not v:
                continue
            try:
                pts, reb, ast, tpm = int(v[lb.index("PTS")]), int(v[lb.index("REB")]), int(v[lb.index("AST")]), int(v[lb.index("3PT")].split("-")[0])
            except Exception:
                continue
            arr.append((T.norm(a["athlete"]["displayName"]).split(), {"pts": pts, "reb": reb, "ast": ast, "3pm": tpm, "pra": pts + reb + ast}))
        out[t["team"]["abbreviation"]] = arr
    return out


def find_player(box, team, name):
    toks = T.norm(name).split()
    arr = box.get(team, [])
    return (next((o for t, o in arr if t == toks), None) or
            next((o for t, o in arr if t and toks and t[-1] == toks[-1] and t[0][0] == toks[0][0]), None))


def leg_game(p, l):
    return "|".join(sorted([l["team"], l["opp"]])) if l.get("opp") else "|".join(sorted(p["teams"]))


def settle(L, now):
    def last_t(p):
        return max(T.iso(l.get("t") or p["game_t"]) for l in p["legs"])
    open_ = [p for p in L["parlays"] if p["status"] == "open" and now > last_t(p) + dt.timedelta(hours=2)]
    if not open_:
        return 0
    dates = {T.iso(l.get("t") or p["game_t"]).astimezone(ET).strftime("%Y%m%d") for p in open_ for l in p["legs"]}
    games = espn_games(sorted(dates))
    boxes, n = {}, 0
    for p in open_:
        gs = {leg_game(p, l) for l in p["legs"]}
        if any(games.get(k, {}).get("state") != "post" for k in gs):
            continue
        ok = True
        for k in gs:
            gid = games[k]["id"]
            if gid not in boxes:
                try:
                    boxes[gid] = espn_box(gid)
                except Exception:
                    ok = False
        if not ok:
            continue
        lost, voids = False, 1.0
        for l in p["legs"]:
            box = boxes[games[leg_game(p, l)]["id"]]
            o = find_player(box, l["team"], l["name"])
            if o is None:
                l["result"] = "void"
                voids *= 1 / l["ask"]
                continue
            hit = o[l["market"]] >= l["N"]
            l["actual"] = o[l["market"]]
            l["result"] = "won" if (hit == (l["side"] == "over")) else "lost"
            lost |= l["result"] == "lost"
        if lost:
            p.update(status="lost", payout=0.0, pnl=round(-p["stake"], 2))
        else:
            dec = p["dec"] / voids
            pay = round(p["stake"] * dec, 2)
            p.update(status="won" if dec > 1.0001 else "void", payout=pay, pnl=round(pay - p["stake"], 2))
        p["settled"] = now.isoformat(timespec="minutes")
        p["settled_by"] = "espn box score"
        L["cash"] = round(L["cash"] + p["payout"], 2)
        n += 1
    return n


# ------------------------------------------------------------------ real exchange access (signed)
class US:
    def __init__(self, key_id, secret_b64, base=API):
        from cryptography.hazmat.primitives.asymmetric import ed25519
        self.kid, self.base = key_id, base
        self.pk = ed25519.Ed25519PrivateKey.from_private_bytes(base64.b64decode(secret_b64)[:32])

    def req(self, method, path, body=None):
        st, d = self._req(method, path, body, path.split("?")[0])      # docs: sign the path without the query
        if st in (401, 403) and "?" in path:
            st, d = self._req(method, path, body, path)                # fallback: sign with the query
        return st, d

    def _req(self, method, path, body, signed_path):
        ts = str(int(time.time() * 1000))
        h = {"X-PM-Access-Key": self.kid, "X-PM-Timestamp": ts,
             "X-PM-Signature": base64.b64encode(self.pk.sign(f"{ts}{method}{signed_path}".encode())).decode(),
             "Content-Type": "application/json", **UA}
        r = urllib.request.Request(self.base + path, headers=h, method=method, data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(r, timeout=30) as x:
                return x.status, json.loads(x.read() or b"null")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read())
            except Exception:
                return e.code, {}
        except Exception as e:
            return 0, {"error": type(e).__name__}


def live_spent(L, now, today_only):
    """today_only: cost bought today (ET). Otherwise: cost of live parlays still open (the exposure cap)."""
    d = now.astimezone(ET).date()
    return sum(p["live"].get("max_cost", 0) for p in L["parlays"] if p.get("live", {}).get("ordered")
               and ((T.iso(p["placed"]).astimezone(ET).date() == d) if today_only else p["status"] == "open"))


def fnum(x):
    if isinstance(x, dict):
        x = x.get("value")
    try:
        return float(x)
    except Exception:
        return 0.0


def exec_summary(resp):
    o = ((resp or {}).get("order") or {}) if "order" in (resp or {}) else {}
    ex = (resp or {}).get("executions") or []
    last = (ex[-1].get("order") if ex else None) or o
    keep = ("state", "avgPx", "cumQuantity", "commissionNotionalTotalCollected")
    s = {k: (fnum(last.get(k)) if k != "state" else last.get(k)) for k in keep if isinstance(last, dict) and last.get(k) is not None}
    rej = [x.get("rejectReason") or (x.get("order") or {}).get("rejectReason") for x in ex if isinstance(x, dict)]
    if any(rej):
        s["reject"] = [r for r in rej if r]
    return s



def rfq_quotes(us, par, cfg):
    """QUOTE-ONLY: create the combo and an RFQ, read the quotes market makers send, close the RFQ. Never accepts, never spends."""
    lv, rec = cfg["live"], {"mode": "rfq_quote", "ordered": False}
    legs = [{"symbol": l["slug"], "side": "SIDE_BUY" if l["side"] == "over" else "SIDE_SELL"} for l in par["legs"]]
    st, d = us.req("POST", "/v1/combos", {"legs": legs})
    combo = (d or {}).get("combo") or {}
    if st != 200 or not combo.get("id"):
        rec["error"] = f"combo create failed ({st})"
        return rec
    rec["combo"], rec["max_price"] = combo["id"], round(min(0.99, par["p"] / (1 + lv["min_ev"])), 4)
    st, d = us.req("POST", "/v1/rfqs", {"symbol": combo["id"], "cashOrderQty": f"{lv['unit']:.2f}", "restRemainder": False})
    rec["rfq_status"] = st
    rid = (d or {}).get("rfqId") or (d or {}).get("id")
    if not rid:
        rec["error"] = "rfq create: " + json.dumps(d)[:300]
        return rec
    seen = []
    for i in range(6):
        time.sleep(2)
        for q in (f"rfqId={rid}", f"rfq_id={rid}"):
            st, d = us.req("GET", "/v1/rfqs/quotes?" + q)
            if st == 200:
                break
        else:
            rec["quotes_status"] = st
            rec["quotes_error"] = json.dumps(d)[:200]
            continue
        seen = [x for x in (d or {}).get("quotes", [])]
        if seen:
            break
    rec["quotes"] = [{k: (x.get(k) if not isinstance(x.get(k), dict) else x[k].get("value")) for k in ("buyPrice", "buyQtyDecimal", "status")} for x in seen][:5]
    us.req("DELETE", f"/v1/rfqs/{rid}")
    return rec


def rfq_buy(us, par, L, cfg, now, save):
    """Create the combo + RFQ, take the cheapest firm quote at or below max price (EV guard), within live caps."""
    lv, rec = cfg["live"], {"mode": "rfq_live", "ordered": False}
    legs = [{"symbol": l["slug"], "side": "SIDE_BUY" if l["side"] == "over" else "SIDE_SELL"} for l in par["legs"]]
    st, d = us.req("POST", "/v1/combos", {"legs": legs})
    combo = (d or {}).get("combo") or {}
    if st != 200 or not combo.get("id"):
        rec["error"] = f"combo create failed ({st}) {json.dumps(d)[:160]}"
        return rec
    pmax = min(0.99, par["p"] / (1 + lv["min_ev"]))
    rec.update(combo=combo["id"], max_price=round(pmax, 4))
    st, d = us.req("POST", "/v1/rfqs", {"symbol": combo["id"], "cashOrderQty": f"{lv['unit']:.2f}", "restRemainder": False})
    rid = (d or {}).get("rfqId") or (d or {}).get("id")
    if not rid:
        rec["error"] = "rfq create: " + json.dumps(d)[:200]
        return rec
    rec["rfq"] = rid
    best = None
    for _ in range(8):
        time.sleep(2)
        st, d = us.req("GET", f"/v1/rfqs/quotes?rfqId={rid}")
        qs = [q for q in ((d or {}).get("quotes") or []) if q.get("status") == "QUOTE_STATUS_ACTIVE" and fnum(q.get("buyPrice")) > 0]
        if qs:
            best = min(qs, key=lambda q: fnum(q["buyPrice"]))
            rec["best_quote"], rec["n_quotes"] = fnum(best["buyPrice"]), len(qs)
            if _ >= 2:
                break
    if not best:
        us.req("DELETE", f"/v1/rfqs/{rid}")
        rec["error"] = "no quotes"
        return rec
    px, qty = fnum(best["buyPrice"]), fnum(best.get("buyQtyDecimal"))
    cost = px * qty
    rec.update(qty=qty, max_cost=round(cost, 2))
    if px > pmax:
        us.req("DELETE", f"/v1/rfqs/{rid}")
        rec["error"] = f"best quote {px} above my max {rec['max_price']}"
        return rec
    if cost > lv["unit"] * 1.1 or qty <= 0:
        us.req("DELETE", f"/v1/rfqs/{rid}")
        rec["error"] = f"quote size/cost off ({qty} x {px})"
        return rec
    if live_spent(L, now, True) + cost > lv["daily_cap"] or live_spent(L, now, False) + cost > lv["total_cap"]:
        us.req("DELETE", f"/v1/rfqs/{rid}")
        rec["error"] = "live cap reached"
        return rec
    rec["ordered"], par["live"] = True, rec                  # recorded first so a crash can never double-buy
    save()
    st, d = us.req("PUT", f"/v1/rfqs/{rid}/quotes/{best['id']}/accept", {"acceptedSide": "SIDE_BUY"})
    rec["accept_status"] = st
    if st != 200:
        rec["error"] = f"accept failed ({st}): " + json.dumps(d)[:200]
        return rec
    q = {}
    for _ in range(5):                                         # the maker has a ~3s last look; read the outcome
        time.sleep(2)
        st, d = us.req("GET", f"/v1/rfqs/quotes?rfqId={rid}&quoteId={best['id']}")
        q = next(iter((d or {}).get("quotes") or []), {}) if st == 200 else {}
        if q.get("executedTime") or q.get("status") not in (None, "QUOTE_STATUS_ACTIVE", "QUOTE_STATUS_ACCEPTED", "QUOTE_STATUS_CONFIRMED"):
            break
    rec["quote_status"] = q.get("status")
    rec["verified"] = bool(q.get("executedTime") or q.get("rfqCreatorOrderId"))
    if rec["verified"] or q.get("status") in (None, "QUOTE_STATUS_ACTIVE", "QUOTE_STATUS_ACCEPTED", "QUOTE_STATUS_CONFIRMED"):
        rec.update(filled=True, qty_filled=qty, avg_px=px, cost=round(cost, 4))
    else:
        rec["error"] = "maker declined: " + str(q.get("status"))
    return rec


def execute(us, par, L, cfg, now, live, save):
    """create the real combo, preview a real order, and (live only) place it. Returns a small, non-private record."""
    lv, rec = cfg["live"], {"mode": "live" if live else "shadow", "ordered": False}
    legs = [{"symbol": l["slug"], "side": "SIDE_BUY" if l["side"] == "over" else "SIDE_SELL"} for l in par["legs"]]
    st, d = us.req("POST", "/v1/combos", {"legs": legs})
    combo = (d or {}).get("combo") or {}
    rec["combo_status"] = st
    if st != 200 or not combo.get("id"):
        rec["error"] = f"combo create failed ({st})"
        return rec
    rec["combo"], rec["combo_state"] = combo["id"], combo.get("state")
    tick = combo.get("tickSize") or 0.01
    tick = tick if 0.0001 <= tick <= 0.1 else 0.01
    pmax = min(0.99, par["p"] / (1 + lv["min_ev"]))            # highest price that still leaves min_ev on our chance
    pmax = math.floor(pmax / tick + 1e-9) * tick
    if pmax < 0.01:
        rec["error"] = "no price meets min_ev"
        return rec
    qty = math.floor(lv["unit"] * 0.92 / pmax * 100) / 100       # keep ~8% of the unit free for fees
    rec.update(max_price=round(pmax, 4), qty=qty, max_cost=round(qty * pmax, 2))
    body = {"marketSlug": combo["id"], "type": "ORDER_TYPE_LIMIT", "price": {"value": f"{pmax:.4f}".rstrip("0").rstrip(".") if tick < 0.01 else f"{pmax:.2f}", "currency": "USD"},
            "quantity": qty, "tif": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL", "intent": "ORDER_INTENT_BUY_LONG",
            "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC"}
    st, d = us.req("POST", "/v1/order/preview", {"request": body})
    rec["preview_status"], rec["preview"] = st, exec_summary(d)
    if not live:
        return rec
    if st != 200 or not rec["preview"].get("avgPx") or not rec["preview"].get("cumQuantity"):
        rec["error"] = "preview showed no fill at an acceptable price"
        return rec
    if live_spent(L, now, True) + rec["max_cost"] > lv["daily_cap"] or live_spent(L, now, False) + rec["max_cost"] > lv["total_cap"]:
        rec["error"] = "live cap reached"
        return rec
    rec["ordered"] = True                                          # recorded first so a crash can never double-order
    par["live"] = rec
    save()
    st, d = us.req("POST", "/v1/orders", dict(body, synchronousExecution=True, maxBlockTime="5"))
    rec["order_status"], rec["order_id"], rec["result"] = st, (d or {}).get("id"), exec_summary(d)
    if st != 200:
        rec["error"] = f"order failed ({st})"
    r = rec["result"]
    try:
        q, px = fnum(r.get("cumQuantity")), fnum(r.get("avgPx"))
    except Exception:
        q = px = 0
    if q > 0 and px > 0:
        fee = fnum(r.get("commissionNotionalTotalCollected"))
        rec.update(filled=True, qty_filled=q, avg_px=px, cost=round(q * px + fee, 4))
    return rec


def account_snapshot(us):
    """real balance + positions, from the exchange (read-only). Values only; no ids or per-position detail."""
    st, d = us.req("GET", "/v1/account/balances")
    b = next(iter((d or {}).get("balances") or []), None) if st == 200 else None
    if not b:
        return None
    acc = {"balance": fnum(b.get("currentBalance")), "buying_power": fnum(b.get("buyingPower")), "open_orders": fnum(b.get("openOrders")),
           "positions_value": fnum(b.get("assetNotional"))}
    st, d = us.req("GET", "/v1/portfolio/positions")
    if st == 200 and isinstance(d, dict):
        pos = d.get("positions")
        if isinstance(pos, dict):
            pos = list(pos.values())
        acc["positions_count"] = len(pos) if isinstance(pos, list) else None
    return acc


def cross_pass(L, legs, cfg, now, us, live, run, rows_ok=True):
    """2-leg parlays across DIFFERENT games. Legs are independent, so chance = product of the two leg chances."""
    cx = cfg["cross"]
    cand = [c for c in legs if (T.iso(c["t"]) - now).total_seconds() / 60 > cx["min_mins_to_tip"]]
    cand = sorted(cand, key=lambda c: c["ask"] - c["p"])[:cx["pool"]]
    pairs = []
    for a, b in itertools.combinations(cand, 2):
        if {a["team"], a["opp"]} == {b["team"], b["opp"]}:
            continue                                   # same game: handled by the 3-leg logic
        p = a["p"] * b["p"]
        dec_p = a["dec"] * b["dec"]
        ev = p * dec_p * (1 - cfg.get("fee_friction", 0)) - 1
        if ev >= cfg["min_ev"] and p >= cx["min_joint_p"]:
            pairs.append((ev, p, dec_p, a, b))
    pairs.sort(key=lambda x: -x[0])
    used, top = set(), []
    for ev, p, dec_p, a, b in pairs:
        if a["key"] in used or b["key"] in used:
            continue
        used |= {a["key"], b["key"]}
        top.append((ev, p, dec_p, a, b))
        if len(top) >= cx["max_listed"]:
            break
    have = {q["id"]: q for q in L["parlays"] if q.get("cross")}
    held = {l["key"] for q in have.values() if q["status"] == "open" for l in q["legs"]}
    ids_now = set()
    orders = 0
    today = now.astimezone(ET).date()
    for ev, p, dec_p, a, b in top:
        combo = [a, b]
        pid_ = T.pid(["x"], combo)
        ids_now.add(pid_)
        if pid_ in have or a["key"] in held or b["key"] in held:
            continue
        spent_today = sum(q["stake"] for q in L["parlays"] if T.iso(q["placed"]).astimezone(ET).date() == today)
        open_risk = sum(q["stake"] for q in L["parlays"] if q["status"] == "open")
        if cfg["unit"] > L["cash"] or spent_today + cfg["unit"] > cfg["daily_risk_cap"] or \
                open_risk + cfg["unit"] > cfg["max_open_risk_pct"] * cfg["start_bankroll"]:
            run["notes"].append("2-leg: risk cap hit")
            break
        if orders >= cx["max_per_run"]:
            break
        first = min(combo, key=lambda c: c["t"])
        label = f"{a['name']} + {b['name']}"
        par = {"id": pid_, "venue": cfg["venue"], "game": "x|" + pid_, "game_t": first["t"], "cross": True,
               "teams": ["2-LEG", "+".join(c["team"] for c in combo)], "placed": now.isoformat(timespec="minutes"),
               "mins_to_tip": round((T.iso(first["t"]) - now).total_seconds() / 60), "stake": cfg["unit"],
               "p_indep": round(p, 3), "corr_factor": 1.0, "p": round(p, 3), "dec_product": round(dec_p, 3),
               "dec": round(dec_p * (1 - cfg.get("fee_friction", 0)), 3), "ev": round(ev, 3), "pairs": [], "status": "open",
               "payout": None, "pnl": None, "listed": True, "listed_at_tip": None,
               "legs": [{"key": c["key"], "name": c["name"], "team": c["team"], "opp": c["opp"], "t": c["t"], "market": c["market"],
                         "label": c["label"], "side": c["side"], "line": c["line"], "N": c["N"], "slug": c["slug"], "p": round(c["p"], 3),
                         "ask": c["ask"], "bid": c["bid"], "market_id": c["market_id"], "idx": 0 if c["side"] == "over" else 1,
                         "q": c["pm_q"]} for c in combo]}
        orders += 1
        if us and live:
            par["live"] = rfq_buy(us, par, L, cfg, now, lambda: (L["parlays"].append(par) if par not in L["parlays"] else None, None))
            lv = par["live"]
            if not lv.get("filled"):
                if par in L["parlays"]:
                    L["parlays"].remove(par)
                run["notes"].append(f"2-leg {label}: NOT bet ({lv.get('error') or 'no fill'}; my max {lv.get('max_price')}, model p {par['p']})")
                continue
            par["stake"], par["dec"], par["qty"] = lv["cost"], round(lv["qty_filled"] / lv["cost"], 3), lv["qty_filled"]
        elif not (us and live):
            run["notes"].append("2-leg: not live, skipped")
            continue
        if par not in L["parlays"]:
            L["parlays"].append(par)
        L["cash"] = round(L["cash"] - par["stake"], 2)
        run["placed"] += 1
        run["notes"].append(f"2-leg {label}: bet, p={par['p']}, pays x{par['dec']}, ev={par['ev']}")
    for q in have.values():
        if q["status"] == "open" and T.iso(q["game_t"]) > now:
            q["listed"] = q["id"] in ids_now
            if not q["listed"] and not q.get("dropped_at"):
                q["dropped_at"] = now.isoformat(timespec="minutes")
            if q["listed"]:
                q.pop("dropped_at", None)
            if (T.iso(q["game_t"]) - now).total_seconds() / 60 <= cfg["tipoff_window_min"]:
                q["listed_at_tip"] = q["listed"]


# ------------------------------------------------------------------ main
def new_ledger(cfg):
    return {"venue": cfg["venue"], "start_bankroll": cfg["start_bankroll"], "unit": cfg["unit"], "cash": cfg["start_bankroll"],
            "parlays": [], "equity": [], "runs": []}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", default=os.path.join(HERE, "ledger.json"))
    ap.add_argument("--tracker", default=os.environ.get("PROP_TRACKER_DIR") or os.path.join(HERE, "prop-tracker"))
    ap.add_argument("--now")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    cfg = json.load(open(os.path.join(HERE, "config.json")))
    now = T.iso(a.now) if a.now else dt.datetime.now(dt.timezone.utc)
    L = json.load(open(a.ledger)) if os.path.exists(a.ledger) else new_ledger(cfg)
    kid, sec = os.environ.get("POLYMARKET_API_KEY_ID", "").strip(), os.environ.get("POLYMARKET_API_KEY_SECRET", "").strip()
    us, live = None, False
    if kid and sec:
        try:
            us = US(kid, sec)
            live = (os.environ.get("LIVE", "").strip() == "1" or cfg["live"].get("enabled")) and not cfg["paused"]
        except Exception:
            us = None
    mode = "live" if live else "shadow" if us else "paper"
    run = {"t": now.isoformat(timespec="minutes"), "mode": mode, "settled": 0, "placed": 0, "notes": []}

    def save():
        if not a.dry:
            json.dump(L, open(a.ledger, "w"), separators=(",", ":"))

    run["settled"] = settle(L, now)
    if cfg["paused"]:
        run["notes"].append("paused")
    elif not us:
        run["notes"].append("no keys")
    elif False:
        run["notes"].append("paused")
    else:
        PL, PK, GM, rows, slate, out = T.load_tracker(a.tracker)
        pool = T.leg_pool(PK, PL, GM, slate, out, cfg, now)
        idx = us_markets(cfg, now)
        legs, st = price_legs(pool, idx, cfg)
        run["notes"].append(f"{len(pool)} tracker legs, {st['matched']} matched on Polymarket US, {st['priced']} priced")
        games = {}
        for c in legs:
            games.setdefault((c["t"], *sorted((c["team"], c["opp"]))), []).append(c)
        by_game = {}
        for p in L["parlays"]:
            if not p.get("cross"):                      # 2-leg cross-game parlays are handled by cross_pass
                by_game.setdefault(p["game"], []).append(p)
        today = now.astimezone(ET).date()
        spent_today = sum(p["stake"] for p in L["parlays"] if T.iso(p["placed"]).astimezone(ET).date() == today)
        orders = 0
        for gk in sorted({*("|".join(k) for k in games), *(k for k in by_game if T.iso(k.split("|")[0]) > now)}):
            parts = gk.split("|")
            mins = (T.iso(parts[0]) - now).total_seconds() / 60
            if mins <= 0:
                continue
            gl = games.get(tuple(parts), [])
            best = T.best_combo(sorted(gl, key=lambda c: c["ask"] - c["p"])[:cfg["max_legs_per_game"]], rows, PL, cfg) if gl else None
            best_id = T.pid(parts, best["combo"]) if best else None
            label = f"{parts[1]}/{parts[2]}"
            mine = by_game.setdefault(gk, [])
            if best and best_id not in {p["id"] for p in mine}:
                open_risk = sum(p["stake"] for p in L["parlays"] if p["status"] == "open")
                if len(mine) >= cfg["max_bets_per_game"]:
                    run["notes"].append(f"{label}: listing changed but {cfg['max_bets_per_game']} bets already on this game")
                elif cfg["unit"] > L["cash"] or spent_today + cfg["unit"] > cfg["daily_risk_cap"] or \
                        open_risk + cfg["unit"] > cfg["max_open_risk_pct"] * cfg["start_bankroll"]:
                    run["notes"].append(f"{label}: risk cap hit")
                else:
                    par = {"id": best_id, "venue": cfg["venue"], "game": gk, "game_t": parts[0], "teams": [parts[1], parts[2]],
                           "placed": now.isoformat(timespec="minutes"), "mins_to_tip": round(mins), "stake": cfg["unit"],
                           "p_indep": round(best["p_indep"], 3), "corr_factor": round(best["factor"], 3), "p": round(best["p"], 3),
                           "dec_product": round(best["dec_product"], 3), "dec": round(best["dec"], 3), "ev": round(best["ev"], 3),
                           "pairs": best["pairs"], "status": "open", "payout": None, "pnl": None, "listed": True, "listed_at_tip": None,
                           "legs": [{"key": c["key"], "name": c["name"], "team": c["team"], "market": c["market"], "label": c["label"],
                                     "side": c["side"], "line": c["line"], "N": c["N"], "slug": c["slug"], "p": round(c["p"], 3),
                                     "ask": c["ask"], "bid": c["bid"], "market_id": c["market_id"],
                                     "idx": 0 if c["side"] == "over" else 1, "q": c["pm_q"]} for c in best["combo"]]}
                    if us and cfg["live"].get("mode") == "rfq_quote":
                        if orders < cfg["live"]["max_orders_per_run"]:
                            orders += 1
                            q = rfq_quotes(us, par, cfg)
                            run["notes"].append(f"{label}: QUOTE-ONLY, my max {q.get('max_price')}, model p {par['p']}, result {json.dumps({k: v for k, v in q.items() if k not in ('mode', 'ordered', 'combo')})[:600]}")
                        continue
                    if us and cfg["live"].get("mode") == "rfq_live":
                        if orders < cfg["live"]["max_orders_per_run"]:
                            orders += 1
                            par["live"] = rfq_buy(us, par, L, cfg, now, lambda: (L["parlays"].append(par) if par not in L["parlays"] else None, save()))
                    elif us and orders < cfg["live"]["max_orders_per_run"]:
                        orders += 1
                        par["live"] = execute(us, par, L, cfg, now, live, lambda: (L["parlays"].append(par) if par not in L["parlays"] else None, save()))
                    lvr = par.get("live") or {}
                    if live and not lvr.get("filled"):
                        if par in L["parlays"]:
                            L["parlays"].remove(par)
                        run["notes"].append(f"{label}: NOT bet ({lvr.get('error') or 'no fill'}; my max price {lvr.get('max_price')}, model p {par['p']}, preview {lvr.get('preview')}, legs asks product {par['dec_product']})")
                        continue
                    if live:
                        par["stake"], par["dec"], par["qty"] = lvr["cost"], round(lvr["qty_filled"] / lvr["cost"], 3), lvr["qty_filled"]
                    if not a.dry:
                        if par not in L["parlays"]:
                            L["parlays"].append(par)
                        mine.append(par)
                        L["cash"] = round(L["cash"] - par["stake"], 2)
                        spent_today += par["stake"]
                    run["placed"] += 1
                    lv = par.get("live") or {}
                    run["notes"].append(f"{label}: listed and bet, p={par['p']}, paper pays x{par['dec']}, ev={par['ev']}"
                                        + (f", {lv['mode']} preview {lv.get('preview') or lv.get('error')}" if lv else ""))
            elif not best:
                run["notes"].append(f"{label}: no qualifying parlay listed")
            live_px = {l["key"]: l for l in gl}
            for p in mine:
                p["listed"] = p["id"] == best_id
                if not p["listed"] and not p.get("dropped_at"):
                    p["dropped_at"] = now.isoformat(timespec="minutes")
                if p["listed"]:
                    p.pop("dropped_at", None)
                if mins <= cfg["tipoff_window_min"]:
                    p["listed_at_tip"] = p["listed"]
                    if all(l["key"] in live_px for l in p["legs"]):
                        p["close_dec_product"] = round(math.prod(live_px[l["key"]]["dec"] for l in p["legs"]), 3)

        if cfg.get("cross", {}).get("enabled"):
            cross_pass(L, legs, cfg, now, us, live, run)

    open_cost = sum(p["stake"] for p in L["parlays"] if p["status"] == "open")
    L["equity"].append({"t": run["t"], "equity": round(L["cash"] + open_cost, 2)})
    L["equity"], L["runs"] = L["equity"][-3000:], (L["runs"] + [run])[-300:]
    L["updated"] = run["t"]
    if us and cfg.get("show_account", True):
        try:
            acc = account_snapshot(us)
            if acc:
                L["account"] = dict(acc, t=run["t"])
                L.setdefault("deposit", 100.0)
                run["notes"].append("account balance refreshed")
        except Exception:
            pass
    print(json.dumps(run, indent=1))
    save()


if __name__ == "__main__":
    main()
