"""
Unit tests for Phase 4: Deterministic 90-Day Cash-Flow Simulation and Affordability Search.

Validates:
  1. An immediate payment that is safe and stays above the minimum.
  2. A payment that is unsafe due to a future pending debit or essential expense.
  3. A later confirmed/scheduled salary that makes a full payment safe on a later date.
  4. Binary-search boundary checks: returned safe amount passes; one smallest currency unit more fails.
  5. A request with no safe full-payment date within 90 days.
  6. A test proving earliest_date_for_full_payment equals request_date without payment-method choices.
  7. Payment schedule validation (installments, invalid future/past dates).
  8. Integration checks on real dataset sample requests (request_01, request_09, request_12).

Phase 4 Recurrence Forecasting Tests:
  9. Historical monthly rent with no future scheduled row is charged again during the forecast.
  10. Projected rent changes an otherwise-safe payment into an unsafe one.
  11. A scheduled recurring occurrence already present in the ledger is not duplicated.
  12. Monthly cadence works through a month-end boundary (31st day landing on February's final day).
  13. A recurring salary already synthesized by Phase 3 is not duplicated by Phase 4.

Phase 4 Conservative Variable Essentials Tests:
  14. Protected groceries with only historical records create future forecast debits.
  15. Those grocery debits turn an otherwise-safe payment into unsafe.
  16. Frequent but unprotected/optional dining is not forecast.
  17. A known pending or scheduled essential debit is not duplicated.
"""

from __future__ import annotations

import json
import os
import pytest
from datetime import date, timedelta
from decimal import Decimal

from lib.loaders import DataStore
from lib.models import FinancialEvent, FinancialProfile, Request
from lib.reconciliation import (
    ReconciledEvent,
    ReconciledLedger,
    reconcile_user_ledger,
)
from lib.simulation import (
    SafetyResult,
    SimulationTimeline,
    clean_decimal,
    compute_amount_safe_to_pay,
    evaluate_schedule_safety,
    find_earliest_date_for_full_payment,
    generate_future_recurring_occurrences,
    generate_future_variable_essential_occurrences,
    get_conservative_recurring_amount,
    get_recurring_cadence_day,
    simulate_cash_flow,
)


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
    """Helper to construct a controlled ReconciledLedger for unit testing."""
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
    is_recurring: bool = False,
    description: str | None = None,
) -> ReconciledEvent:
    """Helper to construct a ReconciledEvent."""
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
        flexibility="fixed",
        minimum_allowed_amount=None,
        is_recurring=is_recurring,
        is_protected=True,
        is_reducible=False,
        is_stoppable=False,
    )


# ---------------------------------------------------------------------------
# Test Scenario 1: Immediate payment that is safe and stays above minimum
# ---------------------------------------------------------------------------

def test_scenario_1_immediate_payment_safe():
    """An immediate payment that is safe and stays above minimum across 90 days."""
    req_date = date(2026, 5, 1)
    req_amount = Decimal("5000.00")
    ledger = _build_test_ledger(
        current_available_balance=Decimal("10000.00"),
        minimum_balance_to_keep=Decimal("2000.00"),
    )

    # 1. Timeline simulation with proposed payment
    timeline = simulate_cash_flow(
        ledger=ledger,
        request_date=req_date,
        payments=[(req_date, req_amount)],
    )
    assert len(timeline.daily_balances) == 91
    assert timeline.start_date == req_date
    assert timeline.end_date == req_date + timedelta(days=90)
    assert timeline.daily_balances[req_date] == Decimal("5000.00")
    assert timeline.daily_balances[req_date + timedelta(days=90)] == Decimal("5000.00")
    assert timeline.minimum_balance == Decimal("5000.00")

    # 2. Safety predicate
    safety = evaluate_schedule_safety(
        ledger=ledger,
        request_date=req_date,
        payments=[(req_date, req_amount)],
    )
    assert safety.is_safe is True
    assert safety.minimum_balance == Decimal("5000.00")
    assert safety.first_unsafe_date is None

    # 3. amount_safe_to_pay
    safe_pay = compute_amount_safe_to_pay(
        ledger=ledger,
        request_date=req_date,
        requested_amount=req_amount,
    )
    assert safe_pay == Decimal("5000.00")

    # 4. earliest_date_for_full_payment
    earliest = find_earliest_date_for_full_payment(
        ledger=ledger,
        request_date=req_date,
        requested_amount=req_amount,
    )
    assert earliest == req_date


