# Graph Report - delta  (2026-10-14)

## Corpus Check
- 18 files · ~16,475 words
- Verdict: corpus is large enough that graph structure adds value.
- Unclassified: 5 file(s) not represented in the graph (top: (none) 4, .example 1)

## Summary
- 228 nodes · 513 edges · 10 communities (6 shown, 4 thin omitted)
- Extraction: 86% EXTRACTED · 14% INFERRED · 0% AMBIGUOUS · INFERRED: 70 edges (avg confidence: 0.88)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `a8e4b4e8`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- strategy.py
- DeltaExchangeAPI
- pandas
- engine.py
- data_fetch.py
- BollingerBandsAnalyzer
- analyse_pnl_delta_exchange.py
- Delta Exchange Options Trading Bot

## God Nodes (most connected - your core abstractions)
1. `DeltaExchangeAPI` - 16 edges
2. `OptionsStrategy` - 15 edges
3. `main()` - 13 edges
4. `main()` - 12 edges
5. `fetch_dte0_dataset()` - 11 edges
6. `run_atm_backtest()` - 11 edges
7. `BacktestParams` - 11 edges
8. `main()` - 11 edges
9. `BollingerBandsAnalyzer` - 11 edges
10. `run_backtest()` - 10 edges

## Surprising Connections (you probably didn't know these)
- `run_atm_backtest()` --uses--> `BollingerBandsAnalyzer`  [INFERRED]
  strategies/btc_options_bb/atm_backtest.py → strategies/btc_options_bb/strategy.py
- `check_signal()` --uses--> `BollingerBandsAnalyzer`  [INFERRED]
  strategies/btc_options_bb/backtest.py → strategies/btc_options_bb/strategy.py
- `run_backtest()` --uses--> `BollingerBandsAnalyzer`  [INFERRED]
  strategies/btc_options_bb/backtest.py → strategies/btc_options_bb/strategy.py
- `fetch_all_candles()` --uses--> `DeltaExchangeAPI`  [INFERRED]
  strategies/btc_options_bb/fetch.py → strategies/btc_options_bb/strategy.py
- `get_current_strikes()` --uses--> `DeltaExchangeAPI`  [INFERRED]
  strategies/btc_options_bb/fetch.py → strategies/btc_options_bb/strategy.py

## Import Cycles
- None detected.

## Communities (10 total, 4 thin omitted)

### Community 0 - "strategy.py"
Cohesion: 0.08
Nodes (20): _build_indices(), get_atm_strike(), load_option_strikes(), main(), nearest_available(), run_atm_backtest(), BacktestParams, check_exit() (+12 more)

### Community 1 - "DeltaExchangeAPI"
Cohesion: 0.08
Nodes (4): DeltaExchangeAPI, main(), OptionsStrategy, Position

### Community 2 - "pandas"
Cohesion: 0.12
Nodes (22): apply_costs(), CostModel, BacktestConfig, compute_pnl(), run_backtest(), build_combos(), _init_worker(), main() (+14 more)

### Community 3 - "engine.py"
Cohesion: 0.18
Nodes (10): compute_trailing_sl(), day_low_so_far(), htf_bucket_low(), resolve_itm_strike(), resolve_reference_low(), run_session(), candles_between(), place_pending() (+2 more)

### Community 4 - "data_fetch.py"
Cohesion: 0.12
Nodes (13): build_daily_expiry_index(), fetch_all_products(), fetch_dte0_dataset(), _fetch_one_symbol(), fetch_spot_history(), _get(), main(), spot_range_for_window() (+5 more)

### Community 9 - "Delta Exchange Options Trading Bot"
Cohesion: 0.11
Nodes (18): 1. Live Trading Bot (`strategy.py`), 2. Historical Data Fetcher (`fetch.py`), 3. Per-Symbol Backtester (`backtest.py`), 4. ATM-Following Backtester (`atm_backtest.py`), 5. Parameter Optimizer (`optimizer.py`), 6. P&L Analyzer (`pnl_analysis/analyse_pnl_delta_exchange.py`), `btc_liquidity_sweep` — research port, no live bot (yet), Current Best Parameters (120626 expiry, 9-day ATM grid search) (+10 more)

## Knowledge Gaps
- **15 isolated node(s):** `Project Structure`, ``btc_liquidity_sweep` — research port, no live bot (yet)`, `1. Live Trading Bot (`strategy.py`)`, `2. Historical Data Fetcher (`fetch.py`)`, `3. Per-Symbol Backtester (`backtest.py`)` (+10 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 94 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **4 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `DeltaExchangeAPI` connect `DeltaExchangeAPI` to `strategy.py`, `data_fetch.py`?**
  _High betweenness centrality (0.124) - this node is a cross-community bridge._
- **Are the 3 inferred relationships involving `DeltaExchangeAPI` (e.g. with `fetch_all_candles()` and `get_current_strikes()`) actually correct?**
  _`DeltaExchangeAPI` has 3 INFERRED edges - model-reasoned connections that need verification._
- **What connects `Project Structure`, ``btc_liquidity_sweep` — research port, no live bot (yet)`, `1. Live Trading Bot (`strategy.py`)` to the rest of the system?**
  _15 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `strategy.py` be split into smaller, more focused modules?**
  _Cohesion score 0.08345428156748912 - nodes in this community are weakly interconnected._
- **Why does `OptionsStrategy` connect `DeltaExchangeAPI` to `strategy.py`, `data_fetch.py`, `BollingerBandsAnalyzer`?**
  _High betweenness centrality (0.106) - this node is a cross-community bridge._
- **Should `DeltaExchangeAPI` be split into smaller, more focused modules?**
  _Cohesion score 0.07692307692307693 - nodes in this community are weakly interconnected._
- **Why does `BollingerBandsAnalyzer` connect `BollingerBandsAnalyzer` to `strategy.py`, `DeltaExchangeAPI`?**
  _High betweenness centrality (0.091) - this node is a cross-community bridge._