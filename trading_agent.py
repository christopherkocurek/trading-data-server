#!/usr/bin/env python3
"""
Autonomous Trading Agent
Runs hourly via cron to analyze markets and post commentary.

Setup cron (every hour):
    0 * * * * cd /path/to/server && python trading_agent.py >> /var/log/trading_agent.log 2>&1

Required env vars:
    ANTHROPIC_API_KEY - Claude API key
    DATABASE_PATH - Path to SQLite database (optional, defaults to trading_data.db)
"""

import os
import sys
import json
import requests
from datetime import datetime
from typing import Dict, Optional

# Add parent directory for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database import get_database

# Configuration
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
SERVER_URL = os.getenv("TRADING_SERVER_URL", "https://web-production-c15bf.up.railway.app")

# Trading Expert System Prompt - Embedded Knowledge Base
TRADING_EXPERT_SYSTEM = """You are an elite BTC trading analyst with deep expertise in macro, on-chain, derivatives, and technical analysis. You follow a systematic 4-step framework and legendary trader principles.

## ANALYSIS FRAMEWORK (Priority Order)

### Step 1: Macro Regime (Highest Priority)
- Global M2: +0.94 correlation, 10-12 week lag. Expansion = bullish, Contraction = bearish
- DXY: -0.70 inverse correlation. Below 105 = bullish, Above 105 = bearish
- Fed Policy: Cuts/QE = bullish, Hikes/QT = bearish
- VIX: Below 20 = risk-on, Above 30 = caution

### Step 2: On-Chain Health
- MVRV Z-Score: >3.7 = SELL (cycle top), <1.0 = BUY (undervalued), <0 = generational buy
- Reserve Risk: >0.02 = SELL, <0.0026 = BUY
- Exchange Flows: Sustained outflows = bullish supply squeeze, Spike inflows = bearish
- NUPL: >0.75 = Euphoria SELL, <-0.25 = Capitulation BUY

### Step 3: Derivatives Positioning
- Funding Rate: >+0.10% = longs crowded (contrarian short), <-0.05% = shorts crowded (contrarian long)
- Open Interest + Price: Both rising = trend continuation, OI falling + price rising = short squeeze
- Liquidation Clusters: Dense cluster = price magnet, never place stops in dense zones

### Step 4: Technical Confirmation
- 200 MA: Above = bullish structure, Below = bearish structure (PTJ's primary rule)
- RSI: <30 = oversold, >70 = overbought
- VCP: Decreasing pullback depth + declining volume = breakout setup

## KEY ENTRY SIGNALS (High Conviction)
1. Hash Ribbon Buy: 30d hashrate crosses above 60d + MVRV <1 (87% win rate, 557% avg return)
2. Funding Squeeze: Extreme funding + liquidation cluster visible (25/25 score)
3. Wyckoff Spring: Break below support + rapid recovery with absorption
4. Extreme Fear Prolonged: F&G <25 for 14+ days at Fibonacci support

## KEY EXIT SIGNALS
1. MVRV >3.7: Exit 50-75% (all major cycle tops)
2. NUPL >0.75: Exit 40-60% (5 confirmed tops)
3. Extreme Greed >85 for 5+ days: Scale out 10-20% per 5 points
4. LTH Distribution 4+ weeks: Exit 30-50%

## RISK FRAMEWORK
- Position Sizing: Risk Amount / Stop Distance (1-2% per trade)
- Kelly: Use Quarter Kelly (25%) for crypto volatility
- Portfolio Heat: Max 6% total risk, adjust for crypto correlation (1.3x factor)
- Drawdown Protocol: 10%=reduce 25%, 15%=reduce 50% + review, 20%=HALT
- Black Swan Defense: Never >25% single position, maintain 20-30% cash reserve

## BTC-SPECIFIC PATTERNS
- Halving Cycle: Accumulate 6-12 months before, peak 12-18 months after (diminishing returns each cycle)
- Mining Signals: Hash Ribbon, Puell Multiple <0.5, Difficulty Ribbon compression <0.02
- Dominance: >60% = BTC season, <45% = altseason peak (exit alts)
- Weekend: 60% mean reversion win rate, reduce position size 50%
- ETF Flows: 5+ consecutive days inflows = bullish, 3+ days outflows = reduce risk

## LEGENDARY TRADER RULES
- PTJ: "200-day MA is my metric for everything" - be long above, cash below
- Druckenmiller: "Sizing is 70-80% of the equation" - size up on conviction (5+ signals)
- Livermore: "Never average losses, only add to winners" - pyramid 25% at 0%, 5%, 10%, 15% profit
- Minervini: 7-8% stop-loss rule, VCP breakouts, Stage 2 trend template
- Raschke: Time-based exits - if trade doesn't work in expected time, exit

## FEAR & GREED PROTOCOL
- <20: Aggressive accumulation (staged: 25% each at 20, 15, 10, 5)
- 25-75: Neutral zone
- >85: Aggressive distribution (10-20% per 5-point increase)
- Contrarian edge: 63% of extreme fear periods ended positive

## OUTPUT FORMAT
Be direct and conversational like a senior trader briefing a colleague. Cover:
1. Current situation (price action, key levels, sentiment)
2. Framework assessment (which step is dominant right now)
3. Key levels to watch (support/resistance, liquidation clusters)
4. Actionable bias with confidence level

End with: **Bias: [BULLISH/BEARISH/NEUTRAL]** | Confidence: X/10"""


def calculate_rsi(closes: list, period: int = 14) -> Optional[float]:
    """Calculate RSI from closing prices."""
    if len(closes) < period + 1:
        return None

    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calculate_macd(closes: list, fast: int = 12, slow: int = 26, signal: int = 9) -> Dict:
    """Calculate MACD from closing prices."""
    if len(closes) < slow + signal:
        return {}

    def ema(data, period):
        multiplier = 2 / (period + 1)
        ema_values = [sum(data[:period]) / period]
        for price in data[period:]:
            ema_values.append((price - ema_values[-1]) * multiplier + ema_values[-1])
        return ema_values

    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)

    # Align arrays
    offset = slow - fast
    macd_line = [ema_fast[i + offset] - ema_slow[i] for i in range(len(ema_slow))]

    if len(macd_line) < signal:
        return {}

    signal_line = ema(macd_line, signal)
    histogram = macd_line[-1] - signal_line[-1]

    return {
        "macd_line": round(macd_line[-1], 2),
        "macd_signal": round(signal_line[-1], 2),
        "macd_histogram": round(histogram, 2)
    }


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