# ---------------------------------------------------------------------------
# Test Scenario 2: Unsafe due to future pending debit or essential expense
# ---------------------------------------------------------------------------

def test_scenario_2_payment_unsafe_due_to_future_debit():
    """A payment that looks safe on request_date but fails due to a future pending debit."""
    req_date = date(2026, 5, 1)
    pending_debit_date = date(2026, 5, 5)
    pending_event = _build_test_event(
        event_id="pending_01",
        event_date=date(2026, 4, 28),
        settlement_date=pending_debit_date,
        direction="debit",
        amount=Decimal("2000.00"),
        status="pending",
        is_recurring=False,
    )
    ledger = _build_test_ledger(
        current_available_balance=Decimal("10000.00"),
        minimum_balance_to_keep=Decimal("2000.00"),
        events=[pending_event],
    )

    # 1. Full 7,000 payment is unsafe on 2026-05-05
    safety = evaluate_schedule_safety(
        ledger=ledger,
        request_date=req_date,
        payments=[(req_date, Decimal("7000.00"))],
    )
    assert safety.is_safe is False
    assert safety.minimum_balance == Decimal("1000.00")
    assert safety.first_unsafe_date == pending_debit_date

    # 2. compute_amount_safe_to_pay returns exactly 6,000.00
    safe_pay = compute_amount_safe_to_pay(
        ledger=ledger,
        request_date=req_date,
        requested_amount=Decimal("7000.00"),
    )
    assert safe_pay == Decimal("6000.00")

    # 3. Boundary check: 6,000.00 passes, 6,000.01 fails
    safe_test = evaluate_schedule_safety(
        ledger=ledger,
        request_date=req_date,
        payments=[(req_date, Decimal("6000.00"))],
    )
    assert safe_test.is_safe is True
    assert safe_test.minimum_balance == Decimal("2000.00")

    unsafe_test = evaluate_schedule_safety(
        ledger=ledger,
        request_date=req_date,
        payments=[(req_date, Decimal("6000.01"))],
    )
    assert unsafe_test.is_safe is False
    assert unsafe_test.minimum_balance == Decimal("1999.99")
    assert unsafe_test.first_unsafe_date == pending_debit_date


# ---------------------------------------------------------------------------
# Test Scenario 3: Later confirmed/scheduled salary makes payment safe later
# ---------------------------------------------------------------------------

def test_scenario_3_later_salary_makes_full_payment_safe():
    """Payment unsafe immediately, but becomes safe after a scheduled salary credit."""
    req_date = date(2026, 5, 1)
    salary_date = date(2026, 5, 25)
    salary_event = _build_test_event(
        event_id="sched_sal_01",
        event_date=salary_date,
        settlement_date=salary_date,
        direction="credit",
        amount=Decimal("5000.00"),
        category="salary",
        status="scheduled",
        is_recurring=True,
    )
    ledger = _build_test_ledger(
        current_available_balance=Decimal("3000.00"),
        minimum_balance_to_keep=Decimal("1500.00"),
        events=[salary_event],
    )

    # 1. Immediate payment is unsafe
    imm_safety = evaluate_schedule_safety(
        ledger=ledger,
        request_date=req_date,
        payments=[(req_date, Decimal("4000.00"))],
    )
    assert imm_safety.is_safe is False
    assert imm_safety.first_unsafe_date == req_date

    # 2. Safe to pay today is 1,500.00
    safe_pay = compute_amount_safe_to_pay(
        ledger=ledger,
        request_date=req_date,
        requested_amount=Decimal("4000.00"),
    )
    assert safe_pay == Decimal("1500.00")

    # 3. Earliest full payment date is salary_date (2026-05-25)
    earliest = find_earliest_date_for_full_payment(
        ledger=ledger,
        request_date=req_date,
        requested_amount=Decimal("4000.00"),
    )
    assert earliest == salary_date

    # 4. Verify payment on day before salary is unsafe
    day_before = salary_date - timedelta(days=1)
    day_before_safety = evaluate_schedule_safety(
        ledger=ledger,
        request_date=req_date,
        payments=[(day_before, Decimal("4000.00"))],
    )
    assert day_before_safety.is_safe is False
    assert day_before_safety.first_unsafe_date == day_before

    # 5. Verify payment on salary date is safe
    salary_day_safety = evaluate_schedule_safety(
        ledger=ledger,
        request_date=req_date,
        payments=[(salary_date, Decimal("4000.00"))],
    )
    assert salary_day_safety.is_safe is True
    assert salary_day_safety.minimum_balance == Decimal("3000.00")


