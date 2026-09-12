"""
Deterministic Decision and Payment-Plan Construction Engine (Phase 5).

Consumes:
  1. Request (request_date, requested_amount, desired_completion_date, allows_partial_payment)
  2. FinancialProfile (minimum_balance_to_keep, expense_categories_to_protect,
                       expense_categories_user_is_willing_to_reduce,
                       expense_categories_user_is_willing_to_stop,
                       payment_methods_user_will_consider, max_installment_months)
  3. ReconciledLedger (current_available_balance, all_events, recurring_events, etc.)
  4. PaymentOption list (seller/provider options for this request)
  5. Phase 4 simulation APIs (compute_amount_safe_to_pay, find_earliest_date_for_full_payment,
                              evaluate_schedule_safety, clean_decimal, StreamSpendingChange)

Produces:
  DecisionResult: typed result containing the 8 required fields:
    - request_id
    - amount_safe_to_pay
    - affordability_status ("affordable_now", "affordable_with_plan", "affordable_later", "not_affordable")
    - recommended_payment_method ("full_payment", "partial_payment", "installments", "wait", "not_recommended")
    - payment_plan (e.g. "YYYY-MM-DD:amount|..." or "none")
    - earliest_date_for_full_payment (date or None)
    - spending_changes_needed (e.g. "stop:<id>|reduce_to:<id>:<amt>" or "none")
    - decision_explanation (grounded summary or placeholder)

Strict monetary precision:
  - All monetary calculations and balance validations use `Decimal`.
  - Zero floating-point arithmetic.
"""

from __future__ import annotations

import itertools
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import AbstractSet, Dict, List, Literal, Optional, Sequence, Set, Tuple

from .models import FinancialProfile, PaymentOption, Request
from .reconciliation import ReconciledEvent, ReconciledLedger
from .simulation import (
    SafetyResult,
    StreamSpendingChange,
    clean_decimal,
    compute_amount_safe_to_pay,
    evaluate_schedule_safety,
    find_earliest_date_for_full_payment,
    get_recurring_stream_identifier,
)


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CandidatePlan:
    """An eligible candidate payment plan evaluated for safety.

    Attributes:
        recommended_payment_method: Payment method type.
        affordability_status: Resulting status if this candidate is chosen.
        payments: Chronological sequence of (payment_date, amount) tuples.
        payment_plan_str: Formatted payment plan string (YYYY-MM-DD:amount|... or 'none').
        spending_changes: Tuple of formatted spending change actions (e.g. ('stop:event_1',)).
        spending_changes_str: Formatted spending changes string (or 'none').
        completion_date: Date on which the full requested expense is completed.
        total_paid: Total sum payable across all payments in the plan.
        first_payment_date: Date of the first payment.
        number_of_payments: Total number of payments in the plan.
        payment_option_id: Optional ID of the seller payment option used (for tie-breaking).
        is_safe: True if plan passes schedule safety.
        stream_spending_changes: Optional tuple of StreamSpendingChange applied during simulation.
    """
    recommended_payment_method: Literal[
        "full_payment", "partial_payment", "installments", "wait", "not_recommended"
    ]
    affordability_status: Literal[
        "affordable_now", "affordable_with_plan", "affordable_later", "not_affordable"
    ]
    payments: Tuple[Tuple[date, Decimal], ...]
    payment_plan_str: str
    spending_changes: Tuple[str, ...]
    spending_changes_str: str
    completion_date: date
    total_paid: Decimal
    first_payment_date: date
    number_of_payments: int
    payment_option_id: Optional[str] = None
    is_safe: bool = True
    stream_spending_changes: Optional[Tuple[StreamSpendingChange, ...]] = None


@dataclass(frozen=True)
class DecisionResult:
    """The final typed decision result for an evaluation request."""
    request_id: str
    amount_safe_to_pay: Decimal
    affordability_status: Literal[
        "affordable_now", "affordable_with_plan", "affordable_later", "not_affordable"
    ]
    recommended_payment_method: Literal[
        "full_payment", "partial_payment", "installments", "wait", "not_recommended"
    ]
    payment_plan: str
    earliest_date_for_full_payment: Optional[date]
    spending_changes_needed: str
    decision_explanation: str
    candidate_plan: Optional[CandidatePlan] = None