def fetch_klines(symbol: str = "BTC", limit: int = 250) -> list:
    """Fetch kline data - tries multiple sources."""

    # Try Binance first
    try:
        resp = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": f"{symbol}USDT", "interval": "1d", "limit": limit},
            headers=HEADERS,
            timeout=10
        )
        result = resp.json()
        if isinstance(result, list) and len(result) > 0 and isinstance(result[0], list):
            # Return close prices (index 4)
            return [float(k[4]) for k in result]
        print(f"Binance returned: {str(result)[:100]}")
    except Exception as e:
        print(f"Binance klines error: {e}")

    # Fallback to CryptoCompare
    try:
        print("Trying CryptoCompare fallback...")
        resp = requests.get(
            "https://min-api.cryptocompare.com/data/v2/histoday",
            params={"fsym": symbol, "tsym": "USD", "limit": limit},
            headers=HEADERS,
            timeout=10
        )
        result = resp.json()
        if result.get("Response") == "Success":
            data = result.get("Data", {}).get("Data", [])
            if data:
                return [float(d["close"]) for d in data if d.get("close")]
        print(f"CryptoCompare returned: {str(result)[:100]}")
    except Exception as e:
        print(f"CryptoCompare error: {e}")

    # Final fallback - use CoinGecko market chart
    try:
        print("Trying CoinGecko fallback...")
        resp = requests.get(
            "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart",
            params={"vs_currency": "usd", "days": str(limit)},
            headers=HEADERS,
            timeout=15
        )
        result = resp.json()
        if "prices" in result:
            return [float(p[1]) for p in result["prices"]]
    except Exception as e:
        print(f"CoinGecko chart error: {e}")

    return []


def fetch_technical_indicators() -> Dict:
    """Calculate technical indicators from price data."""
    data = {}

    try:
        closes = fetch_klines()  # Now returns close prices directly
        if not closes:
            print("No price data available for technical indicators")
            return data

        if len(closes) < 50:
            return data

        # RSI
        rsi = calculate_rsi(closes)
        if rsi:
            data["rsi"] = round(rsi, 1)

        # MACD
        macd = calculate_macd(closes)
        data.update(macd)

        # 200 MA
        if len(closes) >= 200:
            data["ma_200"] = round(sum(closes[-200:]) / 200, 2)

        # 50 MA for additional context
        if len(closes) >= 50:
            data["ma_50"] = round(sum(closes[-50:]) / 50, 2)

        # Price relative to MAs
        if closes:
            current_price = closes[-1]
            if data.get("ma_200"):
                data["above_200ma"] = current_price > data["ma_200"]
                data["pct_from_200ma"] = round((current_price / data["ma_200"] - 1) * 100, 1)

    except Exception as e:
        print(f"Error calculating technical indicators: {e}")

    return data


def fetch_dxy_vix() -> Dict:
    """Fetch DXY and VIX from Yahoo Finance via yfinance-like endpoint."""
    data = {}

    # Try Alpha Vantage or similar free API for DXY
    # Fallback: use a simple forex endpoint
    try:
        # DXY approximation using USD strength
        resp = requests.get(
            "https://api.exchangerate-api.com/v4/latest/USD",
            timeout=10
        )
        if resp.status_code == 200:
            rates = resp.json().get("rates", {})
            # DXY basket approximation (weighted EUR, JPY, GBP, CAD, SEK, CHF)
            eur = rates.get("EUR", 1)
            jpy = rates.get("JPY", 100) / 100
            gbp = rates.get("GBP", 1)

            # Simplified DXY proxy (not exact, but directional)
            if eur and jpy and gbp:
                dxy_proxy = (1/eur * 0.576) + (jpy * 0.136) + (1/gbp * 0.119)
                data["dxy_proxy"] = round(dxy_proxy * 100, 1)
                data["dxy_note"] = "Proxy from forex rates"
    except Exception as e:
        print(f"Error fetching DXY proxy: {e}")

    # Try to get VIX from free source
    try:
        # Use CBOE or alternative free API
        resp = requests.get(
            "https://cdn.cboe.com/api/global/delayed_quotes/indices/.json",
            timeout=10
        )
        # Note: This endpoint may not work, fallback below
    except:
        pass

    # Alternative: Use Fear & Greed as VIX proxy for crypto
    # (High fear often correlates with high VIX)

    return data


def fetch_onchain_metrics() -> Dict:
    """Fetch on-chain metrics from available free APIs."""
    data = {}

    # Try CoinGlass for some metrics
    try:
        resp = requests.get(
            "https://open-api.coinglass.com/public/v2/index/bitcoin-profitable-days",
            timeout=10
        )
        # Note: May require API key
    except:
        pass

    # Try blockchain.info for basic on-chain
    try:
        # Hash rate
        resp = requests.get(
            "https://api.blockchain.info/charts/hash-rate?timespan=30days&format=json",
            timeout=10
        )
        if resp.status_code == 200:
            values = resp.json().get("values", [])
            if values:
                data["hash_rate"] = round(values[-1].get("y", 0) / 1e9, 1)  # EH/s
                # Check if hash rate is rising (bullish)
                if len(values) >= 7:
                    recent = values[-1].get("y", 0)
                    week_ago = values[-7].get("y", 0)
                    if week_ago:
                        data["hash_rate_7d_change"] = round((recent/week_ago - 1) * 100, 1)
    except Exception as e:
        print(f"Error fetching hash rate: {e}")

    # Exchange reserves (approximate from available sources)
    try:
        resp = requests.get(
            "https://api.blockchain.info/charts/balance?timespan=30days&format=json",
            timeout=10
        )
        # Note: This is total balance, not exchange reserves
    except:
        pass

    # MVRV and NUPL typically require paid APIs (Glassnode, etc.)
    # Add placeholder for when available
    data["mvrv"] = None  # Would need Glassnode/LookIntoBitcoin API
    data["nupl"] = None  # Would need Glassnode/LookIntoBitcoin API
    data["onchain_note"] = "MVRV/NUPL require premium API - using available free metrics"

    return data


