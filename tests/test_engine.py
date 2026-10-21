"""Offline tests: the whole pipeline on synthetic fixtures (tests/fixtures/acme), no network.

    python3 -m unittest discover -s tests
"""

import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from cw import core  # noqa: E402
from cw.cli import extract_json, main  # noqa: E402
from cw.plan import _reliable_lastmod, news_offtopic  # noqa: E402
from cw.sources import news_windows, normalize_social  # noqa: E402

CONFIG = {
    "project": {"name": "Rival Builder", "description": "AI app builder for founders",
                "market_terms": ["app builder", "no-code"]},
    "competitors": [{
        "slug": "acme", "name": "Acme AI", "domain": "acme.example", "why": "Same buyer.",
        "aliases": ["Acme AI"], "name_in_news_query": False, "exclude_terms": ["anvil"],
        "pages": ["https://acme.example/", "https://acme.example/pricing"],
        "social": [{"url": "https://www.linkedin.com/company/acme/", "label": "company"},
                   {"url": "https://www.linkedin.com/in/jane-acme/", "label": "Jane Acme (CEO)"}]}],
}
FAKE_AGENT = f"{sys.executable} {REPO / 'tests' / 'fake_agent.py'}"


def cw(data, *args) -> tuple:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = main(["--data", str(data), *args])
    return rc, out.getvalue() + err.getvalue()


def pack_items(data, d):
    p = Path(data) / "cache" / "scans" / d / "packs" / "acme.json"
    return json.loads(p.read_text())["items"] if p.exists() else []


