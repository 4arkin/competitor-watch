# competitor-watch

**See every competitor move the week it happens. Read only the moves that matter.**

A snapshot shows where a competitor is. A timeline shows which way it moves.

competitor-watch records each competitor's moves with their dates. It watches four sources: the pages you choose, new pages in the sitemap, news, and LinkedIn posts from the company and its founders. After each scan, your AI agent sends you only the significant moves, one line each, with a link to the source. In a quiet week, you get no message.

Ask your agent *"what is Replit doing?"* and it reads the timeline. The example in this repo gives the answer for October. Replit announced an office in Japan, promoted a partner program for agencies, made OpenAI its default model and cut its discount in half. Each move alone is news. Together, the moves show a push into enterprise and new countries.

The example covers four AI app builders over 90 days. The tool removed 605 news results about other things, such as chapel events and football scores. The agent labeled the other 314 items and found 59 significant moves.

It is for marketers, and for founders who do their own marketing. You need an AI agent that works in a folder: Claude Code, Codex, Gemini CLI or similar. You do not need code, a marketing background or a paid plan. You send one message, and the agent installs the watch in about 15 minutes. A weekly watch on five competitors costs nothing: it uses about $1 of the free $5 Apify credit each month.

> **AI agents:** read [`AGENTS.md`](AGENTS.md). It is the complete protocol.

---

## Start with one message

Open your project in your AI agent and send:

```
Install https://github.com/4arkin/competitor-watch, read its AGENTS.md,
and set up a competitor watch for this project.
```

Your agent then:

```
reads your project ──→ proposes competitors, pages, people ──→ you confirm (~2 min)
        │
        ▼
asks for two free API keys ──→ checks everything works ──→ pulls the last 90 days
        │
        ▼
shows you each competitor's timeline ──→ asks how often and where to send updates
        │
        ▼
schedules it ──→ RUNNING: new moves land in Slack, Discord, email or a folder
```

Setup takes about 15 minutes, most of it waiting for the backfill.

## What it watches

| Signal | What you get |
|---|---|
| **Website pages** | The pages you choose (pricing, homepage, changelog) are diffed every scan. On setup, compared against a Wayback Machine capture from three months ago. |
| **Sitemap** | New and removed pages, often the first sign of a launch, a new segment or a quiet retreat. |
| **News** | Google News, plus Tavily news search. Homonyms and off-topic coverage are filtered out before your AI sees them. |
| **LinkedIn** | Posts from the company page and up to two people per competitor (usually the founder and a product or marketing lead). |

Your AI agent labels each new item significant, minor or noise. Only significant moves reach the digest; everything except noise goes into the timeline.

## What you get

- **A timeline per competitor** (`data/competitors/<name>.md`, plus JSON for your agent), dated by when things happened.
- **A digest** after each scan with only the significant moves, one line each with a link. Nothing is sent in a quiet week.
- **A report** per scan with the detail and the health of every source.
- **Answers on demand.** Ask your agent *"what has Bolt been doing since summer?"* and it reads the timeline.

## Works with

| Agent | Setup | Unattended runs |
|---|---|---|
| **Claude Code** (CLI or desktop) | Reads `CLAUDE.md` → `AGENTS.md` | Desktop app scheduled task, or launchd/cron with `claude -p` |
| **Codex CLI** | Reads `AGENTS.md` | cron with `codex exec` |
| **Gemini CLI** | Reads `GEMINI.md` → `AGENTS.md` | cron with `gemini -p` |

## Cost

| Service | Used for | Free plan | Typical use (5 competitors, weekly) |
|---|---|---|---|
| [Apify](https://apify.com) | LinkedIn posts | $5/month | about $1/month at most; about $0.60 once for the backfill |
| [Tavily](https://tavily.com) | News search, blocked pages | 1,000 credits/month | about 30 credits/month |
| Google News, Wayback Machine | News, page history | free, no key | — |

`bin/cw doctor` prints the estimate for your own roster before anything is spent. Prices checked October 2026.

## How it works

```
config.json ──→ fetch ──→ plan ──────────→ triage ──→ apply ──→ timeline + digest
(your roster)   (the only  (diff, de-dup,  (your AI    (writes everything,
                network    drop off-topic) agent: the  idempotent)
                step)                      one judgment)
```

Plain Python 3.9+, standard library only: nothing to install. Your data stays in `data/` on your machine (gitignored). The only outbound calls go to the sites you watch, Google News, the Wayback Machine, Apify and Tavily.

## Inside

| Path | What |
|---|---|
| [`AGENTS.md`](AGENTS.md) | The protocol your agent follows: setup, every cycle, reading the timeline |
| [`prompts/`](prompts/) | The setup interview, the triage rules, how to read a timeline |
| [`schedule/`](schedule/) | cron, launchd, desktop-app and webhook recipes |
| [`GOTCHAS.md`](GOTCHAS.md) | Known traps, and the fix for each |
| `bin/cw`, `cw/` | The engine |
| [`examples/`](examples/) | A real run on AI app builders |
| `tests/` | `python3 -m unittest discover -s tests`, offline |

## License

MIT. See [`LICENSE`](LICENSE).