def fetch_derivatives_enhanced() -> Dict:
    """
    Fetch enhanced derivatives data for liquidation/positioning analysis.
    Uses Bybit as primary (not geo-blocked), Binance as fallback.
    """
    result = {
        'oi_trend_24h': None,           # OI change over 24h
        'oi_trend_direction': None,     # 'expanding' | 'contracting'
        'funding_trend_8h': None,       # Average funding over 8h
        'funding_direction': None,      # 'rising' | 'falling' | 'stable'
        'predicted_funding': None,      # Estimated next funding based on trend
        'taker_buy_sell_ratio': None,   # Recent taker ratio (proxy for liq pressure)
        'top_trader_sentiment': None,   # Top traders long/short
        'crowded_side': None,           # 'longs' | 'shorts' | 'balanced'
        'liq_proxy_signal': None,       # Liquidation pressure signal
        'data_source': None,            # Track which API provided data
    }

    # 1. Open Interest History (24h trend) - Try Bybit first, then Binance
    try:
        # Bybit OI History
        resp = requests.get(
            "https://api.bybit.com/v5/market/open-interest",
            params={"category": "linear", "symbol": "BTCUSDT", "intervalTime": "1h", "limit": 24},
            headers=HEADERS,
            timeout=10
        )
        data = resp.json()
        if data.get('retCode') == 0:
            oi_list = data.get('result', {}).get('list', [])
            if len(oi_list) >= 2:
                # Bybit returns newest first, so reverse for chronological
                oi_values = [float(d.get('openInterest', 0)) for d in reversed(oi_list)]
                if oi_values[0] > 0:
                    oi_change = (oi_values[-1] / oi_values[0] - 1) * 100
                    result['oi_trend_24h'] = round(oi_change, 2)
                    result['oi_trend_direction'] = 'expanding' if oi_change > 1 else 'contracting' if oi_change < -1 else 'stable'
                    result['data_source'] = 'bybit'
    except Exception as e:
        print(f"Bybit OI error: {e}")

    # Fallback to Binance if Bybit failed
    if result['oi_trend_24h'] is None:
        try:
            resp = requests.get(
                "https://fapi.binance.com/futures/data/openInterestHist",
                params={"symbol": "BTCUSDT", "period": "1h", "limit": 24},
                headers=HEADERS,
                timeout=10
            )
            oi_data = resp.json()
            if isinstance(oi_data, list) and len(oi_data) >= 2:
                oi_values = [float(d.get('sumOpenInterestValue', 0)) for d in oi_data]
                if oi_values[0] > 0:
                    oi_change = (oi_values[-1] / oi_values[0] - 1) * 100
                    result['oi_trend_24h'] = round(oi_change, 2)
                    result['oi_trend_direction'] = 'expanding' if oi_change > 1 else 'contracting' if oi_change < -1 else 'stable'
                    result['data_source'] = 'binance'
        except Exception as e:
            print(f"Binance OI fallback error: {e}")

    # 2. Funding Rate History - Try Bybit first
    try:
        resp = requests.get(
            "https://api.bybit.com/v5/market/funding/history",
            params={"category": "linear", "symbol": "BTCUSDT", "limit": 8},
            headers=HEADERS,
            timeout=10
        )
        data = resp.json()
        if data.get('retCode') == 0:
            funding_list = data.get('result', {}).get('list', [])
            if len(funding_list) >= 2:
                # Bybit returns newest first
                funding_rates = [float(d.get('fundingRate', 0)) * 100 for d in reversed(funding_list)]
                avg_funding = sum(funding_rates) / len(funding_rates)
                current_funding = funding_rates[-1] if funding_rates else 0

                result['funding_trend_8h'] = round(avg_funding, 4)

                # Determine direction
                if len(funding_rates) >= 3:
                    recent_avg = sum(funding_rates[-3:]) / 3
                    older_avg = sum(funding_rates[:3]) / 3
                    if recent_avg > older_avg + 0.002:
                        result['funding_direction'] = 'rising'
                    elif recent_avg < older_avg - 0.002:
                        result['funding_direction'] = 'falling'
                    else:
                        result['funding_direction'] = 'stable'

                # Predict next funding
                if len(funding_rates) >= 2:
                    momentum = funding_rates[-1] - funding_rates[-2]
                    result['predicted_funding'] = round(current_funding + momentum * 0.5, 4)
    except Exception as e:
        print(f"Bybit funding error: {e}")

    # Fallback to Binance for funding
    if result['funding_trend_8h'] is None:
        try:
            resp = requests.get(
                "https://fapi.binance.com/fapi/v1/fundingRate",
                params={"symbol": "BTCUSDT", "limit": 8},
                headers=HEADERS,
                timeout=10
            )
            funding_data = resp.json()
            if isinstance(funding_data, list) and len(funding_data) >= 2:
                funding_rates = [float(d.get('fundingRate', 0)) * 100 for d in funding_data]
                avg_funding = sum(funding_rates) / len(funding_rates)
                current_funding = funding_rates[-1] if funding_rates else 0

                result['funding_trend_8h'] = round(avg_funding, 4)

                if len(funding_rates) >= 3:
                    recent_avg = sum(funding_rates[-3:]) / 3
                    older_avg = sum(funding_rates[:3]) / 3
                    if recent_avg > older_avg + 0.002:
                        result['funding_direction'] = 'rising'
                    elif recent_avg < older_avg - 0.002:
                        result['funding_direction'] = 'falling'
                    else:
                        result['funding_direction'] = 'stable'

                if len(funding_rates) >= 2:
                    momentum = funding_rates[-1] - funding_rates[-2]
                    result['predicted_funding'] = round(current_funding + momentum * 0.5, 4)
        except Exception as e:
            print(f"Binance funding fallback error: {e}")

    # 3. Long/Short Ratio - Try Bybit account-ratio
    try:
        resp = requests.get(
            "https://api.bybit.com/v5/market/account-ratio",
            params={"category": "linear", "symbol": "BTCUSDT", "period": "1h", "limit": 4},
            headers=HEADERS,
            timeout=10
        )
        data = resp.json()
        if data.get('retCode') == 0:
            ratio_list = data.get('result', {}).get('list', [])
            if len(ratio_list) > 0:
                # Use buyRatio as taker buy ratio proxy
                ratios = [float(d.get('buyRatio', 0.5)) / float(d.get('sellRatio', 0.5))
                          if float(d.get('sellRatio', 0.5)) > 0 else 1
                          for d in ratio_list]
                avg_ratio = sum(ratios) / len(ratios)
                result['taker_buy_sell_ratio'] = round(avg_ratio, 4)

                # Also use for top trader sentiment
                latest = ratio_list[0]  # Newest first
                buy_pct = float(latest.get('buyRatio', 0.5)) * 100
                result['top_trader_sentiment'] = {
                    'long_pct': round(buy_pct, 1),
                    'short_pct': round(100 - buy_pct, 1),
                    'ratio': round(avg_ratio, 2)
                }

                # Interpret ratio
                if avg_ratio > 1.3:
                    result['liq_proxy_signal'] = 'heavy short liquidations likely (buyers dominant)'
                    result['crowded_side'] = 'longs elevated'
                elif avg_ratio < 0.75:
                    result['liq_proxy_signal'] = 'heavy long liquidations likely (sellers dominant)'
                    result['crowded_side'] = 'shorts elevated'
                elif avg_ratio > 1.1:
                    result['liq_proxy_signal'] = 'mild short pressure'
                    result['crowded_side'] = 'longs elevated'
                elif avg_ratio < 0.9:
                    result['liq_proxy_signal'] = 'mild long pressure'
                    result['crowded_side'] = 'shorts elevated'
                else:
                    result['liq_proxy_signal'] = 'balanced flow'
                    result['crowded_side'] = 'balanced'
    except Exception as e:
        print(f"Bybit ratio error: {e}")

    # Fallback to Binance for ratios
    if result['taker_buy_sell_ratio'] is None:
        try:
            resp = requests.get(
                "https://fapi.binance.com/futures/data/takerlongshortRatio",
                params={"symbol": "BTCUSDT", "period": "1h", "limit": 4},
                headers=HEADERS,
                timeout=10
            )
            taker_data = resp.json()
            if isinstance(taker_data, list) and len(taker_data) > 0:
                ratios = [float(d.get('buySellRatio', 1)) for d in taker_data]
                avg_ratio = sum(ratios) / len(ratios)
                result['taker_buy_sell_ratio'] = round(avg_ratio, 4)

                if avg_ratio > 1.3:
                    result['liq_proxy_signal'] = 'heavy short liquidations likely (buyers dominant)'
                elif avg_ratio < 0.75:
                    result['liq_proxy_signal'] = 'heavy long liquidations likely (sellers dominant)'
                elif avg_ratio > 1.1:
                    result['liq_proxy_signal'] = 'mild short pressure'
                elif avg_ratio < 0.9:
                    result['liq_proxy_signal'] = 'mild long pressure'
                else:
                    result['liq_proxy_signal'] = 'balanced flow'
        except Exception as e:
            print(f"Binance taker ratio fallback error: {e}")

    if result['top_trader_sentiment'] is None:
        try:
            resp = requests.get(
                "https://fapi.binance.com/futures/data/topLongShortPositionRatio",
                params={"symbol": "BTCUSDT", "period": "1h", "limit": 4},
                headers=HEADERS,
                timeout=10
            )
            position_data = resp.json()
            if isinstance(position_data, list) and len(position_data) > 0:
                latest = position_data[-1]
                long_ratio = float(latest.get('longShortRatio', 1))
                long_acct = float(latest.get('longAccount', 0.5)) * 100

                result['top_trader_sentiment'] = {
                    'long_pct': round(long_acct, 1),
                    'short_pct': round(100 - long_acct, 1),
                    'ratio': round(long_ratio, 2)
                }

                if long_ratio > 2.0:
                    result['crowded_side'] = 'longs crowded (contrarian short)'
                elif long_ratio < 0.6:
                    result['crowded_side'] = 'shorts crowded (contrarian long)'
                elif long_ratio > 1.5:
                    result['crowded_side'] = 'longs elevated'
                elif long_ratio < 0.8:
                    result['crowded_side'] = 'shorts elevated'
                else:
                    result['crowded_side'] = 'balanced'
        except Exception as e:
            print(f"Binance top trader fallback error: {e}")

    return result


