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
from strategy import BollingerBandsAnalyzer

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

# ── Exit-logic params (Phase 1) ───────────────────────────────────────────────
_TP_MODE         = os.getenv("TP_MODE", "fixed").lower()          # 'fixed' | 'upper_band'
_MAX_BARS        = int(os.getenv("MAX_BARS_IN_TRADE", "0"))       # 0 = no time stop
_TRAIL_ACT_PCT   = float(os.getenv("TRAIL_ACTIVATION_PCT", "0"))  # 0 = trailing off
_TRAIL_STOP_PCT  = float(os.getenv("TRAIL_STOP_PCT", "0"))        # 0 = trailing off

# ── Signal-filter params (Phase 2) ────────────────────────────────────────────
_REQ_STRONG_BODY    = os.getenv("REQUIRE_STRONG_BODY",   "False").lower() == "true"
_REQ_STRONG_BOUNCE  = os.getenv("REQUIRE_STRONG_BOUNCE", "False").lower() == "true"
_MIN_HOURS_TO_EXP   = float(os.getenv("MIN_HOURS_TO_EXPIRY", "0"))  # 0 = no DTE filter
_USE_SPOT_FILTER    = os.getenv("USE_SPOT_FILTER", "False").lower() == "true"
_SPOT_BB_PERIOD     = int(os.getenv("SPOT_BB_PERIOD", "20"))
_SPOT_BB_STD        = float(os.getenv("SPOT_BB_STD",   "2.0"))


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

    # Exit logic (Phase 1)
    tp_mode:             str   = _TP_MODE          # 'fixed' | 'upper_band'
    max_bars_in_trade:   int   = _MAX_BARS         # 0 = off
    trail_activation_pct: float = _TRAIL_ACT_PCT   # 0 = off
    trail_stop_pct:      float = _TRAIL_STOP_PCT   # 0 = off

    # Signal filters (Phase 2)
    require_strong_body:   bool  = _REQ_STRONG_BODY
    require_strong_bounce: bool  = _REQ_STRONG_BOUNCE
    min_hours_to_expiry:   float = _MIN_HOURS_TO_EXP   # 0 = off
    use_spot_filter:       bool  = _USE_SPOT_FILTER    # ATM/live only
    spot_bb_period:        int   = _SPOT_BB_PERIOD
    spot_bb_std:           float = _SPOT_BB_STD

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
        tp  = "TP:UpperBB" if self.tp_mode == "upper_band" else f"TP:{self.take_profit_pct}%"
        extras = []
        if self.max_bars_in_trade:
            extras.append(f"TimeStop:{self.max_bars_in_trade}b")
        if self.trail_stop_pct:
            extras.append(f"Trail:{self.trail_activation_pct}/{self.trail_stop_pct}%")
        if self.require_strong_body:
            extras.append("Body")
        if self.require_strong_bounce:
            extras.append("Bounce")
        if self.min_hours_to_expiry:
            extras.append(f"DTE>{self.min_hours_to_expiry}h")
        if self.use_spot_filter:
            extras.append(f"Spot({self.spot_bb_period},{self.spot_bb_std})")
        extra_str = (" " + " ".join(extras)) if extras else ""
        return (f"BB({self.bb_period},{self.bb_std_dev}) "
                f"{tp} SL:{self.stop_loss_pct}% "
                f"MinRR:{self.min_rr} Expiry:{self.signal_expiry_bars}bars "
                f"{adx} {ema}{extra_str}")


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


def expiry_to_ts(expiry_tag: str) -> Optional[int]:
    """
    Convert a Delta expiry tag 'DDMMYY' → unix timestamp of settlement.
    Delta BTC options settle at 12:00 UTC on the expiry date.
    Returns None if the tag can't be parsed.
    """
    from datetime import timezone
    if not expiry_tag or len(expiry_tag) != 6 or not expiry_tag.isdigit():
        return None
    try:
        dd, mm, yy = int(expiry_tag[:2]), int(expiry_tag[2:4]), 2000 + int(expiry_tag[4:6])
        return int(datetime(yy, mm, dd, 12, 0, tzinfo=timezone.utc).timestamp())
    except ValueError:
        return None


# ── Signal detection ──────────────────────────────────────────────────────────

