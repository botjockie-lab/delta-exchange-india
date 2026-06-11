#!/usr/bin/env python3
"""
Backtester for BTC Options Bollinger Bands Strategy.

Replays historical candle CSVs through the same signal logic as
btc_options_bb_strategy.py and reports per-trade results + summary stats.

Entry mechanic: signal fires on bar i → stop-limit order placed at
candle_high * 1.01 → filled if any of the next MAX_PENDING_BARS candles
reaches that price → TP/SL tracked on each subsequent bar.

Usage:
    python backtest.py data/C-BTC-63000-120626_1m.csv
    python backtest.py data/C-BTC-63000-120626_1m.csv data/P-BTC-63000-120626_1m.csv
"""

import os
import sys
import logging
from datetime import datetime
from typing import List, Dict, Optional, Tuple

import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(__file__))
from btc_options_bb_strategy import BollingerBandsAnalyzer

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ── Strategy parameters (mirrors OptionsStrategy.__init__) ───────────────────
TAKE_PROFIT_PERCENT = float(os.getenv("TAKE_PROFIT_PERCENT", "10"))
STOP_LOSS_PERCENT   = float(os.getenv("STOP_LOSS_PERCENT",   "5"))
BB_PERIOD           = int(os.getenv("BB_PERIOD",             "20"))
BB_STD_DEV          = float(os.getenv("BB_STD_DEV",          "2.0"))
ADX_PERIOD          = int(os.getenv("ADX_PERIOD",            "14"))
ADX_THRESHOLD       = float(os.getenv("ADX_THRESHOLD",       "25"))
USE_ADX_FILTER      = os.getenv("USE_ADX_FILTER", "True").lower() == "true"
EMA_PERIOD          = int(os.getenv("EMA_PERIOD",            "200"))
USE_EMA_FILTER      = os.getenv("USE_EMA_FILTER", "True").lower() == "true"
MIN_OPTION_PRICE    = float(os.getenv("MIN_OPTION_PRICE",    "50"))
MIN_RR              = float(os.getenv("MIN_RR",              "1.5"))

# ── Backtest-only parameters ─────────────────────────────────────────────────
# Cancel pending entry order if not filled within this many bars
MAX_PENDING_BARS = int(os.getenv("MAX_PENDING_BARS", "5"))

# Minimum bars of history needed before the first signal can fire
_max_period = BB_PERIOD
if USE_ADX_FILTER:
    _max_period = max(_max_period, 2 * ADX_PERIOD + 1)
if USE_EMA_FILTER:
    _max_period = max(_max_period, EMA_PERIOD)
REQUIRED_CANDLES = _max_period + 20


# ── Data loading ─────────────────────────────────────────────────────────────

def load_csv(path: str) -> List[Dict]:
    df = pd.read_csv(path)
    missing = {'time', 'open', 'high', 'low', 'close'} - set(df.columns)
    if missing:
        raise ValueError(f"{path}: missing columns {missing}")
    df = df.sort_values('time').reset_index(drop=True)
    rows = []
    for _, r in df.iterrows():
        rows.append({
            'time':   int(r['time']),
            'open':   float(r['open']),
            'high':   float(r['high']),
            'low':    float(r['low']),
            'close':  float(r['close']),
            'volume': float(r.get('volume') or 0),
        })
    return rows


# ── Signal detection (mirrors analyze_option_strike, no API calls) ───────────

