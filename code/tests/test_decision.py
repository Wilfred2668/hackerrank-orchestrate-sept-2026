"""
Unit tests for Phase 5: Deterministic Decision and Payment-Plan Construction.

Validates:
  1. Full payment chosen over more expensive installments.
  2. Partial payment exact two-payment format and sum.
  3. Installment rejection by max months, deadline, or simulation safety.
  4. Wait selected when full payment becomes safe later.
  5. Spending changes restricted to allowed flexible recurring events.
  6. A spending-change full payment returning affordable_with_plan.
  7. Tie-breaks for total cost, first-payment date, payment count, and option ID.
  8. No candidate producing an invalid status/method/plan combination.
"""

from __future__ import annotations

import json
import os
import pytest
from datetime import date, timedelta
from decimal import Decimal

from lib.decision import (
    AtomicSpendingChange,
    CandidatePlan,
    DecisionResult,
    build_candidate_plans,
    candidate_ranking_key,
    evaluate_decision,
    find_eligible_spending_changes,
    generate_spending_change_combinations,
)
from lib.loaders import DataStore
from lib.models import FinancialProfile, PaymentOption, Request
from lib.reconciliation import ReconciledEvent, ReconciledLedger, reconcile_user_ledger
from lib.simulation import StreamSpendingChange, evaluate_schedule_safety, simulate_cash_flow


# ---------------------------------------------------------------------------
# Test Fixtures & Helpers
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def datastore() -> DataStore:
    return DataStore()


