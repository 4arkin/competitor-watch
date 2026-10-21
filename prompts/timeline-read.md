# Reading the timeline — connecting the dots

Use this when the user asks what a competitor is doing, what changed lately, how competitors compare, or what to watch for. The timeline is the evidence; your answer is the pattern it shows.

## 1. Pull the evidence

```bash
bin/cw timeline <slug> --days 90 --json      # one competitor
bin/cw timeline all --days 30 --json         # the whole market, last month
bin/cw status                                # which sources were degraded, and since when
```

Check `status` before concluding anything from silence. "No LinkedIn posts in six weeks" means nothing if LinkedIn was `degraded-social` all along.

## 2. Group by move, not by source

Sort the events into the moves a marketer cares about:
- **Pricing and packaging:** pricing page diffs, plan launches, free-tier changes.
- **Product:** launches, changelog and feature pages, integrations.
- **Market and segment:** new industries, enterprise pages, regional launches, partner programmes.
- **Distribution:** partnerships, marketplaces, resellers, events.
- **Money and people:** funding, revenue claims, acquisitions, senior hires and exits, hiring posts.
- **Message:** homepage headline changes, the founder's repeated themes on LinkedIn.

## 3. Look for sequences across sources

The point of a watch is the signal no single source shows. A pricing page change, then a founder's LinkedIn post about "teams", then a news story about an enterprise deal, all within a few weeks, is one coordinated move upmarket. Three separate bullets miss it.

Things worth naming:
- the same theme showing up in two or more sources within about a month;
- a change in direction: a theme that stops, or a new one that starts;
- two competitors making the same move: a market shift, not one company's idea;
- the founder saying publicly what the website does weeks later, or the reverse.

## 4. Answer

Shape:
1. **The pattern, in one sentence.** *"Bolt is moving upmarket: team pricing, SSO, and an enterprise sales hire, all since August."*
2. **The evidence:** 3–6 dated lines, each a fact from the timeline with its link.
3. **What it means for the user:** clearly marked as your interpretation. Use the project description in `data/config.json`.
4. **What to watch next:** a page, a person, or a term worth adding to the config, if any.

Rules:
- **Facts come from `fact` and `finding`. `so_what` is an earlier interpretation**: use it as a hint, never as evidence.
- **One event is a signal, not a trend.** Say so.
- **Dates are event dates.** Page changes found on a scan carry the scan date and a "since" date; say "between X and Y" for those.
- **Don't fill gaps.** If the timeline doesn't cover something, say so, and offer a one-off web search, labelled as outside the watch.
