"""
Deterministic 90-Day Cash-Flow Simulation and Affordability Search (Phase 4).

Consumes:
  1. ReconciledLedger (from Phase 3 reconciliation)
  2. FinancialProfile (or ledger.minimum_balance_to_keep / current_available_balance)
  3. Request (or request_date, requested_amount)

Provides:
  - simulate_cash_flow: Generates daily balance timelines over [request_date, request_date + 90 days]
  - evaluate_schedule_safety: Tests whether a proposed payment schedule maintains minimum balance
  - compute_amount_safe_to_pay: Binary search for largest safe one-time payment on request_date
  - find_earliest_date_for_full_payment: Searches for earliest date when full payment is safe

Strict monetary precision:
  - All balance and amount calculations use `Decimal`. No floating-point arithmetic.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_FLOOR
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .models import FinancialProfile, Request
from .reconciliation import ReconciledEvent, ReconciledLedger


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SimulationTimeline:
    """Deterministic daily cash-flow timeline over the forecast window.

    Attributes:
        start_date: Starting date of simulation (request_date).
        end_date: Ending date of simulation (request_date + forecast_days).
        daily_balances: Mapping of date to end-of-day balance.
        minimum_balance: Lowest projected balance across the entire window.
        minimum_balance_date: First date on which the lowest balance occurs.
    """
    start_date: date
    end_date: date
    daily_balances: Dict[date, Decimal]
    minimum_balance: Decimal
    minimum_balance_date: date


@dataclass(frozen=True)
class SafetyResult:
    """Result of evaluating the safety of a proposed payment schedule.

    Attributes:
        is_safe: True if balance never drops below minimum_balance_to_keep on any day,
                 and all proposed payments are within the forecast window.
        minimum_balance: Lowest projected balance reached during the simulation.
        minimum_balance_date: First date on which that lowest balance occurs.
        first_unsafe_date: First date on which balance < minimum_balance_to_keep,
                           or the first invalid payment date outside the window, or None if safe.
        unsafe_reason: Optional diagnostic explanation if unsafe, else None.
    """
    is_safe: bool
    minimum_balance: Decimal
    minimum_balance_date: date
    first_unsafe_date: Optional[date] = None
    unsafe_reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def clean_decimal(val: Decimal) -> Decimal:
    """Format Decimal cleanly: preserve up to 2 decimal places, omit trailing zeros if whole."""
    if val == val.to_integral():
        return val.to_integral()
    if val == val.quantize(Decimal("0.1")):
        return val.quantize(Decimal("0.1"))
    return val.quantize(Decimal("0.01"))


# ---------------------------------------------------------------------------
# 1. Daily 90-Day Cash-Flow Simulation
# ---------------------------------------------------------------------------

def simulate_cash_flow(
    ledger: ReconciledLedger,
    request_date: date,
    payments: Optional[Sequence[Tuple[date, Decimal]]] = None,
    extra_events: Optional[Sequence[ReconciledEvent]] = None,
    excluded_event_ids: Optional[Set[str]] = None,
    forecast_days: int = 90,
) -> SimulationTimeline:
    """Run a deterministic daily cash-flow simulation over [request_date, request_date + forecast_days].

    Rules:
      - Starts at `current_available_balance` on `request_date`.
      - Applies each reconciled event on its `settlement_date` (or `event_date` if settlement_date is None).
      - Credits increase balance; debits decrease balance.
      - Pending debits remain reserved and reduce projected balance on their settlement date.
      - Excluded events (from Phase 3) are already filtered out of ReconciledLedger.
      - Proposed extra payments are deducted on their respective payment dates.

    Args:
        ledger: ReconciledLedger containing current balance and canonical cash events.
        request_date: Start date of the 90-day simulation window.
        payments: Optional sequence of (payment_date, amount) deductions.
        extra_events: Optional additional reconciled events to incorporate.
        excluded_event_ids: Optional set of event_ids to exclude (e.g. stopped expenses).
        forecast_days: Length of simulation window in days (default: 90).

    Returns:
        SimulationTimeline with daily balances and minimum projected balance.
    """
    start_date = request_date
    end_date = request_date + timedelta(days=forecast_days)

    # Aggregate daily ledger cash flows
    daily_net_flow: Dict[date, Decimal] = defaultdict(lambda: Decimal("0.00"))

    events_to_process = list(ledger.all_events)
    if extra_events:
        events_to_process.extend(extra_events)

    for event in events_to_process:
        if excluded_event_ids and event.event_id in excluded_event_ids:
            continue

        eff_date = event.settlement_date if event.settlement_date is not None else event.event_date
        # Only events within the forecast window apply during simulation
        if start_date <= eff_date <= end_date:
            if event.direction == "credit":
                daily_net_flow[eff_date] += event.normalized_amount
            elif event.direction == "debit":
                daily_net_flow[eff_date] -= event.normalized_amount

    # Aggregate proposed payments
    if payments:
        for p_date, p_amount in payments:
            if start_date <= p_date <= end_date:
                daily_net_flow[p_date] -= p_amount

    # Simulate daily balances
    daily_balances: Dict[date, Decimal] = {}
    current_balance = ledger.current_available_balance
    minimum_balance = current_balance
    minimum_balance_date = start_date

    total_days = forecast_days + 1  # [request_date, request_date + 90 days] inclusive = 91 days
    for day_idx in range(total_days):
        day_date = start_date + timedelta(days=day_idx)
        if day_date in daily_net_flow:
            current_balance += daily_net_flow[day_date]

        daily_balances[day_date] = current_balance

        if current_balance < minimum_balance:
            minimum_balance = current_balance
            minimum_balance_date = day_date

    return SimulationTimeline(
        start_date=start_date,
        end_date=end_date,
        daily_balances=daily_balances,
        minimum_balance=minimum_balance,
        minimum_balance_date=minimum_balance_date,
    )


# ---------------------------------------------------------------------------
# 2. Safety Predicate
# ---------------------------------------------------------------------------

def evaluate_schedule_safety(
    ledger: ReconciledLedger,
    request_date: date,
    payments: Sequence[Tuple[date, Decimal]],
    minimum_balance_to_keep: Optional[Decimal] = None,
    extra_events: Optional[Sequence[ReconciledEvent]] = None,
    excluded_event_ids: Optional[Set[str]] = None,
    forecast_days: int = 90,
) -> SafetyResult:
    """Determine whether a proposed payment schedule is safe.

    Safety criteria:
      1. Every payment date must be within [request_date, request_date + forecast_days].
      2. Balance must never fall below `minimum_balance_to_keep` on any simulated day.

    Args:
        ledger: ReconciledLedger containing user's financial state.
        request_date: Evaluation request date.
        payments: Sequence of (payment_date, amount) tuples.
        minimum_balance_to_keep: Balance threshold to maintain (defaults to ledger's).
        extra_events: Optional additional events to incorporate.
        excluded_event_ids: Optional event_ids to exclude.
        forecast_days: Horizon in days (default: 90).

    Returns:
        SafetyResult with is_safe, minimum_balance, and first_unsafe_date if unsafe.
    """
    start_date = request_date
    end_date = request_date + timedelta(days=forecast_days)
    target_min_balance = (
        minimum_balance_to_keep
        if minimum_balance_to_keep is not None
        else ledger.minimum_balance_to_keep
    )

    # Check payment date boundaries
    for p_date, p_amount in payments:
        if p_date < start_date or p_date > end_date:
            return SafetyResult(
                is_safe=False,
                minimum_balance=Decimal("0.00"),
                minimum_balance_date=start_date,
                first_unsafe_date=p_date,
                unsafe_reason=(
                    f"Payment date {p_date.isoformat()} is outside the forecast window "
                    f"[{start_date.isoformat()}, {end_date.isoformat()}]."
                ),
            )

    # Run daily simulation
    timeline = simulate_cash_flow(
        ledger=ledger,
        request_date=request_date,
        payments=payments,
        extra_events=extra_events,
        excluded_event_ids=excluded_event_ids,
        forecast_days=forecast_days,
    )

    # Check for violations in chronological order
    total_days = forecast_days + 1
    for day_idx in range(total_days):
        day_date = start_date + timedelta(days=day_idx)
        bal = timeline.daily_balances[day_date]
        if bal < target_min_balance:
            return SafetyResult(
                is_safe=False,
                minimum_balance=timeline.minimum_balance,
                minimum_balance_date=timeline.minimum_balance_date,
                first_unsafe_date=day_date,
                unsafe_reason=(
                    f"Projected balance {clean_decimal(bal)} on {day_date.isoformat()} "
                    f"drops below minimum balance to keep {clean_decimal(target_min_balance)}."
                ),
            )

    return SafetyResult(
        is_safe=True,
        minimum_balance=timeline.minimum_balance,
        minimum_balance_date=timeline.minimum_balance_date,
        first_unsafe_date=None,
        unsafe_reason=None,
    )


# ---------------------------------------------------------------------------
# 3. amount_safe_to_pay (Deterministic Binary Search)
# ---------------------------------------------------------------------------

def compute_amount_safe_to_pay(
    ledger: ReconciledLedger,
    request_date: date,
    requested_amount: Decimal,
    minimum_balance_to_keep: Optional[Decimal] = None,
    step: Decimal = Decimal("0.01"),
    forecast_days: int = 90,
) -> Decimal:
    """Compute the largest safe one-time payment on request_date using binary search.

    Guarantees:
      - Result is in [0, requested_amount].
      - Calculated before optional spending changes and independently of preferences.
      - Uses exact monetary binary search in discrete `step` units (cents).
      - Zero floating-point arithmetic.

    Args:
        ledger: ReconciledLedger containing user's financial state.
        request_date: Date of the one-time payment evaluation.
        requested_amount: Full purchase/payment amount requested.
        minimum_balance_to_keep: Balance threshold (defaults to ledger's).
        step: Smallest currency unit for search granularity (default: Decimal("0.01")).
        forecast_days: Horizon in days (default: 90).

    Returns:
        The largest safe payment amount as a Decimal.
    """
    if requested_amount <= Decimal("0.00"):
        return Decimal("0.00")

    target_min = (
        minimum_balance_to_keep
        if minimum_balance_to_keep is not None
        else ledger.minimum_balance_to_keep
    )

    # Baseline simulation without proposed payment
    baseline = simulate_cash_flow(
        ledger=ledger,
        request_date=request_date,
        payments=None,
        forecast_days=forecast_days,
    )

    max_headroom = baseline.minimum_balance - target_min
    if max_headroom <= Decimal("0.00"):
        return Decimal("0.00")

    upper_bound = min(requested_amount, max_headroom)
    max_steps = int((upper_bound / step).to_integral_value(rounding=ROUND_FLOOR))

    low = 0
    high = max_steps
    best_step = 0

    while low <= high:
        mid = (low + high) // 2
        mid_amount = Decimal(mid) * step
        safety = evaluate_schedule_safety(
            ledger=ledger,
            request_date=request_date,
            payments=[(request_date, mid_amount)],
            minimum_balance_to_keep=target_min,
            forecast_days=forecast_days,
        )
        if safety.is_safe:
            best_step = mid
            low = mid + 1
        else:
            high = mid - 1

    best_amount = Decimal(best_step) * step
    safe_amount = max(Decimal("0.00"), min(requested_amount, best_amount))
    return clean_decimal(safe_amount)


# ---------------------------------------------------------------------------
# 4. earliest_date_for_full_payment
# ---------------------------------------------------------------------------

def find_earliest_date_for_full_payment(
    ledger: ReconciledLedger,
    request_date: date,
    requested_amount: Decimal,
    minimum_balance_to_keep: Optional[Decimal] = None,
    extra_events: Optional[Sequence[ReconciledEvent]] = None,
    excluded_event_ids: Optional[Set[str]] = None,
    forecast_days: int = 90,
) -> Optional[date]:
    """Find the earliest date in [request_date, request_date + forecast_days]
    on which one full payment of requested_amount is safe.

    Rules:
      - Returns request_date if safe immediately.
      - Returns the first conservative projected date if safe later.
      - Returns None if no single full payment is safe within the 90-day horizon.
      - Independent of whether the user accepts full payment (preferences applied in Phase 5).

    Args:
        ledger: ReconciledLedger containing user's financial state.
        request_date: Start of forecast horizon.
        requested_amount: Full purchase amount to test.
        minimum_balance_to_keep: Balance threshold (defaults to ledger's).
        extra_events: Optional additional events.
        excluded_event_ids: Optional event_ids to exclude.
        forecast_days: Horizon in days (default: 90).

    Returns:
        Earliest safe payment date, or None.
    """
    total_days = forecast_days + 1
    for day_idx in range(total_days):
        candidate_date = request_date + timedelta(days=day_idx)
        res = evaluate_schedule_safety(
            ledger=ledger,
            request_date=request_date,
            payments=[(candidate_date, requested_amount)],
            minimum_balance_to_keep=minimum_balance_to_keep,
            extra_events=extra_events,
            excluded_event_ids=excluded_event_ids,
            forecast_days=forecast_days,
        )
        if res.is_safe:
            return candidate_date

    return None
