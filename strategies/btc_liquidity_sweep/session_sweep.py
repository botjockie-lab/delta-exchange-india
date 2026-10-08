#!/usr/bin/env python3
"""
Sweep the trading-session window (start/end UTC hour) for the BTC
liquidity-sweep backtest, holding every other parameter fixed at the current
best-found values from the SL/RR/restrike/interval sweep (optimizer.py):
SL 40% of entry, RR 3.0, restrike 5min, interval 2h, htf DAY, ITM-2.

Session hours are UTC; Delta settles daily BTC options at 17:30 IST =
12:00 UTC, so 0.0 UTC = 05:30 IST and 12.0 UTC = 17:30 IST (literal
settlement, no buffer). Generates all (start, end) pairs from the given
start/end candidate lists with end - start >= --min-duration-hours.

Usage:
    python session_sweep.py --workers 6
"""

import os
import sys
import time
import glob
import argparse
import itertools
import multiprocessing
from datetime import datetime
from typing import Dict, Optional

import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from engine import BacktestConfig, run_backtest
from costs import CostModel
from stats import summarize
from run_backtest import load_daily_frames, DATA_DIR

# Best-found-so-far from the SL/RR/restrike/interval sweep -- see project
# memory / prior sweep output (sweep_20260718_090234.csv, PF 0.757).
FIXED = dict(itm=2, option_types=('C', 'P'), qty=1, htf='DAY',
             sl_pct_of_entry=0.40, rr=3.0, restrike_minutes=5.0, interval_hours=2.0)

# UTC hours. 0.0 = 05:30 IST, 12.0 = 17:30 IST (literal settlement).
START_CANDIDATES = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]
END_CANDIDATES = [7.0, 8.0, 9.0, 10.0, 11.0, 12.0]

MIN_TRADES = int(os.getenv("MIN_TRADES", "20"))

_daily_frames: Optional[Dict[str, pd.DataFrame]] = None
_spot_df: Optional[pd.DataFrame] = None


def _init_worker(options_dir: str, spot_csv: str, days_back: Optional[int]):
    global _daily_frames, _spot_df
    _daily_frames = load_daily_frames(options_dir, days_back)
    _spot_df = pd.read_csv(spot_csv)


def _run_combo(window: Dict) -> Optional[Dict]:
    cfg = BacktestConfig(cost_model=CostModel(), **FIXED,
                          session_start_hour=window['session_start_hour'],
                          session_end_hour=window['session_end_hour'])
    trades = run_backtest(_daily_frames, _spot_df, cfg)
    summary = summarize(trades, starting_capital=10000.0)
    if summary.get('filled_trades', 0) < MIN_TRADES:
        return None
    row = {**window, 'duration_hours': window['session_end_hour'] - window['session_start_hour']}
    for k in ('filled_trades', 'win_rate', 'profit_factor', 'total_pnl',
              'total_return_pct', 'max_drawdown_pct', 'total_cycles'):
        row[k] = summary.get(k)
    return row


def build_combos(min_duration_hours: float):
    combos = []
    for start, end in itertools.product(START_CANDIDATES, END_CANDIDATES):
        if end - start >= min_duration_hours:
            combos.append({'session_start_hour': start, 'session_end_hour': end})
    return combos


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--options-dir', default=os.path.join(DATA_DIR, 'options'))
    parser.add_argument('--spot-csv', default=None)
    parser.add_argument('--days-back', type=int, default=None)
    parser.add_argument('--workers', type=int, default=max(1, multiprocessing.cpu_count() - 1))
    parser.add_argument('--sort-by', default='profit_factor')
    parser.add_argument('--min-duration-hours', type=float, default=2.0)
    args = parser.parse_args()

    spot_csv = args.spot_csv
    if spot_csv is None:
        candidates = sorted(glob.glob(os.path.join(DATA_DIR, 'spot', '*.csv')),
                            key=lambda p: os.path.getsize(p), reverse=True)
        if not candidates:
            print("No spot CSV found -- run data_fetch.py first.")
            sys.exit(1)
        spot_csv = candidates[0]

    combos = build_combos(args.min_duration_hours)
    print(f"Session-window sweep: {len(combos)} (start,end) pairs | workers={args.workers} | "
          f"min_trades={MIN_TRADES} | fixed={FIXED}")
    print(f"  starts (UTC): {START_CANDIDATES}")
    print(f"  ends   (UTC): {END_CANDIDATES}")
    print(f"  min duration: {args.min_duration_hours}h")

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

    print(f"\n\nDone in {time.time()-t0:.0f}s. {len(results)}/{len(combos)} windows "
          f"passed MIN_TRADES={MIN_TRADES}.\n")

    if not results:
        print("No results passed the minimum-trades filter.")
        sys.exit(0)

    df = pd.DataFrame(results)
    df = df.sort_values(args.sort_by, ascending=False).reset_index(drop=True)

    print(f"All {len(df)} session windows by {args.sort_by}:")
    print(df.to_string(index=True))

    out_dir = os.path.join(DATA_DIR, 'results')
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    out_path = os.path.join(out_dir, f'session_sweep_{ts}.csv')
    df.to_csv(out_path, index=False)
    print(f"\nFull results saved to {out_path}")


if __name__ == "__main__":
    main()
