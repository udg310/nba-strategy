# -*- coding: utf-8 -*-
"""NBA band strategy — final framework (2026-11-13).

THREE CHANNELS, ONE IMPLEMENTATION. Nothing else trades.

Every entry is  STATE -> SIGNAL -> ENTRY.  Never a blind buy.
`m_fav` = pregame-favourite score minus pregame-dog score, always.
home/away only rotates the sign of the raw margin; it enters no rule.

  A  弱队领先            weight 1.0   PROCESS exit
     pregame favourite >= 0.60, m_fav <= -5
     signal: m_fav falls >= 3 within 120s of GAME clock
     buy the DOG, TP +20c, no stop, force at Q4 6:00

  C  比分接近时弱队起势    weight 1.0   PROCESS exit
     -5 <= m_fav < 3  (no pregame filter)
     signal: same as A
     buy the DOG, TP +20c, no price stop, 30-minute GAME-CLOCK time
     exit, force at Q4 6:00

  D  末节强队锁胜          weight 2.0   SETTLEMENT / MARGIN STOP
     pregame favourite >= 0.60, Q4, 5 <= m_fav < 10
     take the FIRST such moment in the game; if the entry price is not
     inside 0.70-0.80 there, the game is skipped (do not wait for a
     better price: waiting selects moments the market has turned against)
     buy the FAVOURITE; hold to the final buzzer and settle 1.0 / 0.0.
     If the lead turns negative after entry (m_fav < 0), the thesis is
     broken: exit at the market (`margin_stop`) instead of settling.

HARD INVARIANTS (asserted at run time, not just documented)
  1. m_fav is always favourite minus dog.
  2. A and C buy the dog token; D buys the favourite token.
  3. A and C can only exit via 'target', 'time' (C only) or 'force'.
     They have no settlement path at all — `simulate_process` cannot
     return 0/1.
  4. D can only exit via 'settle' or 'margin_stop'. It has no TP, no
     price stop and no force.
  5. A game skipped by D's price filter is NOT retried later in the game.

Usage:
  python3 -I scripts/strategy.py                 # 971-game backtest
  python3 -I scripts/strategy.py --live          # + the two live days
"""
import sys, os, json, bisect, statistics, collections, random

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nba_data as nd

# ------------------------------------------------------------------ rules
SIGNAL_WIN = 120        # game-clock seconds
SIGNAL_K = 3.0          # points
ENTRY_DELAY = 15        # seconds after the signal
MAX_WAIT = 180          # give up if no print this long after the delay
TP = 0.20               # process channels: take profit, cents
FORCE_EL = 2760         # process channels: Q4 6:00 remaining
EXIT_COST = 0.01        # paid when selling at market
# Real execution cost, measured on-chain 2026-11-13 with four live round trips
# (0.23 / 0.29 / 0.40 / 0.81): the exchange charges about
#     0.10 * p * (1 - p)   per share for a round trip,
# on top of the 1-tick spread. Per side that is 0.05*p*(1-p) + half a tick.
HALF_SPREAD = 0.005
FEE_RATE = 0.10


def entry_cost(p):
    return HALF_SPREAD + 0.5 * FEE_RATE * p * (1 - p)


def exit_cost(p):
    return HALF_SPREAD + 0.5 * FEE_RATE * p * (1 - p)


ENTRY_COST = 0.01       # kept for the label; real cost is entry_cost(p)
PRICE_FLOOR = 0.10      # no entry below this, any channel

CHANNELS = {
    "A": dict(label="弱队领先", weight=1.0, side="dog", style="process",
              pre_min=0.60, state=(-1e9, -5.0), sign=-1, tp=TP,
              price_max=None),
    # Upper bound is 0, not +3: buying the dog while the FAVOURITE still leads by
    # 1-3 contradicts the thesis (the dog is not running, it is behind). That
    # segment averaged -15% and carried the highest full-loss rate.
    "C": dict(label="比分接近时弱队起势", weight=1.0, side="dog", style="process",
              pre_min=None, state=(-5.0, 0.0), sign=-1, tp=TP,
              price_max=None, game_hold=30 * 60),
    # D's thesis is "favourite leading 5-10".  If the lead actually turns
    # negative after entry, the thesis is broken: exit at the market instead
    # of riding a -100% settlement.  In 159 backtest trades this turned every
    # settlement loss into a small stop and lifted the mean from +6.5% to
    # +10.1%; the live Thunder D on 2026-11-14 went from -7.61 to -0.46.
    "D": dict(label="末节强队锁胜", weight=2.0, side="fav", style="settle",
              pre_min=0.60, state=(5.0, 10.0), sign=None, tp=None,
              price_min=0.70, price_max=0.80, stop_margin=0.0),
}
D_EL_MIN = 2040         # D starts at Q3 6:00 remaining (was Q4 = 2400)

