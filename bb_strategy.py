#!/usr/bin/env python3
"""
BTC Options Bollinger Bands Reversal Strategy
Fully Automated Trading Bot for Delta Exchange

Strategy:
- Monitors 5 option strikes (ATM ± 2 strikes)
- Detects bullish reversals from lower Bollinger Band → Buy calls
- Detects bearish reversals from upper Bollinger Band → Buy puts
- Automatic bracket orders with 50% TP and 30% SL
- Complete trade management and position tracking

Author: BotJockie
"""

import hashlib
import hmac
import requests
import time
import json
import logging
import sys
import os
from dotenv import load_dotenv
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
import threading
from dataclasses import dataclass
import math
from urllib.parse import urlencode
import pandas as pd
import matplotlib
# Use Agg backend for non-interactive plotting (saves to file)
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import mplfinance as mpf

# Load environment variables
load_dotenv()

# Force UTF-8 encoding for console output on Windows to support emojis
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('options_strategy.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

@dataclass
class TradingSignal:
    action: str
    symbol: str
    product_id: int
    entry_price: float
    take_profit: float
    stop_loss: float
    reason: str
    bb_data: Dict
    candle_data: Dict
    strike_price: float
    option_type: str

@dataclass
class Position:
    order_id: int
    symbol: str
    entry_price: float
    size: int
    take_profit: float
    stop_loss: float
    timestamp: float
    status: str = "open"

class DeltaExchangeAPI:
    """Delta Exchange API Client with Authentication"""
    
    def __init__(self, api_key: str, api_secret: str, base_url: str = None):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url or os.getenv("DELTA_BASE_URL", "https://api.india.delta.exchange")
        self.session = requests.Session()
        
    def generate_signature(self, secret: str, message: str) -> str:
        """Generate HMAC-SHA256 signature"""
        message = bytes(message, 'utf-8')
        secret = bytes(secret, 'utf-8')
        hash_obj = hmac.new(secret, message, hashlib.sha256)
        return hash_obj.hexdigest()
    
    def make_request(self, method: str, endpoint: str, params: Dict = None, data: Dict = None) -> Dict:
        """Make authenticated API request"""
        path = f"/v2{endpoint}"
        url = f"{self.base_url}{path}"
        
        # Prepare query string
        query_string = ""
        if params:
            query_string = "?" + urlencode(params)
        
        # Prepare payload
        payload = ""
        if data:
            payload = json.dumps(data)
        
        max_retries = 3
        for attempt in range(max_retries):
            # Generate signature with fresh timestamp
            timestamp = str(int(time.time()))
            signature_data = method + timestamp + path + query_string + payload
            signature = self.generate_signature(self.api_secret, signature_data)
            
            # Headers
            headers = {
                'api-key': self.api_key,
                'timestamp': timestamp,
                'signature': signature,
                'User-Agent': 'python-options-bot',
                'Content-Type': 'application/json'
            }
            
            try:
                if method == 'GET':
                    response = self.session.get(url + query_string, headers=headers, timeout=30)
                elif method == 'POST':
                    response = self.session.post(url + query_string, data=payload, headers=headers, timeout=30)
                elif method == 'PUT':
                    response = self.session.put(url + query_string, data=payload, headers=headers, timeout=30)
                elif method == 'DELETE':
                    response = self.session.delete(url + query_string, data=payload, headers=headers, timeout=30)
                
                # Handle Rate Limits (429) and Server Errors (5xx)
                if response.status_code == 429:
                    logger.warning(f"⚠️ Rate limit hit (429). Retrying... (Attempt {attempt+1}/{max_retries})")
                    time.sleep(2 * (attempt + 1))
                    continue
                
                if response.status_code >= 500:
                    logger.warning(f"⚠️ Server error ({response.status_code}). Retrying... (Attempt {attempt+1}/{max_retries})")
                    time.sleep(2 * (attempt + 1))
                    continue
                
                response.raise_for_status()
                
                # Log warning if rate limit is running low
                remaining = response.headers.get('X-RateLimit-Remaining')
                if remaining and int(remaining) < 10:
                    logger.warning(f"⚠️ API Rate Limit Low: {remaining} requests remaining")
                    
                return response.json()
                
            except requests.exceptions.RequestException as e:
                # Log detailed error response if available
                if hasattr(e, 'response') and e.response is not None:
                    logger.error(f"API Error Response: {e.response.status_code} - {e.response.text}")
                
                if attempt == max_retries - 1:
                    logger.error(f"API request failed: {e}")
                    return {"success": False, "error": str(e)}
                logger.warning(f"Request failed: {e}. Retrying... (Attempt {attempt+1}/{max_retries})")
                time.sleep(2 * (attempt + 1))
        
        return {"success": False, "error": "Max retries exceeded"}
    
    def get_btc_spot_price(self) -> Dict:
        """Get current BTC spot price"""
        return self.make_request('GET', '/tickers/BTCUSD')
    
    def get_option_chain(self, underlying: str, expiry_date: str) -> Dict:
        """Get option chain for specific expiry"""
        params = {
            'contract_types': 'call_options,put_options',
            'underlying_asset_symbols': underlying,
            'expiry_date': expiry_date
        }
        return self.make_request('GET', '/tickers', params=params)
    
    def get_candles(self, symbol: str, resolution: str = '5m', lookback_hours: int = 24) -> Dict:
        """Get historical candles for symbol"""
        # Add buffer to end_time to ensure we get the latest forming candle if available
        end_time = int(time.time()) + 120
        start_time = int(time.time()) - (lookback_hours * 3600)
        
        params = {
            'resolution': resolution,
            'symbol': f'MARK:{symbol}',  # Use mark price for options
            'start': start_time,
            'end': end_time
        }
        return self.make_request('GET', '/history/candles', params=params)
    
    def place_order(self, order_data: Dict) -> Dict:
        """Place order with bracket TP/SL"""
        return self.make_request('POST', '/orders', data=order_data)
    
    def get_orders(self, product_id: int = None, state: str = 'open') -> Dict:
        """Get orders"""
        params = {'state': state}
        if product_id:
            params['product_id'] = product_id
        return self.make_request('GET', '/orders', params=params)
    
    def cancel_order(self, order_id: int, product_id: int) -> Dict:
        """Cancel order"""
        data = {'id': order_id, 'product_id': product_id}
        return self.make_request('DELETE', '/orders', data=data)
    
    def get_positions(self, underlying_asset_symbol: str = None) -> Dict:
        """Get current open positions"""
        params = {}
        if underlying_asset_symbol:
            params['underlying_asset_symbol'] = underlying_asset_symbol
        return self.make_request('GET', '/positions', params=params)

class BollingerBandsAnalyzer:
    """Bollinger Bands calculation and signal detection"""
    
    @staticmethod
    def calculate_bollinger_bands(candles: List[Dict], period: int = 20, std_dev: float = 2.0) -> Dict:
        """Calculate Bollinger Bands"""
        # Filter invalid candles
        valid_candles = [c for c in candles if c.get('close') is not None]
        
        if len(valid_candles) < period:
            return None
        
        # Use last 'period' candles
        recent_candles = valid_candles[-period:]
        closes = [float(candle['close']) for candle in recent_candles]
        
        # Calculate SMA
        sma = sum(closes) / len(closes)
        
        # Calculate Standard Deviation
        variance = sum([(close - sma) ** 2 for close in closes]) / len(closes)
        std = math.sqrt(variance)
        
        upper_band = sma + (std_dev * std)
        lower_band = sma - (std_dev * std)
        
        return {
            'upper_band': upper_band,
            'middle_band': sma,
            'lower_band': lower_band,
            'std': std
        }
    
    @staticmethod
    def is_bullish_reversal_candle(candle: Dict, lower_band: float) -> bool:
        """Detect bullish reversal candle from lower BB"""
        try:
            if any(k not in candle or candle[k] is None for k in ['open', 'high', 'low', 'close']):
                return False
                
            open_price = float(candle['open'])
            high_price = float(candle['high'])
            low_price = float(candle['low'])
            close_price = float(candle['close'])
            volume = float(candle.get('volume') or 0)
            
            # Price touched or went below lower band (2% tolerance)
            touched_lower_band = low_price <= lower_band * 1.02
            
            # Bullish candle
            is_bullish = close_price > open_price
            
            # Calculate candle metrics
            body_size = close_price - open_price
            candle_range = high_price - low_price
            
            if candle_range == 0:
                return False
            
            # Strong bullish body (>50% of candle range)
            strong_bullish_body = body_size / candle_range > 0.5
            
            # Strong bounce from low (close in upper 60% of candle)
            strong_bounce = (close_price - low_price) / candle_range > 0.6
            
            # Volume confirmation (basic check)
            volume_ok = volume > 0
            
            return touched_lower_band and is_bullish #and strong_bullish_body #and volume_ok #and strong_bounce
            
        except (ValueError, KeyError) as e:
            logger.error(f"Error in bullish reversal detection: {e}")
            return False
    
    @staticmethod
    def is_bearish_reversal_candle(candle: Dict, upper_band: float) -> bool:
        """Detect bearish reversal candle from upper BB"""
        try:
            if any(k not in candle or candle[k] is None for k in ['open', 'high', 'low', 'close']):
                return False
                
            open_price = float(candle['open'])
            high_price = float(candle['high'])
            low_price = float(candle['low'])
            close_price = float(candle['close'])
            volume = float(candle.get('volume') or 0)
            
            # Price touched or went above upper band (2% tolerance)
            touched_upper_band = high_price >= upper_band * 0.98
            
            # Bearish candle
            is_bearish = close_price < open_price
            
            # Calculate candle metrics
            body_size = open_price - close_price
            candle_range = high_price - low_price
            
            if candle_range == 0:
                return False
            
            # Strong bearish body (>50% of candle range)
            strong_bearish_body = body_size / candle_range > 0.5
            
            # Strong rejection from high (close in lower 40% of candle)
            strong_rejection = (close_price - low_price) / candle_range < 0.4
            
            # Volume confirmation
            volume_ok = volume > 0
            
            return touched_upper_band and is_bearish #and strong_bearish_body #and volume_ok #and strong_rejection
            
        except (ValueError, KeyError) as e:
            logger.error(f"Error in bearish reversal detection: {e}")
            return False
    
    @staticmethod
    def calculate_adx(candles: List[Dict], period: int = 14) -> float:
        """Calculate ADX (Average Directional Index)"""
        if len(candles) < 2 * period + 1:
            return 0.0
            
        # Extract HL C
        highs = [float(c['high']) for c in candles]
        lows = [float(c['low']) for c in candles]
        closes = [float(c['close']) for c in candles]
        
        tr_list = []
        plus_dm_list = []
        minus_dm_list = []
        
        for i in range(1, len(candles)):
            h = highs[i]
            l = lows[i]
            prev_c = closes[i-1]
            prev_h = highs[i-1]
            prev_l = lows[i-1]
            
            tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
            tr_list.append(tr)
            
            up_move = h - prev_h
            down_move = prev_l - l
            
            if up_move > down_move and up_move > 0:
                plus_dm_list.append(up_move)
            else:
                plus_dm_list.append(0.0)
                
            if down_move > up_move and down_move > 0:
                minus_dm_list.append(down_move)
            else:
                minus_dm_list.append(0.0)

        # First smoothed values (Sum of first 'period')
        tr_smooth = sum(tr_list[:period])
        plus_dm_smooth = sum(plus_dm_list[:period])
        minus_dm_smooth = sum(minus_dm_list[:period])
        
        dx_list = []
        
        # Helper to calc DX
        def calc_dx(p_dm, m_dm, tr):
            if tr == 0: return 0.0
            di_plus = 100 * (p_dm / tr)
            di_minus = 100 * (m_dm / tr)
            if di_plus + di_minus == 0: return 0.0
            return 100 * abs(di_plus - di_minus) / (di_plus + di_minus)

        dx_list.append(calc_dx(plus_dm_smooth, minus_dm_smooth, tr_smooth))
        
        # Subsequent smoothing for TR, +DM, -DM
        for i in range(period, len(tr_list)):
            tr_smooth = tr_smooth - (tr_smooth / period) + tr_list[i]
            plus_dm_smooth = plus_dm_smooth - (plus_dm_smooth / period) + plus_dm_list[i]
            minus_dm_smooth = minus_dm_smooth - (minus_dm_smooth / period) + minus_dm_list[i]
            dx_list.append(calc_dx(plus_dm_smooth, minus_dm_smooth, tr_smooth))
            
        # ADX Calculation
        # First ADX is average of first 'period' DX values
        if len(dx_list) < period:
            return 0.0
            
        adx = sum(dx_list[:period]) / period
        
        # Subsequent ADX smoothing
        for i in range(period, len(dx_list)):
            adx = ((adx * (period - 1)) + dx_list[i]) / period
            
        return adx

class OptionsStrategy:
    """Main Options Trading Strategy"""
    
    def __init__(self, api_key: str, api_secret: str, target_expiry: str = None, mock_mode: bool = False):
        self.api = DeltaExchangeAPI(api_key, api_secret)
        self.analyzer = BollingerBandsAnalyzer()
        self.positions: Dict[int, Position] = {}
        self.target_expiry = target_expiry or self._get_next_friday()
        self.running = False
        self.mock_mode = mock_mode
        
        if self.mock_mode:
            logger.info("⚠️ RUNNING IN MOCK MODE - No real orders will be placed")
        
        # Strategy parameters
        self.take_profit_percent = float(os.getenv("TAKE_PROFIT_PERCENT", "10"))  # 10% profit
        self.stop_loss_percent = float(os.getenv("STOP_LOSS_PERCENT", "5"))    # 5% loss
        self.position_size = int(os.getenv("POSITION_SIZE", "1"))             # 1 lot per trade
        self.max_positions = int(os.getenv("MAX_POSITIONS", "4"))             # Maximum concurrent positions
        self.option_type = os.getenv("OPTION_TYPE", "BOTH").upper()           # CALL, PUT, or BOTH
        self.adx_threshold = float(os.getenv("ADX_THRESHOLD", "25"))
        self.adx_period = int(os.getenv("ADX_PERIOD", "14"))
        self.bb_period = int(os.getenv("BB_PERIOD", "20"))
        self.bb_std_dev = float(os.getenv("BB_STD_DEV", "2.0"))
        self.min_option_price = float(os.getenv("MIN_OPTION_PRICE", "50"))
        
        if self.option_type not in ['CALL', 'PUT', 'BOTH']:
            logger.warning(f"Invalid OPTION_TYPE '{self.option_type}', defaulting to BOTH")
            self.option_type = 'BOTH'
            
        # Create charts directory if it doesn't exist
        if not os.path.exists('charts'):
            os.makedirs('charts')
        
        logger.info(f"Strategy initialized for expiry: {self.target_expiry}")
        logger.info(f"Option Type: {self.option_type}")
    
    def _get_next_friday(self) -> str:
        """Get next Friday's date in DD-MM-YYYY format"""
        today = datetime.now()
        days_ahead = 4 - today.weekday()  # Friday is 4
        if days_ahead <= 0:
            days_ahead += 7
        next_friday = today + timedelta(days=days_ahead)
        return next_friday.strftime("%d-%m-%Y")
    
    def get_target_strikes(self, btc_price: float, option_chain: List[Dict]) -> List[Dict]:
        """Select 5 strikes to monitor: ATM ± 2 strikes"""
        # Round to nearest 1000 for ATM
        atm_strike = round(btc_price / 200) * 200
        
        target_strikes = {
            'calls': [atm_strike],
            'puts': [atm_strike]
        }
        
        # Filter based on option type preference
        if self.option_type == 'CALL':
            target_strikes['puts'] = []
        elif self.option_type == 'PUT':
            target_strikes['calls'] = []
        
        monitored_options = []
        
        for option in option_chain:
            try:
                strike = float(option['strike_price'])
                mark_price = float(option.get('mark_price') or 0)
                
                if option['contract_type'] == 'call_options' and strike in target_strikes['calls']:
                    if mark_price < self.min_option_price:
                        logger.warning(f"⚠️ Skipping {option['symbol']} (Strike: {strike}): Price {mark_price} < Min {self.min_option_price}")
                        continue
                        
                    monitored_options.append({
                        'symbol': option['symbol'],
                        'type': 'call',
                        'strike': strike,
                        'product_id': option['product_id'],
                        'mark_price': mark_price
                    })
                    logger.info(f"Monitoring Call: {option['symbol']} (Strike: {strike}, Product ID: {option['product_id']}), Mark Price: {mark_price}")
                    
                
                elif option['contract_type'] == 'put_options' and strike in target_strikes['puts']:
                    if mark_price < self.min_option_price:
                        logger.warning(f"⚠️ Skipping {option['symbol']} (Strike: {strike}): Price {mark_price} < Min {self.min_option_price}")
                        continue
                        
                    monitored_options.append({
                        'symbol': option['symbol'],
                        'type': 'put',
                        'strike': strike,
                        'product_id': option['product_id'],
                        'mark_price': mark_price
                    })
                    logger.info(f"Monitoring Put: {option['symbol']} (Strike: {strike}, Product ID: {option['product_id']}), Mark Price: {mark_price}")
            except (ValueError, KeyError) as e:
                logger.warning(f"Error processing option {option.get('symbol', 'unknown')}: {e}")
                continue
        
        return monitored_options
    
    def generate_signal_chart(self, symbol: str, candles: List[Dict], signal: TradingSignal):
        """Generate and save a chart snapshot for the signal"""
        try:
            # Convert to DataFrame
            df = pd.DataFrame(candles)
            df['time'] = pd.to_datetime(df['time'], unit='s')
            df.set_index('time', inplace=True)
            df = df[['open', 'high', 'low', 'close', 'volume']].astype(float)
            df.columns = ['Open', 'High', 'Low', 'Close', 'Volume']
            
            # Calculate BB for plotting (re-calculate on full series for context)
            period = self.bb_period
            std_dev = self.bb_std_dev
            df['MA'] = df['Close'].rolling(window=period).mean()
            df['STD'] = df['Close'].rolling(window=period).std()
            df['Upper'] = df['MA'] + (std_dev * df['STD'])
            df['Lower'] = df['MA'] - (std_dev * df['STD'])
            
            # Slice last 30 candles for the snapshot
            plot_df = df.tail(30).copy()
            
            # Create 5 future candles for whitespace
            last_time = plot_df.index[-1]
            # Assuming 1m candles based on analyze_option_strike call
            future_dates = [last_time + timedelta(minutes=i) for i in range(1, 6)]
            
            # Create empty dataframe for future dates with same columns
            future_df = pd.DataFrame(index=future_dates, columns=plot_df.columns)
            
            # Combine
            extended_df = pd.concat([plot_df, future_df])
            
            # Determine start and end points for lines
            # Start: 1 candle before signal (signal is at original plot_df index -1)
            start_time = plot_df.index[-2] if len(plot_df) >= 2 else plot_df.index[0]
            end_time = future_dates[-1]
            
            # Define line segments
            seq_entry = [(start_time, signal.entry_price), (end_time, signal.entry_price)]
            seq_tp = [(start_time, signal.take_profit), (end_time, signal.take_profit)]
            seq_sl = [(start_time, signal.stop_loss), (end_time, signal.stop_loss)]
            
            # Add plots for BB
            apds = [
                mpf.make_addplot(extended_df['Upper'], color='green', width=0.8),
                mpf.make_addplot(extended_df['Lower'], color='red', width=0.8),
                mpf.make_addplot(extended_df['MA'], color='blue', width=0.5, linestyle='--')
            ]
            
            # Save plot
            timestamp = int(time.time())
            filename = f"charts/{signal.action}_{symbol}_{timestamp}.png"
            
            # Config for arbitrary lines
            alines_config = dict(
                alines=[seq_entry, seq_tp, seq_sl],
                colors=['blue', 'green', 'red'],
                linestyle='-',
                linewidths=1.0
            )
            
            fig, axlist = mpf.plot(
                extended_df,
                type='candle',
                style='charles',
                addplot=apds,
                alines=alines_config,
                title=f"{signal.action} {symbol}\nEntry: {signal.entry_price:.2f} | TP: {signal.take_profit:.2f} | SL: {signal.stop_loss:.2f} | ADX: {self.analyzer.calculate_adx(candles):.2f}",
                returnfig=True,
                volume=False  # Mark price candles often don't have volume
            )
            
            # Add labels above lines
            ax = axlist[0]
            x_pos = len(plot_df) - 2 if len(plot_df) >= 2 else 0
            
            ax.text(x_pos, signal.entry_price, ' ENTRY', color='blue', fontsize=8, fontweight='bold', verticalalignment='bottom')
            ax.text(x_pos, signal.take_profit, ' TP', color='green', fontsize=8, fontweight='bold', verticalalignment='bottom')
            ax.text(x_pos, signal.stop_loss, ' SL', color='red', fontsize=8, fontweight='bold', verticalalignment='bottom')
            
            fig.savefig(filename)
            plt.close(fig)
            logger.info(f"📸 Chart snapshot saved to {filename}")
            
        except Exception as e:
            logger.error(f"Failed to generate chart: {e}")

    def analyze_option_strike(self, option_data: Dict) -> Optional[TradingSignal]:
        """Analyze individual option strike for BB reversal signals"""
        symbol = option_data['symbol']
        option_type = option_data['type']
        strike = option_data['strike']
        product_id = option_data['product_id']
        mark_price = option_data['mark_price']
        
        # Get historical candles
        candles_response = self.api.get_candles(symbol, resolution='1m', lookback_hours=3)
        
        if not candles_response.get('success') or not candles_response.get('result'):
            # logger.warning(f"No candle data for {symbol}")
            return None
        
        candles = candles_response['result']
        candles.reverse()
        logger.info(f"Received {len(candles)} candles for {symbol}")
        
        if len(candles) < max(self.bb_period, self.adx_period):
            # logger.warning(f"Insufficient candle data for {symbol}: {len(candles)} candles")
            return None
        
        # Use the latest candle for analysis
        analysis_candle = candles[-1]
        
        # Calculate Bollinger Bands
        bb = self.analyzer.calculate_bollinger_bands(candles, period=self.bb_period, std_dev=self.bb_std_dev)
        if not bb:
            return None
        
        # Calculate ADX
        adx = self.analyzer.calculate_adx(candles, period=self.adx_period)
        
        logger.info(
            f"Candle {symbol}: "
            f"Time={datetime.fromtimestamp(analysis_candle.get('time')).strftime('%H:%M')} "
            f"O={analysis_candle.get('open')} H={analysis_candle.get('high')} "
            f"L={analysis_candle.get('low')} C={analysis_candle.get('close')} "
            f"V={analysis_candle.get('volume')}"
        )
        logger.info(f"BB {symbol}: Upper={bb['upper_band']:.2f} Middle={bb['middle_band']:.2f} Lower={bb['lower_band']:.2f} | ADX={adx:.2f}")
        logger.info(f"Chart: https://www.delta.exchange/app/tradingview/mark-chart/options/BTC/{symbol}")
        
        # Log technical analysis details
        try:
            c_open = float(analysis_candle.get('open') or 0)
            c_close = float(analysis_candle.get('close') or 0)
            upper = bb['upper_band']
            lower = bb['lower_band']
            sma = bb['middle_band']
            
            tech_msgs = []
            if c_open <= upper < c_close:
                tech_msgs.append("Crossed OVER Upper Band")
            elif c_open >= upper > c_close:
                tech_msgs.append("Crossed UNDER Upper Band")
            
            if c_open >= lower > c_close:
                tech_msgs.append("Crossed UNDER Lower Band")
            elif c_open <= lower < c_close:
                tech_msgs.append("Crossed OVER Lower Band")
                
            if c_open <= sma < c_close:
                tech_msgs.append("Crossed SMA UP")
            elif c_open >= sma > c_close:
                tech_msgs.append("Crossed SMA DOWN")
                
            if tech_msgs:
                logger.info(f"  Technicals {symbol}: {', '.join(tech_msgs)} (BB: L={lower:.2f} M={sma:.2f} U={upper:.2f})")
        except Exception:
            pass
        
        # Filter by ADX
        if adx < self.adx_threshold:
            return None
        
        # Current price for validation
        current_price = float(mark_price or analysis_candle.get('close') or 0)
        
        # Calculate Stop-Limit Entry Price (High + 1% buffer)
        candle_high = float(analysis_candle.get('high') or current_price)
        entry_price = candle_high * 1.01
        
        # Skip if price is too low (avoid illiquid options)
        if current_price < self.min_option_price:
            return None
        
        signal = None
        
        # Always look for Bullish Reversal from Lower BB to BUY the option
        if self.analyzer.is_bullish_reversal_candle(analysis_candle, bb['lower_band']):
            # Calculate Stop-Limit Entry Price (High + 1% buffer)
            candle_high = float(analysis_candle.get('high') or current_price)
            entry_price = candle_high * 1.01
            
            action = 'BUY_CALL' if option_type == 'call' else 'BUY_PUT'
            
            signal = TradingSignal(
                action=action,
                symbol=symbol,
                product_id=product_id,
                entry_price=entry_price,
                take_profit=entry_price * (1 + self.take_profit_percent / 100),
                stop_loss=entry_price * (1 - self.stop_loss_percent / 100),
                reason=f'Bullish reversal from lower BB at {bb["lower_band"]:.2f}',
                bb_data=bb,
                candle_data=analysis_candle,
                strike_price=strike,
                option_type=option_type
            )
        
        if signal:
            # Generate chart for the signal
            self.generate_signal_chart(symbol, candles, signal)
        
        return signal
    
    def place_bracket_order(self, signal: TradingSignal) -> Optional[Dict]:
        """Place option order with automatic TP/SL bracket"""
        
        # Check if we already have max positions
        if len(self.positions) >= self.max_positions:
            logger.info(f"Max positions ({self.max_positions}) reached, skipping trade")
            return None
        
        side = 'buy' if 'BUY' in signal.action else 'sell'
        # For Buy: Limit is higher than Stop (1.01)
        # For Sell: Limit is lower than Stop (0.99)
        limit_buffer = 1.01 if side == 'buy' else 0.99
        
        order_payload = {
            "product_id": signal.product_id,
            "product_symbol": signal.symbol,
            "size": self.position_size,
            "side": side,
            "order_type": "limit_order",
            "stop_price": str(round(signal.entry_price, 2)),
            "limit_price": str(round(signal.entry_price * limit_buffer, 2)),
            
            # Bracket Take Profit
            "bracket_take_profit_price": str(round(signal.take_profit, 2)),
            "bracket_take_profit_limit_price": str(round(signal.take_profit, 2)),
            
            # Bracket Stop Loss
            "bracket_stop_loss_price": str(round(signal.stop_loss, 2)),
            # Set limit price 5% beyond stop to ensure execution (marketable limit)
            "bracket_stop_loss_limit_price": str(round(signal.stop_loss * (0.95 if side == 'buy' else 1.05), 2)),
            
            # Use mark price for reliable triggering
            "bracket_stop_trigger_method": "mark_price",
            
            # Custom client order ID for tracking
            "client_order_id": f"bb_{signal.symbol.replace('-', '_')}_{int(time.time())}"
        }
        
        logger.info(f"Placing order: {signal.action} {signal.symbol} @ {signal.entry_price}")
        logger.info(f"TP: {signal.take_profit} | SL: {signal.stop_loss}")
        
        if self.mock_mode:
            logger.info(f"🛑 MOCK MODE: Simulating order placement for {signal.symbol}")
            # Generate mock order ID
            order_id = int(time.time() * 1000000)
            
            # Store position
            position = Position(
                order_id=order_id,
                symbol=signal.symbol,
                entry_price=signal.entry_price,
                size=self.position_size,
                take_profit=signal.take_profit,
                stop_loss=signal.stop_loss,
                timestamp=time.time()
            )
            
            self.positions[order_id] = position
            
            logger.info(f"✅ MOCK Order placed successfully: ID {order_id}")
            logger.info(f"Reason: {signal.reason}")
            return {"id": order_id, "status": "open", "product_symbol": signal.symbol}

        response = self.api.place_order(order_payload)
        
        if response.get('success'):
            order_result = response['result']
            order_id = order_result['id']
            
            # Store position
            position = Position(
                order_id=order_id,
                symbol=signal.symbol,
                entry_price=signal.entry_price,
                size=self.position_size,
                take_profit=signal.take_profit,
                stop_loss=signal.stop_loss,
                timestamp=time.time()
            )
            
            self.positions[order_id] = position
            
            logger.info(f"Order placed successfully: ID {order_id}")
            logger.info(f"Reason: {signal.reason}")
            
            return order_result
        else:
            error_msg = response.get('error', 'Unknown error')
            logger.error(f"Order failed: {error_msg}")
            return None
    
    def monitor_positions(self):
        """Monitor existing positions and manage them"""
        if not self.positions:
            return
        
        logger.info(f"Monitoring {len(self.positions)} positions...")
        
        if self.mock_mode:
            logger.info("🛑 MOCK MODE: Skipping API order check. Positions will remain 'open' until restart.")
            for order_id, position in self.positions.items():
                logger.info(f"MOCK Position {position.symbol} (ID: {order_id}) is open")
            return
        
        # Get all open orders
        orders_response = self.api.get_orders(state='open')
        if not orders_response.get('success'):
            logger.error("Failed to fetch open orders")
            return
        
        open_orders = {order['id']: order for order in orders_response.get('result', [])}
        
        # Get active positions from exchange to track filled orders
        positions_response = self.api.get_positions(underlying_asset_symbol='BTC')
        if not positions_response.get('success'):
            logger.error("Failed to fetch positions")
            return
            
        active_exchange_positions = set()
        for pos in positions_response.get('result', []):
            if float(pos.get('size', 0)) > 0:
                active_exchange_positions.add(pos.get('product_symbol'))
        
        # Check each position
        positions_to_remove = []
        
        for order_id, position in self.positions.items():
            # 1. Check if entry order is still pending
            if order_id in open_orders:
                order = open_orders[order_id]
                unfilled_size = float(order.get('unfilled_size', 0))
                
                if unfilled_size == 0:
                    logger.info(f"Position {position.symbol} fully filled")
                else:
                    logger.info(f"Position {position.symbol} partially filled: {position.size - unfilled_size}/{position.size}")
                continue
            
            # 2. Check if position is active on exchange (filled entry)
            if position.symbol in active_exchange_positions:
                # Position is active (filled)
                continue
                
            # 3. Neither pending nor active -> Closed
            logger.info(f"Position {position.symbol} (ID: {order_id}) is closed")
            positions_to_remove.append(order_id)
        
        # Remove closed positions
        for order_id in positions_to_remove:
            del self.positions[order_id]
    
    def run_strategy_cycle(self):
        """Run one complete strategy cycle"""
        try:
            # 1. Get current BTC price
            btc_response = self.api.get_btc_spot_price()
            if not btc_response.get('success'):
                logger.error("Failed to get BTC price")
                return
            
            btc_price = float(btc_response['result']['mark_price'])
            logger.info(f"BTC Price: ${btc_price:,.2f}")
            
            # 2. Get option chain
            option_chain_response = self.api.get_option_chain("BTC", self.target_expiry)
            if not option_chain_response.get('success'):
                logger.error("Failed to get option chain")
                return
            
            option_chain = option_chain_response['result']
            logger.info(f"Received {len(option_chain)} options in chain")
            
            # 3. Select target strikes
            monitored_options = self.get_target_strikes(btc_price, option_chain)
            logger.info(f"Monitoring {len(monitored_options)} strikes around ATM ${round(btc_price/200)*200:,.0f}")
            
            # 4. Analyze each strike for signals
            signals_found = 0
            
            # Get list of currently active symbols
            active_symbols = {p.symbol for p in self.positions.values()}
            
            # Check pending orders on exchange to avoid duplicates
            if not self.mock_mode:
                open_orders_response = self.api.get_orders(state='open')
                if open_orders_response.get('success'):
                    for order in open_orders_response.get('result', []):
                        active_symbols.add(order.get('product_symbol'))
            
            for option in monitored_options:
                if len(self.positions) >= self.max_positions:
                    logger.info("Max positions reached, stopping signal search")
                    break
                
                # Skip if we already have a position for this symbol
                if option['symbol'] in active_symbols:
                    continue
                
                signal = self.analyze_option_strike(option)
                
                if signal:
                    signals_found += 1
                    logger.info(f"SIGNAL DETECTED: {signal.reason}")
                    
                    # Place bracket order
                    order_result = self.place_bracket_order(signal)
                    
                    if order_result:
                        logger.info(f"Trade executed: {signal.symbol}")
                    
                    # Small delay between orders
                    time.sleep(2)
            
            if signals_found == 0:
                logger.info("No signals detected in this cycle")
            
            # 5. Monitor existing positions
            self.monitor_positions()
            
        except Exception as e:
            logger.error(f"Error in strategy cycle: {e}")
    
    def start(self, cycle_interval: int = 60):
        """Start the automated strategy"""
        logger.info("Starting BTC Options Bollinger Bands Strategy")
        logger.info(f"Target Expiry: {self.target_expiry}")
        logger.info(f"Cycle Interval: {cycle_interval} seconds")
        logger.info(f"Max Positions: {self.max_positions}")
        logger.info(f"Option Type: {self.option_type}")
        logger.info(f"ADX Threshold: {self.adx_threshold}")
        logger.info(f"Take Profit: {self.take_profit_percent}%")
        logger.info(f"Stop Loss: {self.stop_loss_percent}%")
        
        self.running = True
        
        while self.running:
            try:
                # Align to next minute start
                now = datetime.now()
                sleep_seconds = 60 - now.second - (now.microsecond / 1000000)
                logger.info(f"Waiting {sleep_seconds:.2f}s for next minute start...")
                time.sleep(sleep_seconds)
                
                cycle_start = time.time()
                
                logger.info("=" * 60)
                logger.info(f"Strategy Cycle - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                logger.info("=" * 60)
                
                self.run_strategy_cycle()
                
                cycle_duration = time.time() - cycle_start
                logger.info(f"Cycle completed in {cycle_duration:.2f}s")
                
            except KeyboardInterrupt:
                logger.info("Strategy stopped by user")
                break
            except Exception as e:
                logger.error(f"Unexpected error: {e}")
                time.sleep(30)  # Wait before retrying
        
        self.running = False
        logger.info("Strategy stopped")
    
    def stop(self):
        """Stop the strategy"""
        self.running = False

def main():
    """Main function to run the strategy"""
    
    # Load configuration from environment variables
    API_KEY = os.getenv("DELTA_API_KEY")
    API_SECRET = os.getenv("DELTA_API_SECRET")
    
    # Strategy configuration
    TARGET_EXPIRY = os.getenv("TARGET_EXPIRY")  # DD-MM-YYYY format, or None for next Friday
    CYCLE_INTERVAL = int(os.getenv("CYCLE_INTERVAL", "60"))  # seconds between strategy cycles
    MOCK_MODE = os.getenv("MOCK_MODE", "False").lower() == "true"     # Set to True to simulate orders without placing them
    
    # Validate API credentials
    if not API_KEY or not API_SECRET or API_KEY == "your_api_key_here":
        logger.error("❌ Please set your actual API credentials in the script")
        logger.error("Get your API keys from: https://www.delta.exchange/app/account/manageapikeys")
        return
    
    try:
        # Initialize and start strategy
        strategy = OptionsStrategy(
            api_key=API_KEY,
            api_secret=API_SECRET,
            target_expiry=TARGET_EXPIRY,
            mock_mode=MOCK_MODE
        )
        
        # Start the automated strategy
        strategy.start(cycle_interval=CYCLE_INTERVAL)
        
    except Exception as e:
        logger.error(f"Failed to start strategy: {e}")

if __name__ == "__main__":
    main()
