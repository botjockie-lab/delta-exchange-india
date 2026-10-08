# Delta Exchange Options Trading Bot

Automated cryptocurrency options trading bot for Delta Exchange (India), with a full research pipeline: historical data fetching, per-symbol and ATM-following backtesting, and a grid-search parameter optimizer.

---

## Project Structure

```
strategies/
├── btc_options_bb/
│   ├── strategy.py          # Live trading bot
│   ├── fetch.py             # Historical candle data fetcher
│   ├── backtest.py          # Per-symbol backtester
│   ├── atm_backtest.py      # ATM-following backtester
│   └── optimizer.py         # Grid-search parameter optimizer
└── btc_liquidity_sweep/     # Research-only backtest port of a "liquidity sweep"
    ├── data_fetch.py        #   entry strategy (dhan_agent) to Delta 0-DTE BTC options
    ├── engine.py             # See its README section below.
    ├── costs.py
    ├── stats.py
    ├── run_backtest.py
    ├── optimizer.py
    ├── session_sweep.py
    └── rolling_session_sweep.py

data/
├── btc_options_bb/
│   ├── spot/                # BTC spot/perp candle CSVs
│   ├── options/{expiry}/    # Option candle CSVs per expiry
│   └── results/             # Timestamped optimizer output + trade logs
└── btc_liquidity_sweep/
    └── results/              # Timestamped sweep-result CSVs (committed).
                                # Raw fetched option/spot data and the product
                                # catalogue cache are gitignored -- ~800MB+ and
                                # fully reproducible via data_fetch.py.

charts/
└── btc_options_bb/          # Equity curve PNGs

pnl_analysis/
└── analyse_pnl_delta_exchange.py   # P&L analysis from Delta trade history
```

Each strategy lives in its own subdirectory under `strategies/` and owns its data under `data/`. Adding a new strategy means creating a parallel `strategies/new_strategy/` tree.

---

## `btc_liquidity_sweep` — research port, no live bot (yet)

A backtest-only port of a "liquidity sweep" options entry strategy (originally
built for NSE index options against a different broker) to Delta's BTC 0-DTE
(daily-expiry) options: a resting BUY limit at an ITM-N option's own day-low
(or a higher-timeframe bucket low), managed with SL/target/step-trailing exit,
reset on a timer and re-struck if it goes stale before filling. There is no
live trading component — see the module docstrings for the full mechanics.

**Data**: Delta serves real per-contract OHLCV directly by exact symbol via
`/v2/history/candles`, unauthenticated, even long after expiry — no
reconstruction trick needed (unlike the broker this was ported from).
`data_fetch.py` pulls each daily contract's full ~24h life (previous day's
settlement to its own settlement) for the last N days.

```bash
python strategies/btc_liquidity_sweep/data_fetch.py --days-back 365 --resolution 5m
python strategies/btc_liquidity_sweep/run_backtest.py --itm 0 --sl-pct-of-entry 0.9 --rr 3.0
python strategies/btc_liquidity_sweep/optimizer.py --workers 6          # SL/RR/itm/etc. grid sweep
python strategies/btc_liquidity_sweep/session_sweep.py --workers 6      # (start,end) session-window grid
python strategies/btc_liquidity_sweep/rolling_session_sweep.py --workers 6  # 6h windows across the full 24h life
```

**Findings (one year of BTC 0-DTE data, 2025-07 to 2026-07)**: across seven
staged sweeps (SL%, RR, ITM offset, restrike cadence, reset interval, htf
reference-low mode, and two rounds of session-window search), no
configuration was found that's both profitable *and* passes basic robustness
checks. One window (17:30–23:30 IST, right after a fresh contract starts
trading) crossed profit factor 1.0 with a whole region of SL/RR values
working, not just one lucky combo — but even the most moderate version of it
loses money once the top 5 winning trades are excluded, and is net negative
across the most recent 5 months of the dataset despite being profitable
overall. That pattern (strong-looking aggregate result, collapses under a
tail-dependency or out-of-sample check) matches what happened with the
Bollinger-Bands strategy on the same underlying (see `btc_options_bb`'s
history) — treat it as informative, not as a validated edge.

Full sweep-by-sweep detail (every parameter tested, every result, why each
dead end was ruled out) is in `optimizer.py`'s "Sweep history" comment block
and in the timestamped result CSVs under `data/btc_liquidity_sweep/results/`.

