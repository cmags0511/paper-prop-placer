#!/usr/bin/env python3
"""Polymarket paper trader: NBA same-game 3-leg parlays. PAPER ONLY - no wallet, no keys, no orders.

Every run (GitHub Actions, every 30 min):
  1. settle open paper parlays from Polymarket's resolved leg markets
  2. read the prop tracker's slate (checked out next to this repo) and give every prop a calibrated chance
     with the tracker's own grading model (book no-vig price + what graded picks have added on top)
  3. match props to Polymarket's single-stat NBA player markets, price each leg from the live CLOB order book
  4. per game, find the best 3-leg same-game parlay (distinct players, correlation from the tracker's game logs)
  5. bet every parlay the moment it is listed (the best one per game, as the prop tracker lists it), 1 unit each.
     If the listing later changes, the old parlay is "dropped" but stays bet and stays on the record, like the
     prop tracker's own record. What is listed at tip-off is flagged, and bets stop at tip-off.

Paper price: Polymarket quotes Combos by RFQ (needs an account), so the price is the product of the legs'
live asks, less the same correlation haircut the prop tracker assumes the book charges.

Usage: python trader.py [--ledger ledger.json] [--tracker ../prop-tracker] [--now ISO] [--dry]
"""
import argparse, itertools, json, math, os, re, sys, time, unicodedata, datetime as dt
import urllib.request
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
HERE = os.path.dirname(os.path.abspath(__file__))
H = {"User-Agent": "Mozilla/5.0 (polymarket-paper-trader)", "Accept": "application/json"}
GAMMA, CLOB = "https://gamma-api.polymarket.com", "https://clob.polymarket.com"


def get(url, tries=3):
    err = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=H), timeout=40) as r:
                return json.loads(r.read())
        except Exception as e:
            err = e
            time.sleep(1.5 * (i + 1))
    raise err


def norm(n):
    n = unicodedata.normalize("NFKD", n).encode("ascii", "ignore").decode().lower()
    w = [x for x in re.sub(r"[^a-z ]", " ", n).split() if x not in ("jr", "sr", "ii", "iii", "iv", "v")]
    return " ".join(w)


def iso(s):
    s = s.strip().replace(" ", "T")
    if re.search(r"[+-]\d\d$", s):
        s += ":00"
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


# ---------------------------------------------------------------- prop tracker (imported from the checkout)
def load_tracker(path):
    path = os.path.abspath(path)
    sys.path.insert(0, path)
    argv, sys.argv = sys.argv, ["x", os.devnull]
    cwd = os.getcwd()
    os.chdir(path)
    try:
        import parlays as PL
        import picks as PK
        GM = PK.grade_model()           # reads calib.json from cwd
    finally:
        os.chdir(cwd)
        sys.argv = argv
    data = json.load(open(os.path.join(path, "props_data.json")))["NBA"]
    cols = data["cols"]
    rows = {p["id"]: [dict(zip(cols, r)) for r in p["g"]] for p in data["players"]}
    slate = json.load(open(os.path.join(path, "slate.json")))["props"]
    out = set()
    try:
        notes = json.load(open(os.path.join(path, "notes.json")))
        out = {"|".join(k.split("|")[:2]) for k, v in notes.get("notes", {}).items() if v.get("flag") == "out"}
    except Exception:
        pass
    return PL, PK, GM, rows, slate, out


def leg_pool(PK, PL, GM, slate, out, cfg, now):
    """every NBA prop with a real book price, both sides, with the tracker's calibrated chance"""
    pool = []
    for s in slate:
        if s["lg"] != "NBA" or s["market"] not in cfg["markets"].values():
            continue
        if s.get("pe") or s.get("po") is None or s.get("pu") is None or s.get("p_over") is None:
            continue
        if f"NBA|{s['id']}" in out or s.get("status") in PK.OUT_STATUS:
            continue
        t = iso(s["t"])
        if t <= now or t > now + dt.timedelta(hours=cfg["horizon_hours"]):
            continue
        bo = PK._imp(s["po"]) / (PK._imp(s["po"]) + PK._imp(s["pu"]))
        for side in ("over", "under"):
            app, bp = (s["p_over"], bo) if side == "over" else (1 - s["p_over"], 1 - bo)
            adj = sum(b * x for b, x in zip(GM[:PK.GRADE_K], PK.grade_feats(side, app, bp, True)))
            p = 1 / (1 + math.exp(-(PK._lg(bp) + adj)))
            p = .5 + (p - .5) * PL.SHRINK
            pool.append({"key": f"NBA|{s['id']}|{s['market']}|{side}", "lg": "NBA", "id": s["id"], "name": s["name"],
                         "team": s["team"], "opp": s["opp"], "t": s["t"], "market": s["market"], "label": s["label"],
                         "side": side, "line": s["line"], "p": p, "book_p": bp})
    return pool


