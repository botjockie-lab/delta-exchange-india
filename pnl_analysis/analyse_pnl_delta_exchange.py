import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.gridspec import GridSpec
import argparse

def analyze_data(file_path):
    # Load data
    data = pd.read_csv(file_path)

    # Ensure required columns exist
    if 'Realised P&L' not in data.columns or 'Trading Fees' not in data.columns:
        print("Error: Required columns ('Realised P&L' and 'Trading Fees') not found in the dataset.")
        return
    
    # Starting capital
    starting_capital = 600
    
    # Calculate Cumulative P&L (Equity Curve)
    data['Cumulative PnL'] = data['Realised P&L'].cumsum() + starting_capital

    # Closing capital
    closing_capital = data['Cumulative PnL'].iloc[-1]

    # Calculate Total Trading Fees
    total_trading_fees = data['Trading Fees'].sum()

    # Calculate Maximum Drawdown
    cum_pnl = data['Cumulative PnL']
    drawdown = cum_pnl - cum_pnl.cummax()
    max_drawdown = drawdown.min()

    # Calculate Maximum Run-Up
    runup = cum_pnl - cum_pnl.cummin()
    max_runup = runup.max()

    # Calculate Profit Factor
    total_gains = data[data['Realised P&L'] > 0]['Realised P&L'].sum()
    total_losses = abs(data[data['Realised P&L'] < 0]['Realised P&L'].sum())
    profit_factor = total_gains / total_losses if total_losses != 0 else np.nan

    # Calculate Win Rate
    win_rate = len(data[data['Realised P&L'] > 0]) / len(data) * 100

    # Calculate Sharpe Ratio
    mean_pnl = data['Realised P&L'].mean()
    std_pnl = data['Realised P&L'].std()
    sharpe_ratio = mean_pnl / std_pnl if std_pnl != 0 else np.nan

    # Plot Equity Curve and Stats Table
    fig = plt.figure(figsize=(12, 7))
    gs = GridSpec(2, 1, height_ratios=[3, 1])

    # Plot Equity Curve
    ax1 = plt.subplot(gs[0])
    ax1.plot(data['Cumulative PnL'], label="Equity Curve", color="blue")
    ax1.set_xlabel("Trade Index")
    ax1.set_ylabel("Cumulative P&L")
    ax1.set_title("Equity Curve with Starting Capital")
    ax1.legend()
    ax1.grid()

    # Add Stats Table
    stats_data = [
        ["Starting Capital", f"{starting_capital:.2f}"],
        ["Closing Capital", f"{closing_capital:.2f}"],
        ["Total Trading Fees", f"{total_trading_fees:.2f}"],
        ["Maximum Drawdown", f"{max_drawdown:.2f}"],
        ["Maximum Run-Up", f"{max_runup:.2f}"],
        ["Profit Factor", f"{profit_factor:.2f}"],
        ["Win Rate (%)", f"{win_rate:.2f}"],
        ["Sharpe Ratio", f"{sharpe_ratio:.2f}"],
    ]
    column_labels = ["Metric", "Value"]

    ax2 = plt.subplot(gs[1])
    ax2.axis('tight')
    ax2.axis('off')
    table = ax2.table(cellText=stats_data, colLabels=column_labels, loc="center", cellLoc="center")
    table.auto_set_font_size(False)
    table.set_fontsize(10)
    table.auto_set_column_width([0, 1])

    plt.tight_layout()
    plt.savefig("equity_curve_with_fees_and_stats.png")
    plt.show()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Analyze trading data.")
    parser.add_argument("file_path", type=str, help="Path to the CSV file containing the trading data.")
    args = parser.parse_args()

    analyze_data(args.file_path)