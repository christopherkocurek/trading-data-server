# Trading Data Server

Cloud server that receives TradingView webhooks and serves real-time data to Claude.

## Architecture

```
TradingView Alerts ──webhook──▶ Your Server ◀──API──▶ Claude Code
                                    │
                                    ▼
                              Exchange APIs
                           (Binance, Coinbase)
```

## Quick Start (Local)

```bash
# Install dependencies
pip install fastapi uvicorn redis python-binance

# Run server
cd /Users/christopherkocurek/trading-expert-research/server
python trading-data-server.py

# Server runs at http://localhost:8080
```

## TradingView Alert Setup

1. **Add the Pine Script indicator** to your BTCUSD chart:
   - Open TradingView → Pine Editor
   - Paste contents of `tradingview-alerts.pine`
   - Add to chart

2. **Create webhook alerts** (requires TradingView Pro+):
   - Right-click indicator → Add Alert
   - Condition: "Periodic Update" (or specific condition)
   - Webhook URL: `https://your-server.com/webhook/tradingview`
   - Message: `{"symbol":"BTCUSD","indicator":"RSI","value":{{plot_0}},"timeframe":"1D"}`

3. **Create alerts for each indicator:**

   | Indicator | Alert Message |
   |-----------|---------------|
   | RSI | `{"symbol":"BTCUSD","indicator":"RSI","value":{{plot_0}},"timeframe":"1D"}` |
   | Price | `{"symbol":"BTCUSD","indicator":"PRICE","value":{{close}},"timeframe":"1D"}` |
   | 200 MA | `{"symbol":"BTCUSD","indicator":"MA200","value":VALUE,"timeframe":"1D"}` |

## Deploy to Cloud

### Option 1: Railway (Easiest)

```bash
# Install Railway CLI
npm install -g @railway/cli

# Login and deploy
railway login
railway init
railway up
```

### Option 2: DigitalOcean App Platform

1. Push code to GitHub
2. Connect repo to DigitalOcean Apps
3. Set environment variables:
   - `WEBHOOK_SECRET=your-secret`
   - `BINANCE_API_KEY=xxx` (optional)
   - `BINANCE_SECRET=xxx` (optional)

### Option 3: Docker

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY trading-data-server.py .
CMD ["uvicorn", "trading-data-server:app", "--host", "0.0.0.0", "--port", "8080"]
```

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/indicators/BTCUSD` | All indicators for BTC |
| `GET /api/indicators/BTCUSD/rsi_1d` | Specific indicator |
| `GET /api/positions` | Open positions |
| `GET /api/summary` | Complete trading summary |
| `POST /webhook/tradingview` | Receive TradingView alerts |
| `GET /health` | Health check |

## Connect to Claude

Update the trading-expert skill to fetch from your server:

```bash
# Add to skill's data fetching section:
curl -s "https://your-server.com/api/summary" | jq '.'
```

## Exchange Integration

To fetch real positions from Binance:

1. Create read-only API key at binance.com
2. Set environment variables:
   ```
   BINANCE_API_KEY=your-key
   BINANCE_SECRET=your-secret
   ```
3. Uncomment the exchange section in `trading-data-server.py`

## Security

- Use HTTPS in production
- Set a strong `WEBHOOK_SECRET`
- Use read-only exchange API keys
- Never expose write permissions

## Costs

| Service | Cost |
|---------|------|
| Railway | Free tier available, ~$5/mo for always-on |
| DigitalOcean | $5/mo for basic droplet |
| TradingView Pro+ | Required for webhooks (~$15/mo) |
| Binance API | Free |

## Next Steps

1. Deploy server to cloud
2. Set up TradingView alerts with webhook URL
3. Update trading-expert skill with your server URL
4. (Optional) Add exchange API integration
5. (Optional) Add Redis for persistence across restarts
