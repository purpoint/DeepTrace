"""Making retrieved text safe to keep.

Everything this system stores as evidence was written by someone else. It is
quoted into a report, handed to a model, served over an API, and rendered in a
browser -- so the moment to make it safe is when it arrives, once, rather than
at each of those four places separately.

Two different dangers, and they need opposite instincts.

**Executable markup** is the familiar one. The browser client already refuses to
render HTML from a report, and that is the real defence; this is the layer
underneath it, for the consumers this project does not control -- the API is a
public surface, the CLI prints markdown to a terminal, and somebody will
eventually pipe a report into a renderer that is less careful. Storing inert
text means that pipe is safe by default rather than by their diligence.

**Hidden and reordered text** is the one that actually threatens *this* system.
DeepTrace's entire claim is that a person can check the work: read the quotation,
follow it to the page, see that it says what the report says it says. A
bidirectional override makes a stored sentence display in an order it was not
verified in, so the reader checks one sentence while the system checked another.
An HTML comment hides text from a reviewer that a model still read. Neither is
cross-site scripting, and neither is caught by anything that looks for it.

**What this deliberately does not do is strip markup wholesale.** DeepTrace
answers technical questions, so its sources are full of `List<String>`, `a < b`,
and XML fragments quoted as examples. Running every page through an HTML parser
would silently delete those -- turning `List<String>` into `List` -- and a
system that corrupts the evidence it stores has broken the thing it exists to
protect. So the removals below are a short list of constructs that are dangerous
or invisible, and every other character survives exactly as written.
"""

from __future__ import annotations

import re

# Elements whose *content* is not readable text and must go with them. A page's
# script body is not evidence, and leaving the text between the tags would put
# JavaScript into a quotation.
_EXECUTABLE_BLOCK = re.compile(
    r"<\s*(script|style|template|noscript|svg|math)\b[^>]*>.*?<\s*/\s*\1\s*>",
    re.IGNORECASE | re.DOTALL,
)

# An unclosed opener for the same elements. A page that opens a script and never
# closes it would otherwise leave a bare tag behind.
_EXECUTABLE_OPENER = re.compile(
    r"<\s*/?\s*(script|style|template|noscript|svg|math|iframe|object|embed|link|meta|base|form)\b[^>]*>",
    re.IGNORECASE,
)

# Comments and CDATA: text a reviewer reading the page never sees, which a model
# reading the source does. Removed because "what the human checked" and "what
# the machine read" being different is this project's worst failure mode.
_COMMENT = re.compile(r"<!--.*?-->|<!\[CDATA\[.*?\]\]>", re.DOTALL)

BIDI_CONTROLS = "‪‫‬‭‮⁦⁧⁨⁩‎‏"
"""Characters that change the order text is displayed in, and nothing else.

LRE, RLE, PDF, LRO, RLO, the four isolates, and the two directional marks. None
of them carry content: removing them cannot change what a sentence says, only
whether it displays in the order it was written. That asymmetry is why they can
be stripped outright while zero-width joiners cannot -- a joiner is load-bearing
in Persian, Hindi, and half the emoji in use.
"""

# C0 and C1 control characters, excluding tab and newline which are legitimate
# layout. A NUL or an escape sequence in stored text corrupts terminals, log
# pipelines, and anything that later writes it to a file.
_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

_BIDI = re.compile(f"[{BIDI_CONTROLS}]")


def strip_bidi(text: str) -> str:
    """Remove display-order controls, leaving every visible character."""
    return _BIDI.sub("", text)


def sanitize_untrusted(text: str) -> str:
    """Make text retrieved from the web safe to store, quote, and render.

    Lossless for anything legitimate. A page that contains code samples, angle
    brackets, or non-Latin scripts comes back character for character; only
    executable blocks, hidden text, display-order controls, and control
    characters are removed.

    Applied at every point where outside text enters the system -- fetched pages
    and search results alike -- because a control applied at one of two
    entrances is not a control.
    """
    if not text:
        return text

    text = _COMMENT.sub(" ", text)
    text = _EXECUTABLE_BLOCK.sub(" ", text)
    text = _EXECUTABLE_OPENER.sub(" ", text)
    text = strip_bidi(text)
    text = _CONTROLS.sub("", text)
    return text


__all__ = ["BIDI_CONTROLS", "sanitize_untrusted", "strip_bidi"]
