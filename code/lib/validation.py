"""
Deterministic fail-closed output validation and serialization for Buy or Wait? decisions.

Enforces:
  1. Exact output schema (8 columns in exact specification order).
  2. Decimal amount bounds (0 <= amount_safe_to_pay <= requested_amount) and non-finite rejection.
  3. Recomputed baseline facts (amount_safe_to_pay and earliest_date_for_full_payment must match
     the independent Phase 4 simulation baseline without spending changes).
  4. Exhaustive status/method matrix enforcement.
  5. Status, method, and payment-plan coherence.
  6. Stream-scoped spending change validity (debit, flexible, recurring, non-protected,
     allowed by user preferences, within minimum allowed amounts).
  7. Independent financial safety re-check using Phase 4 simulation.
  8. Validation-gated deterministic serialization without float conversion.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import AbstractSet, Dict, List, Literal, Optional, Sequence, Set, Tuple

from .decision import DecisionResult
from .models import FinancialProfile, PaymentOption, Request
from .reconciliation import ReconciledEvent, ReconciledLedger
from .simulation import (
    StreamSpendingChange,
    clean_decimal,
    compute_amount_safe_to_pay,
    evaluate_schedule_safety,
    find_earliest_date_for_full_payment,
    get_recurring_stream_identifier,
)


class OutputValidationError(ValueError):
    """Raised when a DecisionResult violates the output contract or financial safety."""
    pass


VALID_AFFORDABILITY_STATUSES: frozenset[str] = frozenset({
    "affordable_now",
    "affordable_with_plan",
    "affordable_later",
    "not_affordable",
})

VALID_RECOMMENDED_PAYMENT_METHODS: frozenset[str] = frozenset({
    "full_payment",
    "partial_payment",
    "installments",
    "wait",
    "not_recommended",
})

ALLOWED_STATUS_METHOD_PAIRS: frozenset[Tuple[str, str]] = frozenset({
    ("affordable_now", "full_payment"),
    ("affordable_with_plan", "full_payment"),
    ("affordable_with_plan", "partial_payment"),
    ("affordable_with_plan", "installments"),
    ("affordable_later", "wait"),
    ("not_affordable", "not_recommended"),
})

OUTPUT_COLUMNS: Tuple[str, ...] = (
    "request_id",
    "amount_safe_to_pay",
    "affordability_status",
    "recommended_payment_method",
    "payment_plan",
    "earliest_date_for_full_payment",
    "spending_changes_needed",
    "decision_explanation",
)


# ---------------------------------------------------------------------------
# Validated Decision Wrapper
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ValidatedDecision:
    """A decision result that has passed fail-closed validation against request, profile, and ledger.
    Must be created via validate_decision_result; direct construction is disallowed.
    """
    decision: DecisionResult
    _validated: bool = False

    def __post_init__(self) -> None:
        if not self._validated:
            raise OutputValidationError(
                "ValidatedDecision cannot be instantiated directly; use validate_decision_result."
            )

    @property
    def request_id(self) -> str:
        return self.decision.request_id

    @property
    def amount_safe_to_pay(self) -> Decimal:
        return self.decision.amount_safe_to_pay

    @property
    def affordability_status(self) -> str:
        return self.decision.affordability_status

    @property
    def recommended_payment_method(self) -> str:
        return self.decision.recommended_payment_method

    @property
    def payment_plan(self) -> str:
        return self.decision.payment_plan

    @property
    def earliest_date_for_full_payment(self) -> Optional[date]:
        return self.decision.earliest_date_for_full_payment

    @property
    def spending_changes_needed(self) -> str:
        return self.decision.spending_changes_needed

    @property
    def decision_explanation(self) -> str:
        return self.decision.decision_explanation

    def __eq__(self, other: object) -> bool:
        if isinstance(other, ValidatedDecision):
            return self.decision == other.decision
        if isinstance(other, DecisionResult):
            return self.decision == other
        return False


# ---------------------------------------------------------------------------
# Parsing Helpers
# ---------------------------------------------------------------------------

def _validate_finite_decimal(val: Decimal, field_name: str) -> None:
    """Ensure a Decimal is finite, rejecting NaN and Infinity."""
    if not isinstance(val, Decimal):
        raise OutputValidationError(f"{field_name} must be Decimal, got {type(val).__name__}.")
    if val.is_nan() or val.is_infinite():
        raise OutputValidationError(f"{field_name} must be a finite Decimal, got {val}.")


def parse_payment_plan(plan_str: str) -> Tuple[Tuple[date, Decimal], ...]:
    """Parse a payment_plan string into a chronological tuple of (date, amount) pairs.

    Raises OutputValidationError if malformed or containing non-finite values.
    """
    if plan_str == "none":
        return ()

    if not plan_str or not plan_str.strip():
        raise OutputValidationError("payment_plan cannot be empty string; use 'none' when no plan is recommended.")

    parts = plan_str.split("|")
    payments: List[Tuple[date, Decimal]] = []

    for item in parts:
        item = item.strip()
        subparts = item.split(":")
        if len(subparts) != 2:
            raise OutputValidationError(f"Invalid payment plan item '{item}'; expected 'YYYY-MM-DD:amount'.")

        date_str, amt_str = subparts[0].strip(), subparts[1].strip()
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            raise OutputValidationError(f"Invalid date '{date_str}' in payment plan item '{item}'.")

        try:
            amt = Decimal(amt_str)
        except InvalidOperation:
            raise OutputValidationError(f"Invalid monetary amount '{amt_str}' in payment plan item '{item}'.")

        if amt.is_nan() or amt.is_infinite():
            raise OutputValidationError(f"Payment plan amount cannot be NaN or infinite in '{item}'.")

        if amt <= Decimal("0"):
            raise OutputValidationError(f"Payment plan amount must be positive, got {amt} in '{item}'.")

        payments.append((d, amt))

    # Verify chronological ordering
    for i in range(1, len(payments)):
        if payments[i][0] < payments[i - 1][0]:
            raise OutputValidationError(
                f"Payment plan dates must be in chronological order: {payments[i-1][0]} is after {payments[i][0]}."
            )

    return tuple(payments)


def parse_spending_changes(spending_str: str) -> List[Tuple[str, str, Optional[Decimal]]]:
    """Parse spending_changes_needed string into a list of (action, event_id, new_amount) tuples.

    Raises OutputValidationError if malformed or containing non-finite amounts.
    """
    if spending_str == "none":
        return []

    if not spending_str or not spending_str.strip():
        raise OutputValidationError("spending_changes_needed cannot be empty string; use 'none' when no changes needed.")

    parts = spending_str.split("|")
    if len(parts) > 3:
        raise OutputValidationError(f"At most 3 spending changes permitted, got {len(parts)}: '{spending_str}'.")

    actions: List[Tuple[str, str, Optional[Decimal]]] = []
    seen_events: Set[str] = set()

    for item in parts:
        item = item.strip()
        subparts = item.split(":")
        if subparts[0] == "stop":
            if len(subparts) != 2 or not subparts[1]:
                raise OutputValidationError(f"Invalid stop action '{item}'; expected 'stop:<event_id>'.")
            ev_id = subparts[1]
            if ev_id in seen_events:
                raise OutputValidationError(f"Duplicate target event '{ev_id}' in spending changes.")
            seen_events.add(ev_id)
            actions.append(("stop", ev_id, None))

        elif subparts[0] == "reduce_to":
            if len(subparts) != 3 or not subparts[1] or not subparts[2]:
                raise OutputValidationError(f"Invalid reduce_to action '{item}'; expected 'reduce_to:<event_id>:<amount>'.")
            ev_id = subparts[1]
            if ev_id in seen_events:
                raise OutputValidationError(f"Duplicate target event '{ev_id}' in spending changes.")
            seen_events.add(ev_id)
            try:
                amt = Decimal(subparts[2])
            except InvalidOperation:
                raise OutputValidationError(f"Invalid amount '{subparts[2]}' in reduce action '{item}'.")
            if amt.is_nan() or amt.is_infinite():
                raise OutputValidationError(f"Reduced amount cannot be NaN or infinite in '{item}'.")
            if amt < Decimal("0"):
                raise OutputValidationError(f"Reduced amount cannot be negative, got {amt} in '{item}'.")
            actions.append(("reduce_to", ev_id, amt))

        else:
            raise OutputValidationError(f"Unknown spending change action '{subparts[0]}' in '{item}'.")

    return actions


# ---------------------------------------------------------------------------
# Core Validator
# ---------------------------------------------------------------------------

def validate_decision_result(
    request: Request,
    profile: FinancialProfile,
    ledger: ReconciledLedger,
    payment_options: Sequence[PaymentOption],
    decision: DecisionResult,
) -> ValidatedDecision:
    """Validate a DecisionResult fail-closed against the output contract, baseline facts, and financial safety.

    Returns a ValidatedDecision wrapper if completely valid.
    Raises OutputValidationError with a descriptive message on any contract, baseline, or safety breach.
    """
    # 1. Request ID match
    if decision.request_id != request.request_id:
        raise OutputValidationError(
            f"Decision request_id '{decision.request_id}' does not match request.request_id '{request.request_id}'."
        )

    # 2. Amount safe to pay bounds, finite Decimal check
    _validate_finite_decimal(decision.amount_safe_to_pay, "amount_safe_to_pay")

    if decision.amount_safe_to_pay < Decimal("0"):
        raise OutputValidationError(
            f"amount_safe_to_pay cannot be negative: {decision.amount_safe_to_pay}."
        )
    if decision.amount_safe_to_pay > request.requested_amount:
        raise OutputValidationError(
            f"amount_safe_to_pay ({decision.amount_safe_to_pay}) cannot exceed requested_amount ({request.requested_amount})."
        )

    # 3. Status and Method domain checks
    if decision.affordability_status not in VALID_AFFORDABILITY_STATUSES:
        raise OutputValidationError(f"Invalid affordability_status: '{decision.affordability_status}'.")
    if decision.recommended_payment_method not in VALID_RECOMMENDED_PAYMENT_METHODS:
        raise OutputValidationError(f"Invalid recommended_payment_method: '{decision.recommended_payment_method}'.")

    # 4. Recompute and enforce baseline affordability facts (independent of spending changes)
    expected_amount_safe = compute_amount_safe_to_pay(
        ledger=ledger,
        request_date=request.request_date,
        requested_amount=request.requested_amount,
        minimum_balance_to_keep=profile.minimum_balance_to_keep,
        protected_categories=profile.expense_categories_to_protect,
        forecast_days=90,
    )
    if decision.amount_safe_to_pay != expected_amount_safe:
        raise OutputValidationError(
            f"Decision amount_safe_to_pay ({decision.amount_safe_to_pay}) does not match "
            f"recomputed baseline safe amount ({expected_amount_safe})."
        )

    expected_earliest_date = find_earliest_date_for_full_payment(
        ledger=ledger,
        request_date=request.request_date,
        requested_amount=request.requested_amount,
        minimum_balance_to_keep=profile.minimum_balance_to_keep,
        protected_categories=profile.expense_categories_to_protect,
        forecast_days=90,
    )
    if decision.earliest_date_for_full_payment != expected_earliest_date:
        raise OutputValidationError(
            f"Decision earliest_date_for_full_payment ({decision.earliest_date_for_full_payment}) does not match "
            f"recomputed baseline earliest date ({expected_earliest_date})."
        )

    # 5. Earliest date for full payment bounds
    if decision.earliest_date_for_full_payment is not None:
        if not isinstance(decision.earliest_date_for_full_payment, date):
            raise OutputValidationError(
                f"earliest_date_for_full_payment must be a date object or None, got {type(decision.earliest_date_for_full_payment).__name__}."
            )
        if decision.earliest_date_for_full_payment < request.request_date:
            raise OutputValidationError(
                f"earliest_date_for_full_payment ({decision.earliest_date_for_full_payment}) cannot be before request_date ({request.request_date})."
            )
        max_horizon = request.request_date + timedelta(days=90)
        if decision.earliest_date_for_full_payment > max_horizon:
            raise OutputValidationError(
                f"earliest_date_for_full_payment ({decision.earliest_date_for_full_payment}) cannot exceed 90-day forecast horizon ({max_horizon})."
            )

    # 6. Status/Method allowed pair matrix check
    pair = (decision.affordability_status, decision.recommended_payment_method)
    if pair not in ALLOWED_STATUS_METHOD_PAIRS:
        raise OutputValidationError(
            f"Invalid status/method combination: affordability_status='{decision.affordability_status}' "
            f"is not permitted with recommended_payment_method='{decision.recommended_payment_method}'."
        )

    # 7. Explanation check
    if not decision.decision_explanation or not decision.decision_explanation.strip():
        raise OutputValidationError("decision_explanation must be a non-empty string.")

    # 8. Parse payment plan and spending changes
    parsed_payments = parse_payment_plan(decision.payment_plan)
    parsed_spending_changes = parse_spending_changes(decision.spending_changes_needed)

    methods_considered = profile.payment_methods_user_will_consider or frozenset()
    protected_categories = profile.expense_categories_to_protect or frozenset()
    willing_stop = profile.expense_categories_user_is_willing_to_stop or frozenset()
    willing_reduce = profile.expense_categories_user_is_willing_to_reduce or frozenset()

    # 9. Status / Method / Plan Coherence
    status = decision.affordability_status
    method = decision.recommended_payment_method

    if status == "affordable_now":
        if method != "full_payment":
            raise OutputValidationError(f"Status 'affordable_now' requires method 'full_payment', got '{method}'.")
        if decision.spending_changes_needed != "none":
            raise OutputValidationError("Status 'affordable_now' cannot require spending changes.")
        if decision.earliest_date_for_full_payment != request.request_date:
            raise OutputValidationError(
                f"Status 'affordable_now' requires earliest_date_for_full_payment to equal request_date ({request.request_date}), "
                f"got {decision.earliest_date_for_full_payment}."
            )
        if decision.amount_safe_to_pay != request.requested_amount:
            raise OutputValidationError(
                f"Status 'affordable_now' requires amount_safe_to_pay ({decision.amount_safe_to_pay}) "
                f"to equal requested_amount ({request.requested_amount})."
            )
        expected_plan = f"{request.request_date.isoformat()}:{clean_decimal(request.requested_amount)}"
        if decision.payment_plan != expected_plan:
            raise OutputValidationError(
                f"Status 'affordable_now' payment plan must be '{expected_plan}', got '{decision.payment_plan}'."
            )
        if "full_payment" not in methods_considered:
            raise OutputValidationError("Method 'full_payment' recommended, but user does not consider full_payment.")

    elif method == "full_payment" and status == "affordable_with_plan":
        # Full payment made safe via spending changes
        if decision.spending_changes_needed == "none":
            raise OutputValidationError("Full payment with 'affordable_with_plan' requires spending changes.")
        expected_plan = f"{request.request_date.isoformat()}:{clean_decimal(request.requested_amount)}"
        if decision.payment_plan != expected_plan:
            raise OutputValidationError(
                f"Full payment plan must be '{expected_plan}', got '{decision.payment_plan}'."
            )
        if "full_payment" not in methods_considered:
            raise OutputValidationError("Method 'full_payment' recommended, but user does not consider full_payment.")

    elif method == "partial_payment":
        if status != "affordable_with_plan":
            raise OutputValidationError(f"Method 'partial_payment' requires status 'affordable_with_plan', got '{status}'.")
        if not request.allows_partial_payment:
            raise OutputValidationError("Method 'partial_payment' recommended, but request does not allow partial payment.")
        if "partial_payment" not in methods_considered:
            raise OutputValidationError("Method 'partial_payment' recommended, but user does not consider partial payment.")
        if decision.amount_safe_to_pay <= Decimal("0") or decision.amount_safe_to_pay >= request.requested_amount:
            raise OutputValidationError(
                f"partial_payment requires 0 < amount_safe_to_pay < requested_amount, got {decision.amount_safe_to_pay}."
            )
        if decision.earliest_date_for_full_payment is None:
            raise OutputValidationError("partial_payment requires a populated earliest_date_for_full_payment.")
        if decision.earliest_date_for_full_payment > request.desired_completion_date:
            raise OutputValidationError(
                f"partial_payment second payment ({decision.earliest_date_for_full_payment}) exceeds desired_completion_date ({request.desired_completion_date})."
            )

        if len(parsed_payments) != 2:
            raise OutputValidationError(f"partial_payment plan must have exactly two payments, got {len(parsed_payments)}.")

        p1_date, p1_amt = parsed_payments[0]
        p2_date, p2_amt = parsed_payments[1]

        if p1_date != request.request_date:
            raise OutputValidationError(f"partial_payment first payment date must be request_date ({request.request_date}), got {p1_date}.")
        if p1_amt != decision.amount_safe_to_pay:
            raise OutputValidationError(
                f"partial_payment first payment amount must equal amount_safe_to_pay ({decision.amount_safe_to_pay}), got {p1_amt}."
            )
        if p2_date != decision.earliest_date_for_full_payment:
            raise OutputValidationError(
                f"partial_payment second payment date must equal earliest_date_for_full_payment ({decision.earliest_date_for_full_payment}), got {p2_date}."
            )
        if p2_amt != request.requested_amount - decision.amount_safe_to_pay:
            raise OutputValidationError(
                f"partial_payment second payment amount must equal requested_amount - amount_safe_to_pay "
                f"({request.requested_amount - decision.amount_safe_to_pay}), got {p2_amt}."
            )
        if p1_amt + p2_amt != request.requested_amount:
            raise OutputValidationError(
                f"partial_payment sum must equal requested_amount ({request.requested_amount}), got {p1_amt + p2_amt}."
            )

    elif method == "installments":
        if status != "affordable_with_plan":
            raise OutputValidationError(f"Method 'installments' requires status 'affordable_with_plan', got '{status}'.")
        if "installments" not in methods_considered:
            raise OutputValidationError("Method 'installments' recommended, but user does not consider installments.")
        if profile.max_installment_months is None:
            raise OutputValidationError("Method 'installments' recommended, but user's max_installment_months is None.")

        # Find matching permitted supplied option
        matched_option: Optional[PaymentOption] = None
        for opt in payment_options:
            if opt.payment_method != "installments":
                continue
            if opt.number_of_payments > profile.max_installment_months:
                continue

            freq = opt.payment_frequency_days or 30
            opt_dates = [
                opt.first_payment_date + timedelta(days=i * freq)
                for i in range(opt.number_of_payments)
            ]
            opt_plan_str = "|".join(
                f"{d.isoformat()}:{clean_decimal(opt.payment_amount)}" for d in opt_dates
            )
            if decision.payment_plan == opt_plan_str:
                if opt_dates[-1] > request.desired_completion_date:
                    raise OutputValidationError(
                        f"Installment option completes on {opt_dates[-1]}, exceeding desired_completion_date {request.desired_completion_date}."
                    )
                plan_total = sum(amt for _, amt in parsed_payments)
                if plan_total != opt.total_payable_amount:
                    raise OutputValidationError(
                        f"Installment plan total {plan_total} does not match option total_payable_amount {opt.total_payable_amount}."
                    )
                matched_option = opt
                break

        if matched_option is None:
            raise OutputValidationError(
                f"Installment plan '{decision.payment_plan}' does not match any permitted supplied payment option."
            )

    elif method == "wait":
        if status != "affordable_later":
            raise OutputValidationError(f"Method 'wait' requires status 'affordable_later', got '{status}'.")
        if "full_payment" not in methods_considered:
            raise OutputValidationError("Method 'wait' requires user to consider 'full_payment'.")
        if decision.spending_changes_needed != "none":
            raise OutputValidationError("Method 'wait' cannot require spending changes.")
        if decision.earliest_date_for_full_payment is None:
            raise OutputValidationError("Method 'wait' requires a populated earliest_date_for_full_payment.")
        if decision.earliest_date_for_full_payment <= request.request_date:
            raise OutputValidationError(
                f"Method 'wait' requires earliest_date_for_full_payment to be after request_date ({request.request_date}), "
                f"got {decision.earliest_date_for_full_payment}."
            )
        if decision.earliest_date_for_full_payment > request.desired_completion_date:
            raise OutputValidationError(
                f"Method 'wait' requires earliest_date_for_full_payment ({decision.earliest_date_for_full_payment}) "
                f"to be on or before desired_completion_date ({request.desired_completion_date})."
            )
        expected_plan = f"{decision.earliest_date_for_full_payment.isoformat()}:{clean_decimal(request.requested_amount)}"
        if decision.payment_plan != expected_plan:
            raise OutputValidationError(
                f"Method 'wait' payment plan must be '{expected_plan}', got '{decision.payment_plan}'."
            )

    elif method == "not_recommended":
        if status != "not_affordable":
            raise OutputValidationError(f"Method 'not_recommended' requires status 'not_affordable', got '{status}'.")
        if decision.payment_plan != "none":
            raise OutputValidationError(f"Method 'not_recommended' requires payment_plan 'none', got '{decision.payment_plan}'.")
        if decision.spending_changes_needed != "none":
            raise OutputValidationError(
                f"Method 'not_recommended' requires spending_changes_needed 'none', got '{decision.spending_changes_needed}'."
            )

    # 10. Spending Change Legality & Stream-Scoping Checks
    events_by_id: Dict[str, ReconciledEvent] = {e.event_id: e for e in ledger.all_events}
    stream_changes: List[StreamSpendingChange] = []
    seen_streams: Set[str] = set()

    for action, target_id, new_amount in parsed_spending_changes:
        if target_id not in events_by_id:
            raise OutputValidationError(
                f"Spending change target event_id '{target_id}' does not exist in user's ledger."
            )

        ev = events_by_id[target_id]

        if ev.direction != "debit":
            raise OutputValidationError(f"Spending change target '{target_id}' is not a debit (direction='{ev.direction}').")
        if not ev.is_recurring:
            raise OutputValidationError(f"Spending change target '{target_id}' is not recurring.")
        if ev.flexibility not in ("reducible", "stoppable", "reducible_or_stoppable"):
            raise OutputValidationError(f"Spending change target '{target_id}' is fixed and cannot be changed.")
        if ev.category in protected_categories:
            raise OutputValidationError(f"Spending change target '{target_id}' belongs to protected category '{ev.category}'.")

        if action == "stop":
            if ev.category not in willing_stop:
                raise OutputValidationError(
                    f"User is not willing to stop category '{ev.category}' (event '{target_id}')."
                )
            if ev.flexibility not in ("stoppable", "reducible_or_stoppable"):
                raise OutputValidationError(
                    f"Event '{target_id}' has flexibility '{ev.flexibility}' and cannot be stopped."
                )
        elif action == "reduce_to":
            if ev.category not in willing_reduce:
                raise OutputValidationError(
                    f"User is not willing to reduce category '{ev.category}' (event '{target_id}')."
                )
            if ev.flexibility not in ("reducible", "reducible_or_stoppable"):
                raise OutputValidationError(
                    f"Event '{target_id}' has flexibility '{ev.flexibility}' and cannot be reduced."
                )
            if ev.minimum_allowed_amount is None:
                raise OutputValidationError(
                    f"Event '{target_id}' has no minimum_allowed_amount defined for reduction."
                )
            assert new_amount is not None
            if new_amount < ev.minimum_allowed_amount:
                raise OutputValidationError(
                    f"Reduced amount {new_amount} is below minimum allowed amount {ev.minimum_allowed_amount} for event '{target_id}'."
                )
            if new_amount >= ev.normalized_amount:
                raise OutputValidationError(
                    f"Reduced amount {new_amount} must be less than current amount {ev.normalized_amount} for event '{target_id}'."
                )

        # Unambiguous recurring stream clustering
        cat_evs = [e for e in ledger.all_events if e.category == ev.category]
        stream_id = get_recurring_stream_identifier(ev, cat_evs)
        if stream_id in seen_streams:
            raise OutputValidationError(
                f"Conflicting or duplicate spending changes for recurring stream '{stream_id}' (event '{target_id}')."
            )
        seen_streams.add(stream_id)

        matched_ids = frozenset(
            e.event_id for e in ledger.all_events
            if get_recurring_stream_identifier(e, cat_evs) == stream_id
        )
        stream_changes.append(StreamSpendingChange(
            action=action,  # type: ignore[arg-type]
            target_event_id=target_id,
            category=ev.category,
            new_amount=new_amount,
            matched_event_ids=matched_ids,
        ))

    # 11. Independent Financial Safety Verification via Simulation
    if method != "not_recommended":
        safety = evaluate_schedule_safety(
            ledger=ledger,
            request_date=request.request_date,
            payments=parsed_payments,
            minimum_balance_to_keep=profile.minimum_balance_to_keep,
            protected_categories=protected_categories,
            stream_spending_changes=stream_changes,
            forecast_days=90,
        )
        if not safety.is_safe:
            raise OutputValidationError(
                f"Financial safety check failed: schedule breaches minimum balance "
                f"(lowest balance: {safety.minimum_balance} < minimum required: {profile.minimum_balance_to_keep}) "
                f"on {safety.first_unsafe_date}: {safety.unsafe_reason}."
            )

    return ValidatedDecision(decision=decision, _validated=True)


# ---------------------------------------------------------------------------
# Validation-Gated Deterministic Serializer
# ---------------------------------------------------------------------------

def serialize_decision_row(target: ValidatedDecision) -> List[str]:
    """Serialize a ValidatedDecision into an exact 8-element row in specification column order.

    Raises TypeError if an unvalidated DecisionResult or arbitrary object is passed.
    """
    if not isinstance(target, ValidatedDecision):
        raise TypeError(
            f"Public serializer requires a ValidatedDecision produced by validate_decision_result, "
            f"got {type(target).__name__}. Raw DecisionResult cannot be serialized directly."
        )
    return [
        target.request_id,
        str(clean_decimal(target.amount_safe_to_pay)),
        target.affordability_status,
        target.recommended_payment_method,
        target.payment_plan,
        target.earliest_date_for_full_payment.isoformat() if target.earliest_date_for_full_payment else "",
        target.spending_changes_needed,
        target.decision_explanation,
    ]


def serialize_decision_csv_line(target: ValidatedDecision) -> str:
    """Serialize a single ValidatedDecision into a valid RFC-4180 CSV line with LF newline.

    Raises TypeError if an unvalidated DecisionResult or arbitrary object is passed.
    """
    if not isinstance(target, ValidatedDecision):
        raise TypeError(
            f"Public serializer requires a ValidatedDecision produced by validate_decision_result, "
            f"got {type(target).__name__}. Raw DecisionResult cannot be serialized directly."
        )
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(serialize_decision_row(target))
    return output.getvalue()


def serialize_decisions_to_csv(
    decisions: Sequence[ValidatedDecision],
    output_path: Optional[str] = None,
) -> str:
    """Serialize a sequence of ValidatedDecision objects into full CSV content with header.

    If output_path is provided, writes the content to that path.
    Returns the serialized CSV text string.
    Raises TypeError if any item is not a ValidatedDecision.
    """
    for i, item in enumerate(decisions):
        if not isinstance(item, ValidatedDecision):
            raise TypeError(
                f"Public serializer requires ValidatedDecision objects produced by validate_decision_result, "
                f"got {type(item).__name__} at index {i}. Raw DecisionResult cannot be serialized directly."
            )

    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(OUTPUT_COLUMNS)

    for decision in decisions:
        writer.writerow(serialize_decision_row(decision))

    content = output.getvalue()

    if output_path is not None:
        with open(output_path, "w", encoding="utf-8", newline="") as f:
            f.write(content)

    return content
