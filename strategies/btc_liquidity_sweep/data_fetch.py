#!/usr/bin/env python3
"""
Historical data fetcher for the BTC liquidity-sweep strategy.

Delta lists a new daily-expiry BTC option series roughly 2 days before each
settlement (12:00 UTC = 17:30 IST). "0 DTE" here means the contract's full
life as the *front* daily expiry: from the previous day's 12:00 UTC settlement
(when it becomes the newest 0-DTE-eligible contract) to its own 12:00 UTC
settlement -- a full 24h window, keyed by CSV filename to the settlement date.
Unlike Dhan's `expired_options_data`, Delta serves real per-contract OHLCV
directly by exact symbol (verified live, see project memory), even long after
expiry, and unauthenticated -- no ATM-relative reconstruction needed.

Usage:
    python data_fetch.py --days-back 365 --itm-buffer 6 --resolution 5m
"""

import os
import sys
import json
import time
import logging
import argparse
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple

import requests
import pandas as pd

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

BASE_URL = "https://api.india.delta.exchange"
DATA_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'btc_liquidity_sweep')
PRODUCTS_CACHE = os.path.join(DATA_DIR, 'products_cache_BTC.json')

SESSION = requests.Session()


def _get(path: str, params: Dict, retries: int = 4, timeout: int = 20) -> Dict:
    """GET against Delta's public (unauthenticated) API with retry/backoff."""
    for attempt in range(retries):
        try:
            resp = SESSION.get(f"{BASE_URL}{path}", params=params, timeout=timeout)
            if resp.status_code == 200:
                return resp.json()
            logger.warning(f"HTTP {resp.status_code} for {path} {params} (attempt {attempt+1})")
        except requests.RequestException as e:
            logger.warning(f"Request error for {path}: {e} (attempt {attempt+1})")
        time.sleep(min(2 ** attempt, 8))
    return {"success": False, "result": []}


# ── Product catalogue (strikes/symbols/settlement per expiry day) ─────────────

def fetch_all_products(underlying: str = "BTC", states: str = "expired,live",
                        use_cache: bool = True) -> List[Dict]:
    """Paginate /v2/products for all call/put options on `underlying`.

    Cached to disk since this is a ~50k-row, ~16-page pull that doesn't need
    repeating every run — delete the cache file to force a refresh (e.g. to
    pick up newly-expired contracts).
    """
    if use_cache and os.path.exists(PRODUCTS_CACHE):
        with open(PRODUCTS_CACHE) as f:
            cached = json.load(f)
        logger.info(f"Loaded {len(cached)} cached products from {PRODUCTS_CACHE}")
        return cached

    all_products: List[Dict] = []
    after = None
    page = 0
    while True:
        params = {
            "states": states,
            "contract_types": "call_options,put_options",
            "underlying_asset_symbols": underlying,
        }
        if after:
            params["after"] = after
        d = _get("/v2/products", params)
        res = d.get("result", [])
        all_products.extend(res)
        page += 1
        meta = d.get("meta", {})
        after = meta.get("after")
        logger.info(f"  products page {page}: +{len(res)} (total {len(all_products)})")
        if not after or not res:
            break

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(PRODUCTS_CACHE, 'w') as f:
        json.dump(all_products, f)
    logger.info(f"Cached {len(all_products)} products to {PRODUCTS_CACHE}")
    return all_products


def build_daily_expiry_index(products: List[Dict]) -> Dict[str, Dict]:
    """Group products by expiry date (YYYY-MM-DD) -> {strike: {'C': product, 'P': product}}."""
    index: Dict[str, Dict] = {}
    for p in products:
        settlement = p.get('settlement_time')
        symbol = p.get('symbol', '')
        if not settlement or not symbol:
            continue
        parts = symbol.split('-')
        if len(parts) != 4 or parts[1] != 'BTC':
            continue
        opt_type, _, strike_s, _ = parts
        if opt_type not in ('C', 'P'):
            continue
        try:
            strike = int(strike_s)
        except ValueError:
            continue
        day = settlement[:10]  # YYYY-MM-DD
        index.setdefault(day, {}).setdefault(strike, {})[opt_type] = p
    return index


