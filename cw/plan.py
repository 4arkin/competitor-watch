"""Turn the cache into a decision pack: what changed, minus duplicates and off-topic noise.

Deterministic and offline. The only judgment in the whole pipeline happens after this,
when an AI agent labels each pack item significant / minor / noise (prompts/triage.md).
"""

from __future__ import annotations

import re
import sys
import urllib.parse
from collections import Counter

from . import core
from .core import clip, normalize_url, read_json, sha16, write_json_atomic
from .normalize import diff_stats
from .sources import news_terms

MIN_DIFF_LINES = 3          # a page change needs ≥3 changed lines …
MIN_DIFF_CHARS = 120        # … or ≥120 changed chars, else it is below the noise floor
NOISY_STREAK = 3            # 3 low-signal changes in a row → the page is marked noisy
NEW_URL_ITEM_CAP = 8        # new sitemap URLs itemized per competitor per cycle
REMOVED_URL_ITEM_CAP = 5
PARTIAL_SITEMAP = 0.5       # losing more than half the URLs at once = a partial fetch, not a purge
DETAIL_CAP = 400
FACT_SNIPPET = 120
RECENT_EVENTS = 6           # timeline events shown to triage, so a repeat is recognised

DEGRADED_HEALTH = {"error", "blocked", "quota", "no-sitemap", "degraded-social", "thin", "partial"}
VERDICTS = ("significant", "minor", "noise")


def item_id(slug: str, source: str, identity: str) -> str:
    return f"{slug}:{source}:{sha16(identity)[:8]}"


def _item(comp, source, identity, key, fact, url, detail, event_date, target, label=""):
    return {"id": item_id(comp["slug"], source, identity), "competitor": comp.get("name") or comp["slug"],
            "slug": comp["slug"], "source": source, "event_date": event_date, "fact": fact,
            "url": url, "detail": clip(detail, DETAIL_CAP), "label": label, "target": target,
            "suspect_noise": False, "ledger_key": key}


# ── pages ────────────────────────────────────────────────────────────


def collect_pages(comp, d, seen) -> tuple:
    """→ (items, ledger_rows, low_signal_targets, status, commits)."""
    slug = comp["slug"]
    items, ledger, low, status, commits = [], [], [], {}, []
    for url in comp.get("pages") or []:
        nurl = normalize_url(url)
        path = core.ws.cache / "pages" / slug / f"{sha16(nurl)}.json"
        rec = read_json(path)
        if not rec or not rec.get("results"):
            status[nurl] = "missing"
            continue
        it = rec["results"][0]
        tkey = f"{slug}:pages:{nurl}"
        if it.get("health") == "thin":
            status[nurl] = f"thin ({it.get('content_chars', 0)} chars)"
            continue
        commits.append({"kind": "page", "path": core.ws.rel(path), "hash": it["content_hash"]})
        if not it.get("changed"):
            status[nurl] = "baseline" if it.get("base_at") == d and it.get("base_source") == "first snapshot" else "unchanged"
            continue
        n_lines, n_chars, added = diff_stats(it.get("diff_unified", ""))
        if n_lines < MIN_DIFF_LINES and n_chars < MIN_DIFF_CHARS:
            status[nurl] = f"low-signal (+{n_lines} lines / {n_chars} chars)"
            low.append(tkey)
            continue
        identity = f"pages|{nurl}|{it['content_hash']}"
        key = "p:" + sha16(identity)
        if key in seen:
            status[nurl] = "already logged"
            continue
        status[nurl] = f"changed (+{n_lines} lines / {n_chars} chars)"
        snippet = clip(" / ".join(added[:2]), FACT_SNIPPET + 40) if added else "content removed"
        since = it.get("base_at") or "the last scan"
        via = " (Wayback Machine)" if (it.get("base_source") or "").startswith("Wayback") else ""
        items.append(_item(comp, "pages", identity, key,
                           f'Page changed since {since}{via} — {nurl}: "{snippet}" ({n_lines} lines changed)',
                           nurl, it.get("diff_unified", ""), d, tkey))
        ledger.append({"key": key, "url": nurl, "kind": "pages", "date": d, "title": clip(it.get("title"), 140)})
    return items, ledger, low, status, commits


