"""
Reconciliation Engine for Buy or Wait? (Phase 3).

Reconstructs a canonical, deduped, currency-normalized cash ledger per user from:
  1. Base financial_events.csv rows (via loaders.py/models.py)
  2. Financial profiles (financial_profiles.csv)
  3. Fixed dated exchange rates (exchange_rates.csv via currency.py)
  4. Extracted evidence (code/data/extracted_deltas.json from Phase 2):
     - Unconditional image extractions for the 16 blank-amount events
     - Structured message deltas with priority conflict resolution

Splits each ledger into:
  - recurring_events vs. one_time_events
  - flexible_events vs. fixed_or_protected_events
  - known unquantifiable commitments (e.g. unspecified childcare payments)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Dict, List, Literal, Optional, Set, Tuple

from .currency import CurrencyConverter, ExchangeRateMissing
from .loaders import DataStore
from .models import FinancialEvent, FinancialProfile, Message

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Output Data Structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class UnquantifiableCommitment:
    """An unquantifiable financial fact (new_value is None, action='new_fact').

    Cannot be included numerically in balance simulations, but tracked per user
    so decision explanation generation can surface it as an explicit caveat.
    """
    user_id: str
    message_id: str
    category: str
    effective_date: Optional[date]
    description: str
    confidence: str


@dataclass(frozen=True)
class ReconciledEvent:
    """A canonical, deduped, currency-normalized cash ledger event."""
    event_id: str
    user_id: str
    event_type: str
    description: str
    category: str
    direction: Literal["credit", "debit"]
    original_amount: Decimal
    original_currency: str
    normalized_amount: Decimal  # Converted to user's home_currency
    home_currency: str
    event_date: date
    settlement_date: date       # Cash movement date
    status: Literal["settled", "pending", "scheduled"]
    flexibility: Literal["fixed", "reducible", "stoppable", "reducible_or_stoppable"]
    minimum_allowed_amount: Optional[Decimal]  # Converted to home_currency
    is_recurring: bool
    is_protected: bool
    is_reducible: bool
    is_stoppable: bool
    linked_event_id: Optional[str] = None
    source_delta_id: Optional[str] = None


@dataclass(frozen=True)
class DeltaCategoryMatch:
    """Result of matching a message delta to a user's ledger category/events."""
    matched: bool
    category: Optional[str]
    event_type: Optional[str]
    target_event_id: Optional[str]
    match_confidence: Literal["high", "low", "none"]
    match_reason: str
    is_unquantifiable: bool = False


@dataclass(frozen=True)
class ReconciledLedger:
    """Complete reconciled financial position for a single user."""
    user_id: str
    home_currency: str
    current_available_balance: Decimal
    minimum_balance_to_keep: Decimal
    all_events: List[ReconciledEvent]
    recurring_events: List[ReconciledEvent]
    one_time_events: List[ReconciledEvent]
    flexible_events: List[ReconciledEvent]
    fixed_or_protected_events: List[ReconciledEvent]
    unquantifiable_commitments: List[UnquantifiableCommitment]
    recurring_salary_amount: Optional[Decimal]
    salary_cancelled: bool
    matched_deltas_count: int
    unmatched_deltas_count: int


# ---------------------------------------------------------------------------
# Core Recurrence Detection
# ---------------------------------------------------------------------------

