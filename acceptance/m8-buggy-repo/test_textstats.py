"""Unittest suite for textstats — stdlib only, deterministic (M8 fixture).

Run from this directory:

    python -m unittest discover -v
"""

from __future__ import annotations

import unittest

import textstats


class WordCountTests(unittest.TestCase):
    def test_counts_whitespace_separated_words(self) -> None:
        self.assertEqual(textstats.word_count("the quick brown fox"), 4)

    def test_empty_text_has_no_words(self) -> None:
        self.assertEqual(textstats.word_count(""), 0)


class CharCountTests(unittest.TestCase):
    def test_ignores_spaces_by_default(self) -> None:
        self.assertEqual(textstats.char_count("a b c"), 3)

    def test_counts_spaces_when_asked(self) -> None:
        self.assertEqual(textstats.char_count("a b c", ignore_spaces=False), 5)


class AverageWordLengthTests(unittest.TestCase):
    def test_whole_number_average(self) -> None:
        # "aa" (2) and "bbbb" (4): average 3.0
        self.assertEqual(textstats.average_word_length("aa bbbb"), 3.0)

    def test_fractional_average(self) -> None:
        # "a" (1) and "bb" (2): average 1.5
        self.assertEqual(textstats.average_word_length("a bb"), 1.5)

    def test_empty_text_returns_zero(self) -> None:
        self.assertEqual(textstats.average_word_length(""), 0.0)


class LongestWordTests(unittest.TestCase):
    def test_returns_the_longest_word(self) -> None:
        self.assertEqual(textstats.longest_word("a bb ccc dd"), "ccc")

    def test_tie_resolves_to_first_occurrence(self) -> None:
        self.assertEqual(textstats.longest_word("ab cd"), "ab")


if __name__ == "__main__":
    unittest.main()