# ── sitemap ──────────────────────────────────────────────────────────


def _reliable_lastmod(lastmod: dict) -> bool:
    """Many sites stamp every URL with the build date. If one date covers most URLs, the
    field says nothing about when a page actually changed."""
    if len(lastmod) < 3:
        return False
    top = Counter(lastmod.values()).most_common(1)[0][1]
    return top / len(lastmod) <= 0.5


LOCALE_SEG = re.compile(r"^/([a-z]{2}(?:[-_][a-z]{2,4})?)(/.*)?$", re.I)


def _locale_copy(url: str, paths: set) -> bool:
    """/fr/blog/x when /blog/x exists: one page in twelve languages is one page."""
    m = LOCALE_SEG.match(urllib.parse.urlsplit(url).path)
    return bool(m) and (m.group(2) or "/") in paths


def _paths(urls) -> set:
    return {urllib.parse.urlsplit(u).path for u in urls}


def collect_sitemap(comp, d, seen, cfg) -> tuple:
    """→ (items, ledger_rows, status, commits)."""
    slug = comp["slug"]
    if comp.get("sitemap", "auto") is None:
        return [], [], {}, []
    path = core.ws.cache / "sitemaps" / f"{slug}.json"
    rec = read_json(path)
    if not rec or not rec.get("results"):
        return [], [], {"sitemap": "missing"}, []
    r = rec["results"][0]
    cur, base = r.get("urls") or [], r.get("base_urls") or []
    lastmod = r.get("lastmod") or {}
    commits = [{"kind": "sitemap", "path": core.ws.rel(path), "hash": sha16("\n".join(cur))}]
    items, ledger, dups = [], [], 0

    def emit(identity, fact, url, detail, event_date):
        nonlocal dups
        key = "m:" + sha16(identity)
        if key in seen:
            dups += 1
            return
        items.append(_item(comp, "sitemap", identity, key, fact, url, detail, event_date, f"{slug}:sitemap"))
        ledger.append({"key": key, "url": url, "kind": "sitemap", "date": d, "title": clip(fact, 140)})

    if rec.get("mode") == "backfill" and cur == base:
        days = int((rec.get("request") or {}).get("backfill_days") or cfg["settings"]["backfill"]["days"])
        since = core.shift(d, -days)
        if not _reliable_lastmod(lastmod):
            return [], [], {"sitemap": "baseline (lastmod dates missing or all the same — no history)"}, commits
        # A date stamped on dozens of URLs at once is a rebuild, not an edit.
        counts, paths = Counter(lastmod.values()), _paths(cur)
        batch = max(10, int(0.05 * len(lastmod)))
        recent = sorted(((lm, u) for u, lm in lastmod.items()
                         if since <= lm <= d and counts[lm] <= batch and not _locale_copy(u, paths)),
                        reverse=True)
        for lm, u in recent[: int(cfg["settings"]["backfill"]["sitemap_items"])]:
            emit(f"sitemap|lastmod|{u}|{lm}", f"Sitemap shows this page updated {lm}: {u}", u,
                 f"<lastmod> {lm} in {slug}'s sitemap.", lm)
        return items, ledger, {"sitemap": f"baseline + {len(items)} recently dated page(s)"}, commits

    paths = _paths(cur) | _paths(base)
    since = r.get("base_at") or "the last scan"
    added = sorted(u for u in set(cur) - set(base) if not _locale_copy(u, paths))
    removed = sorted(u for u in set(base) - set(cur) if not _locale_copy(u, paths))
    note = ""
    if r.get("truncated") and removed:
        # Only some of the index's files were read; a page leaving them may just have moved.
        note = f"; {len(removed)} removals not itemized (sitemap index only partly followed)"
        removed = []
    if removed and len(removed) > 20 and len(removed) > PARTIAL_SITEMAP * len(base):
        # A child sitemap that failed this time looks exactly like a mass deletion.
        note = f"; {len(removed)} removals ignored as a likely partial fetch"
        removed = []
    recent_floor = core.shift(d, -31)
    fresh = [u for u in added if "m:" + sha16(f"sitemap|new|{u}") not in seen]
    dups += len(added) - len(fresh)
    for u in fresh[:NEW_URL_ITEM_CAP]:
        lm = lastmod.get(u)
        emit(f"sitemap|new|{u}", f"New page in the sitemap: {u}", u,
             f"Appeared in {slug}'s sitemap since {since}.", lm if lm and recent_floor <= lm <= d else d)
    rest = fresh[NEW_URL_ITEM_CAP:]
    if rest:
        # Past the cap the new pages go to triage as one item, and each URL is still
        # ledgered under it, so none is lost once the baseline moves on.
        identity = "sitemap|new-rollup|" + sha16("\n".join(rest))
        key = "m:" + sha16(identity)
        items.append(_item(comp, "sitemap", identity, key,
                           f"{len(rest)} more new pages in the sitemap: " + ", ".join(rest[:3])
                           + (" …" if len(rest) > 3 else ""),
                           "", "\n".join(rest), d, f"{slug}:sitemap"))
        ledger.append({"key": key, "url": "", "kind": "sitemap", "date": d,
                       "title": f"{len(rest)} more new pages"})
        ledger += [{"key": "m:" + sha16(f"sitemap|new|{u}"), "item_key": key, "url": u, "kind": "sitemap",
                    "date": d, "title": f"New page in the sitemap: {u}"} for u in rest]
    for u in removed[:REMOVED_URL_ITEM_CAP]:
        emit(f"sitemap|removed|{u}", f"Page removed from the sitemap: {u}", u,
             f"Gone from {slug}'s sitemap since {since}.", d)
    extra = ""
    if len(removed) > REMOVED_URL_ITEM_CAP:
        extra += f"; +{len(removed) - REMOVED_URL_ITEM_CAP} more removed not itemized"
    first = r.get("base_at") == d and cur == base
    status = "baseline" if first else (f"{len(added)} new / {len(removed)} removed"
                                       + (f" / {dups} already logged" if dups else "") + extra + note)
    return items, ledger, {"sitemap": status}, commits


