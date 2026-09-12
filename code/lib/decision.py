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
                              evaluate_schedule_safety, clean_decimal)

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
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import AbstractSet, Dict, List, Literal, Optional, Sequence, Set, Tuple

from .models import FinancialProfile, PaymentOption, Request
from .reconciliation import ReconciledEvent, ReconciledLedger
from .simulation import (
    SafetyResult,
    clean_decimal,
    compute_amount_safe_to_pay,
    evaluate_schedule_safety,
    find_earliest_date_for_full_payment,
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
        stopped_categories: Optional set of categories stopped for simulation.
        reduced_categories: Optional dict of category -> reduced amount for simulation.
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
    stopped_categories: Optional[Tuple[str, ...]] = None
    reduced_categories: Optional[Tuple[Tuple[str, Decimal], ...]] = None


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
    """A single atomic spending change candidate."""
    action: Literal["stop", "reduce_to"]
    category: str
    event_id: str
    new_amount: Optional[Decimal]
    change_str: str


def find_eligible_spending_changes(
    profile: FinancialProfile,
    ledger: ReconciledLedger,
) -> List[AtomicSpendingChange]:
    """Find all eligible atomic spending changes for a user.

    Rules:
      - Target only flexible debit events in ledger.all_events.
      - Never modify categories in profile.expense_categories_to_protect.
      - stop:<event_id> requires category in profile.expense_categories_user_is_willing_to_stop
        and event.flexibility in ('stoppable', 'reducible_or_stoppable').
      - reduce_to:<event_id>:<new_amount> requires category in profile.expense_categories_user_is_willing_to_reduce
        and event.flexibility in ('reducible', 'reducible_or_stoppable') and minimum_allowed_amount is not None.
      - The event_id referenced is the representative/latest settled event of that flexible category.
    """
    protected = profile.expense_categories_to_protect or frozenset()
    willing_stop = profile.expense_categories_user_is_willing_to_stop or frozenset()
    willing_reduce = profile.expense_categories_user_is_willing_to_reduce or frozenset()

    # Find flexible debit events
    candidates: List[AtomicSpendingChange] = []
    category_events: Dict[str, List[ReconciledEvent]] = {}
    for ev in ledger.all_events:
        if ev.direction == "debit" and ev.flexibility in ("reducible", "stoppable", "reducible_or_stoppable"):
            if ev.category not in protected:
                category_events.setdefault(ev.category, []).append(ev)

    for cat, ev_list in sorted(category_events.items()):
        # Select the most recent representative event for this category stream
        latest_ev = max(ev_list, key=lambda x: (x.settlement_date, x.event_date, x.event_id))

        # Check stoppable
        if cat in willing_stop and latest_ev.flexibility in ("stoppable", "reducible_or_stoppable"):
            candidates.append(AtomicSpendingChange(
                action="stop",
                category=cat,
                event_id=latest_ev.event_id,
                new_amount=None,
                change_str=f"stop:{latest_ev.event_id}",
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
            ))

    return candidates


def generate_spending_change_combinations(
    atomic_changes: Sequence[AtomicSpendingChange],
    max_changes: int = 3,
) -> List[Tuple[AtomicSpendingChange, ...]]:
    """Generate all valid subsets of atomic spending changes of size 1 to max_changes.

    Constraints:
      - At most 3 changes.
      - Never both stop and reduce on the same event or category.
      - Deterministic ordering by size and change string.
    """
    valid_subsets: List[Tuple[AtomicSpendingChange, ...]] = []
    for k in range(1, min(max_changes, len(atomic_changes)) + 1):
        for combo in itertools.combinations(atomic_changes, k):
            # Check for conflicting categories (cannot both stop and reduce same category)
            categories = [c.category for c in combo]
            if len(categories) == len(set(categories)):
                valid_subsets.append(combo)

    return valid_subsets


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
      5. Spending-change candidates (when immediate/scheduled plans are unsafe).

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
    if (
        request.allows_partial_payment
        and "partial_payment" in methods_considered
        and Decimal("0.00") < baseline_amount_safe < request.requested_amount
        and baseline_earliest_date is not None
        and baseline_earliest_date <= request.desired_completion_date
    ):
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
    # Only consider spending changes when an otherwise desirable candidate is unsafe.
    # We evaluate spending changes for full payment today, and for any installment options
    # that failed simulation without changes.
    atomic_changes = find_eligible_spending_changes(profile, ledger)
    if atomic_changes:
        change_combos = generate_spending_change_combinations(atomic_changes, max_changes=3)

        # Plan A: Full payment today with spending changes
        if full_payment_eligible and full_payment_unsafe_today and (request.request_date <= request.desired_completion_date):
            fp_payments = ((request.request_date, request.requested_amount),)
            for combo in change_combos:
                stopped_cats = {c.category for c in combo if c.action == "stop"}
                reduced_cats = {c.category: c.new_amount for c in combo if c.action == "reduce_to" and c.new_amount is not None}

                safety = evaluate_schedule_safety(
                    ledger=ledger,
                    request_date=request.request_date,
                    payments=fp_payments,
                    minimum_balance_to_keep=target_min,
                    protected_categories=protected,
                    forecast_days=forecast_days,
                    stopped_categories=frozenset(stopped_cats) if stopped_cats else None,
                    reduced_categories=reduced_cats if reduced_cats else None,
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
                        stopped_categories=tuple(stopped_cats) if stopped_cats else None,
                        reduced_categories=tuple(reduced_cats.items()) if reduced_cats else None,
                    ))

        # Plan B: Installment options with spending changes (if user considers installments)
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
                # Check if already added as safe without changes
                already_safe = any(
                    c.recommended_payment_method == "installments"
                    and c.payment_option_id == opt.payment_option_id
                    and len(c.spending_changes) == 0
                    for c in candidates
                )
                if already_safe:
                    continue

                for combo in change_combos:
                    stopped_cats = {c.category for c in combo if c.action == "stop"}
                    reduced_cats = {c.category: c.new_amount for c in combo if c.action == "reduce_to" and c.new_amount is not None}

                    safety = evaluate_schedule_safety(
                        ledger=ledger,
                        request_date=request.request_date,
                        payments=inst_payments,
                        minimum_balance_to_keep=target_min,
                        protected_categories=protected,
                        forecast_days=forecast_days,
                        stopped_categories=frozenset(stopped_cats) if stopped_cats else None,
                        reduced_categories=reduced_cats if reduced_cats else None,
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
                            stopped_categories=tuple(stopped_cats) if stopped_cats else None,
                            reduced_categories=tuple(reduced_cats.items()) if reduced_cats else None,
                        ))

    return candidates


