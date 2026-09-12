"""
Tests for Phase 2: Evidence Extraction Layer.

Validates:
1. Golden values for all 16 extracted images.
2. Coverage: exactly 16 image extractions and 215 message deltas.
3. Schema validation: all records match required schema, dates YYYY-MM-DD, amounts decimal.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

# Ensure code/ is on sys.path
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), "..")))

from lib.loaders import DataStore

DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "extracted_deltas.json"

GOLDEN_IMAGE_AMOUNTS = {
    "event_253": Decimal("4365000"),
    "event_1442": Decimal("100000"),
    "event_1545": Decimal("41272"),
    "event_1700": Decimal("2854"),
    "event_1786": Decimal("704.05"),
    "event_3051": Decimal("1995"),
    "event_3231": Decimal("8528"),
    "event_4535": Decimal("15339"),
    "event_5170": Decimal("723"),
    "event_6033": Decimal("79679.26"),
    "event_6859": Decimal("3650"),
    "event_7307": Decimal("33.50"),
    "event_7941": Decimal("2298"),
    "event_9421": Decimal("4543"),
    "event_9806": Decimal("9968"),
    "event_10521": Decimal("393.22"),
}


@pytest.fixture(scope="module")
def extracted_data():
    assert DATA_FILE.exists(), f"Missing {DATA_FILE}. Run code/run_phase2.py first."
    with open(DATA_FILE, encoding="utf-8") as f:
        return json.load(f)


def test_coverage(extracted_data):
    """Test 2: Coverage of 16 images and all 215 messages."""
    images = extracted_data.get("image_extractions", [])
    messages = extracted_data.get("message_deltas", [])

    assert len(images) == 16, f"Expected 16 images, got {len(images)}"
    # Multi-clause messages emit multiple deltas, so total deltas >= 215
    assert len(messages) >= 215, f"Expected at least 215 deltas, got {len(messages)}"

    # Check all 16 blank-amount events and all 215 messages are covered
    image_event_ids = {img["event_id"] for img in images}
    assert len(image_event_ids) == 16

    message_ids = {msg["message_id"] for msg in messages}
    assert len(message_ids) == 215, f"Expected all 215 messages covered, got {len(message_ids)}"


def test_target_invariants(extracted_data):
    """Test target invariant: target may ONLY be an event_id if related_event_id matches."""
    ds = DataStore()
    msg_by_id = {m.message_id: m for m in ds.messages}
    for d in extracted_data.get("message_deltas", []):
        m = msg_by_id[d["message_id"]]
        target = d.get("target")
        if target and target != "user_general":
            assert target == m.related_event_id, (
                f"{d['message_id']} has target={target} but related_event_id={m.related_event_id}"
            )
        if d.get("new_value") is None and d.get("action") != "confirm_no_change":
            assert d.get("confidence") == "low", f"Unquantifiable delta in {d['message_id']} must have confidence='low'"



def test_golden_image_values(extracted_data):
    """Test 1: All 16 images match verified golden values."""
    images = {img["event_id"]: img for img in extracted_data.get("image_extractions", [])}

    mismatches = []
    for event_id, expected in GOLDEN_IMAGE_AMOUNTS.items():
        assert event_id in images, f"Missing {event_id} in extractions"
        actual_str = images[event_id]["extracted_amount"]
        actual = Decimal(actual_str)
        if actual != expected:
            mismatches.append(f"{event_id}: expected {expected}, got {actual}")

    assert not mismatches, f"Golden image mismatches:\n" + "\n".join(mismatches)


def test_schema_validation(extracted_data):
    """Test 3: Schema validation for image extractions and message deltas."""
    # Validate images
    for img in extracted_data.get("image_extractions", []):
        assert "event_id" in img
        assert "extracted_amount" in img
        Decimal(img["extracted_amount"])  # must be Decimal-compatible
        assert img["confidence"] in ("high", "medium", "low")
        assert "currency" in img
        assert "reasoning" in img
        assert "source_image_id" in img

    # Validate messages
    valid_actions = {"confirm_no_change", "amend", "cancel", "delay", "new_fact"}
    for msg in extracted_data.get("message_deltas", []):
        assert "message_id" in msg
        action = msg.get("action")
        assert action in valid_actions, f"Invalid action: {action} in {msg['message_id']}"

        if action != "confirm_no_change":
            assert "user_id" in msg
            assert "target" in msg
            assert "field" in msg
            assert "confidence" in msg
            assert msg["confidence"] in ("high", "medium", "low")
            if msg.get("effective_date"):
                # must parse as YYYY-MM-DD
                date.fromisoformat(msg["effective_date"])
