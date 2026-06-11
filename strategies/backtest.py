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
from dataclasses import dataclass
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

# ── Module-level defaults (read from .env, used by BacktestParams.from_env()) ─
_TP_PCT          = float(os.getenv("TAKE_PROFIT_PERCENT", "10"))
_SL_PCT          = float(os.getenv("STOP_LOSS_PERCENT",   "5"))
_BB_PERIOD       = int(os.getenv("BB_PERIOD",             "20"))
_BB_STD_DEV      = float(os.getenv("BB_STD_DEV",          "2.0"))
_ADX_PERIOD      = int(os.getenv("ADX_PERIOD",            "14"))
_ADX_THRESHOLD   = float(os.getenv("ADX_THRESHOLD",       "25"))
_USE_ADX         = os.getenv("USE_ADX_FILTER", "True").lower() == "true"
_EMA_PERIOD      = int(os.getenv("EMA_PERIOD",            "200"))
_USE_EMA         = os.getenv("USE_EMA_FILTER", "True").lower() == "true"
_MIN_PRICE       = float(os.getenv("MIN_OPTION_PRICE",    "50"))
_MIN_RR          = float(os.getenv("MIN_RR",              "1.5"))
_SIGNAL_EXPIRY_BARS = int(os.getenv("SIGNAL_EXPIRY_BARS", "20"))


# ── Parameter container ───────────────────────────────────────────────────────

@dataclass
class BacktestParams:
    take_profit_pct: float  = _TP_PCT
    stop_loss_pct:   float  = _SL_PCT
    bb_period:       int    = _BB_PERIOD
    bb_std_dev:      float  = _BB_STD_DEV
    adx_period:      int    = _ADX_PERIOD
    adx_threshold:   float  = _ADX_THRESHOLD
    use_adx_filter:  bool   = _USE_ADX
    ema_period:      int    = _EMA_PERIOD
    use_ema_filter:  bool   = _USE_EMA
    min_option_price: float = _MIN_PRICE
    min_rr:          float  = _MIN_RR
    signal_expiry_bars: int = _SIGNAL_EXPIRY_BARS

    @property
    def required_candles(self) -> int:
        max_p = self.bb_period
        if self.use_adx_filter:
            max_p = max(max_p, 2 * self.adx_period + 1)
        if self.use_ema_filter:
            max_p = max(max_p, self.ema_period)
        return max_p + 20

    @classmethod
    def from_env(cls) -> 'BacktestParams':
        return cls()

    def label(self) -> str:
        adx = f"ADX>{self.adx_threshold}" if self.use_adx_filter else "ADX:off"
        ema = f"EMA{self.ema_period}" if self.use_ema_filter else "EMA:off"
        return (f"BB({self.bb_period},{self.bb_std_dev}) "
                f"TP:{self.take_profit_pct}% SL:{self.stop_loss_pct}% "
                f"MinRR:{self.min_rr} Expiry:{self.signal_expiry_bars}bars "
                f"{adx} {ema}")


# ── Data loading ──────────────────────────────────────────────────────────────

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


# ── Signal detection ──────────────────────────────────────────────────────────

def check_signal(candles: List[Dict], bar_idx: int,
                 analyzer: BollingerBandsAnalyzer,
                 p: BacktestParams) -> Optional[Dict]:
    """
    Check for a signal at `bar_idx` (the most recently closed bar).
    Uses targeted slices rather than candles[:bar_idx+1] to avoid O(n²) copies.
    """
    if bar_idx < p.required_candles:
        return None

    analysis_candle = candles[bar_idx]
    current_price   = float(analysis_candle['close'] or 0)

    if current_price < p.min_option_price:
        return None

    # BB uses only the last bb_period candles
    bb_candles = candles[bar_idx - p.bb_period + 1 : bar_idx + 1]
    bb = analyzer.calculate_bollinger_bands(bb_candles, period=p.bb_period, std_dev=p.bb_std_dev)
    if not bb:
        return None

    # ADX / EMA need more history — use required_candles window
    hist_start   = max(0, bar_idx - p.required_candles + 1)
    hist_candles = candles[hist_start : bar_idx + 1]

    adx = None
    if p.use_adx_filter:
        adx = analyzer.calculate_adx(hist_candles, period=p.adx_period)
        if adx is None or adx < p.adx_threshold:
            return None

    ema = None
    if p.use_ema_filter:
        ema = analyzer.calculate_ema(hist_candles, period=p.ema_period)

    if not analyzer.is_bullish_reversal_candle(analysis_candle, bb['lower_band']):
        return None

    if p.use_ema_filter:
        if ema is None or float(analysis_candle['close']) < ema:
            return None

    candle_high = float(analysis_candle['high'] or current_price)
    entry_price = candle_high * 1.01
    stop_loss   = entry_price * (1 - p.stop_loss_pct / 100)
    take_profit = entry_price * (1 + p.take_profit_pct / 100)

    risk     = entry_price - stop_loss
    reward   = bb['upper_band'] - entry_price
    rr_ratio = (reward / risk) if risk > 0 else 0.0

    if rr_ratio < p.min_rr:
        return None

    return {
        'entry_price':   entry_price,
        'take_profit':   take_profit,
        'stop_loss':     stop_loss,
        'upper_band':    bb['upper_band'],
        'lower_band':    bb['lower_band'],
        'middle_band':   bb['middle_band'],
        'adx':           adx,
        'ema':           ema,
        'rr_ratio':      rr_ratio,
        'signal_candle': analysis_candle,
    }


