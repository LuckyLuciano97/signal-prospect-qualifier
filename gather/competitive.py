"""Module 4 — optional, low-weight competitive capability gap.

Runs only for rows where the user named a competitor domain, and checks one
cheaply observable thing: does the competitor's homepage carry a live chat /
support widget the company's own site lacks? That is the whole module, on
purpose. "Doing worse than a competitor" invites exactly the kind of
inference this tool bans — revenue guesses, headcount guesses, vibes — so
the comparison is restricted to a fact both sites state in their own HTML,
it carries the lowest weight in config, and it can never be the difference
between FAIL and PASS on its own.
"""

from __future__ import annotations

import logging

from gather import add_signal
from gather.site import CHAT_WIDGETS
from model import CompanyResult
from net import HostBlocked, PoliteClient

log = logging.getLogger("signal.competitive")


def gather(client: PoliteClient, result: CompanyResult, company_has_widget: bool) -> None:
    if not result.competitor_domain:
        result.coverage["competitive"] = "skipped"
        return
    if company_has_widget:
        # No gap to report; do not fetch the competitor for nothing.
        result.coverage["competitive"] = "ok"
        result.notes.append("competitive: company already has a chat widget; "
                            "no gap to check")
        return

    page = None
    try:
        for base in (f"https://{result.competitor_domain}",
                     f"https://www.{result.competitor_domain}"):
            try:
                resp = client.get(base, allow_status=(200, 404))
            except HostBlocked:
                raise
            except (PermissionError, RuntimeError):
                continue
            if resp.status == 200:
                page = resp
                break
    except HostBlocked as exc:
        result.coverage["competitive"] = "blocked"
        result.notes.append(f"competitive: {exc}")
        return

    if page is None:
        result.coverage["competitive"] = "unreachable"
        result.notes.append(f"competitive: {result.competitor_domain} could not be fetched")
        return

    result.coverage["competitive"] = "ok"
    lowered = page.body.lower()
    for name, needles in CHAT_WIDGETS.items():
        if any(needle.lower() in lowered for needle in needles):
            add_signal(
                result, "competitor_gap", "competitor site", page.url,
                f"competitor {result.competitor_domain} runs a live chat/support "
                f"widget ({name}); none detected on {result.domain}",
            )
            return
    result.notes.append(f"competitive: no chat widget on {result.competitor_domain} "
                        "either; no gap")
