"""Fetch every source into the cache. The only module that calls the network (through net.py).

Sources per competitor:
  sitemap  new / removed URLs (and, on backfill, recent <lastmod> dates)
  pages    watched pages, diffed against the snapshot last committed by `apply`
           (on backfill: against the oldest Wayback Machine capture in the window)
  news     Google News RSS, plus Tavily news search when a key is set
  social   LinkedIn posts of the company page and the people listed in config (Apify)

Every source writes a cache file even when it finds nothing: quiet is not broken.
Health per target lands in cache/state.json.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse

from . import core, net
from .core import mark_target, normalize_url, read_json, write_json_atomic
from .normalize import (locale_copy, mentions, normalize_page, parse_rss, parse_sitemap, tokens,
                        unified_diff, url_paths)

TTL_DAYS = 0.5                  # a same-day re-run reuses the cache; --force refetches
SITEMAP_CHILD_CAP = 5           # child sitemaps followed from an index (sorted, so stable)
SITEMAP_URL_CAP = 5000
NEW_URL_ITEM_CAP = 8            # new sitemap URLs itemized (and read) per competitor per cycle
NEW_PAGE_TEXT_CAP = 1500        # text kept from each new page, for triage
MIN_CONTENT_CHARS = 80          # below this a page is a JS shell: stored, never diffed
SOCIAL_EXCERPT = 280

TAVILY_SEARCH = "https://api.tavily.com/search"
TAVILY_EXTRACT = "https://api.tavily.com/extract"
APIFY_RUN = "https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"
WAYBACK_CDX = "https://web.archive.org/cdx/search/cdx"
WAYBACK_RAW = "https://web.archive.org/web/{ts}id_/{url}"

HEALTH_OK = "ok"


def _envelope(provider: str, endpoint: str, ctx, request: dict, results, **extra) -> dict:
    rec = {"provider": provider, "endpoint": endpoint, "fetched_at": core.now_iso(),
           "source": "fixture" if ctx.fixture else "live",
           "mode": "backfill" if ctx.backfill else "regular",
           "request": request, "results": results}
    rec.update(extra)
    return rec


# ── sitemap ──────────────────────────────────────────────────────────


def _robots_sitemaps(domain: str, ctx) -> tuple:
    """→ (sitemap_urls, note, hard_error)."""
    status, body, e = net.http_get(f"https://{domain}/robots.txt", ctx.timeout)
    if not (200 <= status < 300):
        return [], f"robots.txt: {e or status}", not _clean_miss(status)
    found = re.findall(r"(?im)^\s*sitemap:\s*(\S+)\s*$", body.decode("utf-8", "replace"))
    return found, "" if found else "robots.txt lists no sitemap", False


def _clean_miss(status: int) -> bool:
    """404/410 and similar 4xx mean "not here". 0, 403, 429 and 5xx mean blocked or broken."""
    return status in (404, 410) or (400 <= status < 500 and status not in (403, 429))


def product_terms(comp) -> list:
    """The search terms minus the domain itself: on a shared domain the domain names
    the parent company, not the product."""
    dom = (comp.get("domain") or "").lower().removeprefix("www.")
    return [t for t in news_terms(comp) if t.lower().removeprefix("www.") != dom]


def names_product(url: str, comp) -> bool:
    """With `domain_shared: true` (a product on its parent company's site) a sitemap URL
    counts only if it names the product. Without it, every URL on the domain counts."""
    if not comp.get("domain_shared"):
        return True
    toks = tokens(urllib.parse.urlsplit(url).path.replace("-", " ").replace("_", " ").replace("/", " / "))
    return any(mentions(toks, t) for t in product_terms(comp))


def new_url_key(url: str) -> str:
    return "m:" + core.sha16(f"sitemap|new|{url}")


def fresh_new_urls(comp, snap: dict, seen: set) -> list:
    """New sitemap URLs, in the order plan itemizes them: not a locale copy, about the
    product, not judged before. Shared by fetch (which reads the first few) and plan."""
    cur, base = snap.get("urls") or [], snap.get("base_urls") or []
    paths = url_paths(cur) | url_paths(base)
    added = sorted(u for u in set(cur) - set(base) if not locale_copy(u, paths) and names_product(u, comp))
    return [u for u in added if new_url_key(u) not in seen]


def read_new_page(url: str, slug: str, ctx) -> dict:
    """One direct GET of a page that just appeared, so triage judges its content, not its
    URL. No Tavily fallback: those credits are for news search."""
    if ctx.fixture:
        try:
            body, status, err = net.read_fixture(slug, net.page_fixture_rel(url), ctx.fixture_variant), 200, ""
        except FileNotFoundError as e:
            return {"status": "error", "note": str(e)}
    else:
        status, body, err = net.http_get(url, ctx.timeout)
    if status != 200:
        return {"status": "blocked" if status in (403, 429, 503) else "error", "note": err or f"HTTP {status}"}
    html = (body or b"").decode("utf-8", "replace")
    if "<" not in html[:2000]:
        return {"status": "not-html", "note": "not an HTML page"}
    title, text = normalize_page(html)
    if len(text) < MIN_CONTENT_CHARS:
        return {"status": "thin", "title": title, "note": f"only {len(text)} chars of text"}
    return {"status": "ok", "title": title, "text": text[:NEW_PAGE_TEXT_CAP]}


FIXTURE_SITEMAP = "fixture:sitemap.xml"


def _sitemap_get(u: str, slug: str, ctx) -> tuple:
    """→ (status, body, error). Offline, the root is sitemap.xml and each child sitemap
    is sitemaps/<file name>; a missing fixture answers 503, like a broken server."""
    if not ctx.fixture:
        return net.http_get(u, ctx.timeout)
    rel = "sitemap.xml" if u == FIXTURE_SITEMAP else "sitemaps/" + urllib.parse.urlsplit(u).path.rsplit("/", 1)[-1]
    try:
        return 200, net.read_fixture(slug, rel, ctx.fixture_variant), ""
    except FileNotFoundError as e:
        return 503, b"", str(e)


def do_sitemap(comp, cfg, st, ctx) -> dict:
    slug = comp["slug"]
    tkey = f"{slug}:sitemap"
    conf = comp.get("sitemap", "auto")
    if conf is None:
        return {"target": tkey, "status": "skipped", "note": "sitemap not watched (config null)"}
    path = core.ws.cache / "sitemaps" / f"{slug}.json"
    prev = read_json(path)
    if not core.stale(prev, TTL_DAYS, ctx.force):
        mark_target(st, tkey, prev["results"][0].get("health", HEALTH_OK), True, "cache hit")
        return {"target": tkey, "status": "cached"}

    urls, lastmod, notes, hard = [], {}, [], False
    used, children_failed, truncated = [], 0, False
    read_last_time = set(((prev or {}).get("results") or [{}])[0].get("sources") or [])
    domain = comp.get("domain") or ""
    if ctx.fixture:
        queue, tried_robots = [FIXTURE_SITEMAP], True
    elif conf and conf != "auto":
        queue, tried_robots = [conf], False
    else:
        queue, note, hard = _robots_sitemaps(domain, ctx)
        if note:
            notes.append(note)
        if not queue:
            queue = [f"https://{domain}/sitemap.xml"]
        tried_robots = True
    child_urls, followed = set(), 0
    while queue and len(urls) < SITEMAP_URL_CAP:
        u = queue.pop(0)
        status, body, e = _sitemap_get(u, slug, ctx)
        usable = False
        if 200 <= status < 300:
            page_urls, children, lm, perr = parse_sitemap(body)
            if perr:
                notes.append(f"{u}: {perr}")     # an HTML shell served at /sitemap.xml
                hard = True
            else:
                usable = True
                used.append(u)
                urls += page_urls
                lastmod.update(lm)
                for c in sorted(children):
                    if followed >= SITEMAP_CHILD_CAP:
                        truncated = True
                        break
                    followed += 1
                    child_urls.add(c)
                    queue.append(c)
        else:
            notes.append(f"{u}: {e or status}")
            hard = hard or not _clean_miss(status)
        if not usable and u in child_urls:
            # Only a file that answered last time loses known URLs; one that never
            # answers is just missing from both snapshots.
            children_failed += u in read_last_time
        elif not usable and not tried_robots and domain:
            tried_robots = True
            more, note, h = _robots_sitemaps(domain, ctx)
            queue += [m for m in more if m != u]
            if note:
                notes.append(note)
            hard = hard or h
    truncated = truncated or bool(queue)

    if urls and children_failed:
        # An index whose child failed lists only part of the site. Diffed, the missing
        # part reads as removed pages this week and as new pages the next.
        mark_target(st, tkey, "partial", False, f"{children_failed} sitemap file(s) failed, last full "
                    f"snapshot kept: {'; '.join(notes)}")
        return {"target": tkey, "status": "error", "error": "; ".join(notes)}

    if not urls:
        health = "error" if hard else "no-sitemap"
        mark_target(st, tkey, health, False, "; ".join(notes) or "no URLs")
        return {"target": tkey, "status": "error" if hard else "no-sitemap",
                "error": "; ".join(notes) or "no sitemap published"}

    norm = {}
    for u in urls:
        norm.setdefault(normalize_url(u), core.to_date(lastmod.get(u)))
    cur = sorted(norm)[:SITEMAP_URL_CAP]
    p0 = (prev or {}).get("results", [{}])[0] if prev else {}
    base, base_at = p0.get("base_urls"), p0.get("base_at")
    note = ""
    if base is None:                       # first snapshot = the baseline
        base, base_at = cur, ctx.date
    elif p0.get("sources") is not None and p0["sources"] != used:
        # Other sitemap files than last time (a new child, a moved index, the robots.txt
        # fallback): the URL sets aren't comparable, so start over instead of diffing.
        base, base_at = cur, ctx.date
        note = "; re-baselined: sitemap files changed"
    if truncated:
        note += f"; index lists more files than the {SITEMAP_CHILD_CAP} followed, removals not itemized"
    snap = {"health": HEALTH_OK, "url_count": len(cur), "urls": cur,
            "lastmod": {u: norm[u] for u in cur if norm[u]},
            "sources": used, "truncated": truncated, "base_urls": base, "base_at": base_at}
    to_read = fresh_new_urls(comp, snap, core.load_seen(slug, before=ctx.date))[:NEW_URL_ITEM_CAP]
    snap["new_pages"] = {u: read_new_page(u, slug, ctx) for u in to_read}
    if to_read:
        note += f"; read {sum(p['status'] == 'ok' for p in snap['new_pages'].values())}/{len(to_read)} new page(s)"
    rec = _envelope("web", "sitemap", ctx,
                    {"competitor": slug, "sitemap": conf, "backfill_days": ctx.backfill}, [snap])
    write_json_atomic(path, rec)
    mark_target(st, tkey, HEALTH_OK, True, f"{len(cur)} URLs{note}")
    return {"target": tkey, "status": "ok", "count": len(cur)}


# ── pages ────────────────────────────────────────────────────────────


def _tavily_key() -> str:
    return os.environ.get("TAVILY_API_KEY") or ""


def _tavily_extract(url: str, comp, cfg, st, ctx) -> tuple:
    """→ (html_or_text, error). One credit per call."""
    if core.tavily_budget_left(st, ctx.date, cfg) <= 0:
        return "", "Tavily cap reached"
    if ctx.fixture:
        try:
            raw = json.loads(net.read_fixture(comp["slug"], "tavily_extract.json", ctx.fixture_variant))
        except FileNotFoundError as e:
            return "", str(e)
    else:
        if not _tavily_key():
            return "", "TAVILY_API_KEY not set"
        status, raw, err = net.http_json("POST", TAVILY_EXTRACT, ctx.timeout, {"urls": [url]},
                                         {"Authorization": f"Bearer {_tavily_key()}"})
        if status != 200 or not raw:
            return "", err or f"HTTP {status}"
        core.tavily_spend(st)
    for r in (raw or {}).get("results", []):
        if r.get("raw_content") or r.get("content"):
            return r.get("raw_content") or r.get("content"), ""
    return "", "Tavily extract returned no content"


def _wayback_baseline(url: str, comp, ctx) -> tuple:
    """Oldest Wayback Machine capture of `url` inside the backfill window.
    → (html, capture_date, note). Best effort: a miss or a 429 just means no history."""
    since = core.shift(ctx.date, -ctx.backfill)
    if ctx.fixture:
        try:
            rows = json.loads(net.read_fixture(comp["slug"], "wayback_cdx.json", 1))
            html = net.read_fixture(comp["slug"], net.page_fixture_rel(url).replace(".html", ".wayback.html"), 1)
        except FileNotFoundError:
            return "", None, "no Wayback fixture"
        ts = rows[1][0] if len(rows) > 1 else ""
        return html.decode("utf-8", "replace"), (f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}" if ts else None), ""
    q = urllib.parse.urlencode({"url": url, "from": since.replace("-", ""), "to": ctx.date.replace("-", ""),
                                "output": "json", "fl": "timestamp,original",
                                "filter": "statuscode:200", "collapse": "digest", "limit": "1"})
    status, body, e = net.http_get(f"{WAYBACK_CDX}?{q}", ctx.timeout)
    if status != 200:
        return "", None, f"Wayback lookup: {e or status}"
    try:
        rows = json.loads(body.decode("utf-8", "replace") or "[]")
    except ValueError:
        return "", None, "Wayback lookup: not JSON"
    if len(rows) < 2:
        return "", None, f"no Wayback capture since {since}"
    ts, original = rows[1][0], rows[1][1]
    status, body, e = net.http_get(WAYBACK_RAW.format(ts=ts, url=original), max(ctx.timeout, 30))
    if status != 200:
        return "", None, f"Wayback capture {ts}: {e or status}"
    return body.decode("utf-8", "replace"), f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}", ""


def do_pages(comp, cfg, st, ctx) -> list:
    slug = comp["slug"]
    if not comp.get("pages"):
        return [{"target": f"{slug}:pages", "status": "skipped", "note": "no pages configured"}]
    fallback = comp.get("page_fallback", "tavily_extract")
    out = []
    for url in comp["pages"]:
        nurl = normalize_url(url)
        tkey = f"{slug}:pages:{nurl}"
        path = core.ws.cache / "pages" / slug / f"{core.sha16(nurl)}.json"
        prev = read_json(path)
        if not core.stale(prev, TTL_DAYS, ctx.force):
            h = prev["results"][0].get("health", HEALTH_OK)
            mark_target(st, tkey, h, h != "thin", "cache hit")
            out.append({"target": tkey, "status": "cached"})
            continue

        html, status, err, health = "", None, "", HEALTH_OK
        if ctx.fixture:
            try:
                html = net.read_fixture(slug, net.page_fixture_rel(url), ctx.fixture_variant).decode("utf-8", "replace")
                status = 200
            except FileNotFoundError as e:
                err, health, status = str(e), "blocked", 403
        else:
            status, body, err = net.http_get(url, ctx.timeout)
            if status == 200:
                html = body.decode("utf-8", "replace")
            else:
                health = "blocked" if status in (403, 429, 503) else "error"
        if not html and fallback == "tavily_extract":
            text, ferr = _tavily_extract(url, comp, cfg, st, ctx)
            if text:
                html, health, err = text, "fallback", ""
            else:
                err = f"{err or status}; fallback: {ferr}"
        if not html:
            mark_target(st, tkey, health, False, err)
            out.append({"target": tkey, "status": "error", "error": err})
            continue              # never clobber a good snapshot with an error

        title, text = normalize_page(html)
        if len(text) < MIN_CONTENT_CHARS and fallback == "tavily_extract" and health != "fallback":
            ftext, ferr = _tavily_extract(url, comp, cfg, st, ctx)
            ftitle, fnorm = normalize_page(ftext) if ftext else ("", "")
            if len(fnorm) >= MIN_CONTENT_CHARS:
                title, text, health = ftitle or title, fnorm, "fallback"
            else:
                err = f"fallback: {ferr or 'extract text also thin'}"
        if len(text) < MIN_CONTENT_CHARS:
            health = "thin"
            err = (f"{err}; " if err else "") + f"only {len(text)} chars of text (a JS-rendered page?)"
        provider = "tavily" if health == "fallback" else "web"

        p0 = (prev or {}).get("results", [{}])[0] if prev else {}
        base = {k: p0.get(k) for k in ("base_hash", "base_text", "base_provider", "base_at", "base_source")}
        chash = core.sha16(text)
        notes = [err] if err else []
        if not base["base_hash"] and health != "thin":
            if ctx.backfill and provider == "web" and cfg["settings"]["backfill"].get("wayback", True):
                wb_html, wb_date, wb_note = _wayback_baseline(url, comp, ctx)
                _, wb_text = normalize_page(wb_html) if wb_html else ("", "")
                if len(wb_text) >= MIN_CONTENT_CHARS:
                    base = {"base_hash": core.sha16(wb_text), "base_text": wb_text, "base_provider": "web",
                            "base_at": wb_date, "base_source": f"Wayback capture {wb_date}"}
                elif wb_note:
                    notes.append(wb_note)
            if not base["base_hash"]:
                base = {"base_hash": chash, "base_text": text, "base_provider": provider,
                        "base_at": ctx.date, "base_source": "first snapshot"}
        changed = health != "thin" and bool(base["base_hash"]) and base["base_hash"] != chash
        if changed and base["base_provider"] != provider:
            # A direct fetch and a Tavily extract turn the same page into differently shaped
            # text, so a diff across the two measures the extractor, not the page.
            changed = False
            notes.append(f"re-baselined: {base['base_provider']}→{provider}")
            base = {"base_hash": chash, "base_text": text, "base_provider": provider,
                    "base_at": ctx.date, "base_source": "re-baseline"}
        rec = _envelope(provider, "page", ctx, {"competitor": slug, "url": url}, [dict(
            url=nurl, http_status=status, title=title, content_hash=chash, normalized_text=text,
            content_chars=len(text), health=health, changed=changed,
            diff_unified=unified_diff(base["base_text"] or "", text) if changed else "",
            **base)])
        write_json_atomic(path, rec)
        mark_target(st, tkey, health, health != "thin",
                    f"changed={changed}" + (f"; {'; '.join(notes)}" if notes else ""))
        out.append({"target": tkey, "status": "ok", "changed": changed, "health": health})
    return out


# ── news ─────────────────────────────────────────────────────────────


def news_terms(comp) -> list:
    """The search terms, shared by the query and by plan's off-topic filter.
    `name_in_news_query: false` drops the display name and searches the aliases alone —
    for a name that is also an ordinary word or another company's name."""
    terms = list(comp.get("aliases") or [])
    if comp.get("name_in_news_query", True) or not terms:
        terms = [comp.get("name") or comp["slug"]] + terms
    seen, out = set(), []
    for t in terms:
        t = (t or "").strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def news_query(comp) -> str:
    return "(" + " OR ".join(f'"{t}"' for t in news_terms(comp)) + ")"


def google_news_url(comp, cfg, when: str, extra: str = "") -> str:
    loc = cfg["settings"]["news_locale"]
    q = " ".join(x for x in (news_query(comp), extra, when) if x)
    return ("https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": q, "hl": loc.get("hl", "en-US"), "gl": loc.get("gl", "US"), "ceid": loc.get("ceid", "US:en")}))


