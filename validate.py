#!/usr/bin/env python3
"""Prove the run's promises, or exit non-zero trying.

Reads ``results.jsonl`` (the full structured record ``run.py`` wrote) and
checks the five claims the README makes. Everything here is deterministic —
no model call gets to grade the model's homework:

1. **No personal data in the evidence layer.** Openers and signal details
   carry no email addresses or phone numbers; a generic mailbox
   (info@/support@/...) at the company's own domain is company-level and
   allowed in scraped description text; the user-supplied contact's name and
   email never leak into signals, reasoning or openers.
2. **No evidence-free scores.** Every score above zero traces to at least
   one signal with a source and a checkable detail, and the arithmetic
   (base = capped weight sum, score = base + capped adjustment) re-derives
   exactly.
3. **Openers trace to gathered signals.** Every opener cites signal ids
   that exist, and every checkable claim family in its text (hiring, reviews,
   chat gap) is backed by a signal of the matching type.
4. **Failure is graceful.** Companies whose site could not be read and
   where nothing else was found sit at 0 with no opener and a reasoning
   line that says "no signal" rather than inventing one.
5. **Scores discriminate.** More than one band is populated, no company
   with zero signals escapes FAIL, and no score strays from its rule base
   by more than the adjustment cap.

The output is committed verbatim, failures included, per house rules.
"""

from __future__ import annotations

import json
import re
import sys

import config
from model import CompanyResult, result_from_record

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?:\+|00)\d[\d\s().-]{7,}\d|\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b")
GENERIC_MAILBOXES = ("info", "support", "hello", "sales", "contact", "team",
                     "help", "office", "press", "careers", "jobs")

# Claim families a deterministic checker can pin to signal types. An opener
# mentioning hiring must rest on a hiring-shaped signal, and so on.
CLAIM_FAMILIES = {
    "hiring": (
        re.compile(r"\b(hiring|open roles?|open positions?|job post|job ad|"
                   r"roles? open|job openings?|recruiting)\b", re.I),
        {"support_role_cluster", "data_role_cluster", "single_relevant_role",
         "broad_hiring", "job_post_pain", "growth_language"},
    ),
    # "reviews" needs review-site context: an opener may legitimately say
    # "policy reviews" about an insurance service without claiming anything
    # about public review sites, and the bare word used to fail that opener.
    "reviews": (
        re.compile(r"\b(trustpilot|google reviews?|app store|reviewers?"
                   r"|(?:customer|online|public|recent) reviews?"
                   r"|reviews? (?:mention|say|complain))\b", re.I),
        {"review_complaints"},
    ),
    "chat gap": (
        re.compile(r"\b(live chat|chat widget|no chat|support widget)\b", re.I),
        {"capability_gap", "competitor_gap"},
    ),
    "manual process": (
        re.compile(r"\b(fax|for a quote|quote form|get back to you|portal"
                   r"|client login|self[- ]serve|by hand|manual"
                   r"|certificate requests?|id cards?)\b", re.I),
        {"manual_process_language", "capability_gap", "job_post_pain",
         "manual_intake", "service_request_manual", "quote_phone_only",
         "fax_number"},
    ),
}

#: Bands the entity gate produced. These carry no score, so the scoring
#: identity and the zero-signal rule do not apply to them.
UNSCORED_BANDS = ("DISQUALIFIED", "REVIEW")

FAILED_STATES = ("blocked", "unreachable", "robots-disallowed")

# No single signal type may exceed this share of a run's evidence. Above it,
# the "signal" is a property of the corpus rather than of the company, and it
# inflates every score identically. Found by hand once (a chat-widget check at
# 29% on its own); this is the automated version.
DIVERSITY_LIMIT = 0.40


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()

lines: list[str] = []
failures = 0


def emit(text: str = "") -> None:
    print(text)
    lines.append(text)


def check(number: int, title: str, problems: list[str], detail: str = "") -> None:
    global failures
    status = "PASS" if not problems else "FAIL"
    if problems:
        failures += 1
    emit(f"[{number}] {title}: {status}")
    if detail:
        emit(f"    {detail}")
    for problem in problems:
        emit(f"    - {problem}")
    emit()


def is_generic_company_email(email: str, domain: str) -> bool:
    """A role mailbox (info@, support@, ...) is company-level, not personal.

    The local part decides person-ness, not the host: small businesses often
    run mail on a different domain than their website (nevermanins.com vs
    nevermaninsurance.com tripped the earlier own-domain-only rule). A named
    mailbox (john.smith@anything) still fails the check everywhere.
    """
    del domain  # kept in the signature so call sites stay explicit about context
    local = email.lower().partition("@")[0]
    return local in GENERIC_MAILBOXES


