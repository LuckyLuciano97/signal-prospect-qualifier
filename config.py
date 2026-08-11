"""Central configuration for Signal.

Everything a user would tune between runs lives here: which gather modules
run, how much each signal type is worth, and where the PASS/MAYBE lines sit.
Nothing below this file needs a code change to re-point the tool at a
different offer catalogue or a stricter threshold.

The scoring philosophy, stated once so the numbers below make sense: the
score is *derived from evidence found*, never guessed. Each signal type has a
fixed weight; the rule-based base score is the capped sum of the weights of
the signals actually gathered; the LLM may then nudge that base by at most
LLM_ADJUSTMENT_CAP points in either direction, with a written justification.
A company with no gathered evidence scores 0 and no model call is made for
it — there is nothing for a model to legitimately say about a company we
failed to observe.
"""

from __future__ import annotations

import os
import pathlib

REPO = pathlib.Path(__file__).resolve().parent

INPUT_PATH = REPO / "input_example.csv"
REPORT_PATH = REPO / "report.html"
RESULTS_PATH = REPO / "results.csv"
RESULTS_JSONL_PATH = REPO / "results.jsonl"  # full structured record, for validate.py
SUMMARY_PATH = REPO / "summary.json"
VALIDATION_PATH = REPO / "validation_report.txt"
CACHE_DIR = REPO / "cache"
LOG_DIR = REPO / "logs"

CONTACT = "support@nexistudio.dev"
USER_AGENT = f"signal-prospect-qualifier/1.0 (+{CONTACT})"

# --------------------------------------------------------------------------
# Gather modules
# --------------------------------------------------------------------------

# site is not optional: without reading the company's own site there is no
# fit judgement and nothing to personalize against.
MODULES = {
    "site": True,
    "hiring": True,
    "reviews": True,
    "competitive": True,  # only does anything for rows with a competitor_domain
}

# Politeness. One second minimum between requests to the same host, raised
# further if the host's robots.txt declares a longer Crawl-delay.
MIN_INTERVAL_PER_HOST = 1.0
HTTP_TIMEOUT = 30

# How many relevant job posts to read in full per company. Reading every
# posting of a 500-role board would be rude and pointless; the pain language
# we scan for shows up in the first few support/data posts if it shows up
# at all.
MAX_JOB_POSTS_READ = 3

# How many recent Trustpilot reviews to scan per company (one page).
MAX_REVIEWS_SCANNED = 20

# --------------------------------------------------------------------------
# Signal weights (the rule-anchored half of the score)
# --------------------------------------------------------------------------

# Weight per signal type. A type found more than once still counts once —
# the *_cluster types already encode "more than one of these" — except the
# types in STACK_CAPS, where independent instances genuinely say more than
# one: no chat widget AND no client portal are two separate gaps, a fax
# number AND "call us for a quote" are two separate manual-process markers.
SIGNAL_WEIGHTS = {
    "job_post_pain": 30,        # job text literally names the pain ("manual", "backlog")
    "support_role_cluster": 30, # >= 2 open support/CS roles
    "data_role_cluster": 30,    # >= 2 open data/ops/analyst roles
    "review_complaints": 30,    # recurring relevant complaint pattern in public reviews
    "single_relevant_role": 15, # exactly 1 open support or data/ops role
    "broad_hiring": 12,         # scaling fast across functions (processes break)
    "capability_gap": 12,       # a visible gap on their own site (no chat, no portal)
    "manual_process_language": 12,  # their own copy describes a manual process
    "growth_language": 5,       # "we're hiring" / expansion copy; generic on its own
    "competitor_gap": 8,        # competitor visibly has a capability they lack
}

# How far same-type signals may stack before further instances count 0.
STACK_CAPS = {
    "capability_gap": 36,           # up to three independent gaps
    "manual_process_language": 24,  # up to two independent markers
}
BASE_SCORE_CAP = 85             # rules alone cannot promise a 100

# The model reads the gathered evidence and may adjust the base by at most
# this much either way. Big enough to matter at a band edge, small enough
# that no amount of eloquence turns "no evidence" into a PASS.
LLM_ADJUSTMENT_CAP = 15

# Companies whose site was unreachable AND whose other modules found nothing
# are hard-capped here regardless of anything else. "We could not look" must
# never score like "we looked and it's promising".
UNREACHABLE_SCORE_CAP = 10

# --------------------------------------------------------------------------
# Bands (generous by design — see README)
# --------------------------------------------------------------------------

PASS_THRESHOLD = 70
MAYBE_THRESHOLD = 40

# Which bands get a drafted opener. The user asked for MAYBEs to surface,
# so they get openers too; FAILs never do.
DRAFT_OPENERS_FOR = ("PASS", "MAYBE")


def band_of(score: int) -> str:
    if score >= PASS_THRESHOLD:
        return "PASS"
    if score >= MAYBE_THRESHOLD:
        return "MAYBE"
    return "FAIL"


# --------------------------------------------------------------------------
# The offer catalogue the LLM maps signals onto
# --------------------------------------------------------------------------

OFFERS = {
    "triage_agent": "AI support triage agent (classifies, routes and drafts replies to support tickets)",
    "scraper": "structured web data extraction (scrapers with validation and monitoring)",
    "data_pipeline": "data pipelines and reporting automation (spreadsheets and manual data work replaced)",
    "general_automation": "workflow automation for repetitive manual back-office processes",
    "unclear": "signals are mixed or too thin to name one offer",
}

DEFAULT_MODEL = os.environ.get("SIGNAL_MODEL", "claude-opus-5")


def load_dotenv(path: pathlib.Path | None = None) -> None:
    """Minimal .env loader so no dependency is needed just to read a key.

    utf-8-sig because PowerShell's redirection operators write a BOM, which
    would otherwise silently rename the first key to '\\ufeffANTHROPIC_API_KEY'.
    """
    path = path or (REPO / ".env")
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
