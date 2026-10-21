"""Shared plumbing: the data folder, config, state, de-dup ledgers, small helpers.

Nothing in this module touches the network.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import urllib.parse
from datetime import date as _date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
VERSION = "0.1.0"
REPO_URL = "https://github.com/4arkin/competitor-watch"


# ── the data folder ──────────────────────────────────────────────────


class Workspace:
    """Every path the tool writes. All user data lives under one folder (default
    `<repo>/data`, gitignored), so updating the tool never touches a timeline."""

    def __init__(self, root):
        self.root = Path(root)
        self.config = self.root / "config.json"
        self.env = self.root / ".env"
        self.pause = self.root / ".paused"
        self.timeline = self.root / "timeline"
        self.competitors = self.root / "competitors"
        self.reports = self.root / "reports"
        self.digests = self.root / "digests"
        self.cache = self.root / "cache"
        self.state = self.cache / "state.json"
        self.seen = self.cache / "seen"
        self.scans = self.cache / "scans"

    def scan_dir(self, d: str) -> Path:
        return self.scans / d

    def rel(self, p: Path) -> str:
        try:
            return str(Path(p).relative_to(self.root))
        except ValueError:
            return str(p)


ws = Workspace(os.environ.get("CW_DATA") or REPO / "data")


def use(root) -> Workspace:
    """Point every module at another data folder (tests, or one install serving
    several projects via CW_DATA)."""
    global ws
    ws = Workspace(root)
    return ws


def load_env() -> None:
    """Read KEY=VALUE lines from data/.env into the environment. Real environment
    variables win, so a scheduler can still inject its own."""
    if not ws.env.exists():
        return
    for line in ws.env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and v and not os.environ.get(k):
            os.environ[k] = v


def paused() -> bool:
    if ws.pause.exists():
        print("PAUSED (delete data/.paused to resume)")
        return True
    return False


# ── small helpers ────────────────────────────────────────────────────


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def days_between(a: str, b: str) -> int:
    return (_date.fromisoformat(b) - _date.fromisoformat(a)).days


def shift(d: str, days: int) -> str:
    return (_date.fromisoformat(d) + timedelta(days=days)).isoformat()


def week_start(d: str) -> str:
    dd = _date.fromisoformat(d)
    return (dd - timedelta(days=dd.weekday())).isoformat()


def human_date(d: str) -> str:
    dd = _date.fromisoformat(d)
    return f"{dd.day} {dd.strftime('%b %Y')}"      # no %-d: it breaks on Windows


def sha16(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", "replace")).hexdigest()[:16]


def clip(s, n: int) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return (s[: n - 1] + "…") if len(s) > n else s


def to_date(v) -> str | None:
    """Any date shape a source hands back → 'YYYY-MM-DD', or None.
    Covers ISO-8601 (with Z), RFC 822 (RSS pubDate), 'YYYY-MM-DD HH:MM:SS',
    epoch seconds/milliseconds, and {date|timestamp} dicts."""
    if v in (None, ""):
        return None
    if isinstance(v, dict):
        return to_date(v.get("date")) or to_date(v.get("timestamp"))
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)) or (isinstance(v, str) and v.strip().isdigit()):
        ts = float(v)
        if ts > 1e11:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, timezone.utc).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    s = str(v).strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return _date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            return None
    try:
        dt = parsedate_to_datetime(s)
        if dt.tzinfo:
            dt = dt.astimezone(timezone.utc)
        return dt.date().isoformat()
    except (TypeError, ValueError, IndexError):
        return None


TRACKING_PARAMS = re.compile(
    r"^(utm_|ic|icid|gclid|fbclid|mc_|ref|oc|_hs|hsa_|sc_|s_|rcm|trk|originalSubdomain)", re.I)


def normalize_url(url: str) -> str:
    """Strip tracking params and the fragment so one page lands on one key.
    `rcm` is LinkedIn's per-fetch share token: left in, every re-pulled post looks new."""
    url = (url or "").strip()
    try:
        p = urllib.parse.urlsplit(url)
    except ValueError:
        return url
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
         if not TRACKING_PARAMS.match(k)]
    return urllib.parse.urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path or "/",
                                    urllib.parse.urlencode(sorted(q)), ""))


def host_of(url: str) -> str:
    try:
        return urllib.parse.urlsplit(url or "").netloc.lower().removeprefix("www.")
    except ValueError:
        return ""