# ── news ─────────────────────────────────────────────────────────────


def _tokens(s: str) -> list:
    """Alphanumeric tokens with a '#' break wherever punctuation plus a space ends a clause,
    so a term never matches across it ("A lightning bolt. New records" is not "Bolt New")."""
    out = []
    for seg in re.split(r"[.,;:!?|·•…\"“”()]\s+|\s[-–—|·•]\s", (s or "").lower()):
        out += re.findall(r"[a-z0-9]+", seg) + ["#"]
    return out


def _mentions(toks: list, term: str) -> bool:
    """Term appears as a run of whole tokens, ignoring spacing and punctuation:
    "Base 44", "Base44" and "base44.com" all hit alias "Base44"; "base" alone never does."""
    target = "".join(re.findall(r"[a-z0-9]+", (term or "").lower()))
    if not target:
        return False
    for i in range(len(toks)):
        acc = ""
        for t in toks[i:i + 6]:
            acc += t
            if acc == target:
                return True
            if len(acc) >= len(target) or not target.startswith(acc):
                break
    return False


def news_offtopic(r: dict, comp: dict, cfg: dict):
    """→ a drop reason, or None if the item stays. In order:
    1. a URL on the competitor's own domain always stays;
    2. title + snippet + URL must name a search term (or the bare name next to a
       `news_name_with` companion) → else `no-mention`;
    3. `news_context_required` → must also contain one of the project's market_terms
       (the outlet's own name never counts) → else `no-context`;
    4. `exclude_terms` → any match drops it (`excluded`)."""
    url = r.get("url") or ""
    host, dom = core.host_of(url), (comp.get("domain") or "").lower().removeprefix("www.")
    if dom and (host == dom or host.endswith("." + dom)):
        return None
    text = f"{r.get('title') or ''} {r.get('snippet') or ''}".lower()
    toks = _tokens(f"{text} {url}")
    companions = comp.get("news_name_with") or []
    if not any(_mentions(toks, t) for t in news_terms(comp)) and not (
            companions and _mentions(toks, comp.get("name") or "")
            and any(_mentions(toks, t) for t in companions)):
        return "no-mention"
    src = (r.get("source") or "").lower().strip()
    ctx_text = text.replace(src, " ") if src else text
    market = (cfg.get("project") or {}).get("market_terms") or []
    if comp.get("news_context_required") and market and not any(
            _mentions(_tokens(ctx_text), t) for t in market):
        return "no-context"
    if any(_mentions(toks, t) for t in comp.get("exclude_terms") or []):
        return "excluded"
    return None


