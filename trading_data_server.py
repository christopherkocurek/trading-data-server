"""
Trading Data Server
Receives TradingView webhooks and serves data to Claude via MCP

Requirements:
    pip install fastapi uvicorn requests

Run locally:
    uvicorn trading-data-server:app --host 0.0.0.0 --port 8080

For production, deploy to Railway, Render, or DigitalOcean.
"""

from fastapi import FastAPI, Request, HTTPException, Query, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, Dict, Any, List
from datetime import datetime
import json
import os

# Import our modules
from database import get_database, TradingDatabase
from signal_detector import get_signal_detector, SignalDetector
from exchanges import get_exchange_manager, ExchangeManager

app = FastAPI(title="Trading Data Server", version="2.0.0")

# CORS for local development and KocurekFi
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Singletons
db: TradingDatabase = get_database()
signal_detector: SignalDetector = get_signal_detector()
exchange_manager: ExchangeManager = get_exchange_manager()

# Webhook secret for TradingView (set in environment)
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "your-secret-here")

# ============================================
# TRADINGVIEW WEBHOOK RECEIVER
# ============================================

class TradingViewWebhook(BaseModel):
    """Expected format from TradingView alert webhook"""
    symbol: str = "BTCUSD"
    indicator: str  # "RSI", "MACD", "MA200", "ATR", "VOLUME", "BB", "PRICE"
    value: Optional[float] = None
    value2: Optional[float] = None  # For MACD signal, BB bands, etc.
    value3: Optional[float] = None  # For MACD histogram
    timeframe: str = "1D"
    message: Optional[str] = None
    secret: Optional[str] = None


@app.post("/webhook/tradingview")
async def receive_tradingview_webhook(webhook: TradingViewWebhook, background_tasks: BackgroundTasks):
    """
    Receives webhook from TradingView alerts.

    TradingView Alert Message Format:
    {"symbol":"BTCUSD","indicator":"RSI","value":{{plot_0}},"timeframe":"1D","secret":"your-secret"}
    """

    # Verify secret (optional but recommended)
    if WEBHOOK_SECRET != "your-secret-here" and webhook.secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid webhook secret")

    symbol = webhook.symbol.upper()
    timestamp = datetime.utcnow().isoformat()

    # Map indicator names to storage keys
    indicator_map = {
        "RSI": f"rsi_{webhook.timeframe.lower()}",
        "MACD_LINE": "macd_line",
        "MACD_SIGNAL": "macd_signal",
        "MACD_HISTOGRAM": "macd_histogram",
        "MACD": "macd_line",  # If sending all MACD values
        "MA200": "ma_200",
        "200MA": "ma_200",
        "ATR": "atr_14",
        "VOLUME": "volume_ratio",
        "BB_UPPER": "bb_upper",
        "BB_LOWER": "bb_lower",
        "PRICE": "price",
    }

    indicator_key = indicator_map.get(webhook.indicator.upper(), webhook.indicator.lower())

    # Save to database with history
    db.save_indicator(
        symbol=symbol,
        indicator_name=indicator_key,
        value=webhook.value,
        timeframe=webhook.timeframe,
        value2=webhook.value2,
        value3=webhook.value3,
        source='tradingview'
    )

    # Handle multi-value indicators
    if webhook.indicator.upper() == "MACD" and webhook.value2 is not None:
        db.save_indicator(symbol, "macd_signal", webhook.value2, webhook.timeframe)
        if webhook.value3 is not None:
            db.save_indicator(symbol, "macd_histogram", webhook.value3, webhook.timeframe)

    if webhook.indicator.upper() in ["BB", "BOLLINGER"]:
        if webhook.value is not None:
            db.save_indicator(symbol, "bb_upper", webhook.value, webhook.timeframe)
        if webhook.value2 is not None:
            db.save_indicator(symbol, "bb_lower", webhook.value2, webhook.timeframe)

    # Check for signals in background
    background_tasks.add_task(check_for_signals, symbol)

    return {
        "status": "ok",
        "symbol": symbol,
        "indicator": indicator_key,
        "value": webhook.value,
        "timestamp": timestamp
    }


async def check_for_signals(symbol: str):
    """Check for trading signals after receiving new indicator data."""
    try:
        # Get latest indicators
        latest = db.get_latest_indicators(symbol)
        indicators = {}

        for name, data in latest.get('indicators', {}).items():
            indicators[name] = data.get('value')

        # Run signal detection
        signals = signal_detector.check_all_signals(symbol, indicators)

        if signals:
            print(f"Detected {len(signals)} signals for {symbol}")
    except Exception as e:
        print(f"Signal detection error: {e}")


# ============================================
# DATA ENDPOINTS (For Claude/KocurekFi)
# ============================================

@app.get("/api/indicators/{symbol}")
async def get_indicators(symbol: str = "BTCUSD"):
    """Get all current indicators for a symbol"""
    symbol = symbol.upper()
    return db.get_latest_indicators(symbol)


@app.get("/api/indicators/{symbol}/{indicator}")
async def get_indicator(symbol: str, indicator: str):
    """Get a specific indicator value"""
    symbol = symbol.upper()
    indicator = indicator.lower()

    latest = db.get_latest_indicators(symbol)
    indicators = latest.get('indicators', {})

    if indicator not in indicators:
        raise HTTPException(status_code=404, detail=f"No {indicator} data for {symbol}")

    return {
        "symbol": symbol,
        "indicator": indicator,
        "value": indicators[indicator].get('value'),
        "timeframe": indicators[indicator].get('timeframe'),
        "last_updated": latest.get('last_updated')
    }