def check_signal(candles: List[Dict], analyzer: BollingerBandsAnalyzer) -> Optional[Dict]:
    """
    Run signal checks on `candles` (oldest→newest).
    `candles[-1]` is the analysis (most recently closed) bar.
    Returns a signal dict or None.
    """
    if len(candles) < REQUIRED_CANDLES:
        return None

    analysis_candle = candles[-1]
    current_price = float(analysis_candle['close'] or 0)

    if current_price < MIN_OPTION_PRICE:
        return None

    bb = analyzer.calculate_bollinger_bands(candles, period=BB_PERIOD, std_dev=BB_STD_DEV)
    if not bb:
        return None

    # ADX filter
    if USE_ADX_FILTER:
        adx = analyzer.calculate_adx(candles, period=ADX_PERIOD)
        if adx is None or adx < ADX_THRESHOLD:
            return None
    else:
        adx = None

    # EMA filter
    if USE_EMA_FILTER:
        ema = analyzer.calculate_ema(candles, period=EMA_PERIOD)
    else:
        ema = None

    # Bullish reversal from lower BB
    if not analyzer.is_bullish_reversal_candle(analysis_candle, bb['lower_band']):
        return None

    # EMA directional filter
    if USE_EMA_FILTER:
        if ema is None:
            return None
        if float(analysis_candle['close']) < ema:
            return None

    # Entry, TP, SL
    candle_high = float(analysis_candle['high'] or current_price)
    entry_price = candle_high * 1.01
    stop_loss   = entry_price * (1 - STOP_LOSS_PERCENT / 100)
    take_profit = entry_price * (1 + TAKE_PROFIT_PERCENT / 100)

    # RR check: reward measured to upper BB (same as live strategy)
    risk   = entry_price - stop_loss
    reward = bb['upper_band'] - entry_price
    rr_ratio = (reward / risk) if risk > 0 else 0.0

    if rr_ratio < MIN_RR:
        return None

    return {
        'entry_price':    entry_price,
        'take_profit':    take_profit,
        'stop_loss':      stop_loss,
        'upper_band':     bb['upper_band'],
        'lower_band':     bb['lower_band'],
        'middle_band':    bb['middle_band'],
        'adx':            adx,
        'ema':            ema,
        'rr_ratio':       rr_ratio,
        'signal_candle':  analysis_candle,
    }


# ── Trade execution simulation ───────────────────────────────────────────────

def try_fill(candle: Dict, entry_price: float) -> bool:
    """True if the stop-limit entry could have been filled on this candle."""
    return float(candle['high']) >= entry_price


def check_exit(candle: Dict, take_profit: float, stop_loss: float) -> Optional[Tuple[str, float]]:
    """
    Returns ('TP', price) | ('SL', price) | None.
    Gap-open logic handles overnight jumps.
    Same-bar TP+SL conflict → SL (conservative).
    """
    o = float(candle['open'])
    h = float(candle['high'])
    l = float(candle['low'])

    if o >= take_profit:          # gapped above TP
        return ('TP', take_profit)
    if o <= stop_loss:            # gapped below SL
        return ('SL', stop_loss)

    hit_tp = h >= take_profit
    hit_sl = l <= stop_loss

    if hit_tp and hit_sl:         # both hit intra-bar → conservative
        return ('SL', stop_loss)
    if hit_tp:
        return ('TP', take_profit)
    if hit_sl:
        return ('SL', stop_loss)
    return None


# ── Core backtest loop ───────────────────────────────────────────────────────

def run_backtest(symbol: str, candles: List[Dict]) -> List[Dict]:
    analyzer = BollingerBandsAnalyzer()
    trades: List[Dict] = []

    # state machine: 'flat' | 'pending' | 'in_trade'
    state         = 'flat'
    pending       = None   # signal dict
    pending_bars  = 0
    open_trade    = None   # dict with entry metadata
    entry_bar_idx = None

    for i in range(REQUIRED_CANDLES, len(candles)):
        candle = candles[i]

        # ── Check open trade for TP/SL ───────────────────────────────────
        if state == 'in_trade':
            result = check_exit(candle, open_trade['take_profit'], open_trade['stop_loss'])
            if result:
                outcome, exit_price = result
                bars_held = i - entry_bar_idx
                pct = (exit_price - open_trade['entry_price']) / open_trade['entry_price'] * 100
                trades.append({**open_trade,
                                'exit_time':  datetime.fromtimestamp(candle['time']),
                                'exit_price': round(exit_price, 4),
                                'pct_return': round(pct, 2),
                                'outcome':    outcome,
                                'bars_held':  bars_held})
                state, open_trade, entry_bar_idx = 'flat', None, None

        # ── Try to fill pending entry ────────────────────────────────────
        elif state == 'pending':
            pending_bars += 1
            if try_fill(candle, pending['entry_price']):
                state         = 'in_trade'
                entry_bar_idx = i
                open_trade = {
                    'symbol':       symbol,
                    'signal_time':  datetime.fromtimestamp(pending['signal_candle']['time']),
                    'entry_time':   datetime.fromtimestamp(candle['time']),
                    'entry_price':  round(pending['entry_price'], 4),
                    'take_profit':  round(pending['take_profit'], 4),
                    'stop_loss':    round(pending['stop_loss'],   4),
                    'upper_band':   round(pending['upper_band'],  4),
                    'lower_band':   round(pending['lower_band'],  4),
                    'rr_ratio':     round(pending['rr_ratio'],    2),
                    'adx':          round(pending['adx'], 2) if pending['adx'] else None,
                }
                pending = None

                # Immediately check if fill bar also hits TP/SL
                result = check_exit(candle, open_trade['take_profit'], open_trade['stop_loss'])
                if result:
                    outcome, exit_price = result
                    pct = (exit_price - open_trade['entry_price']) / open_trade['entry_price'] * 100
                    trades.append({**open_trade,
                                   'exit_time':  datetime.fromtimestamp(candle['time']),
                                   'exit_price': round(exit_price, 4),
                                   'pct_return': round(pct, 2),
                                   'outcome':    outcome,
                                   'bars_held':  0})
                    state, open_trade, entry_bar_idx = 'flat', None, None

            elif pending_bars >= MAX_PENDING_BARS:
                state, pending, pending_bars = 'flat', None, 0

        # ── Look for a new signal when flat ─────────────────────────────
        if state == 'flat':
            # Pass all candles up to and including bar i (bar i just closed)
            signal = check_signal(candles[:i + 1], analyzer)
            if signal:
                state        = 'pending'
                pending      = signal
                pending_bars = 0

    # Mark any position still open at end of data
    if state == 'in_trade' and open_trade:
        last = candles[-1]
        exit_price = float(last['close'])
        pct = (exit_price - open_trade['entry_price']) / open_trade['entry_price'] * 100
        trades.append({**open_trade,
                       'exit_time':  datetime.fromtimestamp(last['time']),
                       'exit_price': round(exit_price, 4),
                       'pct_return': round(pct, 2),
                       'outcome':    'OPEN_AT_END',
                       'bars_held':  len(candles) - 1 - entry_bar_idx})

    return trades