def news_windows(d: str, days: int) -> list:
    """Week-long `after:/before:` windows covering `days`. Google News returns ~100 items
    per query with no paging, so one 90-day query would silently drop most of a busy
    competitor's coverage."""
    out, start = [], core.shift(d, -days)
    while start < d:
        end = min(core.shift(start, 8), core.shift(d, 1))
        out.append(f"after:{start} before:{end}")
        start = core.shift(start, 7)
    return out


def _rss(comp, cfg, ctx, when_list: list, extra: str = "") -> tuple:
    """→ (items, errors). One request per window, merged."""
    items, errs = [], []
    if ctx.fixture:
        try:
            got, perr = parse_rss(net.read_fixture(comp["slug"], "news_rss.xml", ctx.fixture_variant))
            items += got
            if perr:
                errs.append(perr)
        except FileNotFoundError as e:
            errs.append(str(e))
        return items, errs
    for when in when_list:
        status, body, e = net.http_get(google_news_url(comp, cfg, when, extra), ctx.timeout)
        if 200 <= status < 300:
            got, perr = parse_rss(body)
            items += got
            if perr:
                errs.append(perr)
        else:
            errs.append(f"Google News: {e or status}")
    return items, errs


def _tavily_news(comp, cfg, st, ctx, days: int, max_items: int) -> tuple:
    """→ (items, health, error)."""
    if not cfg["settings"]["tavily"].get("enabled", True):
        return [], HEALTH_OK, ""
    if core.tavily_budget_left(st, ctx.date, cfg) <= 0:
        return [], "quota", "Tavily cap reached for this week or month"
    if ctx.fixture:
        try:
            raw = json.loads(net.read_fixture(comp["slug"], "tavily_search.json", ctx.fixture_variant))
        except FileNotFoundError as e:
            return [], "error", str(e)
    else:
        if not _tavily_key():
            return [], HEALTH_OK, ""          # optional source: RSS carries the news alone
        payload = {"query": news_query(comp), "topic": "news", "search_depth": "basic",
                   "max_results": max_items, "start_date": core.shift(ctx.date, -days),
                   "end_date": ctx.date}
        hdr = {"Authorization": f"Bearer {_tavily_key()}"}
        status, raw, err = net.http_json("POST", TAVILY_SEARCH, ctx.timeout, payload, hdr)
        if status == 400:                     # older API revisions take `days` instead
            payload.pop("start_date"), payload.pop("end_date")
            payload["days"] = days
            status, raw, err = net.http_json("POST", TAVILY_SEARCH, ctx.timeout, payload, hdr)
        if status != 200 or not raw:
            return [], "quota" if status in (401, 402, 429, 432, 433) else "error", err or f"HTTP {status}"
        core.tavily_spend(st)
    items = [{"title": re.sub(r"\s+", " ", r.get("title") or "").strip(),
              "url": (r.get("url") or "").strip(),
              "published_at": (r.get("published_date") or r.get("published_at") or "").strip(),
              "source": core.host_of(r.get("url") or ""),
              "snippet": re.sub(r"\s+", " ", r.get("content") or "")[:400].strip(),
              "provider": "tavily"} for r in (raw or {}).get("results", [])]
    return items, HEALTH_OK, ""


