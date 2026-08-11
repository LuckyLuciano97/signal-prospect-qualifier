"""Read a crawl bundle produced elsewhere (n8n, cron, a colleague) and serve
it to the gather modules as if it had just been fetched.

The bundle format is the contract between whatever crawls and this program:

    {
      "company": "Example Insurance Agency",
      "domain": "example.com",
      "team_size": 12,
      "crawled_at": "2026-08-11T18:00:00Z",
      "robots": {"fetched": true, "allows_crawl": true, "disallowed_paths": []},
      "pages": [
        {"url": "...", "page_type": "contact", "status": 200,
         "fetched_at": "...", "html": "<!doctype html>...", "text": "..."}
      ],
      "coverage": {
        "attempted": ["homepage", "about", "contact", "careers", "services"],
        "reached":   ["homepage", "contact"],
        "failed":    [{"page_type": "about", "reason": "404"}],
        "blocked":   [{"source": "reviews", "reason": "robots-disallowed"}]
      }
    }

``coverage`` is mandatory and is not inferred from ``pages``. A page that is
absent from the bundle could mean "we looked and it 404ed" or "we never
looked", and those are different sentences in the report. The crawler is the
only thing that knows which, so it has to say.

:class:`BundleClient` deliberately implements the same small surface the
gather modules already use on ``net.PoliteClient`` (``get``, ``post``,
``allowed_by_robots``, ``stats``). That is why none of the detectors,
scoring, opener or validation code needs to know a bundle exists.
"""

from __future__ import annotations

import json
import logging
import pathlib
from urllib.parse import urlsplit

from model import CompanyResult
from net import HostBlocked, Response

log = logging.getLogger("signal.bundle")

REQUIRED_KEYS = ("company", "domain", "pages", "coverage")
COVERAGE_KEYS = ("attempted", "reached", "failed", "blocked")


class BundleError(ValueError):
    """The bundle is not usable. Never guessed around, always reported."""


def _norm(url: str) -> str:
    """Compare URLs the way a person would: scheme, www and trailing / are noise."""
    split = urlsplit(url.strip())
    host = (split.netloc or "").lower().removeprefix("www.")
    path = (split.path or "/").rstrip("/") or "/"
    return f"{host}{path}"


def load(path: pathlib.Path) -> dict:
    """Read and structurally validate one bundle file."""
    try:
        bundle = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BundleError(f"{path.name}: unreadable ({exc})") from exc

    missing = [k for k in REQUIRED_KEYS if k not in bundle]
    if missing:
        raise BundleError(f"{path.name}: missing key(s) {', '.join(missing)}")
    if not isinstance(bundle["pages"], list):
        raise BundleError(f"{path.name}: 'pages' must be a list")
    coverage = bundle["coverage"]
    if not isinstance(coverage, dict):
        raise BundleError(f"{path.name}: 'coverage' must be an object")
    missing = [k for k in COVERAGE_KEYS if k not in coverage]
    if missing:
        raise BundleError(
            f"{path.name}: coverage is missing {', '.join(missing)}. Coverage is "
            f"not optional: it is what lets the report say 'could not look' "
            f"instead of inventing a reason.")
    for i, page in enumerate(bundle["pages"]):
        for key in ("url", "status"):
            if key not in page:
                raise BundleError(f"{path.name}: pages[{i}] missing '{key}'")
        if "html" not in page and "text" not in page:
            raise BundleError(f"{path.name}: pages[{i}] has neither html nor text")
    return bundle