# ---------------------------------------------------------------------------
# Spending Change Candidates Discovery
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AtomicSpendingChange:
    """A single atomic spending change candidate scoped to a flexible recurring stream."""
    action: Literal["stop", "reduce_to"]
    category: str
    event_id: str
    new_amount: Optional[Decimal]
    change_str: str
    stream_id: str = ""
    matched_event_ids: Optional[frozenset[str]] = None


def find_eligible_spending_changes(
    profile: FinancialProfile,
    ledger: ReconciledLedger,
) -> List[AtomicSpendingChange]:
    """Find all eligible atomic spending changes for a user.

    Rules:
      - Considers ONLY events that are ALL of:
          1. debit (direction == 'debit')
          2. flexible (flexibility in ('reducible', 'stoppable', 'reducible_or_stoppable'))
          3. is_recurring == True
          4. outside protected categories (category not in expense_categories_to_protect)
          5. allowed by relevant stop/reduce preference:
             - stop:<event_id> requires category in expense_categories_user_is_willing_to_stop
               and flexibility in ('stoppable', 'reducible_or_stoppable')
             - reduce_to:<event_id>:<new_amount> requires category in expense_categories_user_is_willing_to_reduce
               and flexibility in ('reducible', 'reducible_or_stoppable') and minimum_allowed_amount is not None
      - Groups candidates by stream identity (using get_recurring_stream_identifier),
        allowing multiple distinct streams sharing a category to be discovered and managed independently.
      - The event_id referenced is the representative/latest settled event of that flexible recurring stream.
    """
    protected = profile.expense_categories_to_protect or frozenset()
    willing_stop = profile.expense_categories_user_is_willing_to_stop or frozenset()
    willing_reduce = profile.expense_categories_user_is_willing_to_reduce or frozenset()

    # Find flexible recurring debit events and group by stream identity
    eligible_events = [
        ev for ev in ledger.all_events
        if (
            ev.direction == "debit"
            and ev.is_recurring
            and ev.flexibility in ("reducible", "stoppable", "reducible_or_stoppable")
            and ev.category not in protected
        )
    ]

    stream_events: Dict[str, List[ReconciledEvent]] = defaultdict(list)
    for ev in eligible_events:
        cat_evs = [e for e in eligible_events if e.category == ev.category]
        stream_id = get_recurring_stream_identifier(ev, cat_evs)
        stream_events[stream_id].append(ev)

    candidates: List[AtomicSpendingChange] = []
    for stream_id, ev_list in sorted(stream_events.items()):
        cat = ev_list[0].category
        # Select the most recent representative event for this recurring stream
        latest_ev = max(ev_list, key=lambda x: (x.settlement_date or x.event_date, x.event_id))
        matched_ids = frozenset(e.event_id for e in ev_list)

        # Check stoppable
        if cat in willing_stop and latest_ev.flexibility in ("stoppable", "reducible_or_stoppable"):
            candidates.append(AtomicSpendingChange(
                action="stop",
                category=cat,
                event_id=latest_ev.event_id,
                new_amount=None,
                change_str=f"stop:{latest_ev.event_id}",
                stream_id=stream_id,
                matched_event_ids=matched_ids,
            ))

        # Check reducible
        if (
            cat in willing_reduce
            and latest_ev.flexibility in ("reducible", "reducible_or_stoppable")
            and latest_ev.minimum_allowed_amount is not None
        ):
            clean_amt = clean_decimal(latest_ev.minimum_allowed_amount)
            candidates.append(AtomicSpendingChange(
                action="reduce_to",
                category=cat,
                event_id=latest_ev.event_id,
                new_amount=clean_amt,
                change_str=f"reduce_to:{latest_ev.event_id}:{clean_amt}",
                stream_id=stream_id,
                matched_event_ids=matched_ids,
            ))

    return candidates


