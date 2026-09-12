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

import calendar
from collections import Counter
import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta
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
    is_user_general: bool = False


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
    user_general_matched_count: int  # active user_general deltas matched
    matched_deltas_count: int        # total active deltas matched (user_general + specific)
    unmatched_deltas_count: int
    no_change_deltas_count: int = 0  # confirm_no_change deltas
    protected_categories: Optional[frozenset[str]] = None


# ---------------------------------------------------------------------------
# Core Recurrence Detection & Date Helpers
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
        desc = event.description.lower()
        if any(k in desc for k in ("outstanding", "arrear", "deposit", "balance", "one-time")):
            return False
        return True
    if event.category == "salary":
        desc = event.description.lower()
        if any(k in desc for k in ("arrear", "bonus", "one-time", "adjustment")):
            return False
        return True

    # Fixed variable spending / one-off events are not recurring commitments
    if event.flexibility == "fixed" and event.category in ("groceries", "transport", "dining", "shopping", "windfall", "work_expense", "investment"):
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


def infer_salary_payday(events: List[FinancialEvent]) -> int:
    """Prefer an explicit future settlement, then a repeated historical cadence.

    A single receipt or delayed payment does not establish a new recurring payday.
    Explicit date amendments are handled by the caller before this inference.
    """
    regular = [e for e in events if e.category == "salary" and e.direction == "credit"
               and e.status in ("settled", "scheduled")
               and not any(k in e.description.lower() for k in
                           ("arrear", "bonus", "commission", "one-time", "adjustment", "final", "severance"))]
    scheduled = [e for e in regular if e.status == "scheduled"]
    if scheduled:
        first = min(scheduled, key=lambda e: e.settlement_date or e.event_date)
        return (first.settlement_date or first.event_date).day
    if not regular:
        return 15
    dates = sorted({e.settlement_date or e.event_date for e in regular})
    counts = Counter(d.day for d in dates)
    day, count = max(counts.items(), key=lambda item: (item[1], item[0]))
    if count >= 3 and count > len(dates) / 2:
        return day
    return dates[-1].day


def _get_monthly_dates(start_date: date, end_date: date, day_of_month: int) -> List[date]:
    """Generate calendar dates with day=day_of_month falling within [start_date, end_date]."""
    dates: List[date] = []
    cur_year = start_date.year
    cur_month = start_date.month

    while (cur_year, cur_month) <= (end_date.year, end_date.month):
        max_day = calendar.monthrange(cur_year, cur_month)[1]
        actual_day = min(day_of_month, max_day)
        candidate = date(cur_year, cur_month, actual_day)
        if start_date <= candidate <= end_date:
            dates.append(candidate)
        if cur_month == 12:
            cur_year += 1
            cur_month = 1
        else:
            cur_month += 1

    return dates


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
    Uses word boundaries to prevent substring collisions (e.g. 'current' != 'rent').
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
            is_user_general=(delta.get("target") == "user_general"),
        )

    target = delta.get("target")
    is_ug = (target == "user_general")

    # 1. Explicit target event ID
    if target and target != "user_general":
        matched_ev = next((e for e in user_events if e.event_id == target), None)
        return DeltaCategoryMatch(
            matched=True,
            category=matched_ev.category if matched_ev else "housing",
            event_type=matched_ev.event_type if matched_ev else "expense",
            target_event_id=target,
            match_confidence="high",
            match_reason=f"Explicit target event_id: {target}",
            is_user_general=False,
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
            is_user_general=True,
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
            is_user_general=True,
        )

    # 4. Rent / Lease changes (Word-bounded matching to avoid 'current' matching 'rent')
    rent_providers = ["stayledger", "rentnest", "renttrack", "homeportal"]
    rent_keywords = [r"\brent\b", r"\blease\b", r"\bsewa\b", r"\bperpanjangan sewa\b"]
    if any(p in txt for p in rent_providers) or any(re.search(k, reason) for k in rent_keywords):
        rent_evs = [e for e in user_events if e.category == "rent"]
        most_recent = max(rent_evs, key=lambda e: e.event_date) if rent_evs else None
        return DeltaCategoryMatch(
            matched=True,
            category="rent",
            event_type="expense",
            target_event_id=most_recent.event_id if most_recent else None,
            match_confidence="high",
            match_reason="Rent/lease delta matched to recurring rent events",
            is_user_general=True,
        )

    # 5. Service provider client invoice income
    invoice_providers = ["invoiceflow", "clientdesk", "freelancehub"]
    invoice_keywords = [r"\binvoice\b", r"\bfaktur\b", r"\bclient\b"]
    if (
        any(p in txt for p in invoice_providers)
        or any(re.search(k, reason) for k in invoice_keywords)
        or (src == "service_provider" and any(re.search(k, txt) for k in invoice_keywords))
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
            is_user_general=True,
        )

    # 6. Employer payroll / salary (Word-bounded keywords)
    salary_keywords = [
        r"\bsalary\b", r"\bgaji\b", r"\bpayroll\b", r"\bpenggajian\b",
        r"\bpay\b", r"\bpendapatan\b", r"\bunpaid leave\b", r"\bcontract\b"
    ]
    if (
        src == "employer"
        or any(re.search(k, reason) for k in salary_keywords)
        or any(re.search(k, txt) for k in salary_keywords)
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
            is_user_general=True,
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
        is_user_general=is_ug,
    )


