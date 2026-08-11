"""Module 1 — the company's own website. Always runs.

Reads the homepage and, when they exist, the about / services / careers /
contact pages, and extracts three kinds of things:

* **Context** — what the company does, for the qualifier and the opener.
  Title, meta description and the opening of the about page; never a guess.
* **Capability gaps** — things visibly absent from their own pages: no live
  chat or support widget in the HTML of any page checked, a contact page
  that offers only a form or an email address. Each claim states exactly
  what was checked, because "not detected in N pages" is an observation and
  "they have no chat" would be an inference.
* **Growth signals** — hiring and expansion language in the visible text,
  quoted.

It also collects two things for the other modules: the careers page URL and
any Greenhouse/Lever board links found in the markup (a link the company
itself published beats guessing board tokens from the domain).
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from gather import add_signal, visible_text
from model import CompanyResult
from net import HostBlocked, PoliteClient

log = logging.getLogger("signal.site")

# Substrings in raw HTML that identify a live chat / support widget. Used to
# *suppress* the capability-gap claim, so a false positive here errs safe:
# we fail to claim a gap rather than claim one that is not there.
CHAT_WIDGETS = {
    "Intercom": ("intercom.io", "intercomcdn", "window.Intercom"),
    "Drift": ("driftt.com", "drift.com/embed"),
    "Crisp": ("crisp.chat",),
    "Tawk": ("tawk.to",),
    "Zendesk widget": ("zdassets.com", "zopim"),
    "LiveChat": ("livechatinc.com", "cdn.livechat"),
    "HubSpot": ("js.hs-scripts.com", "usemessages.com"),
    "Gorgias": ("gorgias.chat",),
    "Freshchat": ("freshchat.com", "freshworks.com/live-chat"),
    "Olark": ("olark.com",),
    "Tidio": ("tidio.co",),
    "Help Scout Beacon": ("beacon-v2.helpscout", "helpscout.net"),
    "Chatwoot": ("chatwoot",),
    "Smartsupp": ("smartsupp",),
    "LivePerson": ("liveperson", "lpcdn."),
    "Userlike": ("userlike",),
    "Front chat": ("chat.frontapp.com",),
    "Salesforce chat": ("embeddedservice", "salesforceliveagent"),
    "Ada": ("ada.support",),
}

GROWTH_RE = re.compile(
    r"\b(we'?re hiring|we are hiring|now hiring|join (?:our|the) (?:growing |global )?team"
    r"|we'?re (?:growing|expanding)|rapidly (?:growing|expanding)|open (?:roles|positions))\b",
    re.IGNORECASE,
)

# Manual-process markers: the company's own visible copy describing a
# process a person performs by hand. Each marker found becomes one signal
# quoting the phrase (never numbers — a fax signal says "lists a fax
# number", it does not store the number). Added after the first SMB run:
# tiny service businesses have no job boards and no review pages, so the
# way they describe their own workflow is the strongest public signal left.
MANUAL_MARKERS = {
    "call-for-quote": re.compile(
        r"(?:call|contact|phone)(?: us)?[^.!?]{0,40}?for (?:a |your |free |an )*quot(?:e|ation)s?\b",
        re.IGNORECASE),
    "we-will-get-back": re.compile(
        r"(?:we[’']ll|we will) (?:get back to you|be in touch|reach out|contact you)",
        re.IGNORECASE),
    "fax": re.compile(r"\bfax\b", re.IGNORECASE),
}

# Any of these in visible text or an href means the company offers some
# self-serve login; its absence across every page checked is a capability
# gap. Matching is deliberately broad because it errs toward *suppressing*
# the gap claim, never toward inventing one.
PORTAL_RE = re.compile(
    r"\b(?:log ?in|sign ?in|portal|my account|client (?:center|login|area)"
    r"|policyholder)\b", re.IGNORECASE)
PORTAL_HREF_RE = re.compile(r"login|signin|sign-in|portal|account", re.IGNORECASE)

BOARD_LINK_RE = re.compile(
    r"(?:job-boards|boards)\.(?:eu\.)?greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9._-]+)"
    r"|jobs\.(?:eu\.)?lever\.co/([A-Za-z0-9._-]+)",
    re.IGNORECASE,
)

# href / anchor-text patterns for the standard subpages, tried in order; the
# literal conventional paths are probed as a fallback when no link matches,
# and a 404 on a guessed path is quietly accepted.
PAGE_PATTERNS = {
    "about": (re.compile(r"about|company|who[-_ ]we[-_ ]are", re.I), ("/about", "/about-us")),
    "services": (re.compile(r"services|products|solutions|platform|features|what[-_ ]we[-_ ]do", re.I),
                 ("/products", "/services")),
    "careers": (re.compile(r"careers|jobs|join[-_ ]?us|work[-_ ]with[-_ ]us|vacancies", re.I),
                ("/careers", "/jobs")),
    "contact": (re.compile(r"contact", re.I), ("/contact", "/contact-us")),
}


def _fetch_page(client: PoliteClient, url: str):
    """One page fetch; None when the page is not there or not allowed.

    HostBlocked is re-raised, not swallowed: a 403 is coverage the caller
    must record, not a page that happens to be missing.
    """
    try:
        resp = client.get(url, allow_status=(200, 404, 410))
    except HostBlocked:
        raise
    except (PermissionError, RuntimeError) as exc:
        log.debug("miss %s (%s)", url, exc)
        return None
    return resp if resp.status == 200 else None


def _find_subpage_urls(base_url: str, soup: BeautifulSoup) -> dict[str, str]:
    """First same-site link matching each PAGE_PATTERNS category."""
    found: dict[str, str] = {}
    base_host = urlsplit(base_url).netloc.removeprefix("www.")
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        absolute = urljoin(base_url, href)
        host = urlsplit(absolute).netloc.removeprefix("www.")
        text = anchor.get_text(" ", strip=True)
        for category, (pattern, _) in PAGE_PATTERNS.items():
            if category in found:
                continue
            if pattern.search(urlsplit(absolute).path) or pattern.search(text):
                # careers pages legitimately live off-site (jobs.lever.co);
                # everything else must stay on the company's own host.
                if host == base_host or category == "careers":
                    found[category] = absolute
    return found


def _detect_widget(html_pages: dict[str, str]) -> str | None:
    for name, needles in CHAT_WIDGETS.items():
        for html in html_pages.values():
            lowered = html.lower()
            if any(needle.lower() in lowered for needle in needles):
                return name
    return None


def _describe(soup: BeautifulSoup, homepage_text: str, about_text: str) -> str:
    """What the company says it does, assembled only from what it says."""
    parts: list[str] = []
    if soup.title and soup.title.string:
        parts.append(soup.title.string.strip())
    meta = soup.find("meta", attrs={"name": "description"}) or \
        soup.find("meta", attrs={"property": "og:description"})
    if meta and meta.get("content"):
        parts.append(meta["content"].strip())
    if about_text:
        parts.append(about_text[:500])
    parts.append(homepage_text[:600])
    seen: list[str] = []
    for part in parts:
        if part and part not in seen:
            seen.append(part)
    return " | ".join(seen)[:1200]


def gather(client: PoliteClient, result: CompanyResult) -> dict:
    """Read the site; return facts the other modules build on."""
    facts: dict = {"careers_url": None, "board_hints": [], "chat_widget": None,
                   "pages_checked": []}

    homepage = None
    candidates = [f"https://{result.domain}"]
    if not result.domain.startswith("www."):
        candidates.append(f"https://www.{result.domain}")
    try:
        for base in candidates:
            homepage = _fetch_page(client, base)
            if homepage is not None:
                break
    except HostBlocked as exc:
        result.coverage["site"] = "blocked"
        result.notes.append(f"site: {exc}")
        return facts

    if homepage is None:
        result.coverage["site"] = "unreachable"
        result.notes.append("site: homepage could not be fetched (no signal, not scored)")
        return facts

    soup = BeautifulSoup(homepage.body, "html.parser")
    pages: dict[str, str] = {"homepage": homepage.body}
    page_urls: dict[str, str] = {"homepage": homepage.url}
    subpages = _find_subpage_urls(homepage.url, soup)

    for category, (_, fallback_paths) in PAGE_PATTERNS.items():
        url = subpages.get(category)
        urls_to_try = [url] if url else [urljoin(homepage.url, p) for p in fallback_paths]
        for candidate in urls_to_try:
            try:
                resp = _fetch_page(client, candidate)
            except HostBlocked:
                break  # host said stop mid-crawl; keep what we already have
            if resp is not None:
                pages[category] = resp.body
                page_urls[category] = resp.url
                break

    facts["pages_checked"] = list(page_urls.values())
    facts["careers_url"] = page_urls.get("careers")

    # Board links anywhere in the fetched markup are the strongest token hint.
    for html in pages.values():
        for match in BOARD_LINK_RE.finditer(html):
            gh_token, lever_token = match.group(1), match.group(2)
            hint = ("greenhouse", gh_token) if gh_token else ("lever", lever_token)
            if hint not in facts["board_hints"]:
                facts["board_hints"].append(hint)

    texts = {name: visible_text(html) for name, html in pages.items()}
    result.description = _describe(soup, texts["homepage"], texts.get("about", ""))
    result.coverage["site"] = "ok"

    # -- capability gap: no chat/support widget anywhere we looked ---------
    widget = _detect_widget(pages)
    facts["chat_widget"] = widget
    if widget:
        result.notes.append(f"site: live chat/support widget present ({widget})")
    else:
        add_signal(
            result, "capability_gap", "site", homepage.url,
            f"no live chat or support widget detected in the HTML of "
            f"{len(pages)} page(s) checked ({', '.join(sorted(pages))})",
        )

    # -- capability gap: contact page is a dead drop -----------------------
    contact_html = pages.get("contact")
    if contact_html:
        has_form = "<form" in contact_html.lower()
        has_mailto = "mailto:" in contact_html.lower()
        has_phone = "tel:" in contact_html.lower()
        if (has_form or has_mailto) and not has_phone and not widget:
            channel = "a contact form" if has_form else "an email address"
            add_signal(
                result, "capability_gap", "site", page_urls["contact"],
                f"contact page offers only {channel}; no phone number or live "
                f"channel found on it",
            )

    # -- capability gap: no self-serve login/portal anywhere ---------------
    has_portal = any(PORTAL_RE.search(text) for text in texts.values()) or \
        any(PORTAL_HREF_RE.search(a.get("href", ""))
            for html_body in pages.values()
            for a in BeautifulSoup(html_body, "html.parser").find_all("a", href=True))
    if not has_portal:
        add_signal(
            result, "capability_gap", "site", homepage.url,
            f"no client login or self-serve portal found across "
            f"{len(pages)} page(s) checked",
        )

    # -- manual-process language -------------------------------------------
    for marker, pattern in MANUAL_MARKERS.items():
        for name, text in texts.items():
            match = pattern.search(text)
            if not match:
                continue
            if marker == "fax":
                detail = f"{name} page lists a fax number"
            else:
                detail = f'{name} page says "{match.group(0)}"'
            add_signal(result, "manual_process_language", "site",
                       page_urls[name], detail)
            break

    # -- growth language ---------------------------------------------------
    for name, text in texts.items():
        match = GROWTH_RE.search(text)
        if match:
            add_signal(
                result, "growth_language", "site", page_urls[name],
                f'{name} page says "{match.group(0)}"',
            )
            break

    return facts