def generate_spending_change_combinations(
    atomic_changes: Sequence[AtomicSpendingChange],
    max_changes: int = 3,
) -> List[Tuple[AtomicSpendingChange, ...]]:
    """Generate all valid subsets of atomic spending changes of size 1 to max_changes.

    Constraints:
      - At most 3 changes.
      - Never both stop and reduce on the same event or stream.
      - Deterministic ordering.
    """
    valid_subsets: List[Tuple[AtomicSpendingChange, ...]] = []
    for k in range(1, min(max_changes, len(atomic_changes)) + 1):
        for combo in itertools.combinations(atomic_changes, k):
            # Check for conflicting streams (cannot both stop and reduce same stream)
            stream_keys = [c.stream_id or c.event_id for c in combo]
            if len(stream_keys) == len(set(stream_keys)):
                valid_subsets.append(combo)

    return valid_subsets


def to_stream_spending_changes(combo: Sequence[AtomicSpendingChange]) -> List[StreamSpendingChange]:
    """Convert atomic spending changes into stream-scoped simulation directives."""
    return [
        StreamSpendingChange(
            action=c.action,
            target_event_id=c.event_id,
            category=c.category,
            new_amount=c.new_amount,
            matched_event_ids=c.matched_event_ids,
        )
        for c in combo
    ]


# ---------------------------------------------------------------------------
# Candidate Plan Builder
# ---------------------------------------------------------------------------

