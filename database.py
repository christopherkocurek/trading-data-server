"""
SQLite Database for Trading Data Server
Stores indicators, signals, and exchange positions with history
"""

import sqlite3
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from contextlib import contextmanager
import json
import os

class TradingDatabase:
    """SQLite database for trading data persistence."""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or os.getenv("DATABASE_PATH", "trading_data.db")
        self._init_db()

    @contextmanager
    def _get_connection(self):
        """Context manager for database connections."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self):
        """Initialize database schema."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            # Trading indicators - stores historical values from TradingView
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trading_indicators (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL DEFAULT 'BTCUSD',
                    indicator_name TEXT NOT NULL,
                    value REAL NOT NULL,
                    value2 REAL,
                    value3 REAL,
                    timeframe TEXT DEFAULT '1D',
                    source TEXT DEFAULT 'tradingview',
                    received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Create indexes for fast lookups
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_indicators_symbol
                ON trading_indicators(symbol)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_indicators_name
                ON trading_indicators(indicator_name)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_indicators_time
                ON trading_indicators(received_at DESC)
            """)

            # Trading signals - detected patterns and alerts
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trading_signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL DEFAULT 'BTCUSD',
                    signal_type TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    strength TEXT DEFAULT 'MEDIUM',
                    price_at_signal REAL,
                    indicator_values TEXT,
                    message TEXT,
                    received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    acknowledged INTEGER DEFAULT 0,
                    acknowledged_at TIMESTAMP
                )
            """)

            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_signals_symbol
                ON trading_signals(symbol)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_signals_time
                ON trading_signals(received_at DESC)
            """)

            # Exchange positions - open trades
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS exchange_positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    exchange TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    entry_price REAL,
                    current_price REAL,
                    quantity REAL NOT NULL,
                    notional_value REAL,
                    unrealized_pnl REAL,
                    leverage REAL DEFAULT 1.0,
                    margin_mode TEXT,
                    stop_loss REAL,
                    take_profit REAL,
                    opened_at TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(exchange, symbol, side)
                )
            """)

            # Exchange balances
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS exchange_balances (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    exchange TEXT NOT NULL,
                    asset TEXT NOT NULL,
                    free REAL NOT NULL,
                    locked REAL DEFAULT 0,
                    total REAL NOT NULL,
                    usd_value REAL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(exchange, asset)
                )
            """)

            # Latest indicators cache (for quick access)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS latest_indicators (
                    symbol TEXT NOT NULL,
                    indicator_name TEXT NOT NULL,
                    value REAL NOT NULL,
                    value2 REAL,
                    value3 REAL,
                    timeframe TEXT DEFAULT '1D',
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (symbol, indicator_name)
                )
            """)

    # ==========================================
    # INDICATOR METHODS
    # ==========================================

    def save_indicator(self, symbol: str, indicator_name: str, value: float,
                       timeframe: str = '1D', value2: float = None,
                       value3: float = None, source: str = 'tradingview') -> int:
        """
        Save an indicator value with history.
        Returns the inserted row ID.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()

            # Insert into history table
            cursor.execute("""
                INSERT INTO trading_indicators
                (symbol, indicator_name, value, value2, value3, timeframe, source)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (symbol, indicator_name, value, value2, value3, timeframe, source))

            row_id = cursor.lastrowid

            # Update latest cache
            cursor.execute("""
                INSERT OR REPLACE INTO latest_indicators
                (symbol, indicator_name, value, value2, value3, timeframe, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """, (symbol, indicator_name, value, value2, value3, timeframe))

            return row_id

    def get_latest_indicators(self, symbol: str = 'BTCUSD') -> Dict[str, Any]:
        """Get all latest indicator values for a symbol."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT indicator_name, value, value2, value3, timeframe, updated_at
                FROM latest_indicators
                WHERE symbol = ?
            """, (symbol,))

            result = {
                "symbol": symbol,
                "indicators": {},
                "last_updated": None
            }

            for row in cursor.fetchall():
                name = row['indicator_name']
                result["indicators"][name] = {
                    "value": row['value'],
                    "value2": row['value2'],
                    "value3": row['value3'],
                    "timeframe": row['timeframe']
                }
                # Track most recent update
                if not result["last_updated"] or row['updated_at'] > result["last_updated"]:
                    result["last_updated"] = row['updated_at']

            return result

    def get_indicator_history(self, symbol: str, indicator_name: str,
                               hours: int = 24) -> List[Dict]:
        """Get historical values for an indicator."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            since = datetime.utcnow() - timedelta(hours=hours)

            cursor.execute("""
                SELECT value, value2, value3, received_at
                FROM trading_indicators
                WHERE symbol = ? AND indicator_name = ? AND received_at > ?
                ORDER BY received_at ASC
            """, (symbol, indicator_name, since.isoformat()))

            return [dict(row) for row in cursor.fetchall()]

    # ==========================================
    # SIGNAL METHODS
    # ==========================================

    def save_signal(self, symbol: str, signal_type: str, direction: str,
                    strength: str = 'MEDIUM', price: float = None,
                    indicator_values: Dict = None, message: str = None) -> int:
        """Save a trading signal."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                INSERT INTO trading_signals
                (symbol, signal_type, direction, strength, price_at_signal,
                 indicator_values, message)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                symbol, signal_type, direction, strength, price,
                json.dumps(indicator_values) if indicator_values else None,
                message
            ))

            return cursor.lastrowid

    def get_signals(self, symbol: str = None, limit: int = 50,
                    acknowledged: bool = None) -> List[Dict]:
        """Get recent trading signals."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            query = "SELECT * FROM trading_signals WHERE 1=1"
            params = []

            if symbol:
                query += " AND symbol = ?"
                params.append(symbol)

            if acknowledged is not None:
                query += " AND acknowledged = ?"
                params.append(1 if acknowledged else 0)

            query += " ORDER BY received_at DESC LIMIT ?"
            params.append(limit)

            cursor.execute(query, params)

            signals = []
            for row in cursor.fetchall():
                signal = dict(row)
                if signal.get('indicator_values'):
                    signal['indicator_values'] = json.loads(signal['indicator_values'])
                signals.append(signal)

            return signals

    def acknowledge_signal(self, signal_id: int) -> bool:
        """Mark a signal as acknowledged."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE trading_signals
                SET acknowledged = 1, acknowledged_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (signal_id,))
            return cursor.rowcount > 0

    # ==========================================
    # POSITION METHODS
    # ==========================================

    def upsert_position(self, exchange: str, symbol: str, side: str,
                        quantity: float, entry_price: float = None,
                        current_price: float = None, unrealized_pnl: float = None,
                        leverage: float = 1.0, margin_mode: str = None,
                        stop_loss: float = None, take_profit: float = None) -> int:
        """Insert or update an exchange position."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            notional = quantity * current_price if current_price else None

            cursor.execute("""
                INSERT INTO exchange_positions
                (exchange, symbol, side, quantity, entry_price, current_price,
                 notional_value, unrealized_pnl, leverage, margin_mode,
                 stop_loss, take_profit, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(exchange, symbol, side) DO UPDATE SET
                    quantity = excluded.quantity,
                    current_price = excluded.current_price,
                    notional_value = excluded.notional_value,
                    unrealized_pnl = excluded.unrealized_pnl,
                    updated_at = CURRENT_TIMESTAMP
            """, (
                exchange, symbol, side, quantity, entry_price, current_price,
                notional, unrealized_pnl, leverage, margin_mode,
                stop_loss, take_profit
            ))

            return cursor.lastrowid

    def get_positions(self, exchange: str = None) -> List[Dict]:
        """Get all open positions, optionally filtered by exchange."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            if exchange:
                cursor.execute("""
                    SELECT * FROM exchange_positions
                    WHERE exchange = ? AND quantity != 0
                    ORDER BY updated_at DESC
                """, (exchange,))
            else:
                cursor.execute("""
                    SELECT * FROM exchange_positions
                    WHERE quantity != 0
                    ORDER BY updated_at DESC
                """)

            return [dict(row) for row in cursor.fetchall()]

    def clear_positions(self, exchange: str):
        """Clear all positions for an exchange (before refresh)."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM exchange_positions WHERE exchange = ?", (exchange,))

    # ==========================================
    # BALANCE METHODS
    # ==========================================

    def upsert_balance(self, exchange: str, asset: str, free: float,
                       locked: float = 0, usd_value: float = None):
        """Insert or update an exchange balance."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            total = free + locked

            cursor.execute("""
                INSERT INTO exchange_balances
                (exchange, asset, free, locked, total, usd_value, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(exchange, asset) DO UPDATE SET
                    free = excluded.free,
                    locked = excluded.locked,
                    total = excluded.total,
                    usd_value = excluded.usd_value,
                    updated_at = CURRENT_TIMESTAMP
            """, (exchange, asset, free, locked, total, usd_value))

    def get_balances(self, exchange: str = None) -> List[Dict]:
        """Get all balances, optionally filtered by exchange."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            if exchange:
                cursor.execute("""
                    SELECT * FROM exchange_balances
                    WHERE exchange = ? AND total > 0
                    ORDER BY usd_value DESC NULLS LAST
                """, (exchange,))
            else:
                cursor.execute("""
                    SELECT * FROM exchange_balances
                    WHERE total > 0
                    ORDER BY usd_value DESC NULLS LAST
                """)

            return [dict(row) for row in cursor.fetchall()]

    # ==========================================
    # UTILITY METHODS
    # ==========================================

    def get_summary(self, symbol: str = 'BTCUSD') -> Dict[str, Any]:
        """Get complete trading summary for Claude/UI."""
        indicators = self.get_latest_indicators(symbol)
        signals = self.get_signals(symbol=symbol, limit=10)
        positions = self.get_positions()

        # Extract key indicator values
        ind = indicators.get("indicators", {})

        # Determine trend
        rsi = ind.get("rsi_1d", {}).get("value")
        above_ma = ind.get("ma_200", {}).get("value")
        price = ind.get("price", {}).get("value")

        trend = "NEUTRAL"
        if price and above_ma:
            trend = "BULLISH" if price > above_ma else "BEARISH"

        return {
            "timestamp": datetime.utcnow().isoformat(),
            "symbol": symbol,
            "price": price,
            "trend": trend,
            "indicators": {
                "rsi_daily": ind.get("rsi_1d", {}).get("value"),
                "rsi_4h": ind.get("rsi_4h", {}).get("value"),
                "macd": {
                    "line": ind.get("macd_line", {}).get("value"),
                    "signal": ind.get("macd_signal", {}).get("value"),
                    "histogram": ind.get("macd_histogram", {}).get("value")
                },
                "ma_200": above_ma,
                "above_200ma": (price > above_ma) if price and above_ma else None,
                "atr": ind.get("atr_14", {}).get("value"),
                "volume_ratio": ind.get("volume_ratio", {}).get("value"),
                "bollinger": {
                    "upper": ind.get("bb_upper", {}).get("value"),
                    "lower": ind.get("bb_lower", {}).get("value")
                }
            },
            "active_signals": signals[:5],
            "positions": positions,
            "total_unrealized_pnl": sum(p.get("unrealized_pnl", 0) or 0 for p in positions),
            "data_freshness": indicators.get("last_updated")
        }

    def cleanup_old_data(self, days: int = 30):
        """Remove indicator history older than specified days."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            cutoff = datetime.utcnow() - timedelta(days=days)

            cursor.execute("""
                DELETE FROM trading_indicators
                WHERE received_at < ?
            """, (cutoff.isoformat(),))

            cursor.execute("""
                DELETE FROM trading_signals
                WHERE received_at < ? AND acknowledged = 1
            """, (cutoff.isoformat(),))

            return cursor.rowcount


# Singleton instance
_db_instance = None

def get_database() -> TradingDatabase:
    """Get or create database singleton."""
    global _db_instance
    if _db_instance is None:
        _db_instance = TradingDatabase()
    return _db_instance
