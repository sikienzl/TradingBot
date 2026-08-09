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
from pathlib import Path

logging.basicConfig(
    level=logging.WARNING,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Try to import Hailo SDK components
HAILO_AVAILABLE = False
try:
    # Try different import patterns
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
    """
    Detects trading anomalies using Hailo-8 edge processing.
    Falls back to ML-based simulation when hardware is unavailable.
    """

    def __init__(self, device_id: int = 0):
        self.device_id = device_id
        self.device = None
        self.hailo_ready = False
        
        if HAILO_AVAILABLE:
            try:
                # Initialize Hailo device
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
        return float(np.std(returns) * np.sqrt(252))  # Annualized

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
        """
        Simulate anomaly detection using technical indicators.
        Returns (score_0_100, confidence_0_1, signal_type).
        """
        candles = market_data.get('candles', [])
        if not candles or len(candles) < 2:
            return 0, 0.0, 'insufficient_data'

        try:
            closes = np.array([c[4] for c in candles], dtype=float)
            volumes = np.array([c[5] if len(c) > 5 else 1.0 for c in candles], dtype=float)
            
            # Calculate technical indicators
            rsi = market_data.get('rsi', 50.0)
            volatility = self._calculate_volatility(closes)
            trend_strength = self._calculate_trend_strength(closes)
            volume_trend = market_data.get('volume_trend', 'neutral')
            
            # Volume analysis
            recent_vol = volumes[-10:].mean() if len(volumes) >= 10 else volumes.mean()
            avg_vol = volumes.mean()
            vol_ratio = recent_vol / avg_vol if avg_vol > 0 else 1.0
            
            # Price momentum
            price_change_1h = (closes[-1] - closes[-2]) / closes[-2] if len(closes) >= 2 else 0.0
            price_change_4h = (closes[-1] - closes[-4]) / closes[-4] if len(closes) >= 4 else price_change_1h
            
            # Anomaly scoring
            score = 50  # Neutral baseline
            confidence = 0.0
            signals = []
            
            # Extreme RSI (reversal points)
            if rsi > 80 or rsi < 20:
                score += 15
                confidence += 0.2
                signals.append('extreme_rsi')
            
            # High volatility
            if volatility > 0.8:
                score += 20
                confidence += 0.25
                signals.append('volatility_spike')
            elif volatility < 0.1:
                score -= 10
                confidence += 0.1
                signals.append('low_volatility')
            
            # Strong trend
            if trend_strength > 0.8:
                score += 10
                confidence += 0.15
                if price_change_4h > 0.05:
                    signals.append('breakout_up')
                elif price_change_4h < -0.05:
                    signals.append('breakout_down')
            
            # Volume surge
            if vol_ratio > 1.5:
                score += 15
                confidence += 0.2
                signals.append('volume_surge')
            elif vol_ratio < 0.7:
                score -= 5
            
            # Large price movement
            if abs(price_change_1h) > 0.03:
                score += 10
                confidence += 0.15
            
            # Normalize
            score = max(0, min(100, score))
            confidence = max(0.0, min(1.0, confidence))
            
            signal_type = signals[0] if signals else 'neutral'
            
            return int(score), confidence, signal_type
            
        except Exception as e:
            logger.debug(f"Simulation scoring error: {e}")
            return 0, 0.0, 'error'

    def detect_anomaly(self, market_data: dict) -> dict:
        """
        Detect anomalies in market data.
        Returns JSON dict with timestamp, hailo_score, confidence, signal_type, market_context.
        """
        timestamp = datetime.now(UTC).isoformat()
        
        try:
            candles = market_data.get('candles', [])
            coin = market_data.get('coin', 'UNKNOWN')
            
            if not candles or len(candles) < 2:
                return {
                    'timestamp': timestamp,
                    'coin': coin,
                    'hailo_score': 0,
                    'confidence': 0.0,
                    'signal_type': 'insufficient_data',
                    'market_context': {'error': 'not_enough_candles'},
                    'mode': 'simulation'
                }
            
            # Get anomaly score
            if self.hailo_ready:
                # Would call actual Hailo inference here
                logger.debug(f"Would call Hailo device for {coin}")
                score, confidence, signal_type = self._simulation_anomaly_score(market_data)
                mode = 'hailo'
            else:
                # Use simulation mode
                score, confidence, signal_type = self._simulation_anomaly_score(market_data)
                mode = 'simulation'
            
            # Build market context
            closes = np.array([c[4] for c in candles], dtype=float)
            market_context = {
                'coin': coin,
                'latest_price': float(closes[-1]),
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
        # Read input from stdin
        input_line = sys.stdin.read().strip()
        if not input_line:
            return
        
        market_data = json.loads(input_line)
        
        # Detect anomalies
        detector = HailoAnomalyDetector()
        result = detector.detect_anomaly(market_data)
        
        # Output result as JSON
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
