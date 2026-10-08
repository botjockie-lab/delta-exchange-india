"""
Liquidity-sweep backtest engine for BTC 0-DTE options on Delta.

Ported from dhan_agent's liquidity-sweep + dhan-backtester skills: a resting
BUY limit at an ITM-N option contract's own day-low (or HTF low, see
resolve_reference_low), managed with a fixed SL/TP/step-trailing exit, reset
every `interval_hours` (cancel any still-pending order, re-strike off current
spot) and restruck every `restrike_minutes` if the resting order has gone
stale (latest candle already ran through where its SL/TP would sit without
ever filling).

One backtest "session" = one 0-DTE expiry day, 00:00-11:00 UTC (05:30-16:30
IST -- Delta settles daily BTC options at 17:30 IST/12:00 UTC; entries stop an
hour before that, matching the live trading-hours guidance this was built
against) -- there is no live daemon here, only the engine that replays
historical candles through the same cycle structure.
"""

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import pandas as pd

from costs import CostModel, apply_costs


@dataclass
class BacktestConfig:
    itm: int = 2                       # ITM-N offset, in strike intervals
    option_types: Tuple[str, ...] = ('C', 'P')
    interval_hours: float = 1.0        # unconditional reset cadence
    restrike_minutes: float = 5.0      # staleness check cadence
    sl_pct_of_entry: float = 0.50      # SL distance = 50% of entry price
    rr: float = 4.0                    # target = rr * sl_points
    qty: int = 1                       # contracts per leg (fixed-size backtest, no live sizing)
    contract_value: float = 0.001      # BTC per contract (Delta default for BTC options)
    session_start_hour: float = 0.0    # 00:00 UTC = 05:30 IST
    session_end_hour: float = 11.0     # 11:00 UTC = 16:30 IST (1h before 17:30 IST settlement)
    htf: str = 'DAY'                   # 'DAY' (running low so far) | '1H' | '4H'
    cost_model: CostModel = field(default_factory=CostModel)


def resolve_itm_strike(spot: float, strikes: List[int], interval: int,
                        itm: int, option_type: str) -> Optional[int]:
    """ATM = nearest listed strike to spot. PE ITM-N = ATM + N*interval (above
    spot), CE ITM-N = ATM - N*interval (below spot) -- same convention as the
    Dhan skills this is ported from."""
    if not strikes:
        return None
    atm = min(strikes, key=lambda k: abs(k - spot))
    target = atm + itm * interval if option_type == 'P' else atm - itm * interval
    candidate = min(strikes, key=lambda k: abs(k - target))
    return candidate


def day_low_so_far(candles: pd.DataFrame, session_start_ts: int, as_of_ts: int) -> Optional[float]:
    window = candles[(candles['time'] >= session_start_ts) & (candles['time'] <= as_of_ts)]
    if window.empty:
        return None
    return float(window['low'].min())