# ---------------------------------------------------------------------------
# Test Scenario 4: Exact Binary-Search Boundary Checks
# ---------------------------------------------------------------------------

def test_scenario_4_binary_search_boundary_checks():
    """Verify binary search returns exact safe cent, and safe_amount + 0.01 fails."""
    req_date = date(2026, 5, 1)
    debit_date = req_date + timedelta(days=10)
    debit_event = _build_test_event(
        event_id="debit_01",
        event_date=debit_date,
        settlement_date=debit_date,
        direction="debit",
        amount=Decimal("1234.50"),
        category="utilities",
        status="scheduled",
        is_recurring=False,
    )
    ledger = _build_test_ledger(
        current_available_balance=Decimal("5432.10"),
        minimum_balance_to_keep=Decimal("1000.00"),
        events=[debit_event],
    )

    safe_pay = compute_amount_safe_to_pay(
        ledger=ledger,
        request_date=req_date,
        requested_amount=Decimal("4000.00"),
        step=Decimal("0.01"),
    )
    assert safe_pay == Decimal("3197.60")

    # Boundary check 1: safe_pay passes
    res_pass = evaluate_schedule_safety(
        ledger=ledger,
        request_date=req_date,
        payments=[(req_date, safe_pay)],
    )
    assert res_pass.is_safe is True
    assert res_pass.minimum_balance == Decimal("1000.00")

    # Boundary check 2: safe_pay + 0.01 fails
    res_fail = evaluate_schedule_safety(
        ledger=ledger,
        request_date=req_date,
        payments=[(req_date, safe_pay + Decimal("0.01"))],
    )
    assert res_fail.is_safe is False
    assert res_fail.minimum_balance == Decimal("999.99")
    assert res_fail.first_unsafe_date == debit_date


# ---------------------------------------------------------------------------
# Test Scenario 5: No safe full-payment date within 90 days
# ---------------------------------------------------------------------------

def test_scenario_5_no_safe_date_within_90_days():
    """When a full payment is impossible throughout the entire 90-day horizon."""
    req_date = date(2026, 5, 1)
    debit_event = _build_test_event(
        event_id="debit_02",
        event_date=req_date + timedelta(days=10),
        settlement_date=req_date + timedelta(days=10),
        direction="debit",
        amount=Decimal("500.00"),
        category="insurance",
        status="scheduled",
        is_recurring=False,
    )
    ledger = _build_test_ledger(
        current_available_balance=Decimal("2000.00"),
        minimum_balance_to_keep=Decimal("1000.00"),
        events=[debit_event],
    )

    earliest = find_earliest_date_for_full_payment(
        ledger=ledger,
        request_date=req_date,
        requested_amount=Decimal("10000.00"),
    )
    assert earliest is None

    safe_pay = compute_amount_safe_to_pay(
        ledger=ledger,
        request_date=req_date,
        requested_amount=Decimal("10000.00"),
    )
    assert safe_pay == Decimal("500.00")


# ---------------------------------------------------------------------------
# Test Scenario 6: earliest_date_for_full_payment equals request_date without preferences
# ---------------------------------------------------------------------------

def test_scenario_6_earliest_date_equals_request_date_independent_of_preferences():
    """earliest_date_for_full_payment evaluates purely financial safety independently
    of whether the user profile accepts full payment or installments."""
    req_date = date(2026, 6, 1)
    ledger = _build_test_ledger(
        current_available_balance=Decimal("50000.00"),
        minimum_balance_to_keep=Decimal("5000.00"),
    )

    earliest = find_earliest_date_for_full_payment(
        ledger=ledger,
        request_date=req_date,
        requested_amount=Decimal("10000.00"),
    )
    assert earliest == req_date


# ---------------------------------------------------------------------------
# Test Scenario 7: Multi-Payment Schedule and Out-of-Window Dates
# ---------------------------------------------------------------------------