def analyze_derivatives_signal(derivatives: Dict, funding_rate: float = None) -> Dict:
    """
    Analyze derivatives data for trading signals.
    Returns bias and reasoning.
    """
    signals_bull = 0
    signals_bear = 0
    reasons = []

    # 1. OI Trend Analysis
    oi_trend = derivatives.get('oi_trend_24h')
    oi_dir = derivatives.get('oi_trend_direction')
    if oi_trend is not None:
        if oi_dir == 'expanding' and oi_trend > 3:
            signals_bull += 1
            reasons.append(f"OI expanding +{oi_trend:.1f}% (fresh positioning)")
        elif oi_dir == 'contracting' and oi_trend < -3:
            reasons.append(f"OI contracting {oi_trend:.1f}% (deleveraging)")

    # 2. Funding Trend
    funding_dir = derivatives.get('funding_direction')
    predicted = derivatives.get('predicted_funding')
    current_funding = funding_rate if funding_rate is not None else 0

    if predicted is not None and current_funding is not None:
        if predicted > current_funding + 0.005:
            signals_bear += 1  # Rising funding = crowded longs
            reasons.append(f"Funding rising toward {predicted:.4f}% (longs getting expensive)")
        elif predicted < current_funding - 0.005:
            signals_bull += 1  # Falling funding = shorts paying
            reasons.append(f"Funding falling toward {predicted:.4f}% (shorts paying)")

    # 3. Taker Ratio (Liquidation Proxy)
    taker_ratio = derivatives.get('taker_buy_sell_ratio')
    liq_signal = derivatives.get('liq_proxy_signal')
    if taker_ratio:
        if taker_ratio > 1.25:
            signals_bull += 1
            reasons.append(f"Taker buy/sell {taker_ratio:.2f}x - {liq_signal}")
        elif taker_ratio < 0.8:
            signals_bear += 1
            reasons.append(f"Taker buy/sell {taker_ratio:.2f}x - {liq_signal}")

    # 4. Crowded Side Analysis
    crowded = derivatives.get('crowded_side')
    if crowded:
        if 'shorts crowded' in crowded:
            signals_bull += 1
            reasons.append(f"Top traders: {crowded}")
        elif 'longs crowded' in crowded:
            signals_bear += 1
            reasons.append(f"Top traders: {crowded}")

    # Determine overall derivatives bias
    if signals_bull > signals_bear + 1:
        bias = 'BULLISH'
    elif signals_bear > signals_bull + 1:
        bias = 'BEARISH'
    elif signals_bull > signals_bear:
        bias = 'SLIGHTLY_BULLISH'
    elif signals_bear > signals_bull:
        bias = 'SLIGHTLY_BEARISH'
    else:
        bias = 'NEUTRAL'

    return {
        'bias': bias,
        'bull_signals': signals_bull,
        'bear_signals': signals_bear,
        'reasons': reasons,
        'has_data': len(reasons) > 0
    }


