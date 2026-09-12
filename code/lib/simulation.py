"""
Deterministic 90-Day Cash-Flow Simulation and Affordability Search (Phase 4).

Consumes:
  1. ReconciledLedger (from Phase 3 reconciliation)
  2. FinancialProfile (or ledger.minimum_balance_to_keep / current_available_balance / protected_categories)
  3. Request (or request_date, requested_amount)

Provides:
  - generate_future_recurring_occurrences: Generates future recurring debits across [request_date, request_date + 90 days]
  - generate_future_variable_essential_occurrences: Generates future conservative essential variable debits (e.g. groceries, transport)
  - simulate_cash_flow: Generates daily balance timelines over [request_date, request_date + 90 days]
  - evaluate_schedule_safety: Tests whether a proposed payment schedule maintains minimum balance
  - compute_amount_safe_to_pay: Binary search for largest safe one-time payment on request_date
  - find_earliest_date_for_full_payment: Searches for earliest date when full payment is safe

Strict monetary precision:
  - All balance and amount calculations use `Decimal`. No floating-point arithmetic.
"""

from __future__ import annotations

import calendar
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_FLOOR
from typing import AbstractSet, Dict, List, Literal, Optional, Sequence, Set, Tuple

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


@dataclass(frozen=True)
class StreamSpendingChange:
    """A targeted spending change applied to a specific flexible recurring stream.

    Attributes:
        action: 'stop' (completely exclude stream) or 'reduce_to' (cap stream amount).
        target_event_id: The representative event_id of the flexible recurring stream.
        category: The category of the recurring stream.
        new_amount: The reduced amount if action == 'reduce_to', else None.
        matched_event_ids: Optional frozenset of event_ids in this stream lineage.
    """
    action: Literal["stop", "reduce_to"]
    target_event_id: str
    category: str
    new_amount: Optional[Decimal] = None
    matched_event_ids: Optional[frozenset[str]] = None


def get_recurring_stream_identifier(
    event: ReconciledEvent,
    category_events: Optional[Sequence[ReconciledEvent]] = None,
) -> str:
    """Return an unambiguous recurring-stream identity string.

    Distinguishes distinct recurring streams sharing a category based on category,
    direction, and normalized description or lineage link.
    """
    cat = (event.category or "").strip().lower()
    direction = (event.direction or "").strip().lower()
    desc = (event.description or "").strip().lower()

    if event.linked_event_id:
        return f"{cat}::{direction}::link::{event.linked_event_id}"

    # For discretionary/variable categories where receipts reflect merchant/store names
    # (e.g. dining, groceries, transport, entertainment, shopping), all transactions
    # in the category represent the user's overall recurring spending cadence in that category.
    if cat in ("dining", "shopping", "entertainment", "groceries", "transport"):
        return f"{cat}::{direction}"

    generic_descs = {"", cat, f"test {cat}", f"projected recurring {cat}"}

    if category_events:
        specific_descs = {
            (e.description or "").strip().lower()
            for e in category_events
            if (e.description or "").strip().lower() not in generic_descs
        }
        if len(specific_descs) <= 1:
            canonical_desc = next(iter(specific_descs)) if specific_descs else "default"
            return f"{cat}::{direction}::{canonical_desc}"

    if desc and desc not in generic_descs:
        return f"{cat}::{direction}::{desc}"
    if desc:
        return f"{cat}::{direction}::default"
    return f"{cat}::{direction}::id::{event.event_id}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def clean_decimal(val: Decimal) -> Decimal:
    """Format Decimal cleanly: preserve whole integer if integral, else 2 decimal places."""
    if val == val.to_integral():
        return val.to_integral()
    return val.quantize(Decimal("0.01"))


# ---------------------------------------------------------------------------
# Recurrence Forecasting & Cadence Helpers
# ---------------------------------------------------------------------------