def htf_bucket_low(candles: pd.DataFrame, session_start_ts: int, as_of_ts: int,
                    bucket_hours: float) -> Optional[float]:
    """Low of the most recently *fully-closed* `bucket_hours` candle, buckets
    anchored at session start -- e.g. 4H buckets on an 11:00 UTC session start
    at 00:00 UTC: [00:00,04:00), [04:00,08:00), [08:00,12:00)... A bucket only
    counts once its end has passed `as_of_ts`; if none has closed yet (e.g.
    within the first 4h of the session for 4H), returns None -- same "no
    completed bucket yet" semantics as the live daemon's resolve_htf_low(),
    not a bug/gap to fill."""
    bucket_secs = int(bucket_hours * 3600)
    elapsed = as_of_ts - session_start_ts
    last_closed_k = (elapsed // bucket_secs) - 1
    if last_closed_k < 0:
        return None
    bucket_start = session_start_ts + last_closed_k * bucket_secs
    bucket_end = bucket_start + bucket_secs
    window = candles[(candles['time'] >= bucket_start) & (candles['time'] < bucket_end)]
    if window.empty:
        return None
    return float(window['low'].min())


HTF_BUCKET_HOURS = {'1H': 1.0, '2H': 2.0, '4H': 4.0, '6H': 6.0}


def resolve_reference_low(candles: pd.DataFrame, session_start_ts: int, as_of_ts: int,
                           htf: str) -> Optional[float]:
    if htf == 'DAY':
        return day_low_so_far(candles, session_start_ts, as_of_ts)
    hours = HTF_BUCKET_HOURS.get(htf)
    if hours is None:
        raise ValueError(f"Unsupported htf mode: {htf!r} (expected DAY or one of "
                         f"{sorted(HTF_BUCKET_HOURS)})")
    return htf_bucket_low(candles, session_start_ts, as_of_ts, hours)


def compute_trailing_sl(entry_price: float, sl_points: float, trailing_jump: float,
                          peak_price: float) -> float:
    """Step trailing stop: SL steps up by `trailing_jump` for every `trailing_jump`
    the peak has advanced past entry, floored at the original static SL."""
    static_sl = entry_price - sl_points
    if trailing_jump <= 0:
        return static_sl
    advance = max(0.0, peak_price - entry_price)
    steps = math.floor(advance / trailing_jump)
    trailed_sl = static_sl + steps * trailing_jump
    return max(static_sl, trailed_sl)


def run_session(day_df: pd.DataFrame, spot_df: pd.DataFrame, session_date_ts: int,
                 cfg: BacktestConfig) -> List[Dict]:
    """Replay one 0-DTE expiry day's liquidity-sweep cycle. Returns a list of
    trade dicts (filled and unfilled cycles both -- `filled: bool` distinguishes)."""
    session_start_ts = int(session_date_ts + cfg.session_start_hour * 3600)
    session_end_ts = int(session_date_ts + cfg.session_end_hour * 3600)

    strikes = sorted(day_df['strike'].unique())
    if len(strikes) < 2:
        return []
    interval = int(min(b - a for a, b in zip(strikes, strikes[1:])))

    candles_by_symbol: Dict[str, pd.DataFrame] = {
        sym: g.sort_values('time').reset_index(drop=True)
        for sym, g in day_df.groupby('symbol')
    }
    strike_symbol: Dict[Tuple[int, str], str] = {
        (int(row.strike), row.option_type): row.symbol
        for row in day_df[['strike', 'option_type', 'symbol']].drop_duplicates().itertuples()
    }

    spot_window = spot_df[(spot_df['time'] >= session_start_ts) & (spot_df['time'] < session_end_ts)]
    if spot_window.empty:
        return []

    reset_step = int(cfg.interval_hours * 3600)
    restrike_step = int(cfg.restrike_minutes * 60)

    trades: List[Dict] = []
    # per option_type: currently pending order (dict) or None; None once filled/closed
    pending: Dict[str, Optional[Dict]] = {t: None for t in cfg.option_types}
    open_positions: Dict[str, Optional[Dict]] = {t: None for t in cfg.option_types}

    def spot_at(ts: int) -> Optional[float]:
        row = spot_window[spot_window['time'] <= ts]
        if row.empty:
            return None
        return float(row.iloc[-1]['close'])

    def place_pending(option_type: str, ts: int) -> Optional[Dict]:
        spot = spot_at(ts)
        if spot is None:
            return None
        strike = resolve_itm_strike(spot, strikes, interval, cfg.itm, option_type)
        if strike is None:
            return None
        symbol = strike_symbol.get((strike, option_type))
        if symbol is None or symbol not in candles_by_symbol:
            return None
        candles = candles_by_symbol[symbol]
        low = resolve_reference_low(candles, session_start_ts, ts, cfg.htf)
        if low is None or low <= 0:
            return None
        sl_points = low * cfg.sl_pct_of_entry
        target_points = cfg.rr * sl_points
        trailing_jump = round(sl_points, 2)
        return {
            'option_type': option_type, 'symbol': symbol, 'strike': strike,
            'entry_price': low, 'sl_points': sl_points, 'target_points': target_points,
            'trailing_jump': trailing_jump,
            'stop_loss': low - sl_points, 'target': low + target_points,
            'placed_ts': ts, 'peak_price': low,
        }

    def candles_between(symbol: str, start_ts: int, end_ts: int) -> pd.DataFrame:
        c = candles_by_symbol.get(symbol)
        if c is None:
            return pd.DataFrame()
        return c[(c['time'] >= start_ts) & (c['time'] < end_ts)]

    def try_fill_and_manage(order: Dict, start_ts: int, end_ts: int) -> Optional[Dict]:
        """Check `order`'s pending fill and, if filled, manage it through exit --
        either within [start_ts, end_ts) or (if still open at end_ts) return an
        'open' position dict to keep managing in the next window."""
        bars = candles_between(order['symbol'], start_ts, end_ts)
        if bars.empty:
            return None

        filled = order.get('_filled', False)
        peak = order['peak_price']

        for row in bars.itertuples():
            if not filled:
                if row.low <= order['entry_price']:
                    filled = True
                    order['_filled'] = True
                    order['fill_ts'] = row.time
                    peak = order['entry_price']
                else:
                    continue

            peak = max(peak, row.high)
            order['peak_price'] = peak
            trail_sl = compute_trailing_sl(order['entry_price'], order['sl_points'],
                                            order['trailing_jump'], peak)
            order['stop_loss'] = trail_sl

            if row.low <= trail_sl:
                order['exit_ts'] = row.time
                order['exit_price'] = trail_sl
                order['exit_reason'] = 'TRAIL' if trail_sl > order['entry_price'] - order['sl_points'] else 'SL'
                return order
            if row.high >= order['target']:
                order['exit_ts'] = row.time
                order['exit_price'] = order['target']
                order['exit_reason'] = 'TARGET'
                return order

        order['_filled'] = filled
        order['peak_price'] = peak
        return None  # neither filled-and-exited nor unfilled-and-done -- still open/pending

    ts = session_start_ts
    last_restrike_check = {t: session_start_ts for t in cfg.option_types}

    while ts < session_end_ts:
        next_ts = min(ts + restrike_step, session_end_ts)

        for opt_type in cfg.option_types:
            if open_positions[opt_type] is not None:
                pos = open_positions[opt_type]
                result = try_fill_and_manage(pos, ts, next_ts)
                if result is not None and result.get('exit_reason'):
                    trades.append({**result, 'filled': True})
                    open_positions[opt_type] = None
                continue

            if pending[opt_type] is None:
                pending[opt_type] = place_pending(opt_type, ts)
                continue

            order = pending[opt_type]
            result = try_fill_and_manage(order, ts, next_ts)
            if result is not None and result.get('exit_reason'):
                trades.append({**result, 'filled': True})
                pending[opt_type] = None
            elif order.get('_filled'):
                open_positions[opt_type] = order
                pending[opt_type] = None
            else:
                # still unfilled -- check staleness against only the latest completed
                # candle (this cycle's window), matching the live daemon's spec:
                # "checks every still-PENDING order's own latest 5-min candle" --
                # NOT the cumulative range since placement (that fires on almost
                # every restrike since a resting day-low sits far below current
                # price by construction).
                if ts - last_restrike_check[opt_type] >= restrike_step - 1:
                    last_restrike_check[opt_type] = ts
                    bars = candles_between(order['symbol'], ts, next_ts)
                    if not bars.empty:
                        ran_through_sl = bars['low'].min() <= order['stop_loss']
                        ran_through_tp = bars['high'].max() >= order['target']
                        if ran_through_sl or ran_through_tp:
                            trades.append({**order, 'filled': False, 'exit_reason': 'RESTRUCK'})
                            pending[opt_type] = None

        # unconditional hourly reset: cancel any still-pending (not open) order
        if (ts - session_start_ts) % reset_step < restrike_step and ts > session_start_ts:
            for opt_type in cfg.option_types:
                if pending[opt_type] is not None:
                    trades.append({**pending[opt_type], 'filled': False, 'exit_reason': 'CANCELLED_RESET'})
                    pending[opt_type] = None

        ts = next_ts

    # session end: force-close open positions, drop unfilled pendings as EOD-unfilled
    for opt_type in cfg.option_types:
        pos = open_positions[opt_type]
        if pos is not None:
            last_bar = candles_by_symbol.get(pos['symbol'])
            close_price = pos['peak_price']
            if last_bar is not None and not last_bar.empty:
                tail = last_bar[last_bar['time'] < session_end_ts]
                if not tail.empty:
                    close_price = float(tail.iloc[-1]['close'])
            trades.append({**pos, 'filled': True, 'exit_ts': session_end_ts,
                            'exit_price': close_price, 'exit_reason': 'EOD_SQUAREOFF'})
        if pending[opt_type] is not None:
            trades.append({**pending[opt_type], 'filled': False, 'exit_reason': 'UNFILLED_EOD'})

    return trades


def compute_pnl(trade: Dict, cfg: BacktestConfig) -> float:
    if not trade.get('filled'):
        return 0.0
    entry = trade['entry_price']
    exitp = trade['exit_price']
    # entry/exit prices are already per-contract USD premium, so PnL is just
    # (exit - entry) * contract count -- contract_value (BTC/contract) doesn't
    # enter here, it would only matter for margin/notional-based costs.
    gross = (exitp - entry) * cfg.qty
    cost = apply_costs(entry, exitp, cfg.qty, cfg.cost_model)
    return gross - cost


def run_backtest(daily_frames: Dict[str, pd.DataFrame], spot_df: pd.DataFrame,
                  cfg: BacktestConfig) -> List[Dict]:
    """`daily_frames`: {YYYY-MM-DD: day_df} as produced by data_fetch.py's saved CSVs."""
    all_trades: List[Dict] = []
    for day_str, day_df in sorted(daily_frames.items()):
        import datetime as dt
        y, m, d = (int(x) for x in day_str.split('-'))
        session_date_ts = int(dt.datetime(y, m, d, tzinfo=dt.timezone.utc).timestamp())
        trades = run_session(day_df, spot_df, session_date_ts, cfg)
        for t in trades:
            t['day'] = day_str
            t['pnl'] = compute_pnl(t, cfg)
        all_trades.extend(trades)
    return all_trades
