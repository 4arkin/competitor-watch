"""Apply the triage verdicts: grow each competitor's timeline, then render the human views.

Order matters: timeline rows are written before the de-dup ledger, so an item can never
be marked "seen" without having landed anywhere. `noise` reaches no timeline and no
report; only its de-dup key is kept, so it is never judged twice.
"""

from __future__ import annotations

import itertools
import json
import sys
from pathlib import Path

from . import core
from .core import clip, read_json, write_json_atomic, write_text_atomic
from .plan import DEGRADED_HEALTH, VERDICTS

KEPT = ("significant", "minor")
NOTABLE_MIN, NOTABLE_MAX = 3, 6
REPORT_MAX_LINES = 200
REPORT_MINOR_CAP = 5
BACKFILL_DIGEST_PER_COMPETITOR = 5
LINK_LABEL = {"news": "article", "social": "post", "pages": "page", "sitemap": "page"}


# ── decisions ────────────────────────────────────────────────────────


def load_decisions(d: str, path: str | None) -> tuple:
    """→ (verdicts_by_id, error). Reads one decisions file, a folder of them (one per
    competitor pack), or by default cache/scans/<d>/decisions.json plus decisions/*.json.
    A file may be {"verdicts": [...]} or a bare list."""
    sdir = core.ws.scan_dir(d)
    if path:
        p = Path(path)
        files = sorted(p.glob("*.json")) if p.is_dir() else [p]
    else:
        files = [f for f in [sdir / "decisions.json"] if f.exists()] + sorted((sdir / "decisions").glob("*.json"))
    if not files:
        return {}, f"no decisions found (looked for {core.ws.rel(sdir / 'decisions.json')} and {core.ws.rel(sdir / 'decisions')}/)"
    out = {}
    for f in files:
        raw = read_json(f)
        if raw is None:
            return {}, f"{f} is not valid JSON"
        rows = raw if isinstance(raw, list) else raw.get("verdicts")
        if not isinstance(rows, list):
            return {}, f"{f}: expected a list of verdicts or {{\"verdicts\": [...]}}"
        for v in rows:
            vid = (v or {}).get("id")
            verdict = str((v or {}).get("verdict") or "").strip().lower()
            if verdict not in VERDICTS:
                return {}, f"{f}: item {vid!r} has verdict {v.get('verdict')!r}; expected one of {VERDICTS}"
            out[vid] = {"verdict": verdict, "so_what": str(v.get("so_what") or "").strip(),
                        "finding": str(v.get("finding") or "").strip()}
    return out, ""


def check_decisions(scans: list, verdicts: dict) -> list:
    ids = [i["id"] for s in scans for i in s["items"]]
    problems = []
    missing = [i for i in ids if i not in verdicts]
    unknown = [i for i in verdicts if i not in set(ids)]
    no_finding = [k for k, v in verdicts.items() if v["verdict"] == "significant" and not v["finding"]]
    if missing:
        problems.append(f"{len(missing)} item(s) have no verdict: {missing[:5]}")
    if unknown:
        problems.append(f"{len(unknown)} verdict(s) for ids not in the pack: {unknown[:5]}")
    if no_finding:
        problems.append(f"{len(no_finding)} significant item(s) without a finding: {no_finding[:5]}")
    return problems


# ── commits (move the page/sitemap baselines forward) ────────────────


def commit_baselines(scan: dict, d: str) -> None:
    """Pages and sitemaps diff against the snapshot committed here, not against the last
    fetch, so a second fetch before apply can never swallow a change."""
    for c in scan.get("commits") or []:
        path = core.ws.root / c["path"]
        rec = read_json(path)
        if not rec or not rec.get("results"):
            continue
        r = rec["results"][0]
        if c["kind"] == "page" and r.get("content_hash") == c["hash"]:
            r.update(base_hash=r["content_hash"], base_text=r["normalized_text"], base_provider=rec.get("provider"),
                     base_at=d, base_source="committed", changed=False, diff_unified="")
            write_json_atomic(path, rec)
        elif c["kind"] == "sitemap" and core.sha16("\n".join(r.get("urls") or [])) == c["hash"]:
            r.update(base_urls=r.get("urls") or [], base_at=d)
            write_json_atomic(path, rec)


# ── timeline ─────────────────────────────────────────────────────────


