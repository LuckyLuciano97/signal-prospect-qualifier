"""The four signal-gathering modules and their tiny shared toolkit.

Each module exposes ``gather(client, result, ...)``, appends
:class:`model.Signal` objects to the result, and records its own coverage
honestly: ``ok`` (looked and found), ``none-found`` (looked, nothing there),
``blocked`` / ``unreachable`` / ``robots-disallowed`` (could not look), or
``skipped`` (toggled off, or missing an input it needs). Downstream code
treats "could not look" and "nothing there" differently, so modules never
conflate them.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from model import CompanyResult, Signal


def add_signal(result: CompanyResult, type_: str, source: str, url: str, detail: str) -> Signal:
    """Append a signal with the next stable id ("s1", "s2", ...)."""
    signal = Signal(id=f"s{len(result.signals) + 1}", type=type_,
                    source=source, url=url, detail=detail)
    result.signals.append(signal)
    return signal


def visible_text(html: str) -> str:
    """Page text as a person would read it, whitespace collapsed.

    A body that opens with a JSON delimiter is not a web page, whatever the
    content-type header claims - and site builders do claim text/html while
    returning their config document. Parsed as HTML it yields tens of
    thousands of characters of stylesheet, which then reach the detectors and
    the entity gate as if they were company copy. Refusing it here means the
    company is recorded as unreadable, which is true, instead of being
    described from its own CSS.
    """
    if html.lstrip()[:1] in ("{", "["):
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "template", "svg"]):
        tag.extract()
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()


def excerpt(text: str, match: re.Match, radius: int = 60) -> str:
    """A short checkable quote around a regex match, ellipsised at both ends."""
    start = max(0, match.start() - radius)
    end = min(len(text), match.end() + radius)
    snippet = re.sub(r"\s+", " ", text[start:end]).strip()
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text) else ""
    return f"{prefix}{snippet}{suffix}"
