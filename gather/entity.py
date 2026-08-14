"""Module 0 — is this entity even a business that could buy?

Runs after the site is read and **before** anything is scored. The scorer
measures whether a *site* looks manual; it has no concept of whether the
*entity* behind it is a prospect. In the batch-2 run that gap put a trade
association, an insurance trade publisher and an SIAA agency network into a
list of prospects, one of them at rank 4.

A disqualified company gets no score and no model qualification call. There
is nothing to qualify: a trade association is not going to buy a support
triage agent no matter how manual its certificate process looks.

Three deliberate design choices, each paid for by a real mistake:

* **Evidence or it did not happen.** Every non-target verdict carries a
  quote that must appear verbatim in the fetched page text. ``validate.py``
  re-checks the substring independently. A verdict the page does not support
  is discarded and the company is treated as ``unclear``.
* **Never disqualify on a name.** The spec that prompted this module named
  "Brown and Brown" as a national brokerage to filter out. The company in
  the run was a 20-person independent agency in Auburn, Indiana that happens
  to share the name; a name blocklist would have thrown away a genuine
  prospect. National-scale is therefore judged on what the site *says about
  its own footprint*, never on the name alone.
* **``unclear`` is an answer.** It is surfaced to the operator as a review
  item rather than scored or silently dropped.
"""

from __future__ import annotations

import json
import logging
import re

import config
from llm import AnthropicEngine, LLMError
from model import CompanyResult

log = logging.getLogger("signal.entity")

#: ``unassessed`` is not in this list on purpose: it is set by the coverage
#: guard below, never chosen by the model.
ENTITY_TYPES = [
    "target",          # an operating business of the kind we sell to
    "association",     # trade body, member organisation
    "publisher",       # media, directory, ratings, trade press
    "network",         # aggregator, cluster, franchise network
    "national_brand",  # multi-state/enterprise, has procurement
    "vendor",          # sells software/services into this niche
    "wholesaler",      # MGA/wholesale broker; its customers are retail agents
    "unclear",
]

# Deterministic pre-filters. Each maps a pattern to the type it suggests.
# These do not decide alone: a hit means "ask the model, with this in mind",
# except where the phrase is unambiguous (see STRONG below).
PREFILTERS = {
    "association": re.compile(
        r"\b(trade association|member(?:ship)? (?:benefits|dues|directory)|join or renew"
        r"|our members|find a member|become a member|chapter|governing board"
        r"|legislative advocacy|big ?i\b|independent insurance agents of)\b", re.I),
    "publisher": re.compile(
        r"\b(magazine|journal|publishing|publisher|editorial|media kit|subscribe"
        r"|subscription|advertise with us|our publications|newsstand)\b", re.I),
    "network": re.compile(
        r"\b(siaa|agency network|member agencies|aggregator|cluster|franchise"
        r"|benefits of membership|join our network|master agency)\b", re.I),
    "national_brand": re.compile(
        r"\b(\d{2,3}\+? offices|offices (?:nationwide|across the country)"
        r"|in all 50 states|fortune \d+|global headquarters|nyse:|nasdaq:)\b", re.I),
    "vendor": re.compile(
        r"\b(agency management system|our software|request a demo|pricing plans"
        r"|for insurance agencies\b[^.!?]{0,40}\bplatform)\b", re.I),
    "wholesaler": re.compile(
        r"\b(wholesale (?:insurance|broker|brokerage|distribution)"
        r"|managing general agent|retail agents?|retail (?:agency )?partners"
        r"|program administrator|binding authority)\b", re.I),
}

# Phrases decisive enough to skip the model entirely.
#
# Ordered most-specific first, and evaluated in this order on purpose: an
# agency network and a trade association share almost all of their membership
# vocabulary, so the generic "our members" family cannot be allowed to claim a
# page that also says "the SIAA difference". The regression test caught exactly
# this - Underwriters Alliance was correctly disqualified but labelled an
# association instead of a network.
STRONG: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Wholesalers first, and for the same reason: J.M. Wilson is a wholesale
    # brokerage whose page carries a newsletter signup, so "subscribe to" won
    # and it was disqualified as a publisher - right answer, wrong reason.
    # "retail agent" is the decisive tell: a wholesaler calls its customers
    # that, a retail agency never does. "surplus lines" is deliberately
    # absent, because retail agencies place surplus lines business too.
    ("wholesaler", re.compile(r"\b(wholesale (?:insurance|broker|brokerage)"
                              r"|managing general agent|retail agents?"
                              r"|binding authority)\b", re.I)),
    ("network", re.compile(r"\b(the siaa difference|siaa|member agencies"
                           r"|benefits of membership|our network of agencies)\b", re.I)),
    ("publisher", re.compile(r"\b(media kits?|our publications|latest issue"
                             r"|subscribe to)\b", re.I)),
    ("association", re.compile(r"\b(join or renew|find a member|membership benefits"
                               r"|member benefits overview|code of conduct)\b", re.I)),
)

