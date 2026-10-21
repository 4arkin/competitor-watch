"""The `cw` command. Run `bin/cw --help`, or read AGENTS.md for the whole protocol."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import date as _date
from pathlib import Path
from types import SimpleNamespace

from . import core, net
from .apply import apply as do_apply, check_decisions
from .plan import VERDICTS, plan as do_plan
from .sources import SOURCES, fetch as do_fetch

EXIT_NEEDS_TRIAGE = 3
TRIAGE_CHUNK = 50               # items per agent call; big backfill packs are split
APIFY_PRICE_PER_POST = 0.002    # harvestapi~linkedin-profile-posts, free tier, checked 2026-10-21
APIFY_FREE_USD = 5.0
TAVILY_FREE_CREDITS = 1000


def _ctx(args, backfill: int = 0) -> SimpleNamespace:
    return SimpleNamespace(date=args.date, force=getattr(args, "force", False) or bool(backfill),
                           fixture=getattr(args, "fixture", False),
                           fixture_variant=getattr(args, "fixture_variant", 1),
                           timeout=getattr(args, "timeout", 20), backfill=backfill)


# ── init / doctor / status ───────────────────────────────────────────


def cmd_init(args, cfg=None) -> int:
    w = core.ws
    for p in (w.root, w.timeline, w.competitors, w.reports, w.digests, w.cache):
        p.mkdir(parents=True, exist_ok=True)
    if not w.config.exists():
        w.config.write_text((core.REPO / "templates" / "config.example.json").read_text(encoding="utf-8"),
                            encoding="utf-8")
        print(f"created {w.config} from the template — the setup interview fills it in")
    if not w.env.exists():
        w.env.write_text((core.REPO / "templates" / "env.example").read_text(encoding="utf-8"), encoding="utf-8")
        print(f"created {w.env} — put your keys there (it is gitignored)")
    print(f"data folder ready: {w.root}")
    return 0


def _estimate(cfg) -> list:
    s = cfg["settings"]
    comps = [c for c in cfg["competitors"] if c.get("enabled", True)]
    accounts = sum(len(core.social_targets(c)) for c in comps)
    runs_per_month = 30 / max(1, int(s["cadence_days"]))
    posts_month = accounts * int(s["social"]["max_posts"]) * runs_per_month
    posts_backfill = accounts * int(s["backfill"]["max_posts"])
    tav_month = len(comps) * runs_per_month
    lines = [f"roster: {len(comps)} competitor(s), {accounts} LinkedIn account(s), a scan every {s['cadence_days']} day(s)"]
    if accounts:
        lines.append(f"Apify (upper bound): ~${posts_month * APIFY_PRICE_PER_POST:.2f}/month for regular scans, "
                     f"~${posts_backfill * APIFY_PRICE_PER_POST:.2f} once for the backfill "
                     f"(free plan: ${APIFY_FREE_USD:.0f}/month; most weeks post far fewer than max_posts)")
    lines.append(f"Tavily: ~{tav_month + len(comps):.0f} credits/month for news, plus 1 per blocked page "
                 f"(free plan: {TAVILY_FREE_CREDITS}/month; capped at {s['tavily']['monthly_cap']})")
    return lines


def cmd_doctor(args, cfg=None) -> int:
    fails, warns = [], []

    def say(kind, msg):
        print(f"{kind:4} {msg}")
        (fails if kind == "FAIL" else warns if kind == "WARN" else []).append(msg)

    if sys.version_info < (3, 9):
        say("FAIL", f"Python {sys.version.split()[0]} — need 3.9 or newer.")
    else:
        say("PASS", f"Python {sys.version.split()[0]}")
    try:
        core.ws.root.mkdir(parents=True, exist_ok=True)
        probe = core.ws.root / ".write-test"
        probe.write_text("ok")
        probe.unlink()
        say("PASS", f"data folder writable: {core.ws.root}")
    except OSError as e:
        say("FAIL", f"data folder not writable: {core.ws.root} ({e})")
    cfg = core.load_config(required=False)
    if not cfg:
        say("FAIL", f"no config at {core.ws.config} — run `bin/cw init`, then the setup interview.")
    else:
        problems = core.config_problems(cfg)
        for p in problems:
            say("FAIL", f"config: {p}")
        if not problems:
            say("PASS", f"config: {len(cfg['competitors'])} competitor(s), valid")
            for line in _estimate(cfg):
                print(f"     {line}")

    apify, tavily = os.environ.get("APIFY_TOKEN"), os.environ.get("TAVILY_API_KEY")
    if not apify:
        say("WARN", "APIFY_TOKEN not set — LinkedIn falls back to news coverage. Free key: https://console.apify.com/sign-up")
    elif args.offline:
        say("PASS", "APIFY_TOKEN set (not checked: --offline)")
    else:
        status, raw, err = net.http_json("GET", "https://api.apify.com/v2/users/me", 20,
                                         headers={"Authorization": f"Bearer {apify}"})
        if status == 200:
            say("PASS", f"Apify key works (account: {((raw or {}).get('data') or {}).get('username', '?')})")
        else:
            say("FAIL", f"Apify key rejected ({err or status}) — copy it again from https://console.apify.com/settings/integrations")
    if not tavily:
        say("WARN", "TAVILY_API_KEY not set — news comes from Google News only, and blocked pages have no fallback. "
                    "Free key: https://app.tavily.com")
    elif args.offline:
        say("PASS", "TAVILY_API_KEY set (not checked: --offline)")
    else:
        status, raw, err = net.http_json("GET", "https://api.tavily.com/usage", 20,
                                         headers={"Authorization": f"Bearer {tavily}"})
        if status == 200:
            say("PASS", "Tavily key works")
        elif status in (401, 403):
            say("FAIL", f"Tavily key rejected ({err or status}) — copy it again from https://app.tavily.com")
        else:
            say("PASS", f"TAVILY_API_KEY set (no free endpoint answered to verify it: {err or status}; "
                        f"the first fetch will tell)")
    print(f"DOCTOR: {'FAIL' if fails else 'PASS'}" + (f" ({len(fails)} to fix)" if fails else "")
          + (f", {len(warns)} warning(s)" if warns else ""))
    return 1 if fails else 0


def cmd_status(args, cfg) -> int:
    st = core.load_state()
    d = args.date
    print(f"competitor-watch {core.VERSION} · data: {core.ws.root} · {d}")
    led = st.get("tavily") or {}
    print(f"Tavily used: {led.get('week_used', 0)} this week, {led.get('month_used', 0)} this month "
          f"(caps {cfg['settings']['tavily']['weekly_cap']}/{cfg['settings']['tavily']['monthly_cap']})")
    print("")
    print(f"{'competitor':24} {'events':>6} {'last scan':10}  next due")
    for c in cfg["competitors"]:
        last = (st["competitors"].get(c["slug"]) or {}).get("last_scanned")
        every = int(c.get("every_days") or cfg["settings"]["cadence_days"])
        nxt = core.shift(last, every - 1) if last else "now"
        flag = "" if c.get("enabled", True) else " (disabled)"
        print(f"{c['slug'][:24]:24} {len(core.load_timeline(c['slug'])):>6} {last or '—':10}  {nxt}{flag}")
    if core.prune_orphans(st, cfg):
        core.save_state(st)
    rows = [(k, t) for k, t in sorted(st["targets"].items()) if t.get("health")]
    if rows:
        width = min(90, max(len(k) for k, _ in rows))
        print("")
        print(f"{'target':{width}} {'health':16} {'noisy':5} note")
        for k, t in rows:
            key = k if len(k) <= width else k[: width // 2 - 1] + "…" + k[-(width // 2):]
            print(f"{key:{width}} {str(t.get('health')):16} {'yes' if t.get('noisy') else 'no':5} {core.clip(t.get('note'), 60)}")
    return 0


def cmd_probe(args, cfg) -> int:
    """Read every configured page once and say whether the tool can diff it. Writes nothing,
    spends nothing: run it before the backfill, so unreadable pages are swapped while the
    backfill can still give the replacements their history."""
    from .normalize import normalize_page
    from .sources import MIN_CONTENT_CHARS
    bad = 0
    for c in cfg["competitors"]:
        if not c.get("enabled", True):
            continue
        for url in c.get("pages") or []:
            status, body, err = net.http_get(url, args.timeout)
            if status != 200:
                verdict = f"BLOCKED ({err or status}) — needs a Tavily key, or pick another page"
                bad += 1
            else:
                _, text = normalize_page(body.decode("utf-8", "replace"))
                if len(text) < MIN_CONTENT_CHARS:
                    verdict = f"THIN ({len(text)} chars: rendered by JavaScript) — swap for a changelog, docs or blog page"
                    bad += 1
                else:
                    verdict = f"ok ({len(text)} chars)"
            print(f"{c['slug']:16} {verdict:60} {url}")
    print(f"PROBE: {'all pages readable' if not bad else f'{bad} page(s) to fix before the backfill'}")
    return 1 if bad else 0


# ── fetch / plan / apply ─────────────────────────────────────────────


def _due(cfg, args, ignore_due=False) -> list:
    st = core.load_state()
    return core.select_competitors(cfg, st, getattr(args, "competitor", None), args.date, ignore_due)


def cmd_fetch(args, cfg) -> int:
    comps = _due(cfg, args, args.all)
    if not comps:
        print(f"nothing due on {args.date} (cadence {cfg['settings']['cadence_days']} days; `cw status` shows when).")
        return 0
    sources = SOURCES if args.source == "all" else (args.source,)
    return do_fetch(cfg, _ctx(args, args.backfill_days or 0), comps, sources)


def cmd_plan(args, cfg) -> int:
    """Plans every competitor fetched on this date (news is fetched for every scanned one)."""
    comps = _due(cfg, args, True)
    if args.competitor in (None, "all"):
        comps = [c for c in comps if (core.ws.cache / "news" / c["slug"] / f"{args.date}.json").exists()]
    if not comps:
        print(f"FAIL: nothing fetched on {args.date} — run `bin/cw fetch` first.", file=sys.stderr)
        return 2
    return do_plan(cfg, _ctx(args), comps)


def cmd_apply(args, cfg) -> int:
    return do_apply(cfg, _ctx(args), args.decisions)


# ── triage through a headless agent CLI ──────────────────────────────


def extract_json(text: str):
    """The first JSON object or array in an agent's reply (fenced or bare)."""
    m = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", text, re.S)
    candidates = [m.group(1)] if m else []
    spans = []
    for open_, close in (("{", "}"), ("[", "]")):
        a, b = text.find(open_), text.rfind(close)
        if a != -1 and b > a:
            spans.append((a, text[a:b + 1]))
    candidates += [s for _, s in sorted(spans)]      # whichever bracket opens first is the outer one
    for c in candidates:
        try:
            return json.loads(c)
        except ValueError:
            continue
    return None


