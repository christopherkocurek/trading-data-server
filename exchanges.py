"""
Exchange API Connectors for Trading Data Server
Supports Coinbase and Binance (extensible to others)
"""

import os
from typing import Dict, List, Optional
from datetime import datetime
from database import get_database
import requests


class ExchangeConnector:
    """Base class for exchange connections."""

    def __init__(self):
        self.db = get_database()

    def sync_positions(self) -> Dict:
        """Override in subclass to sync positions."""
        raise NotImplementedError

    def sync_balances(self) -> Dict:
        """Override in subclass to sync balances."""
        raise NotImplementedError


class CoinbaseConnector(ExchangeConnector):
    """
    Coinbase Advanced Trade API connector.
    Uses the same auth pattern as KocurekFi.
    """

    def __init__(self, api_key: str = None, api_secret: str = None):
        super().__init__()
        self.api_key = api_key or os.getenv('COINBASE_API_KEY')
        self.api_secret = api_secret or os.getenv('COINBASE_API_SECRET')
        self.client = None
        self._init_client()

    def _init_client(self):
        """Initialize Coinbase client if credentials available."""
        if self.api_key and self.api_secret:
            try:
                from coinbase.rest import RESTClient
                self.client = RESTClient(
                    api_key=self.api_key,
                    api_secret=self.api_secret
                )
            except ImportError:
                print("coinbase-advanced-py not installed")
            except Exception as e:
                print(f"Coinbase client init failed: {e}")

    def is_connected(self) -> bool:
        """Check if Coinbase client is available."""
        return self.client is not None

    def sync_balances(self) -> Dict:
        """Sync Coinbase account balances."""
        if not self.client:
            return {"error": "Coinbase not connected", "balances": []}

        try:
            accounts = self.client.get_accounts()
            balances = []

            for account in accounts.get('accounts', []):
                balance = float(account.get('available_balance', {}).get('value', 0))
                currency = account.get('currency', 'UNKNOWN')

                if balance > 0:
                    # Get USD value
                    usd_value = self._get_usd_value(currency, balance)

                    self.db.upsert_balance(
                        exchange='coinbase',
                        asset=currency,
                        free=balance,
                        locked=0,
                        usd_value=usd_value
                    )

                    balances.append({
                        'asset': currency,
                        'free': balance,
                        'usd_value': usd_value
                    })

            return {
                "exchange": "coinbase",
                "balances": balances,
                "synced_at": datetime.utcnow().isoformat()
            }

        except Exception as e:
            return {"error": str(e), "balances": []}

    def _get_usd_value(self, currency: str, amount: float) -> Optional[float]:
        """Get USD value for a currency amount."""
        if currency in ['USD', 'USDC', 'USDT']:
            return amount

        try:
            response = requests.get(
                f"https://api.coinbase.com/v2/prices/{currency}-USD/spot",
                timeout=5
            )
            if response.status_code == 200:
                price = float(response.json()['data']['amount'])
                return amount * price
        except:
            pass

        return None

    def get_portfolio_value(self) -> Dict:
        """Get total portfolio value."""
        if not self.client:
            return {"error": "Coinbase not connected"}

        try:
            accounts = self.client.get_accounts()
            total_usd = 0
            holdings = []

            for account in accounts.get('accounts', []):
                balance = float(account.get('available_balance', {}).get('value', 0))
                currency = account.get('currency', 'UNKNOWN')

                if balance > 0:
                    usd_value = self._get_usd_value(currency, balance) or 0
                    total_usd += usd_value
                    holdings.append({
                        'currency': currency,
                        'balance': balance,
                        'usd_value': usd_value
                    })

            return {
                "exchange": "coinbase",
                "total_usd_value": total_usd,
                "holdings": holdings,
                "timestamp": datetime.utcnow().isoformat()
            }

        except Exception as e:
            return {"error": str(e)}


