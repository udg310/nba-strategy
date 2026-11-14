# -*- coding: utf-8 -*-
"""Replay the 14 historical live games (11-12/13) + today's 9 games (11-14).

The 14 come from data/live_probe + live_full_prices.json; the 9 come from
data/live9_20261114.json.  Uses the same rules as live_runner.py:
A unchanged, C 30 game-clock minutes, D margin_stop at m_fav < 0.
"""
import json, os, statistics, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nba_data as nd
import strategy as S


def show(label, tr):
    if not tr:
        return
    acc = S.portfolio(tr)
    print(f"{label:16s} n={len(tr):3d} 每笔{100*statistics.mean([t['ret'] for t in tr]):+6.1f}% "
          f"终值${acc['end']:7.2f} 回撤${acc['mdd']:5.1f} 敞口${acc['peak_exposure']:.0f}")
    for ch in ("A", "C", "D"):
        x = [t for t in tr if t["ch"] == ch]
        if x:
            print(f"   {ch}: n={len(x):3d} 每笔{100*statistics.mean([t['ret'] for t in x]):+6.1f}% "
                  f"全损{100*sum(1 for t in x if t['ret'] <= -0.80)/len(x):5.1f}% "
                  f"{dict(__import__('collections').Counter(t['reason'] for t in x))}")


def main():
    live14 = S.load_live(["2026-11-12", "2026-11-13"])
    live9 = json.load(open(os.path.join(ROOT, "data/live9_20261114.json")))
    games = live14 + live9
    tr, drops = S.run_live(games)
    g9 = {g["gid"] for g in live9}
    print(f"games={len(games)} trades={len(tr)} drops={dict(drops)}")
    show("14 historic", [t for t in tr if t["gid"] not in g9])
    show("9 today", [t for t in tr if t["gid"] in g9])
    show("23 total", tr)
    print("\nD trades:")
    for t in tr:
        if t["ch"] == "D":
            print(f"  {t['gid']} 入{t['entry']:.3f} 出{t['exit']:.3f} "
                  f"({t['reason']}) {100*t['ret']:+7.1f}%")


if __name__ == "__main__":
    main()