def test_scenario_7_installment_schedule_and_boundary_dates():
    """Verify safety evaluation across multi-payment schedules and out-of-window dates."""
    req_date = date(2026, 5, 1)
    ledger = _build_test_ledger(
        current_available_balance=Decimal("6000.00"),
        minimum_balance_to_keep=Decimal("1000.00"),
    )

    schedule = [
        (req_date, Decimal("1500.00")),
        (req_date + timedelta(days=30), Decimal("1500.00")),
        (req_date + timedelta(days=60), Decimal("1500.00")),
    ]
    res = evaluate_schedule_safety(
        ledger=ledger,
        request_date=req_date,
        payments=schedule,
    )
    assert res.is_safe is True
    assert res.minimum_balance == Decimal("1500.00")

    # Payment date before request_date
    past_schedule = [(req_date - timedelta(days=1), Decimal("1000.00"))]
    past_res = evaluate_schedule_safety(
        ledger=ledger,
        request_date=req_date,
        payments=past_schedule,
    )
    assert past_res.is_safe is False
    assert past_res.first_unsafe_date == req_date - timedelta(days=1)

    # Payment date after 90 days
    future_schedule = [(req_date + timedelta(days=91), Decimal("1000.00"))]
    future_res = evaluate_schedule_safety(
        ledger=ledger,
        request_date=req_date,
        payments=future_schedule,
    )
    assert future_res.is_safe is False
    assert future_res.first_unsafe_date == req_date + timedelta(days=91)


# ---------------------------------------------------------------------------
# Test Scenario 8: Integration on Real Dataset Sample Requests
# ---------------------------------------------------------------------------

def test_scenario_8_real_dataset_sample_requests(datastore: DataStore, extracted_data: dict):
    """Verify simulation and affordability search on real dataset requests."""
    ledger_01 = reconcile_user_ledger("user_01", datastore, extracted_data)
    req_01 = next(r for r in datastore.sample_requests if r.request_id == "request_01")
    safe_pay_01 = compute_amount_safe_to_pay(ledger_01, req_01.request_date, req_01.requested_amount)
    earliest_01 = find_earliest_date_for_full_payment(ledger_01, req_01.request_date, req_01.requested_amount)

    assert safe_pay_01 == Decimal("25256")
    assert earliest_01 == date(2024, 3, 3)

    ledger_09 = reconcile_user_ledger("user_09", datastore, extracted_data)
    req_09 = next(r for r in datastore.sample_requests if r.request_id == "request_09")
    safe_pay_09 = compute_amount_safe_to_pay(ledger_09, req_09.request_date, req_09.requested_amount)
    earliest_09 = find_earliest_date_for_full_payment(ledger_09, req_09.request_date, req_09.requested_amount)

    assert safe_pay_09 == Decimal("166.61")
    assert earliest_09 == date(2026, 7, 4)

    ledger_12 = reconcile_user_ledger("user_12", datastore, extracted_data)
    req_12 = next(r for r in datastore.sample_requests if r.request_id == "request_12")
    safe_pay_12 = compute_amount_safe_to_pay(ledger_12, req_12.request_date, req_12.requested_amount)
    earliest_12 = find_earliest_date_for_full_payment(ledger_12, req_12.request_date, req_12.requested_amount)

    assert safe_pay_12 == Decimal("65164")
    assert earliest_12 == date(2026, 4, 5)


# ===========================================================================
# Phase 4 Recurrence Forecasting Tests
# ===========================================================================

def test_historical_monthly_rent_charged_again_during_forecast():
    """Historical monthly rent with no future scheduled row is charged again during the forecast."""
    req_date = date(2026, 5, 15)
    rent_events = [
        _build_test_event(
            event_id=f"rent_{m}",
            event_date=date(2026, m, 1),
            settlement_date=date(2026, m, 1),
            direction="debit",
            amount=Decimal("3000.00"),
            category="rent",
            is_recurring=True,
            description="Monthly rent",
        )
        for m in (3, 4, 5)
    ]
    ledger = _build_test_ledger(
        current_available_balance=Decimal("15000.00"),
        minimum_balance_to_keep=Decimal("2000.00"),
        events=rent_events,
    )

    projected = generate_future_recurring_occurrences(ledger, req_date, forecast_days=90)
    assert len(projected) == 3
    proj_dates = [e.settlement_date for e in projected]
    assert proj_dates == [date(2026, 6, 1), date(2026, 7, 1), date(2026, 8, 1)]
    assert all(e.normalized_amount == Decimal("3000.00") for e in projected)
    assert all(e.category == "rent" for e in projected)

    # Simulate timeline with projected rent
    timeline = simulate_cash_flow(ledger, req_date)
    assert timeline.daily_balances[date(2026, 5, 15)] == Decimal("15000.00")
    assert timeline.daily_balances[date(2026, 5, 31)] == Decimal("15000.00")
    assert timeline.daily_balances[date(2026, 6, 1)] == Decimal("12000.00")
    assert timeline.daily_balances[date(2026, 6, 30)] == Decimal("12000.00")
    assert timeline.daily_balances[date(2026, 7, 1)] == Decimal("9000.00")
    assert timeline.daily_balances[date(2026, 7, 31)] == Decimal("9000.00")
    assert timeline.daily_balances[date(2026, 8, 1)] == Decimal("6000.00")
    assert timeline.daily_balances[date(2026, 8, 13)] == Decimal("6000.00")
    assert timeline.minimum_balance == Decimal("6000.00")


