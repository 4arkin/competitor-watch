"""Stands in for `claude -p` / `codex exec` / `gemini -p` in tests: reads the prompt (last
argument), labels every item, prints JSON the way a chatty agent might (inside a fence)."""

import json
import sys

prompt = sys.argv[-1]
pack = json.loads(prompt[prompt.index("{"):prompt.rindex("}") + 1].split("\n\nYour previous answer")[0])
out = []
for it in pack["items"]:
    text = it["fact"]
    if any(w in text for w in ("Series A", "Teams", "$20 per month", "Pro plan")) or it["source"] == "pages":
        out.append({"id": it["id"], "verdict": "significant", "so_what": "Moves upmarket against us.",
                    "finding": "Acme AI " + ("changed its pricing page." if it["source"] == "pages" else "announced a Series A or Teams plan.")})
    else:
        out.append({"id": it["id"], "verdict": "minor", "so_what": "Routine activity."})
print("Here are the verdicts:\n```json\n" + json.dumps({"verdicts": out}) + "\n```")
