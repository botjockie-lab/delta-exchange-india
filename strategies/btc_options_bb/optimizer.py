#!/usr/bin/env python3
"""
Parameter optimizer for the BTC Options BB Strategy.

Runs a grid search across parameter combinations and ranks them by a
chosen metric. Results are printed as a ranked table and saved to CSV.

Edit PARAM_GRID below to set the ranges you want to sweep.
Fixed parameters (not in the grid) are read from .env as defaults.

Usage:
    python optimizer.py data/C-BTC-63000-120626_1m.csv
    python optimizer.py data/C-BTC-63000-120626_1m.csv data/P-BTC-63000-120626_1m.csv

Env overrides:
    SORT_BY=calmar_ratio   python optimizer.py ...
    MIN_TRADES=10          python optimizer.py ...
    WORKERS=8              python optimizer.py ...
"""

import os
import sys
import time
import logging
import itertools
import multiprocessing
from dataclasses import asdict
from datetime import datetime
from typing import List, Dict, Optional

import pandas as pd
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(__file__))
from backtest import BacktestParams, load_csv, run_backtest, compute_metrics
from atm_backtest import run_atm_backtest, load_option_strikes

load_dotenv()

logging.basicConfig(level=logging.WARNING,  # quiet during sweep
                    format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# PARAMETER GRID — edit these lists to control the search space.
#
# Recommendations:
#   bb_period      : [10, 15, 20, 30]   — short vs medium lookback
#   bb_std_dev     : [0.5, 1.0, 1.5, 2.0, 2.5] — band width (tighter = more signals)
#   take_profit_pct: [10, 15, 20, 30, 50]  — target profit per trade
#   stop_loss_pct  : [5, 7.5, 10, 15]     — max loss per trade
#   min_rr         : [1.5, 2.0, 2.5, 3.0] — min reward:risk to upper BB
#   use_adx_filter : [False, True]         — enable/disable ADX trend filter
#   adx_threshold  : [20, 25, 30]          — only used when use_adx_filter=True
#   max_pending_bars: [3, 5, 10]           — bars to wait for entry fill
#
# ⚠ Combinatorial note: N values × M params = total combos. Keep small for speed.
#   Default below = 3×4×3×2×3 = 216 combos. Add use_adx_filter=[True] + 3
#   thresholds to get 216×4 = 864.
# ═══════════════════════════════════════════════════════════════════════════════

PARAM_GRID: Dict[str, list] = {
    # bb_period: dropped 15 (weakly supported, signals overlap with 10/20)
    'bb_period':        [10, 20, 30],
    # bb_std_dev: dropped 0.5 (redundant signals) and 2.5 (never appeared in top-20)
    'bb_std_dev':       [1.0, 1.5, 2.0, 3.0],
    # take_profit_pct: dropped 10 (always underperformed; kills R:R at min_rr constraints)
    'take_profit_pct':  [20, 30],
    'stop_loss_pct':    [5, 10, 15],
    'min_rr':           [1.5, 2.0, 3.0],
    # ADX filter: fixed off — prior run showed identical results with/without it
    'use_adx_filter':   [False],
    'adx_threshold':    [20],
    # EMA filter: fixed off — no prior data; re-enable once ADX case is established
    'use_ema_filter':   [False],
    'ema_period':       [50],
    # signal_expiry_bars is a strategy param set via .env (SIGNAL_EXPIRY_BARS), not swept here
}

# ── Optimizer config (can also override via env) ──────────────────────────────
SORT_BY         = os.getenv("SORT_BY",    "profit_factor")
MIN_TRADES      = int(os.getenv("MIN_TRADES", "5"))
TOP_N           = int(os.getenv("TOP_N",      "20"))
WORKERS         = int(os.getenv("WORKERS",    str(max(1, multiprocessing.cpu_count() - 1))))
STRIKE_INTERVAL = int(os.getenv("STRIKE_INTERVAL", "200"))


# ── Worker (must be module-level for multiprocessing pickling) ─────────────────

def _run_combo(args) -> Optional[Dict]:
    """Run one parameter combo across all symbols and return aggregated metrics."""
    candles_by_symbol, params_dict = args

    # Reconstruct params — keys in params_dict map 1:1 to BacktestParams fields
    base  = asdict(BacktestParams())          # defaults
    base.update(params_dict)
    p     = BacktestParams(**base)

    all_trades = []
    for symbol, candles in candles_by_symbol.items():
        if len(candles) < p.required_candles + 10:
            continue
        trades = run_backtest(symbol, candles, p)
        all_trades.extend(trades)

    m = compute_metrics(all_trades)
    if m['total_trades'] < MIN_TRADES:
        return None

    return {**params_dict, **m}


def _run_atm_combo(args) -> Optional[Dict]:
    """ATM-mode worker: one combo against the pre-loaded spot + strike data."""
    spot_candles, call_by_strike, put_by_strike, params_dict = args

    base = asdict(BacktestParams())
    base.update(params_dict)
    p = BacktestParams(**base)

    trades = run_atm_backtest(spot_candles, call_by_strike, put_by_strike,
                              p, STRIKE_INTERVAL)
    m = compute_metrics(trades)
    if m['total_trades'] < MIN_TRADES:
        return None

    return {**params_dict, **m}


# ── Combo generation ──────────────────────────────────────────────────────────

def build_combos(grid: Dict[str, list]) -> List[Dict]:
    """
    Generate all combos, skipping duplicates caused by irrelevant params
    (e.g. adx_threshold doesn't matter when use_adx_filter=False).
    """
    keys   = list(grid.keys())
    combos = []
    seen   = set()

    for values in itertools.product(*grid.values()):
        d = dict(zip(keys, values))

        # Normalize: zero out irrelevant dimensions to detect duplicates
        canonical = dict(d)
        if not canonical.get('use_adx_filter', True):
            canonical['adx_threshold'] = None
        if not canonical.get('use_ema_filter', True):
            canonical['ema_period'] = None

        key = tuple(sorted(canonical.items()))
        if key in seen:
            continue
        seen.add(key)
        combos.append(d)

    return combos


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--atm', action='store_true',
                        help='ATM-following mode: routes each bar to the current ATM strike')
    parser.add_argument('--spot', metavar='SPOT_CSV',
                        help='[ATM mode] BTC spot/perp 1m CSV for ATM determination')
    parser.add_argument('--data-dir', metavar='DIR', default='data/',
                        help='[ATM mode] directory containing option CSVs (default: data/)')
    parser.add_argument('--expiry', default='120626',
                        help='[ATM mode] option expiry tag (default: 120626)')
    parser.add_argument('csv_files', nargs='*',
                        help='[per-symbol mode] one or more option CSV files')
    args = parser.parse_args()

    combos = build_combos(PARAM_GRID)
    total  = len(combos)

    # ── ATM mode ──────────────────────────────────────────────────────────────
    if args.atm:
        if not args.spot:
            parser.error("--atm requires --spot SPOT_CSV")

        print(f"Loading spot: {args.spot}")
        spot_candles = load_csv(args.spot)
        print(f"  {len(spot_candles)} spot candles")

        call_by_strike, put_by_strike = load_option_strikes(
            args.data_dir, args.expiry)
        if not call_by_strike and not put_by_strike:
            print("No option data found — check --data-dir and --expiry")
            sys.exit(1)

        mode_label   = f"ATM (expiry={args.expiry}, interval={STRIKE_INTERVAL})"
        worker_fn    = _run_atm_combo
        worker_args  = [(spot_candles, call_by_strike, put_by_strike, c) for c in combos]

    # ── Per-symbol mode ───────────────────────────────────────────────────────
    else:
        if not args.csv_files:
            parser.print_help()
            sys.exit(1)

        candles_by_symbol: Dict[str, List] = {}
        for path in args.csv_files:
            if not os.path.exists(path):
                print(f"File not found: {path}")
                sys.exit(1)
            symbol = os.path.splitext(os.path.basename(path))[0]
            print(f"Loading {path} ...")
            candles_by_symbol[symbol] = load_csv(path)
            print(f"  {len(candles_by_symbol[symbol])} candles for {symbol}")

        mode_label  = f"{len(candles_by_symbol)} symbol(s)"
        worker_fn   = _run_combo
        worker_args = [(candles_by_symbol, c) for c in combos]

    print(f"\nGrid search: {total} combos | Mode: {mode_label} | "
          f"Workers: {WORKERS} | Sort: {SORT_BY} | Min trades: {MIN_TRADES}")
    print("Param ranges:")
    for k, v in PARAM_GRID.items():
        print(f"  {k}: {v}")
    print()

    # ── Run sweep ─────────────────────────────────────────────────────────────
    results = []
    t0      = time.time()
    done    = 0

    with multiprocessing.Pool(processes=WORKERS) as pool:
        for result in pool.imap_unordered(worker_fn, worker_args, chunksize=4):
            done += 1
            if result is not None:
                results.append(result)
            elapsed = time.time() - t0
            eta     = (elapsed / done) * (total - done) if done else 0
            print(f"\r  {done}/{total} ({done/total*100:.0f}%)  "
                  f"valid: {len(results)}  elapsed: {elapsed:.0f}s  eta: {eta:.0f}s    ",
                  end='', flush=True)

    elapsed = time.time() - t0
    print(f"\n\nDone in {elapsed:.1f}s. {len(results)}/{total} combos passed MIN_TRADES={MIN_TRADES}.\n")

    if not results:
        print("No results passed the minimum trades filter.")
        sys.exit(0)

    df = pd.DataFrame(results)
    ascending = SORT_BY in ('max_drawdown_pct', 'max_consec_losses')
    df = df.sort_values(SORT_BY, ascending=ascending).reset_index(drop=True)

    # ── Print top N ───────────────────────────────────────────────────────────
    param_cols  = list(PARAM_GRID.keys())
    metric_cols = ['total_trades', 'win_rate', 'profit_factor',
                   'total_return_pct', 'max_drawdown_pct', 'calmar_ratio',
                   'max_consec_losses']
    show_cols = [c for c in param_cols + metric_cols if c in df.columns]

    print(f"Top {min(TOP_N, len(df))} results sorted by {SORT_BY}:")
    print(df.head(TOP_N)[show_cols].to_string(index=True))
    print()

    best_row = df.iloc[0]
    print("─" * 60)
    print(f"Best combo (rank #1 by {SORT_BY}):")
    for k in param_cols:
        print(f"  {k:20s} = {best_row[k]}")
    print()
    for k in metric_cols:
        if k in best_row:
            print(f"  {k:20s} = {best_row[k]}")
    print("─" * 60)

    # ── Save timestamped results ──────────────────────────────────────────────
    out_dir  = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'data', 'btc_options_bb', 'results')
    os.makedirs(out_dir, exist_ok=True)
    ts       = datetime.now().strftime('%Y%m%d_%H%M%S')
    out_path = os.path.join(out_dir, f'optimizer_results_{ts}.csv')
    df.to_csv(out_path, index=False)
    print(f"\nFull results ({len(df)} rows) saved to {out_path}")
    print("Tip: sort by calmar_ratio or profit_factor to explore.")


if __name__ == "__main__":
    main()
