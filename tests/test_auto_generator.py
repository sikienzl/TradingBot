import numpy as np
from src.autoresearch.strategies.auto_generator import create


def test_generator_buy():
    data = {"close": np.linspace(1, 2, 50)}
    gen = create({"short": 3, "long": 10})
    res = gen.generate_signals(data)
    assert res["signal"] in ("buy", "hold")


def test_generator_insufficient():
    data = {"close": np.array([1, 2, 3])}
    gen = create({"short": 3, "long": 10})
    res = gen.generate_signals(data)
    assert res["signal"] == "hold"
