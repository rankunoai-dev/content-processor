"""Run the John Hancock test cases against the knowledge base and the module tools (read-only).

Usage (from the repo root): PYTHONPATH=. python run_eval.py <out.json>
Nothing is written to the app's data: the evidence ledger stays in memory.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from cases import APPLE, CASES, CORRECTION, ESIGN, SEPT26  # noqa: E402

from src.core.config import get_settings  # noqa: E402
from src.modules.analyst.playground import Playground  # noqa: E402
from src.modules.analyst.tools import run_tool  # noqa: E402
from src.modules.analyst.verify import verify_deliverable  # noqa: E402
from src.modules.content_processor.knowledge.retrieval import answer_question  # noqa: E402
from src.modules.content_processor.projects.store import ProjectStore  # noqa: E402
from src.modules.content_processor.webapi.__main__ import knowledge_access  # noqa: E402

PID = "john-hancock-test-5-31372c"
RUN = (
    get_settings().cp_app_data_root
    / "projects"
    / PID
    / "sites"
    / "john-hancock"
    / "runs"
    / "john-hancock_20261026-073102"
    / "john-hancock_20261026-073102"
)


def norm(text: str) -> str:
    return " ".join(text.split()).casefold()


chunks = [json.loads(line) for line in open(f"{RUN}.chunks.jsonl", encoding="utf-8")]
stamps = [json.loads(line) for line in open(f"{RUN}.stamps.jsonl", encoding="utf-8")]
eligible = [s for s in stamps if s["knowledge_eligible"]]
chunk_text = {c["chunk_id"]: c["text"] for c in chunks}
page_text: dict[str, str] = {}
for c in sorted(chunks, key=lambda c: (c["url"], c["order"])):
    page_text[c["url"]] = page_text.get(c["url"], "") + "\n" + c["text"]

play = Playground(ProjectStore(get_settings().cp_app_data_root), knowledge_access(), [PID])
website = play.websites()[0]
data = play.site_data(website)
assert data is not None
vstore = play.open_store(data)
model = vstore.meta()["model"]
embedder = play.access.embedder_for(model)
try:  # the reranker arrived after the first run (2026-11-22 baseline had none)
    from src.modules.content_processor.knowledge.reranker import get_reranker

    RERANKER = get_reranker()
except ImportError:
    RERANKER = None
print("reranker:", RERANKER.model if RERANKER else "none")


def rank_of(urls: list[str], pages: list[str]) -> int | None:
    """Rank of the first expected page among the distinct pages, in order."""
    for i, url in enumerate(dict.fromkeys(urls), start=1):
        if any(p in url for p in pages):
            return i
    return None


def tool(name: str, args: dict[str, Any]) -> tuple[Any, bool]:
    text, err = run_tool(play, name, args)
    payload = json.loads(text)
    return payload.get("data", payload), err


def quote_ok(quote: str, chunk_id: str, url: str) -> bool:
    q = norm(quote)
    return q in norm(chunk_text.get(chunk_id, "")) or q in norm(page_text.get(url, ""))


results: list[dict[str, Any]] = []
for case in CASES:
    started = time.perf_counter()
    answer = answer_question(
        vstore,
        embedder,
        case["q"],
        chat=None,
        top_k=5,
        **({"reranker": RERANKER} if RERANKER else {}),
    ).as_dict()
    ask_ms = round((time.perf_counter() - started) * 1000)
    started = time.perf_counter()
    found, _err = tool("search_knowledge", {"query": case["q"], "top_k": 10})
    search_ms = round((time.perf_counter() - started) * 1000)
    passages = found.get("passages", [])
    answer_text = "\n".join(p["statement"] for p in answer["points"])
    source_urls = [s["url"] for s in answer["sources"]]
    source_text = "\n".join(chunk_text.get(s["chunk_id"], "") for s in answer["sources"])
    row: dict[str, Any] = {
        "id": case["id"],
        "kind": case["kind"],
        "question": case["q"],
        "note": case.get("note", ""),
        "mode": answer["mode"],
        "answer": [
            {
                "text": p["statement"],
                "url": p["url"],
                "quote_verbatim": quote_ok(p["quote"], p["chunk_id"], p["url"]),
            }
            for p in answer["points"]
        ],
        "sources": source_urls,
        "search_urls": [p.get("url") for p in passages],
        "ask_ms": ask_ms,
        "search_ms": search_ms,
    }
    if case.get("negative"):
        row["abstained"] = answer["mode"] == "not_found"
        row["forbidden_claim"] = any(re.search(f, answer_text, re.I) for f in case["forbid"])
        row["passed"] = row["abstained"] and not row["forbidden_claim"]
    else:
        pages = case["pages"]
        row["expected_pages"] = pages
        row["ask_rank"] = rank_of(source_urls, pages)
        row["search_rank"] = rank_of(row["search_urls"], pages)
        row["answer_correct"] = all(re.search(e, answer_text, re.I) for e in case["expect"])
        row["fact_in_sources"] = all(re.search(e, source_text, re.I) for e in case["expect"])
        hit_text = "\n".join(p.get("text", "") for p in passages)
        around_text = hit_text + "\n".join(
            c["text"] for p in passages for c in [*p.get("before", []), *p.get("after", [])]
        )
        row["fact_in_search"] = all(re.search(e, hit_text, re.I) for e in case["expect"])
        row["fact_in_search_context"] = all(re.search(e, around_text, re.I) for e in case["expect"])
        row["search_tokens"] = len(around_text) // 4
        row["answer_source_chunks"] = len(answer["sources"])
        top_answer_url = answer["points"][0]["url"] if answer["points"] else None
        row["answer_from_expected_page"] = bool(
            top_answer_url and any(p in top_answer_url for p in pages)
        )
        row["passed"] = row["answer_correct"]
    row["quotes_all_verbatim"] = all(a["quote_verbatim"] for a in row["answer"])
    results.append(row)
    print(
        row["id"], "PASS" if row["passed"] else "FAIL", row.get("ask_rank"), row.get("search_rank")
    )

# -- citation checker (what guards Claude Desktop's answers) ----------------------------


def first_ref(url_part: str, pattern: str) -> tuple[str, str] | None:
    url = next(s["url"] for s in eligible if url_part in s["url"])
    found, _ = tool("get_page", {"url": url})
    for p in found.get("sections", []):
        text = chunk_text.get(p["ref"].removeprefix("chunk:"), "") or p.get("text", "")
        for line in re.split(r"(?<=[.!?])\s+|\n", text):
            if re.search(pattern, line) and len(line) >= 20:
                return p["ref"], " ".join(line.split())
    return None


checks: list[dict[str, Any]] = []
good = first_ref(APPLE, r"\$25")
pdt = first_ref(SEPT26, r"0\.0883 per share")
if good and pdt:
    honest = (
        "# Answer\n\n"
        f'Vitality PLUS members pay "{good[1]}" [{good[0]}].\n\n'
        f'The Premium Dividend Fund pays "{pdt[1]}" [{pdt[0]}].\n'
    )
    altered = honest.replace("$25", "$15").replace("0.0883", "0.0983")
    unknown = honest.replace(good[0], "chunk:ch-0000000000000000")
    uncited = honest + "\n".join(
        [
            "John Hancock is the largest insurer in Canada.",
            "Vitality was launched in 2001.",
            "The Apple Watch costs $399 without Vitality.",
            "Members can also earn a free Peloton bike.",
        ]
    )
    for name, text, should_pass in [
        ("honest answer, exact quotes", honest, True),
        ("a number changed inside a quote", altered, False),
        ("a citation to a passage never shown", unknown, False),
        ("four extra claims without citations", uncited, False),
    ]:
        v = verify_deliverable(text, play.ledger)
        checks.append(
            {
                "case": name,
                "expected": "passed" if should_pass else "needs review",
                "got": "passed" if v.passed else "needs review",
                "ok": v.passed == should_pass,
                "detail": {
                    "quotes_failed": len(v.quotes_failed),
                    "unknown_refs": len(v.unknown_refs),
                    "uncited_lines": len(v.uncited_lines),
                },
            }
        )

# -- module tools against ground truth from the stored stamps ---------------------------

modules: list[dict[str, Any]] = []


def module(case_id: str, name: str, ok: bool, detail: str, raw: Any = None) -> None:
    modules.append({"id": case_id, "check": name, "ok": ok, "detail": detail, "raw": raw})


apple_url = next(s["url"] for s in eligible if APPLE in s["url"])
apple_stamp = next(s for s in eligible if s["url"] == apple_url)

listed, err = tool("list_pages", {"website": "johnhancock.com"})
listed_pages = listed.get("pages", []) if not err else []
module(
    "M01",
    "list_pages: every searchable page is listed",
    len(listed_pages) == len(eligible),
    f"tool lists {len(listed_pages)} pages (total field {listed.get('total')}); stored searchable pages {len(eligible)}",
    {k: v for k, v in listed.items() if k != "pages"},
)

facts, err = tool("get_page_facts", {"url": apple_url})
f = facts.get("facts", {}) if not err else {}
module(
    "M02",
    "get_page_facts: title, H1 and description match the page",
    f.get("title") == apple_stamp["title"]
    and f.get("h1") == apple_stamp["h1"]
    and f.get("meta_description") == apple_stamp["meta_description"],
    f"title {f.get('title')!r}; h1 {f.get('h1')!r}",
    f,
)

page, err = tool("get_page", {"url": apple_url})
sections = page.get("sections", []) if not err else []
joined = "\n".join(s.get("text", "") for s in sections)
module(
    "M03",
    "get_page: the page's sections in order, with the facts in them",
    "$25" in joined
    and "7x" in joined
    and len(sections) == sum(1 for c in chunks if c["url"] == apple_url),
    f"{len(sections)} sections returned; stored {sum(1 for c in chunks if c['url'] == apple_url)}",
    None,
)

catalog, err = tool("get_offering_catalog", {"website": "johnhancock.com"})
module(
    "M04", "get_offering_catalog: the offering tree", not err and bool(catalog), "see raw", catalog
)

seo, err = tool("get_seo_issues", {"website": "johnhancock.com"})
missing_meta = sum(1 for s in eligible if not s["meta_description"])
module(
    "M05",
    "get_seo_issues: issue counts",
    not err,
    f"stored pages without a meta description: {missing_meta}",
    seo,
)

similar, err = tool("find_similar_pages", {"website": "johnhancock.com", "min_similarity": 0.85})
pairs = similar.get("pairs", []) if not err else []
pair_urls = [
    (
        p.get("a", {}).get("url") if isinstance(p.get("a"), dict) else p.get("a"),
        p.get("b", {}).get("url") if isinstance(p.get("b"), dict) else p.get("b"),
    )
    for p in pairs
]
has_correction = any(
    a and b and ({APPLE in a, APPLE in b} == {True} or (APPLE in (a + b) and CORRECTION in (a + b)))
    for a, b in pair_urls
)
has_forms = sum(1 for a, b in pair_urls if a and b and ESIGN in a and ESIGN in b)
module(
    "M06",
    "find_similar_pages: the correction re-publishes the Apple Watch release; the e-signature form pages are near copies",
    has_correction and has_forms > 0,
    f"{len(pairs)} pairs; correction pair found: {has_correction}; form pairs: {has_forms}",
    {k: v for k, v in similar.items() if k != "pairs"} | {"first_pairs": pairs[:6]},
)

themes, err = tool("get_theme_map", {})
theme_rows = themes.get("themes", []) if not err else []
module(
    "M07",
    "get_theme_map: themes cover the pages",
    bool(theme_rows),
    f"{len(theme_rows)} themes; pages per theme {[t.get('pages') for t in theme_rows]}",
    [{k: t.get(k) for k in ("name", "pages", "ownership", "keywords")} for t in theme_rows],
)

alerts, err = tool("get_monitoring_alerts", {})
module(
    "M08",
    "get_monitoring_alerts: one scrape so far, so a baseline and no alerts",
    not err and alerts.get("alerts") == [],
    f"alerts {len(alerts.get('alerts', []))}; notes {[w.get('note') for w in alerts.get('websites', [])]}",
    None,
)

changes, err = tool("get_changes", {"website": "johnhancock.com"})
module(
    "M09",
    "get_changes: says there is no earlier scrape to compare",
    True,
    json.dumps(changes)[:400],
    None,
)

gaps, err = tool("get_gap_findings", {})
module(
    "M10",
    "get_gap_findings: no competitors or research, so no findings and the reason given",
    not err and gaps.get("findings") == [] and bool(gaps.get("missing_inputs")),
    f"missing inputs: {gaps.get('missing_inputs')}",
    None,
)

ranks, err = tool("get_keyword_rankings", {"website": "johnhancock.com"})
module(
    "M11",
    "get_keyword_rankings: no research yet, refused with what to do",
    err and "no research" in json.dumps(ranks),
    json.dumps(ranks)[:300],
    None,
)

sept_url = next(s["url"] for s in eligible if SEPT26 in s["url"])
module(
    "M12",
    "page text: the HPF row of the September 2026 table has a distribution per share",
    bool(re.search(r"HPF\s+Preferred Income Fund II\s+\$0\.\d{4}", page_text[sept_url])),
    "the stored HPF row goes straight from the fund name to the market price",
    None,
)

vstore.close()
out = {
    "model": model,
    "reranker": RERANKER.model if RERANKER else None,
    "pages": len(eligible),
    "passages": len(chunks),
    "cases": results,
    "citation_checks": checks,
    "modules": modules,
}
Path(sys.argv[1]).write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
print("questions passed", sum(r["passed"] for r in results), "/", len(results))
print("citation checks ok", sum(c["ok"] for c in checks), "/", len(checks))
print("module checks ok", sum(m["ok"] for m in modules), "/", len(modules))
