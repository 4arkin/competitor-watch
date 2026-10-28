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

from cw import core, net  # noqa: E402
from cw.cli import extract_json, main  # noqa: E402
from cw.plan import _reliable_lastmod, news_offtopic, recent_timeline, story_key  # noqa: E402
from cw.sources import names_product, news_windows, normalize_social  # noqa: E402

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

    def test_pack_shows_the_timeline_so_far(self):
        self.cycle("2026-10-14")
        self.cycle("2026-10-21", "--fixture-variant", "2", "--force")
        pack = json.loads((self.data / "cache/scans/2026-10-21/packs/acme.json").read_text())
        self.assertEqual(pack["window"], {"from": "2026-10-13", "to": "2026-10-21"})   # +1 day overlap
        recent = pack["recent_timeline"]
        self.assertTrue(recent, "last cycle's events reach triage")
        core.use(self.data)                       # apply has now logged this cycle's items too
        self.assertEqual(recent_timeline("acme", "2026-10-21"), recent, "never this cycle's own items")

    def test_rejudged_cycle_replaces_its_timeline_rows(self):
        self.cycle("2026-10-14")
        before = core.load_timeline("acme")
        self.assertGreater(len(before), 1)
        dec = self.data / "cache/scans/2026-10-14/decisions/acme.json"
        rows = json.loads(dec.read_text())["verdicts"]
        for k, r in enumerate(rows):
            r.update(verdict="minor", so_what="Re-judged.", finding="") if k == 0 else r.update(verdict="noise")
        dec.write_text(json.dumps({"verdicts": rows}))
        rc, out = cw(self.data, "--date", "2026-10-14", "apply")
        self.assertEqual(rc, 0, out)
        after = core.load_timeline("acme")
        self.assertEqual([(e["id"], e["so_what"]) for e in after], [(rows[0]["id"], "Re-judged.")])

    def test_replanning_an_old_cycle_keeps_its_website_items(self):
        self.cycle("2026-10-14")
        first = self.cycle("2026-10-21", "--fixture-variant", "2", "--force")
        web = sorted(i["id"] for i in first if i["source"] in ("pages", "sitemap"))
        self.assertTrue(web)
        self.cycle("2026-10-28", "--fixture-variant", "3", "--force")      # rolls every snapshot on
        for step in ("plan", "apply"):                                     # correct 10-21 afterwards
            rc, out = cw(self.data, "--date", "2026-10-21", step)
            self.assertEqual(rc, 0, out)
        again = sorted(i["id"] for i in pack_items(self.data, "2026-10-21") if i["source"] in ("pages", "sitemap"))
        self.assertEqual(again, web, "the cycle's own website items, not the later fetch's")
        logged = {e["id"] for e in core.load_timeline("acme") if e["cycle"] == "2026-10-21"}
        self.assertTrue(set(web) <= logged, "re-applying never deletes them")
        st = json.loads((self.data / "cache/state.json").read_text())
        self.assertEqual(st["competitors"]["acme"]["last_scanned"], "2026-10-28", "an old re-run is not the last scan")

    def test_cache_hit_keeps_the_cached_health(self):
        cfg = json.loads(json.dumps(CONFIG))
        cfg["competitors"][0]["social"] = [{"url": "https://www.linkedin.com/in/nobody/", "label": "Nobody"}]
        (self.data / "config.json").write_text(json.dumps(cfg))
        for _ in range(2):
            cw(self.data, "--date", "2026-10-14", "fetch", "--fixture")
            st = json.loads((self.data / "cache/state.json").read_text())
            self.assertEqual(st["targets"]["acme:social"]["health"], "degraded-social")

    def test_new_sitemap_pages_are_read(self):
        self.cycle("2026-10-14")
        items = self.cycle("2026-10-21", "--fixture-variant", "2", "--force")
        teams = next(i for i in items if i["source"] == "sitemap" and i["url"].endswith("/teams"))
        self.assertIn("Acme AI Teams", teams["fact"], "the page title is in the fact")
        self.assertIn("$40 per seat", teams["detail"], "the page text reaches triage")

    def test_noise_streak_counts_a_cycle_once(self):
        def streak():
            st = json.loads((self.data / "cache/state.json").read_text())
            return st["targets"]["acme:pages:https://acme.example/"]["consecutive_low_signal"]
        self.cycle("2026-10-14")
        self.cycle("2026-10-21", "--fixture-variant", "3", "--force")
        self.assertEqual(streak(), 1)
        for step in ("plan", "apply"):                        # a corrective re-run of the cycle
            rc, out = cw(self.data, "--date", "2026-10-21", step)
            self.assertEqual(rc, 0, out)
        self.assertEqual(streak(), 1, "a re-run is the same cycle")
        self.cycle("2026-10-28", "--fixture-variant", "3", "--force")
        self.assertEqual(streak(), 0, "an unchanged week ends the streak")

    def test_news_over_the_cap_is_listed(self):
        cfg = json.loads(json.dumps(CONFIG))
        cfg["settings"] = {"max_news_items": 1}
        (self.data / "config.json").write_text(json.dumps(cfg))
        self.cycle("2026-10-14")
        scan = json.loads((self.data / "cache/scans/2026-10-14/acme.json").read_text())
        self.assertEqual(len(scan["overflow"]), 1)
        self.assertIn("1 over cap", scan["source_status"]["news"])
        self.assertIn("over the cap", (self.data / "reports/2026-10-14.md").read_text())

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

    def test_domain_shared(self):
        comp = {"slug": "widget", "name": "Widget Pro", "aliases": [], "domain": "parent.example",
                "domain_shared": True}
        self.assertEqual(self.offtopic("Parent posts quarterly results", url="https://parent.example/news/q3",
                                       comp=comp), "no-mention", "the parent's own news is not the product's")
        self.assertIsNone(self.offtopic("Widget Pro adds offline mode", url="https://parent.example/news/x", comp=comp))
        self.assertTrue(names_product("https://parent.example/products/widget-pro/pricing", comp))
        self.assertFalse(names_product("https://parent.example/careers", comp))

    def test_linkedin_post_dated_from_its_id(self):
        got = normalize_social([{"linkedinUrl": "https://www.linkedin.com/posts/acme_x-activity-7385000000000000000-AbCd",
                                 "content": "Undated post"}], {"url": "u", "label": "Acme"})
        self.assertEqual(got[0]["posted_at"], "2025-10-17")

    def test_story_key(self):
        a = story_key("Acme AI raises $30M Series A to take its app builder - TechWire", "TechWire")
        self.assertEqual(a, story_key("Acme AI raises $30M Series A to take its app builder | Finance Wire"))
        self.assertEqual(a, story_key("Acme AI raises $30M Series A to take its app builder"))
        self.assertIsNone(story_key("Acme AI raises"), "too short to identify a story")

    def test_extract_json(self):
        self.assertEqual(extract_json('Sure!\n```json\n{"verdicts": []}\n```'), {"verdicts": []})
        self.assertEqual(extract_json('[{"id": 1}] done'), [{"id": 1}])
        self.assertIsNone(extract_json("no json here"))