@app.get("/api/indicators/{symbol}/{indicator}/history")
async def get_indicator_history(
    symbol: str,
    indicator: str,
    hours: int = Query(default=24, ge=1, le=168)
):
    """Get historical values for an indicator"""
    symbol = symbol.upper()
    indicator = indicator.lower()

    history = db.get_indicator_history(symbol, indicator, hours)
    return {
        "symbol": symbol,
        "indicator": indicator,
        "hours": hours,
        "history": history
    }


@app.get("/api/signals")
async def get_signals(
    symbol: str = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100)
):
    """Get recent trading signals"""
    signals = db.get_signals(symbol=symbol.upper() if symbol else None, limit=limit)
    return {"signals": signals}


@app.post("/api/signals/{signal_id}/acknowledge")
async def acknowledge_signal(signal_id: int):
    """Mark a signal as acknowledged"""
    success = db.acknowledge_signal(signal_id)
    if not success:
        raise HTTPException(status_code=404, detail="Signal not found")
    return {"status": "acknowledged"}


# ============================================
# POSITION & BALANCE ENDPOINTS
# ============================================

@app.get("/api/positions")
async def get_positions(exchange: str = Query(default=None)):
    """Get current positions (from exchange API)"""
    positions = db.get_positions(exchange)
    return {"positions": positions}


@app.get("/api/balances")
async def get_balances(exchange: str = Query(default=None)):
    """Get exchange balances"""
    balances = db.get_balances(exchange)
    return {"balances": balances}


@app.get("/api/exchanges/status")
async def get_exchange_status():
    """Get connection status for all exchanges"""
    return exchange_manager.get_status()


@app.post("/api/exchanges/sync")
async def sync_exchanges(background_tasks: BackgroundTasks):
    """Trigger exchange data sync"""
    background_tasks.add_task(do_exchange_sync)
    return {"status": "sync_started"}


async def do_exchange_sync():
    """Background task to sync exchange data"""
    try:
        results = exchange_manager.sync_all()
        print(f"Exchange sync complete: {results}")
    except Exception as e:
        print(f"Exchange sync error: {e}")


# ============================================
# SUMMARY ENDPOINT (Main endpoint for Claude)
# ============================================

@app.get("/api/summary")
async def get_summary(symbol: str = Query(default="BTCUSD")):
    """Get complete trading summary for Claude"""
    symbol = symbol.upper()
    summary = db.get_summary(symbol)

    # Add exchange total value
    summary['exchange_value'] = exchange_manager.get_total_value()

    return summary


# ============================================
# HEALTH CHECK
# ============================================

@app.get("/health")
async def health_check():
    # Count data points
    btc_indicators = db.get_latest_indicators("BTCUSD")
    data_points = len(btc_indicators.get('indicators', {}))

    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "indicators_tracked": ["BTCUSD"],
        "data_points": data_points,
        "exchanges": exchange_manager.get_status()
    }


# ============================================
# MCP INTEGRATION (For Claude Code)
# ============================================

@app.get("/mcp/tools")
async def mcp_tools():
    """
    Returns tool definitions for MCP integration.
    Claude Code can use these as function calls.
    """
    return {
        "tools": [
            {
                "name": "get_btc_indicators",
                "description": "Get current BTC technical indicators (RSI, MACD, 200MA, ATR, etc.)",
                "parameters": {}
            },
            {
                "name": "get_trading_signals",
                "description": "Get recent trading signals and alerts",
                "parameters": {}
            },
            {
                "name": "get_trading_positions",
                "description": "Get current open positions and P&L",
                "parameters": {}
            },
            {
                "name": "get_trading_summary",
                "description": "Get complete trading summary including indicators, signals, and positions",
                "parameters": {}
            }
        ]
    }


# ============================================
# MAINTENANCE
# ============================================

@app.post("/api/maintenance/cleanup")
async def cleanup_old_data(days: int = Query(default=30, ge=1, le=365)):
    """Remove old indicator history"""
    deleted = db.cleanup_old_data(days)
    return {"status": "ok", "deleted_rows": deleted}


# ============================================
# STARTUP
# ============================================

if __name__ == "__main__":
    import uvicorn
    print("""
    ╔═══════════════════════════════════════════════════════════╗
    ║           TRADING DATA SERVER v2.0                        ║
    ╠═══════════════════════════════════════════════════════════╣
    ║  Webhook URL: http://localhost:8080/webhook/tradingview   ║
    ║  API Base:    http://localhost:8080/api                   ║
    ║  Health:      http://localhost:8080/health                ║
    ╚═══════════════════════════════════════════════════════════╝

    Features:
    - SQLite persistence for indicator history
    - Signal detection (RSI, MACD, 200MA, BB)
    - Exchange integration (Coinbase, Binance)

    TradingView Alert Message Format:
    {"symbol":"BTCUSD","indicator":"RSI","value":{{plot_0}},"timeframe":"1D"}

    Supported indicators: RSI, MACD, MA200, ATR, VOLUME, BB, PRICE
    """)
    uvicorn.run(app, host="0.0.0.0", port=8080)