def get_recurring_cadence_day(events: Sequence[ReconciledEvent]) -> int:
    """Determine the cadence day of the month for a recurring stream.

    If the events land on month-end days (28, 29, 30, 31) across different months,
    returns the maximum day (e.g. 31), so that month-end logic will cap at the
    last day of each subsequent month (e.g. Feb 28, Apr 30, May 31).
    Otherwise returns the most frequent day of month.
    """
    if not events:
        return 1

    regular = [
        e for e in events
        if not any(k in e.description.lower() for k in (
            "arrear", "outstanding", "retry", "catch-up", "adjustment", "one-time", "balance", "prorated"
        ))
    ]
    target_events = regular if regular else list(events)
    days = [e.event_date.day for e in target_events]

    # Check if month-end pattern
    if max(days) in (28, 29, 30, 31) and min(days) in (28, 29, 30, 31):
        return max(days)

    return Counter(days).most_common(1)[0][0]


def get_conservative_recurring_amount(events: Sequence[ReconciledEvent]) -> Decimal:
    """Determine the conservative amount for a recurring stream from event history.

    Rule:
      1. Filter for regular occurrences (excluding one-off arrears, retries, adjustments, etc.).
      2. Take the most recent supported occurrences (up to the last 6 cycles).
      3. Return the maximum normalized amount among those occurrences.
    """
    if not events:
        return Decimal("0.00")

    regular = [
        e for e in events
        if not any(k in e.description.lower() for k in (
            "arrear", "outstanding", "retry", "catch-up", "adjustment", "one-time", "balance", "prorated"
        ))
    ]
    target_events = regular if regular else list(events)
    recent = sorted(target_events, key=lambda x: x.event_date)[-6:]
    return max(e.normalized_amount for e in recent)


def get_conservative_essential_amount(events: Sequence[ReconciledEvent]) -> Decimal:
    """Return a cautious recurring essential amount without repeating an outlier.

    Variable essentials such as groceries legitimately fluctuate, so the estimate
    keeps the largest amount from recent history.  A lone extreme purchase,
    however (for example a bulk stock-up or an image-backed catch-up bill), is
    evidence of one exceptional transaction rather than a weekly commitment.
    Repeating it throughout the 90-day forecast can make a safe recommendation
    falsely look impossible.

    An amount is excluded only when there are at least five recent observations
    and it is more than double *both* the next-largest value and the median.
    This leaves ordinary high weeks in place while filtering only a clearly
    isolated spike.
    """
    if not events:
        return Decimal("0.00")

    recent_amounts = sorted(e.normalized_amount for e in events[-8:])
    if len(recent_amounts) < 5:
        return recent_amounts[-1]

    maximum = recent_amounts[-1]
    second_largest = recent_amounts[-2]
    middle = len(recent_amounts) // 2
    if len(recent_amounts) % 2:
        median = recent_amounts[middle]
    else:
        median = (recent_amounts[middle - 1] + recent_amounts[middle]) / Decimal("2")

    if maximum > second_largest * Decimal("2") and maximum > median * Decimal("2"):
        return second_largest
    return maximum