def news_key(item: dict) -> str:
    """Google News links are opaque per pull, so those de-dup on title + outlet;
    everything else de-dups on the tracking-stripped URL."""
    url = (item.get("url") or "").strip()
    if "news.google.com" in core.host_of(url) or not url:
        return "t:" + sha16(f"{(item.get('title') or '').strip().lower()}|"
                            f"{(item.get('source') or '').strip().lower()}")
    return "u:" + sha16(normalize_url(url))


OUTLET_TAG = re.compile(r"\s+[-–—|]\s+[^-–—|]{1,60}$")
STORY_MIN_WORDS = 5


def story_key(title: str, source: str = "") -> str | None:
    """One story under any URL: the headline minus a trailing " - Outlet" / " | Outlet",
    punctuation-free. Syndicated copies and the Google News / Tavily copies of one article
    share it. None for headlines too short to identify a story."""
    t = (title or "").strip()
    src = (source or "").strip()
    if src and t.lower().endswith(src.lower()):
        t = re.sub(r"\s*[-–—|]\s*$", "", t[: -len(src)])
    else:
        t = OUTLET_TAG.sub("", t)
    words = re.findall(r"[a-z0-9]+", t.lower())
    return "st:" + sha16(" ".join(words)) if len(words) >= STORY_MIN_WORDS else None


def collect_news(comp, d, seen, cfg) -> tuple:
    """→ (items, ledger_rows, status, offtopic)."""
    slug = comp["slug"]
    rec = read_json(core.ws.cache / "news" / slug / f"{d}.json")
    if not rec:
        return [], [], {"news": "missing"}, []
    backfill = rec.get("mode") == "backfill"
    s = cfg["settings"]
    max_items = int(s["backfill"]["max_news_items"] if backfill else s["max_news_items"])
    floor = core.shift(d, -int((rec.get("request") or {}).get("days") or 31) - 1)
    rows = sorted(rec.get("results") or [], key=lambda x: core.to_date(x.get("published_at")) or d, reverse=True)
    items, ledger, keys, dups, offtopic, old = [], [], set(), 0, [], 0
    for r in rows:
        k, sk = news_key(r), story_key(r.get("title"), r.get("source"))
        if k in seen or k in keys or (sk and (sk in seen or sk in keys)):
            dups += 1
            continue
        keys.update(x for x in (k, sk) if x)
        when = core.to_date(r.get("published_at"))
        if when and when < floor:
            old += 1
            continue
        why = news_offtopic(r, comp, cfg)
        if why:
            offtopic.append({"reason": why, "title": clip(r.get("title"), 140), "url": r.get("url") or ""})
            continue
        if len(items) >= max_items:
            continue
        src = clip(r.get("source"), 60) or "unknown outlet"
        items.append(_item(comp, "news", f"news|{k}", k,
                           f'News: "{clip(r.get("title"), 140)}" — {src}, {when or "undated"}',
                           normalize_url(r["url"]) if r.get("url") else "", r.get("snippet"),
                           when or d, f"{slug}:news"))
        ledger.append({"key": k, "url": r.get("url") or "", "kind": "news", "date": d,
                       "title": clip(r.get("title"), 140)})
        if sk:
            ledger.append({"key": sk, "item_key": k, "url": r.get("url") or "", "kind": "story", "date": d,
                           "title": clip(r.get("title"), 140)})
    status = (f"{len(rec.get('results') or [])} raw / {len(items)} new / {dups} dup / "
              f"{len(offtopic)} off-topic" + (f" / {old} too old" if old else ""))
    return items, ledger, {"news": status}, offtopic


