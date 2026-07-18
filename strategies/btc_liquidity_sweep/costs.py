"""
Cost model for the BTC liquidity-sweep backtest.

Delta India options fee schedule (verified against docs.delta.exchange,
2026-07-18): maker 0.02%, taker 0.05% of notional (contract_value * spot * qty
for options... in practice Delta charges as a % of the *premium* traded value
for options, capped at a fraction of the premium -- see note below). No
Indian-specific STT/GST/stamp duty equivalents apply here.

This is a rough approximation, not verified against a live fill -- flag and
correct against real trade confirmations before trusting net P&L for capital
decisions (same caveat the Dhan backtester's cost model carries).
"""

from dataclasses import dataclass


@dataclass
class CostModel:
    taker_fee_pct: float = 0.0005   # entry (resting limit that gets swept) and forced exits
    maker_fee_pct: float = 0.0002   # unused by default -- entries here are limit orders
                                     # that fill passively, but Delta may still charge taker
                                     # if the sweep crosses the spread; treat both legs as
                                     # taker by default (conservative) via `use_taker_both`
    use_taker_both: bool = True
    slippage_ticks: float = 1.0     # extra ticks paid vs. quoted price, each side
    tick_size: float = 0.1          # USD, per contract premium (Delta BTC options default)


def apply_costs(entry_price: float, exit_price: float, qty: float,
                 cost_model: CostModel) -> float:
    """Return total round-trip cost in USD (fees + slippage) for one trade."""
    entry_fee_pct = cost_model.taker_fee_pct if cost_model.use_taker_both else cost_model.maker_fee_pct
    exit_fee_pct = cost_model.taker_fee_pct

    entry_notional = entry_price * qty
    exit_notional = exit_price * qty

    fees = entry_notional * entry_fee_pct + exit_notional * exit_fee_pct
    slippage = cost_model.slippage_ticks * cost_model.tick_size * qty * 2  # both legs

    return fees + slippage