def build_candidate_plans(
    request: Request,
    profile: FinancialProfile,
    ledger: ReconciledLedger,
    payment_options: Sequence[PaymentOption],
    baseline_amount_safe: Decimal,
    baseline_earliest_date: Optional[date],
    forecast_days: int = 90,
) -> List[CandidatePlan]:
    """Build and evaluate all eligible candidate payment plans.

    Considers:
      1. Full payment today (without spending changes).
      2. Partial payment (without spending changes).
      3. Installments (from payment options, without spending changes).
      4. Wait (later full payment, without spending changes).
      5. Spending-change candidates:
         - Full payment today with spending changes.
         - Partial payment with spending changes.
         - Installments with spending changes.

    Returns:
      List of safe CandidatePlan objects.
    """
    candidates: List[CandidatePlan] = []
    methods_considered = profile.payment_methods_user_will_consider or frozenset()
    protected = profile.expense_categories_to_protect or frozenset()
    target_min = profile.minimum_balance_to_keep

    # -----------------------------------------------------------------------
    # 1. Full Payment (no spending changes)
    # -----------------------------------------------------------------------
    full_payment_eligible = "full_payment" in methods_considered
    full_payment_unsafe_today = False

    if full_payment_eligible:
        fp_payments = ((request.request_date, request.requested_amount),)
        safety = evaluate_schedule_safety(
            ledger=ledger,
            request_date=request.request_date,
            payments=fp_payments,
            minimum_balance_to_keep=target_min,
            protected_categories=protected,
            forecast_days=forecast_days,
        )
        if safety.is_safe:
            candidates.append(CandidatePlan(
                recommended_payment_method="full_payment",
                affordability_status="affordable_now",
                payments=fp_payments,
                payment_plan_str=f"{request.request_date.isoformat()}:{clean_decimal(request.requested_amount)}",
                spending_changes=(),
                spending_changes_str="none",
                completion_date=request.request_date,
                total_paid=request.requested_amount,
                first_payment_date=request.request_date,
                number_of_payments=1,
                payment_option_id=None,
                is_safe=True,
            ))
        else:
            full_payment_unsafe_today = True

    # -----------------------------------------------------------------------
    # 2. Partial Payment (no spending changes)
    # -----------------------------------------------------------------------
    # Eligible only if request allows it, user considers it, 0 < amount_safe < requested,
    # and second payment on earliest_date completes by desired_completion_date.
    partial_payment_eligible = (
        request.allows_partial_payment
        and "partial_payment" in methods_considered
        and Decimal("0.00") < baseline_amount_safe < request.requested_amount
        and baseline_earliest_date is not None
        and baseline_earliest_date <= request.desired_completion_date
    )
    partial_payment_unsafe_no_changes = False

    if partial_payment_eligible:
        remainder = request.requested_amount - baseline_amount_safe
        part_payments = (
            (request.request_date, baseline_amount_safe),
            (baseline_earliest_date, remainder),
        )
        safety = evaluate_schedule_safety(
            ledger=ledger,
            request_date=request.request_date,
            payments=part_payments,
            minimum_balance_to_keep=target_min,
            protected_categories=protected,
            forecast_days=forecast_days,
        )
        if safety.is_safe:
            p_plan_str = (
                f"{request.request_date.isoformat()}:{clean_decimal(baseline_amount_safe)}|"
                f"{baseline_earliest_date.isoformat()}:{clean_decimal(remainder)}"
            )
            candidates.append(CandidatePlan(
                recommended_payment_method="partial_payment",
                affordability_status="affordable_with_plan",
                payments=part_payments,
                payment_plan_str=p_plan_str,
                spending_changes=(),
                spending_changes_str="none",
                completion_date=baseline_earliest_date,
                total_paid=request.requested_amount,
                first_payment_date=request.request_date,
                number_of_payments=2,
                payment_option_id=None,
                is_safe=True,
            ))
        else:
            partial_payment_unsafe_no_changes = True

    # -----------------------------------------------------------------------
    # 3. Installments (no spending changes)
    # -----------------------------------------------------------------------
    if "installments" in methods_considered and profile.max_installment_months is not None:
        for opt in payment_options:
            if opt.payment_method != "installments":
                continue
            if opt.number_of_payments > profile.max_installment_months:
                continue

            freq = opt.payment_frequency_days or 30
            dates = [
                opt.first_payment_date + timedelta(days=i * freq)
                for i in range(opt.number_of_payments)
            ]
            # Must fall within 90-day window
            max_sim_date = request.request_date + timedelta(days=forecast_days)
            if any(d < request.request_date or d > max_sim_date for d in dates):
                continue
            # Must complete by desired_completion_date
            if dates[-1] > request.desired_completion_date:
                continue

            inst_payments = tuple((d, opt.payment_amount) for d in dates)
            safety = evaluate_schedule_safety(
                ledger=ledger,
                request_date=request.request_date,
                payments=inst_payments,
                minimum_balance_to_keep=target_min,
                protected_categories=protected,
                forecast_days=forecast_days,
            )
            if safety.is_safe:
                plan_str = "|".join(
                    f"{d.isoformat()}:{clean_decimal(opt.payment_amount)}" for d in dates
                )
                candidates.append(CandidatePlan(
                    recommended_payment_method="installments",
                    affordability_status="affordable_with_plan",
                    payments=inst_payments,
                    payment_plan_str=plan_str,
                    spending_changes=(),
                    spending_changes_str="none",
                    completion_date=dates[-1],
                    total_paid=opt.total_payable_amount,
                    first_payment_date=dates[0],
                    number_of_payments=opt.number_of_payments,
                    payment_option_id=opt.payment_option_id,
                    is_safe=True,
                ))

    # -----------------------------------------------------------------------
    # 4. Wait (no spending changes)
    # -----------------------------------------------------------------------
    if (
        full_payment_eligible
        and baseline_earliest_date is not None
        and baseline_earliest_date > request.request_date
        and baseline_earliest_date <= request.desired_completion_date
    ):
        wait_payments = ((baseline_earliest_date, request.requested_amount),)
        safety = evaluate_schedule_safety(
            ledger=ledger,
            request_date=request.request_date,
            payments=wait_payments,
            minimum_balance_to_keep=target_min,
            protected_categories=protected,
            forecast_days=forecast_days,
        )
        if safety.is_safe:
            candidates.append(CandidatePlan(
                recommended_payment_method="wait",
                affordability_status="affordable_later",
                payments=wait_payments,
                payment_plan_str=f"{baseline_earliest_date.isoformat()}:{clean_decimal(request.requested_amount)}",
                spending_changes=(),
                spending_changes_str="none",
                completion_date=baseline_earliest_date,
                total_paid=request.requested_amount,
                first_payment_date=baseline_earliest_date,
                number_of_payments=1,
                payment_option_id=None,
                is_safe=True,
            ))

    # -----------------------------------------------------------------------
    # 5. Spending-Change Candidates
    # -----------------------------------------------------------------------
    # Evaluated for candidates that are unsafe without changes.
    atomic_changes = find_eligible_spending_changes(profile, ledger)
    if atomic_changes:
        change_combos = generate_spending_change_combinations(atomic_changes, max_changes=3)

        # Plan A: Full payment today with spending changes
        if full_payment_eligible and full_payment_unsafe_today and (request.request_date <= request.desired_completion_date):
            fp_payments = ((request.request_date, request.requested_amount),)
            for combo in change_combos:
                stream_changes = to_stream_spending_changes(combo)
                safety = evaluate_schedule_safety(
                    ledger=ledger,
                    request_date=request.request_date,
                    payments=fp_payments,
                    minimum_balance_to_keep=target_min,
                    protected_categories=protected,
                    forecast_days=forecast_days,
                    stream_spending_changes=stream_changes,
                )
                if safety.is_safe:
                    change_strings = tuple(c.change_str for c in combo)
                    candidates.append(CandidatePlan(
                        recommended_payment_method="full_payment",
                        affordability_status="affordable_with_plan",  # Per rule: affordable_with_plan, NOT affordable_now
                        payments=fp_payments,
                        payment_plan_str=f"{request.request_date.isoformat()}:{clean_decimal(request.requested_amount)}",
                        spending_changes=change_strings,
                        spending_changes_str="|".join(change_strings),
                        completion_date=request.request_date,
                        total_paid=request.requested_amount,
                        first_payment_date=request.request_date,
                        number_of_payments=1,
                        payment_option_id=None,
                        is_safe=True,
                        stream_spending_changes=tuple(stream_changes),
                    ))

        # Plan B: Partial payment with spending changes
        if partial_payment_eligible and partial_payment_unsafe_no_changes:
            remainder = request.requested_amount - baseline_amount_safe
            part_payments = (
                (request.request_date, baseline_amount_safe),
                (baseline_earliest_date, remainder),
            )
            for combo in change_combos:
                stream_changes = to_stream_spending_changes(combo)
                safety = evaluate_schedule_safety(
                    ledger=ledger,
                    request_date=request.request_date,
                    payments=part_payments,
                    minimum_balance_to_keep=target_min,
                    protected_categories=protected,
                    forecast_days=forecast_days,
                    stream_spending_changes=stream_changes,
                )
                if safety.is_safe:
                    change_strings = tuple(c.change_str for c in combo)
                    p_plan_str = (
                        f"{request.request_date.isoformat()}:{clean_decimal(baseline_amount_safe)}|"
                        f"{baseline_earliest_date.isoformat()}:{clean_decimal(remainder)}"
                    )
                    candidates.append(CandidatePlan(
                        recommended_payment_method="partial_payment",
                        affordability_status="affordable_with_plan",
                        payments=part_payments,
                        payment_plan_str=p_plan_str,
                        spending_changes=change_strings,
                        spending_changes_str="|".join(change_strings),
                        completion_date=baseline_earliest_date,
                        total_paid=request.requested_amount,
                        first_payment_date=request.request_date,
                        number_of_payments=2,
                        payment_option_id=None,
                        is_safe=True,
                        stream_spending_changes=tuple(stream_changes),
                    ))

        # Plan C: Installment options with spending changes
        if "installments" in methods_considered and profile.max_installment_months is not None:
            for opt in payment_options:
                if opt.payment_method != "installments":
                    continue
                if opt.number_of_payments > profile.max_installment_months:
                    continue
                freq = opt.payment_frequency_days or 30
                dates = [
                    opt.first_payment_date + timedelta(days=i * freq)
                    for i in range(opt.number_of_payments)
                ]
                max_sim_date = request.request_date + timedelta(days=forecast_days)
                if any(d < request.request_date or d > max_sim_date for d in dates):
                    continue
                if dates[-1] > request.desired_completion_date:
                    continue

                inst_payments = tuple((d, opt.payment_amount) for d in dates)
                # Skip if already added as safe without changes
                already_safe = any(
                    c.recommended_payment_method == "installments"
                    and c.payment_option_id == opt.payment_option_id
                    and len(c.spending_changes) == 0
                    for c in candidates
                )
                if already_safe:
                    continue

                for combo in change_combos:
                    stream_changes = to_stream_spending_changes(combo)
                    safety = evaluate_schedule_safety(
                        ledger=ledger,
                        request_date=request.request_date,
                        payments=inst_payments,
                        minimum_balance_to_keep=target_min,
                        protected_categories=protected,
                        forecast_days=forecast_days,
                        stream_spending_changes=stream_changes,
                    )
                    if safety.is_safe:
                        change_strings = tuple(c.change_str for c in combo)
                        plan_str = "|".join(
                            f"{d.isoformat()}:{clean_decimal(opt.payment_amount)}" for d in dates
                        )
                        candidates.append(CandidatePlan(
                            recommended_payment_method="installments",
                            affordability_status="affordable_with_plan",
                            payments=inst_payments,
                            payment_plan_str=plan_str,
                            spending_changes=change_strings,
                            spending_changes_str="|".join(change_strings),
                            completion_date=dates[-1],
                            total_paid=opt.total_payable_amount,
                            first_payment_date=dates[0],
                            number_of_payments=opt.number_of_payments,
                            payment_option_id=opt.payment_option_id,
                            is_safe=True,
                            stream_spending_changes=tuple(stream_changes),
                        ))

    return candidates


