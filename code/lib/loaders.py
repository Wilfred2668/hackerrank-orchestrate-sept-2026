"""
CSV → typed objects, with joins and indexed accessor functions.

All loaders resolve paths relative to *this file's* location so the code
works regardless of where the repo is checked out.
"""

from __future__ import annotations

import csv
import os
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional

from .models import (
    ExchangeRate,
    FinancialEvent,
    FinancialProfile,
    Image,
    Message,
    PaymentOption,
    Request,
    SampleRequest,
)

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_DATASET_DIR = os.path.normpath(os.path.join(_THIS_DIR, "..", "..", "dataset"))


def _csv_path(filename: str) -> str:
    return os.path.join(_DATASET_DIR, filename)


def dataset_dir() -> str:
    """Return the absolute path to the dataset/ directory."""
    return _DATASET_DIR


# ---------------------------------------------------------------------------
# Tiny parsing helpers
# ---------------------------------------------------------------------------

def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _parse_datetime(s: str) -> datetime:
    # Handle both "2025-07-29T09:30:00Z" and similar ISO formats
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def _parse_decimal(s: str) -> Decimal:
    s = s.strip()
    if not s:
        raise ValueError("Cannot parse empty string as Decimal")
    return Decimal(s)


def _parse_optional_decimal(s: str) -> Optional[Decimal]:
    s = s.strip()
    if not s:
        return None
    return Decimal(s)


def _parse_optional_int(s: str) -> Optional[int]:
    s = s.strip()
    if not s:
        return None
    return int(s)


def _parse_optional_str(s: str) -> Optional[str]:
    s = s.strip()
    return s if s else None


def _parse_pipe_set(s: str) -> frozenset[str]:
    """Parse a pipe-delimited string into a frozenset.
    Empty string → empty frozenset (not {''})."""
    s = s.strip()
    if not s:
        return frozenset()
    return frozenset(part.strip() for part in s.split("|") if part.strip())


def _parse_bool(s: str) -> bool:
    s = s.strip().lower()
    if s in ("true", "1", "yes"):
        return True
    if s in ("false", "0", "no", ""):
        return False
    raise ValueError(f"Cannot parse '{s}' as bool")


# ---------------------------------------------------------------------------
# Individual row parsers
# ---------------------------------------------------------------------------

def _parse_financial_profile(row: dict) -> FinancialProfile:
    return FinancialProfile(
        user_id=row["user_id"].strip(),
        home_currency=row["home_currency"].strip(),
        current_available_balance=_parse_decimal(row["current_available_balance"]),
        minimum_balance_to_keep=_parse_decimal(row["minimum_balance_to_keep"]),
        financial_priorities=_parse_pipe_set(row["financial_priorities"]),
        expense_categories_to_protect=_parse_pipe_set(
            row["expense_categories_to_protect"]
        ),
        expense_categories_user_is_willing_to_reduce=_parse_pipe_set(
            row["expense_categories_user_is_willing_to_reduce"]
        ),
        expense_categories_user_is_willing_to_stop=_parse_pipe_set(
            row["expense_categories_user_is_willing_to_stop"]
        ),
        payment_methods_user_will_consider=_parse_pipe_set(
            row["payment_methods_user_will_consider"]
        ),
        max_installment_months=_parse_optional_int(row["max_installment_months"]),
    )


def _parse_optional_date(s: str):
    s = s.strip()
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%d").date()


def _parse_financial_event(row: dict) -> FinancialEvent:
    return FinancialEvent(
        event_id=row["event_id"].strip(),
        user_id=row["user_id"].strip(),
        event_type=row["event_type"].strip(),
        description=row["description"].strip(),
        category=row["category"].strip(),
        direction=row["direction"].strip(),  # type: ignore[arg-type]
        amount=_parse_optional_decimal(row["amount"]),
        currency=row["currency"].strip(),
        event_date=_parse_date(row["event_date"]),
        settlement_date=_parse_optional_date(row["settlement_date"]),
        status=row["status"].strip(),  # type: ignore[arg-type]
        linked_event_id=_parse_optional_str(row["linked_event_id"]),
        flexibility=row["flexibility"].strip(),  # type: ignore[arg-type]
        minimum_allowed_amount=_parse_optional_decimal(row["minimum_allowed_amount"]),
    )