def append_timeline(slug: str, events: list) -> int:
    have = {e.get("id") for e in core.load_timeline(slug)}
    new = [e for e in events if e["id"] not in have]
    if new:
        p = core.timeline_path(slug)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            for e in new:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    return len(new)


def event_from(item: dict, verdict: dict, cycle: str, mode: str) -> dict:
    return {"id": item["id"], "event_date": item.get("event_date") or cycle, "cycle": cycle, "mode": mode,
            "source": item["source"], "verdict": verdict["verdict"], "fact": item["fact"],
            "url": item.get("url") or "", "label": item.get("label") or "",
            "finding": verdict.get("finding") or "", "so_what": verdict.get("so_what") or ""}


def render_competitor(comp: dict) -> str:
    """competitors/<slug>.md — a readable view of the timeline. Generated; the facts
    only (the interpretive so_what stays in timeline/<slug>.jsonl and the reports)."""
    events = sorted(core.load_timeline(comp["slug"]), key=lambda e: (e.get("event_date") or "", e["id"]), reverse=True)
    social = [s["label"] for s in core.social_targets(comp)]
    watching = [f"{len(comp.get('pages') or [])} page(s)"]
    if comp.get("sitemap", "auto") is not None:
        watching.append("sitemap")
    watching.append("news")
    if social:
        watching.append("LinkedIn: " + ", ".join(social))
    L = ["---", f"name: {json.dumps(comp.get('name') or comp['slug'])}", f"slug: {comp['slug']}",
         f"domain: {comp.get('domain', '')}", f"events: {len(events)}", "generated: true", "---", "",
         f"# {comp.get('name') or comp['slug']}", "",
         f"_Generated from `timeline/{comp['slug']}.jsonl`. Change what is watched in `config.json`, not here._", ""]
    if comp.get("why"):
        L += [f"**Why we watch:** {comp['why']}", ""]
    L += [f"**Watching:** {' · '.join(watching)}", "", "## Timeline", ""]
    if not events:
        L.append("_(nothing logged yet)_")
    month = None
    for e in events:
        m = (e.get("event_date") or "")[:7]
        if m != month:
            if month is not None:
                L.append("")
            L += [f"### {m}", ""]
            month = m
        head = e.get("finding") or e["fact"]
        tag = f"**{e['verdict']}**" if e["verdict"] == "significant" else e["verdict"]
        url = f" ([{LINK_LABEL.get(e['source'], 'source')}]({e['url']}))" if e.get("url") and e["url"] not in head else ""
        L.append(f"- **{e.get('event_date')}** · {e['source']} · {tag} — {head}{url}")
    return "\n".join(L) + "\n"


# ── report + digest ──────────────────────────────────────────────────


def interleave(kept_by_slug: dict, verdict: str) -> list:
    """Items of one verdict, round-robin across competitors, so one busy competitor
    can't fill every slot and bury a rival's single big move."""
    lanes = [[i for i in items if i["verdict"] == verdict] for items in kept_by_slug.values()]
    return [i for tier in itertools.zip_longest(*lanes) for i in tier if i is not None]


def pick_notable(kept_by_slug: dict) -> list:
    notable = interleave(kept_by_slug, "significant")
    if len(notable) < NOTABLE_MIN:
        notable += interleave(kept_by_slug, "minor")
    return notable[:NOTABLE_MAX]


