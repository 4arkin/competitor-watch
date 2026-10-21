# Setup interview — build the roster

Goal: a `data/config.json` the user has confirmed, where each competitor's news query finds the company and not its homonyms. Aim for ten minutes of the user's time. You do the research; they confirm.

## 1. Learn the project (no questions yet)

Read what you can reach: the user's repository (README, landing page copy, pricing, docs), or their website. Write down in one line what they sell, to whom, and what the buyer would use instead.

If there is no project to read, ask one question: *"What do you sell, to whom, and who do prospects compare you with?"*

## 2. Propose competitors

Propose 3–8. Order them by how often a buyer would compare them with the user:
- **Direct:** same buyer, same job. Prospects name them on sales calls.
- **Indirect or adjacent:** same buyer, different approach (an agency, a platform add-on, a bigger suite). Set `"every_days": 28` on these unless the user wants them weekly.

Ask the user who they lose deals to. That answer outranks your research.

## 3. Research each competitor

For each one, fill these fields. Verify with a web search; don't guess URLs.

| Field | How to fill it |
|---|---|
| `slug` | lowercase-with-dashes, e.g. `base44` |
| `name` | how the press writes it |
| `domain` | bare host: `lovable.dev`, not a URL |
| `why` | one line: why the user watches them. Triage uses it to judge significance |
| `aliases` | other names that unambiguously mean this company: product names, the domain, the legal name |
| `pages` | 2–4 URLs: the homepage, pricing, and one of changelog / product / enterprise. These get diffed every cycle |
| `social` | the LinkedIn company page (`/company/<x>/`), plus up to two people (founder/CEO, and head of product or marketing) as `/in/<x>/`, each with a `label` like `"Anton Osika (CEO)"` |
| `sitemap` | leave it out (found automatically). Set `null` only if the doctor or a fetch shows it's blocked |

### The homonym test (the step that decides whether news is useful)

Ask yourself: does the bare name mean something else? (`Lovable`, `Bolt`, `Linear`, `Notion`, `Mercury` are ordinary words or other famous names.)

- **Distinctive name** (`Base44`, `Supabase`): nothing to do.
- **Ordinary-word name:** keep it, and set `"news_context_required": true`. A news item must then also contain one of the project's `market_terms`. Add known homonyms to `exclude_terms` (`["Usain", "bolt action"]` for Bolt).
- **Name that floods even with context:** set `"name_in_news_query": false` so only the aliases are searched, and add `"news_name_with": ["<founder name>", "<parent company>"]`, so the bare name still counts when a companion term appears in the same item.

### Project fields

- `project.name`, `project.domains`, `project.description` (two sentences; triage reads it).
- `project.market_terms`: 5–10 words or phrases that coverage of this market uses (`"app builder"`, `"vibe coding"`, `"no-code"`, `"AI"`). Used only by competitors with `news_context_required`.

## 4. Confirm

Show one table: competitor · why · pages · LinkedIn accounts · homonym handling. Ask: *"Anyone missing, or anyone here you don't care about?"* Apply their edits, write `data/config.json`, run `bin/cw doctor`, then `bin/cw probe` and swap any page it marks `THIN` or `BLOCKED` before the backfill.

## 5. After the backfill, tune once

Open `data/cache/scans/<date>/<slug>.json` for each competitor:
- `offtopic` lists the news items dropped before triage, with a reason. If real coverage was dropped as `no-mention`, add the missing alias or companion. If `no-context` dropped real coverage, add the missing market term.
- If triage labelled most news items `noise`, the query is too loose: add `exclude_terms`, or turn on `news_context_required`.
- `bin/cw status` shows pages that are `blocked` (add a Tavily key, or remove the page) or `thin` (JavaScript-only; swap for a page that renders server-side, such as the changelog or a docs page).

Changes apply from the next cycle. Re-run `bin/cw backfill --competitor <slug>` only if the history was badly wrong.
