"""Unit tests for the narrowing helpers decoded JSON passes through.

Every reader of a workflow document or a cached API answer relies on these to
tell a shape it can read from one it must refuse, so a helper that answered for
a shape it was not given would let a malformed file through as a valid one.
"""

import unittest
from collections import OrderedDict

from github_cron_trigger.decoded import as_dict, as_list, as_mapping, as_str_list


class DecodedTest(unittest.TestCase):
    def test_each_helper_answers_only_for_its_own_shape(self) -> None:
        for value in (None, 3, "text", b"bytes", ("a",), {"a"}):
            with self.subTest(value=value):
                self.assertIsNone(as_mapping(value))
                self.assertIsNone(as_dict(value))
                self.assertIsNone(as_list(value))
                self.assertIsNone(as_str_list(value))

    def test_a_mapping_is_any_mapping_and_a_dict_only_a_dict(self) -> None:
        ordered: OrderedDict[str, int] = OrderedDict(a=1)
        self.assertEqual(as_mapping({"a": 1}), {"a": 1})
        self.assertEqual(as_mapping(ordered), ordered)
        self.assertEqual(as_dict({"a": 1}), {"a": 1})
        self.assertIsNone(as_dict(["a"]))
        self.assertIsNone(as_mapping(["a"]))

    def test_a_list_is_a_list_and_strings_only_when_every_item_is_one(self) -> None:
        self.assertEqual(as_list([1, "a"]), [1, "a"])
        self.assertEqual(as_list([]), [])
        self.assertEqual(as_str_list(["a", "b"]), ["a", "b"])
        self.assertEqual(as_str_list([]), [])
        self.assertIsNone(as_str_list(["a", 1]))
        self.assertIsNone(as_list({"a": 1}))


if __name__ == "__main__":
    unittest.main()