def _triage_chunk(cmd: str, pack: dict, timeout: int) -> tuple:
    """Ask the agent once (plus one corrective retry). → (verdict_rows, error)."""
    ids = [i["id"] for i in pack["items"]]
    prompt = ("Triage this competitor-watch pack. Follow the `instructions` field exactly. "
              "Reply with ONLY the JSON object it asks for — no prose, no code fence.\n\n"
              + json.dumps(pack, ensure_ascii=False))
    for attempt in range(2):
        try:
            p = subprocess.run(shlex.split(cmd) + [prompt], capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            return [], f"triage command not found: {shlex.split(cmd)[0]}"
        except subprocess.TimeoutExpired:
            return [], f"triage command timed out after {timeout}s"
        raw = extract_json(p.stdout or "")
        rows = raw if isinstance(raw, list) else (raw or {}).get("verdicts") if isinstance(raw, dict) else None
        if isinstance(rows, list):
            got = {r.get("id"): r for r in rows if isinstance(r, dict)}
            bad = [i for i in ids if i not in got
                   or str(got[i].get("verdict", "")).lower() not in VERDICTS
                   or (str(got[i].get("verdict", "")).lower() == "significant" and not got[i].get("finding"))]
            if not bad:
                return [got[i] for i in ids], ""
            prompt += (f"\n\nYour previous answer missed or broke these ids: {bad[:10]}. "
                       "Every id needs a verdict; every significant item needs a finding. Answer again, JSON only.")
        else:
            err = (p.stderr or "").strip()[-300:]
            prompt += "\n\nYour previous answer was not valid JSON. Answer again with the JSON object only."
            if attempt == 1:
                return [], f"no JSON in the agent's reply (exit {p.returncode}) {net.scrub(err)}"
    return [], "the agent's verdicts did not cover every item after a retry"


def cmd_triage(args, cfg) -> int:
    cmd = args.cmd or os.environ.get("CW_TRIAGE_CMD") or cfg["settings"].get("triage_cmd")
    if not cmd:
        print("FAIL: no triage command. Pass --cmd \"claude -p\" (or codex exec / gemini -p), "
              "set CW_TRIAGE_CMD, or triage the packs yourself (AGENTS.md § Triage).", file=sys.stderr)
        return 2
    sdir = core.ws.scan_dir(args.date)
    packs = sorted((sdir / "packs").glob("*.json"))
    if not packs:
        print("no packs to triage")
        return 0
    out_dir = sdir / "decisions"
    for pp in packs:
        target = out_dir / pp.name
        if target.exists() and not args.force:
            print(f"  = {pp.stem}: already triaged")
            continue
        pack = core.read_json(pp)
        rows = []
        for k in range(0, len(pack["items"]), TRIAGE_CHUNK):
            chunk = dict(pack, items=pack["items"][k:k + TRIAGE_CHUNK])
            got, err = _triage_chunk(cmd, chunk, args.timeout)
            if err:
                print(f"FAIL: {pp.stem}: {err}", file=sys.stderr)
                return 1
            rows += got
        core.write_json_atomic(target, {"cycle": args.date, "verdicts": [
            {"id": r["id"], "verdict": str(r["verdict"]).lower(), "so_what": r.get("so_what", ""),
             "finding": r.get("finding", "")} for r in rows]})
        sig = sum(1 for r in rows if str(r["verdict"]).lower() == "significant")
        print(f"  + {pp.stem}: {len(rows)} verdict(s), {sig} significant")
    return 0


# ── run / backfill (the whole cycle) ─────────────────────────────────


def _cycle(args, cfg, comps, backfill: int) -> int:
    rc = do_fetch(cfg, _ctx(args, backfill), comps)
    if rc:
        return rc
    rc = do_plan(cfg, _ctx(args, backfill), comps)
    if rc:
        return rc
    sdir = core.ws.scan_dir(args.date)
    if any((sdir / "packs").glob("*.json")):
        if args.triage_cmd or os.environ.get("CW_TRIAGE_CMD") or cfg["settings"].get("triage_cmd"):
            rc = cmd_triage(SimpleNamespace(cmd=args.triage_cmd, date=args.date, force=False,
                                            timeout=args.triage_timeout), cfg)
            if rc:
                return rc
        else:
            scans = [core.read_json(p) for p in sdir.glob("*.json") if p.name != "decision-pack.json"]
            from .apply import load_decisions
            verdicts, _ = load_decisions(args.date, None)
            if check_decisions([s for s in scans if s and "items" in s], verdicts):
                print(f"NEXT: triage the packs in {core.ws.rel(sdir / 'packs')}/ (AGENTS.md § Triage), "
                      f"write {core.ws.rel(sdir / 'decisions')}/<slug>.json, then run `bin/cw apply`.")
                return EXIT_NEEDS_TRIAGE
    rc = do_apply(cfg, _ctx(args), None)
    if rc == 0 and args.deliver:
        rc = cmd_deliver(SimpleNamespace(date=args.date, to=None, format=None, backfill=bool(backfill)), cfg)
    return rc


def cmd_run(args, cfg) -> int:
    comps = _due(cfg, args)
    if not comps:
        print(f"nothing due on {args.date} — see `bin/cw status` for the next scan.")
        return 0
    return _cycle(args, cfg, comps, 0)


def cmd_backfill(args, cfg) -> int:
    days = args.days or int(cfg["settings"]["backfill"]["days"])
    comps = _due(cfg, args, True)
    print(f"backfilling {len(comps)} competitor(s) over {days} days — this takes a few minutes "
          f"(Google News is queried week by week, the Wayback Machine slowly)")
    return _cycle(args, cfg, comps, days)


# ── reading the timeline ─────────────────────────────────────────────


def cmd_timeline(args, cfg) -> int:
    slugs = [c["slug"] for c in cfg["competitors"]] if args.slug in (None, "all") else [args.slug]
    since = args.since or (core.shift(args.date, -args.days) if args.days else None)
    rows = []
    for slug in slugs:
        for e in core.load_timeline(slug):
            if since and (e.get("event_date") or "") < since:
                continue
            if args.verdict and e.get("verdict") != args.verdict:
                continue
            if args.source and e.get("source") != args.source:
                continue
            rows.append(dict(e, competitor=slug))
    rows.sort(key=lambda e: (e.get("event_date") or "", e["competitor"]))
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0
    for e in rows:
        head = e.get("finding") or e["fact"]
        print(f"{e.get('event_date')}  {e['competitor']:16} {e['source']:8} {e['verdict']:11} {core.clip(head, 140)}"
              + (f"  {e['url']}" if e.get("url") and e["url"] not in head else ""))
    print(f"— {len(rows)} event(s)" + (f" since {since}" if since else ""))
    return 0


# ── deliver ──────────────────────────────────────────────────────────


def cmd_deliver(args, cfg) -> int:
    """POST the digest to a webhook (Slack, Discord, or any URL that takes JSON).
    The URL is a secret: keep it in data/.env as CW_WEBHOOK_URL."""
    url = args.to or os.environ.get("CW_WEBHOOK_URL")
    fmt = args.format or os.environ.get("CW_WEBHOOK_FORMAT") or (
        "slack" if url and "hooks.slack.com" in url else "discord" if url and "discord.com" in url else "json")
    suffix = "-backfill" if args.backfill else ""
    md_p, sl_p = core.ws.digests / f"{args.date}{suffix}.md", core.ws.digests / f"{args.date}{suffix}.slack.txt"
    if not md_p.exists():
        print(f"FAIL: no digest at {core.ws.rel(md_p)} — run apply first.", file=sys.stderr)
        return 1
    md, sl = md_p.read_text(encoding="utf-8"), (sl_p.read_text(encoding="utf-8") if sl_p.exists() else "")
    if not md.strip():
        print("nothing significant this cycle — nothing sent")
        return 0
    if not url:
        print(f"no webhook set (CW_WEBHOOK_URL) — the digest is at {core.ws.rel(md_p)}")
        return 0
    if fmt == "slack":
        payloads = [{"text": sl}]
    elif fmt == "discord":
        payloads, chunk = [], ""
        for line in md.splitlines(keepends=True):          # Discord caps a message at 2,000 chars
            if len(chunk) + len(line) > 1900:
                payloads.append({"content": chunk})
                chunk = ""
            chunk += line
        payloads.append({"content": chunk})
    else:
        payloads = [{"date": args.date, "markdown": md, "slack": sl}]
    for p in payloads:
        status, _, err = net.http_json("POST", url, 20, p)
        if not (200 <= (status or 0) < 300):
            print(f"FAIL: webhook answered {err or status}", file=sys.stderr)
            return 1
    print(f"delivered {core.ws.rel(md_p)} as {fmt} ({len(payloads)} message(s))")
    return 0


# ── entry point ──────────────────────────────────────────────────────


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="cw", description="Competitor watch: a dated timeline of what your competitors do.")
    ap.add_argument("--data", help="data folder (default: <repo>/data, or $CW_DATA)")
    ap.add_argument("--date", help="cycle date YYYY-MM-DD (default: today, UTC)")
    sub = ap.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the data folder, a config template and an .env for keys")
    p = sub.add_parser("doctor", help="check Python, config and keys; estimate monthly cost")
    p.add_argument("--offline", action="store_true", help="don't call Apify/Tavily to verify keys")
    sub.add_parser("status", help="last scans, next due dates, target health")
    p = sub.add_parser("probe", help="check every configured page is readable (run before the backfill)")
    p.add_argument("--timeout", type=int, default=20)

    for name, hlp in (("fetch", "fetch sources into the cache"), ("plan", "build the triage packs"),
                      ("run", "one full cycle for every due competitor"),
                      ("backfill", "build the first timeline from the last N days")):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("--competitor", default="all", help="one slug (overrides cadence)")
        p.add_argument("--timeout", type=int, default=20)
        p.add_argument("--fixture", action="store_true", help="offline: read tests/fixtures instead of the network")
        p.add_argument("--fixture-variant", type=int, default=1)
        if name == "fetch":
            p.add_argument("--source", default="all", choices=("all",) + SOURCES)
            p.add_argument("--force", action="store_true", help="ignore the same-day cache")
            p.add_argument("--all", action="store_true", help="every enabled competitor, due or not")
            p.add_argument("--backfill-days", type=int, default=0)
        if name in ("run", "backfill"):
            p.add_argument("--triage-cmd", help='headless agent, e.g. "claude -p", "codex exec", "gemini -p"')
            p.add_argument("--triage-timeout", type=int, default=600)
            p.add_argument("--deliver", action="store_true", help="post the digest to CW_WEBHOOK_URL after apply")
        if name == "backfill":
            p.add_argument("--days", type=int, default=0)

    p = sub.add_parser("triage", help="have a headless agent CLI write the decisions")
    p.add_argument("--cmd", help='e.g. "claude -p"; default $CW_TRIAGE_CMD or settings.triage_cmd')
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument("--force", action="store_true", help="re-triage packs that already have decisions")

    p = sub.add_parser("apply", help="write verdicts into the timeline, report and digest")
    p.add_argument("--decisions", help="decisions file or folder (default: cache/scans/<date>/decisions*)")

    p = sub.add_parser("timeline", help="print the timeline (what your agent reads to connect the dots)")
    p.add_argument("slug", nargs="?", default="all")
    p.add_argument("--since", help="YYYY-MM-DD")
    p.add_argument("--days", type=int, help="the last N days")
    p.add_argument("--verdict", choices=("significant", "minor"))
    p.add_argument("--source", choices=SOURCES)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("deliver", help="post the digest to a webhook")
    p.add_argument("--to", help="webhook URL (default $CW_WEBHOOK_URL)")
    p.add_argument("--format", choices=("slack", "discord", "json"))
    p.add_argument("--backfill", action="store_true", help="send the backfill digest")

    sub.add_parser("pause", help="stop every scheduled run until `cw resume`")
    sub.add_parser("resume")

    args = ap.parse_args(argv)
    if args.data:
        core.use(Path(args.data).expanduser().resolve())
    args.date = args.date or core.today_str()
    try:
        _date.fromisoformat(args.date)
    except ValueError:
        print(f"bad --date {args.date!r} (expected YYYY-MM-DD)", file=sys.stderr)
        return 2
    core.load_env()

    if args.command == "init":
        return cmd_init(args)
    if args.command == "doctor":
        return cmd_doctor(args)
    if args.command == "pause":
        core.ws.root.mkdir(parents=True, exist_ok=True)
        core.ws.pause.write_text("paused\n")
        print("paused — scheduled runs now exit without doing anything")
        return 0
    if args.command == "resume":
        if core.ws.pause.exists():
            core.ws.pause.unlink()
        print("resumed")
        return 0
    cfg = core.load_config()
    problems = core.config_problems(cfg)
    if problems and args.command not in ("status", "timeline"):
        for p in problems:
            print(f"config: {p}", file=sys.stderr)
        return 2
    if args.command in ("fetch", "plan", "run", "backfill", "triage", "apply", "deliver") and core.paused():
        return 0
    return {"status": cmd_status, "probe": cmd_probe, "fetch": cmd_fetch, "plan": cmd_plan, "apply": cmd_apply, "run": cmd_run,
            "backfill": cmd_backfill, "triage": cmd_triage, "timeline": cmd_timeline,
            "deliver": cmd_deliver}[args.command](args, cfg)
