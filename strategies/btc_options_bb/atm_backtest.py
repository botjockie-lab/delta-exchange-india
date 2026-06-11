#!/usr/bin/env python3
"""
ATM-following backtest for BTC Options BB Strategy.

Simulates the live strategy: at each bar the current ATM strike is determined
from BTC spot/perp price. Signal detection runs on the ATM CE and PE using
that strike's own candle history (so BB/EMA/ADX calculations are coherent).
Once a trade is entered it stays on the entry-strike's candles until TP/SL,
even if ATM has shifted.

Usage:
    python atm_backtest.py data/BTCUSD_1m.csv data/ --expiry 120626
    python atm_backtest.py data/BTCUSD_1m.csv data/ --expiry 120626 --interval 200

    BTCUSD_1m.csv : BTC spot/perp 1m candles  (fetch with fetch_historical_data.py BTCUSD)
    data/         : directory containing all *-120626_1m.csv option files

Env overrides (same as backtest.py):
    BB_PERIOD, BB_STD_DEV, TAKE_PROFIT_PERCENT, STOP_LOSS_PERCENT,
    MIN_RR, SIGNAL_EXPIRY_BARS, USE_ADX_FILTER, USE_EMA_FILTER, ...
    STRIKE_INTERVAL=200
"""

import os
import sys
import argparse
import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import pandas as pd
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(__file__))
from backtest import (BacktestParams, load_csv, check_signal, check_exit,
                      try_fill, compute_metrics, print_summary, plot_equity_curve)
from strategy import BollingerBandsAnalyzer

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

STRIKE_INTERVAL = int(os.getenv("STRIKE_INTERVAL", "200"))
RESOLUTION      = os.getenv("RESOLUTION", "1m")


# ── Strike helpers ────────────────────────────────────────────────────────────

def get_atm_strike(spot_price: float, interval: int) -> int:
    return round(spot_price / interval) * interval


def nearest_available(price: float, available: List[int], interval: int) -> Optional[int]:
    """Return the closest listed strike to the ideal ATM."""
    if not available:
        return None
    ideal = get_atm_strike(price, interval)
    if ideal in set(available):
        return ideal
    return min(available, key=lambda s: abs(s - ideal))


# ── Data loading ──────────────────────────────────────────────────────────────

def load_option_strikes(
    data_dir: str,
    expiry: str,
    resolution: str = "1m",
) -> Tuple[Dict[int, List], Dict[int, List]]:
    """
    Scan data_dir for all option CSVs matching the expiry.
    Returns (call_by_strike, put_by_strike): {strike: [candle_dicts]}.
    """
    call_by_strike: Dict[int, List] = {}
    put_by_strike:  Dict[int, List] = {}

    for fname in sorted(os.listdir(data_dir)):
        if not fname.endswith(f"_{resolution}.csv"):
            continue
        base   = fname[: -len(f"_{resolution}.csv")]
        parts  = base.split("-")
        if len(parts) != 4:
            continue
        opt_type, asset, strike_str, fexpiry = parts
        if asset != "BTC" or fexpiry != expiry or opt_type not in ("C", "P"):
            continue
        try:
            strike = int(strike_str)
        except ValueError:
            continue

        candles = load_csv(os.path.join(data_dir, fname))
        if not candles:
            continue
        (call_by_strike if opt_type == "C" else put_by_strike)[strike] = candles

    logger.info(f"Loaded {len(call_by_strike)} call strikes, {len(put_by_strike)} put strikes "
                f"(range: {min(call_by_strike or [0])}–{max(call_by_strike or [0])})")
    return call_by_strike, put_by_strike


def _build_indices(
    by_strike: Dict[int, List],
) -> Dict[int, Tuple[Dict[int, int], Dict[int, Dict]]]:
    """
    For each strike build:
      time_to_idx    : {timestamp → position in candle list}  (for check_signal)
      time_to_candle : {timestamp → candle dict}              (for check_exit / try_fill)
    Returns {strike: (time_to_idx, time_to_candle)}.
    """
    result: Dict[int, Tuple[Dict, Dict]] = {}
    for strike, candles in by_strike.items():
        t2i = {c['time']: i   for i, c in enumerate(candles)}
        t2c = {c['time']: c   for c in candles}
        result[strike] = (t2i, t2c)
    return result


