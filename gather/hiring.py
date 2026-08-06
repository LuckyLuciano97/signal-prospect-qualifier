"""Module 2 — open roles as a pain signal.

Reuses the hiring-signals-tracker approach: the two official board APIs
(Greenhouse, Lever) are the source of truth when the company uses either,
found preferably via a board link the company itself published (module 1
collects those) and otherwise via at most two polite token guesses, where a
404 simply means "no such board".

The roles themselves are the signal:

* two or more open support/customer roles — drowning in tickets;
* two or more data/ops/analyst roles — data pain;
* broad hiring across functions — scaling, processes breaking;
* and, strongest of all, job-post text that literally names the pain
  ("manual", "backlog", "spreadsheets"). Only the few most relevant posts
  are read in full, and the matched phrase is quoted with its job title so
  a reviewer can check it in one click.

When there is no board, the careers page from module 1 is read for role
titles; when those are not machine-readable (JS-rendered pages mostly), the
module says so and reports nothing rather than guessing.
"""

from __future__ import annotations

import html as html_lib
import logging
import re

from bs4 import BeautifulSoup

from gather import add_signal, excerpt
from model import CompanyResult
from net import HostBlocked, PoliteClient

import config

log = logging.getLogger("signal.hiring")

GH_API = "https://boards-api.greenhouse.io/v1/boards"
LEVER_API = "https://api.lever.co/v0/postings"

SUPPORT_TITLE_RE = re.compile(
    r"\b(support|customer service|customer success|customer care|customer experience"
    r"|help ?desk|customer operations|cx)\b", re.IGNORECASE)

# Titles that match a support keyword only because a finance/strategy function
# mentions it in passing ("FP&A Manager - Professional Services and Partner
# Support") are excluded from the support cluster. Caught by eyeballing a real
# run: two such roles at Elastic produced a "support cluster" that the model
# had to talk the score back down from, and an opener nearly claimed a support
# hire that was actually an accountant. Losing a borderline real role to this
# guard is the acceptable direction; claiming a fake one is not.
NOT_SUPPORT_TITLE_RE = re.compile(
    r"\b(fp&a|financial planning|finance|payroll|accounting|revenue strategy"
    r"|revenue operations)\b", re.IGNORECASE)
DATA_TITLE_RE = re.compile(
    r"\b(data analyst|data engineer|data entry|analytics|business intelligence"
    r"|bi analyst|operations analyst|ops analyst|reporting|data operations|data ops"
    r"|data quality)\b", re.IGNORECASE)

# Pain language inside job-post text. Every hit is stored as a quote with its
# job title; the reviewer judges whether "manual" meant drudgery or QA.
PAIN_RE = re.compile(
    r"(manual process(?:es)?|manual(?:ly)? \w+ing|overwhelmed|backlog|spreadsheets?"
    r"|copy[- ]?past(?:e|ing)|high (?:ticket|case|support) volumes?"
    r"|(?:growing|increasing|high) volume of (?:tickets|inquiries|enquiries|requests|cases)"
    r"|repetitive tasks?|time[- ]consuming|keep up with (?:demand|growth|volume)"
    r"|scal(?:e|ing) our (?:support|operations)|drowning in)",
    re.IGNORECASE,
)

# Anchor text that plausibly is a job title on a careers page.
TITLE_WORD_RE = re.compile(
    r"\b(manager|engineer|specialist|analyst|representative|lead|coordinator"
    r"|developer|designer|director|associate|agent|advocate|scientist|architect"
    r"|recruiter|accountant|counsel|intern)\b", re.IGNORECASE)


def _probe_board(client: PoliteClient, token: str):
    """(platform, jobs) for the first board that answers, else None.

    Greenhouse job dicts: id, title, absolute_url. Lever posting dicts keep
    their raw shape because the description text rides along for free.
    """
    resp = client.get(f"{GH_API}/{token}")
    if resp.status == 200:
        try:
            name = (resp.json().get("name") or "").strip()
        except ValueError:
            name = ""
        if name:
            depts = client.get(f"{GH_API}/{token}/departments")
            if depts.status == 200:
                seen: dict[str, dict] = {}
                payload = depts.json()
                # Greenhouse returns every department ever created, most with
                # zero jobs; counting those produced "235 roles across 264
                # departments" in an early run. Only departments actually
                # hiring count.
                departments = [d for d in (payload.get("departments") or [])
                               if d.get("jobs")]
                for dept in departments:
                    for raw in dept.get("jobs") or []:
                        job_id = str(raw.get("id", ""))
                        if job_id and job_id not in seen:
                            seen[job_id] = {
                                "id": job_id,
                                "title": (raw.get("title") or "").strip(),
                                "url": (raw.get("absolute_url") or "").strip(),
                            }
                return "greenhouse", list(seen.values()), len(departments)

    resp = client.get(f"{LEVER_API}/{token}?mode=json")
    if resp.status == 200:
        try:
            postings = resp.json()
        except ValueError:
            return None
        if isinstance(postings, list) and postings:
            jobs = [{
                "id": str(p.get("id", "")),
                "title": (p.get("text") or "").strip(),
                "url": (p.get("hostedUrl") or "").strip(),
                "lever_raw": p,
            } for p in postings if isinstance(p, dict)]
            teams = {(p.get("categories") or {}).get("team", "") for p in postings
                     if isinstance(p, dict)}
            return "lever", jobs, len({t for t in teams if t})
    return None


