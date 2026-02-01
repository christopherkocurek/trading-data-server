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

    # Get TradingView indicators from our server
    try:
        resp = requests.get(f"{SERVER_URL}/api/indicators/BTCUSD", timeout=10)
        indicators = resp.json().get("indicators", {})
        if indicators:
            data["rsi"] = indicators.get("rsi_1d", {}).get("value")
            data["ma_200"] = indicators.get("ma_200", {}).get("value")
            data["macd_line"] = indicators.get("macd_line", {}).get("value")
            data["macd_signal"] = indicators.get("macd_signal", {}).get("value")
            data["macd_histogram"] = indicators.get("macd_histogram", {}).get("value")
    except Exception as e:
        print(f"Error fetching server indicators: {e}")

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

    # Build the prompt
    prompt = f"""You are an autonomous BTC trading analyst running every hour. Analyze the current market and provide your thoughts.

## Current Market Data:
- BTC Price: ${market_data.get('price', 'N/A'):,.0f} ({market_data.get('change_24h', 0):+.2f}% 24h)
- Fear & Greed: {market_data.get('fear_greed', 'N/A')} ({market_data.get('fear_greed_label', 'Unknown')})
- Funding Rate: {market_data.get('funding_rate', 'N/A'):.4f}% (Annualized: {market_data.get('funding_annualized', 'N/A'):.1f}%)
- Open Interest: {market_data.get('open_interest', 'N/A'):,.0f} BTC
- Long/Short: {market_data.get('long_pct', 'N/A'):.1f}% Long / {market_data.get('short_pct', 'N/A'):.1f}% Short
- RSI (Daily): {market_data.get('rsi', 'N/A')}
- 200 MA: ${market_data.get('ma_200', 'N/A'):,.0f}
- MACD: Line {market_data.get('macd_line', 'N/A')}, Signal {market_data.get('macd_signal', 'N/A')}, Histogram {market_data.get('macd_histogram', 'N/A')}
{context}

Write a brief market commentary (2-3 paragraphs) covering:
1. What's happening right now (price action, sentiment)
2. Key levels to watch
3. Your current bias and confidence level

Be direct and conversational. End with a clear bias statement: BULLISH, BEARISH, or NEUTRAL and confidence 1-10."""

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=60
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
    print(f"Price: ${market_data.get('price', 'N/A'):,.0f}")
    print(f"Fear & Greed: {market_data.get('fear_greed', 'N/A')}")

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