@pytest.fixture(scope="module")
def extracted_data() -> dict:
    repo_root = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
    json_path = os.path.join(repo_root, "code", "data", "extracted_deltas.json")
    with open(json_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _build_test_ledger(
    user_id: str = "test_user",
    home_currency: str = "USD",
    current_available_balance: Decimal = Decimal("10000.00"),
    minimum_balance_to_keep: Decimal = Decimal("2000.00"),
    events: list[ReconciledEvent] | None = None,
    protected_categories: frozenset[str] | None = None,
) -> ReconciledLedger:
    ev_list = events or []
    return ReconciledLedger(
        user_id=user_id,
        home_currency=home_currency,
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
        protected_categories=protected_categories if protected_categories is not None else frozenset(),
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
) -> ReconciledEvent:
    return ReconciledEvent(
        event_id=event_id,
        user_id="test_user",
        event_type="expense" if direction == "debit" else "income",
        description=description or f"Test {category}",
        category=category,
        direction=direction,  # type: ignore
        original_amount=amount,
        original_currency="USD",
        normalized_amount=amount,
        home_currency="USD",
        event_date=event_date,
        settlement_date=settlement_date or event_date,
        status=status,  # type: ignore
        flexibility=flexibility,  # type: ignore
        minimum_allowed_amount=minimum_allowed_amount,
        is_recurring=is_recurring,
        is_protected=is_protected,
        is_reducible=is_reducible,
        is_stoppable=is_stoppable,
    )


def _build_test_profile(
    user_id: str = "test_user",
    home_currency: str = "USD",
    current_available_balance: Decimal = Decimal("10000.00"),
    minimum_balance_to_keep: Decimal = Decimal("2000.00"),
    expense_categories_to_protect: frozenset[str] = frozenset({"rent"}),
    expense_categories_user_is_willing_to_reduce: frozenset[str] = frozenset(),
    expense_categories_user_is_willing_to_stop: frozenset[str] = frozenset(),
    payment_methods_user_will_consider: frozenset[str] = frozenset({"full_payment"}),
    max_installment_months: int | None = None,
) -> FinancialProfile:
    return FinancialProfile(
        user_id=user_id,
        home_currency=home_currency,
        current_available_balance=current_available_balance,
        minimum_balance_to_keep=minimum_balance_to_keep,
        financial_priorities=frozenset(),
        expense_categories_to_protect=expense_categories_to_protect,
        expense_categories_user_is_willing_to_reduce=expense_categories_user_is_willing_to_reduce,
        expense_categories_user_is_willing_to_stop=expense_categories_user_is_willing_to_stop,
        payment_methods_user_will_consider=payment_methods_user_will_consider,
        max_installment_months=max_installment_months,
    )


def _build_test_request(
    request_id: str = "req_test",
    request_date: date = date(2026, 5, 1),
    requested_amount: Decimal = Decimal("1000.00"),
    desired_completion_date: date = date(2026, 6, 1),
    allows_partial_payment: bool = False,
) -> Request:
    return Request(
        request_id=request_id,
        user_id="test_user",
        request_date=request_date,
        request_type="purchase",
        requested_amount=requested_amount,
        desired_completion_date=desired_completion_date,
        allows_partial_payment=allows_partial_payment,
        request_text="Test request",
    )


# ---------------------------------------------------------------------------
# Test 1: Full Payment Chosen Over More Expensive Installments
# ---------------------------------------------------------------------------

def test_full_payment_chosen_over_expensive_installments():
    """When both full payment and installments are safe, full payment is preferred
    due to zero financing fees (criterion 3: lowest total amount paid) and fewer payments.
    """
    req_date = date(2026, 5, 1)
    req = _build_test_request(
        request_date=req_date,
        requested_amount=Decimal("1000.00"),
        desired_completion_date=date(2026, 7, 1),
    )
    profile = _build_test_profile(
        current_available_balance=Decimal("5000.00"),
        minimum_balance_to_keep=Decimal("1000.00"),
        payment_methods_user_will_consider=frozenset({"full_payment", "installments"}),
        max_installment_months=3,
    )
    ledger = _build_test_ledger(
        current_available_balance=Decimal("5000.00"),
        minimum_balance_to_keep=Decimal("1000.00"),
    )
    # Installment option with 100.00 fee
    opt = PaymentOption(
        payment_option_id="opt_1",
        request_id=req.request_id,
        payment_method="installments",
        payment_amount=Decimal("366.67"),
        number_of_payments=3,
        first_payment_date=req_date,
        payment_frequency_days=30,
        financing_fee=Decimal("100.00"),
        total_payable_amount=Decimal("1100.00"),
    )

    decision = evaluate_decision(req, profile, ledger, [opt])
    assert decision.affordability_status == "affordable_now"
    assert decision.recommended_payment_method == "full_payment"
    assert decision.payment_plan == "2026-05-01:1000"
    assert decision.earliest_date_for_full_payment == req_date
    assert decision.spending_changes_needed == "none"


# ---------------------------------------------------------------------------
# Test 2: Partial Payment Exact Two-Payment Format and Sum
# ---------------------------------------------------------------------------

def test_partial_payment_exact_two_payment_format_and_sum():
    """Partial payment plan contains exactly:
      - (request_date, amount_safe_to_pay)
      - (earliest_date_for_full_payment, requested_amount - amount_safe_to_pay)
    Both payments sum exactly to requested_amount.
    """
    req_date = date(2026, 5, 1)
    req = _build_test_request(
        request_date=req_date,
        requested_amount=Decimal("3000.00"),
        desired_completion_date=date(2026, 6, 1),
        allows_partial_payment=True,
    )
    # Available balance 3500, min 2000 -> headroom 1500 on request_date
    # Salary of 3000 arrives on 2026-05-15
    events = [
        _build_test_event(
            event_id="sal_1",
            event_date=date(2026, 5, 15),
            settlement_date=date(2026, 5, 15),
            direction="credit",
            amount=Decimal("3000.00"),
            category="salary",
            status="scheduled",
        )
    ]
    profile = _build_test_profile(
        current_available_balance=Decimal("3500.00"),
        minimum_balance_to_keep=Decimal("2000.00"),
        payment_methods_user_will_consider=frozenset({"partial_payment"}),
    )
    ledger = _build_test_ledger(
        current_available_balance=Decimal("3500.00"),
        minimum_balance_to_keep=Decimal("2000.00"),
        events=events,
    )

    decision = evaluate_decision(req, profile, ledger, [])
    assert decision.affordability_status == "affordable_with_plan"
    assert decision.recommended_payment_method == "partial_payment"
    assert decision.amount_safe_to_pay == Decimal("1500")
    assert decision.earliest_date_for_full_payment == date(2026, 5, 15)

    # Validate exact 2-payment format
    parts = decision.payment_plan.split("|")
    assert len(parts) == 2
    d1, a1 = parts[0].split(":")
    d2, a2 = parts[1].split(":")
    assert d1 == "2026-05-01"
    assert Decimal(a1) == Decimal("1500")
    assert d2 == "2026-05-15"
    assert Decimal(a2) == Decimal("1500")
    assert Decimal(a1) + Decimal(a2) == req.requested_amount


# ---------------------------------------------------------------------------
# Test 3: Installment Rejection (Max Months, Deadline, Safety)
# ---------------------------------------------------------------------------

def test_installment_rejection_by_max_months():
    """Option exceeding profile.max_installment_months is rejected."""
    req_date = date(2026, 5, 1)
    req = _build_test_request(
        request_date=req_date,
        requested_amount=Decimal("1000.00"),
        desired_completion_date=date(2026, 9, 1),
    )
    profile = _build_test_profile(
        payment_methods_user_will_consider=frozenset({"installments"}),
        max_installment_months=3,  # Max 3 months
    )
    ledger = _build_test_ledger()
    opt_6_months = PaymentOption(
        payment_option_id="opt_6",
        request_id=req.request_id,
        payment_method="installments",
        payment_amount=Decimal("180.00"),
        number_of_payments=6,  # 6 > 3 -> Rejected
        first_payment_date=req_date,
        payment_frequency_days=30,
        financing_fee=Decimal("80.00"),
        total_payable_amount=Decimal("1080.00"),
    )
    decision = evaluate_decision(req, profile, ledger, [opt_6_months])
    assert decision.affordability_status == "not_affordable"
    assert decision.recommended_payment_method == "not_recommended"


def test_installment_rejection_by_deadline():
    """Option completing after desired_completion_date is rejected."""
    req_date = date(2026, 5, 1)
    req = _build_test_request(
        request_date=req_date,
        requested_amount=Decimal("1000.00"),
        desired_completion_date=date(2026, 6, 15),  # Early deadline
    )
    profile = _build_test_profile(
        payment_methods_user_will_consider=frozenset({"installments"}),
        max_installment_months=3,
    )
    ledger = _build_test_ledger()
    # 3 payments at 30-day frequency: May 1, May 31, June 30 (June 30 > June 15)
    opt = PaymentOption(
        payment_option_id="opt_late",
        request_id=req.request_id,
        payment_method="installments",
        payment_amount=Decimal("350.00"),
        number_of_payments=3,
        first_payment_date=req_date,
        payment_frequency_days=30,
        financing_fee=Decimal("50.00"),
        total_payable_amount=Decimal("1050.00"),
    )
    decision = evaluate_decision(req, profile, ledger, [opt])
    assert decision.affordability_status == "not_affordable"
    assert decision.recommended_payment_method == "not_recommended"


def test_installment_rejection_by_simulation_safety():
    """Option that breaches minimum balance during simulation is rejected."""
    req_date = date(2026, 5, 1)
    req = _build_test_request(
        request_date=req_date,
        requested_amount=Decimal("3000.00"),
        desired_completion_date=date(2026, 8, 1),
    )
    # Available 2500, min 2000 -> headroom 500
    # Payment 1 of 1050 breaches minimum balance immediately
    profile = _build_test_profile(
        current_available_balance=Decimal("2500.00"),
        minimum_balance_to_keep=Decimal("2000.00"),
        payment_methods_user_will_consider=frozenset({"installments"}),
        max_installment_months=3,
    )
    ledger = _build_test_ledger(
        current_available_balance=Decimal("2500.00"),
        minimum_balance_to_keep=Decimal("2000.00"),
    )
    opt = PaymentOption(
        payment_option_id="opt_unsafe",
        request_id=req.request_id,
        payment_method="installments",
        payment_amount=Decimal("1050.00"),
        number_of_payments=3,
        first_payment_date=req_date,
        payment_frequency_days=30,
        financing_fee=Decimal("150.00"),
        total_payable_amount=Decimal("3150.00"),
    )
    decision = evaluate_decision(req, profile, ledger, [opt])
    assert decision.affordability_status == "not_affordable"
    assert decision.recommended_payment_method == "not_recommended"


# ---------------------------------------------------------------------------
# Test 4: Wait Selected When Full Payment Becomes Safe Later
# ---------------------------------------------------------------------------

def test_wait_selected_when_full_payment_safe_later():
    """Full payment is unsafe today, but safe later before desired_completion_date.
    User accepts full_payment. The wait method is chosen.
    """
    req_date = date(2026, 5, 1)
    req = _build_test_request(
        request_date=req_date,
        requested_amount=Decimal("2000.00"),
        desired_completion_date=date(2026, 5, 25),
    )
    # Available 2500, min 2000 -> headroom 500 today (unsafe for 2000)
    # Salary of 3000 on 2026-05-15 makes 2000 full payment safe on 2026-05-15
    events = [
        _build_test_event(
            event_id="sal_wait",
            event_date=date(2026, 5, 15),
            settlement_date=date(2026, 5, 15),
            direction="credit",
            amount=Decimal("3000.00"),
            category="salary",
            status="scheduled",
        )
    ]
    profile = _build_test_profile(
        current_available_balance=Decimal("2500.00"),
        minimum_balance_to_keep=Decimal("2000.00"),
        payment_methods_user_will_consider=frozenset({"full_payment"}),
    )
    ledger = _build_test_ledger(
        current_available_balance=Decimal("2500.00"),
        minimum_balance_to_keep=Decimal("2000.00"),
        events=events,
    )

    decision = evaluate_decision(req, profile, ledger, [])
    assert decision.affordability_status == "affordable_later"
    assert decision.recommended_payment_method == "wait"
    assert decision.payment_plan == "2026-05-15:2000"
    assert decision.earliest_date_for_full_payment == date(2026, 5, 15)
    assert decision.spending_changes_needed == "none"


# ---------------------------------------------------------------------------
# Test 5: Spending Changes Restricted to Allowed Flexible Events
# ---------------------------------------------------------------------------

def test_spending_changes_restricted_to_allowed_flexible_events():
    """Verifies:
      - Protected categories are never stopped or reduced.
      - Fixed events are never stopped or reduced.
      - Stop is only generated if category is in willing_to_stop.
      - Reduce is only generated if category is in willing_to_reduce.
      - Never below minimum_allowed_amount.
    """
    profile = _build_test_profile(
        expense_categories_to_protect=frozenset({"rent", "groceries"}),
        expense_categories_user_is_willing_to_stop=frozenset({"streaming"}),
        expense_categories_user_is_willing_to_reduce=frozenset({"entertainment"}),
    )
    events = [
        # Rent: stoppable flexibility but protected -> MUST NOT BE IN CANDIDATES
        _build_test_event(
            event_id="ev_rent",
            event_date=date(2026, 4, 1),
            settlement_date=date(2026, 4, 1),
            direction="debit",
            amount=Decimal("500.00"),
            category="rent",
            flexibility="stoppable",
            is_protected=True,
        ),
        # Streaming: stoppable, willing to stop, not protected, is_recurring -> ELIGIBLE FOR STOP
        _build_test_event(
            event_id="ev_stream",
            event_date=date(2026, 4, 10),
            settlement_date=date(2026, 4, 10),
            direction="debit",
            amount=Decimal("20.00"),
            category="streaming",
            flexibility="stoppable",
            is_recurring=True,
            is_protected=False,
            is_stoppable=True,
        ),
        # Entertainment: reducible, willing to reduce, min 15.00, is_recurring -> ELIGIBLE FOR REDUCE
        _build_test_event(
            event_id="ev_ent",
            event_date=date(2026, 4, 15),
            settlement_date=date(2026, 4, 15),
            direction="debit",
            amount=Decimal("50.00"),
            category="entertainment",
            flexibility="reducible",
            minimum_allowed_amount=Decimal("15.00"),
            is_recurring=True,
            is_protected=False,
            is_reducible=True,
        ),
        # Dining: reducible, but NOT in willing_to_reduce -> NOT ELIGIBLE
        _build_test_event(
            event_id="ev_dining",
            event_date=date(2026, 4, 20),
            settlement_date=date(2026, 4, 20),
            direction="debit",
            amount=Decimal("80.00"),
            category="dining",
            flexibility="reducible",
            minimum_allowed_amount=Decimal("30.00"),
            is_recurring=True,
            is_protected=False,
            is_reducible=True,
        ),
        # Streaming one-time purchase: stoppable, but NOT is_recurring -> NOT ELIGIBLE
        _build_test_event(
            event_id="ev_stream_onetime",
            event_date=date(2026, 4, 25),
            settlement_date=date(2026, 4, 25),
            direction="debit",
            amount=Decimal("45.00"),
            category="streaming",
            flexibility="stoppable",
            is_recurring=False,
            is_protected=False,
            is_stoppable=True,
        ),
    ]
    ledger = _build_test_ledger(events=events)

    changes = find_eligible_spending_changes(profile, ledger)
    change_strs = [c.change_str for c in changes]

    assert "stop:ev_stream" in change_strs
    assert "reduce_to:ev_ent:15" in change_strs
    # Rent must not appear
    assert not any("rent" in s or "ev_rent" in s for s in change_strs)
    # Dining must not appear
    assert not any("dining" in s or "ev_dining" in s for s in change_strs)
    # One-time streaming debit must not appear
    assert not any("ev_stream_onetime" in s for s in change_strs)

    # Verify combinations generator respects max 3 and distinct categories
    combos = generate_spending_change_combinations(changes, max_changes=3)
    assert len(combos) > 0
    for combo in combos:
        cats = [c.category for c in combo]
        assert len(cats) == len(set(cats)), "Cannot have multiple changes on same category"
        assert len(combo) <= 3


# ---------------------------------------------------------------------------
# Test 6: Spending-Change Full Payment Returns affordable_with_plan
# ---------------------------------------------------------------------------

def test_spending_change_full_payment_returns_affordable_with_plan():
    """A full payment made today only because of allowed spending changes
    must be affordable_with_plan, NOT affordable_now.
    """
    req_date = date(2026, 5, 1)
    req = _build_test_request(
        request_date=req_date,
        requested_amount=Decimal("2000.00"),
        desired_completion_date=date(2026, 5, 10),
    )
    # Balance 4000, min 2000 -> headroom 2000.
    # An upcoming recurring debit on 2026-05-05 of 50.00 would drop balance to 1950 (< 2000)
    # Making full payment 2000 unsafe today without spending changes.
    # But stopping this debit on 2026-05-05 keeps balance at 2000 (>= 2000).
    stream_ev = _build_test_event(
        event_id="ev_stream_01",
        event_date=date(2026, 4, 5),
        settlement_date=date(2026, 4, 5),
        direction="debit",
        amount=Decimal("50.00"),
        category="streaming",
        flexibility="stoppable",
        is_recurring=True,
        is_protected=False,
        is_stoppable=True,
    )
    profile = _build_test_profile(
        current_available_balance=Decimal("4000.00"),
        minimum_balance_to_keep=Decimal("2000.00"),
        expense_categories_to_protect=frozenset({"rent"}),
        expense_categories_user_is_willing_to_stop=frozenset({"streaming"}),
        payment_methods_user_will_consider=frozenset({"full_payment"}),
    )
    ledger = _build_test_ledger(
        current_available_balance=Decimal("4000.00"),
        minimum_balance_to_keep=Decimal("2000.00"),
        events=[stream_ev],
    )

    decision = evaluate_decision(req, profile, ledger, [])
    # Full payment today is achieved through spending change
    assert decision.affordability_status == "affordable_with_plan"
    assert decision.recommended_payment_method == "full_payment"
    assert decision.payment_plan == "2026-05-01:2000"
    assert decision.spending_changes_needed == "stop:ev_stream_01"


# ---------------------------------------------------------------------------
# Test 7: Tie-Breaks (Total Cost, First Payment Date, Count, Option ID)
# ---------------------------------------------------------------------------

def test_tie_breaks():
    """Verify exact candidate ranking order:
      1. Completes by desired_completion_date
      2. Requires no spending changes
      3. Lowest total amount paid
      4. Earlier first payment
      5. Fewer payments
      6. Lowest payment_option_id
    """
    req = _build_test_request(
        request_date=date(2026, 5, 1),
        desired_completion_date=date(2026, 7, 1),
    )

    # A: Lower total paid beats higher total paid
    plan_cheap = CandidatePlan(
        recommended_payment_method="installments",
        affordability_status="affordable_with_plan",
        payments=((date(2026, 5, 1), Decimal("500")),),
        payment_plan_str="plan_cheap",
        spending_changes=(),
        spending_changes_str="none",
        completion_date=date(2026, 6, 1),
        total_paid=Decimal("1000.00"),
        first_payment_date=date(2026, 5, 1),
        number_of_payments=2,
        payment_option_id="opt_2",
    )
    plan_expensive = CandidatePlan(
        recommended_payment_method="installments",
        affordability_status="affordable_with_plan",
        payments=((date(2026, 5, 1), Decimal("600")),),
        payment_plan_str="plan_expensive",
        spending_changes=(),
        spending_changes_str="none",
        completion_date=date(2026, 6, 1),
        total_paid=Decimal("1200.00"),
        first_payment_date=date(2026, 5, 1),
        number_of_payments=2,
        payment_option_id="opt_1",
    )
    assert candidate_ranking_key(plan_cheap, req) < candidate_ranking_key(plan_expensive, req)

    # B: Earlier first payment beats later first payment (when total paid is equal)
    plan_early = CandidatePlan(
        recommended_payment_method="partial_payment",
        affordability_status="affordable_with_plan",
        payments=((date(2026, 5, 1), Decimal("500")),),
        payment_plan_str="plan_early",
        spending_changes=(),
        spending_changes_str="none",
        completion_date=date(2026, 6, 1),
        total_paid=Decimal("1000.00"),
        first_payment_date=date(2026, 5, 1),
        number_of_payments=2,
        payment_option_id=None,
    )
    plan_later = CandidatePlan(
        recommended_payment_method="wait",
        affordability_status="affordable_later",
        payments=((date(2026, 5, 15), Decimal("1000")),),
        payment_plan_str="plan_later",
        spending_changes=(),
        spending_changes_str="none",
        completion_date=date(2026, 5, 15),
        total_paid=Decimal("1000.00"),
        first_payment_date=date(2026, 5, 15),
        number_of_payments=1,
        payment_option_id=None,
    )
    assert candidate_ranking_key(plan_early, req) < candidate_ranking_key(plan_later, req)

    # C: Fewer payments beats more payments (when total paid and first payment are equal)
    plan_1_payment = CandidatePlan(
        recommended_payment_method="full_payment",
        affordability_status="affordable_now",
        payments=((date(2026, 5, 1), Decimal("1000")),),
        payment_plan_str="plan_1",
        spending_changes=(),
        spending_changes_str="none",
        completion_date=date(2026, 5, 1),
        total_paid=Decimal("1000.00"),
        first_payment_date=date(2026, 5, 1),
        number_of_payments=1,
        payment_option_id=None,
    )
    assert candidate_ranking_key(plan_1_payment, req) < candidate_ranking_key(plan_early, req)

    # D: Lowest payment_option_id breaks tie when all else is equal
    plan_opt_a = CandidatePlan(
        recommended_payment_method="installments",
        affordability_status="affordable_with_plan",
        payments=((date(2026, 5, 1), Decimal("500")),),
        payment_plan_str="plan_a",
        spending_changes=(),
        spending_changes_str="none",
        completion_date=date(2026, 6, 1),
        total_paid=Decimal("1000.00"),
        first_payment_date=date(2026, 5, 1),
        number_of_payments=2,
        payment_option_id="payment_option_01",
    )
    plan_opt_b = CandidatePlan(
        recommended_payment_method="installments",
        affordability_status="affordable_with_plan",
        payments=((date(2026, 5, 1), Decimal("500")),),
        payment_plan_str="plan_b",
        spending_changes=(),
        spending_changes_str="none",
        completion_date=date(2026, 6, 1),
        total_paid=Decimal("1000.00"),
        first_payment_date=date(2026, 5, 1),
        number_of_payments=2,
        payment_option_id="payment_option_02",
    )
    assert candidate_ranking_key(plan_opt_a, req) < candidate_ranking_key(plan_opt_b, req)


# ---------------------------------------------------------------------------
# Test 8: No Candidate Produces Invalid Status/Method/Plan Combination
# ---------------------------------------------------------------------------

def test_no_candidate_produces_invalid_combinations(
    datastore: DataStore, extracted_data: dict
):
    """Run decision engine across all 26 sample requests and verify:
      - Valid affordability_status in ('affordable_now', 'affordable_with_plan', 'affordable_later', 'not_affordable')
      - Valid recommended_payment_method in ('full_payment', 'partial_payment', 'installments', 'wait', 'not_recommended')
      - affordable_now only with full_payment, spending_changes == 'none', earliest_date == request_date
      - affordable_later only with wait, spending_changes == 'none'
      - not_affordable only with not_recommended, payment_plan == 'none', earliest_date is None
      - amount_safe_to_pay in [0, requested_amount]
    """
    for sample in datastore.sample_requests:
        profile = datastore.get_profile(sample.user_id)
        opts = datastore.get_payment_options_for_request(sample.request_id)
        ledger = reconcile_user_ledger(sample.user_id, datastore, extracted_data)
        dec = evaluate_decision(sample, profile, ledger, opts)

        # Baseline amount_safe_to_pay bounds
        assert Decimal("0.00") <= dec.amount_safe_to_pay <= sample.requested_amount

        # Valid allowed values
        assert dec.affordability_status in (
            "affordable_now", "affordable_with_plan", "affordable_later", "not_affordable"
        )
        assert dec.recommended_payment_method in (
            "full_payment", "partial_payment", "installments", "wait", "not_recommended"
        )

        # Status/method consistency rules
        if dec.affordability_status == "affordable_now":
            assert dec.recommended_payment_method == "full_payment"
            assert dec.spending_changes_needed == "none"
            assert dec.earliest_date_for_full_payment == sample.request_date

        elif dec.affordability_status == "affordable_later":
            assert dec.recommended_payment_method == "wait"
            assert dec.spending_changes_needed == "none"
            assert dec.earliest_date_for_full_payment is not None
            assert dec.earliest_date_for_full_payment > sample.request_date

        elif dec.affordability_status == "affordable_with_plan":
            assert dec.recommended_payment_method in ("full_payment", "partial_payment", "installments")
            if dec.recommended_payment_method == "full_payment":
                assert dec.spending_changes_needed != "none", "full_payment under affordable_with_plan requires spending changes"

        elif dec.affordability_status == "not_affordable":
            assert dec.recommended_payment_method == "not_recommended"
            assert dec.payment_plan == "none"
            # Per rule 3: earliest_date_for_full_payment is preserved if full payment becomes
            # safe anywhere inside the 90-day horizon; None only if unsafe throughout the horizon.
            if dec.earliest_date_for_full_payment is not None:
                assert dec.earliest_date_for_full_payment >= sample.request_date
            assert dec.spending_changes_needed == "none"


# ---------------------------------------------------------------------------
# Test 9: Stream-Scoping Affects Only Flexible Recurring Stream (Regression)
# ---------------------------------------------------------------------------

def test_stream_scoping_affects_only_flexible_recurring_stream():
    """Regression test for Stream Scoping:
      - Build two flexible recurring debits in the same category but from different streams.
      - A same-category fixed debit and a same-category one-time debit.
    Stop one stream and prove:
      - its historical/future linked occurrences change;
      - the other stream’s current and projected occurrences remain unchanged;
      - no one-time or fixed record changes.
    """
    start_balance = Decimal("10000.00")
    min_balance = Decimal("2000.00")
    req_date = date(2026, 5, 1)

    events = [
        # Stream 1: Flexible recurring Video streaming plan ($20 monthly)
        _build_test_event(
            event_id="ev_stream_A_curr",
            event_date=date(2026, 5, 5),
            settlement_date=date(2026, 5, 5),
            direction="debit",
            amount=Decimal("20.00"),
            category="streaming",
            flexibility="stoppable",
            is_recurring=True,
            is_protected=False,
            is_stoppable=True,
            description="Video streaming plan",
        ),
        # Stream 2: Flexible recurring Audio streaming plan ($10 monthly) in same category
        _build_test_event(
            event_id="ev_stream_B_curr",
            event_date=date(2026, 5, 8),
            settlement_date=date(2026, 5, 8),
            direction="debit",
            amount=Decimal("10.00"),
            category="streaming",
            flexibility="stoppable",
            is_recurring=True,
            is_protected=False,
            is_stoppable=True,
            description="Audio streaming plan",
        ),
        # Same-category one-time debit (discretionary equipment)
        _build_test_event(
            event_id="ev_onetime_stream",
            event_date=date(2026, 5, 10),
            settlement_date=date(2026, 5, 10),
            direction="debit",
            amount=Decimal("300.00"),
            category="streaming",
            flexibility="stoppable",
            is_recurring=False,  # ONE-TIME
            is_protected=False,
            is_stoppable=True,
            description="One-time streaming equipment purchase",
        ),
        # Same-category fixed recurring debit (contractual service)
        _build_test_event(
            event_id="ev_fixed_stream",
            event_date=date(2026, 5, 12),
            settlement_date=date(2026, 5, 12),
            direction="debit",
            amount=Decimal("150.00"),
            category="streaming",
            flexibility="fixed",  # FIXED
            is_recurring=True,
            is_protected=False,
            is_stoppable=False,
            description="Fixed contract streaming connection",
        ),
    ]
    ledger = _build_test_ledger(
        current_available_balance=start_balance,
        minimum_balance_to_keep=min_balance,
        events=events,
    )
    profile = _build_test_profile(
        current_available_balance=start_balance,
        minimum_balance_to_keep=min_balance,
        expense_categories_user_is_willing_to_stop=frozenset({"streaming"}),
    )

    # 1. Discovery verifies both distinct streams are found as independent spending change candidates
    changes = find_eligible_spending_changes(profile, ledger)
    change_strs = [c.change_str for c in changes]
    assert "stop:ev_stream_A_curr" in change_strs
    assert "stop:ev_stream_B_curr" in change_strs

    # 2. Simulate stopping only Stream A (Video streaming plan) with recurrence forecasting enabled
    sc_A = StreamSpendingChange(
        action="stop",
        target_event_id="ev_stream_A_curr",
        category="streaming",
        matched_event_ids=frozenset({"ev_stream_A_curr"}),
    )
    sim = simulate_cash_flow(
        ledger=ledger,
        request_date=req_date,
        stream_spending_changes=[sc_A],
        forecast_days=60,
        include_projected_recurring=True,
        include_projected_essentials=False,
    )

    # Day 5 (Stream A current): $20 is STOPPED -> Balance remains 10000.00
    assert sim.daily_balances[date(2026, 5, 5)] == Decimal("10000.00")
    assert sim.daily_balances[date(2026, 5, 6)] == Decimal("10000.00")

    # Day 8 (Stream B current): $10 debit is NOT stopped -> Balance drops by $10 to 9990.00
    assert sim.daily_balances[date(2026, 5, 8)] == Decimal("9990.00")
    assert sim.daily_balances[date(2026, 5, 9)] == Decimal("9990.00")

    # Day 10 (One-time debit): $300 debit MUST NOT be stopped -> Balance drops by $300 to 9690.00
    assert sim.daily_balances[date(2026, 5, 10)] == Decimal("9690.00")

    # Day 12 (Fixed recurring debit): $150 fixed debit MUST NOT be stopped -> Balance drops by $150 to 9540.00
    assert sim.daily_balances[date(2026, 5, 12)] == Decimal("9540.00")

    # June 5 (Stream A projected occurrence): $20 projected debit is STOPPED -> Balance stays 9540.00
    assert sim.daily_balances[date(2026, 6, 5)] == Decimal("9540.00")
    assert sim.daily_balances[date(2026, 6, 6)] == Decimal("9540.00")

    # June 8 (Stream B projected occurrence): $10 projected debit is NOT stopped -> Balance drops to 9530.00
    assert sim.daily_balances[date(2026, 6, 8)] == Decimal("9530.00")


# ---------------------------------------------------------------------------
# Test 10: Partial-Payment Supported with Permitted Spending Changes
# ---------------------------------------------------------------------------

def test_partial_payment_with_spending_changes():
    """Regression test for Requirement 2:
      - Partial payment is unsafe without an allowed recurring spending change.
      - Safe with the spending change.
      - Preserves baseline amount_safe_to_pay.
      - Retains exact two-payment formula summing to requested_amount.
      - Emits affordable_with_plan.
    """
    req_date = date(2026, 5, 1)
    req = _build_test_request(
        request_date=req_date,
        requested_amount=Decimal("2000.00"),
        desired_completion_date=date(2026, 6, 1),
        allows_partial_payment=True,
    )
    # Available balance 3000, min 2000 -> baseline safe today = 1000.00
    # Headroom = 1000. Payment 1 on May 1 is 1000.00, leaving balance at exactly 2000.00.
    # An upcoming recurring flexible streaming debit of 200 on May 10 would drop balance
    # to 1800 (< 2000 min balance) -> Unsafe without spending changes!
    # Salary arrives on May 15 (+2000), making second payment of 1000 safe on May 15.
    events = [
        _build_test_event(
            event_id="ev_stream_sub",
            event_date=date(2026, 5, 10),
            settlement_date=date(2026, 5, 10),
            direction="debit",
            amount=Decimal("200.00"),
            category="streaming",
            flexibility="stoppable",
            is_recurring=True,
            is_protected=False,
            is_stoppable=True,
        ),
        _build_test_event(
            event_id="sal_may15",
            event_date=date(2026, 5, 15),
            settlement_date=date(2026, 5, 15),
            direction="credit",
            amount=Decimal("2000.00"),
            category="salary",
            status="scheduled",
        ),
    ]
    profile = _build_test_profile(
        current_available_balance=Decimal("3000.00"),
        minimum_balance_to_keep=Decimal("2000.00"),
        expense_categories_to_protect=frozenset({"rent"}),
        expense_categories_user_is_willing_to_stop=frozenset({"streaming"}),
        payment_methods_user_will_consider=frozenset({"partial_payment"}),
    )
    ledger = _build_test_ledger(
        current_available_balance=Decimal("3000.00"),
        minimum_balance_to_keep=Decimal("2000.00"),
        events=events,
    )

    # Verify that partial payment without spending changes is UNSAFE:
    # 1000 paid on May 1 leaves balance 2000; May 10 debit drops balance to 1800 (< 2000 min balance)
    baseline_safe = Decimal("1000")
    earliest_date = date(2026, 5, 15)
    part_payments = ((req_date, baseline_safe), (earliest_date, req.requested_amount - baseline_safe))
    safety_no_changes = evaluate_schedule_safety(
        ledger=ledger,
        request_date=req_date,
        payments=part_payments,
        minimum_balance_to_keep=Decimal("2000.00"),
    )
    assert not safety_no_changes.is_safe
    assert safety_no_changes.first_unsafe_date == date(2026, 5, 10)

    # Now evaluate candidate plans:
    # Spending changes are evaluated and stop:ev_stream_sub makes partial payment safe!
    candidates = build_candidate_plans(
        request=req,
        profile=profile,
        ledger=ledger,
        payment_options=[],
        baseline_amount_safe=baseline_safe,
        baseline_earliest_date=earliest_date,
    )

    assert len(candidates) == 1
    cand = candidates[0]
    assert cand.recommended_payment_method == "partial_payment"
    assert cand.affordability_status == "affordable_with_plan"
    assert cand.spending_changes == ("stop:ev_stream_sub",)
    assert cand.is_safe is True
    assert cand.total_paid == req.requested_amount

    # Validate exact two-payment formula and sum
    parts = cand.payment_plan_str.split("|")
    assert len(parts) == 2
    d1, a1 = parts[0].split(":")
    d2, a2 = parts[1].split(":")
    assert d1 == "2026-05-01"
    assert Decimal(a1) == baseline_safe
    assert d2 == "2026-05-15"
    assert Decimal(a2) == req.requested_amount - baseline_safe
    assert Decimal(a1) + Decimal(a2) == req.requested_amount


# ---------------------------------------------------------------------------
# Test 11: Preserved Earliest Full-Payment Date in Fallback
# ---------------------------------------------------------------------------

def test_not_affordable_preserves_earliest_full_payment_date():
    """Regression test for Requirement 3:
      - Full payment is safe after the desired deadline, but before day 90.
      - No candidate completes by desired_completion_date.
      - Result is not_affordable and not_recommended.
      - earliest_date_for_full_payment is PRESERVED (not None).
    """
    req_date = date(2026, 5, 1)
    req = _build_test_request(
        request_date=req_date,
        requested_amount=Decimal("2000.00"),
        desired_completion_date=date(2026, 5, 10),  # Tight deadline: May 10
        allows_partial_payment=False,
    )
    # Available 1000, min 500 -> headroom 500 (unsafe today for 2000).
    # Salary arrives on May 25 (+3000). Full payment becomes safe on May 25.
    # May 25 is within the 90-day horizon, but AFTER desired completion date (May 10).
    events = [
        _build_test_event(
            event_id="sal_late",
            event_date=date(2026, 5, 25),
            settlement_date=date(2026, 5, 25),
            direction="credit",
            amount=Decimal("3000.00"),
            category="salary",
            status="scheduled",
        )
    ]
    profile = _build_test_profile(
        current_available_balance=Decimal("1000.00"),
        minimum_balance_to_keep=Decimal("500.00"),
        payment_methods_user_will_consider=frozenset({"full_payment"}),
    )
    ledger = _build_test_ledger(
        current_available_balance=Decimal("1000.00"),
        minimum_balance_to_keep=Decimal("500.00"),
        events=events,
    )

    decision = evaluate_decision(req, profile, ledger, [])

    assert decision.affordability_status == "not_affordable"
    assert decision.recommended_payment_method == "not_recommended"
    assert decision.payment_plan == "none"
    # Preserved independent earliest full-payment date!
    assert decision.earliest_date_for_full_payment == date(2026, 5, 25)
    assert decision.spending_changes_needed == "none"


# ---------------------------------------------------------------------------
# Test 12: Lowest Cost Outranks Fewer Spending Changes
# ---------------------------------------------------------------------------

def test_ranking_lowest_cost_outranks_fewer_spending_changes():
    """Regression test for Requirement 4:
    Once both candidates require spending changes, lowest total cost outranks
    the number of spending changes.
    """
    req = _build_test_request(
        request_date=date(2026, 5, 1),
        desired_completion_date=date(2026, 7, 1),
    )

    # Candidate A: 2 spending changes, but lower cost (1000.00)
    plan_a_cheap_2_changes = CandidatePlan(
        recommended_payment_method="installments",
        affordability_status="affordable_with_plan",
        payments=((date(2026, 5, 1), Decimal("500")),),
        payment_plan_str="plan_a",
        spending_changes=("stop:ev_1", "stop:ev_2"),
        spending_changes_str="stop:ev_1|stop:ev_2",
        completion_date=date(2026, 6, 1),
        total_paid=Decimal("1000.00"),
        first_payment_date=date(2026, 5, 1),
        number_of_payments=2,
        payment_option_id="opt_1",
    )

    # Candidate B: 1 spending change, but higher cost (1100.00)
    plan_b_expensive_1_change = CandidatePlan(
        recommended_payment_method="installments",
        affordability_status="affordable_with_plan",
        payments=((date(2026, 5, 1), Decimal("550")),),
        payment_plan_str="plan_b",
        spending_changes=("stop:ev_3",),
        spending_changes_str="stop:ev_3",
        completion_date=date(2026, 6, 1),
        total_paid=Decimal("1100.00"),
        first_payment_date=date(2026, 5, 1),
        number_of_payments=2,
        payment_option_id="opt_2",
    )

    key_a = candidate_ranking_key(plan_a_cheap_2_changes, req)
    key_b = candidate_ranking_key(plan_b_expensive_1_change, req)

    # Candidate A MUST rank ahead of Candidate B (< in sort order)
    assert key_a < key_b

