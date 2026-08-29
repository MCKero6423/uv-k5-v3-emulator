#!/usr/bin/env python3
"""Unit tests for key-name validation."""
import os
import re
import unittest

from uvk5_keys import KEYS, is_valid, normalise


class TestKeys(unittest.TestCase):
    def test_matches_the_model_key_list(self):
        # keypad_key_names in qemu/py32f071.c
        self.assertEqual(set(KEYS), {
            "MENU", "UP", "DOWN", "EXIT", "F", "STAR",
            "0", "1", "2", "3", "4", "5", "6", "7", "8", "9",
            "SIDE1", "SIDE2",
        })

    def test_stays_in_sync_with_the_machine_model(self):
        """Read the real list out of py32f071.c so the two cannot drift.

        If someone adds a key to the model, this fails until KEYS is updated --
        which beats discovering it as a QMP error at runtime.
        """
        src = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           os.pardir, "qemu", "py32f071.c")
        text = open(src).read()
        block = re.search(
            r"keypad_key_names\[[^\]]*\]\s*=\s*\{(.*?)\};", text, re.S)
        self.assertIsNotNone(block, "could not find keypad_key_names in the model")
        names = set(re.findall(r'"([^"]+)"', block.group(1)))
        self.assertEqual(names, set(KEYS))

    def test_ptt_is_not_offered_as_a_key(self):
        # PTT works, but not through the key table: the firmware reads its own pin, so
        # it is a separate boolean property. Setting "press" to PTT would error.
        self.assertFalse(is_valid("PTT"))

    def test_normalise_is_case_insensitive_and_strips(self):
        self.assertEqual(normalise("menu"), "MENU")
        self.assertEqual(normalise("  up  "), "UP")
        self.assertTrue(is_valid("menu"))

    def test_rejects_unknown(self):
        self.assertFalse(is_valid("BANANA"))

    def test_rejects_empty_and_none(self):
        self.assertFalse(is_valid(""))
        self.assertFalse(is_valid(None))


if __name__ == "__main__":
    unittest.main()
