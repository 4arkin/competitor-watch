"""HTTP and fixtures. The only module that opens a socket (sources.py calls it)."""

from __future__ import annotations

import gzip
import json
import re
import time
import zlib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import core

UA = f"Mozilla/5.0 (compatible; CompetitorWatch/{core.VERSION}; +{core.REPO_URL})"
RETRIES = 1                     # 1 retry = 2 attempts
POLITE_SLEEP = 1.0              # seconds between requests to one host
HOST_SLEEP = {"web.archive.org": 4.0}   # the Wayback Machine 429s bulk traffic
ERR_CAP = 120
FIXTURES = core.REPO / "tests" / "fixtures"

_last_hit: dict = {}


def _polite(url: str) -> None:
    host = urllib.parse.urlsplit(url).netloc
    gap = HOST_SLEEP.get(host, POLITE_SLEEP)
    last = _last_hit.get(host)
    if last is not None:
        wait = gap - (time.time() - last)
        if wait > 0:
            time.sleep(wait)
    _last_hit[host] = time.time()


def scrub(txt: str) -> str:
    """Make an error safe to store or print: drop query strings (they can carry a
    token), mask anything token-shaped, cap the length."""
    txt = re.sub(r"(https?://[^\s'\")]+?)\?[^\s'\")]*", r"\1", txt or "")
    txt = re.sub(r"\b(token|key|apikey|api_key)=[^\s&'\")]+", r"\1=***", txt, flags=re.I)
    txt = re.sub(r"\b(tvly|apify_api)[-_][A-Za-z0-9_-]{6,}", "***", txt)
    return re.sub(r"\s+", " ", txt)[:ERR_CAP].strip()


def err_str(e: BaseException) -> str:
    """Keep the exception class and the reason: a bare URLError hides an SSL hostname
    mismatch behind 'URLError'."""
    reason = getattr(e, "reason", None)
    txt = scrub(str(reason if reason is not None else e).strip())
    name = type(e).__name__
    return f"{name}: {txt}" if txt and txt != name else name


def _decode(body: bytes, encoding: str) -> tuple:
    """→ (body, error). The Wayback Machine replays archived pages gzipped whatever the
    request asked for; undecoded, the gzip bytes diff as a whole-page change."""
    enc = (encoding or "").lower().strip()
    try:
        if enc in ("gzip", "x-gzip") or body[:2] == b"\x1f\x8b":
            return gzip.decompress(body), ""
        if enc == "deflate":
            try:
                return zlib.decompress(body), ""
            except zlib.error:
                return zlib.decompress(body, -zlib.MAX_WBITS), ""
    except (OSError, EOFError, zlib.error) as e:
        return b"", f"could not decompress {enc or 'gzip'} body ({e})"
    if enc and enc != "identity":
        return b"", f"unsupported content encoding: {enc}"
    return body, ""


def http_get(url: str, timeout: int, headers: dict | None = None) -> tuple:
    """→ (status, body_bytes, error). Status 0 means the request never got an answer."""
    err = ""
    for attempt in range(RETRIES + 1):
        _polite(url)
        h = {"User-Agent": UA, "Accept": "text/html,application/xml;q=0.9,*/*;q=0.8",
             "Accept-Encoding": "gzip, deflate"}
        h.update(headers or {})
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
                body, derr = _decode(r.read(), r.headers.get("Content-Encoding"))
                return (r.getcode(), body, "") if not derr else (0, b"", derr)
        except urllib.error.HTTPError as e:
            body = b""
            try:
                body = e.read()
            except OSError:
                pass
            if e.code in (403, 429, 503) and attempt < RETRIES:
                err = f"HTTP {e.code}"
                retry_after = (e.headers or {}).get("Retry-After") if e.code == 429 else None
                if retry_after and str(retry_after).isdigit() and int(retry_after) <= 30:
                    time.sleep(int(retry_after))
                continue
            return e.code, body, f"HTTP {e.code}"
        except Exception as e:                      # timeout, DNS, TLS
            err = err_str(e)
    return 0, b"", err or "transport error"


def http_json(method: str, url: str, timeout: int, payload=None, headers: dict | None = None) -> tuple:
    """→ (status, parsed_json_or_None, error). Never logs the payload or headers."""
    h = {"User-Agent": UA, "Accept": "application/json"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.getcode(), (json.loads(raw) if raw.strip() else None), ""
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:200]
        except OSError:
            pass
        detail = scrub(body)
        return e.code, None, f"HTTP {e.code}" + (f": {detail}" if detail else "")
    except ValueError as e:
        return 0, None, f"bad JSON: {scrub(str(e))}"
    except Exception as e:
        return 0, None, err_str(e)


# ── fixtures (offline runs and tests) ────────────────────────────────


def fixture_path(slug: str, rel: str, variant: int) -> Path:
    """tests/fixtures/<slug>/<rel>; variant 2 prefers a `.v2` sibling, so an offline run
    can simulate a changed page or sitemap."""
    base = FIXTURES / slug / rel
    if variant and variant > 1:
        alt = base.with_suffix(f".v{variant}{base.suffix}")
        if alt.exists():
            return alt
    return base


def read_fixture(slug: str, rel: str, variant: int) -> bytes:
    p = fixture_path(slug, rel, variant)
    if not p.exists():
        raise FileNotFoundError(f"no fixture at {slug}/{rel}")
    return p.read_bytes()


def page_fixture_rel(url: str) -> str:
    """https://acme.example/pricing → pages/acme-example-pricing.html"""
    p = urllib.parse.urlsplit(core.normalize_url(url))
    stem = re.sub(r"[^a-z0-9]+", "-", (p.netloc + p.path).lower()).strip("-") or "root"
    return f"pages/{stem}.html"
