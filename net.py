"""A deliberately slow, deliberately loud HTTP client.

Adapted from the hiring-signals-tracker client, with two changes this tool
needs:

* **robots.txt is fetched and obeyed per host.** The tracker spoke only to
  two official job-board APIs; this tool reads arbitrary company websites,
  so every host's robots.txt is read first, our User-Agent's rules applied,
  and a disallowed path is simply not fetched — recorded as
  ``robots-disallowed``, never worked around. A ``Crawl-delay`` longer than
  our own 1s minimum is honoured.
* **403 blocks the host, not the run.** The tracker talked to one source, so
  a 403 meant stop everything. Here one company's site refusing us says
  nothing about the other companies on the list: the host is marked blocked,
  every further request to it short-circuits, and the block is surfaced in
  the company's coverage notes. Still no header games, no retries, no evasion.

Everything else is inherited: serial requests, at least one second between
requests to the same host, descriptive User-Agent with a contact address,
retry only 429/5xx honouring Retry-After, every response cached on disk so
re-runs during development cost the sources nothing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import urllib.robotparser
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests

import config

MAX_ATTEMPTS = 3

log = logging.getLogger("signal.net")


class HostBlocked(RuntimeError):
    """Raised when a host has answered 403. Callers record it and move on."""


@dataclass
class Response:
    url: str
    status: int
    body: str
    from_cache: bool
    fetched_at: str

    def json(self) -> Any:
        return json.loads(self.body)


class PoliteClient:
    """Serial, cached, rate-limited, robots-respecting GET client."""

    def __init__(
        self,
        cache_dir: Path = config.CACHE_DIR,
        cache_max_age: float = 6 * 3600,
        offline: bool = False,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_max_age = cache_max_age
        self.offline = offline
        self._last_request_at: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._blocked_hosts: set[str] = set()
        self.session = requests.Session()
        self.session.headers["User-Agent"] = config.USER_AGENT
        self.session.headers["Accept"] = "text/html,application/json;q=0.9,*/*;q=0.8"
        self.stats = {"network": 0, "cache": 0, "retries": 0, "failures": 0,
                      "robots_disallowed": 0, "blocked_403": 0}

    # -- cache ---------------------------------------------------------------

    def _cache_path(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
        return self.cache_dir / f"{digest}.json"

    def _read_cache(self, url: str) -> Response | None:
        path = self._cache_path(url)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("cache unreadable, ignoring: %s (%s)", path.name, exc)
            return None
        age = time.time() - payload.get("epoch", 0)
        if not self.offline and age > self.cache_max_age:
            return None
        return Response(url, payload["status"], payload["body"], True, payload["fetched_at"])

    def _write_cache(self, url: str, status: int, body: str, fetched_at: str) -> None:
        payload = {"url": url, "status": status, "fetched_at": fetched_at,
                   "epoch": time.time(), "body": body}
        tmp = self._cache_path(url).with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(self._cache_path(url))

    # -- robots --------------------------------------------------------------

    def _robots_for(self, host: str) -> urllib.robotparser.RobotFileParser | None:
        """Fetch and parse a host's robots.txt once per run.

        An unreachable or unparseable robots.txt is treated as 'no rules
        stated' (the standard's own default). A robots.txt we cannot fetch
        because the *host* is down will make the real request fail anyway.
        """
        if host in self._robots:
            return self._robots[host]
        parser = urllib.robotparser.RobotFileParser()
        url = f"https://{host}/robots.txt"
        try:
            resp = self._fetch(url, allow_status=(200, 301, 302, 404))
            if resp.status == 200:
                parser.parse(resp.body.splitlines())
            else:
                parser = None
        except HostBlocked:
            raise
        except Exception as exc:
            log.debug("robots.txt unavailable for %s (%s); assuming no rules", host, exc)
            parser = None
        self._robots[host] = parser
        return parser

    def allowed_by_robots(self, url: str) -> bool:
        host = urlsplit(url).netloc
        parser = self._robots_for(host)
        if parser is None:
            return True
        return parser.can_fetch(config.USER_AGENT, url)

    def _min_interval(self, host: str) -> float:
        parser = self._robots.get(host)
        delay = None
        if parser is not None:
            try:
                delay = parser.crawl_delay(config.USER_AGENT)
            except Exception:
                delay = None
        return max(config.MIN_INTERVAL_PER_HOST, float(delay or 0))

    # -- fetch ---------------------------------------------------------------

    def _throttle(self, host: str) -> None:
        last = self._last_request_at.get(host, 0.0)
        wait = self._min_interval(host) - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        self._last_request_at[host] = time.monotonic()

    @staticmethod
    def _retry_after(resp: requests.Response, attempt: int) -> float:
        raw = resp.headers.get("Retry-After")
        if raw:
            try:
                return max(0.0, float(raw))
            except ValueError:
                pass  # HTTP-date form; fall through to exponential
        return min(60.0, 2.0 ** attempt)

    def _fetch(self, url: str, allow_status: tuple[int, ...],
               json_body: dict | None = None,
               extra_headers: dict | None = None) -> Response:
        """The raw fetch loop: throttle, retry 429/5xx, halt host on 403.

        ``json_body`` switches the request to POST (needed for the Google
        Places search endpoint, which has no GET form). The cache key covers
        the body so different queries never collide. ``extra_headers`` exists
        so API keys travel in headers, never in URLs — cached files and
        evidence links must stay free of credentials.
        """
        host = urlsplit(url).netloc
        if host in self._blocked_hosts:
            raise HostBlocked(f"{host} previously answered 403; not asking again")

        # The cache key must cover everything that changes the response. Body
        # is obvious; headers are not, and the omission cost a debugging round:
        # the Places FieldMask is a header, so adding a field to it produced a
        # cache hit on the old response and the new field silently never
        # arrived. Header values are hashed rather than stored because one of
        # them is an API key and cache files are plain text on disk.
        parts = [url]
        if json_body is not None:
            parts.append(json.dumps(json_body, sort_keys=True))
        if extra_headers:
            digest = hashlib.sha256(
                json.dumps(sorted(extra_headers.items())).encode("utf-8")
            ).hexdigest()[:16]
            parts.append(f"headers={digest}")
        cache_id = "#".join(parts)
        cached = self._read_cache(cache_id)
        if cached is not None:
            self.stats["cache"] += 1
            log.debug("CACHE %s %s", cached.status, url)
            return cached

        if self.offline:
            raise RuntimeError(f"offline mode and no cached response for {url}")

        last_error = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._throttle(host)
            started = time.monotonic()
            try:
                if json_body is None:
                    resp = self.session.get(url, timeout=config.HTTP_TIMEOUT,
                                            headers=extra_headers)
                else:
                    resp = self.session.post(url, json=json_body,
                                             timeout=config.HTTP_TIMEOUT,
                                             headers=extra_headers)
            except requests.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                log.warning("REQERR attempt %d/%d %s -> %s", attempt, MAX_ATTEMPTS, url, last_error)
                self.stats["retries"] += 1
                if attempt < MAX_ATTEMPTS:
                    time.sleep(min(60.0, 2.0 ** attempt))
                continue

            self.stats["network"] += 1
            elapsed = time.monotonic() - started

            if resp.status_code == 403:
                self.stats["blocked_403"] += 1
                self._blocked_hosts.add(host)
                log.warning("BLOCKED 403 %s — noting it and leaving %s alone", url, host)
                raise HostBlocked(f"403 Forbidden from {host}. Noted; no evasion attempted.")

            if resp.status_code == 429 or resp.status_code >= 500:
                delay = self._retry_after(resp, attempt)
                last_error = f"HTTP {resp.status_code}"
                log.warning("RETRY %s attempt %d/%d %s waiting %.1fs", resp.status_code,
                            attempt, MAX_ATTEMPTS, url, delay)
                self.stats["retries"] += 1
                if attempt < MAX_ATTEMPTS:
                    time.sleep(delay)
                continue

            if resp.status_code not in allow_status and resp.status_code not in (301, 302):
                self.stats["failures"] += 1
                raise RuntimeError(f"unexpected HTTP {resp.status_code} from {url}")

            fetched_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            log.info("%s %s %s (%.2fs, %d bytes)",
                     "POST" if json_body is not None else "GET",
                     resp.status_code, url, elapsed, len(resp.content))
            self._write_cache(cache_id, resp.status_code, resp.text, fetched_at)
            return Response(url, resp.status_code, resp.text, False, fetched_at)

        self.stats["failures"] += 1
        raise RuntimeError(f"giving up on {url} after {MAX_ATTEMPTS} attempts: {last_error}")

    def get(self, url: str, allow_status: tuple[int, ...] = (200, 404),
            extra_headers: dict | None = None) -> Response:
        """GET ``url`` if robots.txt permits it.

        Raises PermissionError when robots.txt disallows the path — callers
        record that as coverage, they never route around it.
        """
        if not self.allowed_by_robots(url):
            self.stats["robots_disallowed"] += 1
            log.info("ROBOTS disallows %s — skipping", url)
            raise PermissionError(f"robots.txt of {urlsplit(url).netloc} disallows {url}")
        return self._fetch(url, allow_status, extra_headers=extra_headers)

    def post(self, url: str, json_body: dict,
             allow_status: tuple[int, ...] = (200,),
             extra_headers: dict | None = None) -> Response:
        """POST a JSON body, same politeness rules as get()."""
        if not self.allowed_by_robots(url):
            self.stats["robots_disallowed"] += 1
            log.info("ROBOTS disallows %s — skipping", url)
            raise PermissionError(f"robots.txt of {urlsplit(url).netloc} disallows {url}")
        return self._fetch(url, allow_status, json_body=json_body,
                           extra_headers=extra_headers)


def setup_logging(logfile: Path | None = None, verbose: bool = False) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if logfile:
        logfile.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(logfile, encoding="utf-8"))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-14s %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,
    )
