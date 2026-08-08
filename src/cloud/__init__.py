"""
Cloud Package Initialization

This package contains cloud-based components for the trading bot.
"""

from .strategist_service import GPT5StrategistService as StrategistService

__all__ = [
    'StrategistService'
]