def do_news(comp, cfg, st, ctx) -> dict:
    slug = comp["slug"]
    tkey = f"{slug}:news"
    path = core.ws.cache / "news" / slug / f"{ctx.date}.json"
    prev = read_json(path)
    if not core.stale(prev, TTL_DAYS, ctx.force) and prev.get("mode") == ("backfill" if ctx.backfill else "regular"):
        # Re-report the health the cache was saved with, never a fresh `ok`.
        mark_target(st, tkey, prev.get("health", HEALTH_OK), True, "cache hit")
        return {"target": tkey, "status": "cached"}

    s = cfg["settings"]
    days = ctx.backfill or core.lookback_days(comp, st, ctx.date, cfg)
    max_items = int(s["backfill"]["max_news_items"] if ctx.backfill else s["max_news_items"])
    windows = news_windows(ctx.date, days) if ctx.backfill else [f"when:{days}d"]
    items, errs = _rss(comp, cfg, ctx, windows)
    hard = list(errs) if len(errs) == len(windows) or ctx.fixture else []
    health = HEALTH_OK
    t_items, t_health, t_err = _tavily_news(comp, cfg, st, ctx, days, min(20, max_items))
    items += t_items
    if t_err:
        errs.append(f"Tavily: {t_err}")
        if t_health == "quota":
            health = "quota"
        else:
            hard.append(t_err)
    if not items and hard:
        mark_target(st, tkey, "error", False, "; ".join(errs))
        return {"target": tkey, "status": "error", "error": "; ".join(errs)}
    write_json_atomic(path, _envelope("google_news", "news", ctx,
                                      {"competitor": slug, "query": news_query(comp), "days": days,
                                       "windows": len(windows)},
                                      items, health=health, errors=errs))
    mark_target(st, tkey, health, True, f"{len(items)} raw items" + (f"; {'; '.join(errs)}" if errs else ""))
    return {"target": tkey, "status": "ok", "count": len(items), "health": health}


