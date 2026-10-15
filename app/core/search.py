"""Text rules shared by every searchable or screened module.

Normalisation, full-text query building, and phrase screening live here so the
marketplace's prohibited-items check and the job-scam check cannot drift apart in
what counts as a match.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

_NON_WORD = re.compile(r"[^0-9a-zÀ-ɏ]+")
_QUERY_TERM = re.compile(r"[0-9a-zÀ-ɏ]+")

MAX_QUERY_TERMS = 8

# The full-text configuration for user content. `simple`: no stemming, because
# posts here mix English and Swahili, and an English stemmer helps one while
# mangling the other (decision 5.3).
TEXT_SEARCH_CONFIG = "simple"


def normalise(text: str) -> str:
    """Lower case, punctuation to spaces, spaces collapsed. "iPhone 11, 64GB!!" and
    "iphone 11 64gb" are the same text."""
    return " ".join(_NON_WORD.sub(" ", text.lower()).split())


def matching_phrases(text: str, phrases: Iterable[str]) -> list[str]:
    """Every phrase appearing in the text as whole words, in the order given.

    Whole words, so "rifle" does not match "rifleman" and "ada" does not match
    "adapter". Both sides are normalised, so punctuation and case never hide a
    match.
    """
    haystack = f" {normalise(text)} "
    found: list[str] = []
    for phrase in phrases:
        needle = normalise(phrase)
        if needle and f" {needle} " in haystack:
            found.append(phrase)
    return found


def prefix_tsquery(query: str) -> str | None:
    """A `tsquery` that prefix-matches every word: "sof cush" → `sof:* & cush:*`.

    Built from letters and digits only, so nothing a user types reaches the parser
    as syntax. None when there is nothing to search for.
    """
    terms = _QUERY_TERM.findall(query.lower())[:MAX_QUERY_TERMS]
    if not terms:
        return None
    return " & ".join(f"{term}:*" for term in terms)
