# Delta Exchange Options Trading Bot

This repository contains a fully automated cryptocurrency options trading bot designed for Delta Exchange, along with a companion script for analyzing trading performance and P&L.

## Features

### 1. BTC Options Bollinger Bands Strategy (`strategies/btc_options_bb_strategy.py`)
A robust, automated trading bot that executes a Bollinger Bands reversal strategy on Bitcoin options.
- **Dynamic Strike Selection:** Monitors 5 option strikes (At-The-Money ± 2 strikes).
- **Signal Detection:** 
  - Detects bullish reversals from the lower Bollinger Band to buy CALL options.
  - Detects bearish reversals from the upper Bollinger Band to buy PUT options.
- **Risk Management:** Automatically places bracket orders with configurable Take Profit (e.g., 50%) and Stop Loss (e.g., 30%).
- **Position Tracking:** Complete trade management and concurrent position tracking.

### 2. P&L Analyzer (`pnl_analysis/analyse_pnl_delta_exchange.py`)
A statistical analysis tool to review your trading history.
- Parses Delta Exchange trading history CSV files.
- Calculates key metrics: Cumulative P&L, Total Trading Fees, Maximum Drawdown, Maximum Run-Up, Profit Factor, Win Rate, and Sharpe Ratio.
- Generates a visual equity curve plot (`equity_curve_with_fees_and_stats.png`) with an embedded statistics table.

---

## Prerequisites
- Python 3.8+
- A Delta Exchange account with API Keys.

---

## Setup & Execution (Local Windows)

1. **Clone the repository:**
   ```cmd
   git clone <your_repo_url>
   cd delta
   ```

2. **Create and activate a virtual environment:**
   ```cmd
   python -m venv venv
   venv\Scripts\activate
   ```

3. **Install dependencies:**
   ```cmd
   pip install -r requirements.txt
   ```

4. **Configuration:**
   Copy the example environment file and add your API keys.
   ```cmd
   copy env.example .env
   ```
   Open `.env` in a text editor and fill in your `DELTA_API_KEY`, `DELTA_API_SECRET`, and adjust any trading parameters.

5. **Run the Trading Bot:**
   ```cmd
   python strategies\btc_options_bb_strategy.py
   ```
   *(Note: Set `MOCK_MODE=True` in your `.env` file to simulate trades before risking real capital).*

6. **Run the P&L Analysis:**
   Export your trading history from Delta Exchange as a CSV, then run:
   ```cmd
   python pnl_analysis\analyse_pnl_delta_exchange.py path\to\your_data.csv
   ```

---

## Setup & Execution (Linux VPS)

Running on a Linux Virtual Private Server (VPS) ensures your bot runs 24/7 without interruption.

1. **Update system and install Python/Git:**
   ```bash
   sudo apt update && sudo apt upgrade -y
   sudo apt install python3 python3-venv python3-pip git tmux -y
   ```

2. **Clone the repository:**
   ```bash
   git clone <your_repo_url>
   cd delta
   ```

3. **Create and activate a virtual environment:**
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```

4. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

5. **Configuration:**
   ```bash
   cp env.example .env
   nano .env
   ```
   Fill in your `DELTA_API_KEY`, `DELTA_API_SECRET`, and strategy variables. Press `CTRL+X`, then `Y`, then `Enter` to save and exit.

6. **Run the Trading Bot continuously using `tmux`:**
   We use `tmux` so the bot continues to run after you disconnect from the SSH session.
   ```bash
   # Start a new tmux session
   tmux new -s tradingbot
   
   # Run the bot
   python strategies/btc_options_bb_strategy.py
   ```
   *To detach and leave the bot running in the background, press `CTRL+b` followed by `d`.*
   *To reattach to the session later, run `tmux attach -t tradingbot`.*

7. **Run the P&L Analysis:**
   ```bash
   python pnl_analysis/analyse_pnl_delta_exchange.py path/to/your_data.csv
   ```

## Disclaimer
This software is for educational purposes only. Cryptocurrency spot, futures & options trading carries a high level of risk, and may not be suitable for all investors. Do not trade with money you cannot afford to lose. Use at your own risk.
