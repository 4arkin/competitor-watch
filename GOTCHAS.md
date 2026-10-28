# Gotchas

Traps that this watch can hit, and what the tool does about each. Read this before changing a filter or a threshold. Most of these look like small bugs and end up as wrong data in a report.

## News

- **Ordinary-word names flood the news.** A competitor named after a common word, a dictionary word or another famous company brings back results about everything else. Fix in config: `news_context_required` (must also mention a market term), `exclude_terms`, or `name_in_news_query: false` plus `news_name_with` companions. The setup interview runs this test for every competitor.
- **Search providers match loosely.** Google News stems words and Tavily ignores quotes, so a three-word product name returns any story that contains two of the words. Every news item must therefore name a search term itself, token-aligned and never across a sentence break ("A lightning bolt. New records" is not "Bolt New").
- **The outlet's name is not context.** A newspaper whose name contains a market term would pass the context check for any headline. The outlet name is stripped before the check.
- **An exclusion must be enforced, not written down.** A note in the config that says a name is "excluded" does nothing. Every exclusion here is a config flag the code honours.
- **Google News returns about 100 items per query, with no paging.** One 90-day query drops most of a busy company's coverage, so the backfill queries week by week.
- **A product on its parent company's site.** For a product hosted on a bigger company's domain, every press release and careers page on that domain would pass as the competitor's news. `domain_shared: true` makes news on the domain, and new or removed sitemap pages, count only when they name the product.
- **Over the cap is listed, not lost.** A busy week can bring more news than `max_news_items`. The newest items go to triage; the rest are listed under `overflow` in the scan file and counted in the report.
- **One story, ten outlets.** Syndicated coverage and the Google News and Tavily copies of one article arrive under different URLs, so URL de-dup lets every copy through. Each headline also gets a story key (the headline without its " - Outlet" tag, punctuation removed, five words or more). Once a story is judged, any copy of it is dropped, this week or later.
- **A new article about an old move.** Coverage often retells something that happened weeks ago, and it looks new to every de-dup rule. Each pack carries the competitor's recent timeline and the dates the scan covers. Triage labels a move already on the timeline noise, and an event from before the window minor at most.

## Pages

- **Two extractors, two texts.** A plain fetch and a Tavily extract turn the same page into differently shaped text. A site that blocks requests now and then flips between them, and every flip looks like a whole-page change. When the extractor switches, the tool re-baselines instead of diffing.
- **Theme classes on `<body>`.** The chrome filter drops elements whose class mentions nav, header, footer and so on. Some themes put a class like `mfn-header-template` on `<body>`, which would empty the whole page. `<html>` and `<body>` are never filtered.
- **JavaScript-only pages normalize to nothing.** A hash of empty text looks like a healthy page that never changes. Pages under 80 characters of text are marked `thin` and never diffed. `bin/cw probe` finds them before the backfill; swap them for a page that renders on the server (changelog, docs, blog).
- **Archived pages arrive gzipped.** The Wayback Machine replays captures with their original compression, whatever the request asks for. Undecoded, the bytes diff as a total rewrite. The fetcher decompresses gzip and deflate, and refuses other encodings with an error.
- **A second fetch must not swallow a change.** Pages and sitemaps diff against the snapshot committed by the last `apply`, not against the last fetch. Re-running fetch before apply loses nothing.
- **Small edits that never stop.** A page that changes by a line every week (counters, rotating testimonials) is marked `noisy` after three in a row, and its items reach triage tagged `suspect_noise`. An unchanged scan or a real change ends the streak, and re-running a cycle never counts it twice.

## Sitemaps

- **Many sites stamp every URL with the build date.** If one date covers most URLs, `<lastmod>` says nothing, and the backfill ignores it. A date stamped on dozens of URLs at once counts as a rebuild, not as edits.
- **One page in twelve languages is one page.** `/fr/blog` next to `/blog` is skipped.
- **A failed child sitemap looks like a mass deletion.** Its pages read as removed this week and as new the next. When a child sitemap that answered last time fails, the fetch keeps the last full snapshot and marks the source `partial`. A child that never answers is simply absent from both snapshots. Losing more than half the URLs at once is also treated as a partial fetch.
- **A different set of sitemap files is a different sitemap.** A new child file, a moved index or the `robots.txt` fallback changes which URLs are listed. When the set of files read changes, the tool re-baselines instead of diffing.
- **An index can list more files than the tool follows.** Child sitemaps are read in sorted order, up to five, so every scan reads the same subset. A page that leaves those five may only have moved to an unread file, so removals are not itemized for a sitemap read only in part.
- **A new page judged by its URL is a guess.** `/enterprise` or `/teams` says little on its own. The fetch reads the first eight new pages (one direct request each, no Tavily credits), so triage sees the title and text. A page that can't be read says why.
- **A launch can add dozens of pages at once.** The first eight new pages are itemized. The rest go to triage as one combined item, and every URL is recorded, so none is lost when the baseline moves on.
- **Some "sitemaps" are HTML.** Single-page apps answer `/sitemap.xml` with their app shell. A response whose root is not `<urlset>` or `<sitemapindex>`, well-formed XHTML included, is a parse error, never an empty sitemap, and the tool falls back to `robots.txt`.

## LinkedIn

- **Share links carry a per-fetch token.** LinkedIn adds `rcm=` (and `utm_*`) to post URLs, different on every pull. The tool strips them, or every re-pulled post would look new.
- **The obvious URL can be another company.** `linkedin.com/company/<name>/` often belongs to an unrelated company with the same name. The setup interview verifies every LinkedIn URL with a search before it goes into the config.
- **Some posts come back without a date.** The post's ID holds its creation time, so an undated post is dated from its ID and the recency checks still apply.
- **No key means no LinkedIn, said loudly.** Without `APIFY_TOKEN` the source reports `degraded-social`, in `status` and in the report, so a missing key never passes as a quiet week.

## Running it

- **Quiet is not broken.** A source that works and finds nothing writes an empty cache file with health `ok`. A source that fails writes nothing and is marked `error`. A run where everything fails exits non-zero instead of reporting a quiet week.
- **A cache re-read keeps its health.** A same-day re-run reuses the cache and reports the health that fetch had. A degraded fetch never turns into `ok` by being read twice.
- **A corrected triage rewrites its cycle.** Re-applying a cycle with changed verdicts replaces that cycle's timeline events, and removes those now labelled noise. Other cycles are untouched.
- **Noise is remembered, not re-judged.** Every judged item's key is kept, noise included, so the same story is not triaged again next week.
- **One busy competitor can bury the rest.** The notable list and the digest go round-robin across competitors, so a rival's single big move is not pushed out by someone else's five announcements.
- **Timeline before ledger.** An item is marked seen only after it is written to the timeline, so nothing is lost if a write fails halfway.
