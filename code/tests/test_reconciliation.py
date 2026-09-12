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
    # Pick a user with pending debits and pending credits if available, or test across sample
    # user_02 has a pending debit: event_185 (shopping, debit, 1651100 IDR)
    ledger_u02 = reconcile_user_ledger("user_02", datastore, extracted_data, converter)
    ev_185 = next((e for e in ledger_u02.all_events if e.event_id == "event_185"), None)
    assert ev_185 is not None, "Pending debit event_185 MUST be reserved on the ledger"
    assert ev_185.status == "pending"
    assert ev_185.direction == "debit"

    # Find users with pending credits in raw data
    pending_credits = [e for e in datastore.events if e.status == "pending" and e.direction == "credit"]
    assert len(pending_credits) == 8
    # Reconcile all users with pending credits and verify they are NOT on the cash ledger
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
    # Find an event where event.currency != profile.home_currency
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
# Test 4: Delta Category Matching Logic
# ---------------------------------------------------------------------------

def test_match_general_delta_to_category(datastore: DataStore):
    u_events = datastore.get_events_for_user("user_14")
    msg_10 = next(m for m in datastore.messages if m.message_id == "message_10")

    # Salary delta
    sal_delta = {
        "message_id": "message_10",
        "action": "amend",
        "target": "user_general",
        "field": "recurring_value",
        "new_value": "2717",
        "effective_date": "2025-08-15",
        "reasoning": "Regular salary of 2717 resumes on 2025-08-15.",
    }
    match_sal = match_general_delta_to_category(sal_delta, msg_10, u_events)
    assert match_sal.matched is True
    assert match_sal.category == "salary"
    assert match_sal.event_type == "income"
    assert match_sal.is_unquantifiable is False

    # Childcare unquantifiable delta
    childcare_delta = {
        "message_id": "message_10",
        "action": "new_fact",
        "target": "user_general",
        "field": "recurring_value",
        "new_value": None,
        "effective_date": "2025-08-01",
        "confidence": "low",
        "reasoning": "New recurring childcare payment starts in August 2025, but amount is unspecified.",
    }
    match_child = match_general_delta_to_category(childcare_delta, msg_10, u_events)
    assert match_child.matched is True
    assert match_child.category == "childcare"
    assert match_child.is_unquantifiable is True

    # Unknown delta fallback
    unknown_delta = {
        "message_id": "message_unknown",
        "action": "amend",
        "target": "user_general",
        "field": "unknown_field",
        "new_value": "123",
        "reasoning": "Cryptic alien transmission unrelated to any category.",
    }
    match_unk = match_general_delta_to_category(unknown_delta, None, u_events)
    assert match_unk.matched is False
    assert match_unk.match_confidence == "none"


# ---------------------------------------------------------------------------
# Test 5: Hand-Verified Concrete Test Cases
# ---------------------------------------------------------------------------

def test_hand_verified_case_1_salary_amend_user_216(
    datastore: DataStore, extracted_data: dict, converter: CurrencyConverter
):
    """Case 1: user_216 - Salary amendment via message_169.

    Before: 5 past settled salaries at 1890.72 USD (last 2026-06-15).
    Delta: message_169 raises salary to 2424 USD starting 2026-07-15.
    After: Reconciled ledger has recurring_salary_amount == 2424 USD.
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
    assert ledger.matched_deltas_count == 1
    assert ledger.unmatched_deltas_count == 0


def test_hand_verified_case_2_cancel_salary_user_75(
    datastore: DataStore, extracted_data: dict, converter: CurrencyConverter
):
    """Case 2: user_75 - Employment cancellation via message_57.

    Before: 5 past settled salaries at 50820 ZAR (Nov 2025 to Mar 2026).
    Delta: message_57 states employment ended, no further regular salary.
    After: Reconciled ledger has salary_cancelled == True, recurring_salary_amount == None.
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
    # Confirm no scheduled salary exists on the reconciled ledger
    assert not any(e.category == "salary" and e.status == "scheduled" for e in ledger.all_events)
    assert ledger.matched_deltas_count == 1
    assert ledger.unmatched_deltas_count == 0


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
# Test 6: Full-Dataset Reconciliation Invariants
# ---------------------------------------------------------------------------

def test_full_dataset_reconciliation(
    datastore: DataStore, extracted_data: dict, converter: CurrencyConverter
):
    """Run reconciliation across all 275 profiles and verify dataset-wide invariants."""
    ledgers = reconcile_all_ledgers(datastore, extracted_data, converter)
    assert len(ledgers) == len(datastore.profiles) == 275

    total_matched = sum(l.matched_deltas_count for l in ledgers.values())
    total_unmatched = sum(l.unmatched_deltas_count for l in ledgers.values())
    total_unquant = sum(len(l.unquantifiable_commitments) for l in ledgers.values())

    # Invariants
    assert total_unmatched == 0, f"Expected 0 unmatched deltas, got {total_unmatched}"
    assert total_matched == 114, f"Expected 114 matched active deltas, got {total_matched}"
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
