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
from typing import List, Dict, Optional

import pandas as pd
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(__file__))
from backtest import BacktestParams, load_csv, run_backtest, compute_metrics

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
    'bb_period':        [10, 15, 20, 30],
    'bb_std_dev':       [0.5, 1.0, 1.5, 2.0, 2,5, 3],
    'take_profit_pct':  [10, 20, 30],
    'stop_loss_pct':    [5, 10, 15],
    'min_rr':           [1.5, 2.0, 3.0],
    # ADX filter — add True + multiple thresholds to include in sweep
    'use_adx_filter':   [True,False],
    'adx_threshold':    [15,20,25],
    # signal_expiry_bars is a strategy param set via .env (SIGNAL_EXPIRY_BARS), not swept here
}

# ── Optimizer config (can also override via env) ──────────────────────────────
SORT_BY    = os.getenv("SORT_BY",    "profit_factor")   # or: calmar_ratio, total_return_pct, win_rate
MIN_TRADES = int(os.getenv("MIN_TRADES", "5"))          # skip combos with fewer closed trades
TOP_N      = int(os.getenv("TOP_N",      "20"))         # rows to print in the final table
WORKERS    = int(os.getenv("WORKERS",    str(max(1, multiprocessing.cpu_count() - 1))))


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
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    csv_paths = sys.argv[1:]

    # Load all CSVs once — shared across all combos
    candles_by_symbol: Dict[str, List] = {}
    for path in csv_paths:
        if not os.path.exists(path):
            print(f"File not found: {path}")
            sys.exit(1)
        symbol = os.path.splitext(os.path.basename(path))[0]
        print(f"Loading {path} ...")
        candles_by_symbol[symbol] = load_csv(path)
        print(f"  {len(candles_by_symbol[symbol])} candles for {symbol}")

    combos = build_combos(PARAM_GRID)
    total  = len(combos)
    print(f"\nGrid search: {total} combos × {len(candles_by_symbol)} symbol(s) | "
          f"Workers: {WORKERS} | Sort: {SORT_BY} | Min trades: {MIN_TRADES}")
    print(f"Param ranges:")
    for k, v in PARAM_GRID.items():
        print(f"  {k}: {v}")
    print()

    # Build worker args
    worker_args = [(candles_by_symbol, combo) for combo in combos]

    # Run with progress tracking
    results = []
    t0      = time.time()
    done    = 0

    with multiprocessing.Pool(processes=WORKERS) as pool:
        for result in pool.imap_unordered(_run_combo, worker_args, chunksize=4):
            done += 1
            if result is not None:
                results.append(result)

            # Progress line
            elapsed  = time.time() - t0
            eta      = (elapsed / done) * (total - done) if done else 0
            pct      = done / total * 100
            print(f"\r  {done}/{total} ({pct:.0f}%)  "
                  f"valid: {len(results)}  "
                  f"elapsed: {elapsed:.0f}s  "
                  f"eta: {eta:.0f}s    ", end='', flush=True)

    elapsed = time.time() - t0
    print(f"\n\nDone in {elapsed:.1f}s. {len(results)}/{total} combos passed MIN_TRADES={MIN_TRADES}.\n")

    if not results:
        print("No results passed the minimum trades filter.")
        sys.exit(0)

    df = pd.DataFrame(results)

    # Sort: descending for most metrics, but ascending for max_drawdown
    ascending = SORT_BY in ('max_drawdown_pct', 'max_consec_losses')
    df = df.sort_values(SORT_BY, ascending=ascending).reset_index(drop=True)

    # ── Print top N ───────────────────────────────────────────────────────────
    param_cols  = list(PARAM_GRID.keys())
    metric_cols = ['total_trades', 'win_rate', 'profit_factor',
                   'total_return_pct', 'max_drawdown_pct', 'calmar_ratio',
                   'max_consec_losses']
    show_cols   = [c for c in param_cols + metric_cols if c in df.columns]

    print(f"Top {min(TOP_N, len(df))} results sorted by {SORT_BY}:")
    print(df.head(TOP_N)[show_cols].to_string(index=True))
    print()

    # ── Best combo details ────────────────────────────────────────────────────
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

    # ── Save full results ─────────────────────────────────────────────────────
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data')
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, 'optimizer_results.csv')
    df.to_csv(out_path, index=False)
    print(f"\nFull results ({len(df)} rows) saved to {out_path}")
    print(f"Tip: open in a spreadsheet and sort by calmar_ratio or profit_factor to explore.")


if __name__ == "__main__":
    main()
