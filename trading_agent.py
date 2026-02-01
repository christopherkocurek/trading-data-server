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


def fetch_binance_klines(symbol: str = "BTCUSDT", interval: str = "1d", limit: int = 250) -> list:
    """Fetch kline data from Binance."""
    try:
        resp = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=10
        )
        return resp.json()
    except Exception as e:
        print(f"Error fetching Binance klines: {e}")
        return []


def fetch_technical_indicators() -> Dict:
    """Calculate technical indicators from Binance klines."""
    data = {}

    klines = fetch_binance_klines()
    if not klines:
        return data

    closes = [float(k[4]) for k in klines]  # Close prices

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
    current_price = closes[-1]
    if data.get("ma_200"):
        data["above_200ma"] = current_price > data["ma_200"]
        data["pct_from_200ma"] = round((current_price / data["ma_200"] - 1) * 100, 1)

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
        fng = resp.json().get("data", [{}])[0]
        data["fear_greed"] = int(fng.get("value", 0))
        data["fear_greed_label"] = fng.get("value_classification", "Unknown")
    except Exception as e:
        print(f"Error fetching Fear & Greed: {e}")

    # Binance Funding Rate
    try:
        resp = requests.get(
            "https://fapi.binance.com/fapi/v1/fundingRate",
            params={"symbol": "BTCUSDT", "limit": 1},
            timeout=10
        )
        funding = resp.json()
        if funding:
            rate = float(funding[0].get("fundingRate", 0))
            data["funding_rate"] = rate * 100
            data["funding_annualized"] = rate * 3 * 365 * 100
    except Exception as e:
        print(f"Error fetching funding rate: {e}")

    # Open Interest
    try:
        resp = requests.get(
            "https://fapi.binance.com/fapi/v1/openInterest",
            params={"symbol": "BTCUSDT"},
            timeout=10
        )
        data["open_interest"] = float(resp.json().get("openInterest", 0))
    except Exception as e:
        print(f"Error fetching OI: {e}")

    # Long/Short Ratio
    try:
        resp = requests.get(
            "https://fapi.binance.com/futures/data/globalLongShortAccountRatio",
            params={"symbol": "BTCUSDT", "period": "1h", "limit": 1},
            timeout=10
        )
        ls = resp.json()
        if ls:
            ratio = float(ls[0].get("longShortRatio", 1))
            data["long_pct"] = ratio / (1 + ratio) * 100
            data["short_pct"] = 100 - data["long_pct"]
    except Exception as e:
        print(f"Error fetching L/S ratio: {e}")

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

### STEP 3 - DERIVATIVES DATA:
- **Funding Rate**: {f'{funding:.4f}%' if funding is not None else 'N/A'} (Annualized: {f'{funding_ann:.1f}%' if funding_ann is not None else 'N/A'})
- **Open Interest**: {fmt_num(oi)} BTC
- **Long/Short Ratio**: {fmt_pct(long_pct)} Long / {fmt_pct(short_pct)} Short
- *Interpretation*: {'>0.10% = crowded longs, <-0.05% = crowded shorts' if funding else 'N/A'}

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
                "model": "claude-3-5-sonnet-latest",
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
