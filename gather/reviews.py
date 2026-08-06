"""Module 3 — public review complaints. Honest status: mostly closed.

Two sources are attempted, and as of the August 2026 run both refuse generic
crawlers in their robots.txt, which this tool obeys without exception:

* **Trustpilot** (``trustpilot.com/review/<domain>``) — robots.txt ends with
  ``User-agent: * Disallow: /``: unlisted crawlers are banned site-wide.
* **Apple App Store** — ``itunes.apple.com/robots.txt`` disallows
  ``/search*`` and ``/*/rss/*`` for ``User-agent: *``, which covers both the
  app search endpoint and the customer-reviews feed. Arguably an API
  documented for programmatic use is not "crawling", but this tool applies
  one rule everywhere rather than maintaining a list of exceptions it finds
  convenient.

G2 and Capterra sit behind bot walls and are not attempted at all.

So in practice this module records ``robots-disallowed`` and contributes
nothing, the README says so in plain words, and no company's score suffers a
fabricated substitute. The code below stays functional on purpose: it is the
correct behaviour to demonstrate, robots policies change, and a licensed
integration (Trustpilot's business API, an app-review API vendor) would slot
into these exact seams. If a source ever answers, the app-match guard still
applies: the company name must appear in the app title or seller name, or
the source is skipped — a review count for the wrong company's app would be
worse than no signal.

What leaves this module is deliberately bloodless: **counts of recent
reviews matching complaint families, plus which lexicon terms matched and
which app/page was scanned** — never review text, never a reviewer's name.
"9 of the 50 most recent reviews mention waiting on support" is a company-
level pattern; raw review bodies do not survive past this function. A
family needs MIN_PATTERN_HITS matching reviews before it becomes a signal;
one grumpy review is weather, not climate.
"""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import quote

from bs4 import BeautifulSoup

from gather import add_signal
from model import CompanyResult
from net import HostBlocked, PoliteClient

import config

log = logging.getLogger("signal.reviews")

MIN_PATTERN_HITS = 3

ITUNES_SEARCH = "https://itunes.apple.com/search?term={term}&entity=software&limit=10&country=us"
ITUNES_REVIEWS = "https://itunes.apple.com/us/rss/customerreviews/page=1/id={app_id}/sortby=mostrecent/json"

COMPLAINT_FAMILIES = {
    "slow or absent support replies": (
        "slow to respond", "no response", "never responded", "never heard back",
        "no reply", "waited days", "waited weeks", "waiting for weeks",
        "still waiting", "impossible to reach", "can't reach", "cant reach",
        "no customer service", "customer service is terrible",
        "customer service is awful", "customer support is", "on hold",
        "ignored my", "no help", "unresponsive",
    ),
    "errors, wrong or missing items/charges": (
        "wrong order", "wrong item", "wrong charge", "charged twice",
        "double charged", "billing error", "incorrect", "glitch", "buggy",
        "missing order", "missing refund", "missing items", "lost my order",
        "error in", "did not receive", "never received", "never arrived",
    ),
}


def _scan(result: CompanyResult, texts: list[str], source: str, url: str,
          scanned_label: str) -> bool:
    """Count complaint families over review texts; emit signals; True if any."""
    found = False
    for family, terms in COMPLAINT_FAMILIES.items():
        hits = 0
        seen_terms: set[str] = set()
        for text in texts:
            lowered = re.sub(r"\s+", " ", text.lower())
            matched = [t for t in terms if t in lowered]
            if matched:
                hits += 1
                seen_terms.update(matched)
        if hits >= MIN_PATTERN_HITS:
            term_list = ", ".join(f"'{t}'" for t in sorted(seen_terms)[:4])
            add_signal(result, "review_complaints", source, url,
                       f"{hits} of the {len(texts)} most recent {scanned_label} "
                       f"mention {family} (terms seen: {term_list})")
            found = True
    return found


# -- Trustpilot -------------------------------------------------------------