def render_report(d, mode, scans, kept_by_slug, degraded, cfg) -> str:
    counts = {"website": 0, "news": 0, "social": 0}
    for items in kept_by_slug.values():
        for i in items:
            counts["website" if i["source"] in ("pages", "sitemap") else i["source"]] += 1
    n_sig = sum(1 for k in kept_by_slug.values() for i in k if i["verdict"] == "significant")
    backfill = mode == "backfill"
    days = cfg["settings"]["backfill"]["days"]
    title = (f"Competitor watch — the last {days} days, to {core.human_date(d)}" if backfill
             else f"Competitor watch — {core.human_date(d)}")
    L = ["---", f"date: {d}", f"mode: {mode}", f"competitors_scanned: {len(scans)}",
         f"items: {{website: {counts['website']}, news: {counts['news']}, social: {counts['social']}}}",
         f"significant: {n_sig}", f"health: {'degraded' if degraded else 'ok'}",
         "degraded_targets: [" + ", ".join(f'"{t}"' for t in degraded) + "]", "---", "",
         f"# {title}", "", "## Notable", ""]
    for i in pick_notable(kept_by_slug) or []:
        L.append(f"- **{i['competitor']}** ({i.get('event_date')}) — {i.get('finding') or i['fact']}"
                 + (f" — _{i['so_what']}_" if i.get("so_what") else "")
                 + (f" ([{LINK_LABEL.get(i['source'], 'source')}]({i['url']}))" if i.get("url") else ""))
    if L[-1] == "":
        L.append("_(none)_")

    L += ["", "## Per competitor", ""]
    any_kept = False
    for s in scans:
        kept = sorted(kept_by_slug.get(s["slug"]) or [], key=lambda i: i.get("event_date") or "", reverse=True)
        if not kept:
            continue
        any_kept = True
        L += [f"### {s['competitor']}", ""]
        sig = [i for i in kept if i["verdict"] == "significant"]
        minor = [i for i in kept if i["verdict"] != "significant"]
        for i in sig + minor[:REPORT_MINOR_CAP]:
            link = (f" ([{LINK_LABEL.get(i['source'], 'source')}]({i['url']}))"
                    if i.get("url") and i["url"] not in i["fact"] else "")
            L.append(f"- {i.get('event_date')} · `{i['source']}` **{i['verdict']}** — {i['fact']}{link}"
                     + (f" — _{i['so_what']}_" if i.get("so_what") else ""))
        if len(minor) > REPORT_MINOR_CAP:
            L.append(f"- _+{len(minor) - REPORT_MINOR_CAP} more minor — all in `competitors/{s['slug']}.md`_")
        L.append("")
    if not any_kept:
        L += ["_(none)_", ""]

    quiet = [s["slug"] for s in scans if not kept_by_slug.get(s["slug"])]
    L += ["## Quiet", "", (f"{len(quiet)} of {len(scans)}: " + ", ".join(f"`{q}`" for q in quiet)) if quiet
          else "_(none)_", "", "## Source health", ""]
    rows, n = [], 0
    for s in scans:
        for tkey, v in sorted((s.get("health") or {}).items()):
            n += 1
            note = v.get("note") or ""
            if v.get("health") == "ok" and not v.get("noisy") and "re-baselined" not in note:
                continue
            rows.append(f"| `{tkey}` | {v.get('health')} | {'yes' if v.get('noisy') else 'no'} | {clip(note, 70)} |")
    L.append(f"{n - len(rows)} of {n} targets ok" + (" — the rest:" if rows else ".") + " Full table: `cw status`.")
    if rows:
        L += ["", "| Target | Health | Noisy | Note |", "|---|---|---|---|"] + rows
    dropped = sum(len(s.get("offtopic") or []) for s in scans)
    L += ["", "## Method", "",
          f"- Items de-duplicated against everything already judged; {dropped} off-topic news item(s) dropped "
          f"before triage (listed under `offtopic` in `cache/scans/{d}/<slug>.json`).",
          "- Verdicts by your AI agent per `prompts/triage.md`; `noise` is logged nowhere. "
          "Every kept item is in `timeline/<slug>.jsonl`.", ""]
    if len(L) > REPORT_MAX_LINES:
        L = L[:REPORT_MAX_LINES - 1] + [f"_(cut at {REPORT_MAX_LINES} lines — the full record is in timeline/)_"]
    return "\n".join(L) + "\n"