class Pipeline(unittest.TestCase):
    def setUp(self):
        self.data = Path(tempfile.mkdtemp(prefix="cw-test-"))
        (self.data / "config.json").write_text(json.dumps(CONFIG))

    def tearDown(self):
        shutil.rmtree(self.data, ignore_errors=True)
        core.use(core.REPO / "data")

    def cycle(self, d, *fetch_args):
        rc, out = cw(self.data, "--date", d, "fetch", "--fixture", *fetch_args)
        self.assertEqual(rc, 0, out)
        rc, out = cw(self.data, "--date", d, "plan")
        self.assertEqual(rc, 0, out)
        items = pack_items(self.data, d)
        if items:
            rc, out = cw(self.data, "--date", d, "triage", "--cmd", FAKE_AGENT)
            self.assertEqual(rc, 0, out)
        rc, out = cw(self.data, "--date", d, "apply")
        self.assertEqual(rc, 0, out)
        return items

    def test_regular_cycles_build_a_timeline(self):
        first = self.cycle("2026-10-14")
        sources = {i["source"] for i in first}
        self.assertNotIn("pages", sources, "first snapshot is a baseline, never a change")
        self.assertNotIn("sitemap", sources)
        self.assertIn("social", sources)
        facts = " ".join(i["fact"] for i in first)
        self.assertNotIn("anvil", facts.lower(), "exclude_terms drops the homonym")
        self.assertNotIn("productivity tips", facts.lower(), "no-mention items never reach triage")

        second = self.cycle("2026-10-21", "--fixture-variant", "2", "--force")
        by_source = {}
        for i in second:
            by_source.setdefault(i["source"], []).append(i["fact"])
        self.assertEqual(len(by_source.get("pages", [])), 1, by_source)        # pricing changed; home only by a date
        self.assertIn("$20", by_source["pages"][0])
        sitemap = " ".join(by_source.get("sitemap", []))
        self.assertIn("/teams", sitemap)
        self.assertIn("old-integrations", sitemap)
        self.assertNotIn("careers", sitemap, "tracking params never make a URL look new")
        self.assertNotIn("news", by_source, "news already judged last week is not judged again")

        timeline = core.load_timeline("acme")
        self.assertEqual(len({e["id"] for e in timeline}), len(timeline))
        n = len(timeline)
        rc, out = cw(self.data, "--date", "2026-10-21", "apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(core.load_timeline("acme")), n, "apply is idempotent")

        md = (self.data / "competitors" / "acme.md").read_text()
        self.assertIn("## Timeline", md)
        self.assertIn("changed its pricing page", md)
        digest = (self.data / "digests" / "2026-10-21.md").read_text()
        self.assertIn("Acme AI", digest)
        report = (self.data / "reports" / "2026-10-21.md").read_text()
        self.assertLessEqual(len(report.splitlines()), 200)

    def test_second_fetch_before_apply_keeps_the_change(self):
        self.cycle("2026-10-14")
        for _ in range(2):
            rc, out = cw(self.data, "--date", "2026-10-21", "fetch", "--fixture", "--fixture-variant", "2", "--force")
            self.assertEqual(rc, 0, out)
        rc, out = cw(self.data, "--date", "2026-10-21", "plan")
        self.assertEqual(rc, 0, out)
        self.assertTrue(any(i["source"] == "pages" for i in pack_items(self.data, "2026-10-21")))

    def test_backfill_gives_a_dated_timeline_on_day_one(self):
        rc, out = cw(self.data, "--date", "2026-10-21", "backfill", "--fixture", "--days", "90")
        self.assertEqual(rc, 3, out)                       # needs triage, says so
        self.assertIn("NEXT:", out)
        items = pack_items(self.data, "2026-10-21")
        pages = [i for i in items if i["source"] == "pages"]
        self.assertEqual(len(pages), 1)
        self.assertIn("Wayback", pages[0]["fact"])
        self.assertIn("2026-08-28", {i["event_date"] for i in items if i["source"] == "news"})
        rc, out = cw(self.data, "--date", "2026-10-21", "triage", "--cmd", FAKE_AGENT)
        self.assertEqual(rc, 0, out)
        rc, out = cw(self.data, "--date", "2026-10-21", "apply")
        self.assertEqual(rc, 0, out)
        dates = sorted(e["event_date"] for e in core.load_timeline("acme"))
        self.assertLess(dates[0], "2026-09-01", "events keep their own dates, not the scan date")
        self.assertTrue((self.data / "reports" / "2026-10-21-backfill.md").exists())
        rc, out = cw(self.data, "--date", "2026-10-21", "run", "--fixture")
        self.assertEqual(rc, 0, out)
        self.assertIn("nothing due", out)

    def test_apply_refuses_incomplete_decisions(self):
        cw(self.data, "--date", "2026-10-14", "fetch", "--fixture")
        cw(self.data, "--date", "2026-10-14", "plan")
        items = pack_items(self.data, "2026-10-14")
        bad = {"verdicts": [{"id": items[0]["id"], "verdict": "significant"}]}
        (self.data / "decisions.json").write_text(json.dumps(bad))
        rc, out = cw(self.data, "--date", "2026-10-14", "apply", "--decisions", str(self.data / "decisions.json"))
        self.assertEqual(rc, 1)
        self.assertIn("no verdict", out)
        self.assertIn("without a finding", out)
        self.assertEqual(core.load_timeline("acme"), [])

    def test_pause(self):
        cw(self.data, "pause")
        rc, out = cw(self.data, "--date", "2026-10-14", "run", "--fixture")
        self.assertEqual(rc, 0)
        self.assertIn("PAUSED", out)

    def test_doctor_flags_a_bad_config(self):
        cfg = json.loads(json.dumps(CONFIG))
        cfg["competitors"][0]["domain"] = "https://acme.example/"
        (self.data / "config.json").write_text(json.dumps(cfg))
        rc, out = cw(self.data, "doctor", "--offline")
        self.assertEqual(rc, 1)
        self.assertIn("bare host", out)


class Units(unittest.TestCase):
    comp = CONFIG["competitors"][0]

    def offtopic(self, title, url="https://news.example/x", snippet="", comp=None, market=None):
        cfg = {"project": {"market_terms": market or []}, "settings": core.SETTINGS_DEFAULTS}
        return news_offtopic({"title": title, "url": url, "snippet": snippet}, comp or self.comp, cfg)

    def test_offtopic_rules(self):
        self.assertIsNone(self.offtopic("Acme AI ships agents"))
        self.assertIsNone(self.offtopic("AcmeAI ships agents"), "spacing-insensitive")
        self.assertEqual(self.offtopic("Acme anvils on sale"), "no-mention")
        self.assertEqual(self.offtopic("Acme AI anvil giveaway"), "excluded")
        self.assertIsNone(self.offtopic("Totally unrelated", url="https://acme.example/blog/x"), "own domain stays")
        self.assertEqual(self.offtopic("One acme. AI is everywhere"), "no-mention", "never across a clause break")
        comp = dict(self.comp, news_context_required=True)
        self.assertEqual(self.offtopic("Acme AI opens an office", comp=comp, market=["app builder"]), "no-context")
        self.assertIsNone(self.offtopic("Acme AI, the app builder, opens an office", comp=comp, market=["app builder"]))
        comp = dict(self.comp, name="Atlas", aliases=["Atlas.dev"], news_name_with=["Atlas Labs founder Mia Park"])
        self.assertIsNone(self.offtopic("Atlas Labs founder Mia Park says Atlas will open an office", comp=comp))
        self.assertEqual(self.offtopic("Atlas of world maps", comp=comp), "no-mention")

    def test_news_windows_cover_the_range_in_weeks(self):
        w = news_windows("2026-10-21", 90)
        self.assertEqual(w[0], "after:2026-07-23 before:2026-07-31")
        self.assertTrue(w[-1].endswith("before:2026-10-22"))
        self.assertEqual(len(w), 13)

    def test_to_date(self):
        self.assertEqual(core.to_date("Tue, 20 Oct 2026 09:00:00 GMT"), "2026-10-20")
        self.assertEqual(core.to_date("2025-05-16T18:11:59.821Z"), "2025-05-16")
        self.assertEqual(core.to_date(1747419119821), "2025-05-16")
        self.assertEqual(core.to_date({"timestamp": 1747419119}), "2025-05-16")
        self.assertIsNone(core.to_date("6 days ago"))

    def test_normalize_url_strips_linkedin_share_token(self):
        a = core.normalize_url("https://www.linkedin.com/posts/x-activity-1?utm_source=share&rcm=ABC")
        self.assertEqual(a, "https://www.linkedin.com/posts/x-activity-1")

    def test_social_normalizer(self):
        raw = json.loads((REPO / "tests/fixtures/acme/apify/jane-acme.json").read_text())
        got = normalize_social(raw, {"url": "u", "label": "Jane"})
        self.assertEqual(len(got), 1, "reaction rows are not posts")
        self.assertEqual(got[0]["posted_at"], "2026-10-19")
        self.assertEqual(got[0]["reactions"], 950)

    def test_lastmod_reliability(self):
        self.assertFalse(_reliable_lastmod({f"u{i}": "2026-10-21" for i in range(10)}))
        self.assertTrue(_reliable_lastmod({"a": "2026-10-01", "b": "2026-09-01", "c": "2026-08-01"}))

    def test_extract_json(self):
        self.assertEqual(extract_json('Sure!\n```json\n{"verdicts": []}\n```'), {"verdicts": []})
        self.assertEqual(extract_json('[{"id": 1}] done'), [{"id": 1}])
        self.assertIsNone(extract_json("no json here"))


if __name__ == "__main__":
    unittest.main()