# ── social ───────────────────────────────────────────────────────────


def collect_social(comp, d, seen) -> tuple:
    slug = comp["slug"]
    if not core.social_targets(comp):
        return [], [], {}
    rec = read_json(core.ws.cache / "social" / slug / f"{d}.json")
    if not rec:
        return [], [], {"social": "missing"}
    items, ledger, keys, dups = [], [], set(), 0
    for r in rec.get("results") or []:
        url = r.get("post_url") or ""
        k = "s:" + sha16(normalize_url(url) if url else clip(r.get("text_excerpt"), 120))
        if k in seen or k in keys:
            dups += 1
            continue
        keys.add(k)
        eng = []
        if r.get("reactions") is not None:
            eng.append(f"{r['reactions']} reactions")
        if r.get("comments") is not None:
            eng.append(f"{r['comments']} comments")
        who = r.get("target_label") or r.get("author") or "LinkedIn"
        fact = (f'LinkedIn post by {who}: "{clip(r.get("text_excerpt"), FACT_SNIPPET)}"'
                + (f" ({', '.join(eng)})" if eng else ""))
        items.append(_item(comp, "social", f"social|{k}", k, fact,
                           normalize_url(url) if url else "", r.get("text_excerpt"),
                           r.get("posted_at") or d, f"{slug}:social", label=who))
        ledger.append({"key": k, "url": url, "kind": "social", "date": d, "title": clip(r.get("text_excerpt"), 140)})
    return items, ledger, {"social": f"{len(rec.get('results') or [])} raw / {len(items)} new / {dups} dup"}


# ── scan + pack ──────────────────────────────────────────────────────


def build_scan(comp, cfg, st, d) -> dict:
    slug = comp["slug"]
    seen = core.load_seen(slug, before=d)
    p_items, p_ledger, low, p_status, p_commits = collect_pages(comp, d, seen)
    m_items, m_ledger, m_status, m_commits = collect_sitemap(comp, d, seen, cfg)
    n_items, n_ledger, n_status, offtopic = collect_news(comp, d, seen, cfg)
    s_items, s_ledger, s_status = collect_social(comp, d, seen)

    # Churn: a below-floor change extends a page's streak, a real change resets it.
    # Computed here, committed by apply, so plan never mutates state.
    updates = {}
    for tkey in set(low):
        n = int((st["targets"].get(tkey) or {}).get("consecutive_low_signal") or 0) + 1
        updates[tkey] = {"consecutive_low_signal": n, "noisy": n >= NOISY_STREAK}
    for tkey in {i["target"] for i in p_items}:
        updates[tkey] = {"consecutive_low_signal": 0, "noisy": False}

    items = p_items + m_items + n_items + s_items
    for it in items:
        t = st["targets"].get(it["target"]) or {}
        if t.get("noisy") or updates.get(it["target"], {}).get("noisy"):
            it["suspect_noise"] = True

    src_status = {f"pages:{k}": v for k, v in p_status.items()}
    for part in (m_status, n_status, s_status):
        src_status.update(part)
    health = {k: {"health": t.get("health"), "note": t.get("note"), "last_success": t.get("last_success"),
                  "noisy": t.get("noisy", False)}
              for k, t in st["targets"].items() if k.split(":")[0] == slug and t.get("health")}
    # A source counts as scanned only if its cache exists and its fetch wasn't degraded,
    # so a three-week-old cache plus a blocked fetch can't pass as a successful cycle.
    scanned = 0
    for k, v in src_status.items():
        if v == "missing":
            continue
        tkey = f"{slug}:pages:{k.split(':', 1)[1]}" if k.startswith("pages:") else f"{slug}:{k}"
        if (health.get(tkey) or {}).get("health") in DEGRADED_HEALTH:
            continue
        scanned += 1
    modes = {(read_json(core.ws.cache / src / slug / f"{d}.json") or {}).get("mode") for src in ("news", "social")}
    news_rec = read_json(core.ws.cache / "news" / slug / f"{d}.json") or {}
    days = int((news_rec.get("request") or {}).get("days") or cfg["settings"]["news_window_days"])
    return {"date": d, "slug": slug, "competitor": comp.get("name") or slug,
            "mode": "backfill" if "backfill" in modes else "regular",
            "window": {"from": core.shift(d, -days), "to": d},
            "source_status": src_status, "scanned_sources": scanned, "health": health,
            "items": items, "offtopic": offtopic,
            "ledger_candidates": p_ledger + m_ledger + n_ledger + s_ledger,
            "state_updates": updates, "commits": p_commits + m_commits,
            "counts": {"website": len(p_items) + len(m_items), "news": len(n_items), "social": len(s_items)}}