# ---------------------------------------------------------------------------
# Candidate Ranking & Decision Evaluation
# ---------------------------------------------------------------------------

def candidate_ranking_key(c: CandidatePlan, request: Request) -> Tuple:
    """Exact ranking key for safe, eligible candidates:

    1. Completes by desired_completion_date (False < True, so on-time is 0).
    2. Requires no spending changes (False < True, so 0 changes is 0).
    3. Lowest total amount paid (Decimal).
    4. Earlier first payment (date).
    5. Fewer payments (int).
    6. Lowest payment_option_id (str, for tie-breaking).
    """
    is_late = c.completion_date > request.desired_completion_date
    has_spending_changes = len(c.spending_changes) > 0
    opt_id = c.payment_option_id or ""

    return (
        is_late,
        has_spending_changes,
        c.total_paid,
        c.first_payment_date,
        c.number_of_payments,
        opt_id,
    )


def _format_date(d: date) -> str:
    """Format date as e.g. '15 November 2019' or '8 August 2025'."""
    return f"{d.day} {d.strftime('%B')} {d.year}"


def _format_money(val: Decimal) -> str:
    """Format monetary decimal cleanly with commas for thousands."""
    cleaned = clean_decimal(val)
    parts = str(cleaned).split(".")
    int_part = f"{int(parts[0]):,}"
    if len(parts) > 1:
        return f"{int_part}.{parts[1]}"
    return int_part


