# -*- coding: utf-8 -*-
"""Q3-break Under: P_MAX x lines-per-game, measured PER UNIT OF STAKE.

The trap this script avoids: summing per-trade returns across N lines makes
N=3 look 3x better than N=1, because it is really 3 units of stake earning
3x the dollar amount.  Adjacent lines are the SAME bet (all lose together on
a high-scoring Q4), so extra lines add capital and correlation, not edge.

So every number here is per unit of stake:
  per-trade EV  -- mean return of a single line
  per-game EV   -- mean of (each game's AVERAGE line return), so a game with
                   3 lines does not weigh 3x more than a game with 1
  lower bound   -- 2.5th percentile of the per-game bootstrap, same units
"""
import sys, os, json, bisect, statistics, collections, random
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
M = json.load(open(os.path.join(ROOT, "data", "totals_model.json")))
BIN = M["bin"]; CELLS = M["cells"]; CUTS = {int(k): v for k, v in M["cuts"].items()}


def pmodel(el, S, needed):
    bi = min(23, max(0, int(el // BIN)))
    lo, hi = CUTS[bi]
    k = 0 if S < lo else (1 if S < hi else 2)
    v = CELLS.get(f"{bi},{k}")
    if not v:
        return None
    return 1.0 - bisect.bisect_right(v, needed) / len(v)


def state_at(tl, t):
    lo, hi = 0, len(tl)
    while lo < hi:
        mid = (lo + hi) // 2
        if tl[mid][2] <= t:
            lo = mid + 1
        else:
            hi = mid
    return tl[lo - 1] if lo > 0 else None


def candidates(g, target=2160, tol=120):
    tl = g.get("total_tl") or []
    if not tl:
        return []
    out = []
    for o in g.get("lines") or []:
        for t, p in o.get("over") or []:
            st = state_at(tl, t)
            if not st:
                continue
            el, S, _w = st
            if abs(el - target) > tol:
                continue
            pm = pmodel(el, S, o["line"] - S)
            if pm is None:
                continue
            out.append(dict(line=o["line"], p=p, pm=pm, el=el, S=S))
            break
    return out


def run(games, pmax, gap, n_lines):
    picks = []
    game_means = []
    two_loss = 0
    for g in games:
        ok = [c for c in candidates(g)
              if c["pm"] <= pmax and c["p"] >= c["pm"] + gap]
        ok.sort(key=lambda c: c["pm"])
        sel = ok[:n_lines]
        if not sel:
            continue
        rets = []
        for c in sel:
            price = 1 - c["p"]
            won = g["final_total"] <= c["line"]
            r = (1.0 if won else 0.0) / price - 1
            rets.append(r)
            picks.append(dict(gid=g["gid"], day=g.get("day"), line=c["line"],
                              pm=c["pm"], price=price, won=won, ret=r,
                              el=c["el"]))
        game_means.append(sum(rets) / len(rets))
        if sum(1 for r in rets if r < -0.5) >= 2:
            two_loss += 1
    return picks, game_means, (two_loss / len(game_means) if game_means else 0)


def boot(vals, n=4000, seed=7):
    random.seed(seed)
    return sorted(sum(random.choice(vals) for _ in vals) / len(vals)
                  for _ in range(n))


def report(games, pmax, gap, n_lines):
    picks, gm, two = run(games, pmax, gap, n_lines)
    if len(picks) < 10:
        print(f"  P<={pmax:.2f} N={n_lines}: n={len(picks)} (too few)")
        return None
    bs = boot(gm)
    row = dict(pmax=pmax, n=n_lines, trades=len(picks),
               games=len(gm),
               win=sum(1 for p in picks if p["won"]) / len(picks),
               price=statistics.mean(p["price"] for p in picks),
               ev=statistics.mean(p["ret"] for p in picks),
               game_ev=statistics.mean(gm),
               lo=bs[99], hi=bs[3899], two=two)
    print(f"  P<={pmax:.2f} N={n_lines}: trades={row['trades']:3d} "
          f"({100*row['games']/len(games):4.1f}% of games) "
          f"win={100*row['win']:5.1f}% price={100*row['price']:5.1f}% "
          f"EV/trade={100*row['ev']:+5.1f}% EV/game={100*row['game_ev']:+5.1f}% "
          f"boot[{100*row['lo']:+.1f},{100*row['hi']:+.1f}]% "
          f"2+loss={100*row['two']:4.1f}%")
    return row


def main():
    path = os.path.join(ROOT, "data", "totals_research", "games.json")
    games = [g for g in json.load(open(path))
             if g.get("lines") and g.get("total_tl")]
    print(f"games={len(games)} days={len({g.get('day') for g in games})}")
    rows = []
    for pmax in (0.10, 0.20, 0.25, 0.30):
        print(f"== P_MAX {pmax}")
        for n in (1, 2, 3, 5):
            r = report(games, pmax, 0.03, n)
            if r:
                rows.append(r)
    print("\n=== choose N for P_MAX=0.25 on the worst case ===")
    ok = [r for r in rows if r["pmax"] == 0.25 and r["lo"] > 0 and r["two"] <= 0.10]
    if not ok:
        print("  none with a positive lower bound")
        return
    best = max(ok, key=lambda r: (round(r["lo"], 3), r["ev"]))
    for r in ok:
        mark = " <== choose" if r is best else ""
        print(f"  N={r['n']}: EV/trade {100*r['ev']:+.1f}% "
              f"EV/game {100*r['game_ev']:+.1f}% "
              f"boot_lo {100*r['lo']:+.1f}% 2+loss {100*r['two']:.1f}%{mark}")
    print(f"\nRECOMMEND P_MAX=0.25 LINES_PER_GAME={best['n']}")


if __name__ == "__main__":
    main()
