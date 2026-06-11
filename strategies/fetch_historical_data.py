#!/usr/bin/env python3
"""
Fetch historical candle data for monitored option strikes.

Fetches data in weekly chunks going back up to 30 days (or as far as the
symbol existed) and saves each symbol to a CSV in the data/ directory.

Usage:
    python fetch_historical_data.py                      # auto-detect current ATM strikes
    python fetch_historical_data.py C-BTC-63000-120626   # specific symbols
    python fetch_historical_data.py C-BTC-63000-120626 P-BTC-63000-120626
"""

import os
import sys
import time
import logging
from datetime import datetime, timedelta

import pandas as pd
from dotenv import load_dotenv

# Allow importing from same directory
sys.path.insert(0, os.path.dirname(__file__))
from btc_options_bb_strategy import DeltaExchangeAPI, OptionsStrategy

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), '..', 'data')

RESOLUTION = os.getenv("RESOLUTION", "15m")
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "30"))

# Candles per API request (Delta caps at ~500; use 480 to be safe)
CHUNK_CANDLES = 480

RESOLUTION_MINUTES = {
    '1m': 1, '3m': 3, '5m': 5, '15m': 15,
    '30m': 30, '1h': 60, '2h': 120, '4h': 240,
    '6h': 360, '1d': 1440,
}


def resolution_to_minutes(res: str) -> int:
    minutes = RESOLUTION_MINUTES.get(res)
    if not minutes:
        raise ValueError(f"Unknown resolution '{res}'. Known: {list(RESOLUTION_MINUTES)}")
    return minutes


def fetch_all_candles(api: DeltaExchangeAPI, symbol: str, resolution: str, lookback_days: int) -> pd.DataFrame:
    """Fetch candles in weekly chunks and return a deduplicated DataFrame."""
    res_minutes = resolution_to_minutes(resolution)
    chunk_seconds = CHUNK_CANDLES * res_minutes * 60

    now = int(time.time())
    start = now - lookback_days * 86400

    all_candles = []
    chunk_end = now
    empty_streak = 0
    MAX_EMPTY_CHUNKS = 3  # stop once the option clearly wasn't listed yet

    while chunk_end > start:
        chunk_start = max(chunk_end - chunk_seconds, start)

        params = {
            'resolution': resolution,
            'symbol': f'MARK:{symbol}',
            'start': chunk_start,
            'end': chunk_end,
        }
        response = api.make_request('GET', '/history/candles', params=params)

        if not response.get('success') or not response.get('result'):
            empty_streak += 1
            if empty_streak >= MAX_EMPTY_CHUNKS:
                logger.info(f"  {symbol}: no data for {MAX_EMPTY_CHUNKS} consecutive chunks — reached listing date")
                break
            chunk_end = chunk_start
            continue

        empty_streak = 0

        candles = response['result']
        logger.info(f"  {symbol}: fetched {len(candles)} candles "
                    f"({datetime.fromtimestamp(chunk_start).strftime('%Y-%m-%d %H:%M')} → "
                    f"{datetime.fromtimestamp(chunk_end).strftime('%Y-%m-%d %H:%M')})")
        all_candles.extend(candles)

        # Step back by chunk size for the next iteration
        chunk_end = chunk_start
        time.sleep(0.3)  # be polite to the API

    if not all_candles:
        return pd.DataFrame()

    df = pd.DataFrame(all_candles)
    df['time'] = pd.to_numeric(df['time'])
    df = df.drop_duplicates(subset='time').sort_values('time').reset_index(drop=True)
    df['datetime'] = pd.to_datetime(df['time'], unit='s')

    cols = ['datetime', 'time', 'open', 'high', 'low', 'close', 'volume']
    cols = [c for c in cols if c in df.columns]
    return df[cols]


def get_current_strikes(api: DeltaExchangeAPI) -> list[str]:
    """Use OptionsStrategy logic to find the current ATM strikes."""
    api_key = os.getenv("DELTA_API_KEY")
    api_secret = os.getenv("DELTA_API_SECRET")
    target_expiry = os.getenv("TARGET_EXPIRY")

    strategy = OptionsStrategy(api_key=api_key, api_secret=api_secret, target_expiry=target_expiry)

    btc_response = api.get_btc_spot_price()
    if not btc_response.get('success'):
        logger.error("Failed to get BTC price")
        return []

    btc_price = float(btc_response['result']['mark_price'])
    logger.info(f"BTC Price: ${btc_price:,.2f}")

    chain_response = api.get_option_chain("BTC", strategy.target_expiry)
    if not chain_response.get('success'):
        logger.error("Failed to get option chain")
        return []

    monitored = strategy.get_target_strikes(btc_price, chain_response['result'])
    return [opt['symbol'] for opt in monitored]


def main():
    api_key = os.getenv("DELTA_API_KEY")
    api_secret = os.getenv("DELTA_API_SECRET")

    if not api_key or api_key == "your_api_key_here":
        logger.error("Set DELTA_API_KEY and DELTA_API_SECRET in .env")
        sys.exit(1)

    api = DeltaExchangeAPI(api_key, api_secret)
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    symbols = sys.argv[1:] if len(sys.argv) > 1 else get_current_strikes(api)

    if not symbols:
        logger.error("No symbols to fetch.")
        sys.exit(1)

    logger.info(f"Fetching {LOOKBACK_DAYS} days of {RESOLUTION} candles for: {symbols}")

    for symbol in symbols:
        logger.info(f"\nFetching {symbol}...")
        df = fetch_all_candles(api, symbol, RESOLUTION, LOOKBACK_DAYS)

        if df.empty:
            logger.warning(f"No data returned for {symbol}")
            continue

        out_path = os.path.join(OUTPUT_DIR, f"{symbol}_{RESOLUTION}.csv")
        df.to_csv(out_path, index=False)
        logger.info(f"Saved {len(df)} candles → {out_path}")
        logger.info(f"  Range: {df['datetime'].iloc[0]} → {df['datetime'].iloc[-1]}")


if __name__ == "__main__":
    main()