def check_signal(candles: List[Dict], bar_idx: int,
                 analyzer: BollingerBandsAnalyzer,
                 p: BacktestParams,
                 expiry_ts: Optional[int] = None) -> Optional[Dict]:
    """
    Check for a signal at `bar_idx` (the most recently closed bar).
    Uses targeted slices rather than candles[:bar_idx+1] to avoid O(n²) copies.
    `expiry_ts` (unix seconds) enables the DTE filter when set.
    """
    if bar_idx < p.required_candles:
        return None

    analysis_candle = candles[bar_idx]
    current_price   = float(analysis_candle['close'] or 0)

    if current_price < p.min_option_price:
        return None

    # DTE filter: skip signals too close to expiry (theta bleed)
    if p.min_hours_to_expiry > 0 and expiry_ts is not None:
        hours_left = (expiry_ts - int(analysis_candle['time'])) / 3600.0
        if hours_left < p.min_hours_to_expiry:
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

    if not analyzer.is_bullish_reversal_candle(
            analysis_candle, bb['lower_band'],
            require_strong_body=p.require_strong_body,
            require_strong_bounce=p.require_strong_bounce):
        return None

    if p.use_ema_filter:
        if ema is None or float(analysis_candle['close']) < ema:
            return None

    candle_high = float(analysis_candle['high'] or current_price)
    entry_price = candle_high * 1.01
    stop_loss   = entry_price * (1 - p.stop_loss_pct / 100)
    if p.tp_mode == "upper_band":
        take_profit = bb['upper_band']
    else:
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


def manage_open_trade(candle: Dict, open_trade: Dict, p: BacktestParams,
                      bars_held: int) -> Optional[Tuple[str, float]]:
    """
    Advance one in-trade bar. Centralizes exit handling for both backtesters:
      1. Tighten the trailing stop from the peak reached in *prior* bars
         (avoids using the current bar's high to both set and trigger the stop).
      2. Check fixed TP / SL on this bar.
      3. Relabel a stop hit as 'TRAIL' when the stop has been ratcheted above
         its original level.
      4. Apply the time stop (exit at close after max_bars_in_trade).
      5. Update the running peak for the next bar.

    Mutates open_trade['stop_loss'] and open_trade['peak_price'].
    Returns (outcome, exit_price) | None.
    """
    high = float(candle['high'])

    # 1. Trailing stop — tighten using the peak from prior bars only.
    if p.trail_stop_pct > 0:
        activation = open_trade['entry_price'] * (1 + p.trail_activation_pct / 100)
        if open_trade['peak_price'] >= activation:
            trail = open_trade['peak_price'] * (1 - p.trail_stop_pct / 100)
            if trail > open_trade['stop_loss']:
                open_trade['stop_loss'] = trail

    # 2. TP / SL on this bar.
    result = check_exit(candle, open_trade['take_profit'], open_trade['stop_loss'])

    # 3. Relabel trailed-stop hits.
    if result and result[0] == 'SL' and open_trade['stop_loss'] > open_trade['orig_stop_loss']:
        result = ('TRAIL', result[1])

    # 4. Time stop (only if nothing else fired this bar).
    if result is None and p.max_bars_in_trade and bars_held >= p.max_bars_in_trade:
        result = ('TIME', float(candle['close']))

    # 5. Advance the running peak for the next bar.
    if high > open_trade['peak_price']:
        open_trade['peak_price'] = high

    return result


# ── Trade recording ───────────────────────────────────────────────────────────

_INTERNAL_KEYS = ('peak_price', 'orig_stop_loss')


def build_open_trade(symbol: str, pending: Dict, entry_time: int) -> Dict:
    """Construct the open-trade dict at fill, including internal trailing state."""
    return {
        'symbol':      symbol,
        'signal_time': datetime.fromtimestamp(pending['signal_candle']['time']),
        'entry_time':  datetime.fromtimestamp(entry_time),
        'entry_price': round(pending['entry_price'], 4),
        'take_profit': round(pending['take_profit'], 4),
        'stop_loss':   round(pending['stop_loss'],   4),
        'upper_band':  round(pending['upper_band'],  4),
        'lower_band':  round(pending['lower_band'],  4),
        'rr_ratio':    round(pending['rr_ratio'],    2),
        'adx':         round(pending['adx'], 2) if pending.get('adx') else None,
        # internal trailing-stop state (stripped from the trade record)
        'peak_price':     pending['entry_price'],
        'orig_stop_loss': round(pending['stop_loss'], 4),
    }


def record_trade(trades: List[Dict], open_trade: Dict, exit_ts: int,
                 exit_price: float, outcome: str, bars_held: int,
                 extra: Optional[Dict] = None) -> None:
    """Append a finalized trade, stripping internal-only tracking keys."""
    rec = {k: v for k, v in open_trade.items() if k not in _INTERNAL_KEYS}
    if extra:
        rec.update(extra)
    pct = (exit_price - open_trade['entry_price']) / open_trade['entry_price'] * 100
    rec.update({
        'exit_time':  datetime.fromtimestamp(exit_ts),
        'exit_price': round(exit_price, 4),
        'pct_return': round(pct, 2),
        'outcome':    outcome,
        'bars_held':  bars_held,
    })
    trades.append(rec)


# ── Core backtest loop ────────────────────────────────────────────────────────