def fetch_market_data() -> Dict:
    """Fetch all market data from APIs."""
    data = {}

    # BTC Price from CoinGecko
    try:
        resp = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={
                "ids": "bitcoin",
                "vs_currencies": "usd",
                "include_24hr_change": "true",
                "include_24hr_vol": "true",
                "include_market_cap": "true"
            },
            headers=HEADERS,
            timeout=10
        )
        btc = resp.json().get("bitcoin", {})
        data["price"] = btc.get("usd")
        data["change_24h"] = btc.get("usd_24h_change")
        data["volume_24h"] = btc.get("usd_24h_vol")
        data["market_cap"] = btc.get("usd_market_cap")
    except Exception as e:
        print(f"Error fetching BTC price: {e}")

    # Fear & Greed Index
    try:
        resp = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        fng_data = resp.json()
        if isinstance(fng_data, dict) and "data" in fng_data:
            fng_list = fng_data.get("data", [])
            if isinstance(fng_list, list) and len(fng_list) > 0:
                fng = fng_list[0]
                data["fear_greed"] = int(fng.get("value", 0))
                data["fear_greed_label"] = fng.get("value_classification", "Unknown")
    except Exception as e:
        print(f"Error fetching Fear & Greed: {e}")

    # Funding Rate + OI - Try Bybit tickers endpoint (has current funding, not historical)
    try:
        resp = requests.get(
            "https://api.bybit.com/v5/market/tickers",
            params={"category": "linear", "symbol": "BTCUSDT"},
            headers=HEADERS,
            timeout=10
        )
        bybit_data = resp.json()
        print(f"Bybit tickers response: retCode={bybit_data.get('retCode')}")
        if bybit_data.get('retCode') == 0:
            ticker = bybit_data.get('result', {}).get('list', [{}])[0]
            if ticker:
                # Current funding rate
                funding_str = ticker.get("fundingRate")
                if funding_str:
                    rate = float(funding_str)
                    data["funding_rate"] = rate * 100
                    data["funding_annualized"] = rate * 3 * 365 * 100
                    print(f"Bybit funding rate: {rate * 100:.4f}%")
                # Open interest from same endpoint
                oi = ticker.get("openInterest")
                if oi:
                    data["open_interest"] = float(oi)
                    print(f"Bybit OI: {oi}")
    except Exception as e:
        print(f"Bybit tickers error: {e}")

    # Fallback to OKX (works globally)
    if data.get("funding_rate") is None:
        try:
            resp = requests.get(
                "https://www.okx.com/api/v5/public/funding-rate",
                params={"instId": "BTC-USDT-SWAP"},
                headers=HEADERS,
                timeout=10
            )
            okx_data = resp.json()
            if okx_data.get("code") == "0" and okx_data.get("data"):
                rate_str = okx_data["data"][0].get("fundingRate")
                if rate_str:
                    rate = float(rate_str)
                    data["funding_rate"] = rate * 100
                    data["funding_annualized"] = rate * 3 * 365 * 100
                    data["funding_source"] = "okx"
                    print(f"OKX funding rate: {rate * 100:.4f}%")
        except Exception as e:
            print(f"OKX funding error: {e}")

    # Fallback to Bitget
    if data.get("funding_rate") is None:
        try:
            resp = requests.get(
                "https://api.bitget.com/api/v2/mix/market/current-fund-rate",
                params={"symbol": "BTCUSDT", "productType": "USDT-FUTURES"},
                headers=HEADERS,
                timeout=10
            )
            bitget_data = resp.json()
            if bitget_data.get("code") == "00000" and bitget_data.get("data"):
                rate_str = bitget_data["data"][0].get("fundingRate")
                if rate_str:
                    rate = float(rate_str)
                    data["funding_rate"] = rate * 100
                    data["funding_annualized"] = rate * 3 * 365 * 100
                    data["funding_source"] = "bitget"
                    print(f"Bitget funding rate: {rate * 100:.4f}%")
        except Exception as e:
            print(f"Bitget funding error: {e}")

    # Last resort: Binance (may be geo-blocked)
    if data.get("funding_rate") is None:
        try:
            resp = requests.get(
                "https://fapi.binance.com/fapi/v1/fundingRate",
                params={"symbol": "BTCUSDT", "limit": 1},
                headers=HEADERS,
                timeout=10
            )
            funding = resp.json()
            if isinstance(funding, list) and len(funding) > 0 and isinstance(funding[0], dict):
                rate = float(funding[0].get("fundingRate", 0))
                data["funding_rate"] = rate * 100
                data["funding_annualized"] = rate * 3 * 365 * 100
                data["funding_source"] = "binance"
        except Exception as e:
            print(f"Binance funding fallback error: {e}")

    # Open Interest - Skip if already got from tickers, otherwise try Bybit endpoint
    if data.get("open_interest") is None:
        try:
            resp = requests.get(
                "https://api.bybit.com/v5/market/open-interest",
                params={"category": "linear", "symbol": "BTCUSDT", "intervalTime": "5min", "limit": 1},
                headers=HEADERS,
                timeout=10
            )
            bybit_data = resp.json()
            if bybit_data.get('retCode') == 0:
                oi_list = bybit_data.get('result', {}).get('list', [])
                if oi_list:
                    data["open_interest"] = float(oi_list[0].get("openInterest", 0))
                    print(f"Bybit OI (from open-interest endpoint): {data['open_interest']}")
        except Exception as e:
            print(f"Bybit OI error: {e}")

    # Fallback to OKX for OI
    if data.get("open_interest") is None:
        try:
            resp = requests.get(
                "https://www.okx.com/api/v5/public/open-interest",
                params={"instType": "SWAP", "instId": "BTC-USDT-SWAP"},
                headers=HEADERS,
                timeout=10
            )
            okx_data = resp.json()
            if okx_data.get("code") == "0" and okx_data.get("data"):
                oi = okx_data["data"][0].get("oi")
                if oi:
                    data["open_interest"] = float(oi)
                    data["oi_source"] = "okx"
                    print(f"OKX OI: {oi}")
        except Exception as e:
            print(f"OKX OI error: {e}")

    # Fallback to Bitget for OI
    if data.get("open_interest") is None:
        try:
            resp = requests.get(
                "https://api.bitget.com/api/v2/mix/market/open-interest",
                params={"symbol": "BTCUSDT", "productType": "USDT-FUTURES"},
                headers=HEADERS,
                timeout=10
            )
            bitget_data = resp.json()
            if bitget_data.get("code") == "00000" and bitget_data.get("data"):
                oi = bitget_data["data"].get("openInterestList", [{}])[0].get("openInterest")
                if oi:
                    data["open_interest"] = float(oi)
                    data["oi_source"] = "bitget"
                    print(f"Bitget OI: {oi}")
        except Exception as e:
            print(f"Bitget OI error: {e}")

    # Last resort: Binance (may be geo-blocked)
    if data.get("open_interest") is None:
        try:
            resp = requests.get(
                "https://fapi.binance.com/fapi/v1/openInterest",
                params={"symbol": "BTCUSDT"},
                headers=HEADERS,
                timeout=10
            )
            oi_data = resp.json()
            if isinstance(oi_data, dict) and "openInterest" in oi_data:
                data["open_interest"] = float(oi_data.get("openInterest", 0))
                data["oi_source"] = "binance"
        except Exception as e:
            print(f"Binance OI fallback error: {e}")

    # Long/Short Ratio - Try Bybit first
    try:
        resp = requests.get(
            "https://api.bybit.com/v5/market/account-ratio",
            params={"category": "linear", "symbol": "BTCUSDT", "period": "1h", "limit": 1},
            headers=HEADERS,
            timeout=10
        )
        bybit_data = resp.json()
        if bybit_data.get('retCode') == 0:
            ratio_list = bybit_data.get('result', {}).get('list', [])
            if ratio_list:
                buy_ratio = float(ratio_list[0].get("buyRatio", 0.5))
                data["long_pct"] = buy_ratio * 100
                data["short_pct"] = 100 - data["long_pct"]
    except Exception as e:
        print(f"Bybit L/S ratio error: {e}")

    # Fallback to Binance
    if data.get("long_pct") is None:
        try:
            resp = requests.get(
                "https://fapi.binance.com/futures/data/globalLongShortAccountRatio",
                params={"symbol": "BTCUSDT", "period": "1h", "limit": 1},
                headers=HEADERS,
                timeout=10
            )
            ls = resp.json()
            if isinstance(ls, list) and len(ls) > 0 and isinstance(ls[0], dict):
                ratio = float(ls[0].get("longShortRatio", 1))
                data["long_pct"] = ratio / (1 + ratio) * 100
                data["short_pct"] = 100 - data["long_pct"]
        except Exception as e:
            print(f"Binance L/S ratio fallback error: {e}")

    # Technical Indicators (calculated from Binance klines)
    print("Calculating technical indicators from Binance...")
    tech = fetch_technical_indicators()
    data.update(tech)

    # Macro Indicators (DXY, VIX proxies)
    print("Fetching macro indicators...")
    macro = fetch_dxy_vix()
    data.update(macro)

    # On-Chain Metrics
    print("Fetching on-chain metrics...")
    onchain = fetch_onchain_metrics()
    data.update(onchain)

    # Enhanced Derivatives Data (OI trends, funding trends, liquidation proxies)
    print("Fetching enhanced derivatives data...")
    derivatives_enhanced = fetch_derivatives_enhanced()
    data['derivatives_enhanced'] = derivatives_enhanced

    # Analyze derivatives signals
    derivatives_analysis = analyze_derivatives_signal(
        derivatives_enhanced,
        data.get('funding_rate')
    )
    data['derivatives_analysis'] = derivatives_analysis

    # ETF Flow data (if available)
    try:
        # Try to get BTC ETF flow data from available sources
        # Note: Most ETF data requires premium APIs
        data["etf_note"] = "ETF flow data requires premium subscription"
    except:
        pass

    data["timestamp"] = datetime.utcnow().isoformat()
    return data


