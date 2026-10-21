# Example: four AI app builders, the last 90 days

A real setup run on 21 October 2026: Lovable, Bolt, Base44 and Replit, each with its pricing page, sitemap, news, and LinkedIn posts from the company and its CEO or founder. One backfill pulled 90 days of history; triage was done by `claude -p`.

- 314 new items reached triage. Before that, the tool dropped 605 news results that never named the company (Google News matches loosely: chapel events, football scores, Wix stock notes) or used "lovable" as an ordinary word
- 286 kept in the timelines, 59 of them significant, 28 labelled noise

| File | What |
|---|---|
| [`digest-backfill.md`](digest-backfill.md) | The significant moves, one line each: what landed in Slack |
| [`report-backfill.md`](report-backfill.md) | The full report with the triage notes |
| [`competitors/`](competitors/) | One timeline per competitor, newest first |
| [`timeline/`](timeline/) | The same events as JSON lines, which is what your agent reads |
| [`config.json`](config.json) | The roster, including the homonym settings for Lovable and Bolt |

Ask an agent pointed at this folder *"what is Replit doing?"* and the timeline answers: an enterprise and international push (a Japan office, a partner programme for agencies, workspace sharing, Stripe Atlas incorporation inside the agent), a model-provider switch to OpenAI, and a smaller pricing discount, all in October.