def test_projected_rent_changes_safe_payment_to_unsafe():
    """Projected rent changes an otherwise-safe payment into an unsafe one."""
    req_date = date(2026, 5, 15)
    rent_events = [
        _build_test_event(
            event_id=f"rent_{m}",
            event_date=date(2026, m, 1),
            settlement_date=date(2026, m, 1),
            direction="debit",
            amount=Decimal("3000.00"),
            category="rent",
            is_recurring=True,
        )
        for m in (3, 4, 5)
    ]
    ledger = _build_test_ledger(
        current_available_balance=Decimal("15000.00"),
        minimum_balance_to_keep=Decimal("2000.00"),
        events=rent_events,
    )

    # 1. Before recurrence projection (include_projected_recurring=False): falsely reported safe
    safety_without_proj = evaluate_schedule_safety(
        ledger=ledger,
        request_date=req_date,
        payments=[(req_date, Decimal("10000.00"))],
        include_projected_recurring=False,
    )
    assert safety_without_proj.is_safe is True
    assert safety_without_proj.minimum_balance == Decimal("5000.00")

    # 2. After recurrence projection (include_projected_recurring=True): correctly identified as UNSAFE
    safety_with_proj = evaluate_schedule_safety(
        ledger=ledger,
        request_date=req_date,
        payments=[(req_date, Decimal("10000.00"))],
        include_projected_recurring=True,
    )
    assert safety_with_proj.is_safe is False
    assert safety_with_proj.minimum_balance == Decimal("-4000.00")
    assert safety_with_proj.first_unsafe_date == date(2026, 7, 1)

    # 3. amount_safe_to_pay correctly reflects the conservative headroom (6,000 - 2,000 = 4,000)
    safe_pay = compute_amount_safe_to_pay(
        ledger=ledger,
        request_date=req_date,
        requested_amount=Decimal("10000.00"),
        include_projected_recurring=True,
    )
    assert safe_pay == Decimal("4000.00")


def test_scheduled_recurring_occurrence_not_duplicated():
    """A scheduled recurring occurrence already present in the ledger is not duplicated."""
    req_date = date(2026, 5, 15)
    hist_utilities = [
        _build_test_event(
            event_id=f"util_{m}",
            event_date=date(2026, m, 5),
            settlement_date=date(2026, m, 5),
            direction="debit",
            amount=Decimal("1000.00"),
            category="utilities",
            status="settled",
            is_recurring=True,
        )
        for m in (3, 4, 5)
    ]
    scheduled_june_util = _build_test_event(
        event_id="sched_util_june",
        event_date=date(2026, 6, 5),
        settlement_date=date(2026, 6, 5),
        direction="debit",
        amount=Decimal("1200.00"),
        category="utilities",
        status="scheduled",
        is_recurring=True,
        description="Scheduled electricity bill",
    )
    all_events = hist_utilities + [scheduled_june_util]
    ledger = _build_test_ledger(
        current_available_balance=Decimal("10000.00"),
        minimum_balance_to_keep=Decimal("1000.00"),
        events=all_events,
    )

    projected = generate_future_recurring_occurrences(ledger, req_date, forecast_days=90)
    assert len(projected) == 2
    proj_dates = [e.settlement_date for e in projected]
    assert proj_dates == [date(2026, 7, 5), date(2026, 8, 5)]

    timeline = simulate_cash_flow(ledger, req_date)
    assert timeline.daily_balances[date(2026, 6, 4)] == Decimal("10000.00")
    assert timeline.daily_balances[date(2026, 6, 5)] == Decimal("8800.00")
    assert timeline.daily_balances[date(2026, 7, 5)] == Decimal("7600.00")
    assert timeline.daily_balances[date(2026, 8, 5)] == Decimal("6400.00")