# ── Spot history (continuous, once for the whole span) ────────────────────────

def fetch_spot_history(start_ts: int, end_ts: int, resolution: str = '5m',
                        symbol: str = 'BTCUSD') -> pd.DataFrame:
    """Chunked spot/perp candle fetch — mirrors btc_options_bb/fetch.py's chunking."""
    seconds_per_candle = {'1m': 60, '5m': 300, '15m': 900, '1h': 3600}[resolution]
    max_candles_per_call = 2000
    chunk_span = seconds_per_candle * max_candles_per_call

    all_rows = []
    cur_end = end_ts
    while cur_end > start_ts:
        cur_start = max(start_ts, cur_end - chunk_span)
        d = _get("/v2/history/candles", {
            "resolution": resolution, "symbol": symbol,
            "start": cur_start, "end": cur_end,
        })
        rows = d.get("result", [])
        all_rows.extend(rows)
        cur_end = cur_start
        time.sleep(0.05)

    if not all_rows:
        return pd.DataFrame(columns=['time', 'open', 'high', 'low', 'close', 'volume'])
    df = pd.DataFrame(all_rows).drop_duplicates(subset='time').sort_values('time').reset_index(drop=True)
    df['datetime'] = pd.to_datetime(df['time'], unit='s', utc=True)
    return df


# ── Per-expiry-day option fetch ────────────────────────────────────────────────

def _fetch_one_symbol(symbol: str, start_ts: int, end_ts: int, resolution: str) -> Optional[pd.DataFrame]:
    d = _get("/v2/history/candles", {
        "resolution": resolution, "symbol": symbol, "start": start_ts, "end": end_ts,
    })
    rows = d.get("result", [])
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df['symbol'] = symbol
    return df


def spot_range_for_window(spot_df: pd.DataFrame, start_ts: int, end_ts: int) -> Optional[Tuple[float, float]]:
    window = spot_df[(spot_df['time'] >= start_ts) & (spot_df['time'] <= end_ts)]
    if window.empty:
        return None
    return float(window['low'].min()), float(window['high'].max())


