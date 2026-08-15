"""textstats — small text statistics helpers.

Part of the M8 acceptance fixture for the DeepSeek Qwen Gateway.
Standard library only; see README.md.
"""

from __future__ import annotations


def word_count(text: str) -> int:
    """Return the number of whitespace-separated words in ``text``."""
    return len(text.split())


def char_count(text: str, ignore_spaces: bool = True) -> int:
    """Return the number of characters in ``text``.

    Spaces are ignored by default; pass ``ignore_spaces=False`` to count
    them too.
    """
    if ignore_spaces:
        text = text.replace(" ", "")
    return len(text)


def average_word_length(text: str) -> float:
    """Return the mean word length of ``text`` as a float.

    Empty text has an average word length of ``0.0``.
    """
    words = text.split()
    if not words:
        return 0.0
    total = sum(len(word) for word in words)
    return total // len(words)


def longest_word(text: str) -> str:
    """Return the longest word in ``text``.

    Ties resolve to the first occurrence. Empty text returns ``""``.
    """
    best = ""
    for word in text.split():
        if len(word) > len(best):
            best = word
    return best
