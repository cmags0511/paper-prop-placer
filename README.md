# Polymarket Paper Trader

Fake-money (paper) trading of **NBA same-game 3-leg parlays** on Polymarket, using the
[prop tracker](https://github.com/cmags0511/prop-tracker) as its brain. No wallet, no API keys, no orders are ever placed.

- Bankroll **$400**, flat **$2.50** (1 unit) per parlay. Rules live in `config.json`.
- Every 30 min (tip-off hours) `trader.py` runs on GitHub Actions and commits `ledger.json`; `index.html` is the dashboard.

## How a parlay is chosen
1. Each prop on the tracker's slate with a real book price gets a calibrated chance (the tracker's own grading model: the book's
   no-vig price plus what graded picks have added, e.g. unders have beaten the book), pulled 5% toward 50%.
2. Props are matched to Polymarket's single-stat NBA markets (points, rebounds, assists, threes) at the exact same line.
   Each leg is priced from the live order book (best ask, spread <= 8c, >= 10 shares). Legs priced far from the model (> 8 pts) are
   dropped: that usually means news the model lacks.
3. Per game, the best **3-leg, 3-different-players** parlay is picked (unders allowed). Correlation comes from the tracker's game logs
   (`pair_rho`), and only 60% of the boost is trusted (the best of many noisy estimates is biased high).
4. **Every listed parlay is bet.** The best parlay per game is bet (1 unit) the moment it is listed. If the listing changes later, the
   old parlay is marked *dropped* but **stays bet and stays on the record** (same as the prop tracker's record: if it's counted, it was bet).
   Max 4 bets per game; bets stop at tip-off. At tip-off each bet is flagged listed or dropped, and records a closing price, so the
   dashboard shows how "listed at tip-off" and "dropped before tip-off" bets perform.

## Paper price (important)
Polymarket sells parlays ("Combos") by request-for-quote from market makers, which needs an account. So the paper payout is the
**product of the legs' asks**, less the same half-correlation haircut the tracker assumes. The real quote could be worse.
Fees and slippage on a real combo are not modelled. Settlement uses Polymarket's resolved leg markets; a voided leg drops out.

## Limits
Max 25% of bankroll open, $50 of new risk per ET day, `"paused": true` in `config.json` stops new bets.

## Setup
Push this folder to a new GitHub repo, enable Pages (main branch, root) if you want the dashboard, and let Actions run.
Public repo = unlimited Actions minutes; private repo ~2,000 free min/month, which this schedule can use up.


## Polymarket US (current)
`trader_us.py` runs the same strategy against the Polymarket US exchange ("at least N" player markets).
Old global-price paper results are archived in `ledger_global_paper.json`.

- **paper** (no keys): ledger only.
- **shadow** (keys set, default): also creates the real combo and previews a real order; no order is placed.
- **live**: only when repo variable `LIVE_TRADING` = `1`. IOC limit buys at a max price that keeps `live.min_ev`,
  capped by `live.daily_cap` / `live.total_cap` in config.json. Set `"paused": true` to stop everything.
Real ordering is untested until the first shadow previews are checked.

## 2-leg cross-game parlays
Besides the 3-leg same-game parlays, each run also lists up to 3 two-leg parlays whose legs are in DIFFERENT games
(`cross` in config.json). Legs are treated as independent (chance = product of the two). They are bought the same way
(RFQ, cheapest firm quote under the max price) and settle once both games are final. The Live tab does not track
cross-game parlays leg by leg yet.