def _parse_exchange_rate(row: dict) -> ExchangeRate:
    return ExchangeRate(
        rate_date=_parse_date(row["rate_date"]),
        from_currency=row["from_currency"].strip(),
        to_currency=row["to_currency"].strip(),
        rate=_parse_decimal(row["rate"]),
    )


def _parse_payment_option(row: dict) -> PaymentOption:
    return PaymentOption(
        payment_option_id=row["payment_option_id"].strip(),
        request_id=row["request_id"].strip(),
        payment_method=row["payment_method"].strip(),  # type: ignore[arg-type]
        payment_amount=_parse_decimal(row["payment_amount"]),
        number_of_payments=int(row["number_of_payments"].strip()),
        first_payment_date=_parse_date(row["first_payment_date"]),
        payment_frequency_days=_parse_optional_int(row["payment_frequency_days"]),
        financing_fee=_parse_decimal(row["financing_fee"]),
        total_payable_amount=_parse_decimal(row["total_payable_amount"]),
    )


def _parse_message(row: dict) -> Message:
    return Message(
        message_id=row["message_id"].strip(),
        user_id=row["user_id"].strip(),
        request_id=_parse_optional_str(row["request_id"]),
        related_event_id=_parse_optional_str(row["related_event_id"]),
        sent_at=_parse_datetime(row["sent_at"]),
        source_type=row["source_type"].strip(),
        message_text=row["message_text"],  # preserve raw text as-is
    )


def _parse_image(row: dict) -> Image:
    return Image(
        image_id=row["image_id"].strip(),
        user_id=row["user_id"].strip(),
        request_id=_parse_optional_str(row["request_id"]),
        related_event_id=_parse_optional_str(row["related_event_id"]),
    )


def _parse_request(row: dict) -> Request:
    return Request(
        request_id=row["request_id"].strip(),
        user_id=row["user_id"].strip(),
        request_date=_parse_date(row["request_date"]),
        request_type=row["request_type"].strip(),
        requested_amount=_parse_decimal(row["requested_amount"]),
        desired_completion_date=_parse_date(row["desired_completion_date"]),
        allows_partial_payment=_parse_bool(row["allows_partial_payment"]),
        request_text=row["request_text"],
    )


def _parse_sample_request(row: dict) -> SampleRequest:
    edfp = row.get("earliest_date_for_full_payment", "").strip()
    return SampleRequest(
        request_id=row["request_id"].strip(),
        user_id=row["user_id"].strip(),
        request_date=_parse_date(row["request_date"]),
        request_type=row["request_type"].strip(),
        requested_amount=_parse_decimal(row["requested_amount"]),
        desired_completion_date=_parse_date(row["desired_completion_date"]),
        allows_partial_payment=_parse_bool(row["allows_partial_payment"]),
        request_text=row["request_text"],
        amount_safe_to_pay=_parse_decimal(row["amount_safe_to_pay"]),
        affordability_status=row["affordability_status"].strip(),
        recommended_payment_method=row["recommended_payment_method"].strip(),
        payment_plan=row["payment_plan"].strip(),
        earliest_date_for_full_payment=(
            _parse_date(edfp) if edfp else None
        ),
        spending_changes_needed=row["spending_changes_needed"].strip(),
        decision_explanation=row["decision_explanation"],
    )


# ---------------------------------------------------------------------------
# Generic CSV reader
# ---------------------------------------------------------------------------

def _load_csv(filename: str, parser, *, encoding: str = "utf-8") -> list:
    path = _csv_path(filename)
    results = []
    with open(path, newline="", encoding=encoding) as f:
        reader = csv.DictReader(f)
        for row in reader:
            results.append(parser(row))
    return results


# ---------------------------------------------------------------------------
# DataStore — holds everything and provides indexed access
# ---------------------------------------------------------------------------