# ---------------------------------------------------------------- polymarket
def poly_markets(cfg, now):
    """{(name, stat, line): market} for upcoming NBA games"""
    sp = [x for x in get(f"{GAMMA}/sports") if x["sport"] == "nba"][0]["series"]
    out, off = {}, 0
    horizon = now + dt.timedelta(hours=cfg["horizon_hours"])
    while off < 400:
        ev = get(f"{GAMMA}/events?series_id={sp}&active=true&closed=false&limit=40&offset={off}"
                 f"&end_date_min={now.strftime('%Y-%m-%dT%H:%M:%SZ')}&order=endDate&ascending=true")
        if not ev:
            break
        stop = False
        for e in ev:
            st = iso(e.get("startTime") or e["endDate"])
            if st > horizon:
                stop = True
                continue
            for m in e["markets"]:
                stat = cfg["markets"].get(m.get("sportsMarketType"))
                if not stat or m.get("closed") or not m.get("acceptingOrders") or not m.get("clobTokenIds"):
                    continue
                line = m.get("line")
                if line is None:
                    mm = re.search(r"O/U ([\d.]+)$", m["question"])
                    line = float(mm.group(1)) if mm else None
                if line is None:
                    continue
                name = m["question"].split(": ")[0]
                out[(norm(name), stat, float(line))] = {
                    "id": m["id"], "q": m["question"], "tokens": json.loads(m["clobTokenIds"]),
                    "outcomes": json.loads(m["outcomes"]), "start": m.get("gameStartTime") or e.get("startTime"),
                    "bid": m.get("bestBid"), "ask": m.get("bestAsk")}
        off += 40
        if stop or len(ev) < 40:
            break
    return out


def book(token):
    b = get(f"{CLOB}/book?token_id={token}")
    bids = [(float(x["price"]), float(x["size"])) for x in b.get("bids", [])]
    asks = [(float(x["price"]), float(x["size"])) for x in b.get("asks", [])]
    bb = max(bids)[0] if bids else None
    ba = min(asks)[0] if asks else None
    size = sum(s for p, s in asks if p == ba) if asks else 0
    return bb, ba, size


def price_legs(pool, pm, cfg):
    """attach the live ask for the side's token; keep legs that are close enough to the chance and tight enough"""
    legs, st = [], {"matched": 0, "priced": 0}
    yes_over = cfg["yes_is_over"]
    for c in pool:
        m = pm.get((norm(c["name"]), c["market"], float(c["line"])))
        if not m or abs((iso(m["start"]) - iso(c["t"])).total_seconds()) > 6 * 3600:
            continue
        st["matched"] += 1
        idx = (0 if c["side"] == "over" else 1) if yes_over else (1 if c["side"] == "over" else 0)
        # cheap prefilter from the event feed (bid/ask are for token 0) before spending a book call
        if m["bid"] is not None and m["ask"] is not None:
            est = m["ask"] if idx == 0 else 1 - m["bid"]
            if est > c["p"] - cfg["leg_min_edge"] + .03 or est < c["p"] - cfg["leg_max_edge"] - .03:
                continue
        bb, ba, size = book(m["tokens"][idx])
        time.sleep(.05)
        if bb is None or ba is None or ba - bb > cfg["max_spread"] or size < cfg["min_ask_size"]:
            continue
        if not cfg["leg_min_edge"] <= c["p"] - ba <= cfg["leg_max_edge"]:   # a huge gap usually means news the model lacks
            continue
        st["priced"] += 1
        legs.append(dict(c, market_id=m["id"], token_id=m["tokens"][idx], ask=ba, bid=bb, size=size, dec=1 / ba,
                         pm_q=m["q"]))
    return legs, st


def pid(parts, combo):
    return "|".join(parts) + "|" + "+".join(sorted(f"{c['key']}@{c['line']}" for c in combo))