def _describe_spending_changes(
    spending_changes_str: str,
    ledger: ReconciledLedger,
    currency: str,
) -> str:
    """Describe required spending changes using grounded ledger descriptions."""
    if not spending_changes_str or spending_changes_str == "none":
        return ""

    event_by_id = {e.event_id: e for e in ledger.all_events}
    descriptions: List[str] = []

    for item in spending_changes_str.split("|"):
        item = item.strip()
        if not item:
            continue
        if item.startswith("stop:"):
            ev_id = item.split(":", 1)[1]
            ev = event_by_id.get(ev_id)
            name = ev.description.strip() if ev and ev.description else ev_id
            if not name.isupper():
                name = name[0].lower() + name[1:] if len(name) > 1 else name.lower()
            descriptions.append(f"stop the {name}")
        elif item.startswith("reduce_to:"):
            parts = item.split(":")
            if len(parts) == 3:
                ev_id, new_amt_str = parts[1], parts[2]
                ev = event_by_id.get(ev_id)
                name = ev.description.strip() if ev and ev.description else ev_id
                if not name.isupper():
                    name = name[0].lower() + name[1:] if len(name) > 1 else name.lower()
                amt_formatted = _format_money(Decimal(new_amt_str))
                descriptions.append(f"reduce the {name} to {currency} {amt_formatted}")
            else:
                descriptions.append(f"reduce {item}")
        else:
            descriptions.append(item)

    if not descriptions:
        return ""
    if len(descriptions) == 1:
        desc_text = descriptions[0]
    elif len(descriptions) == 2:
        desc_text = f"{descriptions[0]} and {descriptions[1]}"
    else:
        desc_text = f"{', '.join(descriptions[:-1])}, and {descriptions[-1]}"

    return desc_text[0].upper() + desc_text[1:]