def is_recurring_event(event: FinancialEvent, user_events: List[FinancialEvent]) -> bool:
    """Classify an event as recurring or one-time based on rules and history.

    Rules:
      - Subscriptions and debt payments are recurring commitments.
      - Regular rent and regular salary are recurring.
      - One-time salary adjustments/arrears/bonuses are one-time.
      - Daily variable spending (groceries, dining, transport) is treated as
        one-time transactions (forecasted in simulation).
      - Category cadence check for utilities, insurance, education, etc.
    """
    if event.event_type in ("subscription", "debt_payment"):
        return True
    if event.category == "rent":
        return True
    if event.category == "salary":
        desc = event.description.lower()
        if any(k in desc for k in ("arrear", "bonus", "one-time", "adjustment")):
            return False
        return True

    # Variable spending / one-off events are not recurring fixed commitments
    if event.category in ("groceries", "transport", "dining", "shopping", "windfall", "work_expense", "investment"):
        return False

    # Check for regular cadence in utility/insurance/education/healthcare/family_support
    same_cat = [
        e for e in user_events
        if e.category == event.category and e.direction == event.direction and e.status in ("settled", "scheduled")
    ]
    if len(same_cat) >= 3:
        dates = sorted(e.event_date for e in same_cat)
        diffs = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
        if diffs:
            avg_diff = sum(diffs) / len(diffs)
            if 20 <= avg_diff <= 40:
                return True

    return False


# ---------------------------------------------------------------------------
# Image Extractions: Fill Blank Amounts Unconditionally
# ---------------------------------------------------------------------------

def fill_blank_amounts(
    events: List[FinancialEvent],
    image_extractions: List[dict],
) -> List[FinancialEvent]:
    """Fill blank amount events using extracted image values.

    This is unconditional: these are missing values, not competing facts.
    Raises ValueError if an event with amount=None lacks an image extraction.
    """
    img_map = {x["event_id"]: x for x in image_extractions}
    filled: List[FinancialEvent] = []

    for e in events:
        if e.amount is None:
            if e.event_id not in img_map:
                raise ValueError(
                    f"Event {e.event_id} has blank amount but no matching image extraction."
                )
            extracted = img_map[e.event_id]
            extracted_amt = Decimal(str(extracted["extracted_amount"]))
            filled_event = FinancialEvent(
                event_id=e.event_id,
                user_id=e.user_id,
                event_type=e.event_type,
                description=e.description,
                category=e.category,
                direction=e.direction,
                amount=extracted_amt,
                currency=e.currency,
                event_date=e.event_date,
                settlement_date=e.settlement_date,
                status=e.status,
                linked_event_id=e.linked_event_id,
                flexibility=e.flexibility,
                minimum_allowed_amount=e.minimum_allowed_amount,
            )
            filled.append(filled_event)
        else:
            filled.append(e)

    return filled


# ---------------------------------------------------------------------------
# Message Delta Matching (Target: user_general)
# ---------------------------------------------------------------------------