# ── Trade execution simulation ────────────────────────────────────────────────

def try_fill(candle: Dict, entry_price: float) -> bool:
    return float(candle['high']) >= entry_price


def check_exit(candle: Dict, take_profit: float,
               stop_loss: float) -> Optional[Tuple[str, float]]:
    """
    Returns ('TP', price) | ('SL', price) | None.
    Gap-open edges handled first; same-bar conflict → SL (conservative).
    """
    o, h, l = float(candle['open']), float(candle['high']), float(candle['low'])

    if o >= take_profit:
        return ('TP', take_profit)
    if o <= stop_loss:
        return ('SL', stop_loss)

    hit_tp = h >= take_profit
    hit_sl = l <= stop_loss

    if hit_tp and hit_sl:
        return ('SL', stop_loss)
    if hit_tp:
        return ('TP', take_profit)
    if hit_sl:
        return ('SL', stop_loss)
    return None


# ── Core backtest loop ────────────────────────────────────────────────────────

def run_backtest(symbol: str, candles: List[Dict],
                 p: BacktestParams) -> List[Dict]:
    analyzer = BollingerBandsAnalyzer()
    trades:   List[Dict] = []

    state         = 'flat'
    pending       = None
    pending_bars  = 0
    open_trade    = None
    entry_bar_idx = None

    for i in range(p.required_candles, len(candles)):
        candle = candles[i]

        if state == 'in_trade':
            result = check_exit(candle, open_trade['take_profit'], open_trade['stop_loss'])
            if result:
                outcome, exit_price = result
                pct = (exit_price - open_trade['entry_price']) / open_trade['entry_price'] * 100
                trades.append({**open_trade,
                                'exit_time':  datetime.fromtimestamp(candle['time']),
                                'exit_price': round(exit_price, 4),
                                'pct_return': round(pct, 2),
                                'outcome':    outcome,
                                'bars_held':  i - entry_bar_idx})
                state, open_trade, entry_bar_idx = 'flat', None, None

        elif state == 'pending':
            pending_bars += 1
            if try_fill(candle, pending['entry_price']):
                state         = 'in_trade'
                entry_bar_idx = i
                open_trade = {
                    'symbol':      symbol,
                    'signal_time': datetime.fromtimestamp(pending['signal_candle']['time']),
                    'entry_time':  datetime.fromtimestamp(candle['time']),
                    'entry_price': round(pending['entry_price'], 4),
                    'take_profit': round(pending['take_profit'], 4),
                    'stop_loss':   round(pending['stop_loss'],   4),
                    'upper_band':  round(pending['upper_band'],  4),
                    'lower_band':  round(pending['lower_band'],  4),
                    'rr_ratio':    round(pending['rr_ratio'],    2),
                    'adx':         round(pending['adx'], 2) if pending['adx'] else None,
                }
                pending = None

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

            elif pending_bars >= p.signal_expiry_bars:
                state, pending, pending_bars = 'flat', None, 0

        if state == 'flat':
            signal = check_signal(candles, i, analyzer, p)
            if signal:
                state        = 'pending'
                pending      = signal
                pending_bars = 0

    if state == 'in_trade' and open_trade:
        last       = candles[-1]
        exit_price = float(last['close'])
        pct        = (exit_price - open_trade['entry_price']) / open_trade['entry_price'] * 100
        trades.append({**open_trade,
                       'exit_time':  datetime.fromtimestamp(last['time']),
                       'exit_price': round(exit_price, 4),
                       'pct_return': round(pct, 2),
                       'outcome':    'OPEN_AT_END',
                       'bars_held':  len(candles) - 1 - entry_bar_idx})

    return trades


# ── Metrics helper (used by both backtest and optimizer) ──────────────────────

def compute_metrics(trades: List[Dict]) -> Dict:
    closed = [t for t in trades if t['outcome'] in ('TP', 'SL')]
    if not closed:
        return {'total_trades': 0, 'win_rate': 0, 'profit_factor': 0,
                'total_return_pct': 0, 'max_drawdown_pct': 0,
                'calmar_ratio': 0, 'max_consec_losses': 0, 'avg_bars_held': 0}

    wins   = [t for t in closed if t['outcome'] == 'TP']
    losses = [t for t in closed if t['outcome'] == 'SL']

    gross_profit = sum(t['pct_return'] for t in wins)
    gross_loss   = abs(sum(t['pct_return'] for t in losses))
    pf           = gross_profit / gross_loss if gross_loss > 0 else float('inf')

    equity, eq_curve = 100.0, [100.0]
    for t in closed:
        equity *= (1 + t['pct_return'] / 100)
        eq_curve.append(equity)
    total_return = equity - 100

    peak, max_dd = 100.0, 0.0
    for v in eq_curve:
        peak   = max(peak, v)
        max_dd = max(max_dd, (peak - v) / peak * 100)

    calmar = total_return / max_dd if max_dd > 0 else 0.0

    max_consec = cur = 0
    for t in closed:
        if t['outcome'] == 'SL':
            cur += 1
            max_consec = max(max_consec, cur)
        else:
            cur = 0

    avg_bars = sum(t.get('bars_held', 0) for t in closed) / len(closed)

    return {
        'total_trades':     len(closed),
        'win_rate':         round(len(wins) / len(closed) * 100, 1),
        'profit_factor':    round(pf, 3),
        'total_return_pct': round(total_return, 2),
        'max_drawdown_pct': round(max_dd, 2),
        'calmar_ratio':     round(calmar, 3),
        'max_consec_losses': max_consec,
        'avg_bars_held':    round(avg_bars, 1),
    }


