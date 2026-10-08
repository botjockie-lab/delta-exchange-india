#!/usr/bin/env python3
"""
Grid-search optimizer for the BTC liquidity-sweep backtest.

Runs a parameter sweep across the full fetched dataset and ranks combos by
profit factor. htf/restrike/interval/session-window are fixed per-run (not
swept here) -- edit PARAM_GRID/FIXED below to widen or change what's held
constant.

Each worker process loads the (large) daily option + spot data ONCE via a
Pool initializer, not per-combo, since re-pickling ~587MB of CSVs-turned-
DataFrames for every combo would dominate runtime otherwise.

Usage:
    python optimizer.py --days-back 365 --workers 8
"""

import os
import sys
import time
import glob
import argparse
import multiprocessing
from datetime import datetime
from typing import Dict, Optional

import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from engine import BacktestConfig, run_backtest
from costs import CostModel
from stats import summarize
from run_backtest import load_daily_frames, DATA_DIR

# ── Sweep history ───────────────────────────────────────────────────────────
# Stage 1 (this file, 2026-07-18, SL/RR/restrike/interval, htf=DAY, itm=2,
#   session 0-11h UTC): winner PF 0.757 (SL 40%, RR 3.0, restrike 5min,
#   interval 2h) -- restrike=5min and wider SL/RR both trended toward their
#   grid boundary without peaking. Every one of 120 combos still PF<1.
# Stage 2 (session_sweep.py, 2026-07-18, session window, other params locked
#   to stage 1's winner): best PF 0.803, window 2:00-8:00 UTC (7:30-13:30 IST,
#   6h). Still PF<1 everywhere across 36 windows.
# Stage 3 (this file, 2026-07-18): session window now locked to stage 2's
#   winner (2:00-8:00 UTC); SL/RR range extended past stage 1's boundary
#   (up to 60%/RR4) since that trend hadn't peaked; itm now swept (was fixed
#   at 2 in stages 1-2, never varied) -- restrike/interval stay at stage 1's
#   winner (5min/2h). Winner: PF 0.928 (SL 60%, RR 4.0, itm=0/ATM), 1364
#   trades, win rate 29.3%, total loss -$7,980 -- smallest loss found yet,
#   but SL/RR were STILL trending toward the grid boundary, third straight
#   sweep without a peak. Chosen as the pivot baseline for stage 4.
# Stage 4 (this file, 2026-07-18): itm locked to stage 3's winner (0/ATM);
#   restrike/interval/session held at the same values as stage 3 (not
#   re-swept -- see stage 3's note that this is an untested joint
#   assumption); SL/RR range extended again (up to 90%/RR8) to find where
#   the trend actually peaks, if it does. Winner: PF 0.977 (SL 90%, RR 3.0),
#   895 trades, win rate 32.0%, total loss -$2,210 -- closest to breakeven
#   yet, by a wide margin. SL still hadn't clearly peaked (0.8->0.9 gain was
#   much smaller than earlier steps -- looked like a flattening plateau, not
#   confirmed). RR REVERSED direction at this SL level: at SL 0.8-0.9, lower
#   RR (3.0, the grid floor this stage) won, opposite of every earlier stage
#   -- RR's true optimum may sit below 3.0, untested.
#
# ── LOCKED BASELINE (as of stage 4, explicit user instruction) ────────────────
# sl_pct_of_entry=0.90, rr=3.0, itm=0, restrike_minutes=5.0, interval_hours=2.0,
# htf=DAY, session 2:00-8:00 UTC. PF 0.977 -- best found across all sweeps so
# far, but still <1 (losing). SL/RR/itm/restrike/interval/session stay fixed
# below at this baseline; PARAM_GRID now sweeps whatever's being explored
# next -- update both together so this comment doesn't drift out of sync.
#
# Stage 5 (this file, 2026-07-18): htf swept (1H/2H/4H/6H vs. the locked DAY
# baseline), everything else held at the stage-4 baseline. Result: DAY won
# decisively (PF 0.977 vs 1H 0.628, 2H 0.480, 4H 0.423; 6H produced 0 trades
# -- the 6h-wide locked session never lets a 6H bucket close). htf stays
# locked to DAY per explicit instruction -- htf dimension closed for now.
#
# Stage 6 (this file, 2026-07-18): interval_hours swept (unconditional reset
# cadence -- 15/30/45min plus 1h/2h[baseline]/3h/4h), everything else held
# at the stage-4/5 baseline (htf=DAY, sl=0.90, rr=3.0, itm=0, restrike=5min,
# session 2:00-8:00 UTC). Mostly flat 45min-4h, 15/30min clearly worse.
# Kept interval at 2h -- no lever here.
#
# Rolling-session sweep (rolling_session_sweep.py, 2026-07-18, separate
# script): 6h windows stepped 4h across the full 24h contract life (same
# SL=0.90/RR=3.0/itm=0 baseline). Only 17:30-23:30 IST (right after a fresh
# contract starts trading) crossed PF>1 (1.081, +$11,652). FAILED both
# robustness checks: removing the top 5 of 23 TARGET-hit trades flips it to
# -$651 (not a distributed edge), and it's negative in every one of the last
# 5 months (Mar-Jul 2026) despite being positive Aug 2025-Feb 2026 -- same
# regime-dependent signature as the BB-bounce strategy. See project memory.
#
# Stage 7 (this file, current): user hypothesis -- 17:30-23:30 IST plausibly
# has genuine structural edge (catches London-close/US-open volatility), and
# the tail-dependency on 23 TARGET hits may be an artifact of the wide
# SL=0.90/RR=3.0 baseline (picked by sweeping OTHER windows, never verified
# for this one) rather than the window itself. Sweep SL/RR fresh, session
# locked to 17:30-23:30 IST (-12h,-6h), to see whether PF>1 holds at
# tighter/different SL-RR combos with less skew, or only shows up at the
# specific wide extreme that maximizes a few outlier hits.
PARAM_GRID = {
    'sl_pct_of_entry':  [0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90],
    'rr':               [1.5, 2.0, 2.5, 3.0, 4.0],
}
FIXED = dict(option_types=('C', 'P'), qty=1, itm=0, htf='DAY',
             restrike_minutes=5.0, interval_hours=2.0,
             session_start_hour=-12.0, session_end_hour=-6.0)

