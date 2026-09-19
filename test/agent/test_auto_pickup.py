#!/usr/bin/env python3
"""Pickup intent tests (destination-commitment plan, Phase 0 + Phase 3).

Run from the repository root:

    python3 -m unittest discover -s test/agent -p 'test_auto*.py'

Phase 0 (this file, engine-free): the pickup-shape vocabulary, the
native-vs-manual disposition partition (AC17/AC18) and the pure pickup row
model.  Protocol-shape verification itself is the native probe
(``make -C test/agent native-pickup``) or the mandatory operator-gated manual
probe for a non-constructible shape; the row-model tests here never claim it.
"""

import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pickup_shapes  # noqa: E402


class PickupShapeVocabulary(unittest.TestCase):
    def test_pickup_shape_disposition_is_exhaustive(self):
        """Every one of the eight shapes is dispositioned exactly once."""
        pickup_shapes.validate_partition()          # raises on a gap/overlap
        disp = pickup_shapes.disposition()
        native = disp["native_passed"]
        manual = disp["manual_required"]
        self.assertEqual(sorted(native + manual),
                         sorted(pickup_shapes.SHAPE_KEYS))
        self.assertEqual(len(set(native) & set(manual)), 0)
        # each shape's own record agrees with the sets
        for row in disp["shapes"]:
            expected = "native" if row["key"] in native else "manual"
            self.assertEqual(row["disposition"], expected)
        # the eight stable keys are distinct and titled
        self.assertEqual(len(set(pickup_shapes.SHAPE_KEYS)), 8)
        for key in pickup_shapes.SHAPE_KEYS:
            self.assertTrue(pickup_shapes.shape_title(key))

    def test_disposition_rejects_gap_and_overlap(self):
        with self.assertRaises(ValueError):
            pickup_shapes.validate_partition(native=("no_object",),
                                             manual=("multi_row_pick_any",))
        with self.assertRaises(ValueError):
            pickup_shapes.validate_partition(
                native=("no_object",), manual=("no_object",) +
                tuple(k for k in pickup_shapes.SHAPE_KEYS
                      if k != "no_object"))

    def test_pickup_protocol_shape_requires_native_or_manual_probe(self):
        """A shape is proven natively or requires the manual probe."""
        disp = pickup_shapes.disposition()
        self.assertEqual(disp["native_passed"], ["no_object"])
        # every non-native shape is explicitly manual-required, never dropped
        for key in pickup_shapes.SHAPE_KEYS:
            if key not in disp["native_passed"]:
                self.assertIn(key, disp["manual_required"])


class PickupRowModel(unittest.TestCase):
    """Pure row-model checks; they verify the row model, not the wire shape."""

    RAW_PILE = [
        {"r": 1, "text": "a dagger", "selectable": True},
        {"r": 2, "text": "2 food rations", "selectable": True},
        {"r": 3, "text": "some gold pieces", "selectable": True},
        {"r": 4, "text": "a shop item (unpaid)", "selectable": True},
        {"r": 5, "text": "", "selectable": False},
    ]

    def test_pickup_row_model_shapes_are_self_consistent(self):
        rows = pickup_shapes.parse_rows(self.RAW_PILE)
        self.assertEqual(len(rows), 5)
        # an unpaid row is never selectable for acquisition
        self.assertTrue(pickup_shapes.is_unpaid_row(rows[3]))
        self.assertNotIn(rows[3], pickup_shapes.selectable_rows(rows))
        # a uniquely authorized exact row is selected
        row, why = pickup_shapes.unique_authorized_row(
            rows, lambda t: "food ration" in t)
        self.assertIsNotNone(row)
        self.assertEqual(row.index, 2)
        # an empty predicate match is a decline
        row, why = pickup_shapes.unique_authorized_row(
            rows, lambda t: "no such item" in t)
        self.assertIsNone(row)
        # a broad predicate is a conservative pile cancellation
        row, why = pickup_shapes.unique_authorized_row(
            rows, lambda t: True)
        self.assertIsNone(row)
        self.assertIn("ambiguous", why)

    def test_capacity_prompt_classification(self):
        self.assertTrue(pickup_shapes.is_capacity_prompt(
            "Your backpack is getting hard to carry, continue? [yn]"))
        self.assertTrue(pickup_shapes.is_capacity_prompt(
            "You are now burdened.  Continue? [yn]"))
        self.assertFalse(pickup_shapes.is_capacity_prompt(
            "Really quit without saving? [yn]"))
        self.assertFalse(pickup_shapes.is_capacity_prompt(
            "Do you want your possessions identified? [ynq]"))


if __name__ == "__main__":
    unittest.main()
