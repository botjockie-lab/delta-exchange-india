"""Summary stats + equity curve for the liquidity-sweep backtest."""

from typing import Dict, List

import pandas as pd


def build_equity_curve(trades: List[Dict], starting_capital: float) -> pd.DataFrame:
    filled = [t for t in trades if t.get('filled')]
    filled.sort(key=lambda t: t['exit_ts'])
    rows = []
    equity = starting_capital
    for t in filled:
        equity += t['pnl']
        rows.append({'exit_ts': t['exit_ts'], 'day': t['day'], 'symbol': t['symbol'],
                     'option_type': t['option_type'], 'pnl': t['pnl'], 'equity': equity,
                     'exit_reason': t['exit_reason']})
    return pd.DataFrame(rows)


def summarize(trades: List[Dict], starting_capital: float) -> Dict:
    filled = [t for t in trades if t.get('filled')]
    n = len(filled)
    if n == 0:
        return {'total_trades': 0, 'filled_trades': 0, 'total_cycles': len(trades)}

    wins = [t for t in filled if t['pnl'] > 0]
    losses = [t for t in filled if t['pnl'] <= 0]
    gross_profit = sum(t['pnl'] for t in wins)
    gross_loss = -sum(t['pnl'] for t in losses)
    total_pnl = sum(t['pnl'] for t in filled)

    equity_df = build_equity_curve(trades, starting_capital)
    peak = equity_df['equity'].cummax()
    dd = equity_df['equity'] - peak
    max_dd = float(dd.min()) if not dd.empty else 0.0
    peak_at_dd = float(peak[dd.idxmin()]) if not dd.empty and dd.min() < 0 else starting_capital

    reason_counts = pd.Series([t['exit_reason'] for t in trades]).value_counts().to_dict()

    return {
        'total_cycles': len(trades),
        'filled_trades': n,
        'wins': len(wins),
        'losses': len(losses),
        'win_rate': round(100 * len(wins) / n, 2) if n else 0,
        'profit_factor': round(gross_profit / gross_loss, 3) if gross_loss > 0 else float('inf'),
        'total_pnl': round(total_pnl, 2),
        'total_return_pct': round(100 * total_pnl / starting_capital, 2),
        'max_drawdown': round(max_dd, 2),
        'max_drawdown_pct': round(100 * max_dd / peak_at_dd, 2) if peak_at_dd else 0,
        'avg_win': round(gross_profit / len(wins), 2) if wins else 0,
        'avg_loss': round(-gross_loss / len(losses), 2) if losses else 0,
        'exit_reason_breakdown': reason_counts,
    }