MIN_TRADES = int(os.getenv("MIN_TRADES", "20"))

# Populated once per worker process by _init_worker.
_daily_frames: Optional[Dict[str, pd.DataFrame]] = None
_spot_df: Optional[pd.DataFrame] = None


def _init_worker(options_dir: str, spot_csv: str, days_back: Optional[int]):
    global _daily_frames, _spot_df
    _daily_frames = load_daily_frames(options_dir, days_back)
    _spot_df = pd.read_csv(spot_csv)


def _run_combo(params: Dict) -> Optional[Dict]:
    cfg = BacktestConfig(cost_model=CostModel(), **FIXED, **params)
    trades = run_backtest(_daily_frames, _spot_df, cfg)
    summary = summarize(trades, starting_capital=10000.0)
    if summary.get('filled_trades', 0) < MIN_TRADES:
        return None
    row = {**params}
    for k in ('filled_trades', 'win_rate', 'profit_factor', 'total_pnl',
              'total_return_pct', 'max_drawdown_pct', 'total_cycles'):
        row[k] = summary.get(k)
    return row


def build_combos():
    import itertools
    keys = list(PARAM_GRID.keys())
    return [dict(zip(keys, vals)) for vals in itertools.product(*PARAM_GRID.values())]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--options-dir', default=os.path.join(DATA_DIR, 'options'))
    parser.add_argument('--spot-csv', default=None)
    parser.add_argument('--days-back', type=int, default=None)
    parser.add_argument('--workers', type=int, default=max(1, multiprocessing.cpu_count() - 1))
    parser.add_argument('--sort-by', default='profit_factor')
    parser.add_argument('--top-n', type=int, default=25)
    args = parser.parse_args()

    spot_csv = args.spot_csv
    if spot_csv is None:
        candidates = sorted(glob.glob(os.path.join(DATA_DIR, 'spot', '*.csv')),
                            key=lambda p: os.path.getsize(p), reverse=True)
        if not candidates:
            print("No spot CSV found -- run data_fetch.py first.")
            sys.exit(1)
        spot_csv = candidates[0]

    combos = build_combos()
    print(f"Grid search: {len(combos)} combos | workers={args.workers} | "
          f"min_trades={MIN_TRADES} | fixed={FIXED}")
    for k, v in PARAM_GRID.items():
        print(f"  {k}: {v}")

    t0 = time.time()
    results = []
    with multiprocessing.Pool(processes=args.workers, initializer=_init_worker,
                              initargs=(args.options_dir, spot_csv, args.days_back)) as pool:
        for i, row in enumerate(pool.imap_unordered(_run_combo, combos, chunksize=1)):
            if row is not None:
                results.append(row)
            elapsed = time.time() - t0
            done = i + 1
            eta = (elapsed / done) * (len(combos) - done)
            print(f"\r  {done}/{len(combos)}  valid={len(results)}  "
                  f"elapsed={elapsed:.0f}s  eta={eta:.0f}s   ", end='', flush=True)

    print(f"\n\nDone in {time.time()-t0:.0f}s. {len(results)}/{len(combos)} combos "
          f"passed MIN_TRADES={MIN_TRADES}.\n")

    if not results:
        print("No results passed the minimum-trades filter.")
        sys.exit(0)

    df = pd.DataFrame(results)
    df = df.sort_values(args.sort_by, ascending=False).reset_index(drop=True)

    print(f"Top {min(args.top_n, len(df))} by {args.sort_by}:")
    print(df.head(args.top_n).to_string(index=True))

    out_dir = os.path.join(DATA_DIR, 'results')
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    out_path = os.path.join(out_dir, f'sweep_{ts}.csv')
    df.to_csv(out_path, index=False)
    print(f"\nFull results ({len(df)} rows) saved to {out_path}")


if __name__ == "__main__":
    main()