def analyze_with_claude(market_data: Dict, recent_logs: list) -> Optional[Dict]:
    """Send market data to Claude for analysis."""
    # Read API key at runtime to ensure env var is available
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("No ANTHROPIC_API_KEY set, skipping Claude analysis")
        return generate_rule_based_analysis(market_data)

    # Build context from recent logs
    context = ""
    if recent_logs:
        context = "\n\n## Your Recent Analysis (for context):\n"
        for log in recent_logs[-3:]:
            context += f"\n[{log.get('created_at', 'Unknown')}]\n{log.get('content', '')[:500]}...\n"

    # Safely format numeric values for prompt
    def fmt_price(val): return f"${val:,.0f}" if val else "N/A"
    def fmt_pct(val): return f"{val:.1f}%" if val is not None else "N/A"
    def fmt_num(val): return f"{val:,.0f}" if val else "N/A"

    price = market_data.get('price')
    change = market_data.get('change_24h', 0) or 0
    funding = market_data.get('funding_rate')
    funding_ann = market_data.get('funding_annualized')
    oi = market_data.get('open_interest')
    long_pct = market_data.get('long_pct')
    short_pct = market_data.get('short_pct')
    ma_200 = market_data.get('ma_200')

    # Extract additional data
    rsi = market_data.get('rsi')
    ma_50 = market_data.get('ma_50')
    macd_line = market_data.get('macd_line')
    macd_signal = market_data.get('macd_signal')
    macd_hist = market_data.get('macd_histogram')
    above_200ma = market_data.get('above_200ma')
    pct_from_200ma = market_data.get('pct_from_200ma')
    dxy_proxy = market_data.get('dxy_proxy')
    hash_rate = market_data.get('hash_rate')
    hash_change = market_data.get('hash_rate_7d_change')

    # Enhanced derivatives data
    deriv_enhanced = market_data.get('derivatives_enhanced', {})
    deriv_analysis = market_data.get('derivatives_analysis', {})

    # Build the user prompt with market data
    prompt = f"""## HOURLY MARKET UPDATE - {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}

### STEP 1 - MACRO DATA:
- **DXY Proxy**: {f'{dxy_proxy:.1f}' if dxy_proxy else 'N/A'} (threshold: <105 = bullish, >105 = bearish)
- **Fear & Greed Index**: {market_data.get('fear_greed', 'N/A')} ({market_data.get('fear_greed_label', 'Unknown')})
- *Note: VIX and M2 data require premium feeds - use F&G as risk proxy*

### STEP 2 - ON-CHAIN DATA:
- **Hash Rate**: {f'{hash_rate:.1f} EH/s' if hash_rate else 'N/A'} ({f'{hash_change:+.1f}% 7d' if hash_change else 'N/A'})
- **MVRV Z-Score**: Premium data required (threshold: >3.7 SELL, <1.0 BUY)
- **NUPL**: Premium data required (threshold: >0.75 SELL, <-0.25 BUY)
- *Note: MVRV/NUPL require Glassnode subscription - use hash rate trend as proxy*

### STEP 3 - DERIVATIVES DATA (ENHANCED):
**Current Snapshot:**
- **Funding Rate**: {f'{funding:.4f}%' if funding is not None else 'N/A'} (Annualized: {f'{funding_ann:.1f}%' if funding_ann is not None else 'N/A'})
- **Open Interest**: {fmt_num(oi)} BTC
- **Long/Short Ratio**: {fmt_pct(long_pct)} Long / {fmt_pct(short_pct)} Short

**Trend Analysis (NEW):**
- **OI Trend (24h)**: {f"{deriv_enhanced.get('oi_trend_24h', 'N/A')}%" if deriv_enhanced.get('oi_trend_24h') is not None else 'N/A'} ({deriv_enhanced.get('oi_trend_direction', 'N/A')})
- **Funding Trend (8h avg)**: {f"{deriv_enhanced.get('funding_trend_8h', 'N/A'):.4f}%" if deriv_enhanced.get('funding_trend_8h') is not None else 'N/A'} ({deriv_enhanced.get('funding_direction', 'N/A')})
- **Predicted Next Funding**: {f"{deriv_enhanced.get('predicted_funding', 'N/A'):.4f}%" if deriv_enhanced.get('predicted_funding') is not None else 'N/A'}

**Liquidation Pressure Proxy:**
- **Taker Buy/Sell Ratio**: {f"{deriv_enhanced.get('taker_buy_sell_ratio', 'N/A'):.2f}x" if deriv_enhanced.get('taker_buy_sell_ratio') else 'N/A'}
- **Signal**: {deriv_enhanced.get('liq_proxy_signal', 'N/A')}
- **Top Traders**: {f"{deriv_enhanced.get('top_trader_sentiment', {}).get('long_pct', 'N/A')}% long" if deriv_enhanced.get('top_trader_sentiment') else 'N/A'} ({deriv_enhanced.get('crowded_side', 'N/A')})

**Derivatives Verdict**: {deriv_analysis.get('bias', 'N/A')} ({deriv_analysis.get('bull_signals', 0)} bull / {deriv_analysis.get('bear_signals', 0)} bear signals)
{chr(10).join('- ' + r for r in deriv_analysis.get('reasons', [])) if deriv_analysis.get('reasons') else '- No significant signals'}

*Interpretation*:
- Taker ratio >1.2 = shorts getting squeezed (contrarian long)
- Taker ratio <0.8 = longs getting squeezed (contrarian short)
- OI expanding + price rising = trend continuation
- Funding rising = longs paying more (getting crowded)

### STEP 4 - TECHNICAL DATA:
- **BTC Price**: {fmt_price(price)} ({change:+.2f}% 24h)
- **RSI (14-day)**: {f'{rsi:.1f}' if rsi else 'N/A'} (30=oversold, 70=overbought)
- **200 MA**: {fmt_price(ma_200)} - {'ABOVE ✓ (bullish structure)' if above_200ma else 'BELOW ✗ (bearish structure)' if above_200ma is not None else 'N/A'}
- **50 MA**: {fmt_price(ma_50)}
- **Distance from 200 MA**: {f'{pct_from_200ma:+.1f}%' if pct_from_200ma else 'N/A'}
- **MACD**: Line {macd_line if macd_line else 'N/A'}, Signal {macd_signal if macd_signal else 'N/A'}, Histogram {macd_hist if macd_hist else 'N/A'}
- *MACD Status*: {'Bullish (line > signal)' if macd_line and macd_signal and macd_line > macd_signal else 'Bearish (line < signal)' if macd_line and macd_signal else 'N/A'}
{context}

Using your 4-step trading framework (Macro → On-Chain → Derivatives → Technical), analyze the current market state. Reference the specific thresholds from your knowledge base. Be direct like a senior trader.

IMPORTANT: Even with some premium data unavailable, make assessments based on what IS available. Use proxies where noted.

Cover:
1. Framework assessment at each step with available data
2. Key levels and signals (support, resistance, liquidation clusters)
3. Risk considerations and position sizing thoughts
4. Clear actionable bias with confidence level"""

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-sonnet-4-5",
                "max_tokens": 1500,
                "system": TRADING_EXPERT_SYSTEM,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=90
        )

        if resp.status_code == 200:
            result = resp.json()
            content = result.get("content", [{}])[0].get("text", "")

            # Parse bias and confidence from response
            bias = "NEUTRAL"
            confidence = 5.0
            if "BULLISH" in content.upper():
                bias = "BULLISH"
            elif "BEARISH" in content.upper():
                bias = "BEARISH"

            # Try to extract confidence number
            import re
            conf_match = re.search(r'confidence[:\s]+(\d+)', content.lower())
            if conf_match:
                confidence = float(conf_match.group(1))

            return {
                "content": content,
                "bias": bias,
                "confidence": confidence,
                "sentiment": market_data.get("fear_greed_label", "Unknown")
            }
        else:
            print(f"Claude API error: {resp.status_code} - {resp.text}")
            return generate_rule_based_analysis(market_data)

    except Exception as e:
        print(f"Error calling Claude: {e}")
        return generate_rule_based_analysis(market_data)


