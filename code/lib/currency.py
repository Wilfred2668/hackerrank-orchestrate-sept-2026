"""
Exchange-rate lookup and currency conversion.

Uses exact (from_currency, to_currency, rate_date) matching only.
No automatic inversion. No nearest-date fallback.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Dict, Tuple


class ExchangeRateMissing(Exception):
    """Raised when an exact exchange-rate row cannot be found."""


class CurrencyConverter:
    """Look up pre-loaded exchange rates and convert monetary amounts."""

    def __init__(self, rates: list) -> None:
        """
        Parameters
        ----------
        rates : list[ExchangeRate]
            All rows from exchange_rates.csv, loaded by loaders.py.
        """
        self._index: Dict[Tuple[str, str, date], Decimal] = {}
        for r in rates:
            key = (r.from_currency, r.to_currency, r.rate_date)
            self._index[key] = r.rate

    def convert(
        self,
        amount: Decimal,
        from_currency: str,
        to_currency: str,
        on_date: date,
    ) -> Decimal:
        """Convert *amount* from *from_currency* to *to_currency* on *on_date*.

        Rules
        -----
        - Same currency → return *amount* unchanged (no lookup).
        - Otherwise look up the exact (from, to, date) row.
        - If not found, raise ``ExchangeRateMissing`` — never silently invert
          or guess a nearby date.  This is a financial-safety requirement.
        """
        if from_currency == to_currency:
            return amount

        key = (from_currency, to_currency, on_date)
        rate = self._index.get(key)
        if rate is None:
            raise ExchangeRateMissing(
                f"No exchange rate found for "
                f"({from_currency} -> {to_currency}) on {on_date}. "
                f"Available keys for this pair: "
                f"{[d for (f, t, d) in self._index if f == from_currency and t == to_currency]}"
            )
        return amount * rate
