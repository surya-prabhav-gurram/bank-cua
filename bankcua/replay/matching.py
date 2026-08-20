"""
Boundary-aware matching for checkpoints and detectors.

Lives apart from the surfaces so both of them -- and any future one --
answer "did we land where we meant to" identically. A surface that
disagreed with another about that would make an artifact's guarantees
depend on which driver replayed it.
"""
from __future__ import annotations

def url_match(expected: str, actual: str) -> bool:
    """Does `actual` contain `expected` as a WHOLE path/query component?

    Plain substring containment is the obvious reading and it is unsafe here,
    because checkpoint values are parameterised. A lookup asked for member
    `1002` lands on `/members/100234`, and `"/members/1002" in
    "/members/100234"` is true -- so the run reported SUCCESS and returned a
    different member's balances. On a banking surface that is the worst failure
    available: not an error, an answer about the wrong person.

    A match must therefore end on a boundary -- end of string, or one of `/?&#`
    -- which keeps every legitimate use working (`/menu`, `/members`,
    `/members?by=number&q=100234`) while rejecting a value that is merely a
    prefix of a different one.
    """
    start = 0
    while True:
        i = actual.find(expected, start)
        if i == -1:
            return False
        end = i + len(expected)
        if end == len(actual) or actual[end] in "/?&#":
            return True
        start = i + 1


def text_match(expected: str, haystack: str) -> bool:
    """Does `haystack` contain `expected` as a WHOLE token?

    The same defect as `url_match`, on page text instead of a URL: a member
    record for 100234 "contains" the string 1002, so a check for the member we
    asked about passes while we look at somebody else's account.

    A match must begin and end on a non-word boundary. Every condition string
    this system ships -- "TRANSACTION REJECTED", "Insufficient available
    balance", "is not authorized to perform this" -- is bounded by whitespace,
    punctuation or the end of the page, so this is stricter without being
    narrower in practice.
    """
    if not expected:
        return False
    start = 0
    while True:
        i = haystack.find(expected, start)
        if i == -1:
            return False
        before_ok = i == 0 or not (haystack[i - 1].isalnum()
                                   or haystack[i - 1] in "_-")
        end = i + len(expected)
        after_ok = end == len(haystack) or not (haystack[end].isalnum()
                                                or haystack[end] in "_-")
        if before_ok and after_ok:
            return True
        start = i + 1
