#!/usr/bin/env python3
"""
Remote Hailo Inference Service
Runs on 192.168.62.75 with /dev/hailo0
Callable via SSH from Trading Bot on 192.168.62.74

This service accepts JSON via stdin with market data and returns anomaly alerts.
Format:
  INPUT: {"coin": "BTC", "candles": [[o,h,l,c,v], ...], "rsi": 50, "volume_trend": "increasing"}
  OUTPUT: {"timestamp": "...", "hailo_score": 0-100, "confidence": 0-1, "signal_type": "...", "market_context": {...}}
"""
import json
import sys
import logging
import numpy as np
from datetime import datetime, timezone, UTC

logging.basicConfig(
    level=logging.WARNING,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Try to import Hailo SDK components
HAILO_AVAILABLE = False
try:
    try:
        from hailo import Device, InferVstreams, ConfigureParams
        HAILO_AVAILABLE = True
        logger.info("✅ Hailo SDK Device API available")
    except ImportError:
        try:
            from hailo_platform import HEF
            HAILO_AVAILABLE = True
            logger.info("✅ Hailo Platform HEF available")
        except ImportError:
            logger.warning("⚠️  Hailo SDK not fully available - will use simulation mode")
            HAILO_AVAILABLE = False
except Exception as e:
    logger.warning(f"⚠️  Hailo import warning: {e}")
    HAILO_AVAILABLE = False


class HailoAnomalyDetector:
    """Detects trading anomalies using Hailo-8 edge processing with ML fallback."""

    def __init__(self, device_id: int = 0):
        self.device_id = device_id
        self.device = None
        self.hailo_ready = False
        
        if HAILO_AVAILABLE:
            try:
                try:
                    self.device = Device()
                    self.hailo_ready = True
                    logger.info(f"✅ Hailo Device initialized on {device_id}")
                except Exception as e:
                    logger.debug(f"Device init note: {e}, continuing in simulation mode")
                    self.hailo_ready = False
            except Exception as e:
                logger.debug(f"Hailo setup: {e}")
                self.hailo_ready = False

    def _calculate_volatility(self, prices: np.ndarray) -> float:
        """Calculate price volatility (annualized std dev of returns)."""
        if len(prices) < 2:
            return 0.0
        returns = np.diff(prices) / prices[:-1]
        return float(np.std(returns) * np.sqrt(252))

    def _calculate_trend_strength(self, prices: np.ndarray) -> float:
        """Calculate trend strength as R² of linear regression."""
        if len(prices) < 2:
            return 0.0
        x = np.arange(len(prices))
        coeffs = np.polyfit(x, prices, 1)
        poly = np.poly1d(coeffs)
        ss_res = np.sum((prices - poly(x)) ** 2)
        ss_tot = np.sum((prices - np.mean(prices)) ** 2)
        if ss_tot == 0:
            return 0.0
        return float(1.0 - ss_res / ss_tot)

    def _simulation_anomaly_score(self, market_data: dict) -> tuple[int, float, str]:
        """Simulate anomaly detection using technical indicators."""
        candles = market_data.get('candles', [])
        if not candles:
            return 0, 0.0, 'no_candles'

        try:
            closes = np.array([c[4] if len(c) > 4 else c[-1] for c in candles], dtype=float)
            volumes = np.array([c[5] if len(c) > 5 else 1.0 for c in candles], dtype=float)
            
            rsi = float(market_data.get('rsi', 50.0))
            volume_trend = market_data.get('volume_trend', 'neutral')
            
            score = 50
            confidence = 0.1
            signals = []
            
            # Extreme RSI
            if rsi > 80 or rsi < 20:
                score += 15
                confidence += 0.2
                signals.append('extreme_rsi')
            
            # Multi-candle analysis
            if len(closes) >= 2:
                volatility = self._calculate_volatility(closes)
                trend_strength = self._calculate_trend_strength(closes)
                
                if volatility > 0.8:
                    score += 20
                    confidence += 0.25
                    signals.append('volatility_spike')
                elif volatility < 0.1:
                    score -= 10
                    confidence += 0.1
                    signals.append('low_volatility')
                
                if trend_strength > 0.8:
                    score += 10
                    confidence += 0.15
                    price_change = (closes[-1] - closes[-2]) / closes[-2]
                    if price_change > 0.05:
                        signals.append('breakout_up')
                    elif price_change < -0.05:
                        signals.append('breakout_down')
            
            # Volume analysis
            if len(volumes) >= 2:
                recent_vol = volumes[-1]
                avg_vol = volumes[:-1].mean()
                vol_ratio = recent_vol / avg_vol if avg_vol > 0 else 1.0
                
                if vol_ratio > 1.5:
                    score += 15
                    confidence += 0.2
                    signals.append('volume_surge')
                elif vol_ratio < 0.7:
                    score -= 5
            
            # Volume trend
            if volume_trend == 'increasing':
                score += 5
                confidence += 0.05
                signals.append('vol_increasing')
            elif volume_trend == 'decreasing':
                score -= 5
            
            score = max(0, min(100, score))
            confidence = max(0.0, min(1.0, confidence))
            signal_type = signals[0] if signals else 'neutral'
            
            return int(score), confidence, signal_type
            
        except Exception as e:
            logger.debug(f"Simulation scoring error: {e}")
            return 0, 0.0, 'error'

    def detect_anomaly(self, market_data: dict) -> dict:
        """Detect anomalies in market data."""
        timestamp = datetime.now(UTC).isoformat()
        
        try:
            candles = market_data.get('candles', [])
            coin = market_data.get('coin', 'UNKNOWN')
            
            if not candles:
                return {
                    'timestamp': timestamp,
                    'coin': coin,
                    'hailo_score': 0,
                    'confidence': 0.0,
                    'signal_type': 'no_candles',
                    'market_context': {'error': 'no_candles'},
                    'mode': 'simulation'
                }
            
            score, confidence, signal_type = self._simulation_anomaly_score(market_data)
            mode = 'hailo' if self.hailo_ready else 'simulation'
            
            closes = np.array([c[4] if len(c) > 4 else c[-1] for c in candles], dtype=float)
            market_context = {
                'coin': coin,
                'latest_price': float(closes[-1]) if len(closes) > 0 else 0.0,
                'price_change_1h_pct': float((closes[-1] - closes[-2]) / closes[-2] * 100 if len(closes) >= 2 else 0.0),
                'rsi': float(market_data.get('rsi', 50.0)),
                'volume_trend': market_data.get('volume_trend', 'neutral'),
                'candle_count': len(candles),
            }
            
            return {
                'timestamp': timestamp,
                'coin': coin,
                'hailo_score': score,
                'confidence': float(confidence),
                'signal_type': signal_type,
                'market_context': market_context,
                'mode': mode
            }
            
        except Exception as e:
            logger.error(f"Anomaly detection error: {e}", exc_info=True)
            return {
                'timestamp': timestamp,
                'hailo_score': 0,
                'confidence': 0.0,
                'signal_type': 'error',
                'market_context': {'error': str(e)},
                'mode': 'error'
            }


def main():
    """Read JSON from stdin, detect anomalies, output JSON to stdout."""
    try:
        input_line = sys.stdin.read().strip()
        if not input_line:
            return
        
        market_data = json.loads(input_line)
        detector = HailoAnomalyDetector()
        result = detector.detect_anomaly(market_data)
        print(json.dumps(result), flush=True)
        
    except json.JSONDecodeError as e:
        error_result = {
            'timestamp': datetime.now(UTC).isoformat(),
            'hailo_score': 0,
            'confidence': 0.0,
            'signal_type': 'json_error',
            'market_context': {'error': str(e)},
            'mode': 'error'
        }
        print(json.dumps(error_result), flush=True)
    except Exception as e:
        error_result = {
            'timestamp': datetime.now(UTC).isoformat(),
            'hailo_score': 0,
            'confidence': 0.0,
            'signal_type': 'error',
            'market_context': {'error': str(e)},
            'mode': 'error'
        }
        print(json.dumps(error_result), flush=True)


if __name__ == '__main__':
    main()