def generate_decision_explanation(
    request: Request,
    profile: FinancialProfile,
    ledger: ReconciledLedger,
    amount_safe_to_pay: Decimal,
    affordability_status: str,
    recommended_payment_method: str,
    payment_plan: str,
    earliest_date_for_full_payment: Optional[date],
    spending_changes_needed: str,
    candidate_plan: Optional[CandidatePlan] = None,
) -> str:
    """Generate a concise, grounded, deterministic decision explanation from decision facts."""
    curr = profile.home_currency
    req_amt_str = f"{curr} {_format_money(request.requested_amount)}"
    min_bal_str = f"{curr} {_format_money(profile.minimum_balance_to_keep)}"

    if affordability_status == "affordable_now" and recommended_payment_method == "full_payment":
        return (
            f"Pay {req_amt_str} today. "
            f"This leaves at least {min_bal_str} available over the next 90 days."
        )

    if affordability_status == "affordable_with_plan":
        if recommended_payment_method == "full_payment":
            changes_desc = _describe_spending_changes(spending_changes_needed, ledger, curr)
            if changes_desc:
                return (
                    f"{changes_desc}, then pay {req_amt_str} today. "
                    f"This leaves at least {min_bal_str} available."
                )
            return (
                f"Pay {req_amt_str} today with spending changes. "
                f"This leaves at least {min_bal_str} available."
            )

        if recommended_payment_method == "partial_payment":
            first_amt_str = f"{curr} {_format_money(amount_safe_to_pay)}"
            rem_amt = request.requested_amount - amount_safe_to_pay
            rem_amt_str = f"{curr} {_format_money(rem_amt)}"
            date_str = _format_date(earliest_date_for_full_payment) if earliest_date_for_full_payment else "a later date"
            changes_desc = _describe_spending_changes(spending_changes_needed, ledger, curr)
            prefix = f"{changes_desc}, then pay" if changes_desc else "Pay"
            return (
                f"{prefix} {first_amt_str} today and the remaining {rem_amt_str} on {date_str}. "
                f"This completes the full request and keeps the {min_bal_str} minimum protected."
            )

        if recommended_payment_method == "installments":
            if candidate_plan and candidate_plan.payments:
                count = candidate_plan.number_of_payments
                first_date = candidate_plan.first_payment_date
                first_date_str = _format_date(first_date)
                inst_amt = candidate_plan.payments[0][1]
                inst_amt_str = f"{curr} {_format_money(inst_amt)}"
                changes_desc = _describe_spending_changes(spending_changes_needed, ledger, curr)
                prefix = f"{changes_desc}, then " if changes_desc else ""
                return (
                    f"{prefix}Use {count} installments of {inst_amt_str}, starting {first_date_str}. "
                    f"This leaves at least {min_bal_str} available."
                )
            return f"Use an installment plan. This leaves at least {min_bal_str} available."

    if affordability_status == "affordable_later" and recommended_payment_method == "wait":
        full_date = earliest_date_for_full_payment or (candidate_plan.first_payment_date if candidate_plan else None)
        date_str = _format_date(full_date) if full_date else "a later date"
        return (
            f"Pay {req_amt_str} in full on {date_str}. "
            f"Paying earlier would take the balance below the {min_bal_str} minimum."
        )

    # not_affordable / not_recommended
    if earliest_date_for_full_payment is None:
        avail_str = f"{curr} {_format_money(amount_safe_to_pay)}"
        return (
            f"Do not proceed with the {req_amt_str} request. "
            f"Although {avail_str} is available today, the full amount cannot be completed safely within 90 days."
        )
    else:
        deadline_str = _format_date(request.desired_completion_date)
        return (
            f"Do not make this payment by {deadline_str}. "
            f"None of the available options keeps the {min_bal_str} minimum protected."
        )