def best_combo(legs, rows, PL, cfg):
    best, cache = None, {}
    for combo in itertools.combinations(legs, cfg["legs"]):
        if len({c["id"] for c in combo}) < cfg["legs"]:      # distinct players (also keeps NBA same-player-under combos out)
            continue
        p0 = math.prod(c["p"] for c in combo)
        dec = math.prod(c["dec"] for c in combo)
        factor, notes = 1.0, []
        for a, b in itertools.combinations(combo, 2):
            k = (a["key"], b["key"])
            if k not in cache:
                cache[k] = PL.pair_rho(a, b, rows)
            rho, n, both = cache[k]
            factor *= PL.joint2(a["p"], b["p"], rho) / (a["p"] * b["p"])
            notes.append(PL.why(a, b, rho, n, both))
        if not (.97 <= factor <= 1.8):
            continue
        # the best of many noisy correlation estimates is biased high, so only part of the boost is trusted
        pj = p0 * (1 + cfg["factor_trust"] * (factor - 1))
        dec_paper = dec / (1 + cfg["book_gives"] * (factor - 1)) if factor > 1 else dec
        dec_paper *= 1 - cfg.get("fee_friction", 0)   # assumed fees/slippage until real combo quotes are known
        ev = pj * dec_paper - 1
        if ev < cfg["min_ev"] or pj < cfg["min_joint_p"] or pj > .9:
            continue
        if best is None or ev > best["ev"]:
            best = {"combo": combo, "p_indep": p0, "factor": factor, "p": pj, "dec_product": dec,
                    "dec": dec_paper, "ev": ev, "pairs": notes}
    return best


# ---------------------------------------------------------------- ledger
def new_ledger(cfg):
    return {"start_bankroll": cfg["start_bankroll"], "unit": cfg["unit"], "cash": cfg["start_bankroll"],
            "parlays": [], "equity": [], "runs": []}