# ---------------------------------------------------------------------------
# Per-User Reconciliation Engine
# ---------------------------------------------------------------------------

def reconcile_user_ledger(
    user_id: str,
    ds: DataStore,
    extracted_data: dict,
    converter: Optional[CurrencyConverter] = None,
    forecast_days: int = 90,
) -> ReconciledLedger:
    """Reconcile the financial ledger for a single user.

    Applies:
      1. Image amount backfills for blank amounts
      2. Cash-state rules (pending debit reservation, pending credit exclusion, non-cash/unrealized exclusion)
      3. Message deltas conflict-priority resolution & deduplication
      4. Preservation of scheduled salary dates when updating amounts
      5. Deterministic recurring-stream date shift for payday amendments
      6. Synthesized confirmed recurring salary stream for active salary users
      7. Exact currency normalization to home_currency via CurrencyConverter
      8. Separation into recurring vs one-time and flexible vs fixed/protected
    """
    if converter is None:
        converter = CurrencyConverter(ds.exchange_rates)

    profile = ds.get_profile(user_id)
    raw_events = ds.get_events_for_user(user_id)
    image_extractions = extracted_data.get("image_extractions", [])
    all_deltas = extracted_data.get("message_deltas", [])
    msg_map = {m.message_id: m for m in ds.messages}

    # Match all user deltas, resolving user_id from message metadata if omitted (e.g. confirm_no_change)
    user_deltas = []
    for d in all_deltas:
        uid = d.get("user_id")
        if not uid and d.get("message_id") in msg_map:
            uid = msg_map[d["message_id"]].user_id
        if uid == user_id:
            user_deltas.append(d)

    # Find user's evaluation request date
    request_date: Optional[date] = None
    if ds is not None:
        for r in ds.requests:
            if r.user_id == user_id:
                request_date = r.request_date
                break
        if request_date is None:
            for r in ds.sample_requests:
                if r.user_id == user_id:
                    request_date = r.request_date
                    break

    # Step 1: Fill blank amounts from image extractions unconditionally
    filled_events = fill_blank_amounts(raw_events, image_extractions)

    # Step 2: Cash-state rules
    # - Reserve pending debits
    # - Ignore pending credits/bonuses/refunds until settled
    # - direction=non_cash and status=unrealized never touch balance
    # - status in ("failed", "cancelled") never touch balance
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
    next_salary_date: Optional[date] = None

    # Counters: separate active counters from confirm_no_change
    matched_deltas_count = 0        # Total active deltas matched
    user_general_matched_count = 0  # Active user_general deltas matched
    unmatched_deltas_count = 0
    no_change_deltas_count = 0      # confirm_no_change deltas

    new_events_to_add: List[FinancialEvent] = []

    for delta in user_deltas:
        action = delta.get("action")
        if action == "confirm_no_change":
            # Explicit rule: confirm_no_change deltas MUST NOT affect active counters
            no_change_deltas_count += 1
            continue

        msg = msg_map.get(delta.get("message_id"))
        match_res = match_general_delta_to_category(delta, msg, filled_events)

        if not match_res.matched:
            unmatched_deltas_count += 1
            continue

        matched_deltas_count += 1
        if match_res.is_user_general:
            user_general_matched_count += 1

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

        # Date amendment (Preserves scheduled event amount, shifts dates with preserved monthly cadence)
        if action == "amend" and delta.get("field") == "date":
            if delta.get("effective_date"):
                eff_d = date.fromisoformat(delta["effective_date"])
                if match_res.category == "salary":
                    next_salary_date = eff_d
                    new_payday = eff_d.day

                    # Deterministic recurring-stream date shift:
                    # Preserves monthly cadence and maps each affected payment to its correct future occurrence
                    updated = []
                    seen_dates: Set[date] = set()

                    for ev in valid_cash_events:
                        if ev.category == "salary" and ev.direction == "credit" and ev.status == "scheduled":
                            orig_d = ev.event_date
                            if (orig_d.year, orig_d.month) >= (eff_d.year, eff_d.month):
                                max_d = calendar.monthrange(orig_d.year, orig_d.month)[1]
                                shifted_d = date(orig_d.year, orig_d.month, min(new_payday, max_d))
                                while shifted_d in seen_dates:
                                    if shifted_d.month == 12:
                                        y, m = shifted_d.year + 1, 1
                                    else:
                                        y, m = shifted_d.year, shifted_d.month + 1
                                    shifted_d = date(y, m, min(new_payday, calendar.monthrange(y, m)[1]))
                                seen_dates.add(shifted_d)
                                ev = FinancialEvent(
                                    event_id=ev.event_id,
                                    user_id=ev.user_id,
                                    event_type=ev.event_type,
                                    description=ev.description,
                                    category=ev.category,
                                    direction=ev.direction,
                                    amount=ev.amount,
                                    currency=ev.currency,
                                    event_date=shifted_d,
                                    settlement_date=shifted_d,
                                    status=ev.status,
                                    linked_event_id=ev.linked_event_id,
                                    flexibility=ev.flexibility,
                                    minimum_allowed_amount=ev.minimum_allowed_amount,
                                )
                            else:
                                seen_dates.add(ev.event_date)
                        updated.append(ev)
                    valid_cash_events = updated
            continue

        # Recurring value or amount amendment:
        # PRESERVES individual event_date and settlement_date when updating amounts!
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
                                # Update ONLY amount; preserve individual event_date and settlement_date
                                ev = FinancialEvent(
                                    event_id=ev.event_id,
                                    user_id=ev.user_id,
                                    event_type=ev.event_type,
                                    description=ev.description,
                                    category=ev.category,
                                    direction=ev.direction,
                                    amount=new_val,
                                    currency=ev.currency,
                                    event_date=ev.event_date,          # PRESERVED
                                    settlement_date=ev.settlement_date,  # PRESERVED
                                    status=ev.status,
                                    linked_event_id=ev.linked_event_id,
                                    flexibility=ev.flexibility,
                                    minimum_allowed_amount=ev.minimum_allowed_amount,
                                )
                        updated.append(ev)
                    valid_cash_events = updated

                elif match_res.category == "rent":
                    img_event_ids = {x["event_id"] for x in image_extractions}
                    updated = []
                    for ev in valid_cash_events:
                        if (
                            ev.category == "rent"
                            and ev.status == "scheduled"
                            and ev.event_id not in img_event_ids
                            and not any(k in ev.description.lower() for k in ("outstanding", "arrear", "deposit", "balance"))
                        ):
                            if eff_d is None or ev.event_date >= eff_d:
                                # Update ONLY amount; preserve individual event_date and settlement_date
                                ev = FinancialEvent(
                                    event_id=ev.event_id,
                                    user_id=ev.user_id,
                                    event_type=ev.event_type,
                                    description=ev.description,
                                    category=ev.category,
                                    direction=ev.direction,
                                    amount=new_val,
                                    currency=ev.currency,
                                    event_date=ev.event_date,          # PRESERVED
                                    settlement_date=ev.settlement_date,  # PRESERVED
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

                if match_res.category == "salary" and delta.get("field") == "recurring_value":
                    recurring_salary_amount = new_val

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
                        status="scheduled" if (request_date and eff_d >= request_date) else "settled",
                        linked_event_id=None,
                        flexibility="fixed",
                        minimum_allowed_amount=None,
                    )
                    new_events_to_add.append(new_ev)

    valid_cash_events.extend(new_events_to_add)

    # Step 4: Synthesize Confirmed Recurring Salary Stream
    # Ensures salary amendments and active recurring salaries are concrete cash events on the ledger
    if not salary_cancelled and request_date is not None:
        all_sal_credits = [
            e for e in valid_cash_events
            if e.category == "salary" and e.direction == "credit"
        ]
        if all_sal_credits:
            latest_sal = max(all_sal_credits, key=lambda x: x.event_date)
            if any(k in latest_sal.description.lower() for k in ("final", "terminated", "severance")):
                salary_cancelled = True

    if not salary_cancelled and request_date is not None:
        # Determine baseline active salary amount
        active_salary_amount: Optional[Decimal] = recurring_salary_amount
        active_salary_currency: Optional[str] = None
        regular_salary_events = [
            e for e in valid_cash_events
            if e.category == "salary" and e.direction == "credit"
            and not any(k in e.description.lower() for k in (
                "arrear", "bonus", "commission", "one-time", "adjustment", "final", "severance"
            ))
        ]
        # A message amendment changes the amount but does not change the currency
        # of the established salary stream.
        if active_salary_amount is not None and regular_salary_events:
            active_salary_currency = max(
                regular_salary_events, key=lambda e: (e.event_date, e.event_id)
            ).currency
        if active_salary_amount is None:
            # Check existing scheduled salary
            sched_sal = next(
                (e for e in valid_cash_events if e.category == "salary" and e.direction == "credit" and e.status == "scheduled"),
                None,
            )
            if sched_sal is not None:
                active_salary_amount = sched_sal.amount
                active_salary_currency = sched_sal.currency
            else:
                # Check most recent regular settled salary
                settled_sal = [
                    e for e in valid_cash_events
                    if e.category == "salary" and e.direction == "credit" and e.status == "settled"
                    and not any(k in e.description.lower() for k in ("arrear", "bonus", "commission", "one-time", "adjustment", "final"))
                ]
                if settled_sal:
                    latest_settled_sal = max(settled_sal, key=lambda x: (x.event_date, x.event_id))
                    active_salary_amount = latest_settled_sal.amount
                    active_salary_currency = latest_settled_sal.currency

        if active_salary_amount is not None and active_salary_amount > 0:
            recurring_salary_amount = active_salary_amount
            active_salary_currency = active_salary_currency or profile.home_currency
            # Determine payday (day of month)
            if next_salary_date is not None:
                payday = next_salary_date.day
            else:
                existing_sal = [
                    e for e in valid_cash_events
                    if e.category == "salary" and e.direction == "credit"
                    and not any(k in e.description.lower() for k in ("arrear", "bonus", "commission", "one-time", "adjustment", "final"))
                ]
                payday = infer_salary_payday(existing_sal)

            # Forecast window [request_date, request_date + forecast_days]
            future_dates = _get_monthly_dates(request_date, request_date + timedelta(days=forecast_days), payday)
            for d_date in future_dates:
                # Deduplication: check if a salary credit already exists on this date
                exists_on_date = any(
                    e.category == "salary" and e.direction == "credit" and (e.settlement_date == d_date or e.event_date == d_date)
                    for e in valid_cash_events
                )
                if not exists_on_date:
                    synth_ev = FinancialEvent(
                        event_id=f"sched_sal_{user_id}_{d_date.isoformat()}",
                        user_id=user_id,
                        event_type="income",
                        description="Confirmed scheduled salary",
                        category="salary",
                        direction="credit",
                        amount=active_salary_amount,
                        # Keep the source currency with the source amount. Currency
                        # conversion happens once during ledger normalization.
                        currency=active_salary_currency,
                        event_date=d_date,
                        settlement_date=d_date,
                        status="scheduled",
                        linked_event_id=None,
                        flexibility="fixed",
                        minimum_allowed_amount=None,
                    )
                    valid_cash_events.append(synth_ev)

    # Step 5: Currency Normalization
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
            source_delta_id=e.event_id if (e.event_id.startswith("delta_") or e.event_id.startswith("sched_sal_")) else None,
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
        user_general_matched_count=user_general_matched_count,
        matched_deltas_count=matched_deltas_count,
        unmatched_deltas_count=unmatched_deltas_count,
        no_change_deltas_count=no_change_deltas_count,
        protected_categories=profile.expense_categories_to_protect,
    )


# ---------------------------------------------------------------------------
# All-Users Reconciliation
# ---------------------------------------------------------------------------

def reconcile_all_ledgers(
    ds: DataStore,
    extracted_data: dict,
    converter: Optional[CurrencyConverter] = None,
    forecast_days: int = 90,
) -> Dict[str, ReconciledLedger]:
    """Reconcile financial ledgers for all users in the dataset."""
    if converter is None:
        converter = CurrencyConverter(ds.exchange_rates)

    ledgers: Dict[str, ReconciledLedger] = {}
    for profile in ds.profiles:
        ledgers[profile.user_id] = reconcile_user_ledger(
            profile.user_id, ds, extracted_data, converter, forecast_days=forecast_days
        )
    return ledgers