BASE_STAKE_FRAC = 0.05  # of capital, per unit of weight
START_BAL = 100.0
MAX_OPEN = 8             # historical max concurrent was 9; 8 captures 1554/1556


# ------------------------------------------------------------------ plumbing
def px_at(ph, t):
    i = bisect.bisect_right([x[0] for x in ph], t) - 1
    return ph[i] if i >= 0 else None


def next_print(ph, t, max_wait=MAX_WAIT):
    for ts, p in ph:
        if ts > t:
            return (ts, p) if ts - t <= max_wait else None
    return None


def margin_at(evs, el):
    i = bisect.bisect_right([e[0] for e in evs], el) - 1
    return evs[i][1] if i >= 0 else None


def view(g):
    """(events oriented to the favourite, pregame fav price, fav prints, dog prints)."""
    evs = nd.margin_timeline(g["plays"])
    hf = g["x"].get("home_favorite", True)
    evs = [(el, (hm if hf else -hm), w) for el, hm, w in evs]   # m_fav
    fph = sorted((int(p[0]), float(p[1])) for p in g["x"].get("history") or [])
    dph = sorted(g["dog"])
    tip = nd.parse_wall(g["x"].get("actual_tip")
                        or (g["x"].get("states") or [[None]])[0][0])
    pre = px_at(fph, tip)[1] if (tip and fph) else None
    return evs, pre, fph, dph


def label_sane(g, pre, dph):
    """Broken records must never be traded (see report §1.1)."""
    if pre is None or pre < 0.50:
        return False
    tip = nd.parse_wall(g["x"].get("actual_tip")
                        or (g["x"].get("states") or [[None]])[0][0])
    if not tip or not dph:
        return True
    d = px_at(dph, tip)
    return d is None or pre >= d[1]


def series_for(ch, fph, dph):
    return dph if ch["side"] == "dog" else fph


# ------------------------------------------------------------------ signals
def process_signal(evs, pre, ch):
    if pre is None:
        return None
    if ch["pre_min"] is not None and pre < ch["pre_min"]:
        return None
    lo, hi = ch["state"]
    for el, m, w in evs:
        if el > FORCE_EL:
            break
        if not (lo <= m < hi):
            continue
        m0 = margin_at(evs, el - SIGNAL_WIN)
        if m0 is None:
            continue
        if (m - m0) * ch["sign"] < SIGNAL_K:
            continue
        return {"el": el, "margin": m, "wall": w}
    return None


def settle_signal(evs, pre, ch):
    """First moment of the state. No retry later in the game."""
    if pre is None or pre < ch["pre_min"]:
        return None
    lo, hi = ch["state"]
    for el, m, w in evs:
        if el < D_EL_MIN:
            continue
        if lo <= m < hi:
            return {"el": el, "margin": m, "wall": w}
    return None


# ------------------------------------------------------------------ exits
def simulate_process(ph, t_sig, t_force, tp, price_max,
                     max_hold=None, evs=None, game_hold=None):
    """PROCESS EXIT ONLY. Returns (entry, exit, reason, t_in, t_out), status.

    Reasons are 'target', 'time' or 'force'. There is no settlement path.
    `game_hold` (game-clock seconds from entry) is the C rule: after 25
    minutes of GAME clock the position is sold at the market.  `max_hold`
    (wall-clock seconds) is kept for research comparisons.
    """
    ent = next_print(ph, t_sig + ENTRY_DELAY)
    if ent is None:
        return None, "no_entry"
    t_in, raw = ent
    entry = raw + entry_cost(raw)
    if entry < PRICE_FLOOR or (price_max is not None and entry > price_max):
        return None, "no_entry"
    if t_force is None:
        return None, "late"
    if game_hold and evs:
        el_in = None
        for el, _m, w in evs:
            if w <= t_in:
                el_in = el
            else:
                break
        if el_in is None:
            el_in = evs[0][0]
        wt = nd.wall_at_el(evs, el_in + game_hold)
        t_end = min(t_force, wt) if wt is not None else t_force
    elif max_hold:
        t_end = min(t_force, t_in + max_hold)
    else:
        t_end = t_force
    if t_in >= t_end:
        return None, "late"
    target = entry + tp
    for ts, p in ph:
        if ts <= t_in:
            continue
        if ts >= t_end:
            break
        if p + 1e-9 >= target:
            return (entry, target - exit_cost(target), "target", t_in, ts), "ok"
    before = [(ts, p) for ts, p in ph if t_in < ts <= t_end]
    if not before:
        return None, "no_exit"
    ts, p = before[-1]
    reason = "time" if (max_hold or game_hold) and t_end < t_force else "force"
    return (entry, max(p - exit_cost(p), 0.0), reason, t_in, ts), "ok"


