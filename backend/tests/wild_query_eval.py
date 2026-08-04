"""
Wild-query eval — measures how the FULL pipeline handles in-the-wild phrasing.

Unlike audit_routing.py (canonical phrasings, expects direct interception),
this suite is deliberately mangled: team abbreviations, player nicknames,
slang stats, typos, fragments, casual syntax. It answers the question the
routing audit can't: "when a real user phrases it wildly, do they still get
a GROUNDED answer, from which tier, and how fast?"

Usage:
    python3 backend/tests/wild_query_eval.py [--base URL] [--sample N]
        [--filter SUBSTR] [--throttle SECS] [--out results.json]

COSTS REAL MONEY: every query hits /query end-to-end. A full run (~130
queries) costs a few dollars worst-case (misses that reach the planner).
Use --sample 50 for the cheap baseline pass.

Terminus classification (from SSE done-event flags + disclaimer text):
    engine   — interceptor/query engine, direct hit           (grounded, $0)
    mapped   — intent mapper rewrite → engine                 (grounded)
    haiku    — Haiku SQL                                      (grounded)
    planner  — sql_planner with DB rows                       (grounded)
    knowledge— ungrounded general-knowledge answer            (MISS)
    error    — transport/server error

Grades: % grounded among data queries (the headline number), terminus
distribution, latency per tier. Wild phrasings that keep landing in
knowledge/planner are promotion candidates → static aliases or parsers.
"""

import argparse
import json
import sys
import time
import urllib.request

DEFAULT_BASE = "https://api.secondsignalapps.com"
DEVICE_ID = "wild-query-eval"