def evaluate_decision(
    request: Request,
    profile: FinancialProfile,
    ledger: ReconciledLedger,
    payment_options: Sequence[PaymentOption],
    forecast_days: int = 90,
) -> DecisionResult:
    """Evaluate an evaluation request and return the typed DecisionResult.

    Steps:
      1. Compute baseline affordability facts (amount_safe_to_pay, earliest_date_for_full_payment).
      2. Build all safe eligible candidate plans.
      3. Rank candidates according to problem criteria.
      4. Map to final DecisionResult fields.
    """
    # Step 1: Baseline affordability facts (without spending changes)
    amount_safe_to_pay = compute_amount_safe_to_pay(
        ledger=ledger,
        request_date=request.request_date,
        requested_amount=request.requested_amount,
        minimum_balance_to_keep=profile.minimum_balance_to_keep,
        protected_categories=profile.expense_categories_to_protect,
        forecast_days=forecast_days,
    )
    earliest_date_for_full_payment = find_earliest_date_for_full_payment(
        ledger=ledger,
        request_date=request.request_date,
        requested_amount=request.requested_amount,
        minimum_balance_to_keep=profile.minimum_balance_to_keep,
        protected_categories=profile.expense_categories_to_protect,
        forecast_days=forecast_days,
    )

    # Step 2: Build eligible candidate plans
    candidates = build_candidate_plans(
        request=request,
        profile=profile,
        ledger=ledger,
        payment_options=payment_options,
        baseline_amount_safe=amount_safe_to_pay,
        baseline_earliest_date=earliest_date_for_full_payment,
        forecast_days=forecast_days,
    )

    # Filter for safe candidates completing on or before desired_completion_date
    on_time_candidates = [
        c for c in candidates
        if c.is_safe and c.completion_date <= request.desired_completion_date
    ]

    if not on_time_candidates:
        # No safe plan completing on time exists.
        # Preserve baseline earliest_date_for_full_payment if it exists in the 90-day horizon.
        explanation = generate_decision_explanation(
            request=request,
            profile=profile,
            ledger=ledger,
            amount_safe_to_pay=amount_safe_to_pay,
            affordability_status="not_affordable",
            recommended_payment_method="not_recommended",
            payment_plan="none",
            earliest_date_for_full_payment=earliest_date_for_full_payment,
            spending_changes_needed="none",
            candidate_plan=None,
        )
        return DecisionResult(
            request_id=request.request_id,
            amount_safe_to_pay=amount_safe_to_pay,
            affordability_status="not_affordable",
            recommended_payment_method="not_recommended",
            payment_plan="none",
            earliest_date_for_full_payment=earliest_date_for_full_payment,
            spending_changes_needed="none",
            decision_explanation=explanation,
            candidate_plan=None,
        )

    # Step 3: Rank candidates
    ranked = sorted(on_time_candidates, key=lambda c: candidate_ranking_key(c, request))
    best = ranked[0]

    # Step 4: Map outcome
    # earliest_date_for_full_payment equals request_date for affordable_now
    final_earliest_date = (
        request.request_date
        if best.affordability_status == "affordable_now"
        else earliest_date_for_full_payment
    )

    explanation = generate_decision_explanation(
        request=request,
        profile=profile,
        ledger=ledger,
        amount_safe_to_pay=amount_safe_to_pay,
        affordability_status=best.affordability_status,
        recommended_payment_method=best.recommended_payment_method,
        payment_plan=best.payment_plan_str,
        earliest_date_for_full_payment=final_earliest_date,
        spending_changes_needed=best.spending_changes_str,
        candidate_plan=best,
    )

    return DecisionResult(
        request_id=request.request_id,
        amount_safe_to_pay=amount_safe_to_pay,
        affordability_status=best.affordability_status,
        recommended_payment_method=best.recommended_payment_method,
        payment_plan=best.payment_plan_str,
        earliest_date_for_full_payment=final_earliest_date,
        spending_changes_needed=best.spending_changes_str,
        decision_explanation=explanation,
        candidate_plan=best,
    )