def simulate_settle(ph, t_sig, final_fav_won, lo, hi,
                    evs=None, stop_margin=None):
    """D: margin_stop or settlement. Returns (entry, exit, reason, t_in, t_out).

    The thesis is "favourite leading 5-10".  If the lead actually turns
    negative after entry (`stop_margin=0`), the thesis is broken: exit at
    the market instead of riding a -100% settlement.  If the price at the
    first state moment is outside [lo, hi], the game is skipped; it is NOT
    retried.
    """
    ent = next_print(ph, t_sig + ENTRY_DELAY)
    if ent is None:
        return None, "no_entry"
    t_in, raw = ent
    entry = raw + entry_cost(raw)
    if not (lo <= entry <= hi):
        return None, "price_band"
    if stop_margin is not None and evs:
        for el, m, w in evs:
            if w <= t_in:
                continue
            if m < stop_margin:
                px = px_at(ph, w)
                if px is not None:
                    return (entry, px[1] - exit_cost(px[1]),
                            "margin_stop", t_in, px[0]), "ok"
                break
    settle = 1.0 if final_fav_won else 0.0
    return (entry, settle, "settle", t_in, t_in), "ok"


# ------------------------------------------------------------------ run
def run(data, data_all=None):
    """data = games with dog prices (A/C). data_all = every usable game (D)."""
    trades, drops = [], collections.Counter()
    dog_ids = {g["x"]["id"] for g in data}
    universe = list(data)
    if data_all is not None:
        universe += [g for g in data_all if g["x"]["id"] not in dog_ids]
    for g in universe:
        evs, pre, fph, dph = view(g)
        if len(evs) < 10 or not label_sane(g, pre, dph):
            continue
        gid, season = g["x"]["id"], g["x"].get("season")
        fav_won = evs[-1][1] > 0
        has_dog = bool(dph)
        for key, ch in CHANNELS.items():
            if ch["style"] == "process" and not has_dog:
                continue          # dog-side channels need the dog price series
            ph = series_for(ch, fph, dph)
            if ch["style"] == "process":
                sig = process_signal(evs, pre, ch)
                if sig is None:
                    continue
                t_force = nd.wall_at_el(evs, FORCE_EL)
                r, st = simulate_process(ph, sig["wall"], t_force, ch["tp"],
                                         ch["price_max"], ch.get("max_hold"),
                                         evs, ch.get("game_hold"))
                if st != "ok":
                    drops[(key, st)] += 1
                    continue
                entry, exit_p, reason, t_in, t_out = r
                assert reason in ("target", "time", "force"), f"process exit leaked: {reason}"
            else:
                sig = settle_signal(evs, pre, ch)
                if sig is None:
                    continue
                r, st = simulate_settle(ph, sig["wall"], fav_won,
                                        ch["price_min"], ch["price_max"],
                                        evs, ch.get("stop_margin"))
                if st != "ok":
                    drops[(key, st)] += 1
                    continue
                entry, exit_p, reason, t_in, t_out = r
                assert reason in ("settle", "margin_stop"), f"settle exit leaked: {reason}"
            side_won = fav_won if ch["side"] == "fav" else (not fav_won)
            worst = min([p for ts, p in ph if t_in < ts <= t_out] or [entry])
            trades.append({"gid": gid, "season": season, "ch": key,
                           "entry": entry, "exit": exit_p, "reason": reason,
                           "t_in": t_in, "t_out": t_out, "worst": worst,
                           "ret": (exit_p - entry) / entry,
                           "side_won": side_won, "el": sig["el"]})
    trades.sort(key=lambda t: t["t_in"])
    return trades, drops