def test_monthly_cadence_month_end_boundary_february():
    """Monthly cadence works through a month-end boundary (31st day landing on February's final day)."""
    req_date = date(2026, 1, 15)
    sub_events = [
        _build_test_event(
            event_id="sub_oct",
            event_date=date(2025, 10, 31),
            settlement_date=date(2025, 10, 31),
            direction="debit",
            amount=Decimal("150.00"),
            category="streaming",
            is_recurring=True,
        ),
        _build_test_event(
            event_id="sub_nov",
            event_date=date(2025, 11, 30),
            settlement_date=date(2025, 11, 30),
            direction="debit",
            amount=Decimal("150.00"),
            category="streaming",
            is_recurring=True,
        ),
        _build_test_event(
            event_id="sub_dec",
            event_date=date(2025, 12, 31),
            settlement_date=date(2025, 12, 31),
            direction="debit",
            amount=Decimal("150.00"),
            category="streaming",
            is_recurring=True,
        ),
    ]
    ledger = _build_test_ledger(
        current_available_balance=Decimal("5000.00"),
        minimum_balance_to_keep=Decimal("500.00"),
        events=sub_events,
    )

    cadence_day = get_recurring_cadence_day(sub_events)
    assert cadence_day == 31

    projected = generate_future_recurring_occurrences(ledger, req_date, forecast_days=90)
    proj_dates = [e.settlement_date for e in projected]
    assert proj_dates == [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31)]


def test_recurring_salary_synthesized_by_phase3_not_duplicated():
    """A recurring salary already synthesized by Phase 3 is not duplicated by Phase 4."""
    req_date = date(2026, 6, 1)
    salary_events = [
        _build_test_event(
            event_id=f"sched_sal_user_2026-0{m}-15",
            event_date=date(2026, m, 15),
            settlement_date=date(2026, m, 15),
            direction="credit",
            amount=Decimal("3000.00"),
            category="salary",
            status="scheduled",
            is_recurring=True,
            description="Confirmed scheduled salary",
        )
        for m in (6, 7, 8)
    ]
    ledger = _build_test_ledger(
        current_available_balance=Decimal("5000.00"),
        minimum_balance_to_keep=Decimal("1000.00"),
        events=salary_events,
    )

    projected = generate_future_recurring_occurrences(ledger, req_date, forecast_days=90)
    assert len(projected) == 0

    timeline = simulate_cash_flow(ledger, req_date)
    assert timeline.daily_balances[date(2026, 6, 14)] == Decimal("5000.00")
    assert timeline.daily_balances[date(2026, 6, 15)] == Decimal("8000.00")
    assert timeline.daily_balances[date(2026, 7, 15)] == Decimal("11000.00")
    assert timeline.daily_balances[date(2026, 8, 15)] == Decimal("14000.00")
    assert timeline.daily_balances[req_date + timedelta(days=90)] == Decimal("14000.00")


# ===========================================================================
# Phase 4 Conservative Variable Essentials Tests (Required by Section 3)
# ===========================================================================

