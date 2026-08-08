"""A simple example strategy for autoresearch tests."""
from dataclasses import dataclass
from typing import Any


@dataclass
class ExampleStrategy:
    params: dict[str, Any]

    def generate_signals(self, market_data):
        """Return a trivial signal dict for demonstration."""
        # market_data is expected to be a pandas.DataFrame-like object
        return {"signal": "hold", "reason": "example"}


def create(params: dict[str, Any]) -> ExampleStrategy:
    return ExampleStrategy(params=params)