# ------------------------------------------------------------------ stats
def summarize(rows):
    if not rows:
        return None
    v = [t["ret"] for t in rows]
    by = collections.defaultdict(list)
    for t in rows:
        by[t["gid"]].append(t["ret"])
    means = [sum(x) / len(x) for x in by.values()]
    random.seed(7)
    bs = sorted(sum(random.choice(means) for _ in means) / len(means)
                for _ in range(2000))
    return {"n": len(rows), "mean": sum(v) / len(v),
            "med": statistics.median(v),
            "win": sum(1 for x in v if x > 0) / len(v),
            "lo": bs[49], "hi": bs[1949],
            "price": statistics.mean(t["entry"] for t in rows),
            "realized": sum(1 for t in rows if t["side_won"]) / len(rows),
            "reasons": dict(collections.Counter(t["reason"] for t in rows))}


def show(label, s):
    if not s:
        print(f"  {label:16s} n=0")
        return
    print(f"  {label:16s} n={s['n']:4d} 每笔{100*s['mean']:+7.1f}% "
          f"中位{100*s['med']:+7.1f}% 胜{100*s['win']:5.1f}% "
          f"价{s['price']:.3f} 实际{100*s['realized']:5.1f}% "
          f"校准{100*(s['realized'] - s['price']):+5.1f}pp "
          f"boot[{100*s['lo']:+.0f},{100*s['hi']:+.0f}] {s['reasons']}")


def portfolio(trades, start=START_BAL, base_frac=BASE_STAKE_FRAC):
    """Cash-constrained equity path. Stake = capital fraction x channel weight."""
    ev = []
    for i, t in enumerate(trades):
        ev.append((t["t_in"], 0, i))
        ev.append((t["t_out"], 1, i))
    ev.sort(key=lambda x: (x[0], x[1]))
    cash, open_, peak, mdd, fills, skipped = start, {}, start, 0.0, 0, 0
    expo = 0.0
    for tm, kind, i in ev:
        t = trades[i]
        stake = start * base_frac * CHANNELS[t["ch"]]["weight"]
        if kind == 0:
            s = min(stake, cash)
            if s < 0.01 or len(open_) >= MAX_OPEN:
                skipped += 1
                continue
            cash -= s
            open_[i] = s
            fills += 1
        else:
            if i not in open_:
                continue
            cash += open_.pop(i) * (1 + t["ret"])
        # mark open positions at their worst print, not at cost — a cost-basis
        # equity curve hides the drawdown of concurrent losers
        mtm = sum(s * (trades[j]["worst"] - trades[j]["entry"]) / trades[j]["entry"]
                  for j, s in open_.items())
        eq = cash + sum(open_.values()) + mtm
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)
        expo = max(expo, sum(open_.values()))
    return {"end": cash, "mdd": mdd, "fills": fills, "skipped": skipped,
            "peak_exposure": expo}


def per_game(trades, base_frac=BASE_STAKE_FRAC, start=START_BAL):
    by = collections.defaultdict(float)
    for t in trades:
        by[t["gid"]] += start * base_frac * CHANNELS[t["ch"]]["weight"] * t["ret"]
    vals = [by[g] for g in by]
    random.seed(7)
    bs = sorted(sum(random.choice(vals) for _ in vals) / len(vals)
                for _ in range(2000))
    return {"games": len(vals), "mean": sum(vals) / len(vals),
            "lo": bs[49], "hi": bs[1949]}


# ------------------------------------------------------------------ live
def load_live(days):
    """Rebuild live days from CLOB history (complete game coverage)."""
    cache = nd.load_live_cache()
    games = []
    for d in days:
        games.extend(nd.load_live_day(d, cache))
    nd.save_live_cache(cache)
    return games