class BinanceConnector(ExchangeConnector):
    """
    Binance API connector for spot and futures.
    """

    def __init__(self, api_key: str = None, api_secret: str = None):
        super().__init__()
        self.api_key = api_key or os.getenv('BINANCE_API_KEY')
        self.api_secret = api_secret or os.getenv('BINANCE_API_SECRET')
        self.client = None
        self._init_client()

    def _init_client(self):
        """Initialize Binance client if credentials available."""
        if self.api_key and self.api_secret:
            try:
                from binance.client import Client
                self.client = Client(self.api_key, self.api_secret)
            except ImportError:
                print("python-binance not installed")
            except Exception as e:
                print(f"Binance client init failed: {e}")

    def is_connected(self) -> bool:
        """Check if Binance client is available."""
        return self.client is not None

    def sync_positions(self) -> Dict:
        """Sync Binance futures positions."""
        if not self.client:
            return {"error": "Binance not connected", "positions": []}

        try:
            # Clear old positions first
            self.db.clear_positions('binance')

            # Get futures account
            futures = self.client.futures_account()
            positions = []

            for pos in futures.get('positions', []):
                qty = float(pos.get('positionAmt', 0))
                if qty == 0:
                    continue

                symbol = pos.get('symbol', '')
                entry_price = float(pos.get('entryPrice', 0))
                unrealized_pnl = float(pos.get('unrealizedProfit', 0))
                leverage = int(pos.get('leverage', 1))
                margin_mode = pos.get('marginType', 'cross')

                # Determine side
                side = 'LONG' if qty > 0 else 'SHORT'
                qty = abs(qty)

                # Get current price
                ticker = self.client.futures_symbol_ticker(symbol=symbol)
                current_price = float(ticker.get('price', 0))

                self.db.upsert_position(
                    exchange='binance',
                    symbol=symbol,
                    side=side,
                    quantity=qty,
                    entry_price=entry_price,
                    current_price=current_price,
                    unrealized_pnl=unrealized_pnl,
                    leverage=leverage,
                    margin_mode=margin_mode
                )

                positions.append({
                    'symbol': symbol,
                    'side': side,
                    'quantity': qty,
                    'entry_price': entry_price,
                    'current_price': current_price,
                    'unrealized_pnl': unrealized_pnl,
                    'leverage': leverage
                })

            return {
                "exchange": "binance",
                "positions": positions,
                "synced_at": datetime.utcnow().isoformat()
            }

        except Exception as e:
            return {"error": str(e), "positions": []}

    def sync_balances(self) -> Dict:
        """Sync Binance spot balances."""
        if not self.client:
            return {"error": "Binance not connected", "balances": []}

        try:
            account = self.client.get_account()
            balances = []

            for balance in account.get('balances', []):
                free = float(balance.get('free', 0))
                locked = float(balance.get('locked', 0))
                total = free + locked

                if total > 0:
                    asset = balance.get('asset', 'UNKNOWN')
                    usd_value = self._get_usd_value(asset, total)

                    self.db.upsert_balance(
                        exchange='binance',
                        asset=asset,
                        free=free,
                        locked=locked,
                        usd_value=usd_value
                    )

                    balances.append({
                        'asset': asset,
                        'free': free,
                        'locked': locked,
                        'usd_value': usd_value
                    })

            return {
                "exchange": "binance",
                "balances": balances,
                "synced_at": datetime.utcnow().isoformat()
            }

        except Exception as e:
            return {"error": str(e), "balances": []}

    def _get_usd_value(self, asset: str, amount: float) -> Optional[float]:
        """Get USD value for an asset amount."""
        if asset in ['USDT', 'USDC', 'BUSD', 'USD']:
            return amount

        try:
            ticker = self.client.get_symbol_ticker(symbol=f"{asset}USDT")
            price = float(ticker.get('price', 0))
            return amount * price
        except:
            pass

        return None

    def get_funding_rate(self, symbol: str = 'BTCUSDT') -> Optional[Dict]:
        """Get current funding rate for a symbol."""
        if not self.client:
            return None

        try:
            rates = self.client.futures_funding_rate(symbol=symbol, limit=1)
            if rates:
                return {
                    'symbol': symbol,
                    'funding_rate': float(rates[0]['fundingRate']),
                    'funding_time': rates[0]['fundingTime']
                }
        except:
            pass

        return None


class ExchangeManager:
    """Manages all exchange connections."""

    def __init__(self):
        self.coinbase = CoinbaseConnector()
        self.binance = BinanceConnector()
        self.db = get_database()

    def get_status(self) -> Dict:
        """Get connection status for all exchanges."""
        return {
            'coinbase': {
                'connected': self.coinbase.is_connected(),
                'type': 'spot'
            },
            'binance': {
                'connected': self.binance.is_connected(),
                'type': 'spot+futures'
            }
        }

    def sync_all(self) -> Dict:
        """Sync all connected exchanges."""
        results = {}

        if self.coinbase.is_connected():
            results['coinbase'] = self.coinbase.sync_balances()

        if self.binance.is_connected():
            results['binance_balances'] = self.binance.sync_balances()
            results['binance_positions'] = self.binance.sync_positions()

        return results

    def get_all_positions(self) -> List[Dict]:
        """Get positions from all exchanges."""
        return self.db.get_positions()

    def get_all_balances(self) -> List[Dict]:
        """Get balances from all exchanges."""
        return self.db.get_balances()

    def get_total_value(self) -> Dict:
        """Get total portfolio value across all exchanges."""
        balances = self.get_all_balances()
        positions = self.get_all_positions()

        total_balance_value = sum(b.get('usd_value', 0) or 0 for b in balances)
        total_position_value = sum(p.get('notional_value', 0) or 0 for p in positions)
        total_unrealized_pnl = sum(p.get('unrealized_pnl', 0) or 0 for p in positions)

        return {
            'total_balance_value': total_balance_value,
            'total_position_value': total_position_value,
            'total_unrealized_pnl': total_unrealized_pnl,
            'total_value': total_balance_value + total_position_value,
            'timestamp': datetime.utcnow().isoformat()
        }


# Singleton instance
_manager_instance = None

def get_exchange_manager() -> ExchangeManager:
    """Get or create exchange manager singleton."""
    global _manager_instance
    if _manager_instance is None:
        _manager_instance = ExchangeManager()
    return _manager_instance