def _slack(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_digests(d, mode, kept_by_slug, cfg) -> tuple:
    """→ (markdown, slack_mrkdwn). Significant findings only: what happened, who did it,
    a link. Empty when nothing significant happened, so a delivery step posts nothing."""
    if mode == "backfill":
        lanes = {k: [i for i in v if i["verdict"] == "significant"][:BACKFILL_DIGEST_PER_COMPETITOR]
                 for k, v in kept_by_slug.items()}
        sig = interleave(lanes, "significant")
        title = f"Competitor watch — the last {cfg['settings']['backfill']['days']} days"
    else:
        sig = interleave(kept_by_slug, "significant")
        title = f"Competitor watch — {core.human_date(d)}"
    if not sig:
        return "", ""
    md, sl = [f"**{title}**", ""], [f"*{_slack(title)}*"]
    for i in sig:
        head = i.get("finding") or clip(i["fact"], 160)
        label = LINK_LABEL.get(i["source"], "source")
        when = f" ({i['event_date']})" if mode == "backfill" and i.get("event_date") else ""
        md.append(f"- **{i['competitor']}**{when} — {head}" + (f" ([{label}]({i['url']}))" if i.get("url") else ""))
        sl.append(f"• *{_slack(i['competitor'])}*{when} — {_slack(head)}" + (f" <{i['url']}|{label}>" if i.get("url") else ""))
    return "\n".join(md) + "\n", "\n".join(sl) + "\n"


# ── the apply command ────────────────────────────────────────────────


def apply(cfg, ctx, decisions_path: str | None) -> int:
    d = ctx.date
    sdir = core.ws.scan_dir(d)
    scans = [read_json(p) for p in sorted(sdir.glob("*.json")) if p.name not in ("decision-pack.json", "decisions.json")]
    scans = [s for s in scans if s and "items" in s]
    if not scans:
        print(f"FAIL: no scans in {core.ws.rel(sdir)} — run `cw plan` first.", file=sys.stderr)
        return 1
    n_items = sum(len(s["items"]) for s in scans)
    verdicts, err = load_decisions(d, decisions_path) if n_items else ({}, "")
    if err:
        print(f"FAIL: {err}", file=sys.stderr)
        return 1
    problems = check_decisions(scans, verdicts)
    if problems:
        for p in problems:
            print(f"FAIL: {p}", file=sys.stderr)
        return 1

    st = core.load_state()
    mode = "backfill" if any(s.get("mode") == "backfill" for s in scans) else "regular"
    kept_by_slug, added, ledgered = {}, 0, 0
    for s in scans:
        kept = [dict(i, **verdicts[i["id"]]) for i in s["items"] if verdicts[i["id"]]["verdict"] in KEPT]
        kept_by_slug[s["slug"]] = kept
        added += append_timeline(s["slug"], [event_from(i, verdicts[i["id"]], d, mode) for i in kept])
        by_key = {i["ledger_key"]: verdicts[i["id"]]["verdict"] for i in s["items"]}
        ledgered += core.append_seen(s["slug"], [dict(r, verdict=by_key[r["key"]])
                                                 for r in s.get("ledger_candidates") or [] if r["key"] in by_key])
        commit_baselines(s, d)
        for tkey, upd in (s.get("state_updates") or {}).items():
            st["targets"].setdefault(tkey, {}).update(upd)
        if s.get("scanned_sources", 0) > 0:
            st["competitors"].setdefault(s["slug"], {})["last_scanned"] = d

    for comp in cfg["competitors"]:
        if comp["slug"] in kept_by_slug or core.timeline_path(comp["slug"]).exists():
            write_text_atomic(core.ws.competitors / f"{comp['slug']}.md", render_competitor(comp))

    degraded = sorted({k for s in scans for k, v in (s.get("health") or {}).items() if v.get("health") in DEGRADED_HEALTH})
    suffix = "-backfill" if mode == "backfill" else ""
    report = core.ws.reports / f"{d}{suffix}.md"
    write_text_atomic(report, render_report(d, mode, scans, kept_by_slug, degraded, cfg))
    md, slack = render_digests(d, mode, kept_by_slug, cfg)
    write_text_atomic(core.ws.digests / f"{d}{suffix}.md", md)
    write_text_atomic(core.ws.digests / f"{d}{suffix}.slack.txt", slack)
    st["runs"].setdefault(d, {})["applied_at"] = core.now_iso()
    core.save_state(st)

    n_sig = sum(1 for k in kept_by_slug.values() for i in k if i["verdict"] == "significant")
    n_kept = sum(len(k) for k in kept_by_slug.values())
    print(f"timeline: +{added} event(s) · ledger: +{ledgered} key(s) · report: {core.ws.rel(report)} · "
          f"digest: {core.ws.rel(core.ws.digests / f'{d}{suffix}.md')}" + ("" if md else " (empty: nothing significant)"))
    print(f"apply {d} ({mode}): competitors={len(scans)} kept={n_kept} significant={n_sig} "
          f"noise={n_items - n_kept} health={'degraded' if degraded else 'ok'}")
    return 0