def generate_rule_based_analysis(market_data: Dict) -> Dict:
    """Generate analysis using rules when Claude is unavailable."""
    price = market_data.get('price', 0)
    fng = market_data.get('fear_greed', 50)
    funding = market_data.get('funding_rate', 0)
    long_pct = market_data.get('long_pct', 50)
    rsi = market_data.get('rsi')
    ma_200 = market_data.get('ma_200')

    # Enhanced derivatives data
    deriv_analysis = market_data.get('derivatives_analysis', {})
    deriv_enhanced = market_data.get('derivatives_enhanced', {})

    signals_bull = 0
    signals_bear = 0
    points = []

    # Fear & Greed
    if fng and fng <= 25:
        signals_bull += 1
        points.append(f"Extreme Fear ({fng}) - contrarian bullish")
    elif fng and fng >= 75:
        signals_bear += 1
        points.append(f"Extreme Greed ({fng}) - contrarian bearish")

    # Funding
    if funding and funding > 0.10:
        signals_bear += 1
        points.append(f"High funding ({funding:.3f}%) - crowded longs")
    elif funding and funding < -0.05:
        signals_bull += 1
        points.append(f"Negative funding ({funding:.3f}%) - crowded shorts")

    # Long/Short
    if long_pct and long_pct > 60:
        signals_bear += 1
        points.append(f"Longs at {long_pct:.0f}% - potential squeeze")
    elif long_pct and long_pct < 40:
        signals_bull += 1
        points.append(f"Shorts dominant ({100-long_pct:.0f}%) - squeeze setup")

    # RSI
    if rsi and rsi < 30:
        signals_bull += 1
        points.append(f"RSI oversold ({rsi:.1f})")
    elif rsi and rsi > 70:
        signals_bear += 1
        points.append(f"RSI overbought ({rsi:.1f})")

    # 200 MA
    if price and ma_200:
        if price > ma_200:
            signals_bull += 1
            points.append(f"Price above 200 MA (${ma_200:,.0f})")
        else:
            signals_bear += 2
            points.append(f"Price BELOW 200 MA (${ma_200:,.0f}) - bearish structure")

    # Enhanced Derivatives Signals
    if deriv_analysis.get('has_data'):
        deriv_bias = deriv_analysis.get('bias', 'NEUTRAL')
        deriv_reasons = deriv_analysis.get('reasons', [])

        if 'BULLISH' in deriv_bias:
            signals_bull += deriv_analysis.get('bull_signals', 0)
        elif 'BEARISH' in deriv_bias:
            signals_bear += deriv_analysis.get('bear_signals', 0)

        for reason in deriv_reasons[:3]:  # Add top 3 reasons
            points.append(f"[Derivatives] {reason}")

    # Determine bias
    if signals_bull > signals_bear + 1:
        bias = "BULLISH"
    elif signals_bear > signals_bull + 1:
        bias = "BEARISH"
    else:
        bias = "NEUTRAL"

    confidence = min(10, abs(signals_bull - signals_bear) + 3)

    content = f"""**Automated Analysis** (Claude unavailable)

BTC at ${price:,.0f}. Key observations:
{chr(10).join('- ' + p for p in points)}

Signal count: {signals_bull} bullish / {signals_bear} bearish

**Bias: {bias}** | Confidence: {confidence}/10"""

    return {
        "content": content,
        "bias": bias,
        "confidence": confidence,
        "sentiment": market_data.get("fear_greed_label", "Unknown")
    }


