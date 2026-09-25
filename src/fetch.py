"""Polite HTTP fetching: timeouts, retries with backoff, robots.txt, per-host delay.

`fetch()` never raises: every failure comes back as a FetchResult with `error` set,
so a dead page can't crash a run.
"""
from __future__ import annotations

import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx
from pydantic import BaseModel
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

BOT_NAME = "NonprofitProfileBot"
USER_AGENT = f"Mozilla/5.0 (compatible; {BOT_NAME}/0.1; research prototype)"
POLITE_DELAY_S = 1.0
MAX_BYTES = 15_000_000

_client = httpx.Client(
    timeout=httpx.Timeout(15.0, connect=10.0),
    follow_redirects=True,
    headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/pdf,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en;q=1.0,*;q=0.5",
    },
)
_last_hit: dict[str, float] = {}
_robots: dict[str, RobotFileParser | None] = {}


class FetchResult(BaseModel):
    url: str
    final_url: str | None = None
    status: int | None = None
    content_type: str | None = None
    content: bytes = b""
    etag: str | None = None  # kept for change detection at scale
    last_modified: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.status is not None and 200 <= self.status < 300

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


class _RetryableStatus(Exception):
    def __init__(self, response: httpx.Response):
        self.response = response


def origin(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def _wait_politely(url: str) -> None:
    host = urlparse(url).netloc
    elapsed = time.monotonic() - _last_hit.get(host, 0.0)
    if elapsed < POLITE_DELAY_S:
        time.sleep(POLITE_DELAY_S - elapsed)
    _last_hit[host] = time.monotonic()


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    retry=retry_if_exception_type((httpx.TransportError, _RetryableStatus)),
    reraise=True,
)
def _get(url: str) -> tuple[httpx.Response, bytes, bool]:
    _wait_politely(url)
    with _client.stream("GET", url) as r:
        if r.status_code == 429 or r.status_code >= 500:
            raise _RetryableStatus(r)
        chunks, size, too_big = [], 0, False
        for chunk in r.iter_bytes():
            size += len(chunk)
            if size > MAX_BYTES:
                too_big = True
                break
            chunks.append(chunk)
        return r, b"".join(chunks), too_big


def _robots_for(url: str) -> RobotFileParser | None:
    o = origin(url)
    if o not in _robots:
        rp: RobotFileParser | None = None
        try:
            _wait_politely(o)
            r = _client.get(o + "/robots.txt", timeout=10.0)
            if r.status_code == 200:
                rp = RobotFileParser()
                rp.parse(r.text.splitlines())
        except httpx.HTTPError:
            pass  # no robots.txt reachable -> treat as allowed
        _robots[o] = rp
    return _robots[o]


def robots_allowed(url: str) -> bool:
    rp = _robots_for(url)
    return rp is None or rp.can_fetch(BOT_NAME, url)


def robots_sitemaps(url: str) -> list[str]:
    rp = _robots_for(url)
    return list(rp.site_maps() or []) if rp else []


def _describe(exc: Exception) -> str:
    if isinstance(exc, httpx.ConnectError):
        return f"could not connect (DNS or connection failure): {exc}"
    if isinstance(exc, httpx.TimeoutException):
        return "timed out"
    return f"{type(exc).__name__}: {exc}"


def fetch(url: str, check_robots: bool = True) -> FetchResult:
    try:
        if check_robots and not robots_allowed(url):
            return FetchResult(url=url, error="disallowed by robots.txt")
        r, body, too_big = _get(url)
    except _RetryableStatus as e:
        return FetchResult(url=url, final_url=str(e.response.url), status=e.response.status_code,
                           error=f"HTTP {e.response.status_code} after retries")
    except Exception as e:  # noqa: BLE001 - fail soft, the caller logs it
        return FetchResult(url=url, error=_describe(e))

    result = FetchResult(
        url=url,
        final_url=str(r.url),
        status=r.status_code,
        content_type=r.headers.get("content-type", "").split(";")[0].strip().lower() or None,
        content=body,
        etag=r.headers.get("etag"),
        last_modified=r.headers.get("last-modified"),
    )
    if too_big:
        result.error = f"response larger than {MAX_BYTES // 1_000_000} MB, skipped"
    elif r.status_code in (401, 403):
        result.error = f"HTTP {r.status_code} (access denied, possibly bot protection)"
    elif not 200 <= r.status_code < 300:
        result.error = f"HTTP {r.status_code}"
    return result
