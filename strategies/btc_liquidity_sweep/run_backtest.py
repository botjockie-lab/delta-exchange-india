#!/usr/bin/env python3
"""
CLI for the BTC liquidity-sweep backtest.

Usage:
    python run_backtest.py --days-back 365 --itm 2 --types C,P \
      --sl-pct-of-entry 0.25 --rr 2.0 --interval-hours 1 --restrike-minutes 5 \
      --starting-capital 10000
"""

import os
import sys
import glob
import argparse
import json

import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from engine import BacktestConfig, run_backtest
from costs import CostModel
from stats import summarize, build_equity_curve

DATA_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'btc_liquidity_sweep')


def load_daily_frames(options_dir: str, days_back: int = None):
    files = sorted(glob.glob(os.path.join(options_dir, '*.csv')))
    if days_back:
        files = files[-days_back:]
    frames = {}
    for f in files:
        ddmmyy = os.path.splitext(os.path.basename(f))[0]
        dd, mm, yy = ddmmyy[:2], ddmmyy[2:4], ddmmyy[4:6]
        day_str = f"20{yy}-{mm}-{dd}"
        df = pd.read_csv(f)
        frames[day_str] = df
    return frames


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--options-dir', default=os.path.join(DATA_DIR, 'options'))
    parser.add_argument('--spot-csv', default=None, help='defaults to the widest spot CSV in data dir')
    parser.add_argument('--days-back', type=int, default=None, help='limit to last N fetched days')
    parser.add_argument('--itm', type=int, default=2)
    parser.add_argument('--types', default='C,P')
    parser.add_argument('--interval-hours', type=float, default=1.0)
    parser.add_argument('--restrike-minutes', type=float, default=5.0)
    parser.add_argument('--sl-pct-of-entry', type=float, default=0.50)
    parser.add_argument('--rr', type=float, default=4.0)
    parser.add_argument('--htf', default='DAY', choices=['DAY', '1H', '2H', '4H', '6H'],
                        help='entry reference low: DAY = running low so far, '
                             '1H/4H = low of the last fully-closed N-hour candle')
    parser.add_argument('--session-start-hour', type=float, default=0.0,
                        help='UTC hour session/entries start (default 0.0 = 05:30 IST)')
    parser.add_argument('--session-end-hour', type=float, default=11.0,
                        help='UTC hour session/entries end (default 11.0 = 16:30 IST, '
                             '1h before Delta\'s 17:30 IST daily settlement)')
    parser.add_argument('--qty', type=int, default=1)
    parser.add_argument('--starting-capital', type=float, default=10000.0)
    parser.add_argument('--out-dir', default=os.path.join(DATA_DIR, 'results'))
    args = parser.parse_args()

    spot_csv = args.spot_csv
    if spot_csv is None:
        candidates = sorted(glob.glob(os.path.join(DATA_DIR, 'spot', '*.csv')),
                            key=lambda p: os.path.getsize(p), reverse=True)
        if not candidates:
            print("No spot CSV found -- run data_fetch.py first.")
            sys.exit(1)
        spot_csv = candidates[0]
    print(f"Spot: {spot_csv}")
    spot_df = pd.read_csv(spot_csv)

    print(f"Options dir: {args.options_dir}")
    daily_frames = load_daily_frames(args.options_dir, args.days_back)
    print(f"Loaded {len(daily_frames)} expiry days")
    if not daily_frames:
        print("No option data found -- run data_fetch.py first.")
        sys.exit(1)

    cfg = BacktestConfig(
        itm=args.itm,
        option_types=tuple(args.types.split(',')),
        interval_hours=args.interval_hours,
        restrike_minutes=args.restrike_minutes,
        sl_pct_of_entry=args.sl_pct_of_entry,
        rr=args.rr,
        qty=args.qty,
        htf=args.htf,
        session_start_hour=args.session_start_hour,
        session_end_hour=args.session_end_hour,
        cost_model=CostModel(),
    )

    print(f"\nConfig: ITM-{cfg.itm} {cfg.option_types}, htf={cfg.htf}, "
          f"session={cfg.session_start_hour}h-{cfg.session_end_hour}h UTC, "
          f"reset={cfg.interval_hours}h, restrike={cfg.restrike_minutes}m, "
          f"SL={cfg.sl_pct_of_entry*100:.0f}% RR={cfg.rr}, qty={cfg.qty}\n")

    trades = run_backtest(daily_frames, spot_df, cfg)
    summary = summarize(trades, args.starting_capital)

    print("─" * 60)
    for k, v in summary.items():
        print(f"  {k:24s} = {v}")
    print("─" * 60)

    os.makedirs(args.out_dir, exist_ok=True)
    trades_df = pd.DataFrame(trades)
    trades_path = os.path.join(args.out_dir, 'trades.csv')
    trades_df.to_csv(trades_path, index=False)
    print(f"\nTrades saved to {trades_path}")

    equity_df = build_equity_curve(trades, args.starting_capital)
    equity_path = os.path.join(args.out_dir, 'equity.csv')
    equity_df.to_csv(equity_path, index=False)

    summary_path = os.path.join(args.out_dir, 'summary.json')
    with open(summary_path, 'w') as f:
        json.dump(summary, f, indent=2, default=str)
    print(f"Summary saved to {summary_path}")


if __name__ == "__main__":
    main()