ENTITY_SCHEMA = {
    "type": "object",
    "properties": {
        "is_target": {"type": "boolean"},
        "entity_type": {"type": "string", "enum": ENTITY_TYPES},
        "evidence_quote": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["is_target", "entity_type", "evidence_quote", "reason"],
    "additionalProperties": False,
}

ENTITY_SYSTEM = """You decide whether an entity is an operating business that could
buy automation services, or something else wearing similar vocabulary.

You get a company name, its domain, and the visible text of its homepage. Judge
only from that text.

entity_type must be one of:
- target: an operating business that serves its own customers (the kind of
  company that could hire a small automation studio)
- association: a trade body or member organisation; its "customers" are member
  companies who pay dues
- publisher: media, trade press, directory, ratings or catalogue business
- network: an aggregator, cluster, franchise or affiliation network whose
  members are themselves businesses of the target type
- national_brand: a multi-state or enterprise-scale operation with formal
  procurement, judged by what the site says about its own footprint
- vendor: sells software or services into this same niche
- wholesaler: a wholesale broker, managing general agent or program
  administrator whose customers are other agents, not the public. A page that
  talks about serving "retail agents", binding authority or wholesale
  distribution is describing this, not a retail agency.
- unclear: the text does not settle it

Rules:
- evidence_quote must be copied VERBATIM from the homepage text you were given,
  10 to 200 characters, and must be the specific phrase that decides your answer.
  It is checked against the page automatically; an invented quote voids your verdict.
- Do NOT judge by company name. Small local businesses often share a name with a
  large national firm. Only the site's own description of itself counts.
- Being small, old-fashioned, or having a dated website does NOT make something a
  non-target. Those are the targets.
- When the text genuinely does not settle it, answer unclear. That is a useful
  answer, not a failure.
"""


def _prefilter(name: str, domain: str, text: str) -> tuple[str | None, str | None, bool]:
    """(suggested_type, matched_quote, decisive). Name is never decisive alone."""
    haystack = f"{name} {text}"
    for entity_type, pattern in STRONG:
        on_page = pattern.search(text)  # must be on the page, not just in the name
        if on_page:
            excerpt = text[max(0, on_page.start() - 60):on_page.end() + 60].strip()
            return entity_type, excerpt or on_page.group(0), True
    for entity_type, pattern in PREFILTERS.items():
        match = pattern.search(haystack)
        if match:
            on_page = pattern.search(text)
            if on_page:
                excerpt = text[max(0, on_page.start() - 60):on_page.end() + 60].strip()
                return entity_type, excerpt, False
            return entity_type, None, False
    return None, None, False


def check(result: CompanyResult, homepage_text: str,
          engine: AnthropicEngine | None) -> None:
    """Set entity_type / is_target / entity_evidence on the result."""
    if result.coverage.get("site") != "ok" or not homepage_text:
        # "We could not look" is not the same fact as "we looked and cannot
        # tell", and only the second is a review item. Conflating them sent
        # unreachable sites to REVIEW and hid the coverage failure behind an
        # entity question.
        result.entity_type = "unassessed"
        result.is_target = True  # never disqualify on a page we could not read
        result.notes.append("entity: site unreadable, entity type not assessed")
        return

    text = homepage_text
    suggested, quote, decisive = _prefilter(result.company, result.domain, text)

    if decisive and suggested and quote:
        result.entity_type = suggested
        result.is_target = False
        result.entity_evidence = quote[:300]
        log.info("%s disqualified as %s (deterministic)", result.company, suggested)
        return

    if engine is None:
        # Rules-only mode: a suggestion without a model verdict is not enough
        # to disqualify, so flag it for the operator instead of guessing.
        result.entity_type = "unclear" if suggested else "target"
        result.is_target = True
        if suggested:
            result.notes.append(f"entity: '{suggested}' pattern present but no model "
                                f"available to confirm; not disqualified")
        return

    hint = (f"\nA keyword pre-filter suggests this may be a '{suggested}'. "
            f"Confirm or reject it from the text." if suggested else "")
    user = (f"Company: {result.company}\nDomain: {result.domain}\n"
            f"Industry (from the user's list): {result.industry or 'not given'}\n"
            f"{hint}\n\nHomepage text:\n{text[:6000]}")
    try:
        raw = engine.complete(ENTITY_SYSTEM, user, schema=ENTITY_SCHEMA, effort="low")
        verdict = json.loads(raw)
    except (LLMError, ValueError) as exc:
        log.error("entity check failed for %s: %s", result.company, exc)
        result.entity_type = "unclear"
        result.is_target = True
        result.notes.append("entity: check failed; company kept and scored")
        return

    entity_type = verdict.get("entity_type", "unclear")
    quote = (verdict.get("evidence_quote") or "").strip()
    is_target = bool(verdict.get("is_target"))

    # The quote must actually be on the page. Whitespace is normalised because
    # the model reflows text; nothing else is forgiven.
    def _norm(s: str) -> str:
        return re.sub(r"\s+", " ", s).strip().lower()

    grounded = len(quote) >= 10 and _norm(quote) in _norm(text)
    if not is_target and not grounded:
        log.warning("%s: disqualification quote not found on page, treating as unclear",
                    result.company)
        result.notes.append("entity: model's disqualifying quote was not found verbatim "
                            "on the page; verdict discarded")
        result.entity_type = "unclear"
        result.is_target = True
        return

    result.entity_type = entity_type
    # "unclear" never disqualifies, whatever the boolean says: the whole point
    # of the type is that the page did not settle the question, and a company
    # is only ever removed by positive evidence. A model verdict of
    # is_target=false with type=unclear sent a real agency to DISQUALIFIED
    # before this line existed.
    result.is_target = is_target or entity_type in ("target", "unclear")
    result.entity_evidence = quote[:300] if grounded else ""
    if not result.is_target:
        log.info("%s disqualified as %s", result.company, entity_type)
    elif entity_type == "unclear":
        result.notes.append("entity: type unclear from the homepage; review before "
                            "contacting")