def read_json(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_json_atomic(path: Path, obj) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return path


def write_text_atomic(path: Path, text: str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return path


def stale(record: dict | None, ttl_days: float, force: bool) -> bool:
    if force or not record:
        return True
    try:
        when = datetime.strptime(record["fetched_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (KeyError, TypeError, ValueError):
        return True
    return (datetime.now(timezone.utc) - when).total_seconds() / 86400 > ttl_days


# ── config ───────────────────────────────────────────────────────────

SETTINGS_DEFAULTS = {
    "cadence_days": 7,              # how often a competitor is due; the scheduler may fire more often
    "news_window_days": 8,          # first-cycle news window; later cycles look back to the last scan
    "max_news_items": 10,           # news items per competitor per cycle (after filtering)
    "news_locale": {"hl": "en-US", "gl": "US", "ceid": "US:en"},
    "tavily": {"enabled": True, "weekly_cap": 40, "monthly_cap": 900},
    "social": {"actor": "harvestapi~linkedin-profile-posts", "max_posts": 10,
               "include_reposts": False},
    "backfill": {"days": 90, "max_news_items": 30, "max_posts": 20, "sitemap_items": 12,
                 "wayback": True},
}

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def load_config(path: Path | None = None, required: bool = True) -> dict:
    p = Path(path) if path else ws.config
    cfg = read_json(p)
    if cfg is None:
        if not required:
            return {}
        raise SystemExit(f"config missing or not valid JSON: {p}\n"
                         f"Start from templates/config.example.json (AGENTS.md § Setup).")
    cfg.setdefault("project", {})
    cfg["settings"] = _merge(SETTINGS_DEFAULTS, cfg.get("settings") or {})
    cfg.setdefault("competitors", [])
    for c in cfg["competitors"]:
        c.setdefault("name", c.get("slug"))
        c.setdefault("aliases", [])
        c.setdefault("pages", [])
        c.setdefault("social", [])
        c.setdefault("enabled", True)
    return cfg


def config_problems(cfg: dict) -> list[str]:
    """Everything wrong with a config, as fix-it sentences. Empty list = valid."""
    out = []
    comps = cfg.get("competitors") or []
    if not comps:
        out.append("config lists no competitors — run the setup interview (AGENTS.md § Setup).")
    seen = set()
    for i, c in enumerate(comps):
        slug = c.get("slug") or ""
        where = f"competitors[{i}] ({slug or 'no slug'})"
        if not SLUG_RE.match(slug):
            out.append(f"{where}: slug must be lowercase letters, digits and dashes.")
        if slug in seen:
            out.append(f"{where}: duplicate slug.")
        seen.add(slug)
        if not (c.get("name") or "").strip():
            out.append(f"{where}: name is empty.")
        if not c.get("domain"):
            out.append(f"{where}: domain is empty (e.g. \"lovable.dev\").")
        elif "/" in c["domain"] or c["domain"].startswith("http"):
            out.append(f"{where}: domain must be a bare host like \"lovable.dev\", not a URL.")
        for u in c.get("pages") or []:
            if not str(u).startswith(("http://", "https://")):
                out.append(f"{where}: page {u!r} must be a full URL.")
        if not isinstance(c.get("social") or [], list):
            out.append(f"{where}: social must be a list of {{\"url\", \"label\"}} objects.")
        for s in c.get("social") or []:
            u = (s or {}).get("url") if isinstance(s, dict) else None
            if not u or "linkedin.com/" not in u:
                out.append(f"{where}: social entry {s!r} needs a LinkedIn \"url\".")
        if c.get("name_in_news_query") is False and not c.get("aliases"):
            out.append(f"{where}: name_in_news_query is false but there are no aliases to search.")
    return out


def social_targets(comp: dict) -> list[dict]:
    """[{url, label}] — the company page and up to a few people."""
    out = []
    for s in comp.get("social") or []:
        if isinstance(s, dict) and s.get("url"):
            out.append({"url": s["url"].strip(), "label": (s.get("label") or "").strip()
                        or ("company" if "/company/" in s["url"] else "person")})
    return out


# ── state (health, budgets, last scans) ──────────────────────────────


def load_state() -> dict:
    st = read_json(ws.state) or {}
    st.setdefault("tavily", {})
    st.setdefault("targets", {})
    st.setdefault("competitors", {})
    st.setdefault("runs", {})
    return st


def save_state(st: dict) -> None:
    write_json_atomic(ws.state, st)


def tavily_budget_left(st: dict, d: str, cfg: dict) -> int:
    """Credits left under both the weekly and the monthly cap. The monthly cap keeps a
    roster inside Tavily's 1,000-credit free tier; the weekly cap stops one noisy week
    from spending the month."""
    t = cfg["settings"]["tavily"]
    led = st["tavily"]
    if led.get("week_start") != week_start(d):
        led["week_start"], led["week_used"] = week_start(d), 0
    if led.get("month") != d[:7]:
        led["month"], led["month_used"] = d[:7], 0
    wk = int(os.environ.get("TAVILY_WEEKLY_CAP") or t.get("weekly_cap") or 0)
    mo = int(t.get("monthly_cap") or 0)
    return max(0, min(wk - led.get("week_used", 0), mo - led.get("month_used", 0)))


def tavily_spend(st: dict, n: int = 1) -> None:
    led = st["tavily"]
    led["week_used"] = led.get("week_used", 0) + n
    led["month_used"] = led.get("month_used", 0) + n


def mark_target(st: dict, key: str, health: str, ok: bool, note: str = "", **extra) -> None:
    t = st["targets"].setdefault(key, {"health": None, "last_success": None,
                                       "consecutive_low_signal": 0, "noisy": False})
    t["health"], t["note"], t["last_checked"] = health, note, now_iso()
    if ok:
        t["last_success"] = now_iso()
    t.update(extra)


def expected_target_keys(cfg: dict) -> set:
    keys = set()
    for c in cfg.get("competitors") or []:
        slug = c.get("slug")
        if not slug:
            continue
        keys.add(f"{slug}:news")
        if c.get("sitemap", "auto") is not None:
            keys.add(f"{slug}:sitemap")
        for u in c.get("pages") or []:
            keys.add(f"{slug}:pages:{normalize_url(u)}")
        for s in social_targets(c):
            keys.add(f"{slug}:social:{normalize_url(s['url'])}")
        keys.add(f"{slug}:social")
    return keys


def prune_orphans(st: dict, cfg: dict) -> list[str]:
    """Drop health rows for targets the config no longer has, so a stale `error` from a
    retired page never reads as a live failure. De-dup ledgers are never pruned."""
    expected = expected_target_keys(cfg)
    orphans = sorted(k for k in st["targets"] if k not in expected)
    for k in orphans:
        st["targets"].pop(k, None)
    return orphans


def is_due(comp: dict, st: dict, d: str, cfg: dict) -> bool:
    """Due when at least `every_days` have passed since the competitor's last scan
    (one day of slack, so a weekly scheduler that fires a few hours early still counts)."""
    every = int(comp.get("every_days") or cfg["settings"]["cadence_days"])
    last = (st["competitors"].get(comp["slug"]) or {}).get("last_scanned")
    return not last or days_between(last, d) >= max(0, every - 1)


def select_competitors(cfg: dict, st: dict, slug: str | None, d: str,
                       ignore_due: bool = False) -> list[dict]:
    if slug and slug != "all":
        picked = [c for c in cfg["competitors"] if c.get("slug") == slug]
        if not picked:
            raise SystemExit(f"unknown competitor slug: {slug!r}")
        if not picked[0].get("enabled", True):
            print(f"note: {slug} is disabled in config — running it anyway because you named it.",
                  file=sys.stderr)
        return picked
    comps = [c for c in cfg["competitors"] if c.get("enabled", True)]
    return comps if ignore_due else [c for c in comps if is_due(c, st, d, cfg)]


def lookback_days(comp: dict, st: dict, d: str, cfg: dict) -> int:
    """How far back a regular cycle searches news and posts: to the last scan (+1 day of
    overlap; de-dup absorbs it), or `news_window_days` on a competitor's first cycle."""
    last = (st["competitors"].get(comp["slug"]) or {}).get("last_scanned")
    if not last:
        return int(cfg["settings"]["news_window_days"])
    return min(31, max(2, days_between(last, d) + 1))


# ── de-dup ledger ────────────────────────────────────────────────────


def seen_path(slug: str) -> Path:
    return ws.seen / f"{slug}.jsonl"


def load_seen(slug: str, before: str | None = None) -> set:
    """Keys of every item already judged. `before=<cycle>` ignores rows the cycle itself
    wrote, so plan → apply → plan on the same day rebuilds the same pack."""
    p = seen_path(slug)
    if not p.exists():
        return set()
    out = set()
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if before and (row.get("date") or "") >= before:
            continue
        if row.get("key"):
            out.add(row["key"])
    return out


def append_seen(slug: str, rows: list[dict]) -> int:
    if not rows:
        return 0
    have = load_seen(slug)
    new = [r for r in rows if r["key"] not in have]
    if new:
        p = seen_path(slug)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            for r in new:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(new)


# ── timeline ─────────────────────────────────────────────────────────


def timeline_path(slug: str) -> Path:
    return ws.timeline / f"{slug}.jsonl"


def load_timeline(slug: str) -> list[dict]:
    p = timeline_path(slug)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out
