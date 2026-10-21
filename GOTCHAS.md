# Gotchas

Traps that this watch can hit, and what the tool does about each. Read this before changing a filter or a threshold. Most of these look like small bugs and end up as wrong data in a report.

## News

- **Ordinary-word names flood the news.** A competitor named after a common word, a dictionary word or another famous company brings back results about everything else. Fix in config: `news_context_required` (must also mention a market term), `exclude_terms`, or `name_in_news_query: false` plus `news_name_with` companions. The setup interview runs this test for every competitor.
- **Search providers match loosely.** Google News stems words and Tavily ignores quotes, so a three-word product name returns any story that contains two of the words. Every news item must therefore name a search term itself, token-aligned and never across a sentence break ("A lightning bolt. New records" is not "Bolt New").
- **The outlet's name is not context.** A newspaper whose name contains a market term would pass the context check for any headline. The outlet name is stripped before the check.
- **An exclusion must be enforced, not written down.** A note in the config that says a name is "excluded" does nothing. Every exclusion here is a config flag the code honours.
- **Google News returns about 100 items per query, with no paging.** One 90-day query drops most of a busy company's coverage, so the backfill queries week by week.
- **One story, ten outlets.** Syndicated coverage lands as separate items. Triage labels the repeats minor; the original stays the significant one.

## Pages

- **Two extractors, two texts.** A plain fetch and a Tavily extract turn the same page into differently shaped text. A site that blocks requests now and then flips between them, and every flip looks like a whole-page change. When the extractor switches, the tool re-baselines instead of diffing.
- **Theme classes on `<body>`.** The chrome filter drops elements whose class mentions nav, header, footer and so on. Some themes put a class like `mfn-header-template` on `<body>`, which would empty the whole page. `<html>` and `<body>` are never filtered.
- **JavaScript-only pages normalize to nothing.** A hash of empty text looks like a healthy page that never changes. Pages under 80 characters of text are marked `thin` and never diffed. `bin/cw probe` finds them before the backfill; swap them for a page that renders on the server (changelog, docs, blog).
- **Archived pages arrive gzipped.** The Wayback Machine replays captures with their original compression, whatever the request asks for. Undecoded, the bytes diff as a total rewrite. The fetcher decompresses gzip and deflate, and refuses other encodings with an error.
- **A second fetch must not swallow a change.** Pages and sitemaps diff against the snapshot committed by the last `apply`, not against the last fetch. Re-running fetch before apply loses nothing.
- **Small edits that never stop.** A page that changes by a line every week (counters, rotating testimonials) is marked `noisy` after three in a row, and its items reach triage tagged `suspect_noise`.

## Sitemaps

- **Many sites stamp every URL with the build date.** If one date covers most URLs, `<lastmod>` says nothing, and the backfill ignores it. A date stamped on dozens of URLs at once counts as a rebuild, not as edits.
- **One page in twelve languages is one page.** `/fr/blog` next to `/blog` is skipped.
- **A failed child sitemap looks like a mass deletion.** Losing more than half the URLs at once counts as a partial fetch and is not reported. Child sitemaps are read in sorted order, so every scan reads the same subset.
- **Some "sitemaps" are HTML.** Single-page apps answer `/sitemap.xml` with their app shell. That is a parse error, never an empty sitemap, and the tool falls back to `robots.txt`.

## LinkedIn

- **Share links carry a per-fetch token.** LinkedIn adds `rcm=` (and `utm_*`) to post URLs, different on every pull. The tool strips them, or every re-pulled post would look new.
- **The obvious URL can be another company.** `linkedin.com/company/<name>/` often belongs to an unrelated company with the same name. The setup interview verifies every LinkedIn URL with a search before it goes into the config.
- **No key means no LinkedIn, said loudly.** Without `APIFY_TOKEN` the source reports `degraded-social`, in `status` and in the report, so a missing key never passes as a quiet week.

## Running it

- **Quiet is not broken.** A source that works and finds nothing writes an empty cache file with health `ok`. A source that fails writes nothing and is marked `error`. A run where everything fails exits non-zero instead of reporting a quiet week.
- **Noise is remembered, not re-judged.** Every judged item's key is kept, noise included, so the same story is not triaged again next week.
- **One busy competitor can bury the rest.** The notable list and the digest go round-robin across competitors, so a rival's single big move is not pushed out by someone else's five announcements.
- **Timeline before ledger.** An item is marked seen only after it is written to the timeline, so nothing is lost if a write fails halfway.
