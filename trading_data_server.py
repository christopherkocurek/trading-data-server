"""
Trading Data Server
Receives TradingView webhooks and serves data to Claude via MCP

Requirements:
    pip install fastapi uvicorn requests apscheduler

Run locally:
    uvicorn trading_data_server:app --host 0.0.0.0 --port 8080

For production, deploy to Railway, Render, or DigitalOcean.
"""

from fastapi import FastAPI, Request, HTTPException, Query, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from pydantic import BaseModel
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone
import json
import os
import threading

# Import our modules
from database import get_database, TradingDatabase
from signal_detector import get_signal_detector, SignalDetector
from exchanges import get_exchange_manager, ExchangeManager

# Scheduler for autonomous agent
scheduler = None

def run_trading_agent():
    """Run the trading agent analysis (called by scheduler)."""
    print(f"\n[{datetime.now(timezone.utc).isoformat()}] Running scheduled trading agent...")
    try:
        from trading_agent import run_analysis
        run_analysis()
    except Exception as e:
        print(f"Agent error: {e}")

def start_scheduler():
    """Start the APScheduler for hourly agent runs."""
    global scheduler
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger

        scheduler = BackgroundScheduler()

        # Run at the top of every hour
        scheduler.add_job(
            run_trading_agent,
            CronTrigger(minute=0),  # Every hour at :00
            id='trading_agent',
            name='Hourly Trading Analysis',
            replace_existing=True
        )

        # Also run once at startup (after 30 seconds to let server initialize)
        scheduler.add_job(
            run_trading_agent,
            'date',
            run_date=datetime.now(timezone.utc).replace(second=0, microsecond=0),
            id='trading_agent_startup'
        )

        scheduler.start()
        print("Scheduler started - Trading agent will run every hour at :00")
    except ImportError:
        print("APScheduler not installed - agent scheduling disabled")
    except Exception as e:
        print(f"Scheduler error: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    print("Starting Trading Data Server...")
    start_scheduler()
    yield
    # Shutdown
    if scheduler:
        scheduler.shutdown()
        print("Scheduler stopped")

app = FastAPI(title="Trading Data Server", version="2.1.0", lifespan=lifespan)

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
# AGENT LOGS & CHAT
# ============================================

class AgentLogRequest(BaseModel):
    content: str
    log_type: str = "analysis"
    symbol: str = "BTCUSD"
    title: str = None
    market_data: Dict = None
    sentiment: str = None
    bias: str = None
    confidence: float = None


class ChatRequest(BaseModel):
    message: str


@app.post("/api/agent/log")
async def post_agent_log(log: AgentLogRequest):
    """Save an agent analysis log."""
    log_id = db.save_agent_log(
        content=log.content,
        log_type=log.log_type,
        symbol=log.symbol,
        title=log.title,
        market_data=log.market_data,
        sentiment=log.sentiment,
        bias=log.bias,
        confidence=log.confidence
    )
    return {"status": "ok", "log_id": log_id}


@app.get("/api/agent/logs")
async def get_agent_logs(
    limit: int = Query(default=20, ge=1, le=100),
    log_type: str = Query(default=None),
    hours: int = Query(default=None, ge=1, le=168)
):
    """Get agent analysis logs."""
    logs = db.get_agent_logs(limit=limit, log_type=log_type, hours=hours)
    return {"logs": logs}


@app.get("/api/agent/latest")
async def get_latest_analysis():
    """Get the most recent agent analysis."""
    analysis = db.get_latest_agent_analysis()
    return {"analysis": analysis}


@app.post("/api/agent/chat")
async def chat_with_agent(chat: ChatRequest):
    """Chat with the trading agent."""
    import os
    import requests as req

    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

    if not ANTHROPIC_API_KEY:
        return {"error": "ANTHROPIC_API_KEY not configured", "response": None}

    # Save user message
    db.save_chat_message("user", chat.message)

    # Get context: recent logs and chat history
    recent_logs = db.get_agent_logs(limit=5, log_type='analysis')
    chat_history = db.get_chat_history(limit=20)

    # Get current market data
    summary = db.get_summary("BTCUSD")

    # Use the trading expert system prompt
    from trading_agent import TRADING_EXPERT_SYSTEM
    system = TRADING_EXPERT_SYSTEM + "\n\nYou are chatting with a trader. Use your recent analyses for context. Be helpful and actionable."

    # Build messages
    messages = []

    # Add context about recent analyses
    if recent_logs:
        context = "Your recent analyses:\n"
        for log in recent_logs[-3:]:
            context += f"\n[{log.get('created_at', '')}] Bias: {log.get('bias', 'N/A')}\n"
            context += log.get('content', '')[:500] + "...\n"
        messages.append({"role": "user", "content": f"[CONTEXT - Recent Analyses]\n{context}"})
        messages.append({"role": "assistant", "content": "I have my recent analyses loaded for context."})

    # Add chat history
    for msg in chat_history[-10:]:
        messages.append({"role": msg["role"], "content": msg["content"]})

    # Add current message with market data
    price = summary.get('price')
    price_str = f"${price:,.0f}" if price else "N/A"
    rsi = summary.get('indicators', {}).get('rsi_daily', 'N/A')
    above_ma = summary.get('indicators', {}).get('above_200ma', 'N/A')

    current_msg = f"""Current market snapshot:
- BTC: {price_str}
- RSI: {rsi}
- Above 200 MA: {above_ma}

User question: {chat.message}"""

    messages.append({"role": "user", "content": current_msg})

    try:
        resp = req.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-3-5-sonnet-20241022",
                "max_tokens": 1024,
                "system": system,
                "messages": messages
            },
            timeout=60
        )

        if resp.status_code == 200:
            result = resp.json()
            response_text = result.get("content", [{}])[0].get("text", "")

            # Save assistant response
            db.save_chat_message("assistant", response_text)

            return {"response": response_text}
        else:
            return {"error": f"API error: {resp.status_code}", "response": None}

    except Exception as e:
        return {"error": str(e), "response": None}


@app.get("/api/agent/chat/history")
async def get_chat_history(limit: int = Query(default=50, ge=1, le=200)):
    """Get chat history."""
    history = db.get_chat_history(limit=limit)
    return {"history": history}


@app.delete("/api/agent/chat/clear")
async def clear_chat_history():
    """Clear chat history."""
    db.clear_chat_history()
    return {"status": "ok"}


@app.post("/api/agent/run")
async def trigger_agent_run(background_tasks: BackgroundTasks):
    """Manually trigger an agent analysis run."""
    background_tasks.add_task(run_trading_agent)
    return {"status": "started", "message": "Agent analysis triggered"}


@app.get("/api/agent/status")
async def get_agent_status():
    """Get agent scheduler status."""
    global scheduler

    status = {
        "scheduler_running": scheduler is not None and scheduler.running if scheduler else False,
        "anthropic_key_set": bool(os.getenv("ANTHROPIC_API_KEY")),
        "next_run": None,
        "jobs": []
    }

    if scheduler and scheduler.running:
        jobs = scheduler.get_jobs()
        for job in jobs:
            status["jobs"].append({
                "id": job.id,
                "name": job.name,
                "next_run": job.next_run_time.isoformat() if job.next_run_time else None
            })
        if jobs:
            status["next_run"] = min(j.next_run_time for j in jobs if j.next_run_time).isoformat()

    # Get latest analysis
    latest = db.get_latest_agent_analysis()
    if latest:
        status["last_analysis"] = {
            "time": latest.get("created_at"),
            "bias": latest.get("bias"),
            "confidence": latest.get("confidence")
        }

    return status


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