def test_protected_groceries_create_future_forecast_debits():
    """Protected groceries with only historical records create future forecast debits.

    Setup:
      - Start balance: 10,000.00
      - Minimum balance to keep: 2,000.00
      - Request date: 2026-05-15
      - Protected categories: {'groceries', 'rent'}
      - History of settled weekly groceries (Fridays):
        2026-04-24: 500.00, 2026-05-01: 500.00, 2026-05-08: 500.00
      - No future scheduled grocery debits in ledger.
    Expectation:
      - Cadence = 7 days.
      - First future grocery debit falls on 2026-05-15 (500.00).
      - Subsequent debits on 2026-05-22, 2026-05-29, etc.
      - Daily balance drops by 500.00 on each of those Fridays.
    """
    req_date = date(2026, 5, 15)
    groc_events = [
        _build_test_event(
            event_id="groc_01",
            event_date=date(2026, 4, 24),
            settlement_date=date(2026, 4, 24),
            direction="debit",
            amount=Decimal("500.00"),
            category="groceries",
            status="settled",
            is_recurring=False,
            description="Weekly grocery shop",
        ),
        _build_test_event(
            event_id="groc_02",
            event_date=date(2026, 5, 1),
            settlement_date=date(2026, 5, 1),
            direction="debit",
            amount=Decimal("500.00"),
            category="groceries",
            status="settled",
            is_recurring=False,
            description="Weekly grocery shop",
        ),
        _build_test_event(
            event_id="groc_03",
            event_date=date(2026, 5, 8),
            settlement_date=date(2026, 5, 8),
            direction="debit",
            amount=Decimal("500.00"),
            category="groceries",
            status="settled",
            is_recurring=False,
            description="Weekly grocery shop",
        ),
    ]
    ledger = _build_test_ledger(
        current_available_balance=Decimal("10000.00"),
        minimum_balance_to_keep=Decimal("2000.00"),
        events=groc_events,
        protected_categories=frozenset({"groceries", "rent"}),
    )

    proj_essentials = generate_future_variable_essential_occurrences(ledger, req_date, forecast_days=90)
    assert len(proj_essentials) >= 12
    assert proj_essentials[0].settlement_date == date(2026, 5, 15)
    assert proj_essentials[1].settlement_date == date(2026, 5, 22)
    assert proj_essentials[2].settlement_date == date(2026, 5, 29)
    assert all(e.normalized_amount == Decimal("500.00") for e in proj_essentials)
    assert all(e.category == "groceries" for e in proj_essentials)

    timeline = simulate_cash_flow(ledger, req_date)
    # Day 0 (2026-05-15): 10,000 - 500 = 9,500.00
    assert timeline.daily_balances[date(2026, 5, 15)] == Decimal("9500.00")
    # Day 6 (2026-05-21): 9,500.00
    assert timeline.daily_balances[date(2026, 5, 21)] == Decimal("9500.00")
    # Day 7 (2026-05-22): 9,500 - 500 = 9,000.00
    assert timeline.daily_balances[date(2026, 5, 22)] == Decimal("9000.00")
    # Day 14 (2026-05-29): 9,000 - 500 = 8,500.00
    assert timeline.daily_balances[date(2026, 5, 29)] == Decimal("8500.00")


def test_grocery_debits_turn_otherwise_safe_payment_unsafe():
    """Those grocery debits turn an otherwise-safe payment into unsafe.

    Setup:
      - Start balance: 10,000.00
      - Minimum to keep: 2,000.00
      - Request date: 2026-05-15
      - Proposed payment: 7,500.00 on 2026-05-15
      - Groceries: 500.00 weekly on Fridays (settled 2026-04-24, 2026-05-01, 2026-05-08)
    Comparison:
      - Without essential variable forecasting:
        Balance after payment = 10,000 - 7,500 = 2,500.00 >= 2,000.00 (falsely reported SAFE)
      - With essential variable forecasting:
        2026-05-15: Balance = 10,000 - 7,500 - 500 = 2,000.00
        2026-05-22: Balance = 2,000 - 500 = 1,500.00 (< 2,000.00) -> UNSAFE!
      - amount_safe_to_pay drops to 1,500.00 (min balance across 13 weeks = 3,500 - 2,000 = 1,500)
    """
    req_date = date(2026, 5, 15)
    groc_events = [
        _build_test_event(
            event_id=f"groc_{m}",
            event_date=d,
            settlement_date=d,
            direction="debit",
            amount=Decimal("500.00"),
            category="groceries",
            status="settled",
            is_recurring=False,
        )
        for m, d in enumerate([date(2026, 4, 24), date(2026, 5, 1), date(2026, 5, 8)])
    ]
    ledger = _build_test_ledger(
        current_available_balance=Decimal("10000.00"),
        minimum_balance_to_keep=Decimal("2000.00"),
        events=groc_events,
        protected_categories=frozenset({"groceries"}),
    )

    # 1. Without variable essentials forecasting: falsely reported SAFE
    res_without = evaluate_schedule_safety(
        ledger=ledger,
        request_date=req_date,
        payments=[(req_date, Decimal("7500.00"))],
        include_projected_essentials=False,
    )
    assert res_without.is_safe is True
    assert res_without.minimum_balance == Decimal("2500.00")

    # 2. With variable essentials forecasting: correctly identified as UNSAFE on 2026-05-22
    res_with = evaluate_schedule_safety(
        ledger=ledger,
        request_date=req_date,
        payments=[(req_date, Decimal("7500.00"))],
        include_projected_essentials=True,
    )
    assert res_with.is_safe is False
    assert res_with.first_unsafe_date == date(2026, 5, 22)
    assert res_with.minimum_balance < Decimal("2000.00")

    # 3. amount_safe_to_pay drops from 7,500.00 to 1,500.00
    safe_pay = compute_amount_safe_to_pay(
        ledger=ledger,
        request_date=req_date,
        requested_amount=Decimal("7500.00"),
        include_projected_essentials=True,
    )
    # Total grocery spend across 90 days: 13 x 500 = 6,500.00
    # Minimum balance = 10,000 - 6,500 = 3,500.00
    # Headroom = 3,500 - 2,000 = 1,500.00
    assert safe_pay == Decimal("1500.00")