def triage_instructions() -> str:
    p = core.REPO / "prompts" / "triage.md"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def recent_timeline(slug: str, d: str) -> list:
    """The last events logged before this cycle. Triage needs them to tell a new move
    from a new article about a move already on the timeline."""
    events = [e for e in core.load_timeline(slug) if (e.get("cycle") or "") < d]
    events.sort(key=lambda e: e.get("event_date") or "", reverse=True)
    return [{"event_date": e.get("event_date"), "source": e.get("source"), "verdict": e.get("verdict"),
             "what": clip(e.get("finding") or e.get("fact"), 200)} for e in events[:RECENT_EVENTS]]


def pack_item(i: dict) -> dict:
    return {k: i[k] for k in ("id", "source", "event_date", "fact", "url", "detail", "suspect_noise")}


def plan(cfg, ctx, comps: list) -> int:
    d = ctx.date
    st = core.load_state()
    sdir = core.ws.scan_dir(d)
    scans = []
    for comp in comps:
        scan = build_scan(comp, cfg, st, d)
        write_json_atomic(sdir / f"{comp['slug']}.json", scan)
        scans.append(scan)
    scanned = sum(1 for s in scans if s["scanned_sources"] > 0)
    degraded = sorted({k for s in scans for k, v in s["health"].items() if v.get("health") in DEGRADED_HEALTH})
    if not scans or scanned == 0:
        print("FAIL: nothing scanned — every source is missing or degraded for every competitor. "
              "Run `cw fetch` first, then check `cw status`.", file=sys.stderr)
        return 2

    project = cfg.get("project") or {}
    head = {"cycle": d, "mode": "backfill" if any(s["mode"] == "backfill" for s in scans) else "regular",
            "project": {"name": project.get("name", ""), "description": project.get("description", "")},
            "instructions": triage_instructions()}
    by_slug = {c["slug"]: c for c in comps}
    packs_dir = sdir / "packs"
    for old in packs_dir.glob("*.json") if packs_dir.exists() else []:
        old.unlink()
    total = 0
    for s in scans:
        if not s["items"]:
            continue
        c = by_slug[s["slug"]]
        total += len(s["items"])
        write_json_atomic(packs_dir / f"{s['slug']}.json", dict(head, competitor={
            "slug": s["slug"], "name": s["competitor"], "domain": c.get("domain", ""),
            "why_we_watch": c.get("why", "")}, window=s["window"],
            recent_timeline=recent_timeline(s["slug"], d), items=[pack_item(i) for i in s["items"]]))
    write_json_atomic(sdir / "decision-pack.json", dict(head, competitors=[
        {"slug": s["slug"], "name": s["competitor"], "why_we_watch": by_slug[s["slug"]].get("why", ""),
         "window": s["window"], "recent_timeline": recent_timeline(s["slug"], d),
         "items": [pack_item(i) for i in s["items"]]} for s in scans if s["items"]]))
    c = {k: sum(s["counts"][k] for s in scans) for k in ("website", "news", "social")}
    print(f"wrote {core.ws.rel(sdir / 'decision-pack.json')} and {len([s for s in scans if s['items']])} "
          f"per-competitor pack(s) in {core.ws.rel(packs_dir)}/ — {total} item(s) to triage")
    print(f"plan {d}: scanned={scanned}/{len(scans)} web={c['website']} news={c['news']} "
          f"social={c['social']} degraded={len(degraded)} health={'degraded' if degraded else 'ok'}")
    return 0
