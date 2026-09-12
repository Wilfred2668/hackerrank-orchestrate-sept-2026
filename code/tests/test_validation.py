"""
Unit tests for Phase 6: Fail-closed output validation and deterministic serialization.

Tests:
  1. Valid cases for each payment method:
     - full_payment (affordable_now)
     - partial_payment (affordable_with_plan)
     - installments (affordable_with_plan)
     - wait (affordable_later)
     - not_recommended (not_affordable)
  2. Invalid bounds and types:
     - amount_safe_to_pay < 0 or > requested_amount
     - Non-decimal amounts
     - Request ID mismatches
  3. Malformed payment plans:
     - Non-chronological dates
     - Missing or negative amounts
     - Invalid date strings
  4. Status/method/plan incoherence:
     - affordable_now with spending changes or non-matching dates/amounts
     - partial_payment with wrong dates, unequal sums, or disallowed requests
     - installments mismatching supplied options or exceeding max_installment_months
     - wait completing after deadline or before request_date
     - not_recommended with non-empty plans or spending changes
  5. Spending change validations:
     - Non-existent ledger event
     - Non-recurring target
     - Protected category target
     - Category not allowed by user stop/reduce preferences
     - Reduced amount below minimum allowed amount
     - Conflicting/duplicate changes on the same stream
  6. Independent simulation safety check:
     - Schedule that breaches minimum balance is caught and rejected fail-closed
  7. Exact header/order and deterministic serialization.
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from decimal import Decimal
import pytest

from lib.decision import DecisionResult
from lib.models import FinancialProfile, PaymentOption, Request
from lib.reconciliation import ReconciledEvent, ReconciledLedger
from lib.validation import (
    OUTPUT_COLUMNS,
    OutputValidationError,
    parse_payment_plan,
    parse_spending_changes,
    serialize_decision_csv_line,
    serialize_decision_row,
    serialize_decisions_to_csv,
    validate_decision_result,
)


# ---------------------------------------------------------------------------
# Test Fixtures & Helpers
# ---------------------------------------------------------------------------

def _build_test_request(
    request_id: str = "req_val_01",
    user_id: str = "test_user",
    request_date: date = date(2026, 5, 1),
    requested_amount: Decimal = Decimal("1000.00"),
    desired_completion_date: date = date(2026, 6, 1),
    allows_partial_payment: bool = True,
) -> Request:
    return Request(
        request_id=request_id,
        user_id=user_id,
        request_date=request_date,
        item_description="Test Laptop Purchase",
        category="electronics",
        requested_amount=requested_amount,
        currency="USD",
        desired_completion_date=desired_completion_date,
        allows_partial_payment=allows_partial_payment,
    )


def _build_test_profile(
    user_id: str = "test_user",
    current_available_balance: Decimal = Decimal("5000.00"),
    minimum_balance_to_keep: Decimal = Decimal("1000.00"),
    payment_methods_user_will_consider: frozenset[str] = frozenset(
        {"full_payment", "partial_payment", "installments"}
    ),
    max_installment_months: int | None = 6,
    expense_categories_to_protect: frozenset[str] = frozenset({"rent", "groceries"}),
    expense_categories_user_is_willing_to_stop: frozenset[str] = frozenset({"streaming"}),
    expense_categories_user_is_willing_to_reduce: frozenset[str] = frozenset({"entertainment"}),
) -> FinancialProfile:
    return FinancialProfile(
        user_id=user_id,
        home_currency="USD",
        current_available_balance=current_available_balance,
        minimum_balance_to_keep=minimum_balance_to_keep,
        financial_priorities=frozenset({"savings"}),
        expense_categories_to_protect=expense_categories_to_protect,
        expense_categories_user_is_willing_to_reduce=expense_categories_user_is_willing_to_reduce,
        expense_categories_user_is_willing_to_stop=expense_categories_user_is_willing_to_stop,
        payment_methods_user_will_consider=payment_methods_user_will_consider,
        max_installment_months=max_installment_months,
    )


def _build_test_event(
    event_id: str,
    event_date: date,
    settlement_date: date | None,
    direction: str,
    amount: Decimal,
    category: str = "general",
    status: str = "settled",
    flexibility: str = "fixed",
    minimum_allowed_amount: Decimal | None = None,
    is_recurring: bool = False,
    is_protected: bool = True,
    is_reducible: bool = False,
    is_stoppable: bool = False,
    description: str | None = None,
    linked_event_id: str | None = None,
) -> ReconciledEvent:
    return ReconciledEvent(
        event_id=event_id,
        user_id="test_user",
        event_type="expense" if direction == "debit" else "income",
        description=description or f"Test {category}",
        category=category,
        direction=direction,  # type: ignore[arg-type]
        original_amount=amount,
        original_currency="USD",
        normalized_amount=amount,
        home_currency="USD",
        event_date=event_date,
        settlement_date=settlement_date,
        status=status,  # type: ignore[arg-type]
        flexibility=flexibility,  # type: ignore[arg-type]
        minimum_allowed_amount=minimum_allowed_amount,
        is_recurring=is_recurring,
        is_protected=is_protected,
        is_reducible=is_reducible,
        is_stoppable=is_stoppable,
        linked_event_id=linked_event_id,
    )


def _build_test_ledger(
    user_id: str = "test_user",
    current_available_balance: Decimal = Decimal("5000.00"),
    minimum_balance_to_keep: Decimal = Decimal("1000.00"),
    events: list[ReconciledEvent] | None = None,
) -> ReconciledLedger:
    ev_list = events or []
    return ReconciledLedger(
        user_id=user_id,
        home_currency="USD",
        current_available_balance=current_available_balance,
        minimum_balance_to_keep=minimum_balance_to_keep,
        all_events=ev_list,
        recurring_events=[e for e in ev_list if e.is_recurring],
        one_time_events=[e for e in ev_list if not e.is_recurring],
        flexible_events=[e for e in ev_list if e.is_reducible or e.is_stoppable],
        fixed_or_protected_events=[e for e in ev_list if e.is_protected],
        unquantifiable_commitments=[],
        recurring_salary_amount=None,
        salary_cancelled=False,
        user_general_matched_count=0,
        matched_deltas_count=0,
        unmatched_deltas_count=0,
        no_change_deltas_count=0,
        protected_categories=frozenset({"rent", "groceries"}),
    )


def _build_test_payment_option(
    payment_option_id: str = "opt_01",
    request_id: str = "req_val_01",
    payment_method: str = "installments",
    number_of_payments: int = 3,
    payment_amount: Decimal = Decimal("350.00"),
    total_payable_amount: Decimal = Decimal("1050.00"),
    first_payment_date: date = date(2026, 5, 1),
    payment_frequency_days: int = 30,
) -> PaymentOption:
    return PaymentOption(
        payment_option_id=payment_option_id,
        request_id=request_id,
        payment_method=payment_method,  # type: ignore[arg-type]
        number_of_payments=number_of_payments,
        payment_amount=payment_amount,
        total_payable_amount=total_payable_amount,
        currency="USD",
        first_payment_date=first_payment_date,
        payment_frequency_days=payment_frequency_days,
    )


# ---------------------------------------------------------------------------
# Test 1: Valid Outcomes Across All Payment Methods
# ---------------------------------------------------------------------------

def test_validate_valid_full_payment_affordable_now():
    """Valid full payment safe immediately with no changes."""
    req = _build_test_request(requested_amount=Decimal("1000.00"))
    profile = _build_test_profile(current_available_balance=Decimal("5000.00"))
    ledger = _build_test_ledger(current_available_balance=Decimal("5000.00"))

    decision = DecisionResult(
        request_id=req.request_id,
        amount_safe_to_pay=Decimal("1000.00"),
        affordability_status="affordable_now",
        recommended_payment_method="full_payment",
        payment_plan="2026-05-01:1000",
        earliest_date_for_full_payment=date(2026, 5, 1),
        spending_changes_needed="none",
        decision_explanation="Full payment is completely safe today.",
    )

    validated = validate_decision_result(req, profile, ledger, [], decision)
    assert validated == decision


def test_validate_valid_partial_payment():
    """Valid partial payment split over two safe dates."""
    req = _build_test_request(
        requested_amount=Decimal("2000.00"),
        desired_completion_date=date(2026, 5, 25),
    )
    # Available 2000, min 1000 -> safe today is 1000
    profile = _build_test_profile(current_available_balance=Decimal("2000.00"))
    salary_event = _build_test_event(
        event_id="sal_01",
        event_date=date(2026, 5, 20),
        settlement_date=date(2026, 5, 20),
        direction="credit",
        amount=Decimal("3000.00"),
        category="salary",
        status="scheduled",
    )
    ledger = _build_test_ledger(
        current_available_balance=Decimal("2000.00"),
        events=[salary_event],
    )

    decision = DecisionResult(
        request_id=req.request_id,
        amount_safe_to_pay=Decimal("1000.00"),
        affordability_status="affordable_with_plan",
        recommended_payment_method="partial_payment",
        payment_plan="2026-05-01:1000|2026-05-20:1000",
        earliest_date_for_full_payment=date(2026, 5, 20),
        spending_changes_needed="none",
        decision_explanation="Partial payment safe today and completed after salary.",
    )

    validated = validate_decision_result(req, profile, ledger, [], decision)
    assert validated == decision


def test_validate_valid_installments():
    """Valid installment plan matching a permitted option."""
    req = _build_test_request(
        requested_amount=Decimal("1000.00"),
        desired_completion_date=date(2026, 7, 15),
    )
    opt = _build_test_payment_option(
        payment_option_id="opt_inst_3m",
        number_of_payments=3,
        payment_amount=Decimal("350.00"),
        total_payable_amount=Decimal("1050.00"),
        first_payment_date=date(2026, 5, 1),
        payment_frequency_days=30,
    )
    profile = _build_test_profile(current_available_balance=Decimal("5000.00"), max_installment_months=3)
    ledger = _build_test_ledger(current_available_balance=Decimal("5000.00"))

    decision = DecisionResult(
        request_id=req.request_id,
        amount_safe_to_pay=Decimal("1000.00"),
        affordability_status="affordable_with_plan",
        recommended_payment_method="installments",
        payment_plan="2026-05-01:350|2026-05-31:350|2026-06-30:350",
        earliest_date_for_full_payment=date(2026, 5, 1),
        spending_changes_needed="none",
        decision_explanation="3-month installment plan accepted.",
    )

    validated = validate_decision_result(req, profile, ledger, [opt], decision)
    assert validated == decision


def test_validate_valid_wait():
    """Valid wait recommendation when full payment becomes safe later."""
    req = _build_test_request(
        requested_amount=Decimal("3000.00"),
        desired_completion_date=date(2026, 5, 25),
    )
    # Available 1500, min 1000 -> unsafe today
    profile = _build_test_profile(current_available_balance=Decimal("1500.00"))
    sal = _build_test_event(
        event_id="sal_wait",
        event_date=date(2026, 5, 15),
        settlement_date=date(2026, 5, 15),
        direction="credit",
        amount=Decimal("4000.00"),
        category="salary",
        status="scheduled",
    )
    ledger = _build_test_ledger(
        current_available_balance=Decimal("1500.00"),
        events=[sal],
    )

    decision = DecisionResult(
        request_id=req.request_id,
        amount_safe_to_pay=Decimal("500.00"),
        affordability_status="affordable_later",
        recommended_payment_method="wait",
        payment_plan="2026-05-15:3000",
        earliest_date_for_full_payment=date(2026, 5, 15),
        spending_changes_needed="none",
        decision_explanation="Wait until May 15 salary.",
    )

    validated = validate_decision_result(req, profile, ledger, [], decision)
    assert validated == decision


def test_validate_valid_not_recommended_preserves_earliest_date():
    """Valid not_recommended with preserved earliest full-payment date beyond deadline."""
    req = _build_test_request(
        requested_amount=Decimal("3000.00"),
        desired_completion_date=date(2026, 5, 10),  # tight deadline
    )
    profile = _build_test_profile(current_available_balance=Decimal("1500.00"))
    sal = _build_test_event(
        event_id="sal_late",
        event_date=date(2026, 5, 25),
        settlement_date=date(2026, 5, 25),
        direction="credit",
        amount=Decimal("4000.00"),
        category="salary",
        status="scheduled",
    )
    ledger = _build_test_ledger(
        current_available_balance=Decimal("1500.00"),
        events=[sal],
    )

    decision = DecisionResult(
        request_id=req.request_id,
        amount_safe_to_pay=Decimal("500.00"),
        affordability_status="not_affordable",
        recommended_payment_method="not_recommended",
        payment_plan="none",
        earliest_date_for_full_payment=date(2026, 5, 25),
        spending_changes_needed="none",
        decision_explanation="Cannot complete by desired completion date.",
    )

    validated = validate_decision_result(req, profile, ledger, [], decision)
    assert validated == decision


# ---------------------------------------------------------------------------
# Test 2: Invalid Bounds and Field Constraints
# ---------------------------------------------------------------------------

def test_validate_rejects_negative_amount_safe():
    req = _build_test_request()
    profile = _build_test_profile()
    ledger = _build_test_ledger()
    decision = DecisionResult(
        request_id=req.request_id,
        amount_safe_to_pay=Decimal("-10.00"),
        affordability_status="not_affordable",
        recommended_payment_method="not_recommended",
        payment_plan="none",
        earliest_date_for_full_payment=None,
        spending_changes_needed="none",
        decision_explanation="Invalid negative amount safe.",
    )
    with pytest.raises(OutputValidationError, match="cannot be negative"):
        validate_decision_result(req, profile, ledger, [], decision)


def test_validate_rejects_amount_safe_exceeding_requested():
    req = _build_test_request(requested_amount=Decimal("500.00"))
    profile = _build_test_profile()
    ledger = _build_test_ledger()
    decision = DecisionResult(
        request_id=req.request_id,
        amount_safe_to_pay=Decimal("600.00"),
        affordability_status="affordable_now",
        recommended_payment_method="full_payment",
        payment_plan="2026-05-01:500",
        earliest_date_for_full_payment=date(2026, 5, 1),
        spending_changes_needed="none",
        decision_explanation="Safe amount exceeds requested amount.",
    )
    with pytest.raises(OutputValidationError, match="cannot exceed requested_amount"):
        validate_decision_result(req, profile, ledger, [], decision)


def test_validate_rejects_mismatched_request_id():
    req = _build_test_request(request_id="req_correct")
    profile = _build_test_profile()
    ledger = _build_test_ledger()
    decision = DecisionResult(
        request_id="req_wrong",
        amount_safe_to_pay=Decimal("500.00"),
        affordability_status="not_affordable",
        recommended_payment_method="not_recommended",
        payment_plan="none",
        earliest_date_for_full_payment=None,
        spending_changes_needed="none",
        decision_explanation="Wrong ID.",
    )
    with pytest.raises(OutputValidationError, match="does not match"):
        validate_decision_result(req, profile, ledger, [], decision)


# ---------------------------------------------------------------------------
# Test 3: Malformed Payment Plans
# ---------------------------------------------------------------------------

def test_validate_rejects_non_chronological_payment_plan():
    req = _build_test_request(requested_amount=Decimal("2000.00"))
    profile = _build_test_profile()
    ledger = _build_test_ledger()
    decision = DecisionResult(
        request_id=req.request_id,
        amount_safe_to_pay=Decimal("1000.00"),
        affordability_status="affordable_with_plan",
        recommended_payment_method="partial_payment",
        payment_plan="2026-05-20:1000|2026-05-01:1000",  # backwards dates
        earliest_date_for_full_payment=date(2026, 5, 20),
        spending_changes_needed="none",
        decision_explanation="Non-chronological dates.",
    )
    with pytest.raises(OutputValidationError, match="chronological order"):
        validate_decision_result(req, profile, ledger, [], decision)


def test_validate_rejects_empty_payment_plan():
    req = _build_test_request()
    profile = _build_test_profile()
    ledger = _build_test_ledger()
    decision = DecisionResult(
        request_id=req.request_id,
        amount_safe_to_pay=Decimal("0.00"),
        affordability_status="not_affordable",
        recommended_payment_method="not_recommended",
        payment_plan="",  # must be 'none'
        earliest_date_for_full_payment=None,
        spending_changes_needed="none",
        decision_explanation="Empty string plan.",
    )
    with pytest.raises(OutputValidationError, match="payment_plan cannot be empty"):
        validate_decision_result(req, profile, ledger, [], decision)


# ---------------------------------------------------------------------------
# Test 4: Status / Method / Plan Incoherence
# ---------------------------------------------------------------------------

def test_validate_rejects_affordable_now_with_spending_changes():
    req = _build_test_request(requested_amount=Decimal("1000.00"))
    profile = _build_test_profile()
    ledger = _build_test_ledger()
    decision = DecisionResult(
        request_id=req.request_id,
        amount_safe_to_pay=Decimal("1000.00"),
        affordability_status="affordable_now",
        recommended_payment_method="full_payment",
        payment_plan="2026-05-01:1000",
        earliest_date_for_full_payment=date(2026, 5, 1),
        spending_changes_needed="stop:ev_stream",
        decision_explanation="Cannot have spending changes with affordable_now.",
    )
    with pytest.raises(OutputValidationError, match="cannot require spending changes"):
        validate_decision_result(req, profile, ledger, [], decision)


def test_validate_rejects_partial_payment_unequal_sum():
    req = _build_test_request(requested_amount=Decimal("2000.00"))
    profile = _build_test_profile()
    ledger = _build_test_ledger()
    decision = DecisionResult(
        request_id=req.request_id,
        amount_safe_to_pay=Decimal("1000.00"),
        affordability_status="affordable_with_plan",
        recommended_payment_method="partial_payment",
        payment_plan="2026-05-01:1000|2026-05-20:900",  # sums to 1900 != 2000
        earliest_date_for_full_payment=date(2026, 5, 20),
        spending_changes_needed="none",
        decision_explanation="Payments sum to 1900.",
    )
    with pytest.raises(OutputValidationError, match="second payment amount must equal"):
        validate_decision_result(req, profile, ledger, [], decision)


def test_validate_rejects_partial_payment_when_disallowed():
    req = _build_test_request(allows_partial_payment=False)
    profile = _build_test_profile()
    ledger = _build_test_ledger()
    decision = DecisionResult(
        request_id=req.request_id,
        amount_safe_to_pay=Decimal("500.00"),
        affordability_status="affordable_with_plan",
        recommended_payment_method="partial_payment",
        payment_plan="2026-05-01:500|2026-05-20:500",
        earliest_date_for_full_payment=date(2026, 5, 20),
        spending_changes_needed="none",
        decision_explanation="Partial payment not allowed.",
    )
    with pytest.raises(OutputValidationError, match="request does not allow partial payment"):
        validate_decision_result(req, profile, ledger, [], decision)


def test_validate_rejects_installment_not_matching_options():
    req = _build_test_request()
    profile = _build_test_profile()
    ledger = _build_test_ledger()
    decision = DecisionResult(
        request_id=req.request_id,
        amount_safe_to_pay=Decimal("1000.00"),
        affordability_status="affordable_with_plan",
        recommended_payment_method="installments",
        payment_plan="2026-05-01:500|2026-06-01:500",  # Fake option not supplied
        earliest_date_for_full_payment=date(2026, 5, 1),
        spending_changes_needed="none",
        decision_explanation="Invented installment option.",
    )
    with pytest.raises(OutputValidationError, match="does not match any permitted supplied payment option"):
        validate_decision_result(req, profile, ledger, [], decision)


def test_validate_rejects_wait_past_desired_completion_date():
    req = _build_test_request(desired_completion_date=date(2026, 5, 15))
    profile = _build_test_profile()
    ledger = _build_test_ledger()
    decision = DecisionResult(
        request_id=req.request_id,
        amount_safe_to_pay=Decimal("500.00"),
        affordability_status="affordable_later",
        recommended_payment_method="wait",
        payment_plan="2026-05-25:1000",
        earliest_date_for_full_payment=date(2026, 5, 25),  # 25th > 15th
        spending_changes_needed="none",
        decision_explanation="Completes after desired completion date.",
    )
    with pytest.raises(OutputValidationError, match="on or before desired_completion_date"):
        validate_decision_result(req, profile, ledger, [], decision)


# ---------------------------------------------------------------------------
# Test 5: Spending Change Validation & Stream Scoping
# ---------------------------------------------------------------------------

def test_validate_rejects_spending_change_on_nonexistent_event():
    req = _build_test_request()
    profile = _build_test_profile()
    ledger = _build_test_ledger()
    decision = DecisionResult(
        request_id=req.request_id,
        amount_safe_to_pay=Decimal("1000.00"),
        affordability_status="affordable_with_plan",
        recommended_payment_method="full_payment",
        payment_plan="2026-05-01:1000",
        earliest_date_for_full_payment=date(2026, 5, 1),
        spending_changes_needed="stop:nonexistent_event_999",
        decision_explanation="Nonexistent target.",
    )
    with pytest.raises(OutputValidationError, match="does not exist in user's ledger"):
        validate_decision_result(req, profile, ledger, [], decision)


def test_validate_rejects_spending_change_on_protected_category():
    req = _build_test_request()
    profile = _build_test_profile(expense_categories_to_protect=frozenset({"rent"}))
    rent_event = _build_test_event(
        event_id="ev_rent",
        event_date=date(2026, 5, 1),
        settlement_date=date(2026, 5, 1),
        direction="debit",
        amount=Decimal("500.00"),
        category="rent",
        flexibility="stoppable",
        is_recurring=True,
    )
    ledger = _build_test_ledger(events=[rent_event])
    decision = DecisionResult(
        request_id=req.request_id,
        amount_safe_to_pay=Decimal("1000.00"),
        affordability_status="affordable_with_plan",
        recommended_payment_method="full_payment",
        payment_plan="2026-05-01:1000",
        earliest_date_for_full_payment=date(2026, 5, 1),
        spending_changes_needed="stop:ev_rent",
        decision_explanation="Cannot stop protected category.",
    )
    with pytest.raises(OutputValidationError, match="protected category"):
        validate_decision_result(req, profile, ledger, [], decision)


def test_validate_rejects_reduction_below_minimum():
    req = _build_test_request()
    profile = _build_test_profile(expense_categories_user_is_willing_to_reduce=frozenset({"entertainment"}))
    ent_event = _build_test_event(
        event_id="ev_ent",
        event_date=date(2026, 5, 5),
        settlement_date=date(2026, 5, 5),
        direction="debit",
        amount=Decimal("100.00"),
        category="entertainment",
        flexibility="reducible",
        minimum_allowed_amount=Decimal("40.00"),
        is_recurring=True,
        is_protected=False,
        is_reducible=True,
    )
    ledger = _build_test_ledger(events=[ent_event])
    decision = DecisionResult(
        request_id=req.request_id,
        amount_safe_to_pay=Decimal("1000.00"),
        affordability_status="affordable_with_plan",
        recommended_payment_method="full_payment",
        payment_plan="2026-05-01:1000",
        earliest_date_for_full_payment=date(2026, 5, 1),
        spending_changes_needed="reduce_to:ev_ent:25",  # 25 < minimum 40
        decision_explanation="Below minimum allowed amount.",
    )
    with pytest.raises(OutputValidationError, match="below minimum allowed amount"):
        validate_decision_result(req, profile, ledger, [], decision)


def test_validate_rejects_conflicting_changes_on_same_stream():
    req = _build_test_request()
    profile = _build_test_profile(
        expense_categories_user_is_willing_to_stop=frozenset({"streaming"}),
        expense_categories_user_is_willing_to_reduce=frozenset({"streaming"}),
    )
    stream_ev1 = _build_test_event(
        event_id="ev_stream_01",
        event_date=date(2026, 5, 5),
        settlement_date=date(2026, 5, 5),
        direction="debit",
        amount=Decimal("50.00"),
        category="streaming",
        flexibility="reducible_or_stoppable",
        minimum_allowed_amount=Decimal("20.00"),
        is_recurring=True,
        is_protected=False,
        description="Music streaming plan",
    )
    stream_ev2 = _build_test_event(
        event_id="ev_stream_02",
        event_date=date(2026, 6, 5),
        settlement_date=date(2026, 6, 5),
        direction="debit",
        amount=Decimal("50.00"),
        category="streaming",
        flexibility="reducible_or_stoppable",
        minimum_allowed_amount=Decimal("20.00"),
        is_recurring=True,
        is_protected=False,
        description="Music streaming plan",
    )
    ledger = _build_test_ledger(events=[stream_ev1, stream_ev2])

    decision = DecisionResult(
        request_id=req.request_id,
        amount_safe_to_pay=Decimal("1000.00"),
        affordability_status="affordable_with_plan",
        recommended_payment_method="full_payment",
        payment_plan="2026-05-01:1000",
        earliest_date_for_full_payment=date(2026, 5, 1),
        spending_changes_needed="stop:ev_stream_01|reduce_to:ev_stream_02:25",  # same stream
        decision_explanation="Conflict on same stream.",
    )
    with pytest.raises(OutputValidationError, match="Conflicting or duplicate spending changes"):
        validate_decision_result(req, profile, ledger, [], decision)


# ---------------------------------------------------------------------------
# Test 6: Independent Financial Safety Check Rejection
# ---------------------------------------------------------------------------

def test_validate_rejects_financially_unsafe_plan():
    """Validator independently re-runs simulation and catches unsafe schedules."""
    req = _build_test_request(requested_amount=Decimal("4500.00"))
    # Balance 5000, min 1000. Headroom is 4000. Paying 4500 drops balance to 500 < 1000!
    profile = _build_test_profile(
        current_available_balance=Decimal("5000.00"),
        minimum_balance_to_keep=Decimal("1000.00"),
    )
    ledger = _build_test_ledger(
        current_available_balance=Decimal("5000.00"),
        minimum_balance_to_keep=Decimal("1000.00"),
    )

    # Deceitful decision claiming 4500 is affordable_now
    decision = DecisionResult(
        request_id=req.request_id,
        amount_safe_to_pay=Decimal("4500.00"),
        affordability_status="affordable_now",
        recommended_payment_method="full_payment",
        payment_plan="2026-05-01:4500",
        earliest_date_for_full_payment=date(2026, 5, 1),
        spending_changes_needed="none",
        decision_explanation="Falsely claiming full payment is safe.",
    )

    with pytest.raises(OutputValidationError, match="Financial safety check failed"):
        validate_decision_result(req, profile, ledger, [], decision)


# ---------------------------------------------------------------------------
# Test 7: Deterministic Serialization & Header Formatting
# ---------------------------------------------------------------------------

def test_serialization_exact_columns_and_formatting():
    decision = DecisionResult(
        request_id="req_001",
        amount_safe_to_pay=Decimal("1250.00"),
        affordability_status="affordable_with_plan",
        recommended_payment_method="partial_payment",
        payment_plan="2026-05-01:1250|2026-05-20:1250",
        earliest_date_for_full_payment=date(2026, 5, 20),
        spending_changes_needed="stop:ev_sub_01",
        decision_explanation="Approved with partial payment and streaming cancellation.",
    )

    row = serialize_decision_row(decision)
    assert len(row) == 8
    assert row == [
        "req_001",
        "1250",
        "affordable_with_plan",
        "partial_payment",
        "2026-05-01:1250|2026-05-20:1250",
        "2026-05-20",
        "stop:ev_sub_01",
        "Approved with partial payment and streaming cancellation.",
    ]

    line = serialize_decision_csv_line(decision)
    assert line.endswith("\n")
    assert "1250,affordable_with_plan,partial_payment" in line

    csv_output = serialize_decisions_to_csv([decision])
    header, data_line = csv_output.strip().split("\n")
    assert header == ",".join(OUTPUT_COLUMNS)
    assert "req_001" in data_line