# ── Core ATM backtest ─────────────────────────────────────────────────────────

def run_atm_backtest(
    spot_candles:     List[Dict],
    call_by_strike:   Dict[int, List],
    put_by_strike:    Dict[int, List],
    p:                BacktestParams,
    strike_interval:  int = 200,
) -> List[Dict]:
    """
    ATM-following backtest. Returns a combined list of trades (calls + puts).

    For each bar in spot_candles:
      • flat    → determine ATM, check ATM CE then ATM PE for signals
      • pending → track fill on the pending strike's own candles
      • in_trade→ track TP/SL on the entry strike's own candles

    signal detection uses each strike's own candle history so BB/EMA are clean.
    """
    analyzer = BollingerBandsAnalyzer()

    call_idx = _build_indices(call_by_strike)   # {strike: (t2i, t2c)}
    put_idx  = _build_indices(put_by_strike)

    avail_calls = sorted(call_by_strike.keys())
    avail_puts  = sorted(put_by_strike.keys())

    trades:       List[Dict] = []
    state         = 'flat'
    pending:      Optional[Dict] = None
    pending_bars  = 0
    open_trade:   Optional[Dict] = None
    trade_type:   Optional[str]  = None   # 'C' or 'P'
    trade_strike: Optional[int]  = None
    entry_bar:    Optional[int]  = None   # bar-idx within entry strike's candle list

    for spot_bar in spot_candles:
        t          = int(spot_bar['time'])
        spot_price = float(spot_bar['close'])

        # ── In-trade: track entry-strike candles ──────────────────────────────
        if state == 'in_trade':
            idx_map = call_idx if trade_type == 'C' else put_idx
            t2i, t2c = idx_map.get(trade_strike, ({}, {}))
            candle = t2c.get(t)
            if candle is None:
                continue

            result = check_exit(candle, open_trade['take_profit'], open_trade['stop_loss'])
            if result:
                outcome, exit_price = result
                cur_bar = t2i.get(t, entry_bar)
                pct = (exit_price - open_trade['entry_price']) / open_trade['entry_price'] * 100
                trades.append({
                    **open_trade,
                    'exit_time':   datetime.fromtimestamp(t),
                    'exit_price':  round(exit_price, 4),
                    'pct_return':  round(pct, 2),
                    'outcome':     outcome,
                    'bars_held':   cur_bar - entry_bar,
                })
                state = 'flat'
                open_trade = trade_type = trade_strike = entry_bar = None

        # ── Pending: check fill on pending-strike candles ─────────────────────
        elif state == 'pending':
            idx_map = call_idx if pending['opt_type'] == 'C' else put_idx
            t2i, t2c = idx_map.get(pending['strike'], ({}, {}))
            candle = t2c.get(t)

            if candle is not None:
                pending_bars += 1
                if try_fill(candle, pending['entry_price']):
                    trade_type   = pending['opt_type']
                    trade_strike = pending['strike']
                    entry_bar    = t2i.get(t, 0)
                    state        = 'in_trade'
                    open_trade   = {
                        'symbol':      f"{trade_type}-BTC-{trade_strike}",
                        'signal_time': datetime.fromtimestamp(pending['signal_candle']['time']),
                        'entry_time':  datetime.fromtimestamp(t),
                        'entry_price': round(pending['entry_price'], 4),
                        'take_profit': round(pending['take_profit'], 4),
                        'stop_loss':   round(pending['stop_loss'],   4),
                        'upper_band':  round(pending['upper_band'],  4),
                        'lower_band':  round(pending['lower_band'],  4),
                        'rr_ratio':    round(pending['rr_ratio'],    2),
                        'adx':         round(pending['adx'], 2) if pending.get('adx') else None,
                        'spot_at_entry': round(spot_price, 0),
                    }
                    pending = None

                    # Check exit on the same fill bar
                    result = check_exit(candle, open_trade['take_profit'], open_trade['stop_loss'])
                    if result:
                        outcome, exit_price = result
                        pct = (exit_price - open_trade['entry_price']) / open_trade['entry_price'] * 100
                        trades.append({
                            **open_trade,
                            'exit_time':   datetime.fromtimestamp(t),
                            'exit_price':  round(exit_price, 4),
                            'pct_return':  round(pct, 2),
                            'outcome':     outcome,
                            'bars_held':   0,
                        })
                        state = 'flat'
                        open_trade = trade_type = trade_strike = entry_bar = None

                elif pending_bars >= p.signal_expiry_bars:
                    state, pending, pending_bars = 'flat', None, 0

        # ── Flat: check ATM CE then ATM PE for a signal ───────────────────────
        if state == 'flat':
            for opt_type, by_strike, idx_map, available in [
                ('C', call_by_strike, call_idx, avail_calls),
                ('P', put_by_strike,  put_idx,  avail_puts),
            ]:
                atm = nearest_available(spot_price, available, strike_interval)
                if atm is None:
                    continue

                t2i, _ = idx_map.get(atm, ({}, {}))
                bar_idx = t2i.get(t)
                if bar_idx is None or bar_idx < p.required_candles:
                    continue

                candles = by_strike[atm]
                signal  = check_signal(candles, bar_idx, analyzer, p)
                if signal:
                    signal['strike']   = atm
                    signal['opt_type'] = opt_type
                    state        = 'pending'
                    pending      = signal
                    pending_bars = 0
                    break  # one position at a time; calls take priority

    # ── Close any open trade at end of data ───────────────────────────────────
    if state == 'in_trade' and open_trade:
        idx_map = call_idx if trade_type == 'C' else put_idx
        _, t2c  = idx_map.get(trade_strike, ({}, {}))
        # Use the last available candle for this strike
        if t2c:
            last_candle = list(t2c.values())[-1]
            exit_price  = float(last_candle['close'])
            pct = (exit_price - open_trade['entry_price']) / open_trade['entry_price'] * 100
            t2i, _ = idx_map.get(trade_strike, ({}, {}))
            last_bar = max(t2i.values()) if t2i else entry_bar
            trades.append({
                **open_trade,
                'exit_time':  datetime.fromtimestamp(last_candle['time']),
                'exit_price': round(exit_price, 4),
                'pct_return': round(pct, 2),
                'outcome':    'OPEN_AT_END',
                'bars_held':  last_bar - entry_bar,
            })

    return trades


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('spot_csv',  help='BTC spot/perp 1m CSV (for ATM determination)')
    parser.add_argument('data_dir',  help='Directory containing option CSVs')
    parser.add_argument('--expiry',   default='120626', help='Option expiry tag (default: 120626)')
    parser.add_argument('--interval', type=int, default=STRIKE_INTERVAL,
                        help=f'Strike spacing (default: {STRIKE_INTERVAL})')
    args = parser.parse_args()

    p = BacktestParams.from_env()
    logger.info(f"Params: {p.label()}")

    logger.info(f"Loading spot: {args.spot_csv}")
    spot_candles = load_csv(args.spot_csv)
    logger.info(f"  {len(spot_candles)} spot candles")

    call_by_strike, put_by_strike = load_option_strikes(
        args.data_dir, args.expiry, RESOLUTION)

    if not call_by_strike and not put_by_strike:
        logger.error("No option data found — check data_dir and --expiry")
        sys.exit(1)

    logger.info(f"Running ATM-following backtest (strike_interval={args.interval}) ...")
    trades = run_atm_backtest(
        spot_candles, call_by_strike, put_by_strike, p,
        strike_interval=args.interval,
    )

    label = f"ATM-Following (expiry={args.expiry}, interval={args.interval})"
    print_summary(trades, label, p)

    if trades:
        out_trades = os.path.join(args.data_dir, "atm_backtest_trades.csv")
        pd.DataFrame(trades).to_csv(out_trades, index=False)
        logger.info(f"Trade log → {out_trades}")

        chart_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'charts', 'btc_options_bb')
        os.makedirs(chart_dir, exist_ok=True)
        plot_equity_curve(trades, os.path.join(chart_dir, 'atm_backtest_equity.png'))


if __name__ == "__main__":
    main()
