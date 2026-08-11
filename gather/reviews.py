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
import os
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
          scanned_label: str, min_hits: int = MIN_PATTERN_HITS) -> bool:
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
        if hits >= min_hits:
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


# -- Google Places (the one live source; official API, key required) --------
#
# Auth travels in headers so the key never appears in cached URLs or evidence
# links. Only the rating, the rating count, and complaint-family counts over
# the (at most five) review texts the API returns leave this function —
# review bodies and reviewer identities are never stored. The place must
# match confidently: a distinctive token of the company name has to appear
# in the place's display name, or the source is skipped with a note.

GOOGLE_SEARCH = "https://places.googleapis.com/v1/places:searchText"
GOOGLE_MIN_PATTERN_HITS = 2   # only ~5 texts available; 2 recurring is a pattern
GOOGLE_LOW_RATING = 3.7
GOOGLE_MIN_RATINGS = 10

GENERIC_NAME_TOKENS = {"insurance", "agency", "agencies", "group", "inc", "llc",
                       "the", "and", "of", "company", "associates", "services"}


def _match_place(result: CompanyResult, places: list[dict]) -> dict | None:
    tokens = [t for t in re.split(r"[^a-z0-9]+", result.company.lower())
              if len(t) > 2 and t not in GENERIC_NAME_TOKENS]
    for place in places:
        name = str((place.get("displayName") or {}).get("text") or "").lower()
        if any(t in name for t in tokens):
            return place
    return None


def _google(client: PoliteClient, result: CompanyResult) -> str:
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if not api_key:
        return "skipped"
    query = " ".join(p for p in (result.company, result.location) if p)
    try:
        resp = client.post(
            GOOGLE_SEARCH, {"textQuery": query},
            allow_status=(200, 400, 404),
            extra_headers={
                "X-Goog-Api-Key": api_key,
                "X-Goog-FieldMask": "places.id,places.displayName,"
                                    "places.rating,places.userRatingCount,"
                                    "places.googleMapsUri",
            })
        if resp.status != 200:
            result.notes.append(f"reviews: Google Places search answered "
                                f"HTTP {resp.status}; skipped")
            return "unreachable"
        place = _match_place(result, resp.json().get("places") or [])
        if place is None:
            result.notes.append(f"reviews: no Google place confidently matching "
                                f"'{result.company}'; skipped")
            return "none-found"
        details = client.get(
            f"https://places.googleapis.com/v1/places/{place['id']}"
            f"?fields=rating,userRatingCount,reviews,googleMapsUri",
            allow_status=(200, 400, 404),
            extra_headers={"X-Goog-Api-Key": api_key})
        if details.status != 200:
            result.notes.append(f"reviews: Google place details answered "
                                f"HTTP {details.status}; skipped")
            return "unreachable"
        payload = details.json()
    except HostBlocked:
        # For this API a 403 nearly always means "Places API (New) is not
        # enabled for the key's Google Cloud project", not a crawl block.
        result.notes.append("reviews: Google Places answered 403 (enable "
                            "'Places API (New)' for this key's project); skipped")
        return "blocked"
    except (PermissionError, RuntimeError, ValueError) as exc:
        result.notes.append(f"reviews: Google Places lookup failed ({exc})")
        return "unreachable"

    name = str((place.get("displayName") or {}).get("text") or "place")
    maps_url = payload.get("googleMapsUri") or place.get("googleMapsUri") or ""
    rating = payload.get("rating")
    count = payload.get("userRatingCount") or 0
    if rating is not None:
        result.notes.append(f"reviews: Google shows '{name}' rated "
                            f"{rating:.1f} from {count} review(s)")
        if rating <= GOOGLE_LOW_RATING and count >= GOOGLE_MIN_RATINGS:
            add_signal(result, "review_complaints", "Google reviews", maps_url,
                       f"Google rating is {rating:.1f} out of 5 across {count} "
                       f"reviews for '{name}'")
    texts = [str((r.get("text") or {}).get("text") or "")
             for r in (payload.get("reviews") or [])]
    texts = [t for t in texts if t][:5]
    if texts:
        _scan(result, texts, "Google reviews", maps_url,
              f"Google reviews of '{name}'", min_hits=GOOGLE_MIN_PATTERN_HITS)
    return "ok"


def gather(client: PoliteClient, result: CompanyResult) -> None:
    trustpilot_status = _trustpilot(client, result)
    appstore_status = _appstore(client, result)
    google_status = _google(client, result)

    # One combined coverage value: "ok" if anything was actually scanned,
    # otherwise the most informative of the failure states.
    statuses = (google_status, trustpilot_status, appstore_status)
    if "ok" in statuses:
        result.coverage["reviews"] = "ok"
    elif "none-found" in statuses:
        result.coverage["reviews"] = "none-found"
    else:
        result.coverage["reviews"] = trustpilot_status