def match_general_delta_to_category(
    delta: dict,
    message: Optional[Message],
    user_events: List[FinancialEvent],
) -> DeltaCategoryMatch:
    """Decide which ledger category/event a user_general fact applies to.

    Matches by user_id + category / event_type + recency — NOT fuzzy text matching.
    Logs and flags any delta that cannot be confidently matched.
    """
    action = delta.get("action")
    if action == "confirm_no_change":
        return DeltaCategoryMatch(
            matched=True,
            category=None,
            event_type=None,
            target_event_id=None,
            match_confidence="high",
            match_reason="confirm_no_change signals no ledger modification needed",
        )

    target = delta.get("target")
    # 1. Explicit target event ID
    if target and target != "user_general":
        matched_ev = next((e for e in user_events if e.event_id == target), None)
        return DeltaCategoryMatch(
            matched=True,
            category=matched_ev.category if matched_ev else None,
            event_type=matched_ev.event_type if matched_ev else None,
            target_event_id=target,
            match_confidence="high",
            match_reason=f"Explicit target event_id: {target}",
        )

    reason = delta.get("reasoning", "").lower()
    txt = message.message_text.lower() if message else ""
    src = message.source_type if message else ""

    # 2. Childcare commitments (unquantifiable facts)
    if "childcare" in reason:
        childcare_evs = [e for e in user_events if e.category == "childcare"]
        most_recent = max(childcare_evs, key=lambda e: e.event_date) if childcare_evs else None
        return DeltaCategoryMatch(
            matched=True,
            category="childcare",
            event_type="expense",
            target_event_id=most_recent.event_id if most_recent else None,
            match_confidence="high",
            match_reason="Childcare commitment matched by reasoning",
            is_unquantifiable=(delta.get("new_value") is None and action == "new_fact"),
        )

    # 3. Salary arrears adjustments
    if "arrears" in reason:
        salary_evs = [e for e in user_events if e.category == "salary"]
        most_recent = max(salary_evs, key=lambda e: e.event_date) if salary_evs else None
        return DeltaCategoryMatch(
            matched=True,
            category="salary",
            event_type="income",
            target_event_id=most_recent.event_id if most_recent else None,
            match_confidence="high",
            match_reason="Salary arrears matched to user's salary income",
        )

    # 4. Rent / Lease changes
    if any(k in reason for k in ["rent", "lease", "sewa"]) or any(
        k in txt for k in ["rent", "lease", "sewa", "stayledger", "rentnest", "renttrack", "homeportal"]
    ):
        rent_evs = [e for e in user_events if e.category == "rent"]
        most_recent = max(rent_evs, key=lambda e: e.event_date) if rent_evs else None
        return DeltaCategoryMatch(
            matched=True,
            category="rent",
            event_type="expense",
            target_event_id=most_recent.event_id if most_recent else None,
            match_confidence="high",
            match_reason="Rent/lease delta matched to recurring rent events",
        )

    # 5. Service provider client invoice income
    if any(k in reason for k in ["invoice", "faktur"]) or any(
        k in txt for k in ["invoice", "invoiceflow", "faktur", "clientdesk", "freelancehub"]
    ):
        income_evs = [
            e for e in user_events
            if e.event_type == "income" or "invoice" in e.description.lower() or "client" in e.description.lower()
        ]
        most_recent = max(income_evs, key=lambda e: e.event_date) if income_evs else None
        return DeltaCategoryMatch(
            matched=True,
            category="income",
            event_type="income",
            target_event_id=most_recent.event_id if most_recent else None,
            match_confidence="high",
            match_reason="Client invoice income matched to freelance/consulting income",
        )

    # 6. Employer payroll / salary
    if src == "employer" or any(k in reason for k in ["salary", "gaji", "pay", "payroll"]) or any(
        k in txt for k in ["salary", "gaji", "penggajian", "payroll"]
    ):
        salary_evs = [e for e in user_events if e.category == "salary"]
        most_recent = max(salary_evs, key=lambda e: e.event_date) if salary_evs else None
        return DeltaCategoryMatch(
            matched=True,
            category="salary",
            event_type="income",
            target_event_id=most_recent.event_id if most_recent else None,
            match_confidence="high",
            match_reason="Employer payroll delta matched to recurring salary events",
        )

    # Unmatched fallback
    logger.warning("Unmatched user_general delta: %s", delta)
    return DeltaCategoryMatch(
        matched=False,
        category=None,
        event_type=None,
        target_event_id=None,
        match_confidence="none",
        match_reason="Could not confidently match delta to any known category or event",
    )


# ---------------------------------------------------------------------------
# Per-User Reconciliation Engine
# ---------------------------------------------------------------------------

