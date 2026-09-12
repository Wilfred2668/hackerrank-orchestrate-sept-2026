"""
Tests for currency conversion — run against the ACTUAL exchange_rates.csv.
"""

from __future__ import annotations

import sys
import os
import unittest
from datetime import date
from decimal import Decimal

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), "..")))

from lib.loaders import DataStore
from lib.currency import CurrencyConverter, ExchangeRateMissing


class TestCurrencyConverter(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        ds = DataStore()
        cls.converter = CurrencyConverter(ds.exchange_rates)

    def test_same_currency(self):
        """Same currency returns amount unchanged, no lookup."""
        result = self.converter.convert(Decimal("12345.67"), "ZAR", "ZAR", date(2023, 10, 15))
        self.assertEqual(result, Decimal("12345.67"))

    def test_eur_to_zar_20231015(self):
        """EUR->ZAR on 2023-10-15 has rate=20 (verified from raw file)."""
        result = self.converter.convert(
            Decimal("100"), "EUR", "ZAR", date(2023, 10, 15)
        )
        self.assertEqual(result, Decimal("2000"))

    def test_usd_to_eur_20231015(self):
        """USD->EUR on 2023-10-15 has rate=0.92."""
        result = self.converter.convert(
            Decimal("100"), "USD", "EUR", date(2023, 10, 15)
        )
        self.assertEqual(result, Decimal("92.00"))

    def test_missing_triple_raises(self):
        """A missing (from,to,date) triple must raise ExchangeRateMissing,
        not silently invert or guess."""
        with self.assertRaises(ExchangeRateMissing):
            self.converter.convert(
                Decimal("100"), "ZAR", "EUR", date(2023, 10, 15)
            )

    def test_missing_date_raises(self):
        """Even if the pair exists on another date, a missing date raises."""
        with self.assertRaises(ExchangeRateMissing):
            self.converter.convert(
                Decimal("100"), "EUR", "ZAR", date(1999, 1, 1)
            )

    def test_zero_amount(self):
        """Converting zero is valid and returns zero."""
        result = self.converter.convert(Decimal("0"), "EUR", "ZAR", date(2023, 10, 15))
        self.assertEqual(result, Decimal("0"))


if __name__ == "__main__":
    unittest.main()
