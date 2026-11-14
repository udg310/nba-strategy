# -*- coding: utf-8 -*-
"""Build data/backtest_bundle.json.gz from the raw backtest sources.

The raw inputs are large (games.json ~178MB, diverse_opponents.json ~136MB,
data/cache ~236MB).  The bundle is the exact same rows as
`nba_data.load_data_raw()` — {"x": game, "plays": ..., "dog": ...} — gzipped
so the repository can stay small while remaining a complete replay dataset.

Run once after collecting new games:
    python3 -I scripts/make_bundle.py
"""
import gzip, json, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nba_data as nd

rows = nd.load_data_raw(require_dog=False)
with gzip.open(nd.BUNDLE, "wt", compresslevel=9) as f:
    json.dump(rows, f, separators=(",", ":"))
size = os.path.getsize(nd.BUNDLE)
print(f"wrote {nd.BUNDLE}: {len(rows)} games, {size/1024/1024:.1f} MB")
