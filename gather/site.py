"""Module 1 — the company's own website. Always runs.

Reads the homepage and, when they exist, the about / services / careers /
contact pages, and extracts three kinds of things:

* **Context** — what the company does, for the qualifier and the opener.
* **Manual-process evidence** — the company's own copy describing work a
  person does by hand: documents returned by email or fax, printable intake
  forms, service requests (certificates, ID cards, policy changes) routed to
  a human, quotes with no online path. These are the signals that actually
  discriminate between one small agency and the next.
* **Capability gaps** — a self-serve thing visibly absent: no client portal,
  or a contact page that genuinely offers no way to reach a person.

Two hard-won rules live in this file:

1. **Detect on visible text, not markup.** An early version matched the
   ``&quot;`` HTML entity as the word "quote".
2. **Absence claims must survive the way small sites are actually built.**
   The contact-channel check used to look only for ``tel:`` links and so
   announced "no phone number found" about a page that displayed
   ``260-925-4766`` next to the words "call, email or stop by". That false
   line reached a draft opener. Absence is now only claimed when the phone
   pattern is missing from the rendered text as well.

A note on what is *not* here: "no live chat widget" was deleted. It fired on
15 of 17 companies in the batch-2 corpus and made up 29% of all evidence.
Widget presence is still detected, because it usefully *suppresses* the
contact-channel gap and feeds the competitive module, but it is no longer a
signal of its own.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from gather import add_signal, excerpt, visible_text
from model import CompanyResult
from net import HostBlocked, PoliteClient

log = logging.getLogger("signal.site")

# Substrings in raw HTML that identify a live chat / support widget. A false
# positive here errs safe: it suppresses a gap claim rather than inventing one.
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
    "Podium": ("podium.com", "podium.js"),
    "Birdeye": ("birdeye.com",),
}

GROWTH_RE = re.compile(
    r"\b(we'?re hiring|we are hiring|now hiring|join (?:our|the) (?:growing |global )?team"
    r"|we'?re (?:growing|expanding)|rapidly (?:growing|expanding)|open (?:roles|positions))\b",
    re.IGNORECASE,
)

# A phone number as a person reads it. Used only to *withhold* the
# "no way to reach anyone" claim, so a loose pattern is the safe direction.
PHONE_TEXT_RE = re.compile(
    r"(?:\+\d{1,2}[\s.-]?)?\(?\b\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b")

# -- heavyweight: documents and forms that move by hand ---------------------
MANUAL_INTAKE_RE = {
    "documents returned by email or fax": re.compile(
        r"(?:e-?mail|fax|mail)\s+(?:it|them|us|back|your|the|completed)[^.!?]{0,60}?"
        r"\b(form|application|document|paperwork|declaration|dec page|policy)\b",
        re.IGNORECASE),
    "printable form to complete and return": re.compile(
        r"\b(print|download|complete|fill out)\b[^.!?]{0,60}?\b(form|application)\b"
        r"[^.!?]{0,60}?\b(return|mail|fax|e-?mail|bring|drop off|sign)\b",
        re.IGNORECASE),
    "ACORD paper form referenced": re.compile(r"\bacord\b[^.!?]{0,40}\b(form|application|25|125)\b",
                                              re.IGNORECASE),
}

# -- heavyweight: routine service work routed to a person -------------------
# Requires BOTH a service-request noun and a human channel in the same breath.
# "Request Certificate" as a portal link must NOT fire; "call us for a
# certificate" must.
SERVICE_NOUNS = (r"certificates? of insurance|certificates?|\bcoi\b|auto id card|id cards?"
                 r"|policy change|add (?:a )?(?:driver|vehicle)|proof of insurance"
                 r"|declaration page|dec page")
HUMAN_CHANNEL = r"call|phone|e-?mail|fax|stop by|come in|in person|contact (?:us|our office)"
SERVICE_REQUEST_MANUAL_RE = re.compile(
    rf"(?:(?:{HUMAN_CHANNEL})[^.!?]{{0,60}}?(?:{SERVICE_NOUNS})"
    rf"|(?:{SERVICE_NOUNS})[^.!?]{{0,60}}?(?:{HUMAN_CHANNEL}))",
    re.IGNORECASE)

# -- medium: generic manual-process phrasing --------------------------------
MANUAL_MARKERS = {
    "call-for-quote": re.compile(
        r"(?:call|contact|phone)(?: us)?[^.!?]{0,40}?for (?:a |your |free |an )*quot(?:e|ation)s?\b",
        re.IGNORECASE),
    "we-will-get-back": re.compile(
        r"(?:we[’']ll|we will) (?:get back to you|be in touch|reach out|contact you)",
        re.IGNORECASE),
}

FAX_RE = re.compile(r"\bfax\b", re.IGNORECASE)

# Quote language, and the things that mean an online quote path exists.
QUOTE_MENTION_RE = re.compile(r"\bquot(?:e|es|ation)\b", re.IGNORECASE)
QUOTE_PATH_RE = re.compile(r"quote|rate-?quote|get-?a-?quote|apply|application", re.IGNORECASE)

PORTAL_RE = re.compile(
    r"\b(?:log ?in|sign ?in|portal|my account|client (?:center|login|area)"
    r"|policyholder|pay (?:my |your )?bill(?: online)?)\b", re.IGNORECASE)
PORTAL_HREF_RE = re.compile(r"login|signin|sign-in|portal|account|payment|paybill",
                            re.IGNORECASE)

BOARD_LINK_RE = re.compile(
    r"(?:job-boards|boards)\.(?:eu\.)?greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9._-]+)"
    r"|jobs\.(?:eu\.)?lever\.co/([A-Za-z0-9._-]+)",
    re.IGNORECASE,
)

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


def _has_online_quote(pages: dict[str, str], page_urls: dict[str, str]) -> bool:
    """Does any fetched page link to, or host, an online quote path?"""
    for url in page_urls.values():
        if QUOTE_PATH_RE.search(urlsplit(url).path):
            return True
    for html in pages.values():
        soup = BeautifulSoup(html, "html.parser")
        for anchor in soup.find_all("a", href=True):
            if QUOTE_PATH_RE.search(anchor["href"]) or \
                    QUOTE_PATH_RE.search(anchor.get_text(" ", strip=True)):
                return True
    return False


def gather(client: PoliteClient, result: CompanyResult) -> dict:
    """Read the site; return facts the other modules build on."""
    facts: dict = {"careers_url": None, "board_hints": [], "chat_widget": None,
                   "pages_checked": [], "homepage_text": ""}

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

    for html in pages.values():
        for match in BOARD_LINK_RE.finditer(html):
            gh_token, lever_token = match.group(1), match.group(2)
            hint = ("greenhouse", gh_token) if gh_token else ("lever", lever_token)
            if hint not in facts["board_hints"]:
                facts["board_hints"].append(hint)

    texts = {name: visible_text(html) for name, html in pages.items()}
    result.description = _describe(soup, texts["homepage"], texts.get("about", ""))
    result.coverage["site"] = "ok"
    facts["homepage_text"] = texts["homepage"]

    widget = _detect_widget(pages)
    facts["chat_widget"] = widget
    if widget:
        result.notes.append(f"site: live chat/support widget present ({widget})")

    # -- heavyweight: documents and forms moving by hand -------------------
    for label, pattern in MANUAL_INTAKE_RE.items():
        hit = None
        for name, text in texts.items():
            match = pattern.search(text)
            if match:
                hit = (name, match)
                break
        if hit:
            name, match = hit
            add_signal(result, "manual_intake", "site", page_urls[name],
                       f'{name} page: {label} - "{excerpt(texts[name], match, radius=40)}"')
            break

    # -- heavyweight: routine service requests routed to a person ----------
    for name, text in texts.items():
        match = SERVICE_REQUEST_MANUAL_RE.search(text)
        if match:
            add_signal(result, "service_request_manual", "site", page_urls[name],
                       f'{name} page routes service requests to a person - '
                       f'"{excerpt(text, match, radius=40)}"')
            break

    # -- heavyweight: quotes invited with no online path -------------------
    all_text = " ".join(texts.values())
    if QUOTE_MENTION_RE.search(all_text) and not _has_online_quote(pages, page_urls):
        add_signal(result, "quote_phone_only", "site", homepage.url,
                   f"site invites quotes but no online quote form or link was found "
                   f"on any of the {len(pages)} page(s) checked "
                   f"({', '.join(sorted(pages))})")

    # -- medium: no self-serve client portal -------------------------------
    has_portal = any(PORTAL_RE.search(text) for text in texts.values()) or \
        any(PORTAL_HREF_RE.search(a.get("href", ""))
            for html_body in pages.values()
            for a in BeautifulSoup(html_body, "html.parser").find_all("a", href=True))
    if not has_portal:
        add_signal(result, "capability_gap", "site", homepage.url,
                   f"no client login, portal or online bill-pay found across "
                   f"{len(pages)} page(s) checked")

    # -- medium: contact page with genuinely no human channel --------------
    # Absence is claimed only when the phone pattern is missing from the
    # rendered text too, not merely from the markup.
    contact_html = pages.get("contact")
    if contact_html:
        contact_text = texts["contact"]
        has_form = "<form" in contact_html.lower()
        has_mailto = "mailto:" in contact_html.lower()
        has_phone = ("tel:" in contact_html.lower()
                     or bool(PHONE_TEXT_RE.search(contact_text)))
        if (has_form or has_mailto) and not has_phone and not widget:
            channel = "a contact form" if has_form else "an email address"
            add_signal(result, "capability_gap", "site", page_urls["contact"],
                       f"contact page offers only {channel}; no phone number appears "
                       f"in its text and no live channel was found on it")

    # -- medium: generic manual phrasing -----------------------------------
    for marker, pattern in MANUAL_MARKERS.items():
        for name, text in texts.items():
            match = pattern.search(text)
            if match:
                add_signal(result, "manual_process_language", "site",
                           page_urls[name], f'{name} page says "{match.group(0)}"')
                break

    # -- weak: fax, its own low-weight type --------------------------------
    for name, text in texts.items():
        if FAX_RE.search(text):
            add_signal(result, "fax_number", "site", page_urls[name],
                       f"{name} page still lists a fax number")
            break

    # -- weak: growth language ---------------------------------------------
    for name, text in texts.items():
        match = GROWTH_RE.search(text)
        if match:
            add_signal(result, "growth_language", "site", page_urls[name],
                       f'{name} page says "{match.group(0)}"')
            break

    return facts