def reconcile_user_ledger(
    user_id: str,
    ds: DataStore,
    extracted_data: dict,
    converter: Optional[CurrencyConverter] = None,
) -> ReconciledLedger:
    """Reconcile the financial ledger for a single user.

    Applies:
      1. Image amount backfills for blank amounts
      2. Cash-state rules (pending debit reservation, pending credit exclusion, non-cash/unrealized exclusion)
      3. Message deltas conflict-priority resolution & deduplication
      4. Exact currency normalization to home_currency via CurrencyConverter
      5. Separation into recurring vs one-time and flexible vs fixed/protected
    """
    if converter is None:
        converter = CurrencyConverter(ds.exchange_rates)

    profile = ds.get_profile(user_id)
    raw_events = ds.get_events_for_user(user_id)
    image_extractions = extracted_data.get("image_extractions", [])
    all_deltas = extracted_data.get("message_deltas", [])
    user_deltas = [d for d in all_deltas if d.get("user_id") == user_id]
    msg_map = {m.message_id: m for m in ds.messages}

    # Step 1: Fill blank amounts from image extractions unconditionally
    filled_events = fill_blank_amounts(raw_events, image_extractions)

    # Step 2: Cash-state rules
    # - Reserve pending debits
    # - Ignore pending credits/bonuses/refunds until settled
    # - direction=non_cash and status=unrealized never touch balance
    # - status in ("failed", "cancelled") never touch balance
    # - linked_event_id chains a lifecycle but does NOT by itself decide cash-flow inclusion
    valid_cash_events: List[FinancialEvent] = []
    for e in filled_events:
        if e.direction == "non_cash" or e.status in ("unrealized", "failed", "cancelled"):
            continue
        if e.status == "pending" and e.direction == "credit":
            continue
        valid_cash_events.append(e)

    # Step 3: Message deltas conflict resolution
    unquantifiable_commitments: List[UnquantifiableCommitment] = []
    salary_cancelled = False
    recurring_salary_amount: Optional[Decimal] = None
    matched_deltas_count = 0
    unmatched_deltas_count = 0
    new_events_to_add: List[FinancialEvent] = []

    for delta in user_deltas:
        action = delta.get("action")
        if action == "confirm_no_change":
            matched_deltas_count += 1
            continue

        msg = msg_map.get(delta.get("message_id"))
        match_res = match_general_delta_to_category(delta, msg, filled_events)

        if not match_res.matched:
            unmatched_deltas_count += 1
            continue
        matched_deltas_count += 1

        # Unquantifiable commitment (new_value is None, action='new_fact')
        if match_res.is_unquantifiable:
            eff_d = date.fromisoformat(delta["effective_date"]) if delta.get("effective_date") else None
            unquantifiable_commitments.append(
                UnquantifiableCommitment(
                    user_id=user_id,
                    message_id=delta["message_id"],
                    category=match_res.category or "unspecified",
                    effective_date=eff_d,
                    description=delta.get("reasoning", ""),
                    confidence=delta.get("confidence", "low"),
                )
            )
            continue

        # Cancellation (e.g. employment ended)
        if action == "cancel":
            if match_res.category == "salary":
                salary_cancelled = True
                eff_d = date.fromisoformat(delta["effective_date"]) if delta.get("effective_date") else None
                # Filter out scheduled salary events on or after effective_date
                filtered = []
                for ev in valid_cash_events:
                    if ev.category == "salary" and ev.status == "scheduled":
                        if eff_d is None or ev.event_date >= eff_d:
                            continue
                    filtered.append(ev)
                valid_cash_events = filtered
            continue

        # Date amendment
        if action == "amend" and delta.get("field") == "date":
            if delta.get("effective_date"):
                eff_d = date.fromisoformat(delta["effective_date"])
                if match_res.category == "salary":
                    updated = []
                    for ev in valid_cash_events:
                        if ev.category == "salary" and ev.status == "scheduled":
                            ev = FinancialEvent(
                                event_id=ev.event_id,
                                user_id=ev.user_id,
                                event_type=ev.event_type,
                                description=ev.description,
                                category=ev.category,
                                direction=ev.direction,
                                amount=ev.amount,
                                currency=ev.currency,
                                event_date=eff_d,
                                settlement_date=eff_d,
                                status=ev.status,
                                linked_event_id=ev.linked_event_id,
                                flexibility=ev.flexibility,
                                minimum_allowed_amount=ev.minimum_allowed_amount,
                            )
                        updated.append(ev)
                    valid_cash_events = updated
            continue

        # Recurring value or amount amendment
        if action == "amend" and delta.get("field") in ("recurring_value", "amount"):
            val_str = delta.get("new_value")
            if val_str is not None:
                new_val = Decimal(str(val_str))
                eff_d = date.fromisoformat(delta["effective_date"]) if delta.get("effective_date") else None

                if match_res.category == "salary":
                    recurring_salary_amount = new_val
                    updated = []
                    for ev in valid_cash_events:
                        if ev.category == "salary" and ev.status == "scheduled":
                            if eff_d is None or ev.event_date >= eff_d:
                                ev = FinancialEvent(
                                    event_id=ev.event_id,
                                    user_id=ev.user_id,
                                    event_type=ev.event_type,
                                    description=ev.description,
                                    category=ev.category,
                                    direction=ev.direction,
                                    amount=new_val,
                                    currency=ev.currency,
                                    event_date=eff_d or ev.event_date,
                                    settlement_date=eff_d or ev.settlement_date,
                                    status=ev.status,
                                    linked_event_id=ev.linked_event_id,
                                    flexibility=ev.flexibility,
                                    minimum_allowed_amount=ev.minimum_allowed_amount,
                                )
                        updated.append(ev)
                    valid_cash_events = updated

                elif match_res.category == "rent":
                    updated = []
                    for ev in valid_cash_events:
                        if ev.category == "rent" and ev.status == "scheduled":
                            if eff_d is None or ev.event_date >= eff_d:
                                ev = FinancialEvent(
                                    event_id=ev.event_id,
                                    user_id=ev.user_id,
                                    event_type=ev.event_type,
                                    description=ev.description,
                                    category=ev.category,
                                    direction=ev.direction,
                                    amount=new_val,
                                    currency=ev.currency,
                                    event_date=eff_d or ev.event_date,
                                    settlement_date=eff_d or ev.settlement_date,
                                    status=ev.status,
                                    linked_event_id=ev.linked_event_id,
                                    flexibility=ev.flexibility,
                                    minimum_allowed_amount=ev.minimum_allowed_amount,
                                )
                        updated.append(ev)
                    valid_cash_events = updated
            continue

        # New fact (e.g. approved invoice payment, arrears)
        if action == "new_fact" and delta.get("field") in ("amount", "recurring_value"):
            val_str = delta.get("new_value")
            if val_str is not None:
                new_val = Decimal(str(val_str))
                eff_d = (
                    date.fromisoformat(delta["effective_date"])
                    if delta.get("effective_date")
                    else (msg.sent_at.date() if msg else None)
                )

                # Deduplication check: does a settled event already describe this fact?
                already_exists = False
                for ev in valid_cash_events:
                    if ev.amount == new_val:
                        if match_res.category == "salary" and (
                            "arrear" in ev.description.lower() or ev.category == "salary"
                        ):
                            already_exists = True
                            break
                        elif match_res.category == "income" and (
                            "invoice" in ev.description.lower() or "client" in ev.description.lower()
                        ):
                            if ev.event_date == eff_d:
                                already_exists = True
                                break

                if not already_exists and eff_d:
                    new_ev = FinancialEvent(
                        event_id=f"delta_{delta['message_id']}",
                        user_id=user_id,
                        event_type=match_res.event_type or "income",
                        description=delta.get("reasoning", "Confirmed message delta"),
                        category=match_res.category or "income",
                        direction="credit",
                        amount=new_val,
                        currency=profile.home_currency,
                        event_date=eff_d,
                        settlement_date=eff_d,
                        status="settled",
                        linked_event_id=None,
                        flexibility="fixed",
                        minimum_allowed_amount=None,
                    )
                    new_events_to_add.append(new_ev)

    valid_cash_events.extend(new_events_to_add)

    # Step 4: Currency Normalization
    reconciled_events: List[ReconciledEvent] = []
    for e in valid_cash_events:
        settlement_d = e.settlement_date or e.event_date
        # Normalization raises ExchangeRateMissing loudly if rate is missing
        norm_amt = converter.convert(e.amount, e.currency, profile.home_currency, settlement_d)
        norm_min_amt = None
        if e.minimum_allowed_amount is not None:
            norm_min_amt = converter.convert(
                e.minimum_allowed_amount, e.currency, profile.home_currency, settlement_d
            )

        is_rec = is_recurring_event(e, valid_cash_events)

        # Spending change flexibility rules
        is_prot = (e.category in profile.expense_categories_to_protect) or (e.flexibility == "fixed")
        is_stop = (
            is_rec
            and e.direction == "debit"
            and (e.category in profile.expense_categories_user_is_willing_to_stop)
            and (e.category not in profile.expense_categories_to_protect)
            and (e.flexibility in ("stoppable", "reducible_or_stoppable"))
        )
        is_red = (
            is_rec
            and e.direction == "debit"
            and (e.category in profile.expense_categories_user_is_willing_to_reduce)
            and (e.category not in profile.expense_categories_to_protect)
            and (e.flexibility in ("reducible", "reducible_or_stoppable"))
        )

        rec_ev = ReconciledEvent(
            event_id=e.event_id,
            user_id=e.user_id,
            event_type=e.event_type,
            description=e.description,
            category=e.category,
            direction=e.direction,
            original_amount=e.amount,
            original_currency=e.currency,
            normalized_amount=norm_amt,
            home_currency=profile.home_currency,
            event_date=e.event_date,
            settlement_date=settlement_d,
            status=e.status,
            flexibility=e.flexibility,
            minimum_allowed_amount=norm_min_amt,
            is_recurring=is_rec,
            is_protected=is_prot,
            is_reducible=is_red,
            is_stoppable=is_stop,
            linked_event_id=e.linked_event_id,
            source_delta_id=e.event_id if e.event_id.startswith("delta_") else None,
        )
        reconciled_events.append(rec_ev)

    # Sort all events chronologically by settlement_date, then event_id
    reconciled_events.sort(key=lambda x: (x.settlement_date, x.event_id))

    # Split into recurring vs one-time, flexible vs fixed/protected
    recurring_events = [e for e in reconciled_events if e.is_recurring]
    one_time_events = [e for e in reconciled_events if not e.is_recurring]
    flexible_events = [e for e in reconciled_events if e.is_stoppable or e.is_reducible]
    fixed_or_protected_events = [e for e in reconciled_events if not (e.is_stoppable or e.is_reducible)]

    return ReconciledLedger(
        user_id=user_id,
        home_currency=profile.home_currency,
        current_available_balance=profile.current_available_balance,
        minimum_balance_to_keep=profile.minimum_balance_to_keep,
        all_events=reconciled_events,
        recurring_events=recurring_events,
        one_time_events=one_time_events,
        flexible_events=flexible_events,
        fixed_or_protected_events=fixed_or_protected_events,
        unquantifiable_commitments=unquantifiable_commitments,
        recurring_salary_amount=recurring_salary_amount,
        salary_cancelled=salary_cancelled,
        matched_deltas_count=matched_deltas_count,
        unmatched_deltas_count=unmatched_deltas_count,
    )


# ---------------------------------------------------------------------------
# All-Users Reconciliation
# ---------------------------------------------------------------------------

def reconcile_all_ledgers(
    ds: DataStore,
    extracted_data: dict,
    converter: Optional[CurrencyConverter] = None,
) -> Dict[str, ReconciledLedger]:
    """Reconcile financial ledgers for all users in the dataset."""
    if converter is None:
        converter = CurrencyConverter(ds.exchange_rates)

    ledgers: Dict[str, ReconciledLedger] = {}
    for profile in ds.profiles:
        ledgers[profile.user_id] = reconcile_user_ledger(
            profile.user_id, ds, extracted_data, converter
        )
    return ledgers
