#!/usr/bin/env python3
"""Game-day runner. Looks up NBA tip-off times in the prop tracker's slate and runs the trader at set offsets from each
tip-off (before: buy; after: settle). GitHub's cron is unreliable, so this one job stays alive and does the waiting itself,
then hands off to a fresh run before GitHub's job time limit.

  python gameday.py            run for real (sleeps, runs the trader, commits the ledger)
  python gameday.py --plan     print the plan and exit
"""
import argparse, datetime as dt, json, os, subprocess, sys, time
import trader as T

HERE = os.path.dirname(os.path.abspath(__file__))
BEFORE_MIN = (90, 45, 20, 8)       # minutes before tip-off: look for and buy parlays
AFTER_MIN = (170,)                 # minutes after tip-off: settle the finished game
MERGE_MIN = 6                      # runs closer than this are merged
MAX_HOURS = 5.5                    # GitHub jobs stop at 6h; hand off before that


def tipoffs(tracker, now):
    slate = json.load(open(os.path.join(tracker, "slate.json")))
    ts = {T.iso(p["t"]) for p in slate.get("props", []) if p.get("lg", "NBA") == "NBA" and p.get("t")}
    return sorted(t for t in ts if t + dt.timedelta(minutes=max(AFTER_MIN) + 5) > now)


def plan(tracker, now):
    times = []
    for t in tipoffs(tracker, now):
        for m in BEFORE_MIN:
            times.append(t - dt.timedelta(minutes=m))
        for m in AFTER_MIN:
            times.append(t + dt.timedelta(minutes=m))
    times = sorted(x for x in times if x > now + dt.timedelta(seconds=60))
    out = []
    for x in times:
        if not out or (x - out[-1]).total_seconds() > MERGE_MIN * 60:
            out.append(x)
    return out


def sh(*a, check=False):
    return subprocess.run(a, cwd=HERE, check=check, capture_output=True, text=True)


def refresh_tracker(tracker):
    sh("git", "-C", tracker, "fetch", "--depth", "1", "origin")
    sh("git", "-C", tracker, "reset", "--hard", "FETCH_HEAD")


def run_trader(tracker):
    refresh_tracker(tracker)
    r = subprocess.run([sys.executable, "trader_us.py", "--tracker", tracker], cwd=HERE, capture_output=True, text=True)
    print(r.stdout[-3000:])
    if r.returncode:
        print("trader failed:", r.stderr[-1500:])
    sh("git", "config", "user.name", "paper-trader-bot")
    sh("git", "config", "user.email", "bot@users.noreply.github.com")
    sh("git", "add", "ledger.json")
    if sh("git", "diff", "--cached", "--quiet").returncode:
        sh("git", "commit", "-m", "Update ledger")
        for _ in range(3):
            sh("git", "pull", "--rebase", "origin", "main")
            if sh("git", "push").returncode == 0:
                break


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tracker", default=os.environ.get("PROP_TRACKER_DIR") or os.path.join(HERE, "prop-tracker"))
    ap.add_argument("--plan", action="store_true")
    a = ap.parse_args()
    start = dt.datetime.now(dt.timezone.utc)
    deadline = start + dt.timedelta(hours=MAX_HOURS)
    todo = plan(a.tracker, start)
    mine = [x for x in todo if x <= deadline]
    later = [x for x in todo if x > deadline]
    print("now", start.isoformat(timespec="minutes"), "| runs this job:", [x.strftime("%H:%M") for x in mine],
          "| after hand-off:", len(later))
    if a.plan:
        return
    if not todo:
        print("no NBA tip-offs ahead; nothing to do")
        return
    run_trader(a.tracker)                              # immediate run, so a late start still catches up
    for x in mine:
        wait = (x - dt.datetime.now(dt.timezone.utc)).total_seconds()
        if wait > 0:
            time.sleep(wait)
        run_trader(a.tracker)
    if later:
        print("handing off to a new run")
        sh("gh", "workflow", "run", "gameday.yml", "--ref", "main")


if __name__ == "__main__":
    main()
