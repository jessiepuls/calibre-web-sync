"""Ensure the benchmark compares equivalent states and counts content reads."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import benchmark_cache


class BenchmarkTests(unittest.TestCase):
    def test_benchmark_checks_stable_and_changed_plans(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(benchmark_cache, 'BOOKS', 3), patch.object(benchmark_cache, 'BYTES_PER_BOOK', 64):
                result = benchmark_cache.benchmark(Path(temporary))
        self.assertTrue(result['plans_agree'])
        self.assertEqual(result['uncached']['bytes_hashed'], 384)
        self.assertEqual(result['cold']['bytes_hashed'], 384)
        self.assertEqual(result['warm']['bytes_hashed'], 0)
        self.assertEqual(result['one_file_changed']['bytes_hashed'], 64)
        self.assertEqual(result['one_file_changed']['decisions'], {'review': 1, 'present': 2})
