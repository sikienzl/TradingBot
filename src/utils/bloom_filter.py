"""
Pure-Python Bloom Filter — no external dependencies.

Used for O(1) duplicate suppression in the Hailo→Strategist alert pipeline.

Usage:
    bf = BloomFilter(capacity=5000, error_rate=0.01)
    bf.add("BTC/USD:breakout")
    "BTC/USD:breakout" in bf   # True (no false negatives)
    bf.clear()                 # wipe all bits
"""

import hashlib
import math


class BloomFilter:
    """
    Space-efficient probabilistic set using k hash functions over a bit array.

    False positives are possible; false negatives are not.
    Thread-safety: NOT thread-safe — use a lock if sharing across threads.
    """

    def __init__(self, capacity: int = 10_000, error_rate: float = 0.01) -> None:
        """
        Args:
            capacity:   Expected number of distinct items.
            error_rate: Acceptable false-positive probability (0 < p < 1).
        """
        if not (0 < error_rate < 1):
            raise ValueError("error_rate must be between 0 and 1 (exclusive)")
        if capacity < 1:
            raise ValueError("capacity must be >= 1")

        # Optimal bit-array size and number of hash functions
        self._m: int = self._optimal_m(capacity, error_rate)
        self._k: int = self._optimal_k(self._m, capacity)
        self._bits: bytearray = bytearray(math.ceil(self._m / 8))
        self._count: int = 0

        # Pre-compute salts for k hash functions
        self._salts: list[bytes] = [
            str(i).encode() for i in range(self._k)
        ]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add(self, item: str) -> None:
        """Add *item* to the filter."""
        for idx in self._hashes(item):
            byte, bit = divmod(idx, 8)
            self._bits[byte] |= 1 << bit
        self._count += 1

    def __contains__(self, item: object) -> bool:
        """Return True if *item* was probably added; False if definitely not."""
        if not isinstance(item, str):
            return False
        return all(
            self._bits[byte] & (1 << bit)
            for byte, bit in (divmod(idx, 8) for idx in self._hashes(item))
        )

    def clear(self) -> None:
        """Reset all bits — effectively empties the filter."""
        for i in range(len(self._bits)):
            self._bits[i] = 0
        self._count = 0

    @property
    def approximate_count(self) -> int:
        """Number of times :meth:`add` was called (not deduplicated)."""
        return self._count

    @property
    def bit_count(self) -> int:
        """Total number of bits in the backing array."""
        return self._m

    @property
    def hash_count(self) -> int:
        """Number of independent hash functions used."""
        return self._k

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _hashes(self, item: str) -> list[int]:
        """Return k bit-array indices for *item* using SHA-256 with different salts."""
        raw = item.encode()
        indices: list[int] = []
        for salt in self._salts:
            digest = hashlib.sha256(salt + raw).digest()
            # Use first 8 bytes of digest as a 64-bit integer
            val = int.from_bytes(digest[:8], "big")
            indices.append(val % self._m)
        return indices

    @staticmethod
    def _optimal_m(n: int, p: float) -> int:
        """Optimal bit-array size for n items and false-positive rate p."""
        return max(1, int(-n * math.log(p) / (math.log(2) ** 2)))

    @staticmethod
    def _optimal_k(m: int, n: int) -> int:
        """Optimal number of hash functions."""
        return max(1, round(m / n * math.log(2)))