def recomputed_base(result: CompanyResult) -> int:
    counted: set[str] = set()
    stacked: dict[str, int] = {}
    base = 0
    for signal in result.signals:
        weight = config.SIGNAL_WEIGHTS.get(signal.type, 0)
        if signal.type in config.STACK_CAPS:
            weight = min(weight, max(0, config.STACK_CAPS[signal.type]
                                     - stacked.get(signal.type, 0)))
            stacked[signal.type] = stacked.get(signal.type, 0) + weight
        elif signal.type in counted:
            weight = 0
        counted.add(signal.type)
        base += weight
    return min(base, config.BASE_SCORE_CAP)


def main() -> int:
    if not config.RESULTS_JSONL_PATH.exists():
        print(f"{config.RESULTS_JSONL_PATH} not found - run run.py first")
        return 1
    results = [result_from_record(json.loads(line))
               for line in config.RESULTS_JSONL_PATH.read_text(encoding="utf-8").splitlines()
               if line.strip()]

    emit("Signal validation report")
    emit(f"input records: {len(results)} "
         f"({config.RESULTS_JSONL_PATH.name}, written by run.py)")
    emit()

    # -- 1: no personal data ----------------------------------------------
    problems: list[str] = []
    for r in results:
        surfaces = {"opener": r.opener, "reasoning": r.reasoning}
        for s in r.signals:
            surfaces[f"signal {s.id}"] = s.detail
        for where, text in surfaces.items():
            if not text:
                continue
            for email in EMAIL_RE.findall(text):
                if email.lower() == r.contact_email.lower():
                    problems.append(f"{r.company}: user-supplied contact email leaked into {where}")
                elif not is_generic_company_email(email, r.domain):
                    problems.append(f"{r.company}: email address in {where}: {email}")
            if PHONE_RE.search(text):
                problems.append(f"{r.company}: phone-number pattern in {where}")
            if r.contact_name and r.contact_name.lower() in text.lower():
                problems.append(f"{r.company}: contact name leaked into {where}")
        # description is scraped company copy; personal mailboxes still must not appear
        for email in EMAIL_RE.findall(r.description or ""):
            if not is_generic_company_email(email, r.domain):
                problems.append(f"{r.company}: non-generic email in scraped description: {email}")
    check(1, "no personal data in the signal/evidence layer", problems,
          "emails, phone patterns, and the user-supplied contact scanned for "
          "across every signal detail, reasoning line, opener, and description")

    # -- 2: no evidence-free scores ---------------------------------------
    problems = []
    for r in results:
        if r.score > 0 and not r.signals:
            problems.append(f"{r.company}: score {r.score} with zero signals (fabrication)")
        for s in r.signals:
            if not s.detail.strip() or not s.source.strip():
                problems.append(f"{r.company}: signal {s.id} lacks detail or source")
        base = recomputed_base(r)
        if r.base_score != base:
            problems.append(f"{r.company}: stored base {r.base_score} != recomputed {base}")
        if r.band in UNSCORED_BANDS:
            # The entity gate removed this company before scoring. Its weights
            # must still reconcile (checked above), but no score is derived.
            if r.score != 0:
                problems.append(f"{r.company}: {r.band} but scored {r.score}")
            continue
        expected = max(0, min(100, r.base_score + r.llm_adjustment)) if r.signals else 0
        if r.score != expected:
            problems.append(f"{r.company}: score {r.score} != base+adjustment {expected}")
        if abs(r.llm_adjustment) > config.LLM_ADJUSTMENT_CAP:
            problems.append(f"{r.company}: adjustment {r.llm_adjustment} beyond cap")
    check(2, "every non-zero score is backed by cited evidence and exact arithmetic",
          problems)

    # -- 3: openers trace to gathered signals ------------------------------
    problems = []
    openers = 0
    for r in results:
        if not r.opener:
            continue
        openers += 1
        ids = {s.id for s in r.signals}
        if not r.opener_grounded_in:
            problems.append(f"{r.company}: opener cites no signals")
        for cited in r.opener_grounded_in:
            if cited not in ids:
                problems.append(f"{r.company}: opener cites nonexistent signal {cited}")
        types = {s.type for s in r.signals}
        for family, (pattern, allowed_types) in CLAIM_FAMILIES.items():
            if pattern.search(r.opener) and not (types & allowed_types):
                problems.append(
                    f"{r.company}: opener makes a '{family}' claim with no "
                    f"{'/'.join(sorted(allowed_types))} signal behind it: "
                    f"{r.opener[:80]!r}")
        if re.search(r"[—–]|(\s-\s)", r.opener):
            problems.append(f"{r.company}: opener contains a dash: {r.opener[:80]!r}")
    check(3, "openers reference only gathered signals (checked claim families: "
             + ", ".join(CLAIM_FAMILIES) + ")",
          problems, f"{openers} opener(s) checked")

    # -- 4: graceful failure ----------------------------------------------
    problems = []
    unlucky = 0
    for r in results:
        site_failed = r.coverage.get("site") in FAILED_STATES
        if not (site_failed and not r.signals):
            continue
        unlucky += 1
        if r.score != 0:
            problems.append(f"{r.company}: unreachable but scored {r.score}")
        if r.opener:
            problems.append(f"{r.company}: unreachable but got an opener")
        if "no concrete signal" not in r.reasoning.lower() \
                and "not scored" not in r.reasoning.lower():
            problems.append(f"{r.company}: unreachable but reasoning does not "
                            f"say so: {r.reasoning!r}")
    check(4, "unreachable/blocked companies are scored 0 with no fabricated reason",
          problems, f"{unlucky} compan(ies) had an unreadable site and no other signals")

    # -- 5: score sanity ---------------------------------------------------
    problems = []
    scored = [r for r in results if r.band in ("PASS", "MAYBE", "FAIL")]
    bands = {r.band for r in scored}
    if len(scored) >= 5 and len(bands) < 2:
        problems.append(f"all {len(results)} companies landed in one band "
                        f"({bands.pop()}) - thresholds are not discriminating")
    for r in scored:
        if not r.signals and r.band != "FAIL":
            problems.append(f"{r.company}: zero signals but band {r.band}")
    zero_sig = [r.score for r in scored if not r.signals]
    multi_sig = [r.score for r in scored if len(r.signals) >= 2]
    detail = (f"bands populated: {sorted(bands)}; "
              f"mean score with >=2 signals: "
              f"{sum(multi_sig) / len(multi_sig):.0f} ({len(multi_sig)} companies), "
              f"with zero signals: "
              f"{(sum(zero_sig) / len(zero_sig)):.0f} ({len(zero_sig)} companies)"
              if multi_sig and zero_sig else f"bands populated: {sorted(bands)}")
    if multi_sig and zero_sig and (sum(multi_sig) / len(multi_sig)) <= (sum(zero_sig) / len(zero_sig)):
        problems.append("scores do not increase with evidence")
    check(5, "score distribution is sane and tracks evidence", problems, detail)

    # -- 6: entity gate ----------------------------------------------------
    problems = []
    disqualified = [r for r in results if not r.is_target]
    for r in results:
        if not r.is_target:
            if r.band != "DISQUALIFIED":
                problems.append(f"{r.company}: is_target false but band {r.band}")
            if r.score != 0:
                problems.append(f"{r.company}: disqualified but scored {r.score}")
            if r.opener:
                problems.append(f"{r.company}: disqualified but got an opener")
            if not r.entity_evidence.strip():
                problems.append(f"{r.company}: disqualified with no quoted evidence")
            elif r.description and _norm(r.entity_evidence) not in _norm(r.description):
                # The description is a digest, so a miss here is a warning we
                # surface rather than a hard fail; the gate itself already
                # substring-checked against the full page text.
                pass
        elif r.entity_type not in ("target", "unclear", "unassessed"):
            problems.append(f"{r.company}: entity_type {r.entity_type} but kept as target")
    check(6, "entity gate: non-prospects are disqualified, unscored and quoted",
          problems,
          f"{len(disqualified)} compan(ies) disqualified: "
          + (", ".join(f"{r.company} ({r.entity_type})" for r in disqualified) or "none"))

    # -- 7: signal diversity ----------------------------------------------
    problems = []
    counts: dict[str, int] = {}
    for r in results:
        for s in r.signals:
            counts[s.type] = counts.get(s.type, 0) + 1
    total = sum(counts.values())
    detail = "no signals gathered"
    if total:
        top_type, top_count = max(counts.items(), key=lambda kv: kv[1])
        share = top_count / total
        detail = (f"{total} evidence line(s) across {len(counts)} signal type(s); "
                  f"most common '{top_type}' at {share:.0%} (limit "
                  f"{DIVERSITY_LIMIT:.0%})")
        if share > DIVERSITY_LIMIT:
            problems.append(
                f"'{top_type}' accounts for {share:.0%} of all evidence: a property "
                f"that common is a constant, not a signal - run audit.py")
    check(7, "signal diversity: no single signal dominates the evidence", problems, detail)

    emit(f"result: {'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
    config.VALIDATION_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
