"""
Typed dataclasses for every CSV row type in the Buy or Wait? dataset.

All monetary fields use decimal.Decimal — never float — so that payment_plan
values can sum exactly to requested_amount in later phases.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Optional


# ---------------------------------------------------------------------------
# Financial Profile
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FinancialProfile:
    user_id: str
    home_currency: str
    current_available_balance: Decimal
    minimum_balance_to_keep: Decimal
    financial_priorities: frozenset[str]
    expense_categories_to_protect: frozenset[str]
    expense_categories_user_is_willing_to_reduce: frozenset[str]
    expense_categories_user_is_willing_to_stop: frozenset[str]
    payment_methods_user_will_consider: frozenset[str]
    max_installment_months: Optional[int]  # None ⇒ will not consider installments


# ---------------------------------------------------------------------------
# Financial Event
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FinancialEvent:
    event_id: str
    user_id: str
    event_type: str
    description: str
    category: str
    direction: Literal["credit", "debit", "non_cash"]
    amount: Optional[Decimal]  # None when the CSV cell is blank (16 rows)
    currency: str              # always populated even when amount is blank
    event_date: date
    settlement_date: Optional[date]  # None for unrealized investment_valuation (10 rows)
    status: Literal[
        "settled", "pending", "scheduled", "failed", "cancelled", "unrealized"
    ]
    linked_event_id: Optional[str]
    flexibility: Literal[
        "fixed", "reducible", "stoppable", "reducible_or_stoppable"
    ]
    minimum_allowed_amount: Optional[Decimal]  # None for fixed/stoppable

    @property
    def amount_pending_extraction(self) -> bool:
        """True when the amount was blank in the source CSV and must be
        extracted from an associated image in a later phase."""
        return self.amount is None


# ---------------------------------------------------------------------------
# Exchange Rate
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExchangeRate:
    rate_date: date
    from_currency: str
    to_currency: str
    rate: Decimal


# ---------------------------------------------------------------------------
# Payment Option (seller/provider-supplied)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PaymentOption:
    payment_option_id: str
    request_id: str
    payment_method: Literal["full_payment", "installments"]
    payment_amount: Decimal
    number_of_payments: int
    first_payment_date: date
    payment_frequency_days: Optional[int]  # blank/irrelevant when number_of_payments==1
    financing_fee: Decimal
    total_payable_amount: Decimal


# ---------------------------------------------------------------------------
# Message
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Message:
    message_id: str
    user_id: str
    request_id: Optional[str]          # populated on 128/215 rows
    related_event_id: Optional[str]    # populated on 39/215 rows
    sent_at: datetime
    source_type: str
    message_text: str


# ---------------------------------------------------------------------------
# Image
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Image:
    image_id: str
    user_id: str
    request_id: Optional[str]
    related_event_id: Optional[str]

    def resolve_path(self, dataset_dir: str) -> str:
        """Return the absolute path to the image file under dataset/media/images/
        and assert the file exists."""
        path = os.path.join(dataset_dir, "media", "images", f"{self.image_id}.png")
        if not os.path.isfile(path):
            raise FileNotFoundError(
                f"Image file not found: {path} (image_id={self.image_id})"
            )
        return path


# ---------------------------------------------------------------------------
# Request (from requests.csv / sample_requests.csv)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Request:
    request_id: str
    user_id: str
    request_date: date
    request_type: str
    requested_amount: Decimal
    desired_completion_date: date
    allows_partial_payment: bool
    request_text: str


# ---------------------------------------------------------------------------
# Sample Request (sample_requests.csv — includes ground-truth output columns)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SampleRequest:
    """A request from sample_requests.csv that includes the ground-truth
    output columns.  Inherits all input fields from Request plus the output."""
    request_id: str
    user_id: str
    request_date: date
    request_type: str
    requested_amount: Decimal
    desired_completion_date: date
    allows_partial_payment: bool
    request_text: str
    # ---- output columns ----
    amount_safe_to_pay: Decimal
    affordability_status: str
    recommended_payment_method: str
    payment_plan: str
    earliest_date_for_full_payment: Optional[date]
    spending_changes_needed: str
    decision_explanation: str