# ── Reporting ─────────────────────────────────────────────────────────────────

def print_summary(trades: List[Dict], label: str):
    if not trades:
        print(f"\n{label}: No trades generated.")
        return

    df = pd.DataFrame(trades)
    closed = df[df['outcome'].isin(['TP', 'SL'])]
    wins   = closed[closed['outcome'] == 'TP']
    losses = closed[closed['outcome'] == 'SL']

    total      = len(closed)
    win_rate   = len(wins) / total * 100 if total else 0
    avg_win    = wins['pct_return'].mean()   if len(wins)   else 0.0
    avg_loss   = losses['pct_return'].mean() if len(losses) else 0.0
    gross_profit = wins['pct_return'].sum()
    gross_loss   = abs(losses['pct_return'].sum())
    pf           = gross_profit / gross_loss if gross_loss > 0 else float('inf')

    # Compound equity (base 100), closed trades only
    equity = 100.0
    eq_curve = [equity]
    for r in closed['pct_return']:
        equity *= (1 + r / 100)
        eq_curve.append(equity)
    total_return = equity - 100

    # Max drawdown
    peak, max_dd = eq_curve[0], 0.0
    for v in eq_curve:
        peak  = max(peak, v)
        max_dd = max(max_dd, (peak - v) / peak * 100)

    # Max consecutive losses
    max_consec_losses, cur = 0, 0
    for o in closed['outcome']:
        if o == 'SL':
            cur += 1
            max_consec_losses = max(max_consec_losses, cur)
        else:
            cur = 0

    # Average bars held
    avg_bars = closed['bars_held'].mean() if 'bars_held' in closed.columns else 0

    period_start = df['signal_time'].min()
    period_end   = df['exit_time'].max()

    print(f"\n{'═'*58}")
    print(f"  Backtest Results — {label}")
    print(f"{'═'*58}")
    print(f"  Period:               {period_start} → {period_end}")
    print(f"  Total Trades:         {total}  (excl. open at end: {len(df) - len(closed)})")
    print(f"  Win Rate:             {win_rate:.1f}%  ({len(wins)}W / {len(losses)}L)")
    print(f"  Avg Win:              +{avg_win:.2f}%")
    print(f"  Avg Loss:             {avg_loss:.2f}%")
    print(f"  Profit Factor:        {pf:.2f}")
    print(f"  Total Return:         {total_return:+.2f}%")
    print(f"  Max Drawdown:         -{max_dd:.2f}%")
    print(f"  Max Consec. Losses:   {max_consec_losses}")
    print(f"  Avg Bars Held:        {avg_bars:.1f}")
    print(f"{'─'*58}")
    print(f"  BB({BB_PERIOD},{BB_STD_DEV})  TP:{TAKE_PROFIT_PERCENT}%  SL:{STOP_LOSS_PERCENT}%  MinRR:{MIN_RR}  PendBars:{MAX_PENDING_BARS}")
    if USE_ADX_FILTER:
        print(f"  ADX filter: >{ADX_THRESHOLD} (period {ADX_PERIOD})")
    else:
        print(f"  ADX filter: OFF")
    if USE_EMA_FILTER:
        print(f"  EMA filter: >{EMA_PERIOD}-period EMA")
    else:
        print(f"  EMA filter: OFF")
    print(f"{'═'*58}\n")

    # Per-trade table
    display_cols = ['symbol', 'signal_time', 'entry_time', 'exit_time',
                    'entry_price', 'exit_price', 'pct_return', 'outcome', 'bars_held', 'rr_ratio']
    display_cols = [c for c in display_cols if c in df.columns]
    print(df[display_cols].to_string(index=False))
    print()


