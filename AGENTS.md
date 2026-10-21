# AGENTS.md — operating protocol

You are setting up and running a competitor watch for the person you work with. The tool collects what their competitors do — website changes, news, LinkedIn posts from the company and its leaders — into one dated timeline per competitor. You do the one judgment step (labelling items) and, later, read the timeline back to connect the dots.

Everything runs through `bin/cw` (Python 3.9+, standard library only, nothing to install). Run it from the repository folder.

## Rules

- **Setup isn't finished until the watch runs on its own.** A first scan without a schedule is a one-off audit. Steps 1–6 below all happen in the first session.
- **Keys live only in `data/.env`.** Ask the user to paste them into that file themselves, or to give them to you once so you write them there. Never echo a key back, put one in a command line, or write one anywhere else.
- **Never edit `data/timeline/`, `data/competitors/` or `data/cache/` by hand.** `bin/cw apply` writes them. To change what is watched, edit `data/config.json`.
- **`data/` is the user's private data.** It is gitignored. Never commit it, and never move it out of the clone without asking.
- **Say what costs money before you spend it.** `bin/cw doctor` prints the estimate. The free tiers cover a typical roster, but the user decides.
- **Report what happened, including failures.** If a source is degraded, say which and why. Don't present a blind scan as a quiet week.

## 1. Install

If the user gave you a link to this repository, clone it next to their project, or inside it with the folder added to their `.gitignore`. Then:

```bash
bin/cw init        # creates data/ with config.json and .env templates
bin/cw doctor --offline
```

## 2. Setup interview → `data/config.json`

Follow [`prompts/setup-interview.md`](prompts/setup-interview.md). In short:
1. Read the user's project, or ask what they sell and to whom.
2. Propose 3–8 competitors, each with domain, aliases, pages to watch, LinkedIn company page and up to two people, and the homonym risk you checked.
3. Show the roster as a short table and let the user confirm or edit it.
4. Write `data/config.json` (schema: `templates/config.example.json`; every field is explained in the interview file).
5. Run `bin/cw probe`. Every page must read `ok`. Swap `THIN` pages (rendered by JavaScript) for a changelog, docs or blog page, and `BLOCKED` ones for another page or a Tavily key. Do this before the backfill: a page added later has no history.

## 3. Keys

Ask for two free keys and have them saved in `data/.env`:

| Key | What it does | Without it | Free tier |
|---|---|---|---|
| `APIFY_TOKEN` | LinkedIn posts of the company and its people | No LinkedIn signal (health shows `degraded-social`) | $5/month — [sign up](https://console.apify.com/sign-up), then Settings → API & Integrations |
| `TAVILY_API_KEY` | Extra news search; reads pages that block plain requests | Google News only; blocked pages stay blocked | 1,000 credits/month — [app.tavily.com](https://app.tavily.com) |

Then run `bin/cw doctor`. It must end in `DOCTOR: PASS`. It also prints the monthly cost estimate for this roster; tell the user.

## 4. Backfill — a timeline on day one

```bash
bin/cw backfill            # last 90 days; takes a few minutes
```

It exits with code 3 and prints `NEXT:` when there are items to label. Do § Triage, then `bin/cw apply`. Show the user the result: open `data/reports/<date>-backfill.md` and summarise the significant moves per competitor in a few lines. This is the first moment the tool proves itself, so make it concrete.

## 5. Schedule and destination

Ask two questions: **how often** (weekly is the default; `settings.cadence_days`) and **where the digest should land** (Slack, Discord, email, Notion, a file, or just the reports folder). Then wire it with what the user already has. [`schedule/README.md`](schedule/README.md) has recipes for cron, macOS launchd, the Claude desktop app's scheduled tasks, and a webhook for Slack/Discord.

Scheduled runs need a headless agent for triage: set `CW_TRIAGE_CMD` in `data/.env` to the user's CLI (`claude -p`, `codex exec`, or `gemini -p`). If the scheduler is itself an agent session (a desktop scheduled task), it triages in-session instead.

**Before you finish, prove the schedule works:** trigger it once by hand (`schedule/run-cycle.sh`) and show the user the log line. On the backfill day it will say `nothing due`; that is the expected pass.

## 6. Hand-off

Tell the user, in at most ten lines: what is watched, when the next scan runs, where the digest lands, roughly what it costs, and that they can ask you "what has <competitor> been doing?" at any time.

## Each cycle

```bash
bin/cw run                 # fetch → plan for every competitor that is due
```

- `nothing due` → done.
- Exit 3 with `NEXT:` → do § Triage, then `bin/cw apply`, then `bin/cw deliver` (if a webhook is set).
- Exit 0 after apply → done. The digest is in `data/digests/<date>.md` (empty when nothing significant happened).
- Exit 1 or 2 → read the message, run `bin/cw status`, fix or report.

With `CW_TRIAGE_CMD` set, `bin/cw run --deliver` does all of it unattended.

## Triage

Each competitor with new items has a pack at `data/cache/scans/<date>/packs/<slug>.json`. For each pack:
1. Read the pack. Its `instructions` field is [`prompts/triage.md`](prompts/triage.md); follow it exactly.
2. Write `data/cache/scans/<date>/decisions/<slug>.json` as `{"verdicts": [{"id", "verdict", "so_what", "finding"}]}` — one entry per item, `finding` on every significant one.

Then `bin/cw apply`. It refuses incomplete or unknown verdicts and tells you which; fix and rerun. `bin/cw triage --cmd "<your cli>"` does the same through a headless agent.

## Reading the timeline

When the user asks what a competitor is doing, what changed lately, or how two competitors compare, follow [`prompts/timeline-read.md`](prompts/timeline-read.md). The data:

```bash
bin/cw timeline lovable --days 90            # one competitor, readable
bin/cw timeline --days 30 --verdict significant
bin/cw timeline all --since 2026-07-01 --json # everything, for analysis
```

Each event has `event_date` (when it happened), `source`, `verdict`, `fact` (what the tool saw), `finding` (one-line fact, significant only), `so_what` (the triage note, interpretation), and `url`.

## Changing the roster

Edit `data/config.json`, then `bin/cw doctor`. A new competitor has no history: run `bin/cw backfill --competitor <slug>`, triage, apply. To stop watching one without losing its timeline, set `"enabled": false`. `bin/cw pause` / `bin/cw resume` stops and restarts every scheduled run.

## Commands

| Command | Does |
|---|---|
| `init` | Create `data/` with templates |
| `doctor [--offline]` | Check Python, config, keys; print the cost estimate |
| `probe` | Check every configured page is readable, before the backfill |
| `backfill [--days N] [--competitor S]` | Build the first timeline from the last N days |
| `run [--deliver] [--triage-cmd C]` | One cycle for every due competitor |
| `fetch` / `plan` / `triage` / `apply` | The cycle's steps, one at a time |
| `deliver [--to URL] [--format slack\|discord\|json]` | Post the digest to a webhook |
| `timeline [slug] [--days N] [--verdict V] [--json]` | Print events |
| `status` | Last and next scan per competitor, health of every target |
| `pause` / `resume` | Stop / restart scheduled runs |

Global flags: `--data <folder>` (another data folder, e.g. one per client), `--date YYYY-MM-DD`.

Exit codes: 0 done · 1 something failed (message says what) · 2 bad config or nothing to work on · 3 waiting for triage.

## When something looks wrong

Read [`GOTCHAS.md`](GOTCHAS.md) first: it lists the traps this tool has already hit, with the fix for each.