# (query, expectation) — expectation:
#   "grounded" — our DB has this data; anything ungrounded is a coverage miss
#   "opinion"  — genuinely not a DB lookup; planner/knowledge is CORRECT here
WILD: list[tuple[str, str]] = [
    # --- Team shorthand / nicknames → team stats & records ---
    ("AZ diamondbacks stats", "grounded"),
    ("dbacks record", "grounded"),
    ("how are the yanks doing", "grounded"),
    ("nats team stats", "grounded"),
    ("halos record this year", "grounded"),
    ("bosox w-l", "grounded"),
    ("the bombers record", "grounded"),
    ("mets w l this season", "grounded"),
    ("jays team batting", "grounded"),
    ("phils record 2025", "grounded"),
    # --- Player nicknames / shorthand / lowercase ---
    ("big papi 2004 numbers", "grounded"),
    ("vladdy stats", "grounded"),
    ("vlad jr homers this year", "grounded"),
    ("the kid career stats", "grounded"),
    ("stats for lg jr", "grounded"),
    ("tatis jr steals", "grounded"),
    ("acuna stats rn", "grounded"),
    ("bobby witt numbers", "grounded"),
    ("jrod this season", "grounded"),
    ("shotime pitching stats", "grounded"),
    # --- Slang stat names ---
    ("judge dingers this yr", "grounded"),
    ("who leads mlb in bombs", "grounded"),
    ("ohtani jacks 2024", "grounded"),
    ("cole punchouts this season", "grounded"),
    ("who has the most ribbies in the al", "grounded"),
    ("soto free passes this year", "grounded"),
    ("skenes ks per 9", "grounded"),
    ("whos winning the batting title race", "grounded"),
    ("most taters in 2001", "grounded"),
    ("crochet whiffs this year", "grounded"),
    # --- Typos ---
    ("Arron Judge home runs 2024", "grounded"),
    ("shohei ohtami stats", "grounded"),
    ("mookie bets ops this season", "grounded"),
    ("fransisco lindor rbis", "grounded"),
    ("ronald acunia jr steals 2023", "grounded"),
    ("guerrero jr batting avarage", "grounded"),
    # --- Fragments ---
    ("judge hrs 2024", "grounded"),
    ("soto ops", "grounded"),
    ("cole era", "grounded"),
    ("ohtani 2023", "grounded"),
    ("trout career", "grounded"),
    ("betts vs lefties", "grounded"),
    ("harper june", "grounded"),
    ("skenes whip 2025", "grounded"),
    # --- Casual syntax ---
    ("hows soto been lately", "grounded"),
    ("is judge hot right now", "grounded"),
    ("whos raking rn", "grounded"),
    ("who leads the al in homers", "grounded"),
    ("hows the phillies pitching been", "grounded"),
    ("can you tell me witts avg pls", "grounded"),
    ("i wanna know who has the best era", "grounded"),
    ("gimme trouts numbers from 2019", "grounded"),
    ("yo who hit the most hrs last year", "grounded"),
    ("hey whats elly's stolen base count", "grounded"),
    # --- Leaderboards, wild forms ---
    ("best bats in the al this yr", "grounded"),
    ("top 5 sluggers 2024", "grounded"),
    ("who mashes lefties the best", "grounded"),
    ("best arm in the nl rn", "grounded"),
    ("worst era among starters", "grounded"),
    ("who strikes out the most", "grounded"),
    ("obp kings this season", "grounded"),
    ("stolen base leaders nl", "grounded"),
    # --- Thresholds ---
    ("anyone hit 40 bombs last yr", "grounded"),
    ("who went 30 30 in 2024", "grounded"),
    ("guys with 200 ks this season", "grounded"),
    ("how many players batted over .300 last season", "grounded"),
    ("anybody with 50 steals and 20 homers ever", "grounded"),
    # --- Splits: platoon / home-away / RISP / month ---
    ("judge against lefties", "grounded"),
    ("how does vladdy do at home", "grounded"),
    ("soto with runners in scoring position", "grounded"),
    ("ohtani in july", "grounded"),
    ("harper on the road this year", "grounded"),
    ("freeman vs righties 2024", "grounded"),
    ("devers with risp this season", "grounded"),
    ("whos best against lefty pitching", "grounded"),
    # --- Streaks / current form ---
    ("is judge streaking", "grounded"),
    ("longest hit streak ever jeter", "grounded"),
    ("whos on a heater right now", "grounded"),
    ("acuna hitting streak", "grounded"),
    ("longest on base streak this season", "grounded"),
    # --- Comparisons ---
    ("judge or soto whos better this year", "grounded"),
    ("ohtani vs judge", "grounded"),
    ("compare witt and lindor", "grounded"),
    ("trout vs griffey career", "grounded"),
    # --- Player vs team ---
    ("judge against the sox", "grounded"),
    ("how does devers hit vs the yankees", "grounded"),
    ("ohtani numbers against houston", "grounded"),
    # --- Game logs / recent windows ---
    ("judges last 10", "grounded"),
    ("soto past week", "grounded"),
    ("show me witts last 5 games", "grounded"),
    ("harpers game log", "grounded"),
    # --- Pitching, wild forms ---
    ("coles whip", "grounded"),
    ("best era in the nl", "grounded"),
    ("degrom ks this season", "grounded"),
    ("skenes vs lefties", "grounded"),
    ("wheeler home road splits", "grounded"),
    ("most wins by a pitcher 2024", "grounded"),
    ("sale strikeout rate", "grounded"),
    # --- Career / season lookups, wild ---
    ("what did bonds hit in 2001", "grounded"),
    ("hank aaron total homers", "grounded"),
    ("pujols career slash", "grounded"),
    ("ichiro hits 2004", "grounded"),
    ("rickey henderson steals all time", "grounded"),
    ("teddy ballgame 1941", "grounded"),
    # --- Control group: canonical phrasing (must stay engine-direct) ---
    ("Aaron Judge home runs in 2024", "grounded"),
    ("OPS leaders in 2025", "grounded"),
    ("players with 40+ home runs in 2024", "grounded"),
    ("Aaron Judge vs lefties in 2025", "grounded"),
    ("compare Aaron Judge and Juan Soto", "grounded"),
    ("Yankees record in 2025", "grounded"),
    ("Gerrit Cole ERA in 2025", "grounded"),
    ("Derek Jeter career stats", "grounded"),
    # --- Genuinely non-lookup: planner/knowledge is the RIGHT answer ---
    ("should the mets trade for a closer", "opinion"),
    ("who wins the world series this year", "opinion"),
    ("build me an all star lineup from current players", "opinion"),
    ("why is judge so good", "opinion"),
    ("explain the infield fly rule", "opinion"),
]