def run_live(games):
    """Same rules, applied to rebuilt live games (prints, same cost model)."""
    trades, drops = [], collections.Counter()
    for g in games:
        evs, pre, fph = g["evs"], g["pre"], g["prices"].get(g["fav"]) or []
        dph = g["prices"].get(next(o for o in g["outcomes"] if o != g["fav"])) or []
        if not fph or not dph or pre is None:
            continue
        fav_won = evs[-1][1] > 0
        has_dog = bool(dph)
        for key, ch in CHANNELS.items():
            if ch["style"] == "process" and not has_dog:
                continue          # dog-side channels need the dog price series
            ph = series_for(ch, fph, dph)
            if ch["style"] == "process":
                sig = process_signal(evs, pre, ch)
                if sig is None:
                    continue
                t_force = nd.wall_at_el(evs, FORCE_EL)
                r, st = simulate_process(ph, sig["wall"], t_force, ch["tp"],
                                         ch["price_max"], ch.get("max_hold"),
                                         evs, ch.get("game_hold"))
            else:
                sig = settle_signal(evs, pre, ch)
                if sig is None:
                    continue
                r, st = simulate_settle(ph, sig["wall"], fav_won,
                                        ch["price_min"], ch["price_max"],
                                        evs, ch.get("stop_margin"))
            if st != "ok":
                drops[(key, st)] += 1
                continue
            entry, exit_p, reason, t_in, t_out = r
            side_won = fav_won if ch["side"] == "fav" else (not fav_won)
            worst = min([p for ts, p in ph if t_in < ts <= t_out] or [entry])
            trades.append({"gid": g["gid"], "ch": key, "entry": entry,
                           "exit": exit_p, "reason": reason, "t_in": t_in,
                           "t_out": t_out, "worst": worst,
                           "ret": (exit_p - entry) / entry,
                           "side_won": side_won, "el": sig["el"]})
    trades.sort(key=lambda t: t["t_in"])
    return trades, drops


# ------------------------------------------------------------------ checks
def self_check(data):
    print("自检（断言，不是文档）")
    bad = 0
    for g in data:
        evs, pre, fph, dph = view(g)
        if pre is not None and dph and not label_sane(g, pre, dph):
            bad += 1
    print(f"  标签不可信的场次 {bad}/{len(data)}（已由 label_sane 排除）")
    print(f"  通道方向: " + "，".join(f"{k} 买 {c['side']}" for k, c in CHANNELS.items()))
    print(f"  出场: A/C 只允许 target/time(C)/force（永无结算）；"
          f"D 只允许 settle/margin_stop")
    return True


def main():
    data = nd.load_data(require_dog=True)
    data_all = nd.load_data(require_dog=False)
    print(f"games={len(data)}（含弱队价格）/ {len(data_all)}（全部）  "
          f"成本 入场{100*ENTRY_COST:.0f}c + 出场{100*EXIT_COST:.0f}c  "
          f"基础仓位 {100*BASE_STAKE_FRAC:.0f}%×权重")
    self_check(data)
    trades, drops = run(data, data_all)
    print(f"\n=== 回测 ===")
    for key, ch in CHANNELS.items():
        show(f"{key} {ch['label']}", summarize([t for t in trades if t["ch"] == key]))
    print(f"  丢弃: {dict(drops)}")
    pg = per_game(trades)
    acc = portfolio(trades)
    print(f"\n  每场组合损益 ${pg['mean']:+.3f}  (n={pg['games']} 场) "
          f"boot95% [${pg['lo']:+.3f}, ${pg['hi']:+.3f}]")
    print(f"  成交频率 {len(trades)} 笔 / {len(data_all)} 场 = {len(trades)/len(data_all):.2f} 笔/场")
    print(f"  账户 ${START_BAL:.0f} → ${acc['end']:.2f}  盯市回撤 ${acc['mdd']:.1f}  "
          f"峰值敞口 ${acc['peak_exposure']:.0f}  成交 {acc['fills']}  跳过 {acc['skipped']}")

    if "--live" in sys.argv:
        days = ["2026-11-12", "2026-11-13"]
        games = load_live(days)
        lt, ldrops = run_live(games)
        print(f"\n=== 实盘 {len(games)} 场 ===")
        for key, ch in CHANNELS.items():
            show(f"{key} {ch['label']}", summarize([t for t in lt if t["ch"] == key]))
        print(f"  丢弃: {dict(ldrops)}")
        lacc = portfolio(lt)
        print(f"  账户 ${START_BAL:.0f} → ${lacc['end']:.2f}  盯市回撤 ${lacc['mdd']:.1f}  "
              f"峰值敞口 ${lacc['peak_exposure']:.0f}  成交 {lacc['fills']}  跳过 {lacc['skipped']}")
        for t in lt:
            print(f"    {t['gid']} {t['ch']} 入{t['entry']:.3f} 出{t['exit']:.3f} "
                  f"({t['reason']}) {100*t['ret']:+7.1f}%")


if __name__ == "__main__":
    main()