def plot_equity_curve(all_trades: List[Dict], output_path: str):
    closed = [t for t in all_trades if t['outcome'] in ('TP', 'SL')]
    if not closed:
        return

    closed.sort(key=lambda t: t['entry_time'])
    equity, labels = [100.0], ['start']
    for t in closed:
        equity.append(equity[-1] * (1 + t['pct_return'] / 100))
        labels.append(f"{t['outcome']} #{len(labels)}")

    fig, ax = plt.subplots(figsize=(14, 5))
    xs = range(len(equity))
    ax.plot(xs, equity, color='steelblue', linewidth=1.5, zorder=3)
    ax.axhline(100, color='gray', linestyle='--', linewidth=0.8)
    ax.fill_between(xs, equity, 100,
                    where=[e >= 100 for e in equity], alpha=0.2, color='green', label='Profit')
    ax.fill_between(xs, equity, 100,
                    where=[e <  100 for e in equity], alpha=0.25, color='red', label='Loss')

    # Mark TP and SL points
    for idx, t in enumerate(closed, start=1):
        color = 'green' if t['outcome'] == 'TP' else 'red'
        ax.scatter(idx, equity[idx], color=color, s=25, zorder=4)

    peak = max(equity)
    peak_idx = equity.index(peak)
    ax.annotate(f"Peak {peak:.1f}", xy=(peak_idx, peak),
                xytext=(peak_idx + 1, peak + 1), fontsize=8, color='navy')

    ax.set_xlabel('Trade #')
    ax.set_ylabel('Equity (start = 100)')
    ax.set_title('Backtest Equity Curve')
    ax.legend(loc='upper left', fontsize=8)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)
    logger.info(f"Equity curve saved to {output_path}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    csv_paths  = sys.argv[1:]
    all_trades = []

    logger.info(f"Settings: BB({BB_PERIOD},{BB_STD_DEV}) TP:{TAKE_PROFIT_PERCENT}% SL:{STOP_LOSS_PERCENT}% "
                f"MinRR:{MIN_RR} ADX:{'ON >'+str(ADX_THRESHOLD) if USE_ADX_FILTER else 'OFF'} "
                f"EMA:{'ON >'+str(EMA_PERIOD) if USE_EMA_FILTER else 'OFF'} "
                f"RequiredCandles:{REQUIRED_CANDLES}")

    for path in csv_paths:
        if not os.path.exists(path):
            logger.error(f"File not found: {path}")
            continue

        symbol = os.path.splitext(os.path.basename(path))[0]
        logger.info(f"Loading {path} ...")
        candles = load_csv(path)
        logger.info(f"  {len(candles)} candles loaded for {symbol}")

        if len(candles) < REQUIRED_CANDLES + 10:
            logger.warning(f"  Not enough candles (need >{REQUIRED_CANDLES}), skipping")
            continue

        trades = run_backtest(symbol, candles)
        print_summary(trades, symbol)

        if trades:
            out_csv = os.path.join(os.path.dirname(os.path.abspath(path)),
                                   f"{symbol}_trades.csv")
            pd.DataFrame(trades).to_csv(out_csv, index=False)
            logger.info(f"Trade log → {out_csv}")

        all_trades.extend(trades)

    if all_trades:
        chart_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'charts')
        os.makedirs(chart_dir, exist_ok=True)
        plot_equity_curve(all_trades, os.path.join(chart_dir, 'backtest_equity.png'))

        if len(csv_paths) > 1:
            print_summary(all_trades, "COMBINED")


if __name__ == "__main__":
    main()
