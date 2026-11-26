"""Per-case tables of the report (Markdown) and a CSV, from results.json."""

import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from reasons import EXPECTED, REASON  # noqa: E402

here = Path(__file__).parent
d = json.loads((here / "results.json").read_text(encoding="utf-8"))
NAMES = {
    "fact": "Facts",
    "number": "Numbers and tables",
    "explain": "Explanations",
    "rule": "Rules and conditions",
    "people": "People and roles",
    "keyword": "Exact codes",
    "paraphrase": "Paraphrase and other language",
    "recency": "Most recent of many",
    "negative": "Not on the website",
}


def cell(text: str, limit: int = 150) -> str:
    text = " ".join(str(text).split()).replace("|", "/")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def rank(x: int | None) -> str:
    return "not in top 10" if x is None else str(x)


lines: list[str] = []
rows = []
for kind, name in NAMES.items():
    cases = [r for r in d["cases"] if r["kind"] == kind]
    if not cases:
        continue
    passed = sum(r["passed"] for r in cases)
    lines += [
        f"### {name} ({passed} of {len(cases)} passed)",
        "",
        "| Case | Question | Expected (from the page) | What the system answered | Right page in search | Result | Why |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in cases:
        said = r["answer"][0]["text"] if r["answer"] else "(not found)"
        result = "Pass" if r["passed"] else "Fail"
        lines.append(
            f"| {r['id']} | {cell(r['question'], 110)} | {cell(EXPECTED[r['id']], 70)} | {cell(said)} | "
            f"{'-' if kind == 'negative' else rank(r.get('search_rank'))} | **{result}** | {cell(REASON[r['id']], 220)} |"
        )
        rows.append(
            {
                "case": r["id"],
                "intent": name,
                "question": r["question"],
                "expected": EXPECTED[r["id"]],
                "answer_mode": r["mode"],
                "answer_points": " || ".join(a["text"] for a in r["answer"]),
                "answer_pages": " || ".join(a["url"] for a in r["answer"]),
                "search_rank_of_right_page": r.get("search_rank"),
                "fact_in_retrieved_passages": r.get("fact_in_sources"),
                "quotes_verbatim": r["quotes_all_verbatim"],
                "result": result,
                "why": REASON[r["id"]],
                "ask_ms": r["ask_ms"],
                "search_ms": r["search_ms"],
            }
        )
    lines.append("")
(here / "case_tables.md").write_text("\n".join(lines), encoding="utf-8")
with (here / "rag_eval_cases.csv").open("w", newline="", encoding="utf-8-sig") as handle:
    writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
print(len(rows), "rows")