def settle(L, now, dry):
    n = 0
    for p in L["parlays"]:
        if p["status"] != "open" or now < iso(p["game_t"]):
            continue
        lost, pending, voids = False, False, 1.0
        for l in p["legs"]:
            if l.get("result"):
                pass
            else:
                try:
                    m = get(f"{GAMMA}/markets/{l['market_id']}")
                except Exception:
                    pending = True
                    continue
                if not m.get("closed"):
                    pending = True
                    continue
                v = float(json.loads(m["outcomePrices"])[l["idx"]])
                l["result"] = "won" if v >= .99 else "lost" if v <= .01 else "void" if abs(v - .5) < .05 else None
                if l["result"] is None:
                    pending = True
                    continue
            lost |= l["result"] == "lost"
            if l["result"] == "void":
                voids *= l["ask"] and 1 / l["ask"]
        if lost:
            p.update(status="lost", payout=0.0, pnl=round(-p["stake"], 2))
        elif pending:
            continue
        else:
            dec = p["dec"] / voids
            pay = round(p["stake"] * dec, 2)
            p.update(status="won" if dec > 1.0001 else "void", payout=pay, pnl=round(pay - p["stake"], 2))
        p["settled"] = now.isoformat(timespec="minutes")
        L["cash"] = round(L["cash"] + p["payout"], 2)
        n += 1
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", default=os.path.join(HERE, "ledger.json"))
    ap.add_argument("--tracker", default=os.environ.get("PROP_TRACKER_DIR") or os.path.join(HERE, "prop-tracker"))
    ap.add_argument("--now")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    cfg = json.load(open(os.path.join(HERE, "config.json")))
    now = iso(a.now) if a.now else dt.datetime.now(dt.timezone.utc)
    L = json.load(open(a.ledger)) if os.path.exists(a.ledger) else new_ledger(cfg)
    run = {"t": now.isoformat(timespec="minutes"), "settled": 0, "placed": 0, "notes": []}

    run["settled"] = settle(L, now, a.dry)
    if cfg["paused"]:
        run["notes"].append("paused")
    else:
        PL, PK, GM, rows, slate, out = load_tracker(a.tracker)
        pool = leg_pool(PK, PL, GM, slate, out, cfg, now)
        pm = poly_markets(cfg, now)
        legs, st = price_legs(pool, pm, cfg)
        run["notes"].append(f"{len(pool)} tracker legs, {st['matched']} matched on Polymarket, {st['priced']} priced")
        games = {}
        for c in legs:
            games.setdefault((c["t"], *sorted((c["team"], c["opp"]))), []).append(c)
        today = now.astimezone(ET).date()
        spent_today = sum(p["stake"] for p in L["parlays"] if iso(p["placed"]).astimezone(ET).date() == today)
        by_game = {}
        for p in L["parlays"]:
            by_game.setdefault(p["game"], []).append(p)
        for gk in sorted({*("|".join(k) for k in games), *(k for k in by_game if iso(k.split("|")[0]) > now)}):
            parts = gk.split("|")
            tip = iso(parts[0])
            mins = (tip - now).total_seconds() / 60
            if mins <= 0:
                continue                                    # after tip-off nothing new is bet and the listing is frozen
            gl = games.get(tuple(parts), [])
            best = best_combo(sorted(gl, key=lambda c: c["ask"] - c["p"])[:cfg["max_legs_per_game"]], rows, PL, cfg) if gl else None
            best_id = pid(parts, best["combo"]) if best else None
            label = f"{parts[1]}/{parts[2]}"
            mine = by_game.get(gk, [])
            # every parlay the app lists is bet the moment it is listed; one that later drops off the list stays bet and counted
            if best and best_id not in {p["id"] for p in mine}:
                stake = cfg["unit"]
                open_risk = sum(p["stake"] for p in L["parlays"] if p["status"] == "open")
                if len(mine) >= cfg["max_bets_per_game"]:
                    run["notes"].append(f"{label}: listing changed but {cfg['max_bets_per_game']} bets already on this game")
                elif stake > L["cash"] or spent_today + stake > cfg["daily_risk_cap"] or \
                        open_risk + stake > cfg["max_open_risk_pct"] * cfg["start_bankroll"]:
                    run["notes"].append(f"{label}: risk cap hit")
                else:
                    par = {"id": best_id, "game": gk, "game_t": parts[0], "teams": [parts[1], parts[2]],
                           "placed": now.isoformat(timespec="minutes"), "mins_to_tip": round(mins),
                           "stake": stake, "p_indep": round(best["p_indep"], 3), "corr_factor": round(best["factor"], 3),
                           "p": round(best["p"], 3), "dec_product": round(best["dec_product"], 3), "dec": round(best["dec"], 3),
                           "ev": round(best["ev"], 3), "pairs": best["pairs"], "status": "open", "payout": None, "pnl": None,
                           "listed": True, "listed_at_tip": None,
                           "legs": [{"key": c["key"], "name": c["name"], "team": c["team"], "market": c["market"],
                                     "label": c["label"], "side": c["side"], "line": c["line"], "p": round(c["p"], 3),
                                     "ask": c["ask"], "bid": c["bid"], "market_id": c["market_id"],
                                     "idx": 0 if (c["side"] == "over") == cfg["yes_is_over"] else 1, "q": c["pm_q"]}
                                    for c in best["combo"]]}
                    if not a.dry:
                        L["parlays"].append(par)
                        mine.append(par)
                        L["cash"] = round(L["cash"] - stake, 2)
                        spent_today += stake
                    run["placed"] += 1
                    run["notes"].append(f"{label}: listed and bet, p={par['p']}, paper payout x{par['dec']}, ev={par['ev']}")
            elif not best:
                run["notes"].append(f"{label}: no qualifying parlay listed")
            # listing status of every bet on this game (they stay on the record either way)
            live = {l["key"]: l for l in gl}
            for p in mine:
                p["listed"] = p["id"] == best_id
                if not p["listed"] and not p.get("dropped_at"):
                    p["dropped_at"] = now.isoformat(timespec="minutes")
                if p["listed"]:
                    p.pop("dropped_at", None)
                if mins <= cfg["tipoff_window_min"]:
                    p["listed_at_tip"] = p["listed"]
                    if all(l["key"] in live for l in p["legs"]):
                        p["close_dec_product"] = round(math.prod(live[l["key"]]["dec"] for l in p["legs"]), 3)

    open_cost = sum(p["stake"] for p in L["parlays"] if p["status"] == "open")
    L["equity"].append({"t": run["t"], "equity": round(L["cash"] + open_cost, 2)})
    L["equity"], L["runs"] = L["equity"][-3000:], (L["runs"] + [run])[-300:]
    L["updated"] = run["t"]
    print(json.dumps(run, indent=1))
    if not a.dry:
        json.dump(L, open(a.ledger, "w"), separators=(",", ":"))


if __name__ == "__main__":
    main()