class BundleClient:
    """Serves a bundle's pages through the client surface gather modules use.

    A requested URL that is not in the bundle raises ``RuntimeError``, which
    the gather modules already treat as "page not available" — the same way
    they treat a live fetch that failed. Nothing is invented to fill a gap.
    """

    def __init__(self, bundle: dict):
        self.bundle = bundle
        self._by_url: dict[str, dict] = {}
        self._by_path: dict[str, dict] = {}
        for page in bundle["pages"]:
            key = _norm(page["url"])
            self._by_url.setdefault(key, page)
            path = urlsplit(page["url"]).path.rstrip("/").lower()
            if path:
                self._by_path.setdefault(path, page)
        robots = bundle.get("robots") or {}
        self._allows_crawl = bool(robots.get("allows_crawl", True))
        self._disallowed = [p.lower() for p in robots.get("disallowed_paths", [])]
        # Same keys as PoliteClient so summary.json stays one shape.
        self.stats = {"network": 0, "cache": len(self._by_url), "retries": 0,
                      "failures": 0, "robots_disallowed": 0, "blocked_403": 0}

    # -- the PoliteClient surface -----------------------------------------

    def allowed_by_robots(self, url: str) -> bool:
        if not self._allows_crawl:
            return False
        path = urlsplit(url).path.lower()
        return not any(path.startswith(d) for d in self._disallowed if d)

    def get(self, url: str, allow_status: tuple[int, ...] = (200, 404),
            extra_headers: dict | None = None) -> Response:
        del extra_headers  # bundles carry no credentials, by design
        if not self.allowed_by_robots(url):
            self.stats["robots_disallowed"] += 1
            raise PermissionError(f"crawler recorded robots.txt disallowing {url}")

        page = self._by_url.get(_norm(url))
        if page is None:
            path = urlsplit(url).path.rstrip("/").lower()
            page = self._by_path.get(path) if path else None
        if page is None:
            self.stats["failures"] += 1
            raise RuntimeError(f"not in bundle: {url}")

        status = int(page.get("status", 200))
        if status == 403:
            self.stats["blocked_403"] += 1
            raise HostBlocked(f"crawler recorded 403 for {url}")
        body = page.get("html") or page.get("text") or ""
        return Response(url=page["url"], status=status, body=body,
                        from_cache=True, fetched_at=page.get("fetched_at", ""))

    def post(self, url: str, json_body: dict,
             allow_status: tuple[int, ...] = (200,),
             extra_headers: dict | None = None) -> Response:
        """Bundles are offline. A live POST here would silently reintroduce
        crawling into a mode whose whole point is that it does none."""
        del json_body, allow_status, extra_headers
        raise RuntimeError(f"bundle mode makes no live requests (POST {url})")


def apply_coverage(bundle: dict, result: CompanyResult) -> None:
    """Overwrite inferred coverage with what the crawler actually recorded.

    Called after the gather modules run. They infer coverage from what they
    could read, which is a reasonable guess but only a guess; the crawler
    knows the difference between a page it never attempted and one that
    answered 404, and the report's honesty depends on that distinction.
    """
    coverage = bundle["coverage"]
    reached = {str(x).lower() for x in coverage.get("reached", [])}
    attempted = {str(x).lower() for x in coverage.get("attempted", [])}

    if "homepage" not in attempted:
        result.coverage["site"] = "skipped"
    elif "homepage" not in reached:
        robots = bundle.get("robots") or {}
        if robots.get("fetched") and not robots.get("allows_crawl", True):
            result.coverage["site"] = "robots-disallowed"
        else:
            reason = next((f.get("reason") for f in coverage.get("failed", [])
                           if str(f.get("page_type", "")).lower() == "homepage"), "")
            result.coverage["site"] = "blocked" if "403" in str(reason) else "unreachable"
        result.notes.append(f"site: crawler could not reach the homepage"
                            + (f" ({reason})" if reason else ""))

    for failure in coverage.get("failed", []):
        page_type = failure.get("page_type", "?")
        if page_type != "homepage":
            result.notes.append(f"crawl: {page_type} page not reached "
                                f"({failure.get('reason', 'no reason given')})")

    for block in coverage.get("blocked", []):
        source = str(block.get("source", "")).lower()
        reason = block.get("reason", "blocked")
        if source in result.coverage or source in ("reviews", "hiring", "competitive"):
            result.coverage[source] = reason
        result.notes.append(f"{source or 'source'}: {reason} (recorded by the crawler)")

    crawled_at = bundle.get("crawled_at")
    if crawled_at:
        result.notes.append(f"crawl: pages fetched {crawled_at}")


def iter_bundles(directory: pathlib.Path):
    """Yield (path, bundle) for every *.json in a directory, sorted."""
    for path in sorted(directory.glob("*.json")):
        yield path, load(path)