def _trustpilot(client: PoliteClient, result: CompanyResult) -> str:
    """Returns a coverage status; emits signals on success."""
    domain = result.domain.removeprefix("www.")
    page = None
    try:
        for candidate in (domain, f"www.{domain}"):
            resp = client.get(f"https://www.trustpilot.com/review/{candidate}")
            if resp.status == 200:
                page = resp
                break
    except HostBlocked as exc:
        result.notes.append(f"reviews: Trustpilot {exc}")
        return "blocked"
    except PermissionError:
        result.notes.append("reviews: Trustpilot robots.txt disallows crawling "
                            "(User-agent: * is banned site-wide); respected, skipped")
        return "robots-disallowed"
    except RuntimeError as exc:
        result.notes.append(f"reviews: Trustpilot unreachable ({exc})")
        return "unreachable"

    if page is None:
        return "none-found"

    soup = BeautifulSoup(page.body, "html.parser")
    texts: list[str] = []
    script = soup.find("script", id="__NEXT_DATA__")
    if script and script.string:
        try:
            payload = json.loads(script.string)
            for review in payload["props"]["pageProps"].get("reviews") or []:
                text = " ".join(str(review.get(k) or "") for k in ("title", "text")).strip()
                if text:
                    texts.append(text)
        except (ValueError, KeyError, TypeError) as exc:
            log.debug("__NEXT_DATA__ shape unexpected: %s", exc)
    if not texts:
        for node in soup.select("[data-service-review-text-typography], "
                                "[data-service-review-title-typography]"):
            text = node.get_text(" ", strip=True)
            if text:
                texts.append(text)
    if not texts:
        result.notes.append("reviews: Trustpilot page exists but reviews were "
                            "not extractable from it")
        return "none-found"

    texts = texts[: config.MAX_REVIEWS_SCANNED]
    if not _scan(result, texts, "Trustpilot", page.url, "Trustpilot reviews"):
        result.notes.append(f"reviews: {len(texts)} recent Trustpilot reviews "
                            "scanned, no recurring relevant complaint pattern")
    return "ok"


# -- Apple App Store --------------------------------------------------------

def _match_app(result: CompanyResult, apps: list[dict]) -> dict | None:
    """The company's own app, or None when no candidate matches confidently."""
    company = result.company.lower()
    domain_base = result.domain.removeprefix("www.").split(".")[0].lower()
    for app in apps:
        title = str(app.get("trackName") or "").lower()
        seller = str(app.get("sellerName") or "").lower()
        if company in title or company in seller \
                or domain_base in title or domain_base in seller:
            return app
    return None


def _appstore(client: PoliteClient, result: CompanyResult) -> str:
    try:
        resp = client.get(ITUNES_SEARCH.format(term=quote(result.company)))
        if resp.status != 200:
            return "unreachable"
        apps = resp.json().get("results") or []
        app = _match_app(result, apps)
        if app is None:
            result.notes.append(
                f"reviews: no App Store app confidently matching "
                f"'{result.company}' among {len(apps)} search results; skipped")
            return "none-found"
        reviews_resp = client.get(ITUNES_REVIEWS.format(app_id=app["trackId"]))
        if reviews_resp.status != 200:
            return "none-found"
        entries = (reviews_resp.json().get("feed") or {}).get("entry") or []
    except HostBlocked as exc:
        result.notes.append(f"reviews: App Store {exc}")
        return "blocked"
    except PermissionError:
        result.notes.append("reviews: App Store robots.txt disallows the feed; skipped")
        return "robots-disallowed"
    except (RuntimeError, ValueError) as exc:
        result.notes.append(f"reviews: App Store lookup failed ({exc})")
        return "unreachable"

    if isinstance(entries, dict):  # single-entry feeds come as a bare object
        entries = [entries]
    texts = []
    for entry in entries:
        text = " ".join((
            ((entry.get("title") or {}).get("label") or ""),
            ((entry.get("content") or {}).get("label") or ""),
        )).strip()
        if text:
            texts.append(text)
    if not texts:
        result.notes.append("reviews: matched App Store app has no readable "
                            "recent reviews")
        return "none-found"

    texts = texts[: max(config.MAX_REVIEWS_SCANNED, 50)]
    app_name = app.get("trackName", "app")
    page_url = app.get("trackViewUrl") or ""
    label = f"App Store reviews of '{app_name}'"
    if not _scan(result, texts, "App Store", page_url, label):
        result.notes.append(f"reviews: {len(texts)} recent App Store reviews of "
                            f"'{app_name}' scanned, no recurring relevant "
                            f"complaint pattern")
    return "ok"


def gather(client: PoliteClient, result: CompanyResult) -> None:
    trustpilot_status = _trustpilot(client, result)
    appstore_status = _appstore(client, result)

    # One combined coverage value: "ok" if anything was actually scanned,
    # otherwise the more informative of the two failure states.
    statuses = (trustpilot_status, appstore_status)
    if "ok" in statuses:
        result.coverage["reviews"] = "ok"
    elif "none-found" in statuses:
        result.coverage["reviews"] = "none-found"
    else:
        result.coverage["reviews"] = trustpilot_status
