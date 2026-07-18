#!/usr/bin/env python3
"""
Sweep rolling 6h session windows across a 0-DTE BTC option contract's full
24h life, stepped every 4h, starting at the moment it becomes the fresh
front daily expiry (previous day's 12:00 UTC settlement = 17:30 IST) through
its own settlement (12:00 UTC = 17:30 IST).

Windows are expressed as hour offsets relative to session_date_ts (00:00 UTC
on the expiry date, the anchor engine.py/data_fetch.py already use):
  W1: [-12, -6]  = 17:30-23:30 IST (prior day)
  W2: [-8, -2]   = 21:30 IST (prior day) - 03:30 IST
  W3: [-4, +2]   = 01:30 - 07:30 IST
  W4: [0, +6]    = 05:30 - 11:30 IST
  W5: [+4, +10]  = 09:30 - 15:30 IST
  W6: [+8, +12]  = 13:30 - 17:30 IST (clipped to settlement, 4h not 6h)

Requires the 24h-window option data (data_fetch.py fetches previous day's
12:00 UTC to this day's 12:00 UTC as of 2026-07-18) -- the original 12h-only
fetch doesn't cover hours before 00:00 UTC and every window before W4 would
silently come back empty against that older data.

Other params held at the locked baseline (SL 90%, RR 3.0, ITM-0, restrike
5min, interval 2h, htf DAY) unless overridden.

Usage:
    python rolling_session_sweep.py --workers 6
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

FIXED = dict(itm=0, option_types=('C', 'P'), qty=1, htf='DAY',
             sl_pct_of_entry=0.90, rr=3.0,
             restrike_minutes=5.0, interval_hours=2.0)

WINDOW_WIDTH_HOURS = 6.0
STEP_HOURS = 4.0
RANGE_START = -12.0   # previous day's 12:00 UTC settlement = 17:30 IST
RANGE_END = 12.0       # this day's 12:00 UTC settlement = 17:30 IST

MIN_TRADES = int(os.getenv("MIN_TRADES", "5"))

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


def _ist_label(utc_hour_offset: float) -> str:
    """Render an hour-offset-from-day-00:00-UTC as an IST clock time string,
    with a day-shift suffix relative to the expiry day's own calendar date."""
    from datetime import timedelta
    ref_date = datetime(2000, 1, 1).date()
    base = datetime(2000, 1, 1) + timedelta(hours=5.5)  # day 00:00 UTC = 05:30 IST, same date
    t = base + timedelta(hours=utc_hour_offset)
    shift = (t.date() - ref_date).days
    suffix = f" ({shift:+d}d)" if shift != 0 else ""
    return t.strftime('%H:%M') + suffix


def build_windows():
    windows = []
    start = RANGE_START
    while start < RANGE_END:
        end = min(start + WINDOW_WIDTH_HOURS, RANGE_END)
        windows.append({'session_start_hour': start, 'session_end_hour': end})
        start += STEP_HOURS
    return windows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--options-dir', default=os.path.join(DATA_DIR, 'options'))
    parser.add_argument('--spot-csv', default=None)
    parser.add_argument('--days-back', type=int, default=None)
    parser.add_argument('--workers', type=int, default=max(1, multiprocessing.cpu_count() - 1))
    parser.add_argument('--sort-by', default='profit_factor')
    args = parser.parse_args()

    spot_csv = args.spot_csv
    if spot_csv is None:
        candidates = sorted(glob.glob(os.path.join(DATA_DIR, 'spot', '*.csv')),
                            key=lambda p: os.path.getsize(p), reverse=True)
        if not candidates:
            print("No spot CSV found -- run data_fetch.py first.")
            sys.exit(1)
        spot_csv = candidates[0]

    windows = build_windows()
    print(f"Rolling session sweep: {len(windows)} windows | workers={args.workers} | "
          f"min_trades={MIN_TRADES} | fixed={FIXED}")
    for w in windows:
        print(f"  [{w['session_start_hour']:+.1f}h, {w['session_end_hour']:+.1f}h] UTC-offset "
              f"= {_ist_label(w['session_start_hour'])} - {_ist_label(w['session_end_hour'])} IST")

    t0 = time.time()
    results = []
    with multiprocessing.Pool(processes=args.workers, initializer=_init_worker,
                              initargs=(args.options_dir, spot_csv, args.days_back)) as pool:
        for i, row in enumerate(pool.imap_unordered(_run_combo, windows, chunksize=1)):
            if row is not None:
                results.append(row)
            elapsed = time.time() - t0
            done = i + 1
            eta = (elapsed / done) * (len(windows) - done)
            print(f"\r  {done}/{len(windows)}  valid={len(results)}  "
                  f"elapsed={elapsed:.0f}s  eta={eta:.0f}s   ", end='', flush=True)

    print(f"\n\nDone in {time.time()-t0:.0f}s. {len(results)}/{len(windows)} windows "
          f"passed MIN_TRADES={MIN_TRADES}.\n")

    if not results:
        print("No results passed the minimum-trades filter -- did you re-run data_fetch.py "
              "with the extended 24h window? Old 0-11h-only data won't have anything before "
              "session_start_hour=0.")
        sys.exit(0)

    df = pd.DataFrame(results)
    df['ist_label'] = df.apply(
        lambda r: f"{_ist_label(r['session_start_hour'])}-{_ist_label(r['session_end_hour'])}", axis=1)
    df = df.sort_values(args.sort_by, ascending=False).reset_index(drop=True)

    print(f"All {len(df)} rolling windows by {args.sort_by}:")
    print(df.to_string(index=True))

    out_dir = os.path.join(DATA_DIR, 'results')
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    out_path = os.path.join(out_dir, f'rolling_session_sweep_{ts}.csv')
    df.to_csv(out_path, index=False)
    print(f"\nFull results saved to {out_path}")


if __name__ == "__main__":
    main()
