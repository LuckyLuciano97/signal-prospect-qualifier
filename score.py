"""Evidence-weighted scoring with a bounded LLM qualification on top.

The split of labour, and why it is split this way:

* **The rules produce the base score.** Each gathered signal carries the
  fixed weight config assigns its type (first instance counts; capability
  gaps may stack to a cap); the base is the capped sum. Two companies with
  the same evidence get the same base, mechanically, and the report shows
  the addition.
* **The model produces judgement, inside a fence.** It reads the gathered
  evidence — only the gathered evidence — maps it to the one Nexis offer it
  best supports, writes the one-line reasoning, and may adjust the base by
  at most ±LLM_ADJUSTMENT_CAP for relevance the rules cannot see (five open
  "support" roles at a company that *sells* support software is a weaker
  buy-signal than three at a company that sells shoes). Every adjustment
  must cite signal ids; a positive adjustment citing nothing is zeroed.
* **A company with no gathered evidence is never sent to the model.** Its
  score is 0 and its reasoning says "no concrete signal found" (or "could
  not look", when coverage says so). There is nothing legitimate a model
  could add to an empty observation, so it is not asked.

``--no-llm`` runs the rules alone with a template reasoning line, so the
pipeline stays runnable without a key; openers are skipped in that mode.
"""

from __future__ import annotations

import json
import logging

import config
from llm import AnthropicEngine, LLMError
from model import CompanyResult

log = logging.getLogger("signal.score")

# Only the schema keywords the structured-output API accepts (the triage
# repo learned the hard way that range keywords 400): the numeric bounds on
# `adjustment` are enforced by the clamp in score_company instead.
QUALIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "offer": {"type": "string", "enum": list(config.OFFERS)},
        "adjustment": {"type": "integer"},
        "cited": {"type": "array", "items": {"type": "string"}},
        "reasoning": {"type": "string"},
    },
    "required": ["offer", "adjustment", "cited", "reasoning"],
    "additionalProperties": False,
}

QUALIFY_SYSTEM = f"""You qualify B2B prospects for Nexi Studio, a small automation studio.

The offers you may map a prospect onto:
{json.dumps(config.OFFERS, indent=2)}

You will receive the evidence one gathering run actually found for one company:
its scraped self-description and a numbered list of signals, each with source
and detail. That list is the entire universe of facts. You know nothing else
about this company; anything you remember about it from elsewhere is off
limits and must not move your answer.

Your job:
1. offer: the single offer this evidence best supports, or "unclear" when the
   signals point nowhere or in several directions at once.
2. adjustment: an integer between -{config.LLM_ADJUSTMENT_CAP} and +{config.LLM_ADJUSTMENT_CAP} applied to a
   rule-computed base score. Adjust for what the rules cannot read: signals
   that are formally present but irrelevant to what this company does deserve
   a negative adjustment; several independent signals pointing at the same
   pain deserve a positive one. 0 is a perfectly good answer.
3. cited: the ids of the signals your adjustment and reasoning rest on. Cite
   only ids that exist. If you cite nothing, your adjustment must be <= 0.
4. reasoning: one plain sentence a reviewer reads to decide whether to trust
   the score. State what was found and what it means, no filler.
"""


def _weigh(result: CompanyResult) -> int:
    """Fill per-signal weights and return the capped rule base."""
    counted: set[str] = set()
    gap_total = 0
    base = 0
    for signal in result.signals:
        weight = config.SIGNAL_WEIGHTS.get(signal.type, 0)
        if signal.type == "capability_gap":
            allowed = max(0, config.CAPABILITY_GAP_STACK_CAP - gap_total)
            weight = min(weight, allowed)
            gap_total += weight
        elif signal.type in counted:
            weight = 0  # same type found twice counts once; shown as 0 in the breakdown
        counted.add(signal.type)
        signal.weight = weight
        base += weight
    return min(base, config.BASE_SCORE_CAP)


def _coverage_sentence(result: CompanyResult) -> str:
    failed = {m: s for m, s in result.coverage.items()
              if s in ("blocked", "unreachable", "robots-disallowed")}
    if not failed:
        return "No concrete signal found."
    looked = ", ".join(f"{m} {s}" for m, s in sorted(failed.items()))
    return f"No concrete signal found, and coverage was partial ({looked})."


def _offer_fallback(result: CompanyResult) -> str:
    types = {s.type for s in result.signals}
    support = "support_role_cluster" in types or any(
        s.type == "review_complaints" and "support" in s.detail for s in result.signals)
    data = "data_role_cluster" in types
    if support and not data:
        return "triage_agent"
    if data and not support:
        return "data_pipeline"
    if support and data:
        return "general_automation"
    return "unclear"


def score_company(result: CompanyResult, engine: AnthropicEngine | None) -> None:
    result.base_score = _weigh(result)

    if not result.signals:
        result.score = 0
        result.band = config.band_of(0)
        result.offer = "unclear"
        result.reasoning = _coverage_sentence(result)
        return

    if engine is None:
        result.llm_adjustment = 0
        result.score = result.base_score
        result.offer = _offer_fallback(result)
        result.reasoning = (f"Rule-based only (no model): {len(result.signals)} "
                            f"signal(s) found, weights sum to {result.base_score}.")
    else:
        evidence = "\n".join(
            f"  {s.id} [{s.type}, weight {s.weight}] ({s.source}): {s.detail}"
            for s in result.signals)
        user = (f"Company: {result.company}\nDomain: {result.domain}\n"
                f"Industry (from the user's list): {result.industry or 'not given'}\n"
                f"Scraped self-description:\n{result.description or '(site unreachable)'}\n\n"
                f"Signals found (the complete list):\n{evidence}\n\n"
                f"Rule-computed base score: {result.base_score}/100")
        try:
            raw = engine.complete(QUALIFY_SYSTEM, user, schema=QUALIFY_SCHEMA, effort="medium")
            verdict = json.loads(raw)
        except (LLMError, ValueError) as exc:
            log.error("qualification failed for %s, keeping rule base: %s", result.company, exc)
            verdict = {"offer": _offer_fallback(result), "adjustment": 0,
                       "cited": [], "reasoning": "Model unavailable; rule-based score only."}

        valid_ids = {s.id for s in result.signals}
        cited = [c for c in verdict.get("cited", []) if c in valid_ids]
        adjustment = max(-config.LLM_ADJUSTMENT_CAP,
                         min(config.LLM_ADJUSTMENT_CAP, int(verdict.get("adjustment", 0))))
        if adjustment > 0 and not cited:
            log.warning("%s: positive adjustment with no cited evidence, zeroing it",
                        result.company)
            adjustment = 0
        result.llm_adjustment = adjustment
        result.score = max(0, min(100, result.base_score + adjustment))
        result.offer = verdict.get("offer", "unclear")
        result.reasoning = verdict.get("reasoning", "").strip()

    result.band = config.band_of(result.score)
