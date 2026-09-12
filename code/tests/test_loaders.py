"""
Tests for the data loaders — run against the ACTUAL dataset/ files.
No mocked data.
"""

from __future__ import annotations

import sys
import os
import unittest
from decimal import Decimal

# Ensure code/ is on sys.path regardless of cwd
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), "..")))

from lib.loaders import DataStore, dataset_dir


class TestDataStoreLoad(unittest.TestCase):
    """Verify the DataStore loads without exceptions and basic counts are right."""

    @classmethod
    def setUpClass(cls):
        cls.ds = DataStore()

    # ---- profile tests ----

    def test_profile_count(self):
        self.assertEqual(len(self.ds.profiles), 275)

    def test_profile_user_01(self):
        p = self.ds.get_profile("user_01")
        self.assertEqual(p.home_currency, "ZAR")
        self.assertEqual(p.current_available_balance, Decimal("58481.1"))
        self.assertEqual(p.minimum_balance_to_keep, Decimal("18000"))

    def test_profile_pipe_sets(self):
        p = self.ds.get_profile("user_01")
        self.assertEqual(
            p.financial_priorities,
            frozenset({"education", "debt_repayment"}),
        )
        self.assertEqual(
            p.expense_categories_to_protect,
            frozenset({"rent", "education", "groceries", "debt_repayment"}),
        )
        self.assertIn("dining", p.expense_categories_user_is_willing_to_reduce)
        self.assertIn(
            "delivery_membership",
            p.expense_categories_user_is_willing_to_stop,
        )

    def test_profile_payment_methods(self):
        p = self.ds.get_profile("user_01")
        self.assertEqual(p.payment_methods_user_will_consider, frozenset({"full_payment"}))

    def test_profile_max_installment_months_blank_is_none(self):
        """user_01 has blank max_installment_months → must be None, not 0."""
        p = self.ds.get_profile("user_01")
        self.assertIsNone(p.max_installment_months)

    def test_profile_max_installment_months_populated(self):
        """user_02 has max_installment_months=7."""
        p = self.ds.get_profile("user_02")
        self.assertEqual(p.max_installment_months, 7)

    # ---- event tests ----

    def test_event_01(self):
        events = self.ds.get_events_for_user("user_01")
        e01 = next(e for e in events if e.event_id == "event_01")
        self.assertEqual(e01.amount, Decimal("5148"))
        self.assertEqual(e01.currency, "ZAR")
        self.assertEqual(e01.flexibility, "fixed")

    def test_blank_amount_count(self):
        """Exactly 16 events have amount is None."""
        blank = [e for e in self.ds.events if e.amount is None]
        self.assertEqual(len(blank), 16)

    def test_blank_amount_has_currency(self):
        """Even when amount is blank, currency is always populated."""
        blank = [e for e in self.ds.events if e.amount is None]
        for e in blank:
            self.assertTrue(e.currency, f"{e.event_id} has empty currency")

    def test_amount_pending_extraction_property(self):
        blank = [e for e in self.ds.events if e.amount is None]
        for e in blank:
            self.assertTrue(e.amount_pending_extraction)
        non_blank = [e for e in self.ds.events if e.amount is not None]
        for e in non_blank[:10]:  # spot-check
            self.assertFalse(e.amount_pending_extraction)

    def test_flexibility_fixed_has_no_minimum(self):
        """An event with flexibility='fixed' has minimum_allowed_amount is None."""
        fixed_events = [e for e in self.ds.events if e.flexibility == "fixed"]
        self.assertTrue(len(fixed_events) > 0)
        for e in fixed_events:
            self.assertIsNone(
                e.minimum_allowed_amount,
                f"{e.event_id} is 'fixed' but has minimum_allowed_amount={e.minimum_allowed_amount}",
            )

    def test_flexibility_stoppable_has_no_minimum(self):
        """Events with flexibility='stoppable' have minimum_allowed_amount is None."""
        stoppable_events = [e for e in self.ds.events if e.flexibility == "stoppable"]
        self.assertTrue(len(stoppable_events) > 0)
        for e in stoppable_events:
            self.assertIsNone(
                e.minimum_allowed_amount,
                f"{e.event_id} is 'stoppable' but has minimum_allowed_amount={e.minimum_allowed_amount}",
            )

    def test_reducible_has_minimum(self):
        """Events with flexibility in {reducible, reducible_or_stoppable}
        always have a populated minimum_allowed_amount."""
        reducible = [
            e for e in self.ds.events
            if e.flexibility in ("reducible", "reducible_or_stoppable")
        ]
        self.assertTrue(len(reducible) > 0)
        for e in reducible:
            self.assertIsNotNone(
                e.minimum_allowed_amount,
                f"{e.event_id} is '{e.flexibility}' but minimum_allowed_amount is None",
            )

    def test_reducible_count(self):
        """2907 events have flexibility in {reducible, reducible_or_stoppable}."""
        reducible = [
            e for e in self.ds.events
            if e.flexibility in ("reducible", "reducible_or_stoppable")
        ]
        self.assertEqual(len(reducible), 2907)

    def test_total_event_count(self):
        self.assertEqual(len(self.ds.events), 25342)

    # ---- images ↔ blank-amount events 1:1 ----

    def test_images_count(self):
        self.assertEqual(len(self.ds.images), 16)

    def test_images_and_blank_amount_events_are_1_to_1(self):
        """The 16 images with related_event_id map 1:1 to the 16 events
        with blank amount."""
        blank_event_ids = {
            e.event_id for e in self.ds.events if e.amount is None
        }
        image_event_ids = {
            img.related_event_id
            for img in self.ds.images
            if img.related_event_id is not None
        }
        self.assertEqual(blank_event_ids, image_event_ids)

    def test_image_resolve_path(self):
        """Every image resolves to an existing .png file."""
        dd = dataset_dir()
        for img in self.ds.images:
            path = img.resolve_path(dd)
            self.assertTrue(os.path.isfile(path), f"Missing: {path}")

    # ---- payment options ----

    def test_payment_methods_only_two(self):
        """Only 'full_payment' and 'installments' appear."""
        methods = {o.payment_method for o in self.ds.payment_options}
        self.assertEqual(methods, {"full_payment", "installments"})

    # ---- messages ----

    def test_message_count(self):
        self.assertEqual(len(self.ds.messages), 215)

    def test_messages_with_request_id(self):
        with_req = [m for m in self.ds.messages if m.request_id is not None]
        self.assertEqual(len(with_req), 128)

    def test_messages_with_related_event_id(self):
        with_event = [m for m in self.ds.messages if m.related_event_id is not None]
        self.assertEqual(len(with_event), 39)

    # ---- requests ----

    def test_request_count(self):
        self.assertEqual(len(self.ds.requests), 250)

    def test_sample_request_count(self):
        self.assertEqual(len(self.ds.sample_requests), 25)

    # ---- accessor functions ----

    def test_get_events_for_user(self):
        events = self.ds.get_events_for_user("user_01")
        self.assertTrue(len(events) > 0)
        for e in events:
            self.assertEqual(e.user_id, "user_01")

    def test_get_payment_options_for_request(self):
        opts = self.ds.get_payment_options_for_request("request_01")
        self.assertTrue(len(opts) >= 2)
        for o in opts:
            self.assertEqual(o.request_id, "request_01")

    def test_get_messages_for_request(self):
        msgs = self.ds.get_messages_for_request("request_03")
        self.assertTrue(len(msgs) > 0)
        for m in msgs:
            self.assertEqual(m.request_id, "request_03")

    def test_get_messages_for_user_general(self):
        """user_02/message_01 has no request_id and no related_event_id →
        should appear in general user messages."""
        general = self.ds.get_messages_for_user_general("user_02")
        ids = [m.message_id for m in general]
        self.assertIn("message_01", ids)

    def test_get_image_for_event(self):
        img = self.ds.get_image_for_event("event_253")
        self.assertIsNotNone(img)
        self.assertEqual(img.image_id, "image_01")

    def test_get_image_for_event_missing(self):
        img = self.ds.get_image_for_event("event_01")
        self.assertIsNone(img)

    def test_get_images_for_request(self):
        imgs = self.ds.get_images_for_request("request_03")
        self.assertTrue(len(imgs) > 0)

    def test_profile_missing_raises(self):
        with self.assertRaises(KeyError):
            self.ds.get_profile("nonexistent_user")


if __name__ == "__main__":
    unittest.main()