---

## Features

### 1. Live Trading Bot (`strategy.py`)
Fully automated bot that monitors BTC options on Delta Exchange and executes a Bollinger Bands reversal strategy.

- **ATM Strike Tracking:** Monitors At-The-Money strikes based on live BTC spot price.
- **Signal Detection:** Bullish reversals from lower BB → buy CALL; bearish reversals from upper BB → buy PUT.
- **Filters:** Optional ADX trend filter and EMA directional filter.
- **Risk Management:** Configurable Take Profit, Stop Loss, and minimum Reward:Risk ratio.
- **Position Tracking:** Bracket order management with concurrent position limits.

### 2. Historical Data Fetcher (`fetch.py`)
Fetches 1m (or any resolution) mark-price candle data from Delta Exchange for options and spot/perp symbols.

- Auto-routes output: option symbols → `data/btc_options_bb/options/{expiry}/`, spot/perp → `data/btc_options_bb/spot/`.
- Fetches in chunks to work around API limits; handles gaps and listing-date boundaries cleanly.

```bash
# Fetch BTC spot (9 days, 1m resolution)
RESOLUTION=1m LOOKBACK_DAYS=9 python strategies/btc_options_bb/fetch.py BTCUSD

# Fetch specific option strikes
RESOLUTION=1m LOOKBACK_DAYS=9 python strategies/btc_options_bb/fetch.py \
  C-BTC-63000-120626 P-BTC-63000-120626 \
  C-BTC-63200-120626 P-BTC-63200-120626
```

### 3. Per-Symbol Backtester (`backtest.py`)
Replays historical candle CSVs through the same signal logic as the live bot. Produces a per-trade log, summary statistics, and an equity curve chart.

```bash
python strategies/btc_options_bb/backtest.py \
  data/btc_options_bb/options/120626/C-BTC-63000-120626_1m.csv \
  data/btc_options_bb/options/120626/P-BTC-63000-120626_1m.csv
```

Override parameters via env vars:
```bash
BB_PERIOD=10 BB_STD_DEV=3.0 TAKE_PROFIT_PERCENT=30 STOP_LOSS_PERCENT=5 MIN_RR=2.0 \
  python strategies/btc_options_bb/backtest.py data/btc_options_bb/options/120626/*.csv
```

### 4. ATM-Following Backtester (`atm_backtest.py`)
Simulates the live strategy more accurately: at each bar the current ATM strike is determined from BTC spot price, and signal detection runs on that strike's own candle history. Trades stay on their entry strike until TP/SL even if ATM has since shifted.

```bash
python strategies/btc_options_bb/atm_backtest.py \
  data/btc_options_bb/spot/BTCUSD_1m.csv \
  data/btc_options_bb/options/120626 \
  --expiry 120626
```

### 5. Parameter Optimizer (`optimizer.py`)
Grid-search over `PARAM_GRID` defined at the top of the file. Supports two modes:

**ATM mode** (recommended — mirrors live strategy behaviour):
```bash
MIN_TRADES=8 python strategies/btc_options_bb/optimizer.py \
  --atm \
  --spot data/btc_options_bb/spot/BTCUSD_1m.csv \
  --data-dir data/btc_options_bb/options/120626 \
  --expiry 120626
```

**Per-symbol mode** (runs each file independently):
```bash
python strategies/btc_options_bb/optimizer.py \
  data/btc_options_bb/options/120626/C-BTC-63000-120626_1m.csv \
  data/btc_options_bb/options/120626/P-BTC-63000-120626_1m.csv
```

Results are saved as timestamped CSVs (`optimizer_results_YYYYMMDD_HHMMSS.csv`) in `data/btc_options_bb/results/` so runs are never overwritten. Sort by `calmar_ratio` or `profit_factor` to explore.

Env overrides: `SORT_BY`, `MIN_TRADES`, `TOP_N`, `WORKERS`, `STRIKE_INTERVAL`.

### 6. P&L Analyzer (`pnl_analysis/analyse_pnl_delta_exchange.py`)
Statistical analysis of your Delta Exchange trading history.

- Parses Delta Exchange trade history CSV exports.
- Computes: Cumulative P&L, fees, max drawdown, max run-up, profit factor, win rate, Sharpe ratio.
- Generates an equity curve with an embedded stats table.