INDEX = ('<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{}</sitemapindex>')
URLSET = '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{}</urlset>'


def index_of(*names):
    return INDEX.format("".join(f"<sitemap><loc>https://beta.example/sitemaps/{n}.xml</loc></sitemap>" for n in names))


def urlset_of(*paths):
    return URLSET.format("".join(f"<url><loc>https://beta.example/{p}</loc></url>" for p in paths))


class SitemapIntegrity(unittest.TestCase):
    """Sitemap indexes on throwaway fixtures: a sitemap that was only partly read must
    never report pages as removed or new."""

    def setUp(self):
        self.data = Path(tempfile.mkdtemp(prefix="cw-test-"))
        self.fx = Path(tempfile.mkdtemp(prefix="cw-fx-"))
        self._fixtures, net.FIXTURES = net.FIXTURES, self.fx
        (self.data / "config.json").write_text(json.dumps({
            "settings": {"tavily": {"enabled": False}},
            "competitors": [{"slug": "beta", "name": "Beta Labs", "domain": "beta.example"}]}))
        self.write("news_rss.xml", '<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>')

    def tearDown(self):
        net.FIXTURES = self._fixtures
        shutil.rmtree(self.data, ignore_errors=True)
        shutil.rmtree(self.fx, ignore_errors=True)
        core.use(core.REPO / "data")

    def write(self, rel, text):
        p = self.fx / "beta" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def cycle(self, d, variant=1):
        rc, out = cw(self.data, "--date", d, "fetch", "--fixture", "--fixture-variant", str(variant), "--force")
        self.assertEqual(rc, 0, out)
        rc, out = cw(self.data, "--date", d, "plan")
        self.assertEqual(rc, 0, out)
        p = self.data / "cache" / "scans" / d / "packs" / "beta.json"
        items = [i for i in json.loads(p.read_text())["items"] if i["source"] == "sitemap"] if p.exists() else []
        if p.exists():
            rc, out = cw(self.data, "--date", d, "triage", "--cmd", FAKE_AGENT)
            self.assertEqual(rc, 0, out)
        rc, out = cw(self.data, "--date", d, "apply")
        self.assertEqual(rc, 0, out)
        return items

    def target(self):
        return json.loads((self.data / "cache/state.json").read_text())["targets"]["beta:sitemap"]

    def baseline(self):
        self.write("sitemap.xml", index_of("a", "b"))
        self.write("sitemaps/a.xml", urlset_of("a1", "a2"))
        self.write("sitemaps/b.xml", urlset_of("b1", "b2"))
        self.assertEqual(self.cycle("2026-10-14"), [])

    def test_failed_child_keeps_the_last_full_snapshot(self):
        self.baseline()
        self.write("sitemaps/b.v2.xml", "<html>maintenance</html>")
        self.assertEqual(self.cycle("2026-10-21", 2), [], "b's pages are not 'removed'")
        self.assertEqual(self.target()["health"], "partial")
        self.assertEqual(self.cycle("2026-10-28"), [], "nor 'new' when b answers again")

    def test_a_child_that_never_answers_is_not_partial(self):
        self.write("sitemap.xml", index_of("a", "dead"))
        self.write("sitemaps/a.xml", urlset_of("a1"))
        self.assertEqual(self.cycle("2026-10-14"), [])
        self.write("sitemaps/a.v2.xml", urlset_of("a1", "a2"))
        self.assertEqual([i["url"] for i in self.cycle("2026-10-21", 2)], ["https://beta.example/a2"])
        self.assertEqual(self.target()["health"], "ok")

    def test_domain_shared_counts_only_product_pages(self):
        (self.data / "config.json").write_text(json.dumps({
            "settings": {"tavily": {"enabled": False}},
            "competitors": [{"slug": "beta", "name": "Widget", "domain": "beta.example", "domain_shared": True}]}))
        self.baseline()
        self.write("sitemaps/a.v2.xml", urlset_of("a1", "a2", "widget-pricing", "careers"))
        items = self.cycle("2026-10-21", 2)
        self.assertEqual([i["url"] for i in items], ["https://beta.example/widget-pricing"])
        self.assertIn("not read", items[0]["detail"], "an unreadable page says so")

    def test_new_sitemap_files_rebaseline(self):
        self.baseline()
        self.write("sitemap.v2.xml", index_of("a", "b", "c"))
        self.write("sitemaps/c.xml", urlset_of("c1"))
        self.assertEqual(self.cycle("2026-10-21", 2), [])
        self.assertIn("re-baselined", self.target()["note"])

    def test_new_urls_past_the_cap_roll_up(self):
        self.baseline()
        self.write("sitemaps/a.v2.xml", urlset_of("a1", "a2", *[f"n{k:02}" for k in range(1, 13)]))
        items = self.cycle("2026-10-21", 2)
        self.assertEqual(len(items), 9)
        self.assertIn("4 more new pages", items[-1]["fact"])
        ledger = (self.data / "cache/seen/beta.jsonl").read_text()
        self.assertIn("https://beta.example/n12", ledger, "every URL is ledgered, rolled up or not")

    def test_truncated_index_itemizes_no_removals(self):
        names = ["a", "b", "c", "d", "e", "f"]
        self.write("sitemap.xml", index_of(*names))
        for n in names:
            self.write(f"sitemaps/{n}.xml", urlset_of(f"{n}1"))
        self.assertEqual(self.cycle("2026-10-14"), [])
        self.write("sitemaps/a.v2.xml", urlset_of())
        self.assertEqual(self.cycle("2026-10-21", 2), [], "a page may only have moved to an unread file")


if __name__ == "__main__":
    unittest.main()