def test_frequent_unprotected_dining_not_forecast():
    """Frequent but unprotected/optional dining is not forecast.

    Setup:
      - User has frequent settled dining events every 7 days (2026-04-24, 2026-05-01, 2026-05-08).
      - User profile's expense_categories_to_protect does NOT include dining (only {'rent', 'groceries'}).
    Expectation:
      - generate_future_variable_essential_occurrences outputs ZERO dining occurrences.
      - Normal simulation does not deduct future dining spend.
    """
    req_date = date(2026, 5, 15)
    dining_events = [
        _build_test_event(
            event_id=f"dining_{m}",
            event_date=d,
            settlement_date=d,
            direction="debit",
            amount=Decimal("300.00"),
            category="dining",
            status="settled",
            is_recurring=False,
        )
        for m, d in enumerate([date(2026, 4, 24), date(2026, 5, 1), date(2026, 5, 8)])
    ]
    ledger = _build_test_ledger(
        current_available_balance=Decimal("10000.00"),
        minimum_balance_to_keep=Decimal("2000.00"),
        events=dining_events,
        protected_categories=frozenset({"rent", "groceries"}),
    )

    proj_essentials = generate_future_variable_essential_occurrences(ledger, req_date, forecast_days=90)
    assert len(proj_essentials) == 0
    assert not any(e.category == "dining" for e in proj_essentials)

    timeline = simulate_cash_flow(ledger, req_date)
    # Daily balances remain 10,000.00 since dining is optional and not protected
    assert timeline.minimum_balance == Decimal("10000.00")


def test_pending_or_scheduled_essential_debit_not_duplicated():
    """A known pending or scheduled essential debit is not duplicated.

    Setup:
      - Request date: 2026-05-15 (Friday)
      - Historical weekly groceries on Fridays (2026-04-24, 2026-05-01, 2026-05-08) for 500.00
      - Next expected occurrence is Friday 2026-05-15.
      - In ledger.all_events, a pending grocery debit ALREADY exists settling on 2026-05-15 for 550.00.
    Expectation:
      - The pending debit on 2026-05-15 is preserved.
      - Phase 4 MUST NOT create a duplicate grocery debit on 2026-05-15.
      - The first projected grocery debit starts on 2026-05-22.
      - On 2026-05-15, the balance is debited by exactly 550.00 once (not 550 + 500 = 1050).
    """
    req_date = date(2026, 5, 15)
    hist_groceries = [
        _build_test_event(
            event_id=f"groc_hist_{m}",
            event_date=d,
            settlement_date=d,
            direction="debit",
            amount=Decimal("500.00"),
            category="groceries",
            status="settled",
            is_recurring=False,
        )
        for m, d in enumerate([date(2026, 4, 24), date(2026, 5, 1), date(2026, 5, 8)])
    ]
    pending_grocery = _build_test_event(
        event_id="pending_groc_01",
        event_date=date(2026, 5, 10),
        settlement_date=date(2026, 5, 15),
        direction="debit",
        amount=Decimal("550.00"),
        category="groceries",
        status="pending",
        is_recurring=False,
        description="Pending supermarket debit",
    )
    all_events = hist_groceries + [pending_grocery]
    ledger = _build_test_ledger(
        current_available_balance=Decimal("10000.00"),
        minimum_balance_to_keep=Decimal("2000.00"),
        events=all_events,
        protected_categories=frozenset({"groceries"}),
    )

    proj_essentials = generate_future_variable_essential_occurrences(ledger, req_date, forecast_days=90)
    # The occurrence on 2026-05-15 is skipped because of the pending debit
    assert proj_essentials[0].settlement_date == date(2026, 5, 22)
    assert not any(e.settlement_date == date(2026, 5, 15) for e in proj_essentials)

    timeline = simulate_cash_flow(ledger, req_date)
    # On 2026-05-15: only the 550.00 pending debit applies -> 10,000 - 550 = 9,450.00
    assert timeline.daily_balances[date(2026, 5, 15)] == Decimal("9450.00")
    # On 2026-05-22: first projected grocery debit of 500.00 -> 9,450 - 500 = 8,950.00
    assert timeline.daily_balances[date(2026, 5, 22)] == Decimal("8950.00")