# ---------------------------------------------------------------------------
# Candidate Ranking & Decision Evaluation
# ---------------------------------------------------------------------------

def candidate_ranking_key(c: CandidatePlan, request: Request) -> Tuple:
    """Exact ranking key for safe, eligible candidates:

    1. Completes by desired_completion_date (False < True, so on-time is 0).
    2. Requires no spending changes (False < True, so 0 changes is 0).
    3. Fewer spending changes (if spending changes are needed).
    4. Lowest total amount paid (Decimal).
    5. Earlier first payment (date).
    6. Fewer payments (int).
    7. Lowest payment_option_id (str, for tie-breaking).
    """
    is_late = c.completion_date > request.desired_completion_date
    has_spending_changes = len(c.spending_changes) > 0
    num_spending_changes = len(c.spending_changes)
    opt_id = c.payment_option_id or ""

    return (
        is_late,
        has_spending_changes,
        num_spending_changes,
        c.total_paid,
        c.first_payment_date,
        c.number_of_payments,
        opt_id,
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
        # No safe plan completing on time exists
        return DecisionResult(
            request_id=request.request_id,
            amount_safe_to_pay=amount_safe_to_pay,
            affordability_status="not_affordable",
            recommended_payment_method="not_recommended",
            payment_plan="none",
            earliest_date_for_full_payment=None,
            spending_changes_needed="none",
            decision_explanation="Not affordable: no safe payment plan completes by the deadline.",
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

    return DecisionResult(
        request_id=request.request_id,
        amount_safe_to_pay=amount_safe_to_pay,
        affordability_status=best.affordability_status,
        recommended_payment_method=best.recommended_payment_method,
        payment_plan=best.payment_plan_str,
        earliest_date_for_full_payment=final_earliest_date,
        spending_changes_needed=best.spending_changes_str,
        decision_explanation=f"Recommended {best.recommended_payment_method} plan.",
        candidate_plan=best,
    )