def run_query(base: str, question: str, timeout: int = 90):
    """POST /query, consume SSE. Returns (terminus, latency_s, text_snippet)."""
    body = json.dumps({
        "question": question,
        "device_id": DEVICE_ID,
        "history": [],
    }).encode()
    req = urllib.request.Request(
        f"{base}/query", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    t0 = time.time()
    text_parts: list[str] = []
    done_event: dict = {}
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            for raw in r:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data: "):
                    continue
                try:
                    ev = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                if ev.get("type") == "text":
                    text_parts.append(ev.get("text", ""))
                elif ev.get("type") == "done":
                    done_event = ev
                elif ev.get("type") in ("error", "quota_exceeded"):
                    return "error", time.time() - t0, ev.get("message", ev.get("type", ""))
    except Exception as e:
        return "error", time.time() - t0, str(e)[:120]

    latency = time.time() - t0
    full_text = "".join(text_parts)
    snippet = full_text.replace("\n", " ")[:100]

    if done_event.get("mapped"):
        return "mapped", latency, snippet
    if done_event.get("intercepted"):
        return "engine", latency, snippet
    if done_event.get("haiku_sql"):
        return "haiku", latency, snippet
    if done_event.get("insight"):
        if "general knowledge" in full_text:
            return "knowledge", latency, snippet
        return "planner", latency, snippet
    if "general knowledge" in full_text:
        return "knowledge", latency, snippet
    return "unknown", latency, snippet


GROUNDED_TIERS = {"engine", "mapped", "haiku", "planner"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--sample", type=int, default=0, help="run only the first N")
    ap.add_argument("--filter", default="", help="substring filter on query text")
    ap.add_argument("--throttle", type=float, default=1.0)
    ap.add_argument("--out", default="", help="write per-query results JSON here")
    args = ap.parse_args()

    cases = [c for c in WILD if args.filter.lower() in c[0].lower()]
    if args.sample:
        cases = cases[: args.sample]

    results = []
    consecutive_errors = 0
    print(f"Running {len(cases)} wild queries against {args.base}\n")
    for i, (q, expectation) in enumerate(cases, 1):
        terminus, latency, snippet = run_query(args.base, q)
        grounded = terminus in GROUNDED_TIERS
        ok = grounded if expectation == "grounded" else True
        results.append({
            "query": q, "expectation": expectation, "terminus": terminus,
            "grounded": grounded, "ok": ok,
            "latency_s": round(latency, 2), "snippet": snippet,
        })
        flag = "  " if ok else "✗ "
        print(f"{flag}[{i:3}/{len(cases)}] {terminus:10} {latency:6.1f}s  {q}")
        consecutive_errors = consecutive_errors + 1 if terminus == "error" else 0
        if consecutive_errors >= 5:
            print("\n5 consecutive errors — backend struggling, stopping early.")
            break
        time.sleep(args.throttle)

    data = [r for r in results if r["expectation"] == "grounded"]
    grounded_n = sum(1 for r in data if r["grounded"])
    print("\n================ SUMMARY ================")
    if data:
        print(f"GROUNDED: {grounded_n}/{len(data)} "
              f"({100.0 * grounded_n / len(data):.1f}%) of data queries")
    tiers: dict[str, list[float]] = {}
    for r in results:
        tiers.setdefault(r["terminus"], []).append(r["latency_s"])
    print("\nTerminus distribution (all queries):")
    for tier, lats in sorted(tiers.items(), key=lambda kv: -len(kv[1])):
        avg = sum(lats) / len(lats)
        print(f"  {tier:10} n={len(lats):3}  avg {avg:5.1f}s  max {max(lats):5.1f}s")
    misses = [r for r in data if not r["grounded"]]
    if misses:
        print("\nUngrounded data queries (promotion candidates):")
        for r in misses:
            print(f"  [{r['terminus']:9}] {r['query']}")
    if args.out:
        with open(args.out, "w") as f:
            json.dump(results, f, indent=2)
        print(f"\nPer-query results → {args.out}")
    return 0 if not misses else 1


if __name__ == "__main__":
    sys.exit(main())
