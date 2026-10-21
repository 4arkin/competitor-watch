# Scheduling and delivery

The watch only works if it runs without anyone remembering to run it. Pick one scheduler and one destination below. The scheduler can fire daily: `bin/cw run` scans only the competitors that are due (`settings.cadence_days`, default 7) and says `nothing due` otherwise.

## The triage step in an unattended run

Every run that finds something needs one AI judgment pass. Two ways:

- **A headless agent CLI** does it. Put the command in `data/.env`:
  ```
  CW_TRIAGE_CMD=claude -p
  ```
  `codex exec` and `gemini -p` work the same way: prompt as the last argument, answer on stdout. The tool sends each pack, checks the answer covers every item, and retries once if it doesn't.
- **The scheduler is an agent session** (the Claude desktop app's scheduled tasks, or any agent that runs a prompt on a timer). It triages in-session, following AGENTS.md § Each cycle. No `CW_TRIAGE_CMD` needed.

## Schedulers

### macOS: launchd (recommended on a Mac)

cron on macOS often can't reach the keychain where agent CLIs keep their login; launchd runs as you. Save as `~/Library/LaunchAgents/dev.competitor-watch.plist`, with the two paths replaced:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>dev.competitor-watch</string>
  <key>ProgramArguments</key><array><string>/ABSOLUTE/PATH/competitor-watch/schedule/run-cycle.sh</string></array>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>7</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>/ABSOLUTE/PATH/competitor-watch/data/run.log</string>
  <key>StandardErrorPath</key><string>/ABSOLUTE/PATH/competitor-watch/data/run.log</string>
</dict></plist>
```

```bash
launchctl load ~/Library/LaunchAgents/dev.competitor-watch.plist
launchctl start dev.competitor-watch      # run once now, then check data/run.log
```

A Mac that is asleep at 07:00 runs the job when it wakes.

### Linux (or any always-on machine): cron

```
0 7 * * *  /ABSOLUTE/PATH/competitor-watch/schedule/run-cycle.sh >> /ABSOLUTE/PATH/competitor-watch/data/run.log 2>&1
```

### Claude desktop app: scheduled task

Create a local scheduled task on the competitor-watch folder (daily or weekly) with this prompt:

> Run `bin/cw run`. If it prints NEXT, triage the packs as AGENTS.md § Triage describes, then run `bin/cw apply` and `bin/cw deliver`. Report the significant findings in three lines, or "quiet week" if there were none.

### A calendar reminder

No scheduler at hand? A weekly calendar reminder to tell your agent "run the competitor watch" does the same job.

## Destinations

The digest is always written to `data/digests/<date>.md` (and `.slack.txt`). It is empty when nothing significant happened, and then nothing is sent.

| Destination | How |
|---|---|
| **Slack** | Create an incoming webhook (Slack → Apps → Incoming Webhooks), put it in `data/.env` as `CW_WEBHOOK_URL=...`. `run --deliver` posts it. |
| **Discord** | Channel settings → Integrations → Webhooks; same `CW_WEBHOOK_URL`. Long digests are split into several messages. |
| **Any JSON endpoint** (Zapier, Make, n8n, your own) | `CW_WEBHOOK_URL` plus `CW_WEBHOOK_FORMAT=json`. You get `{"date", "markdown", "slack"}`. |
| **Email, Notion, a doc** | Have the agent session that runs the cycle send it with the tools it already has (a mail or Notion connector), or route the JSON webhook through Zapier, Make or n8n. |
| **Just the folder** | Nothing to do. Ask your agent "what did the competitor watch find?" whenever you like. |

Webhook URLs are secrets: keep them in `data/.env`, never in `config.json`.

## Check it

After wiring, trigger one run by hand (`schedule/run-cycle.sh`, or `launchctl start …`) and read `data/run.log`. On the day of the backfill it says `nothing due`, which is a pass. Stop everything with `bin/cw pause`.