class DataStore:
    """Central, read-only data store with indexed accessor functions."""

    def __init__(self) -> None:
        # ----- raw lists -----
        self.profiles: List[FinancialProfile] = _load_csv(
            "financial_profiles.csv", _parse_financial_profile
        )
        self.events: List[FinancialEvent] = _load_csv(
            "financial_events.csv", _parse_financial_event
        )
        self.exchange_rates: List[ExchangeRate] = _load_csv(
            "exchange_rates.csv", _parse_exchange_rate
        )
        self.payment_options: List[PaymentOption] = _load_csv(
            "request_payment_options.csv", _parse_payment_option
        )
        self.messages: List[Message] = _load_csv("messages.csv", _parse_message)
        self.images: List[Image] = _load_csv("images.csv", _parse_image)
        self.requests: List[Request] = _load_csv("requests.csv", _parse_request)
        self.sample_requests: List[SampleRequest] = _load_csv(
            "sample_requests.csv", _parse_sample_request
        )

        # ----- indexes (built once) -----
        self._profile_by_user: Dict[str, FinancialProfile] = {
            p.user_id: p for p in self.profiles
        }

        self._events_by_user: Dict[str, List[FinancialEvent]] = defaultdict(list)
        for e in self.events:
            self._events_by_user[e.user_id].append(e)

        self._options_by_request: Dict[str, List[PaymentOption]] = defaultdict(list)
        for o in self.payment_options:
            self._options_by_request[o.request_id].append(o)

        self._messages_by_request: Dict[str, List[Message]] = defaultdict(list)
        self._messages_general_by_user: Dict[str, List[Message]] = defaultdict(list)
        self._messages_by_event: Dict[str, List[Message]] = defaultdict(list)
        for m in self.messages:
            if m.request_id:
                self._messages_by_request[m.request_id].append(m)
            if m.related_event_id:
                self._messages_by_event[m.related_event_id].append(m)
            # General user messages: no request_id AND no related_event_id
            if not m.request_id and not m.related_event_id:
                self._messages_general_by_user[m.user_id].append(m)

        self._image_by_event: Dict[str, Image] = {}
        self._images_by_request: Dict[str, List[Image]] = defaultdict(list)
        for img in self.images:
            if img.related_event_id:
                self._image_by_event[img.related_event_id] = img
            if img.request_id:
                self._images_by_request[img.request_id].append(img)

        self._request_by_id: Dict[str, Request] = {
            r.request_id: r for r in self.requests
        }

    # ----- accessor functions -----

    def get_profile(self, user_id: str) -> FinancialProfile:
        """Return the profile for *user_id* or raise KeyError."""
        try:
            return self._profile_by_user[user_id]
        except KeyError:
            raise KeyError(f"No financial profile found for user_id='{user_id}'")

    def get_events_for_user(self, user_id: str) -> List[FinancialEvent]:
        """Return all financial events for *user_id* (may be empty)."""
        return self._events_by_user.get(user_id, [])

    def get_payment_options_for_request(
        self, request_id: str
    ) -> List[PaymentOption]:
        """Return all seller/provider payment options for *request_id*."""
        return self._options_by_request.get(request_id, [])

    def get_messages_for_request(self, request_id: str) -> List[Message]:
        """Return messages whose request_id matches."""
        return self._messages_by_request.get(request_id, [])

    def get_messages_for_user_general(self, user_id: str) -> List[Message]:
        """Return messages with no request_id AND no related_event_id,
        but matching user_id — background facts like payroll change notices."""
        return self._messages_general_by_user.get(user_id, [])

    def get_messages_for_event(self, event_id: str) -> List[Message]:
        """Return messages whose related_event_id matches."""
        return self._messages_by_event.get(event_id, [])

    def get_image_for_event(self, event_id: str) -> Optional[Image]:
        """Return the single image linked to *event_id*, or None."""
        return self._image_by_event.get(event_id)

    def get_images_for_request(self, request_id: str) -> List[Image]:
        """Return all images linked to *request_id*."""
        return self._images_by_request.get(request_id, [])

    def get_request(self, request_id: str) -> Request:
        """Return the Request for *request_id* or raise KeyError."""
        try:
            return self._request_by_id[request_id]
        except KeyError:
            raise KeyError(f"No request found for request_id='{request_id}'")