def strikes_in_band(day_strikes: Dict[int, Dict], spot_lo: float, spot_hi: float,
                     interval: int, buffer_strikes: int) -> List[int]:
    lo = int(spot_lo // interval) * interval - buffer_strikes * interval
    hi = (int(spot_hi // interval) + 1) * interval + buffer_strikes * interval
    return sorted(k for k in day_strikes if lo <= k <= hi)


def fetch_dte0_dataset(days_back: int = 365, itm_buffer_strikes: int = 6,
                        resolution: str = '5m', max_workers: int = 12,
                        out_dir: Optional[str] = None) -> None:
    """Fetch a full 24h window per expiry day: from the *previous* day's
    12:00 UTC settlement (17:30 IST -- the moment this contract becomes the
    fresh front daily expiry and starts trading as the "0 DTE" contract) to
    this day's own 12:00 UTC settlement. Originally this only fetched
    00:00-12:00 UTC (the second half); extended 2026-07-18 to the full 24h
    life so session windows can be swept across the whole thing, not just
    the last 12h before settlement.

    One CSV per expiry day at data/btc_liquidity_sweep/options/<DDMMYY>.csv
    (keyed by the *settlement* date, same as before), long format (timestamp,
    symbol, strike, option_type, OHLCV). Resumable — days whose CSV already
    exists are skipped, so re-running after this extension requires clearing
    old (narrower-window) CSVs first or they'll be skipped as "already done".
    """
    out_dir = out_dir or os.path.join(DATA_DIR, 'options')
    os.makedirs(out_dir, exist_ok=True)

    products = fetch_all_products()
    daily_index = build_daily_expiry_index(products)

    today = datetime.now(timezone.utc).date()
    days = [(today - timedelta(days=n)) for n in range(1, days_back + 1)]
    days.sort()

    # Fetch spot once for the whole span (padded a day either side).
    span_start = int(datetime.combine(days[0], datetime.min.time(), tzinfo=timezone.utc).timestamp()) - 86400
    span_end = int(datetime.combine(days[-1], datetime.min.time(), tzinfo=timezone.utc).timestamp()) + 172800
    spot_path = os.path.join(DATA_DIR, 'spot', f'BTCUSD_{resolution}_{days_back}d.csv')
    os.makedirs(os.path.dirname(spot_path), exist_ok=True)
    if os.path.exists(spot_path):
        spot_df = pd.read_csv(spot_path)
        logger.info(f"Loaded cached spot history: {len(spot_df)} rows")
    else:
        logger.info(f"Fetching {days_back}d spot history ({resolution})...")
        spot_df = fetch_spot_history(span_start, span_end, resolution=resolution)
        spot_df.to_csv(spot_path, index=False)
        logger.info(f"Saved spot history: {len(spot_df)} rows -> {spot_path}")

    n_done = n_skipped = n_nodata = 0
    for day in days:
        day_str = day.strftime('%Y-%m-%d')
        ddmmyy = day.strftime('%d%m%y')
        out_path = os.path.join(out_dir, f'{ddmmyy}.csv')
        if os.path.exists(out_path):
            n_skipped += 1
            continue

        day_strikes = daily_index.get(day_str)
        if not day_strikes:
            n_nodata += 1
            continue

        day_00utc = int(datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).timestamp())
        start_ts = day_00utc - 12 * 3600  # previous day's 12:00 UTC settlement (17:30 IST)
        end_ts = day_00utc + 12 * 3600    # this day's 12:00 UTC settlement (17:30 IST)

        rng = spot_range_for_window(spot_df, start_ts, end_ts)
        if rng is None:
            n_nodata += 1
            continue
        spot_lo, spot_hi = rng

        strikes_sorted = sorted(day_strikes.keys())
        interval = strikes_sorted[1] - strikes_sorted[0] if len(strikes_sorted) > 1 else 200
        band = strikes_in_band(day_strikes, spot_lo, spot_hi, interval, itm_buffer_strikes)
        if not band:
            n_nodata += 1
            continue

        symbols = []
        for strike in band:
            for opt_type in ('C', 'P'):
                prod = day_strikes[strike].get(opt_type)
                if prod:
                    symbols.append(prod['symbol'])

        frames = []
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(_fetch_one_symbol, sym, start_ts, end_ts, resolution): sym
                       for sym in symbols}
            for fut in as_completed(futures):
                df = fut.result()
                if df is not None:
                    frames.append(df)

        if not frames:
            n_nodata += 1
            continue

        day_df = pd.concat(frames, ignore_index=True)
        day_df['strike'] = day_df['symbol'].str.split('-').str[2].astype(int)
        day_df['option_type'] = day_df['symbol'].str.split('-').str[0]
        day_df = day_df.sort_values(['symbol', 'time']).reset_index(drop=True)
        day_df.to_csv(out_path, index=False)
        n_done += 1
        logger.info(f"{day_str}: {len(symbols)} symbols, {len(day_df)} rows -> {out_path}")

    logger.info(f"Done. fetched={n_done} skipped(existing)={n_skipped} no_data={n_nodata} "
                f"of {len(days)} days.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--days-back', type=int, default=365)
    parser.add_argument('--itm-buffer', type=int, default=6,
                        help='Extra strikes fetched beyond the day\'s spot range, each side')
    parser.add_argument('--resolution', default='5m', choices=['1m', '5m', '15m'])
    parser.add_argument('--max-workers', type=int, default=12)
    parser.add_argument('--refresh-products', action='store_true',
                        help='Ignore the cached product catalogue and re-paginate')
    args = parser.parse_args()

    if args.refresh_products and os.path.exists(PRODUCTS_CACHE):
        os.remove(PRODUCTS_CACHE)

    fetch_dte0_dataset(days_back=args.days_back, itm_buffer_strikes=args.itm_buffer,
                        resolution=args.resolution, max_workers=args.max_workers)


if __name__ == "__main__":
    main()
