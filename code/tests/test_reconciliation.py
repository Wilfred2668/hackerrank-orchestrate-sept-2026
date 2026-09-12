"""
Comprehensive unit tests for the Reconciliation Engine (Phase 3).
"""

from __future__ import annotations

import json
import os
import pytest
from datetime import date
from decimal import Decimal

from lib.currency import CurrencyConverter, ExchangeRateMissing
from lib.loaders import DataStore
from lib.models import FinancialEvent, Message
from lib.reconciliation import (
    DeltaCategoryMatch,
    ReconciledEvent,
    ReconciledLedger,
    UnquantifiableCommitment,
    fill_blank_amounts,
    is_recurring_event,
    match_general_delta_to_category,
    reconcile_all_ledgers,
    reconcile_user_ledger,
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


@pytest.fixture(scope="module")
def converter(datastore: DataStore) -> CurrencyConverter:
    return CurrencyConverter(datastore.exchange_rates)


# ---------------------------------------------------------------------------
# Test 1: Image Extractions (Unconditional Blank Amount Fill)
# ---------------------------------------------------------------------------

def test_fill_blank_amounts_all_16_events(datastore: DataStore, extracted_data: dict):
    raw_blank_events = [e for e in datastore.events if e.amount is None]
    assert len(raw_blank_events) == 16, f"Expected 16 blank events, got {len(raw_blank_events)}"

    img_extractions = extracted_data["image_extractions"]
    assert len(img_extractions) == 16

    filled = fill_blank_amounts(raw_blank_events, img_extractions)
    assert len(filled) == 16
    for e in filled:
        assert e.amount is not None
        assert isinstance(e.amount, Decimal)
        assert e.amount > 0

    # Spot check specific golden values
    by_id = {e.event_id: e for e in filled}
    assert by_id["event_253"].amount == Decimal("4365000")
    assert by_id["event_1442"].amount == Decimal("100000")
    assert by_id["event_1545"].amount == Decimal("41272.00")
    assert by_id["event_4535"].amount == Decimal("15339.00")
    assert by_id["event_10521"].amount == Decimal("393.22")


def test_fill_blank_amounts_missing_extraction_raises(datastore: DataStore):
    raw_blank = [e for e in datastore.events if e.amount is None][:1]
    with pytest.raises(ValueError, match="blank amount but no matching image extraction"):
        fill_blank_amounts(raw_blank, [])


# ---------------------------------------------------------------------------
# Test 2: Cash-State Rules
# ---------------------------------------------------------------------------

def test_cash_state_rules_filtering(datastore: DataStore, extracted_data: dict, converter: CurrencyConverter):
    # user_02 has a pending debit: event_185 (shopping, debit, 1651100 IDR)
    ledger_u02 = reconcile_user_ledger("user_02", datastore, extracted_data, converter)
    ev_185 = next((e for e in ledger_u02.all_events if e.event_id == "event_185"), None)
    assert ev_185 is not None, "Pending debit event_185 MUST be reserved on the ledger"
    assert ev_185.status == "pending"
    assert ev_185.direction == "debit"

    # Find users with pending credits in raw data
    pending_credits = [e for e in datastore.events if e.status == "pending" and e.direction == "credit"]
    assert len(pending_credits) == 8
    users_with_pending_credits = set(e.user_id for e in pending_credits)
    for uid in users_with_pending_credits:
        ledger = reconcile_user_ledger(uid, datastore, extracted_data, converter)
        event_ids_on_ledger = set(e.event_id for e in ledger.all_events)
        for pc in pending_credits:
            if pc.user_id == uid:
                assert pc.event_id not in event_ids_on_ledger, (
                    f"Pending credit {pc.event_id} should be IGNORED on ledger until settled"
                )

    # Check non-cash / unrealized events are excluded
    non_cash_events = [e for e in datastore.events if e.direction == "non_cash" or e.status == "unrealized"]
    assert len(non_cash_events) == 10
    users_with_non_cash = set(e.user_id for e in non_cash_events)
    for uid in users_with_non_cash:
        ledger = reconcile_user_ledger(uid, datastore, extracted_data, converter)
        event_ids = set(e.event_id for e in ledger.all_events)
        for nc in non_cash_events:
            if nc.user_id == uid:
                assert nc.event_id not in event_ids, f"Non-cash event {nc.event_id} must never touch cash balance"


# ---------------------------------------------------------------------------
# Test 3: Currency Normalization
# ---------------------------------------------------------------------------

def test_currency_normalization(datastore: DataStore, extracted_data: dict, converter: CurrencyConverter):
    foreign_events = []
    for e in datastore.events:
        prof = datastore.get_profile(e.user_id)
        if e.currency != prof.home_currency and e.status == "settled" and e.direction != "non_cash" and e.amount:
            foreign_events.append((prof.home_currency, e))

    assert len(foreign_events) > 0
    sample_home_cur, sample_ev = foreign_events[0]
    ledger = reconcile_user_ledger(sample_ev.user_id, datastore, extracted_data, converter)
    reconciled = next(e for e in ledger.all_events if e.event_id == sample_ev.event_id)

    assert reconciled.home_currency == sample_home_cur
    expected_norm = converter.convert(
        sample_ev.amount, sample_ev.currency, sample_home_cur, sample_ev.settlement_date
    )
    assert reconciled.normalized_amount == expected_norm


# ---------------------------------------------------------------------------
# Test 4: Delta Category Matching Logic & Exact Counts
# ---------------------------------------------------------------------------

def test_exact_active_deltas_category_mapping(datastore: DataStore, extracted_data: dict):
    deltas = extracted_data["message_deltas"]
    active_deltas = [d for d in deltas if d.get("action") != "confirm_no_change"]
    assert len(active_deltas) == 114

    user_general_deltas = [d for d in active_deltas if d.get("target") == "user_general"]
    assert len(user_general_deltas) == 113

    specific_target_deltas = [d for d in active_deltas if d.get("target") != "user_general"]
    assert len(specific_target_deltas) == 1
    assert specific_target_deltas[0]["message_id"] == "message_35"
    assert specific_target_deltas[0]["target"] == "event_4535"

    msg_map = {m.message_id: m for m in datastore.messages}
    cat_counts = {}
    cancel_salary_count = 0

    for d in user_general_deltas:
        msg = msg_map.get(d["message_id"])
        evs = datastore.get_events_for_user(d["user_id"])
        match = match_general_delta_to_category(d, msg, evs)
        assert match.matched is True
        cat = match.category
        cat_counts[cat] = cat_counts.get(cat, 0) + 1
        if d.get("action") == "cancel" and cat == "salary":
            cancel_salary_count += 1

    # Exact breakdown of the 113 user_general active deltas:
    assert cat_counts["salary"] == 83
    assert cat_counts["income"] == 15
    assert cat_counts["childcare"] == 8
    assert cat_counts["rent"] == 7
    assert sum(cat_counts.values()) == 113
    # Explicitly confirm all 6 cancellations are in the salary category
    assert cancel_salary_count == 6


# ---------------------------------------------------------------------------
# Test 5: Hand-Verified Concrete Test Cases 1 - 4
# ---------------------------------------------------------------------------

def test_hand_verified_case_1_salary_amend_user_216(
    datastore: DataStore, extracted_data: dict, converter: CurrencyConverter
):
    """Case 1: user_216 - Salary amendment via message_169.

    Before: 5 past settled salaries at 1890.72 USD (last 2026-06-15).
    Delta: message_169 raises salary to 2424 USD starting 2026-07-15.
    After:
      - recurring_salary_amount == 2424 USD
      - User receives future salary cash flow on the ledger across the 90-day forecast!
    """
    raw_salaries = [
        e for e in datastore.get_events_for_user("user_216")
        if e.category == "salary" and e.status == "settled"
    ]
    assert len(raw_salaries) == 5
    assert all(s.amount == Decimal("1890.72") for s in raw_salaries)

    ledger = reconcile_user_ledger("user_216", datastore, extracted_data, converter)
    assert ledger.user_id == "user_216"
    assert ledger.home_currency == "USD"
    assert ledger.salary_cancelled is False
    assert ledger.recurring_salary_amount == Decimal("2424")

    # Verify future salary cash flow on the ledger
    req = next(r for r in datastore.requests if r.user_id == "user_216")
    future_salaries = [
        e for e in ledger.all_events
        if e.category == "salary" and e.direction == "credit" and e.event_date >= req.request_date
    ]
    assert len(future_salaries) == 3, f"Expected 3 monthly salaries in 90-day window, got {len(future_salaries)}"
    expected_dates = [date(2026, 7, 15), date(2026, 8, 15), date(2026, 9, 15)]
    assert [s.event_date for s in future_salaries] == expected_dates
    assert all(s.normalized_amount == Decimal("2424") for s in future_salaries)


def test_hand_verified_case_2_cancel_salary_user_75(
    datastore: DataStore, extracted_data: dict, converter: CurrencyConverter
):
    """Case 2: user_75 - Employment cancellation via message_57.

    Before: 5 past settled salaries at 50820 ZAR (Nov 2025 to Mar 2026).
    Delta: message_57 states employment ended, no further regular salary.
    After: Reconciled ledger has salary_cancelled == True, recurring_salary_amount == None.
           Zero future salary cash flows exist on or after request date.
    """
    raw_salaries = [
        e for e in datastore.get_events_for_user("user_75")
        if e.category == "salary" and e.status == "settled"
    ]
    assert len(raw_salaries) == 5
    assert all(s.amount == Decimal("50820") for s in raw_salaries)

    ledger = reconcile_user_ledger("user_75", datastore, extracted_data, converter)
    assert ledger.user_id == "user_75"
    assert ledger.home_currency == "ZAR"
    assert ledger.salary_cancelled is True
    assert ledger.recurring_salary_amount is None

    req = next(r for r in datastore.requests if r.user_id == "user_75")
    future_salaries = [
        e for e in ledger.all_events
        if e.category == "salary" and e.direction == "credit" and e.event_date >= req.request_date
    ]
    assert len(future_salaries) == 0, "Cancelled salary stream must not have future salary cash flows"


def test_hand_verified_case_3_childcare_split_user_14(
    datastore: DataStore, extracted_data: dict, converter: CurrencyConverter
):
    """Case 3: user_14 - Multi-clause message_10 with childcare unquantifiable fact.

    Before: 3 past settled salaries at 2717 EUR. No childcare events.
    Delta 1: Salary resumes at 2717 EUR on 2025-08-15.
    Delta 2: New recurring childcare begins 2025-08-01, amount unspecified.
    After:
      - recurring_salary_amount == 2717 EUR
      - unquantifiable_commitments contains exactly 1 childcare item with confidence='low'
      - No artificial numerical childcare deduction is invented in ledger amounts.
    """
    raw_events = datastore.get_events_for_user("user_14")
    assert not any(e.category == "childcare" for e in raw_events)

    ledger = reconcile_user_ledger("user_14", datastore, extracted_data, converter)
    assert ledger.user_id == "user_14"
    assert ledger.home_currency == "EUR"
    assert ledger.recurring_salary_amount == Decimal("2717")

    assert len(ledger.unquantifiable_commitments) == 1
    comm = ledger.unquantifiable_commitments[0]
    assert comm.message_id == "message_10"
    assert comm.category == "childcare"
    assert comm.effective_date == date(2025, 8, 1)
    assert comm.confidence == "low"
    assert not any(e.category == "childcare" for e in ledger.all_events)


def test_hand_verified_case_4_arrears_dedupe_user_28(
    datastore: DataStore, extracted_data: dict, converter: CurrencyConverter
):
    """Case 4: user_28 - Arrears deduplication via message_20.

    Before: event_2508 already settled on 2024-05-20 with amount 653.40 EUR (Promotion arrears payment).
    Delta 1: Salary confirmed at 1452 EUR.
    Delta 2: One-time arrears adjustment of 653.40 EUR.
    After: The arrears payment is NOT double-counted. Exactly 1 event with amount 653.40 exists.
    """
    raw_events = datastore.get_events_for_user("user_28")
    raw_arrears = [e for e in raw_events if e.amount == Decimal("653.4") and e.event_id == "event_2508"]
    assert len(raw_arrears) == 1

    ledger = reconcile_user_ledger("user_28", datastore, extracted_data, converter)
    arrears_events = [e for e in ledger.all_events if e.original_amount == Decimal("653.4")]
    assert len(arrears_events) == 1, "Arrears adjustment must be deduped, not double-counted"
    assert arrears_events[0].event_id == "event_2508"
    assert ledger.recurring_salary_amount == Decimal("1452")


# ---------------------------------------------------------------------------
# Test 6: Hand-Verified Case 5 — Preserving Scheduled Dates on Amount Updates
# ---------------------------------------------------------------------------

def test_hand_verified_case_5_preserve_scheduled_salary_dates(
    datastore: DataStore, extracted_data: dict, converter: CurrencyConverter
):
    """Case 5: Verify that updating scheduled salary amounts preserves individual dates.

    Tests a scenario where a real user (user_216) has multiple scheduled salary events
    (e.g., 2026-07-15 and 2026-08-15) on or after an amendment effective date (2026-07-15).
    Verifies that:
      - Amount is updated to 2424 USD on both events.
      - Event 1 retains its date (2026-07-15).
      - Event 2 retains its date (2026-08-15) and is NOT collapsed onto 2026-07-15.
    """
    # Create two pre-existing scheduled events for user_216
    event_jul = FinancialEvent(
        event_id="sched_test_jul",
        user_id="user_216",
        event_type="income",
        description="Scheduled July salary",
        category="salary",
        direction="credit",
        amount=Decimal("1890.72"),
        currency="USD",
        event_date=date(2026, 7, 15),
        settlement_date=date(2026, 7, 15),
        status="scheduled",
        linked_event_id=None,
        flexibility="fixed",
        minimum_allowed_amount=None,
    )
    event_aug = FinancialEvent(
        event_id="sched_test_aug",
        user_id="user_216",
        event_type="income",
        description="Scheduled August salary",
        category="salary",
        direction="credit",
        amount=Decimal("1890.72"),
        currency="USD",
        event_date=date(2026, 8, 15),
        settlement_date=date(2026, 8, 15),
        status="scheduled",
        linked_event_id=None,
        flexibility="fixed",
        minimum_allowed_amount=None,
    )

    # Reconcile user_216 with these two pre-existing scheduled events present
    orig_get_events = datastore.get_events_for_user

    def mock_get_events(uid: str):
        evs = orig_get_events(uid)
        if uid == "user_216":
            return list(evs) + [event_jul, event_aug]
        return evs

    # Temporarily monkeypatch get_events_for_user
    datastore.get_events_for_user = mock_get_events
    try:
        ledger = reconcile_user_ledger("user_216", datastore, extracted_data, converter)
    finally:
        datastore.get_events_for_user = orig_get_events

    ev_jul_rec = next(e for e in ledger.all_events if e.event_id == "sched_test_jul")
    ev_aug_rec = next(e for e in ledger.all_events if e.event_id == "sched_test_aug")

    # Amounts updated to the new amended salary
    assert ev_jul_rec.normalized_amount == Decimal("2424")
    assert ev_aug_rec.normalized_amount == Decimal("2424")

    # Individual dates MUST be preserved (NOT collapsed onto 2026-07-15)
    assert ev_jul_rec.event_date == date(2026, 7, 15)
    assert ev_jul_rec.settlement_date == date(2026, 7, 15)
    assert ev_aug_rec.event_date == date(2026, 8, 15)
    assert ev_aug_rec.settlement_date == date(2026, 8, 15)


# ---------------------------------------------------------------------------
# Test 7: Full-Dataset Reconciliation Invariants
# ---------------------------------------------------------------------------

def test_full_dataset_reconciliation(
    datastore: DataStore, extracted_data: dict, converter: CurrencyConverter
):
    """Run reconciliation across all 275 profiles and verify dataset-wide invariants."""
    ledgers = reconcile_all_ledgers(datastore, extracted_data, converter)
    assert len(ledgers) == len(datastore.profiles) == 275

    total_ug_matched = sum(l.user_general_matched_count for l in ledgers.values())
    total_matched = sum(l.matched_deltas_count for l in ledgers.values())
    total_unmatched = sum(l.unmatched_deltas_count for l in ledgers.values())
    total_unquant = sum(len(l.unquantifiable_commitments) for l in ledgers.values())

    # Invariants
    assert total_unmatched == 0, f"Expected 0 unmatched deltas, got {total_unmatched}"
    assert total_ug_matched == 113, f"Expected 113 matched user_general deltas, got {total_ug_matched}"
    assert total_matched == 114, f"Expected 114 total matched active deltas (113 user_general + 1 specific), got {total_matched}"
    assert total_unquant == 8, f"Expected 8 unquantifiable commitments, got {total_unquant}"

    avg_unquant = total_unquant / len(ledgers)
    assert pytest.approx(avg_unquant, 0.001) == (8 / 275)

    for uid, ledger in ledgers.items():
        assert len(ledger.all_events) > 0
        assert len(ledger.recurring_events) + len(ledger.one_time_events) == len(ledger.all_events)
        assert len(ledger.flexible_events) + len(ledger.fixed_or_protected_events) == len(ledger.all_events)
        for ev in ledger.all_events:
            assert ev.direction in ("credit", "debit")
            assert ev.status in ("settled", "pending", "scheduled")
            assert ev.home_currency == ledger.home_currency
            assert ev.normalized_amount > 0