def run_analysis():
    """Main analysis routine."""
    print(f"\n{'='*50}")
    print(f"Trading Agent Run: {datetime.utcnow().isoformat()}")
    print(f"{'='*50}\n")

    db = get_database()

    # Fetch market data
    print("Fetching market data...")
    market_data = fetch_market_data()

    # Display fetched data summary
    price = market_data.get('price')
    price_str = f"${price:,.0f}" if price else "N/A"
    print(f"\n📊 DATA SUMMARY:")
    print(f"  Price: {price_str}")
    print(f"  Fear & Greed: {market_data.get('fear_greed', 'N/A')} ({market_data.get('fear_greed_label', 'N/A')})")

    rsi = market_data.get('rsi')
    print(f"  RSI: {f'{rsi:.1f}' if rsi else 'N/A'}")

    ma_200 = market_data.get('ma_200')
    above = market_data.get('above_200ma')
    print(f"  200 MA: {f'${ma_200:,.0f}' if ma_200 else 'N/A'} ({'Above ✓' if above else 'Below ✗' if above is not None else 'N/A'})")

    macd_hist = market_data.get('macd_histogram')
    print(f"  MACD Histogram: {macd_hist if macd_hist else 'N/A'}")

    funding = market_data.get('funding_rate')
    print(f"  Funding Rate: {f'{funding:.4f}%' if funding else 'N/A'}")

    # Get recent logs for context
    recent_logs = db.get_agent_logs(limit=5, log_type='analysis')
    print(f"Found {len(recent_logs)} recent analyses for context")

    # Analyze with Claude
    print("\nAnalyzing with Claude...")
    analysis = analyze_with_claude(market_data, recent_logs)

    if analysis:
        # Save to database
        log_id = db.save_agent_log(
            content=analysis["content"],
            log_type="analysis",
            symbol="BTCUSD",
            title=f"Hourly Analysis - {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}",
            market_data=market_data,
            sentiment=analysis.get("sentiment"),
            bias=analysis.get("bias"),
            confidence=analysis.get("confidence")
        )
        print(f"\nAnalysis saved (ID: {log_id})")
        print(f"Bias: {analysis.get('bias')} | Confidence: {analysis.get('confidence')}/10")
        print(f"\n{analysis['content'][:500]}...")

        # Also post to server endpoint
        try:
            requests.post(
                f"{SERVER_URL}/api/agent/log",
                json={
                    "content": analysis["content"],
                    "bias": analysis.get("bias"),
                    "confidence": analysis.get("confidence"),
                    "market_data": market_data
                },
                timeout=10
            )
        except:
            pass  # Server might not have this endpoint yet

    print(f"\n{'='*50}")
    print("Analysis complete")
    print(f"{'='*50}\n")


if __name__ == "__main__":
    run_analysis()
