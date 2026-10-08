# Graph Report - delta  (2026-10-08)

## Corpus Check
- cluster-only mode — file stats not available

## Summary
- 209 nodes · 495 edges · 9 communities (5 shown, 4 thin omitted)
- Extraction: 86% EXTRACTED · 14% INFERRED · 0% AMBIGUOUS · INFERRED: 70 edges (avg confidence: 0.88)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `25478fc4`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- Community 0
- Community 1
- Community 2
- Community 3
- Community 4
- Community 5
- Community 6

## God Nodes (most connected - your core abstractions)
1. `DeltaExchangeAPI` - 16 edges
2. `OptionsStrategy` - 15 edges
3. `main()` - 13 edges
4. `main()` - 12 edges
5. `BacktestParams` - 11 edges
6. `BollingerBandsAnalyzer` - 11 edges
7. `run_atm_backtest()` - 11 edges
8. `main()` - 11 edges
9. `fetch_dte0_dataset()` - 11 edges
10. `run_backtest()` - 10 edges

## Surprising Connections (you probably didn't know these)
- `fetch_all_candles()` --uses--> `DeltaExchangeAPI`  [INFERRED]
  strategies/btc_options_bb/fetch.py → strategies/btc_options_bb/strategy.py
- `get_current_strikes()` --uses--> `DeltaExchangeAPI`  [INFERRED]
  strategies/btc_options_bb/fetch.py → strategies/btc_options_bb/strategy.py
- `main()` --uses--> `DeltaExchangeAPI`  [INFERRED]
  strategies/btc_options_bb/fetch.py → strategies/btc_options_bb/strategy.py
- `get_current_strikes()` --uses--> `OptionsStrategy`  [INFERRED]
  strategies/btc_options_bb/fetch.py → strategies/btc_options_bb/strategy.py
- `main()` --uses--> `CostModel`  [INFERRED]
  strategies/btc_liquidity_sweep/run_backtest.py → strategies/btc_liquidity_sweep/costs.py

## Import Cycles
- None detected.

## Communities (9 total, 4 thin omitted)

### Community 0 - "Community 0"
Cohesion: 0.10
Nodes (20): _build_indices(), get_atm_strike(), load_option_strikes(), main(), nearest_available(), run_atm_backtest(), BacktestParams, check_exit() (+12 more)

### Community 1 - "Community 1"
Cohesion: 0.08
Nodes (4): DeltaExchangeAPI, main(), OptionsStrategy, Position

### Community 2 - "Community 2"
Cohesion: 0.14
Nodes (14): build_combos(), _init_worker(), main(), build_windows(), _init_worker(), _ist_label(), main(), load_daily_frames() (+6 more)

### Community 3 - "Community 3"
Cohesion: 0.12
Nodes (18): apply_costs(), CostModel, BacktestConfig, compute_pnl(), compute_trailing_sl(), day_low_so_far(), htf_bucket_low(), resolve_itm_strike() (+10 more)

### Community 4 - "Community 4"
Cohesion: 0.12
Nodes (13): build_daily_expiry_index(), fetch_all_products(), fetch_dte0_dataset(), _fetch_one_symbol(), fetch_spot_history(), _get(), main(), spot_range_for_window() (+5 more)

## Knowledge Gaps
- **4 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `DeltaExchangeAPI` connect `Community 1` to `Community 4`, `Community 6`?**
  _High betweenness centrality (0.148) - this node is a cross-community bridge._
- **Are the 3 inferred relationships involving `DeltaExchangeAPI` (e.g. with `fetch_all_candles()` and `get_current_strikes()`) actually correct?**
  _`DeltaExchangeAPI` has 3 INFERRED edges - model-reasoned connections that need verification._
- **Should `Community 0` be split into smaller, more focused modules?**
  _Cohesion score 0.10253699788583509 - nodes in this community are weakly interconnected._
- **Why does `OptionsStrategy` connect `Community 1` to `Community 4`, `Community 5`, `Community 6`?**
  _High betweenness centrality (0.126) - this node is a cross-community bridge._
- **Should `Community 1` be split into smaller, more focused modules?**
  _Cohesion score 0.07692307692307693 - nodes in this community are weakly interconnected._
- **Why does `BollingerBandsAnalyzer` connect `Community 5` to `Community 0`, `Community 1`, `Community 6`?**
  _High betweenness centrality (0.108) - this node is a cross-community bridge._
- **Should `Community 2` be split into smaller, more focused modules?**
  _Cohesion score 0.14408602150537633 - nodes in this community are weakly interconnected._