def _job_text(client: PoliteClient, platform: str, token: str, job: dict) -> str:
    """Full description text of one posting, plain."""
    if platform == "lever":
        raw = job.get("lever_raw") or {}
        parts = [raw.get("descriptionPlain") or ""]
        for block in raw.get("lists") or []:
            parts.append(BeautifulSoup(block.get("content") or "", "html.parser")
                         .get_text(" ", strip=True))
        return " ".join(p for p in parts if p)
    resp = client.get(f"{GH_API}/{token}/jobs/{job['id']}")
    if resp.status != 200:
        return ""
    content = resp.json().get("content") or ""
    return BeautifulSoup(html_lib.unescape(content), "html.parser").get_text(" ", strip=True)


def _titles_preview(jobs: list[dict], limit: int = 4) -> str:
    titles = [j["title"] for j in jobs[:limit]]
    more = len(jobs) - len(titles)
    return "; ".join(titles) + (f" (+{more} more)" if more > 0 else "")


def _guess_tokens(result: CompanyResult) -> list[str]:
    domain_base = result.domain.removeprefix("www.").split(".")[0]
    name_slug = re.sub(r"[^a-z0-9]", "", result.company.lower())
    guesses = [domain_base]
    if name_slug and name_slug != domain_base:
        guesses.append(name_slug)
    return guesses


def _careers_page_roles(client: PoliteClient, careers_url: str) -> list[dict] | None:
    """Role titles scraped from a careers page, or None when unreadable."""
    try:
        resp = client.get(careers_url, allow_status=(200, 404))
    except (HostBlocked, PermissionError, RuntimeError):
        return None
    if resp.status != 200:
        return None
    soup = BeautifulSoup(resp.body, "html.parser")
    jobs = []
    for anchor in soup.find_all("a"):
        text = anchor.get_text(" ", strip=True)
        words = text.split()
        if 2 <= len(words) <= 8 and TITLE_WORD_RE.search(text):
            jobs.append({"id": "", "title": text, "url": careers_url})
    return jobs or None


def gather(client: PoliteClient, result: CompanyResult,
           careers_url: str | None = None,
           board_hints: list[tuple[str, str]] | None = None) -> None:
    platform = jobs = board_token = None
    dept_count = 0
    board_source_url = ""

    tokens = [t for _, t in (board_hints or [])] + _guess_tokens(result)
    tried: set[str] = set()
    try:
        for token in tokens:
            if token.lower() in tried:
                continue
            tried.add(token.lower())
            found = _probe_board(client, token)
            if found:
                platform, jobs, dept_count = found
                board_token = token
                board_source_url = (f"https://job-boards.greenhouse.io/{token}"
                                    if platform == "greenhouse"
                                    else f"https://jobs.lever.co/{token}")
                break
    except HostBlocked as exc:
        result.coverage["hiring"] = "blocked"
        result.notes.append(f"hiring: {exc}")
        return
    except PermissionError as exc:
        result.coverage["hiring"] = "robots-disallowed"
        result.notes.append(f"hiring: {exc}")
        return
    except RuntimeError as exc:
        result.coverage["hiring"] = "unreachable"
        result.notes.append(f"hiring: board API unreachable ({exc})")
        return

    source = f"{platform} board" if platform else "careers page"
    if jobs is None and careers_url:
        jobs = _careers_page_roles(client, careers_url)
        board_source_url = careers_url
        if jobs is None:
            result.coverage["hiring"] = "none-found"
            result.notes.append(
                "hiring: careers page present but roles not machine-readable "
                "(likely rendered by JavaScript); not guessed at")
            return

    if jobs is None:
        result.coverage["hiring"] = "none-found"
        result.notes.append("hiring: no public Greenhouse/Lever board found "
                            f"(tokens tried: {', '.join(sorted(tried))})")
        return

    result.coverage["hiring"] = "ok"
    support = [j for j in jobs if SUPPORT_TITLE_RE.search(j["title"])
               and not NOT_SUPPORT_TITLE_RE.search(j["title"])]
    data = [j for j in jobs if DATA_TITLE_RE.search(j["title"])
            and not SUPPORT_TITLE_RE.search(j["title"])]

    if len(support) >= 2:
        add_signal(result, "support_role_cluster", source, board_source_url,
                   f"{len(support)} open support/customer roles: {_titles_preview(support)}")
    if len(data) >= 2:
        add_signal(result, "data_role_cluster", source, board_source_url,
                   f"{len(data)} open data/ops/analyst roles: {_titles_preview(data)}")
    if len(support) + len(data) == 1:
        role = (support + data)[0]
        add_signal(result, "single_relevant_role", source, role["url"] or board_source_url,
                   f'1 open relevant role: "{role["title"]}"')
    if len(jobs) >= 10:
        across = f" across {dept_count} departments/teams" if dept_count else ""
        add_signal(result, "broad_hiring", source, board_source_url,
                   f"{len(jobs)} open roles{across}")

    # -- read the few most relevant posts for explicit pain language -------
    if platform is None:
        return  # careers-page titles only; no descriptions to read
    pain_hits: list[str] = []
    for job in (support + data)[: config.MAX_JOB_POSTS_READ]:
        try:
            text = _job_text(client, platform, board_token, job)
        except (HostBlocked, PermissionError, RuntimeError) as exc:
            log.debug("job text unavailable for %s: %s", job["title"], exc)
            continue
        match = PAIN_RE.search(text)
        if match:
            pain_hits.append(f'"{job["title"]}": "{excerpt(text, match, radius=45)}"')
        if len(pain_hits) == 2:
            break
    if pain_hits:
        add_signal(result, "job_post_pain", f"{platform} job post",
                   (support + data)[0]["url"] or board_source_url,
                   "job text names the pain - " + " / ".join(pain_hits))
