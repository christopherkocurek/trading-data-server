"""
Signal Detection for Trading Data Server
Detects trading signals from indicator changes
"""

from typing import Dict, Optional, List
from datetime import datetime
from database import get_database


class SignalDetector:
    """Detect trading signals from indicator values and changes."""

    # RSI thresholds
    RSI_OVERSOLD = 30
    RSI_EXTREME_OVERSOLD = 20
    RSI_OVERBOUGHT = 70
    RSI_EXTREME_OVERBOUGHT = 80

    # MACD thresholds
    MACD_THRESHOLD = 0  # Histogram crossing zero

    def __init__(self):
        self.db = get_database()
        self._last_values: Dict[str, Dict] = {}  # Cache of previous values

    def check_all_signals(self, symbol: str, indicators: Dict) -> List[Dict]:
        """
        Check for all signal types based on current indicators.
        Returns list of detected signals.
        """
        signals = []

        # Get previous values for comparison
        prev = self._last_values.get(symbol, {})

        # RSI signals
        rsi_signal = self._check_rsi(
            indicators.get('rsi_1d') or indicators.get('rsi'),
            prev.get('rsi_1d') or prev.get('rsi')
        )
        if rsi_signal:
            rsi_signal['symbol'] = symbol
            signals.append(rsi_signal)

        # MACD signals
        macd_signal = self._check_macd(
            indicators.get('macd_line'),
            indicators.get('macd_signal'),
            indicators.get('macd_histogram'),
            prev.get('macd_histogram')
        )
        if macd_signal:
            macd_signal['symbol'] = symbol
            signals.append(macd_signal)

        # 200 MA signals
        ma_signal = self._check_200ma(
            indicators.get('price'),
            indicators.get('ma_200'),
            prev.get('price'),
            prev.get('ma_200')
        )
        if ma_signal:
            ma_signal['symbol'] = symbol
            signals.append(ma_signal)

        # Bollinger Band signals
        bb_signal = self._check_bollinger(
            indicators.get('price'),
            indicators.get('bb_upper'),
            indicators.get('bb_lower')
        )
        if bb_signal:
            bb_signal['symbol'] = symbol
            signals.append(bb_signal)

        # Update cached values
        self._last_values[symbol] = indicators.copy()

        # Save signals to database
        for signal in signals:
            self.db.save_signal(
                symbol=signal['symbol'],
                signal_type=signal['signal_type'],
                direction=signal['direction'],
                strength=signal.get('strength', 'MEDIUM'),
                price=indicators.get('price'),
                indicator_values=indicators,
                message=signal.get('message')
            )

        return signals

    def _check_rsi(self, current_rsi: float, prev_rsi: float) -> Optional[Dict]:
        """Check for RSI-based signals."""
        if current_rsi is None:
            return None

        # Entering oversold
        if prev_rsi and prev_rsi > self.RSI_OVERSOLD and current_rsi <= self.RSI_OVERSOLD:
            strength = 'STRONG' if current_rsi <= self.RSI_EXTREME_OVERSOLD else 'MEDIUM'
            return {
                'signal_type': 'RSI_OVERSOLD',
                'direction': 'BULLISH',
                'strength': strength,
                'message': f'RSI dropped to {current_rsi:.1f} (oversold zone)'
            }

        # Entering overbought
        if prev_rsi and prev_rsi < self.RSI_OVERBOUGHT and current_rsi >= self.RSI_OVERBOUGHT:
            strength = 'STRONG' if current_rsi >= self.RSI_EXTREME_OVERBOUGHT else 'MEDIUM'
            return {
                'signal_type': 'RSI_OVERBOUGHT',
                'direction': 'BEARISH',
                'strength': strength,
                'message': f'RSI rose to {current_rsi:.1f} (overbought zone)'
            }

        # Extreme levels without transition (still noteworthy)
        if current_rsi <= self.RSI_EXTREME_OVERSOLD:
            return {
                'signal_type': 'RSI_EXTREME_OVERSOLD',
                'direction': 'BULLISH',
                'strength': 'STRONG',
                'message': f'RSI at extreme oversold: {current_rsi:.1f}'
            }

        if current_rsi >= self.RSI_EXTREME_OVERBOUGHT:
            return {
                'signal_type': 'RSI_EXTREME_OVERBOUGHT',
                'direction': 'BEARISH',
                'strength': 'STRONG',
                'message': f'RSI at extreme overbought: {current_rsi:.1f}'
            }

        return None

    def _check_macd(self, macd_line: float, macd_signal: float,
                    histogram: float, prev_histogram: float) -> Optional[Dict]:
        """Check for MACD-based signals."""
        if histogram is None:
            return None

        # Histogram crossing zero (bullish)
        if prev_histogram is not None and prev_histogram < 0 and histogram >= 0:
            return {
                'signal_type': 'MACD_BULLISH_CROSS',
                'direction': 'BULLISH',
                'strength': 'MEDIUM',
                'message': f'MACD histogram crossed above zero'
            }

        # Histogram crossing zero (bearish)
        if prev_histogram is not None and prev_histogram > 0 and histogram <= 0:
            return {
                'signal_type': 'MACD_BEARISH_CROSS',
                'direction': 'BEARISH',
                'strength': 'MEDIUM',
                'message': f'MACD histogram crossed below zero'
            }

        # Strong divergence
        if macd_line is not None and macd_signal is not None:
            diff = macd_line - macd_signal
            if abs(diff) > 500:  # Significant divergence for BTC
                direction = 'BULLISH' if diff > 0 else 'BEARISH'
                return {
                    'signal_type': 'MACD_DIVERGENCE',
                    'direction': direction,
                    'strength': 'WEAK',
                    'message': f'MACD shows strong {direction.lower()} divergence'
                }

        return None

    def _check_200ma(self, price: float, ma_200: float,
                     prev_price: float, prev_ma: float) -> Optional[Dict]:
        """Check for 200 MA crossover signals."""
        if price is None or ma_200 is None:
            return None

        # Price crossing above 200 MA
        if prev_price is not None and prev_ma is not None:
            was_below = prev_price < prev_ma
            now_above = price > ma_200

            if was_below and now_above:
                return {
                    'signal_type': 'MA200_BULLISH_CROSS',
                    'direction': 'BULLISH',
                    'strength': 'STRONG',
                    'message': f'Price crossed above 200 MA (${ma_200:,.0f})'
                }

            was_above = prev_price > prev_ma
            now_below = price < ma_200

            if was_above and now_below:
                return {
                    'signal_type': 'MA200_BEARISH_CROSS',
                    'direction': 'BEARISH',
                    'strength': 'STRONG',
                    'message': f'Price crossed below 200 MA (${ma_200:,.0f})'
                }

        return None

    def _check_bollinger(self, price: float, bb_upper: float,
                         bb_lower: float) -> Optional[Dict]:
        """Check for Bollinger Band signals."""
        if price is None or bb_upper is None or bb_lower is None:
            return None

        # Price touching/breaking upper band
        if price >= bb_upper:
            return {
                'signal_type': 'BB_UPPER_TOUCH',
                'direction': 'BEARISH',
                'strength': 'WEAK',
                'message': f'Price touched upper Bollinger Band (${bb_upper:,.0f})'
            }

        # Price touching/breaking lower band
        if price <= bb_lower:
            return {
                'signal_type': 'BB_LOWER_TOUCH',
                'direction': 'BULLISH',
                'strength': 'WEAK',
                'message': f'Price touched lower Bollinger Band (${bb_lower:,.0f})'
            }

        return None


# Singleton instance
_detector_instance = None

def get_signal_detector() -> SignalDetector:
    """Get or create signal detector singleton."""
    global _detector_instance
    if _detector_instance is None:
        _detector_instance = SignalDetector()
    return _detector_instance
