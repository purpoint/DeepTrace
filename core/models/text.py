"""Comparing two statements without asking a model.

Several places need to know whether two pieces of text say the same thing: the
planner refuses duplicate tasks, and the claim layer merges findings that repeat
each other. Both need the answer to be deterministic and free -- these run on
every plan and every analysis, and a check that costs an API call and varies
between runs is the wrong shape for a validation rule.

One definition, in one place. Two modules each with their own idea of "the same
question" eventually disagree, and the disagreement shows up as a duplicate that
one layer removed and the other kept.
"""

from __future__ import annotations

import re

WORD = re.compile(r"[a-z0-9]+")

# Words carrying no topical signal, removed before comparing two task questions.
STOPWORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "do",
        "does",
        "did",
        "of",
        "for",
        "to",
        "in",
        "on",
        "at",
        "by",
        "with",
        "from",
        "about",
        "into",
        "over",
        "after",
        "under",
        "and",
        "or",
        "but",
        "if",
        "then",
        "than",
        "that",
        "this",
        "these",
        "those",
        "it",
        "its",
        "as",
        "how",
        "what",
        "which",
        "who",
        "whom",
        "when",
        "where",
        "why",
        "can",
        "could",
        "should",
        "would",
        "will",
        "shall",
        "may",
        "might",
        "must",
        "have",
        "has",
        "had",
        "between",
        "across",
    ]
)


def stem(word: str) -> str:
    """Strip a trailing plural so ``messages`` and ``message`` compare equal.

    Deliberately minimal. It exists to solve one specific problem: without it,
    a genuine duplicate phrased with a plural ("ordering of messages" versus
    "message ordering") and a legitimate symmetric comparison pair ("Kafka
    ordering" versus "RabbitMQ ordering") both differ by exactly one token and
    score identically. No threshold can separate them.

    Stemming resolves it because the differing token is morphological in the
    first case and semantic in the second: after stemming, the duplicate scores
    1.0 while the comparison pair is unchanged.

    A full stemmer would be more accurate and much harder to reason about. This
    handles the case that actually arises in research questions.
    """
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def content_words(text: str) -> frozenset[str]:
    """The topical words of a statement, stemmed, for comparison.

    Falls back to the unfiltered words when a statement is nothing but
    stopwords, so a short statement compares as itself rather than as the empty
    set -- which would match everything.
    """
    words = WORD.findall(text.lower())
    meaningful = frozenset(stem(word) for word in words if word not in STOPWORDS)
    return meaningful or frozenset(stem(word) for word in words)


_SUFFIXES = ("ingly", "edly", "ing", "ies", "ied", "ed", "es", "ly", "s")


def fold(word: str) -> str:
    """Reduce a word to a rough root, for retrieval rather than for comparison.

    More aggressive than :func:`stem`, deliberately, because the two are used
    where the errors cost different amounts.

    Duplicate detection asks "are these the same question", and a wrong yes
    deletes a task that should have run -- so it stems minimally and errs
    towards treating words as distinct.

    Retrieval asks "might this passage bear on this claim", and a wrong no hides
    a passage that contradicts it, which is the failure verification exists to
    catch. Missing "ordering" because the claim said "order" is exactly that
    failure, so this folds harder and errs towards treating words as related.

    Crude by design. A real stemmer is more accurate and much harder to reason
    about, and the cost of a false match here is one extra passage in a prompt.
    """
    lowered = word.lower()
    for suffix in _SUFFIXES:
        if len(lowered) - len(suffix) >= 4 and lowered.endswith(suffix):
            return lowered[: -len(suffix)]
    return lowered


def retrieval_words(text: str) -> frozenset[str]:
    """Content words folded for retrieval. See :func:`fold`."""
    words = WORD.findall(text.lower())
    meaningful = frozenset(fold(word) for word in words if word not in STOPWORDS)
    return meaningful or frozenset(fold(word) for word in words)


def similarity(left: frozenset[str], right: frozenset[str]) -> float:
    """Jaccard similarity: shared words over total distinct words."""
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


NEGATIONS = frozenset(
    [
        "not",
        "never",
        "no",
        "none",
        "cannot",
        "cant",
        "doesnt",
        "dont",
        "isnt",
        "arent",
        "wont",
        "without",
        "fails",
        "unsupported",
        "unlike",
        "except",
    ]
)
"""Words whose presence flips a statement's meaning.

Token overlap cannot see them. "Order is preserved across partitions" and
"order is not preserved across partitions" share every content word and score as
near-identical, so a merge based on similarity alone would collapse a
disagreement into a single claim -- deleting exactly the finding a reader most
needs. Negation is checked separately for that reason.
"""


def negation_mismatch(left: str, right: str) -> bool:
    """Whether one statement negates and the other does not.

    Deliberately crude: it asks whether the two texts disagree about the
    presence of negation, not what is being negated. A false positive leaves two
    similar statements unmerged, which costs a duplicate line in a report. A
    false negative merges a claim with its opposite, which is a lie. The
    asymmetry is the whole design.
    """
    left_words = set(WORD.findall(left.lower()))
    right_words = set(WORD.findall(right.lower()))
    return bool(left_words & NEGATIONS) != bool(right_words & NEGATIONS)