```bash
python pnl_analysis/analyse_pnl_delta_exchange.py path/to/trade_history.csv
```

---

## Research Workflow

The recommended workflow for a new expiry:

```bash
# 1. Fetch BTC spot candles
RESOLUTION=1m LOOKBACK_DAYS=9 \
  python strategies/btc_options_bb/fetch.py BTCUSD

# 2. Determine the price range BTC traded during the period,
#    then fetch all ATM option strikes across that range (200-pt spacing)
RESOLUTION=1m LOOKBACK_DAYS=9 \
  python strategies/btc_options_bb/fetch.py \
    C-BTC-62000-XXXXXX P-BTC-62000-XXXXXX \
    C-BTC-62200-XXXXXX P-BTC-62200-XXXXXX \
    ...

# 3. Run the ATM-mode optimizer (edit PARAM_GRID in optimizer.py first)
MIN_TRADES=8 python strategies/btc_options_bb/optimizer.py \
  --atm \
  --spot  data/btc_options_bb/spot/BTCUSD_1m.csv \
  --data-dir data/btc_options_bb/options/XXXXXX \
  --expiry XXXXXX

# 4. Validate the best combo with the ATM backtester
BB_PERIOD=10 BB_STD_DEV=3.0 TAKE_PROFIT_PERCENT=30 STOP_LOSS_PERCENT=5 MIN_RR=2.0 \
  python strategies/btc_options_bb/atm_backtest.py \
    data/btc_options_bb/spot/BTCUSD_1m.csv \
    data/btc_options_bb/options/XXXXXX \
    --expiry XXXXXX

# 5. Update .env with confirmed params, then run the live bot
python strategies/btc_options_bb/strategy.py
```

---

## Current Best Parameters (120626 expiry, 9-day ATM grid search)

| Parameter | Value |
|---|---|
| `BB_PERIOD` | 10 |
| `BB_STD_DEV` | 3.0 |
| `TAKE_PROFIT_PERCENT` | 30 |
| `STOP_LOSS_PERCENT` | 5 |
| `MIN_RR` | 2.0 |
| `USE_ADX_FILTER` | False |
| `USE_EMA_FILTER` | False |

Backtest result (11 trades, 9 days): PF 3.43 · Calmar 5.36 · Max DD 18.55% · Win rate 36.4%

> **Note:** 11 trades over 9 days is a small sample. Validate across multiple expiries before trading live.

---

## Setup

### Prerequisites
- Python 3.8+
- Delta Exchange account with API keys

### Installation

```bash
git clone git@github.com:botjockie-lab/delta-exchange-india.git
cd delta

python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

pip install -r requirements.txt

cp env.example .env
# Edit .env: add DELTA_API_KEY, DELTA_API_SECRET, set TARGET_EXPIRY
```

### Key `.env` settings

| Variable | Description |
|---|---|
| `DELTA_API_KEY` / `DELTA_API_SECRET` | Your Delta Exchange API credentials |
| `TARGET_EXPIRY` | Option expiry in `DD-MM-YYYY` format |
| `MOCK_MODE` | `True` to paper-trade without placing real orders |
| `RESOLUTION` | Candle resolution for data fetch (`1m`, `5m`, `15m`, …) |
| `LOOKBACK_DAYS` | Days of history to fetch (default 30) |
| `BB_PERIOD` / `BB_STD_DEV` | Bollinger Band parameters |
| `TAKE_PROFIT_PERCENT` / `STOP_LOSS_PERCENT` | TP/SL as % of entry price |
| `MIN_RR` | Minimum reward:risk ratio to take a trade |
| `SIGNAL_EXPIRY_BARS` | Bars to wait for entry fill before cancelling |

### Running the live bot (Linux VPS)

```bash
# Start a persistent tmux session
tmux new -s tradingbot

source venv/bin/activate
python strategies/btc_options_bb/strategy.py

# Detach: Ctrl+b then d
# Reattach: tmux attach -t tradingbot
```

Set `MOCK_MODE=True` in `.env` to simulate trades before risking real capital.

---

## Disclaimer
This software is for educational purposes only. Cryptocurrency options trading carries substantial risk of loss. Do not trade with money you cannot afford to lose. Use at your own risk.