def generate_future_recurring_occurrences(
    ledger: ReconciledLedger,
    request_date: date,
    forecast_days: int = 90,
) -> List[ReconciledEvent]:
    """Generate future recurring cash events across [request_date, request_date + forecast_days].

    Consumes `ledger.recurring_events` and projects recurring debits (rent, utilities,
    subscriptions, debt payments, healthcare, education, insurance, etc.) that do not
    already have scheduled occurrences in `ledger.all_events`.

    De-duplication Key:
      (category, direction, cadence_year, cadence_month)
      Preserves any occurrence already present in `ledger.all_events` on or after request_date
      that falls on candidate_date or within the same monthly cadence cycle (+/- 7 days).

    Salary streams (direction == 'credit' and category == 'salary') are managed by Phase 3's
    reconciliation synthesis and are never duplicated here.
    """
    start_date = request_date
    end_date = request_date + timedelta(days=forecast_days)

    cat_dir_events: Dict[Tuple[str, str], List[ReconciledEvent]] = defaultdict(list)
    for e in ledger.recurring_events:
        cat_dir_events[(e.category, e.direction)].append(e)

    streams: Dict[str, List[ReconciledEvent]] = defaultdict(list)
    for (cat, direction), ev_list in cat_dir_events.items():
        for e in ev_list:
            s_id = get_recurring_stream_identifier(e, ev_list)
            streams[s_id].append(e)

    projected_events: List[ReconciledEvent] = []

    for stream_id, stream_events in sorted(streams.items()):
        # Phase 3 reconciliation already manages recurring salary synthesis
        if stream_events[0].category == "salary" and stream_events[0].direction == "credit":
            continue

        cat = stream_events[0].category
        direction = stream_events[0].direction
        cadence_day = get_recurring_cadence_day(stream_events)
        conservative_amt = get_conservative_recurring_amount(stream_events)

        # Inherit metadata from the anchor/latest event in the stream
        latest_event = max(stream_events, key=lambda x: (x.settlement_date or x.event_date, x.event_id))

        # Generate occurrences through forecast window
        cur_year = start_date.year
        cur_month = start_date.month

        while (cur_year, cur_month) <= (end_date.year, end_date.month):
            max_day = calendar.monthrange(cur_year, cur_month)[1]
            cand_day = min(cadence_day, max_day)
            cand_date = date(cur_year, cur_month, cand_day)

            if start_date <= cand_date <= end_date:
                # De-duplication check:
                # Does ledger.all_events already contain a scheduled/settled occurrence
                # for this specific recurring stream in this cadence cycle on or after request_date?
                already_exists = any(
                    e.category == cat
                    and e.direction == direction
                    and (
                        e.linked_event_id == latest_event.event_id
                        or get_recurring_stream_identifier(e, ledger.all_events) == stream_id
                        or stream_id.endswith("::default")
                    )
                    and (e.settlement_date or e.event_date) >= start_date
                    and (
                        (e.settlement_date or e.event_date) == cand_date
                        or (
                            (e.settlement_date or e.event_date).year == cand_date.year
                            and (e.settlement_date or e.event_date).month == cand_date.month
                            and abs(((e.settlement_date or e.event_date) - cand_date).days) <= 7
                        )
                    )
                    for e in ledger.all_events
                )

                if not already_exists:
                    proj_ev = ReconciledEvent(
                        event_id=f"proj_{latest_event.event_id}_{cand_date.isoformat()}",
                        user_id=ledger.user_id,
                        event_type=latest_event.event_type,
                        description=latest_event.description or f"Projected recurring {cat}",
                        category=cat,
                        direction=direction,
                        original_amount=conservative_amt,
                        original_currency=ledger.home_currency,
                        normalized_amount=conservative_amt,
                        home_currency=ledger.home_currency,
                        event_date=cand_date,
                        settlement_date=cand_date,
                        status="scheduled",
                        flexibility=latest_event.flexibility,
                        minimum_allowed_amount=latest_event.minimum_allowed_amount,
                        is_recurring=True,
                        is_protected=latest_event.is_protected,
                        is_reducible=latest_event.is_reducible,
                        is_stoppable=latest_event.is_stoppable,
                        linked_event_id=latest_event.event_id,
                    )
                    projected_events.append(proj_ev)

            # Advance to next month
            if cur_month == 12:
                cur_year += 1
                cur_month = 1
            else:
                cur_month += 1

    return projected_events


# ---------------------------------------------------------------------------
# Essential Variable Spending Forecasting
# ---------------------------------------------------------------------------

