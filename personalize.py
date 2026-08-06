"""Draft one honest opening line per PASS/MAYBE company.

The prompt hands the model the gathered signals and nothing else, so the
only facts available to reference are ones a page actually showed us. The
model must also return which signal ids the line rests on; a line grounded
in nothing is discarded on the spot (and validate.py checks the survivors
again, independently).

Style rules live in the system prompt because they are product decisions,
not taste: reference the actual signal, lead with their pain rather than
our service, short and plain, no hype, no dashes, and it is an opener — the
user writes the rest of the email and sends it themselves. The tool sends
nothing.
"""

from __future__ import annotations

import json
import logging

import config
from llm import AnthropicEngine, LLMError
from model import CompanyResult

log = logging.getLogger("signal.personalize")

# No minItems: the structured-output API rejects range keywords, so "must
# cite at least one signal" is enforced by the discard check below instead.
OPENER_SCHEMA = {
    "type": "object",
    "properties": {
        "opener": {"type": "string"},
        "grounded_in": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["opener", "grounded_in"],
    "additionalProperties": False,
}

OPENER_SYSTEM = """You draft the first one or two sentences of a cold email for Nexis Studio,
a small automation studio. The user will write the rest and send it personally.

You get the signals one research run found for one company. Rules, all hard:

- Reference at least one actual signal by its content. "Noticed you have four
  open support roles and recent reviews mention waiting days for a reply" is
  the register. Generic compliments are forbidden.
- Lead with their pain or the outcome, never with what Nexis Studio does or
  sells. Do not name the offer, do not pitch, do not include a call to action.
- Never state anything that is not in the signals. If a fact is not in the
  list, it does not exist. No guesses about their size, growth, or revenue.
- Describe what was observed; do not present your own inference as a fact
  about them. Words like "clearly", "obviously", "must be" are the tell that
  a sentence has crossed from observation into guessing. One soft reading of
  what the signals suggest is fine; a verdict is not.
- Plain short sentences a person would write after 30 seconds of genuine
  homework. No hype words, no exclamation marks, no dashes of any kind.
- Return grounded_in: the ids of the signals your line uses. Only real ids.
"""


def draft_opener(engine: AnthropicEngine, result: CompanyResult) -> None:
    if result.band not in config.DRAFT_OPENERS_FOR or not result.signals:
        return

    evidence = "\n".join(f"  {s.id} ({s.source}): {s.detail}" for s in result.signals)
    user = (f"Company: {result.company} ({result.domain})\n"
            f"What they do, scraped from their site:\n"
            f"{result.description[:400] or '(unavailable)'}\n\n"
            f"Signals:\n{evidence}")
    try:
        raw = engine.complete(OPENER_SYSTEM, user, schema=OPENER_SCHEMA, effort="medium")
        payload = json.loads(raw)
    except (LLMError, ValueError) as exc:
        log.error("opener draft failed for %s: %s", result.company, exc)
        result.notes.append("opener: drafting failed; write one from the evidence above")
        return

    valid_ids = {s.id for s in result.signals}
    grounded = [g for g in payload.get("grounded_in", []) if g in valid_ids]
    opener = " ".join(str(payload.get("opener", "")).split())
    # Belt to the prompt's braces: dashes are banned in outreach copy.
    for dash in ("—", "–", " - "):
        opener = opener.replace(dash, ", ")

    if not opener or not grounded:
        log.warning("%s: opener discarded (grounded in nothing)", result.company)
        result.notes.append("opener: model draft cited no gathered signal; discarded")
        return

    result.opener = opener
    result.opener_grounded_in = grounded
