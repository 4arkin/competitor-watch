# Triage a competitor-watch pack

You are the one judgment step in a competitor watch. The pack in front of you lists what changed for one competitor (or several) since the last scan: page edits, new or removed pages, news, and LinkedIn posts. The tool already removed duplicates and obvious off-topic news. Your job is to label each item, and nothing else.

## Label every item

- **significant**: a move a marketer would act on or bring up in a meeting. A launch, a pricing or packaging change, funding, an acquisition, a partnership, a new segment or market, a clear shift in positioning or messaging, a senior hire or exit, a public number (customers, revenue, users).
- **minor**: real but routine. A blog post, an event or webinar, a small page edit, a hiring post, a thought-leadership post that repeats what they always say, coverage that repeats a story already logged.
- **noise**: not about this competitor, or not about anything. Homonyms (another company or a common word with the same name), syndicated copies of the same press release, press-release spam, cookie or legal boilerplate, cosmetic page diffs (dates, counters, reordered menus).

When you can't decide between minor and noise, choose **minor**. A slightly fuller timeline is better than a missing fact.

Items with `"suspect_noise": true` come from a page that has changed in tiny ways several scans in a row. Label them noise unless the change itself says something substantive.

## Write two short lines

- `so_what` (every item, 15 words max): why it matters, for the project described in the pack. This is interpretation and stays in the report.
- `finding` (required for **significant** items, 20 words max): what happened, as a plain fact. Who did what. No implication, no advice, no "verify", no hype. This is the line people see in the digest.

Good finding: "Bolt cut its Pro plan from $25 to $20 a month and added a team tier."
Bad finding: "Bolt is aggressively undercutting the market — we should respond."

Use the item's own text and URL. Don't invent numbers, names or dates that the item doesn't contain.

## Answer format

Return one JSON object and nothing else:

```json
{"verdicts": [
  {"id": "<item id>", "verdict": "significant", "so_what": "<=15 words", "finding": "<=20 words"},
  {"id": "<item id>", "verdict": "minor", "so_what": "<=15 words"}
]}
```

Every `id` in the pack appears exactly once. No extra ids. The tool rejects the answer otherwise.
