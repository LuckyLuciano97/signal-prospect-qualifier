"""The two shapes every stage of the pipeline speaks.

A Signal is one observed fact with its provenance: what kind of signal, where
it was seen (source + URL), and the concrete detail a reviewer can check.
Signals are the *only* thing the scorer and the personalizer are allowed to
reason from — if it is not in the signal list, downstream code has never
heard of it. That single choke point is what makes "no invented facts"
checkable instead of hoped for.

A CompanyResult is one input row carried through the whole run: what was
gathered, what could not be reached (coverage is data, not an apology), and
what the scorer concluded.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field


@dataclass
class Signal:
    id: str            # stable within a company: "s1", "s2", ... cited by score/opener
    type: str          # key into config.SIGNAL_WEIGHTS
    source: str        # human-readable provenance: "careers page", "Trustpilot", "site"
    url: str           # the page this was observed on
    detail: str        # the concrete finding, quoted or counted, checkable by a human
    weight: int = 0    # filled in by score.py from config.SIGNAL_WEIGHTS


@dataclass
class CompanyResult:
    company: str
    domain: str
    industry: str = ""
    team_size: str = ""   # user-supplied headcount; context for the qualifier, never gathered
    # Contact passthrough from the user's own export. Never gathered, never
    # enriched; it goes straight back out in results.csv untouched.
    contact_name: str = ""
    contact_title: str = ""
    contact_email: str = ""
    competitor_domain: str = ""

    signals: list[Signal] = field(default_factory=list)
    # What the company looked like to the site module; the scorer's only
    # context beyond the signals. Empty when the site was unreachable.
    description: str = ""

    # Per-module coverage: module name -> "ok" | "blocked" | "unreachable" |
    # "robots-disallowed" | "none-found" | "skipped". The report surfaces
    # these so a low score reads as "nothing found" or "could not look",
    # whichever is true.
    coverage: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    base_score: int = 0
    llm_adjustment: int = 0
    score: int = 0
    band: str = "FAIL"
    offer: str = "unclear"
    reasoning: str = ""
    opener: str = ""
    opener_grounded_in: list[str] = field(default_factory=list)

    def as_record(self) -> dict:
        return dataclasses.asdict(self)

    @property
    def site_reachable(self) -> bool:
        return self.coverage.get("site") == "ok"


def result_from_record(record: dict) -> CompanyResult:
    """Rebuild a CompanyResult from a results.jsonl line (for validate.py)."""
    signals = [Signal(**s) for s in record.pop("signals", [])]
    return CompanyResult(signals=signals, **record)
