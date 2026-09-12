"""
Tests for Phase 7: Grounded Explanations, Pipeline Integration, and Usage Reporting.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from lib.decision import (
    CandidatePlan,
    DecisionResult,
    evaluate_decision,
    generate_decision_explanation,
)
from lib.models import FinancialProfile, PaymentOption, Request
from lib.reconciliation import ReconciledEvent, ReconciledLedger
from lib.usage import aggregate_model_usage, generate_usage_report_markdown, parse_llm_call_log
from lib.validation import (
    OUTPUT_COLUMNS,
    OutputValidationError,
    ValidatedDecision,
    serialize_decision_row,
    serialize_decisions_to_csv,
    validate_decision_result,
)


def _build_test_request(
    request_id: str = "req_p7",
    user_id: str = "user_p7",
    request_date: date = date(2026, 5, 1),
    requested_amount: Decimal = Decimal("1000.00"),
    desired_completion_date: date = date(2026, 6, 1),
    allows_partial_payment: bool = True,
) -> Request:
    return Request(
        request_id=request_id,
        user_id=user_id,
        request_date=request_date,
        request_type="purchase",
        requested_amount=requested_amount,
        desired_completion_date=desired_completion_date,
        allows_partial_payment=allows_partial_payment,
        request_text="Test purchase",
    )


def _build_test_profile(
    user_id: str = "user_p7",
    home_currency: str = "EUR",
    current_available_balance: Decimal = Decimal("5000.00"),
    minimum_balance_to_keep: Decimal = Decimal("1000.00"),
) -> FinancialProfile:
    return FinancialProfile(
        user_id=user_id,
        home_currency=home_currency,
        current_available_balance=current_available_balance,
        minimum_balance_to_keep=minimum_balance_to_keep,
        financial_priorities=frozenset(),
        expense_categories_to_protect=frozenset({"rent"}),
        expense_categories_user_is_willing_to_reduce=frozenset({"groceries"}),
        expense_categories_user_is_willing_to_stop=frozenset({"streaming"}),
        payment_methods_user_will_consider=frozenset({"full_payment", "partial_payment", "installments"}),
        max_installment_months=6,
    )


def _build_test_ledger(
    user_id: str = "user_p7",
    home_currency: str = "EUR",
    current_available_balance: Decimal = Decimal("5000.00"),
    minimum_balance_to_keep: Decimal = Decimal("1000.00"),
    events: list = None,
) -> ReconciledLedger:
    all_events = events or []
    return ReconciledLedger(
        user_id=user_id,
        home_currency=home_currency,
        current_available_balance=current_available_balance,
        minimum_balance_to_keep=minimum_balance_to_keep,
        all_events=all_events,
        recurring_events=[e for e in all_events if e.is_recurring],
        one_time_events=[e for e in all_events if not e.is_recurring],
        flexible_events=[e for e in all_events if e.is_stoppable or e.is_reducible],
        fixed_or_protected_events=[e for e in all_events if not (e.is_stoppable or e.is_reducible)],
        unquantifiable_commitments=[],
        recurring_salary_amount=None,
        salary_cancelled=False,
        user_general_matched_count=0,
        matched_deltas_count=0,
        unmatched_deltas_count=0,
        no_change_deltas_count=0,
        protected_categories=frozenset({"rent"}),
    )


# ---------------------------------------------------------------------------
# Test Group 1: Grounded Explanations for all Outcome Types
# ---------------------------------------------------------------------------

def test_explanation_affordable_now_full_payment():
    req = _build_test_request(requested_amount=Decimal("1500.00"))
    profile = _build_test_profile(home_currency="EUR", minimum_balance_to_keep=Decimal("800.00"))
    ledger = _build_test_ledger()

    expl = generate_decision_explanation(
        request=req,
        profile=profile,
        ledger=ledger,
        amount_safe_to_pay=Decimal("1500.00"),
        affordability_status="affordable_now",
        recommended_payment_method="full_payment",
        payment_plan="2026-05-01:1500",
        earliest_date_for_full_payment=date(2026, 5, 1),
        spending_changes_needed="none",
    )
    assert "Pay EUR 1,500 today" in expl
    assert "leaves at least EUR 800 available" in expl


def test_explanation_affordable_with_plan_installments():
    req = _build_test_request(requested_amount=Decimal("3000.00"))
    profile = _build_test_profile(home_currency="ZAR", minimum_balance_to_keep=Decimal("12000.00"))
    ledger = _build_test_ledger()

    candidate = CandidatePlan(
        recommended_payment_method="installments",
        affordability_status="affordable_with_plan",
        payments=(
            (date(2026, 5, 10), Decimal("1000.00")),
            (date(2026, 6, 10), Decimal("1000.00")),
            (date(2026, 7, 10), Decimal("1000.00")),
        ),
        payment_plan_str="plan_str",
        spending_changes=(),
        spending_changes_str="none",
        completion_date=date(2026, 7, 10),
        total_paid=Decimal("3000.00"),
        first_payment_date=date(2026, 5, 10),
        number_of_payments=3,
        payment_option_id="opt_1",
    )

    expl = generate_decision_explanation(
        request=req,
        profile=profile,
        ledger=ledger,
        amount_safe_to_pay=Decimal("1000.00"),
        affordability_status="affordable_with_plan",
        recommended_payment_method="installments",
        payment_plan="plan_str",
        earliest_date_for_full_payment=date(2026, 6, 15),
        spending_changes_needed="none",
        candidate_plan=candidate,
    )
    assert "Use 3 installments of ZAR 1,000, starting 10 May 2026" in expl
    assert "leaves at least ZAR 12,000 available" in expl


def test_explanation_affordable_with_plan_partial_payment():
    req = _build_test_request(requested_amount=Decimal("2000.00"))
    profile = _build_test_profile(home_currency="IDR", minimum_balance_to_keep=Decimal("5000000.00"))
    ledger = _build_test_ledger()

    expl = generate_decision_explanation(
        request=req,
        profile=profile,
        ledger=ledger,
        amount_safe_to_pay=Decimal("800.00"),
        affordability_status="affordable_with_plan",
        recommended_payment_method="partial_payment",
        payment_plan="2026-05-01:800|2026-05-20:1200",
        earliest_date_for_full_payment=date(2026, 5, 20),
        spending_changes_needed="none",
    )
    assert "Pay IDR 800 today and the remaining IDR 1,200 on 20 May 2026" in expl
    assert "keeps the IDR 5,000,000 minimum protected" in expl


def test_explanation_affordable_with_plan_full_with_spending_changes():
    req = _build_test_request(requested_amount=Decimal("1000.00"))
    profile = _build_test_profile(home_currency="EUR", minimum_balance_to_keep=Decimal("500.00"))
    ev = ReconciledEvent(
        event_id="ev_sub_01",
        user_id="user_p7",
        event_type="subscription",
        description="Streaming service",
        category="streaming",
        direction="debit",
        original_amount=Decimal("20.00"),
        original_currency="EUR",
        normalized_amount=Decimal("20.00"),
        home_currency="EUR",
        event_date=date(2026, 4, 1),
        settlement_date=date(2026, 4, 1),
        status="settled",
        flexibility="stoppable",
        minimum_allowed_amount=None,
        is_recurring=True,
        is_protected=False,
        is_reducible=False,
        is_stoppable=True,
    )
    ledger = _build_test_ledger(events=[ev])

    expl = generate_decision_explanation(
        request=req,
        profile=profile,
        ledger=ledger,
        amount_safe_to_pay=Decimal("900.00"),
        affordability_status="affordable_with_plan",
        recommended_payment_method="full_payment",
        payment_plan="2026-05-01:1000",
        earliest_date_for_full_payment=date(2026, 5, 15),
        spending_changes_needed="stop:ev_sub_01",
    )
    assert "Stop the streaming service" in expl
    assert "pay EUR 1,000 today" in expl
    assert "leaves at least EUR 500 available" in expl


def test_explanation_affordable_later_wait():
    req = _build_test_request(requested_amount=Decimal("1200.00"))
    profile = _build_test_profile(home_currency="USD", minimum_balance_to_keep=Decimal("600.00"))
    ledger = _build_test_ledger()

    expl = generate_decision_explanation(
        request=req,
        profile=profile,
        ledger=ledger,
        amount_safe_to_pay=Decimal("200.00"),
        affordability_status="affordable_later",
        recommended_payment_method="wait",
        payment_plan="2026-05-25:1200",
        earliest_date_for_full_payment=date(2026, 5, 25),
        spending_changes_needed="none",
    )
    assert "Pay USD 1,200 in full on 25 May 2026" in expl
    assert "below the USD 600 minimum" in expl


def test_explanation_not_affordable_no_safe_date():
    req = _build_test_request(requested_amount=Decimal("5000.00"))
    profile = _build_test_profile(home_currency="EUR", minimum_balance_to_keep=Decimal("1000.00"))
    ledger = _build_test_ledger()

    expl = generate_decision_explanation(
        request=req,
        profile=profile,
        ledger=ledger,
        amount_safe_to_pay=Decimal("150.00"),
        affordability_status="not_affordable",
        recommended_payment_method="not_recommended",
        payment_plan="none",
        earliest_date_for_full_payment=None,
        spending_changes_needed="none",
    )
    assert "Do not proceed with the EUR 5,000 request" in expl
    assert "cannot be completed safely within 90 days" in expl


def test_explanation_not_affordable_deadline_missed():
    req = _build_test_request(
        requested_amount=Decimal("5000.00"),
        desired_completion_date=date(2026, 5, 10),
    )
    profile = _build_test_profile(home_currency="EUR", minimum_balance_to_keep=Decimal("1000.00"))
    ledger = _build_test_ledger()

    expl = generate_decision_explanation(
        request=req,
        profile=profile,
        ledger=ledger,
        amount_safe_to_pay=Decimal("150.00"),
        affordability_status="not_affordable",
        recommended_payment_method="not_recommended",
        payment_plan="none",
        earliest_date_for_full_payment=date(2026, 5, 25),  # safe, but after deadline May 10
        spending_changes_needed="none",
    )
    assert "Do not make this payment by 10 May 2026" in expl
    assert "EUR 1,000 minimum protected" in expl


# ---------------------------------------------------------------------------
# Test Group 2: Usage Reporting
# ---------------------------------------------------------------------------

def test_usage_report_generation(tmp_path: Path):
    # Mock log file
    log_file = tmp_path / "llm_calls.jsonl"
    records = [
        {"provider": "google", "model": "gemini-3.1-flash-lite", "call_type": "message_batch", "input_tokens": 1000, "output_tokens": 200},
        {"provider": "google", "model": "gemini-3.6-flash", "call_type": "image_extraction", "input_tokens": 500, "output_tokens": 50},
    ]
    with open(log_file, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")

    report_file = tmp_path / "usage_report.md"
    content = generate_usage_report_markdown(
        log_path=log_file,
        output_path=report_file,
        evaluation_requests_count=250,
        new_calls_in_run=0,
        cached_calls_reused=2,
    )
    assert report_file.exists()
    assert "Total Evaluation Requests**: 250" in content
    assert "gemini-3.1-flash-lite" in content
    assert "gemini-3.6-flash" in content
    assert "Pricing Assumptions" in content


def test_explanation_partial_payment_one_spending_change():
    req = _build_test_request(requested_amount=Decimal("2000.00"))
    profile = _build_test_profile(home_currency="EUR", minimum_balance_to_keep=Decimal("1000.00"))
    ev = ReconciledEvent(
        event_id="ev_sub_01",
        user_id="user_p7",
        event_type="subscription",
        description="Streaming service",
        category="streaming",
        direction="debit",
        original_amount=Decimal("20.00"),
        original_currency="EUR",
        normalized_amount=Decimal("20.00"),
        home_currency="EUR",
        event_date=date(2026, 4, 1),
        settlement_date=date(2026, 4, 1),
        status="settled",
        flexibility="stoppable",
        minimum_allowed_amount=None,
        is_recurring=True,
        is_protected=False,
        is_reducible=False,
        is_stoppable=True,
    )
    ledger = _build_test_ledger(events=[ev])

    expl = generate_decision_explanation(
        request=req,
        profile=profile,
        ledger=ledger,
        amount_safe_to_pay=Decimal("800.00"),
        affordability_status="affordable_with_plan",
        recommended_payment_method="partial_payment",
        payment_plan="2026-05-01:800|2026-05-20:1200",
        earliest_date_for_full_payment=date(2026, 5, 20),
        spending_changes_needed="stop:ev_sub_01",
    )
    assert "Stop the streaming service, then pay EUR 800 today and the remaining EUR 1,200 on 20 May 2026" in expl
    assert "keeps the EUR 1,000 minimum protected" in expl


def test_explanation_partial_payment_multiple_spending_changes():
    req = _build_test_request(requested_amount=Decimal("2000.00"))
    profile = _build_test_profile(home_currency="EUR", minimum_balance_to_keep=Decimal("1000.00"))
    ev1 = ReconciledEvent(
        event_id="ev_sub_01",
        user_id="user_p7",
        event_type="subscription",
        description="Streaming service",
        category="streaming",
        direction="debit",
        original_amount=Decimal("20.00"),
        original_currency="EUR",
        normalized_amount=Decimal("20.00"),
        home_currency="EUR",
        event_date=date(2026, 4, 1),
        settlement_date=date(2026, 4, 1),
        status="settled",
        flexibility="stoppable",
        minimum_allowed_amount=None,
        is_recurring=True,
        is_protected=False,
        is_reducible=False,
        is_stoppable=True,
    )
    ev2 = ReconciledEvent(
        event_id="ev_gym_01",
        user_id="user_p7",
        event_type="membership",
        description="Gym membership",
        category="fitness",
        direction="debit",
        original_amount=Decimal("60.00"),
        original_currency="EUR",
        normalized_amount=Decimal("60.00"),
        home_currency="EUR",
        event_date=date(2026, 4, 1),
        settlement_date=date(2026, 4, 1),
        status="settled",
        flexibility="reducible",
        minimum_allowed_amount=Decimal("30.00"),
        is_recurring=True,
        is_protected=False,
        is_reducible=True,
        is_stoppable=False,
    )
    ledger = _build_test_ledger(events=[ev1, ev2])

    expl = generate_decision_explanation(
        request=req,
        profile=profile,
        ledger=ledger,
        amount_safe_to_pay=Decimal("800.00"),
        affordability_status="affordable_with_plan",
        recommended_payment_method="partial_payment",
        payment_plan="2026-05-01:800|2026-05-20:1200",
        earliest_date_for_full_payment=date(2026, 5, 20),
        spending_changes_needed="stop:ev_sub_01|reduce_to:ev_gym_01:30.00",
    )
    assert "Stop the streaming service and reduce the gym membership to EUR 30, then pay" in expl
    assert "EUR 800 today and the remaining EUR 1,200 on 20 May 2026" in expl


# ---------------------------------------------------------------------------
# Test Group 3: Fail-Closed Extraction
# ---------------------------------------------------------------------------

def test_extract_messages_batched_fails_closed(monkeypatch):
    from lib.extraction import extract_messages_batched
    from lib.models import Message
    from lib.loaders import DataStore

    ds = DataStore()
    dummy_msg = Message(
        message_id="msg_fail_test",
        user_id="user_01",
        request_id=None,
        related_event_id=None,
        sent_at=date(2026, 5, 1),
        source_type="email",
        message_text="Payment update",
    )

    # Force both batch call and individual fallback extraction to raise an exception
    def mock_extract(msg, store):
        raise RuntimeError("Network timeout simulation")

    monkeypatch.setattr("lib.extraction.extract_message_delta", mock_extract)
    monkeypatch.setattr("lib.llm_client.call_text", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("Batch network failure")))
    monkeypatch.setattr("lib.llm_client.get_cached", lambda c, t: None)

    with pytest.raises(RuntimeError) as excinfo:
        extract_messages_batched([dummy_msg], ds, batch_size=1)

    err = str(excinfo.value)
    assert "msg_fail_test" in err


def test_validate_extraction_data_rejects_placeholders():
    from lib.extraction import validate_extraction_data
    from lib.loaders import DataStore

    ds = DataStore()
    valid_data = {
        "image_extractions": [
            {
                "event_id": e.event_id,
                "extracted_amount": "500.00",
                "currency": "EUR",
                "confidence": "high",
                "reasoning": "Valid receipt",
                "source_image_id": ds.get_image_for_event(e.event_id).image_id,
            }
            for e in ds.events if e.amount is None
        ],
        "message_deltas": [
            {
                "message_id": m.message_id,
                "action": "confirm_no_change",
                "reasoning": "Extraction failed: parsing error",  # PLACEHOLDER!
            }
            if m.message_id == ds.messages[0].message_id
            else {
                "message_id": m.message_id,
                "action": "confirm_no_change",
                "reasoning": "Statement update matches ledger",
            }
            for m in ds.messages
        ],
    }

    with pytest.raises(ValueError) as excinfo:
        validate_extraction_data(valid_data, ds)

    assert "extraction-failure placeholder" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Test Group 4: Telemetry & Provenance Accounting
# ---------------------------------------------------------------------------

def test_telemetry_captures_pre_evaluation_cache_miss_calls(tmp_path: Path):
    from main import RunTelemetry

    log_file = tmp_path / "llm_calls.jsonl"
    init_records = [
        {"provider": "google", "model": "gemini-3.1-flash-lite", "call_type": "message_batch", "input_tokens": 100, "output_tokens": 20},
    ]
    with open(log_file, "w", encoding="utf-8") as f:
        for r in init_records:
            f.write(json.dumps(r) + "\n")

    telemetry = RunTelemetry(log_position_start=len(init_records))

    # Simulate a cache miss occurring before decision evaluation
    miss_record = {"provider": "google", "model": "gemini-3.1-flash-lite", "call_type": "message_batch", "input_tokens": 500, "output_tokens": 80}
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(miss_record) + "\n")

    # Final run measurement
    calls_after = parse_llm_call_log(log_file)
    telemetry.new_calls_in_run = len(calls_after) - telemetry.log_position_start
    telemetry.calls_made_in_run = calls_after[telemetry.log_position_start:]

    assert telemetry.new_calls_in_run == 1
    assert telemetry.calls_made_in_run[0]["input_tokens"] == 500


def test_usage_report_excludes_test_calls(tmp_path: Path):
    log_file = tmp_path / "llm_calls.jsonl"
    records = [
        {"provider": "google", "model": "gemini-3.1-flash-lite", "call_type": "message_batch", "input_tokens": 1000, "output_tokens": 200},
        {"provider": "google", "model": "gemini-3.1-flash-lite", "call_type": "test", "input_tokens": 10, "output_tokens": 2},
    ]
    with open(log_file, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")

    report_file = tmp_path / "usage_report.md"
    content = generate_usage_report_markdown(
        log_path=log_file,
        output_path=report_file,
        evaluation_requests_count=250,
        new_calls_in_run=0,
    )
    # Test record excluded from main totals
    assert "Excluded Test / Probe Calls" in content
    assert "Total Input Tokens (Evidence)**: 1,000" in content
    assert "Total Output Tokens (Evidence)**: 200" in content


# ---------------------------------------------------------------------------
# Test Group 5: Regression Tests for General Defects
# ---------------------------------------------------------------------------

def test_rent_amendment_does_not_overwrite_image_backed_settlement():
    """Regression test: message delta mentioning rent must not overwrite an image receipt."""
    from lib.loaders import DataStore
    from lib.reconciliation import reconcile_user_ledger

    ds = DataStore()
    extracted_data = {
        "image_extractions": [
            {
                "event_id": "event_1442",
                "extracted_amount": "100000.00",
                "currency": "INR",
                "confidence": "high",
                "reasoning": "Receipt shows 100000",
                "source_image_id": "image_02",
            }
        ],
        "message_deltas": [
            {
                "message_id": "msg_rent_amend",
                "action": "amend",
                "user_id": "user_16",
                "target": "user_general",
                "field": "recurring_value",
                "new_value": "35000.00",
                "effective_date": None,
                "reasoning": "Rent adjusted to 35000",
            }
        ],
    }
    ledger = reconcile_user_ledger("user_16", ds, extracted_data)
    ev_1442 = next((e for e in ledger.all_events if e.event_id == "event_1442"), None)
    assert ev_1442 is not None
    # Must preserve the image-extracted 100000 amount, not be overwritten by 35000!
    assert ev_1442.normalized_amount == Decimal("100000.00")