def generate_future_variable_essential_occurrences(
    ledger: ReconciledLedger,
    request_date: date,
    protected_categories: Optional[AbstractSet[str]] = None,
    forecast_days: int = 90,
) -> List[ReconciledEvent]:
    """Generate future conservative variable essential debits across [request_date, request_date + forecast_days].

    Identifies forecastable variable essentials:
      - Considers historical debit events whose category is in `protected_categories`
        (or `ledger.protected_categories`).
      - Only includes essentials not already managed as fixed monthly recurring commitments
        (e.g. groceries, transport).
      - Ignores optional/unprotected categories (e.g. dining, entertainment), even if frequent.
      - Requires at least 3 historical settled occurrences to support a genuine cadence.

    Cadence and Amount Rules:
      1. Cadence (interval in days) is the statistical mode of positive intervals between
         consecutive settled historical dates.
      2. Conservative amount is the maximum normalized amount among the most recent settled
         occurrences (up to the last 8).
      3. Occurrence timeline begins at `last_settled_date + cadence_days`, advanced to `>= request_date`.
      4. De-duplication: Skips any candidate date where `ledger.all_events` already contains a
         pending or scheduled debit for that category within `cadence_days // 2` days.

    Args:
        ledger: ReconciledLedger containing user's financial state.
        request_date: Start of forecast horizon.
        protected_categories: Optional set of categories to protect (defaults to ledger.protected_categories).
        forecast_days: Horizon length in days (default: 90).

    Returns:
        List of newly projected ReconciledEvent instances for variable essentials.
    """
    start_date = request_date
    end_date = request_date + timedelta(days=forecast_days)

    target_protected: AbstractSet[str] = (
        protected_categories
        if protected_categories is not None
        else (ledger.protected_categories or frozenset())
    )
    if not target_protected:
        return []

    # Exclude categories already managed as fixed recurring commitments
    recurring_cats = set(e.category for e in ledger.recurring_events)

    # Group settled historical debits on or before request_date
    historical_debits_by_cat: Dict[str, List[ReconciledEvent]] = defaultdict(list)
    for e in ledger.all_events:
        eff_d = e.settlement_date if e.settlement_date is not None else e.event_date
        if (
            e.direction == "debit"
            and e.status == "settled"
            and eff_d <= start_date
            and e.category in target_protected
            and e.category not in recurring_cats
        ):
            historical_debits_by_cat[e.category].append(e)

    projected_events: List[ReconciledEvent] = []

    for cat, events_in_cat in historical_debits_by_cat.items():
        # Require at least 3 historical settled events to support a cadence
        if len(events_in_cat) < 3:
            continue

        cat_events = sorted(events_in_cat, key=lambda x: x.event_date)
        dates = sorted(set(e.event_date for e in cat_events))
        if len(dates) < 3:
            continue

        diffs = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1) if (dates[i + 1] - dates[i]).days > 0]
        if not diffs:
            continue

        cadence_days = Counter(diffs).most_common(1)[0][0]
        if cadence_days <= 0:
            cadence_days = 7

        # Conservative amount: peak normal amount among recent settled occurrences.
        # A single exceptional essential purchase must not become a recurring debit.
        recent_events = cat_events[-8:]
        conservative_amt = get_conservative_essential_amount(recent_events)
        latest_event = cat_events[-1]

        # Anchor date: advance from last settled date in steps of cadence_days
        cand_date = dates[-1] + timedelta(days=cadence_days)
        while cand_date < start_date:
            cand_date += timedelta(days=cadence_days)

        dedup_window_days = max(1, cadence_days // 2)

        while cand_date <= end_date:
            # De-duplication: check if ledger already has a pending or scheduled debit in window
            already_exists = any(
                e.category == cat
                and e.direction == "debit"
                and (e.settlement_date or e.event_date) >= start_date
                and abs(((e.settlement_date or e.event_date) - cand_date).days) <= dedup_window_days
                for e in ledger.all_events
            )

            if not already_exists:
                proj_ev = ReconciledEvent(
                    event_id=f"proj_ess_{cat}_{cand_date.isoformat()}",
                    user_id=ledger.user_id,
                    event_type=latest_event.event_type,
                    description=f"Projected essential {cat}",
                    category=cat,
                    direction="debit",
                    original_amount=conservative_amt,
                    original_currency=ledger.home_currency,
                    normalized_amount=conservative_amt,
                    home_currency=ledger.home_currency,
                    event_date=cand_date,
                    settlement_date=cand_date,
                    status="scheduled",
                    flexibility="fixed",
                    minimum_allowed_amount=None,
                    is_recurring=False,
                    is_protected=True,
                    is_reducible=False,
                    is_stoppable=False,
                )
                projected_events.append(proj_ev)

            cand_date += timedelta(days=cadence_days)

    return projected_events


# ---------------------------------------------------------------------------
# 1. Daily 90-Day Cash-Flow Simulation
# ---------------------------------------------------------------------------

def simulate_cash_flow(
    ledger: ReconciledLedger,
    request_date: date,
    payments: Optional[Sequence[Tuple[date, Decimal]]] = None,
    extra_events: Optional[Sequence[ReconciledEvent]] = None,
    excluded_event_ids: Optional[Set[str]] = None,
    protected_categories: Optional[AbstractSet[str]] = None,
    forecast_days: int = 90,
    include_projected_recurring: bool = True,
    include_projected_essentials: bool = True,
    stopped_categories: Optional[AbstractSet[str]] = None,
    reduced_categories: Optional[Dict[str, Decimal]] = None,
    stream_spending_changes: Optional[Sequence[StreamSpendingChange]] = None,
) -> SimulationTimeline:
    """Run a deterministic daily cash-flow simulation over [request_date, request_date + forecast_days].

    Rules:
      - Starts at `current_available_balance` on `request_date`.
      - Incorporates conservative projected occurrences for recurring commitments
        (rent, utilities, subscriptions, debt repayments, etc.).
      - Incorporates conservative projected variable essentials (groceries, transport)
        when listed in protected_categories.
      - Applies each event on its `settlement_date` (or `event_date` if settlement_date is None).
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
        protected_categories: Optional set of categories to protect (defaults to ledger.protected_categories).
        forecast_days: Length of simulation window in days (default: 90).
        include_projected_recurring: If True, forecasts future recurring commitments.
        include_projected_essentials: If True, forecasts future protected variable essentials.
        stopped_categories: Optional set of debit categories to exclude.
        reduced_categories: Optional dict mapping debit category to capped/reduced amount.
        stream_spending_changes: Optional sequence of StreamSpendingChange targeting specific flexible recurring streams.

    Returns:
        SimulationTimeline with daily balances and minimum projected balance.
    """
    start_date = request_date
    end_date = request_date + timedelta(days=forecast_days)

    events_to_process = list(ledger.all_events)
    if include_projected_recurring:
        projected = generate_future_recurring_occurrences(
            ledger=ledger,
            request_date=request_date,
            forecast_days=forecast_days,
        )
        events_to_process.extend(projected)

    if include_projected_essentials:
        proj_essentials = generate_future_variable_essential_occurrences(
            ledger=ledger,
            request_date=request_date,
            protected_categories=protected_categories,
            forecast_days=forecast_days,
        )
        events_to_process.extend(proj_essentials)

    if extra_events:
        events_to_process.extend(extra_events)

    # Aggregate daily ledger cash flows
    daily_net_flow: Dict[date, Decimal] = defaultdict(lambda: Decimal("0.00"))

    for event in events_to_process:
        if excluded_event_ids and event.event_id in excluded_event_ids:
            continue

        is_stopped = False
        reduced_cap: Optional[Decimal] = None

        # Targeted stream spending changes: applies strictly to flexible recurring debits matching anchor lineage
        if stream_spending_changes and event.direction == "debit":
            if event.is_recurring and event.flexibility in ("reducible", "stoppable", "reducible_or_stoppable"):
                for sc in stream_spending_changes:
                    if (
                        event.event_id == sc.target_event_id
                        or event.linked_event_id == sc.target_event_id
                        or (sc.matched_event_ids is not None and event.event_id in sc.matched_event_ids)
                    ):
                        if sc.action == "stop":
                            is_stopped = True
                            break
                        elif sc.action == "reduce_to" and sc.new_amount is not None:
                            if reduced_cap is None or sc.new_amount < reduced_cap:
                                reduced_cap = sc.new_amount

        if is_stopped:
            continue

        if stopped_categories and event.direction == "debit" and event.category in stopped_categories:
            if event.is_recurring and event.flexibility in ("reducible", "stoppable", "reducible_or_stoppable"):
                continue

        eff_date = event.settlement_date if event.settlement_date is not None else event.event_date
        # Only events within the forecast window apply during simulation
        if start_date <= eff_date <= end_date:
            if event.direction == "credit":
                daily_net_flow[eff_date] += event.normalized_amount
            elif event.direction == "debit":
                amt = event.normalized_amount
                if reduced_cap is not None:
                    amt = min(amt, reduced_cap)
                elif reduced_categories and event.category in reduced_categories:
                    if event.is_recurring and event.flexibility in ("reducible", "stoppable", "reducible_or_stoppable"):
                        amt = min(amt, reduced_categories[event.category])
                daily_net_flow[eff_date] -= amt

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
    protected_categories: Optional[AbstractSet[str]] = None,
    forecast_days: int = 90,
    include_projected_recurring: bool = True,
    include_projected_essentials: bool = True,
    stopped_categories: Optional[AbstractSet[str]] = None,
    reduced_categories: Optional[Dict[str, Decimal]] = None,
    stream_spending_changes: Optional[Sequence[StreamSpendingChange]] = None,
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
        protected_categories: Optional set of categories to protect.
        forecast_days: Horizon in days (default: 90).
        include_projected_recurring: If True, forecasts future recurring commitments.
        include_projected_essentials: If True, forecasts future protected variable essentials.
        stopped_categories: Optional set of debit categories to completely exclude.
        reduced_categories: Optional dict mapping debit category to capped/reduced amount.
        stream_spending_changes: Optional sequence of StreamSpendingChange targeting specific flexible recurring streams.

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
        protected_categories=protected_categories,
        forecast_days=forecast_days,
        include_projected_recurring=include_projected_recurring,
        include_projected_essentials=include_projected_essentials,
        stopped_categories=stopped_categories,
        reduced_categories=reduced_categories,
        stream_spending_changes=stream_spending_changes,
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
    protected_categories: Optional[AbstractSet[str]] = None,
    step: Decimal = Decimal("0.01"),
    forecast_days: int = 90,
    include_projected_recurring: bool = True,
    include_projected_essentials: bool = True,
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
        protected_categories: Optional set of categories to protect.
        step: Smallest currency unit for search granularity (default: Decimal("0.01")).
        forecast_days: Horizon in days (default: 90).
        include_projected_recurring: If True, forecasts future recurring commitments.
        include_projected_essentials: If True, forecasts future protected variable essentials.

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
        protected_categories=protected_categories,
        forecast_days=forecast_days,
        include_projected_recurring=include_projected_recurring,
        include_projected_essentials=include_projected_essentials,
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
            protected_categories=protected_categories,
            forecast_days=forecast_days,
            include_projected_recurring=include_projected_recurring,
            include_projected_essentials=include_projected_essentials,
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
    protected_categories: Optional[AbstractSet[str]] = None,
    forecast_days: int = 90,
    include_projected_recurring: bool = True,
    include_projected_essentials: bool = True,
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
        protected_categories: Optional set of categories to protect.
        forecast_days: Horizon in days (default: 90).
        include_projected_recurring: If True, forecasts future recurring commitments.
        include_projected_essentials: If True, forecasts future protected variable essentials.

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
            protected_categories=protected_categories,
            forecast_days=forecast_days,
            include_projected_recurring=include_projected_recurring,
            include_projected_essentials=include_projected_essentials,
        )
        if res.is_safe:
            return candidate_date

    return None