# ── Reporting ─────────────────────────────────────────────────────────────────

def print_summary(trades: List[Dict], label: str, p: BacktestParams):
    m = compute_metrics(trades)
    closed_df = pd.DataFrame([t for t in trades if t['outcome'] in ('TP', 'SL')])
    all_df    = pd.DataFrame(trades)

    if m['total_trades'] == 0:
        print(f"\n{label}: No trades generated.")
        return

    wins   = len([t for t in trades if t['outcome'] == 'TP'])
    losses = len([t for t in trades if t['outcome'] == 'SL'])
    open_end = len(trades) - m['total_trades']

    period_start = all_df['signal_time'].min()
    period_end   = all_df['exit_time'].max()

    print(f"\n{'═'*58}")
    print(f"  Backtest Results — {label}")
    print(f"{'═'*58}")
    print(f"  Period:               {period_start} → {period_end}")
    print(f"  Total Trades:         {m['total_trades']}  (open at end: {open_end})")
    print(f"  Win Rate:             {m['win_rate']}%  ({wins}W / {losses}L)")
    print(f"  Avg Win:              +{closed_df[closed_df['outcome']=='TP']['pct_return'].mean():.2f}%" if wins else "  Avg Win:              N/A")
    print(f"  Avg Loss:             {closed_df[closed_df['outcome']=='SL']['pct_return'].mean():.2f}%" if losses else "  Avg Loss:             N/A")
    print(f"  Profit Factor:        {m['profit_factor']:.2f}")
    print(f"  Total Return:         {m['total_return_pct']:+.2f}%")
    print(f"  Max Drawdown:         -{m['max_drawdown_pct']:.2f}%")
    print(f"  Calmar Ratio:         {m['calmar_ratio']:.2f}")
    print(f"  Max Consec. Losses:   {m['max_consec_losses']}")
    print(f"  Avg Bars Held:        {m['avg_bars_held']}")
    print(f"{'─'*58}")
    print(f"  {p.label()}")
    print(f"{'═'*58}\n")

    display_cols = ['symbol', 'signal_time', 'entry_time', 'exit_time',
                    'entry_price', 'exit_price', 'pct_return', 'outcome', 'bars_held', 'rr_ratio']
    display_cols = [c for c in display_cols if c in all_df.columns]
    print(all_df[display_cols].to_string(index=False))
    print()


def plot_equity_curve(all_trades: List[Dict], output_path: str):
    closed = sorted([t for t in all_trades if t['outcome'] in ('TP', 'SL')],
                    key=lambda t: t['entry_time'])
    if not closed:
        return

    equity = [100.0]
    for t in closed:
        equity.append(equity[-1] * (1 + t['pct_return'] / 100))

    fig, ax = plt.subplots(figsize=(14, 5))
    xs = range(len(equity))
    ax.plot(xs, equity, color='steelblue', linewidth=1.5, zorder=3)
    ax.axhline(100, color='gray', linestyle='--', linewidth=0.8)
    ax.fill_between(xs, equity, 100,
                    where=[e >= 100 for e in equity], alpha=0.2, color='green', label='Profit')
    ax.fill_between(xs, equity, 100,
                    where=[e <  100 for e in equity], alpha=0.25, color='red', label='Loss')
    for idx, t in enumerate(closed, start=1):
        ax.scatter(idx, equity[idx], color='green' if t['outcome'] == 'TP' else 'red', s=25, zorder=4)

    peak     = max(equity)
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

    p          = BacktestParams.from_env()
    csv_paths  = sys.argv[1:]
    all_trades = []

    logger.info(f"Settings: {p.label()}  RequiredCandles:{p.required_candles}")

    for path in csv_paths:
        if not os.path.exists(path):
            logger.error(f"File not found: {path}")
            continue

        symbol  = os.path.splitext(os.path.basename(path))[0]
        logger.info(f"Loading {path} ...")
        candles = load_csv(path)
        logger.info(f"  {len(candles)} candles loaded for {symbol}")

        if len(candles) < p.required_candles + 10:
            logger.warning(f"  Not enough candles (need >{p.required_candles}), skipping")
            continue

        trades = run_backtest(symbol, candles, p)
        print_summary(trades, symbol, p)

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
            print_summary(all_trades, "COMBINED", p)


if __name__ == "__main__":
    main()