# ── social (LinkedIn via Apify) ──────────────────────────────────────


def _first(d: dict, *keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return None


def _int_or_none(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
    return None


def social_rows(raw) -> list:
    if isinstance(raw, list):
        return raw
    return (raw or {}).get("items") or (raw or {}).get("data") or []


def linkedin_id_date(url: str) -> str | None:
    """A LinkedIn activity/ugcPost id carries its creation time: the top 41 bits are
    milliseconds since the epoch. Dates a post the actor returned without one."""
    m = re.search(r"(?:activity|ugcPost|share)[-:](\d{18,20})", url or "")
    return core.to_date(int(m.group(1)) >> 22) if m else None


def normalize_social(raw, target: dict) -> list:
    """Actor output → [{post_url, posted_at, text_excerpt, reactions, comments, author}].
    Defensive about field names, so a swapped actor still parses. Notice rows (no URL and
    no text) and reaction/comment rows are dropped."""
    out = []
    for r in social_rows(raw):
        if not isinstance(r, dict) or (r.get("type") or "post") not in ("post", "repost", "quote"):
            continue
        text = _first(r, "content", "text", "postText", "post_text", "commentary", "description") or ""
        url = _first(r, "linkedinUrl", "post_url", "postUrl", "url", "link")
        excerpt = re.sub(r"\s+", " ", str(text)).strip()[:SOCIAL_EXCERPT]
        if not url and not excerpt:
            continue
        stats = r.get("engagement") if isinstance(r.get("engagement"), dict) else (
            r.get("stats") if isinstance(r.get("stats"), dict) else {})
        reactions = _first(stats, "likes", "total_reactions", "totalReactions", "reactions")
        if isinstance(reactions, list):
            reactions = sum(_int_or_none((x or {}).get("count")) or 0 for x in reactions)
        comments = _first(stats, "comments", "comment_count", "commentsCount")
        if stats:                 # a measured post with an absent counter has zero, not unknown
            reactions = 0 if reactions is None else reactions
            comments = 0 if comments is None else comments
        author = r.get("author") if isinstance(r.get("author"), dict) else {}
        out.append({
            "post_url": url,
            "posted_at": core.to_date(_first(r, "postedAt", "posted_at", "postedAtISO",
                                             "publishedAt", "published_at", "date", "time"))
                         or linkedin_id_date(str(url or "")),
            "text_excerpt": excerpt,
            "reactions": _int_or_none(reactions),
            "comments": _int_or_none(comments),
            "author": (author.get("name") or "").strip(),
            "target_url": target["url"], "target_label": target["label"],
            "provider": "apify",
        })
    return out


def linkedin_handle(url: str) -> str:
    m = re.search(r"/(?:company|showcase|in|school)/([^/?#]+)", urllib.parse.urlsplit(url or "").path, re.I)
    return urllib.parse.unquote(m.group(1)).strip("/") if m else ""


def actor_input(target: dict, since: str, max_posts: int, cfg) -> dict:
    """Default input for harvestapi~linkedin-profile-posts. To use another actor, set
    settings.social.actor and settings.social.actor_input: a template whose strings may
    contain {url}, {handle}, {since} and {max_posts}."""
    s = cfg["settings"]["social"]
    tpl = s.get("actor_input")
    if isinstance(tpl, dict):
        subs = {"{url}": target["url"], "{handle}": linkedin_handle(target["url"]),
                "{since}": since, "{max_posts}": str(max_posts)}
        out = {}
        for k, v in tpl.items():
            if isinstance(v, str):
                if v == "{max_posts}":
                    v = max_posts
                else:
                    for a, b in subs.items():
                        v = v.replace(a, b)
            out[k] = v
        return out
    return {"targetUrls": [target["url"]], "maxPosts": max_posts, "postedLimitDate": since,
            "includeReposts": bool(s.get("include_reposts")), "includeQuotePosts": True,
            "scrapeReactions": False, "scrapeComments": False}


def do_social(comp, cfg, st, ctx) -> dict:
    slug = comp["slug"]
    targets = core.social_targets(comp)
    if not targets:
        return {"target": f"{slug}:social", "status": "skipped", "note": "no LinkedIn accounts configured"}
    path = core.ws.cache / "social" / slug / f"{ctx.date}.json"
    prev = read_json(path)
    if not core.stale(prev, TTL_DAYS, ctx.force) and prev.get("mode") == ("backfill" if ctx.backfill else "regular"):
        h = prev.get("health", HEALTH_OK)
        mark_target(st, f"{slug}:social", h, h == HEALTH_OK, "cache hit")
        return {"target": f"{slug}:social", "status": "cached"}

    s = cfg["settings"]
    days = ctx.backfill or core.lookback_days(comp, st, ctx.date, cfg)
    since = core.shift(ctx.date, -days)
    max_posts = int(s["backfill"]["max_posts"] if ctx.backfill else s["social"]["max_posts"])
    actor = s["social"].get("actor") or "harvestapi~linkedin-profile-posts"
    token = os.environ.get("APIFY_TOKEN") or ""
    items, failed, errs = [], 0, []
    for t in targets:
        tkey = f"{slug}:social:{normalize_url(t['url'])}"
        raw, err = None, ""
        if ctx.fixture:
            try:
                raw = json.loads(net.read_fixture(slug, f"apify/{linkedin_handle(t['url'])}.json", ctx.fixture_variant))
            except FileNotFoundError as e:
                err = str(e)
        elif not token:
            err = "APIFY_TOKEN not set"
        else:
            status, raw, e = net.http_json(
                "POST", APIFY_RUN.format(actor=urllib.parse.quote(actor, safe="~")), max(ctx.timeout, 180),
                actor_input(t, since, max_posts, cfg), {"Authorization": f"Bearer {token}"})
            if not (200 <= (status or 0) < 300 and isinstance(raw, (list, dict))):
                err, raw = e or f"HTTP {status}", None
        got = normalize_social(raw, t) if raw is not None else []
        if raw is not None and not got and social_rows(raw) and not err:
            err = "actor answered with no posts in a shape we read: " + net.scrub(json.dumps(social_rows(raw)[0]))[:80]
        got = [g for g in got if not g["posted_at"] or g["posted_at"] >= since]
        if err:
            failed += 1
            errs.append(f"{t['label']}: {err}")
            mark_target(st, tkey, "degraded-social", False, err)
        else:
            mark_target(st, tkey, HEALTH_OK, True, f"{len(got)} posts")
        items += got

    # Every LinkedIn read failed: say so loudly, so it never passes as a quiet week.
    health = "degraded-social" if failed == len(targets) else HEALTH_OK
    write_json_atomic(path, _envelope("apify", "linkedin_posts", ctx,
                                      {"competitor": slug, "actor": actor, "since": since,
                                       "targets": [t["url"] for t in targets]},
                                      items, health=health, errors=errs))
    mark_target(st, f"{slug}:social", health, health == HEALTH_OK,
                f"{len(items)} posts" + (f"; {'; '.join(errs)}" if errs else ""))
    return {"target": f"{slug}:social", "status": "ok", "count": len(items), "health": health}


# ── the fetch command ────────────────────────────────────────────────

SOURCES = ("sitemap", "pages", "news", "social")


def fetch(cfg, ctx, comps: list, sources=SOURCES) -> int:
    """Fetch the given sources for the given competitors. → exit code."""
    st = core.load_state()
    orphans = core.prune_orphans(st, cfg)
    if orphans:
        print(f"pruned {len(orphans)} health row(s) for targets no longer in config")
    core.tavily_budget_left(st, ctx.date, cfg)           # roll the week/month before spending
    outcomes = []
    for comp in comps:
        for src in sources:
            try:
                if src == "sitemap":
                    outcomes.append(do_sitemap(comp, cfg, st, ctx))
                elif src == "pages":
                    outcomes += do_pages(comp, cfg, st, ctx)
                elif src == "news":
                    outcomes.append(do_news(comp, cfg, st, ctx))
                elif src == "social":
                    outcomes.append(do_social(comp, cfg, st, ctx))
            except Exception as e:                       # one bad target never kills the cycle
                tkey = f"{comp['slug']}:{src}"
                mark_target(st, tkey, "error", False, f"{type(e).__name__}: {net.scrub(str(e))}")
                outcomes.append({"target": tkey, "status": "error", "error": f"{type(e).__name__}: {e}"})
    st["runs"].setdefault(ctx.date, {})["fetched_at"] = core.now_iso()
    core.save_state(st)

    ok = [o for o in outcomes if o["status"] in ("ok", "cached", "no-sitemap")]
    errored = [o for o in outcomes if o["status"] == "error"]
    skipped = [o for o in outcomes if o["status"] == "skipped"]
    for o in errored:
        print(f"  ! {o['target']}: {o.get('error')}", file=sys.stderr)
    for o in outcomes:
        if o["status"] == "no-sitemap":
            print(f"  · {o['target']}: no sitemap published", file=sys.stderr)
        elif o["status"] == "skipped":
            print(f"  · {o['target']}: skipped — {o.get('note')}", file=sys.stderr)
    led = st["tavily"]
    print(f"fetch {ctx.date}{' (backfill ' + str(ctx.backfill) + ' days)' if ctx.backfill else ''}: "
          f"competitors={len(comps)} targets={len(outcomes)} ok={len(ok)} error={len(errored)} "
          f"skipped={len(skipped)} tavily_week={led.get('week_used', 0)} "
          f"source={'fixture' if ctx.fixture else 'live'}")
    if len(outcomes) > len(skipped) and not ok:
        print("FAIL: every target errored — this cycle measured nothing.", file=sys.stderr)
        return 1
    return 0