def run_backtest(symbol: str, candles: List[Dict],
                 p: BacktestParams) -> List[Dict]:
    analyzer = BollingerBandsAnalyzer()
    trades:   List[Dict] = []

    # DTE filter needs the expiry timestamp; derive it from the symbol tag.
    parts     = symbol.split('-')
    expiry_ts = expiry_to_ts(parts[3]) if len(parts) == 4 else None

    state         = 'flat'
    pending       = None
    pending_bars  = 0
    open_trade    = None
    entry_bar_idx = None

    for i in range(p.required_candles, len(candles)):
        candle = candles[i]

        if state == 'in_trade':
            result = manage_open_trade(candle, open_trade, p, i - entry_bar_idx)
            if result:
                outcome, exit_price = result
                record_trade(trades, open_trade, candle['time'], exit_price,
                             outcome, i - entry_bar_idx)
                state, open_trade, entry_bar_idx = 'flat', None, None

        elif state == 'pending':
            pending_bars += 1
            if try_fill(candle, pending['entry_price']):
                state         = 'in_trade'
                entry_bar_idx = i
                open_trade    = build_open_trade(symbol, pending, candle['time'])
                pending       = None

                result = manage_open_trade(candle, open_trade, p, 0)
                if result:
                    outcome, exit_price = result
                    record_trade(trades, open_trade, candle['time'], exit_price,
                                 outcome, 0)
                    state, open_trade, entry_bar_idx = 'flat', None, None

            elif pending_bars >= p.signal_expiry_bars:
                state, pending, pending_bars = 'flat', None, 0

        if state == 'flat':
            signal = check_signal(candles, i, analyzer, p, expiry_ts=expiry_ts)
            if signal:
                state        = 'pending'
                pending      = signal
                pending_bars = 0

    if state == 'in_trade' and open_trade:
        last = candles[-1]
        record_trade(trades, open_trade, last['time'], float(last['close']),
                     'OPEN_AT_END', len(candles) - 1 - entry_bar_idx)

    return trades


# ── Metrics helper (used by both backtest and optimizer) ──────────────────────

CLOSED_OUTCOMES = ('TP', 'SL', 'TIME', 'TRAIL')


def compute_metrics(trades: List[Dict]) -> Dict:
    closed = [t for t in trades if t['outcome'] in CLOSED_OUTCOMES]
    if not closed:
        return {'total_trades': 0, 'win_rate': 0, 'profit_factor': 0,
                'total_return_pct': 0, 'max_drawdown_pct': 0,
                'calmar_ratio': 0, 'max_consec_losses': 0, 'avg_bars_held': 0}

    # Classify by realized return so TIME/TRAIL exits land on the right side.
    wins   = [t for t in closed if t['pct_return'] > 0]
    losses = [t for t in closed if t['pct_return'] <= 0]

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
        if t['pct_return'] < 0:
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
    closed_df = pd.DataFrame([t for t in trades if t['outcome'] in CLOSED_OUTCOMES])
    all_df    = pd.DataFrame(trades)

    if m['total_trades'] == 0:
        print(f"\n{label}: No trades generated.")
        return

    wins   = len(closed_df[closed_df['pct_return'] > 0])
    losses = len(closed_df[closed_df['pct_return'] <= 0])
    open_end = len(trades) - m['total_trades']

    # Outcome breakdown (TP / SL / TIME / TRAIL counts)
    outcome_counts = closed_df['outcome'].value_counts().to_dict()
    outcome_str = ", ".join(f"{k}:{v}" for k, v in sorted(outcome_counts.items()))

    period_start = all_df['signal_time'].min()
    period_end   = all_df['exit_time'].max()

    print(f"\n{'═'*58}")
    print(f"  Backtest Results — {label}")
    print(f"{'═'*58}")
    print(f"  Period:               {period_start} → {period_end}")
    print(f"  Total Trades:         {m['total_trades']}  (open at end: {open_end})")
    print(f"  Win Rate:             {m['win_rate']}%  ({wins}W / {losses}L)")
    print(f"  Exit Breakdown:       {outcome_str}")
    print(f"  Avg Win:              +{closed_df[closed_df['pct_return']>0]['pct_return'].mean():.2f}%" if wins else "  Avg Win:              N/A")
    print(f"  Avg Loss:             {closed_df[closed_df['pct_return']<=0]['pct_return'].mean():.2f}%" if losses else "  Avg Loss:             N/A")
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
    closed = sorted([t for t in all_trades if t['outcome'] in CLOSED_OUTCOMES],
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
        ax.scatter(idx, equity[idx], color='green' if t['pct_return'] > 0 else 'red', s=25, zorder=4)

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
        chart_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'charts', 'btc_options_bb')
        os.makedirs(chart_dir, exist_ok=True)
        plot_equity_curve(all_trades, os.path.join(chart_dir, 'backtest_equity.png'))

        if len(csv_paths) > 1:
            print_summary(all_trades, "COMBINED", p)


if __name__ == "__main__":
    main()
