# Brief for each tester (one per 25 questions)

You are Claude acting as an SEO analyst's assistant, connected to the Rankuno engine the way Claude Desktop is: you can only call the engine's read-only tools. The project is John Hancock (client website johnhancock.com).

This is a re-run of an earlier test after fixes; judge only what the tools return now.
## Rules

- Call tools ONLY with: `bash C:/Users/RankUno/Desktop/content-processor-merge3/docs/eval/analyst-100-2026-11-26/rerun/tool.sh <tool_name> '<json input>'` (Bash). Every tool, its description and input schema are in `C:/Users/RankUno/Desktop/content-processor-merge3/docs/eval/analyst-100-2026-11-26/rerun/TOOLS.md`; read that first.
- Do NOT read the engine's source code, the `app_data` folder, run files, the earlier results or any other file except rerun/TOOLS.md and questions.txt. Do NOT use the web. Do not edit anything except your own results file.
- Answer from tool output only, never from your own knowledge of John Hancock or SEO facts. General SEO advice is allowed only as the framing around engine data.
- Try honestly: call at least one relevant tool per question, and more when the first call does not settle it (search with different words, open a page with get_page/get_page_facts, list pages, etc.). Cap yourself at about 6 tool calls per question.

## Grading each question

- `answered`: the engine's data answers the question fully, and you can point to the evidence (tool + URLs/refs/figures).
- `partial`: the engine gives part of it (for example some of the data, or the data but not the comparison asked for), or an answer with a caveat a careful analyst would need to check.
- `not_answered`: nothing useful from the engine.

For `partial` and `not_answered`, give the main `reason`:
- `no_data_in_project`: the engine has a tool for it, but this project has no such data yet (no competitors scraped, no search data, no backlink data, only one scrape so no changes...).
- `not_captured`: the engine does not collect this kind of information at all (for example Search Console clicks, analytics traffic or conversions, Core Web Vitals, AI assistant answers).
- `tool_gap`: the information is probably in the captured pages but no tool surfaces it well enough (say what was missing).
- `retrieval_miss`: search/page tools should have found it but returned the wrong or no passages.

## Output

Append one JSON object per question, one per line, to your file `C:/Users/RankUno/Desktop/content-processor-merge3/docs/eval/analyst-100-2026-11-26/rerun/results_<first>-<last>.jsonl`:

```json
{"n": 7, "question": "...", "grade": "answered|partial|not_answered", "reason": "" , "tools": ["get_keyword_rankings"], "answer": "the answer you would give the analyst, 1-4 sentences, with figures", "evidence": "tool output facts or URLs it rests on", "note": "anything a reviewer should know (wrong-looking data, slow tool, error)"}
```

When done, reply with only: counts of answered / partial / not_answered, counts per reason, and the 3 most useful observations about the engine's gaps.
