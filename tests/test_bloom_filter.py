"""Tests for src/utils/bloom_filter.py"""

import pytest

from src.utils.bloom_filter import BloomFilter


class TestBloomFilterBasics:
    def test_add_and_contains(self):
        bf = BloomFilter(capacity=100, error_rate=0.01)
        bf.add("BTC/USD:breakout:90")
        assert "BTC/USD:breakout:90" in bf

    def test_not_added_item_not_present(self):
        bf = BloomFilter(capacity=1000, error_rate=0.001)
        bf.add("item-A")
        assert "item-B" not in bf

    def test_clear_resets_filter(self):
        bf = BloomFilter(capacity=100, error_rate=0.01)
        bf.add("hello")
        assert "hello" in bf
        bf.clear()
        assert "hello" not in bf

    def test_approximate_count(self):
        bf = BloomFilter(capacity=100, error_rate=0.01)
        for i in range(10):
            bf.add(f"item-{i}")
        assert bf.approximate_count == 10

    def test_approximate_count_after_clear(self):
        bf = BloomFilter(capacity=100, error_rate=0.01)
        bf.add("x")
        bf.clear()
        assert bf.approximate_count == 0

    def test_multiple_items(self):
        bf = BloomFilter(capacity=500, error_rate=0.01)
        items = [f"coin:{i}:signal" for i in range(50)]
        for item in items:
            bf.add(item)
        for item in items:
            assert item in bf

    def test_non_string_contains_returns_false(self):
        bf = BloomFilter(capacity=100, error_rate=0.01)
        bf.add("test")
        assert 42 not in bf
        assert None not in bf

    def test_optimal_sizing(self):
        bf = BloomFilter(capacity=1000, error_rate=0.01)
        assert bf.bit_count > 0
        assert bf.hash_count >= 1

    def test_invalid_error_rate_raises(self):
        with pytest.raises(ValueError):
            BloomFilter(capacity=100, error_rate=0.0)
        with pytest.raises(ValueError):
            BloomFilter(capacity=100, error_rate=1.0)

    def test_invalid_capacity_raises(self):
        with pytest.raises(ValueError):
            BloomFilter(capacity=0, error_rate=0.01)

    def test_duplicate_add_idempotent(self):
        bf = BloomFilter(capacity=100, error_rate=0.01)
        bf.add("dup")
        bf.add("dup")
        assert "dup" in bf
        assert bf.approximate_count == 2  # count reflects calls, not unique items

    def test_deduplication_use_case(self):
        """Simulate the Hailo alert deduplication scenario."""
        bf = BloomFilter(capacity=2000, error_rate=0.005)
        key = "BTC/USD:breakout:90"

        assert key not in bf
        bf.add(key)
        assert key in bf  # second alert suppressed

        # Different coin should pass through
        assert "ETH/USD:breakout:90" not in bf
