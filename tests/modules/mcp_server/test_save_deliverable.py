"""`save_deliverable`: answers written in Claude Desktop kept in the analyst run history."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import anyio
import httpx
from docx import Document
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp import Client

from src.modules.analyst.desktop import DESKTOP_CARD_ID
from src.modules.analyst.runs import CardRunner
from src.modules.content_processor.projects.runner import ScrapeRunner
from src.modules.content_processor.projects.security import TokenSigner
from src.modules.content_processor.webapi.app import create_app
from src.modules.mcp_server.bridge import McpBridge
from src.modules.mcp_server.catalog import EXTRA_TOOLS, definitions, instructions, parse_scope
from src.modules.mcp_server.server import build_server
from src.modules.reports.catalog import build_report
from tests.modules.analyst.conftest import World
from tests.modules.analyst.conftest import world as world  # noqa: F401 - the fixture
from tests.modules.mcp_server.test_mcp_server import WRITE, _call
from tests.modules.theming.test_theming import themed as themed  # noqa: F401 - the fixture


def _app(world: World, tmp_path: Path) -> tuple[FastAPI, CardRunner]:
    exports = tmp_path / "exports"
    exports.mkdir(exist_ok=True)
    runner = world.runner(enabled=False)
    app = create_app(
        exports,
        projects=(world.store, ScrapeRunner(world.store), TokenSigner(b"k" * 32)),
        knowledge=world.access,
        analyst=runner,
    )
    return app, runner


def _found(client: TestClient, project_id: str) -> tuple[str, str]:
    """A real reference the tools handed out, and the first words of its passage."""
    found, is_error = _call(client, "search_knowledge", project_id=project_id, query="term life")
    assert not is_error
    passage = found["data"]["passages"][0]
    return passage["ref"], " ".join(passage["text"].split()[:6])


def _doc_text(data: bytes) -> str:
    doc = Document(io.BytesIO(data))
    return "\n".join(p.text for p in doc.paragraphs)


def test_the_tool_is_listed_last_in_every_connection_and_announced_as_a_write() -> None:
    assert "save_deliverable" in EXTRA_TOOLS
    for scope in (None, parse_scope("themes"), parse_scope("gaps,research")):
        defs = definitions(scope)
        assert [d.name for d in defs[-3:]] == [
            "present_answer",
            "check_deliverable",
            "save_deliverable",
        ]
        last = defs[-1]
        assert last.writes and not last.exposes_text and last.module is None
        assert last.input_schema["required"] == ["project_id", "title", "markdown"]
        assert set(last.input_schema["properties"]) == {
            "project_id",
            "title",
            "markdown",
            "card_key",
            "module",
        }
        assert last.input_schema["additionalProperties"] is False
        assert "Written in Claude Desktop" in last.description
        assert "save_deliverable" in instructions(scope)
    assert all(not d.writes for d in definitions()[:-1])
    assert "call save_deliverable" in instructions(None)


def test_malformed_calls_are_refused_and_nothing_is_written(themed: World, tmp_path: Path) -> None:
    app, runner = _app(themed, tmp_path)
    client = TestClient(app, base_url="http://127.0.0.1")
    pid = themed.project_id
    text = "## Findings\n\nOne line.\n"

    def error(**arguments: Any) -> str:
        answer, is_error = _call(client, "save_deliverable", **arguments)
        assert is_error, answer
        return str(answer["error"])

    assert "project_id is required" in error(title="x", markdown=text)
    assert "project_id is required" in error(project_id="../x", title="x", markdown=text)
    assert "no project" in error(project_id="nope", title="x", markdown=text)
    assert "title is required" in error(project_id=pid, markdown=text)
    assert "title is required" in error(project_id=pid, title="   ", markdown=text)
    assert "longer than 200" in error(project_id=pid, title="t" * 201, markdown=text)
    assert "markdown is required" in error(project_id=pid, title="x")
    assert "unexpected arguments" in error(project_id=pid, title="x", markdown=text, text=text)
    assert "no action card" in error(project_id=pid, title="x", markdown=text, card_key="nope")
    assert "unknown module" in error(project_id=pid, title="x", markdown=text, module="keywords")
    assert runner.runs.all() == []
    # an app without the analyst module says so instead of failing
    bare = McpBridge(themed.store, themed.access)
    answer, is_error = bare.call(
        "save_deliverable", {"project_id": pid, "title": "x", "markdown": text}
    )
    assert is_error and "without the analyst module" in json.loads(answer)["error"]


def test_a_saved_answer_becomes_a_run_and_a_second_save_is_a_new_version(
    themed: World, tmp_path: Path
) -> None:
    app, runner = _app(themed, tmp_path)
    client = TestClient(app, base_url="http://127.0.0.1")
    pid = themed.project_id
    ref, words = _found(client, pid)
    good = f'## Findings\n\nThe page says "{words}" [{ref}].\n'

    saved, is_error = _call(
        client, "save_deliverable", project_id=pid, title="Term life review", markdown=good
    )
    assert not is_error, saved
    assert saved["status"] == "passed" and saved["saved"] is True
    assert saved["version"] == 1 and saved["versions"] == 1
    assert saved["author"] == "Claude Desktop" and saved["title"] == "Term life review"
    assert "[1]" in saved["readable_markdown"] and ref not in saved["readable_markdown"]
    assert ref in saved["sources_markdown"]
    assert "saved in the Analyst cards tab" in saved["how_to_present"]
    run_id = saved["run_id"]

    # the run is in the history, with everything the app shows
    records = runner.runs.all()
    assert [r.run_id for r in records] == [run_id]
    record = records[0]
    assert record.backend == "claude_desktop" and record.status == "finished"
    assert record.card_id == DESKTOP_CARD_ID and record.project_ids == [pid]
    assert record.title == "Term life review" and record.author == "Claude Desktop"
    assert record.verification is not None and record.verification.passed
    assert record.finished_at is not None and record.module is None
    assert [v.version for v in record.versions] == [1]
    folder = runner.runs.folder(run_id)
    stored = (folder / record.files["deliverable"]).read_text(encoding="utf-8")
    assert "[1]" in stored and "## Sources" in stored and "## Verification (by code)" in stored
    assert (folder / record.files["draft"]).read_text(encoding="utf-8") == good
    assert ref in json.loads((folder / "ledger.json").read_text(encoding="utf-8"))
    assert record.message == "saved from Claude Desktop; the check passed"

    # the web app lists and opens it
    listed = client.get(f"/api/projects/{pid}/analyst/runs").json()
    assert [(r["run_id"], r["backend"], r["title"]) for r in listed] == [
        (run_id, "claude_desktop", "Term life review")
    ]
    detail = client.get(f"/api/projects/{pid}/analyst/runs/{run_id}").json()
    assert detail["record"]["author"] == "Claude Desktop"
    assert "[1]" in detail["text"] and "## Sources" in detail["text"]
    assert "saved" in {e["type"] for e in detail["events"]}
    word = client.get(f"/api/projects/{pid}/analyst/runs/{run_id}/result/result.docx")
    assert word.status_code == 200 and "Sources" in _doc_text(word.content)
    assert "Term_life_review" in word.headers["content-disposition"]
    md = client.get(f"/api/projects/{pid}/analyst/runs/{run_id}/result/result.md")
    assert md.status_code == 200 and b"[1]" in md.content

    # the same title again (case and spacing aside): the same run, one more version
    better = good + "\nA second paragraph with the same source [" + ref + "].\n"
    again, is_error = _call(
        client, "save_deliverable", project_id=pid, title="  term LIFE review ", markdown=better
    )
    assert not is_error and again["run_id"] == run_id
    assert again["version"] == 2 and again["versions"] == 2
    assert "(version 2)" in again["how_to_present"]
    record = runner.runs.get(run_id)
    assert record.version == 2 and [v.version for v in record.versions] == [1, 2]
    assert record.title == "Term life review", "the first spelling is kept"
    assert len(runner.runs.all()) == 1
    latest = client.get(f"/api/projects/{pid}/analyst/runs/{run_id}").json()
    assert "A second paragraph" in latest["text"]
    first = client.get(f"/api/projects/{pid}/analyst/runs/{run_id}", params={"version": 1}).json()
    assert "A second paragraph" not in first["text"] and "[1]" in first["text"]
    assert (
        client.get(f"/api/projects/{pid}/analyst/runs/{run_id}", params={"version": 9}).status_code
        == 404
    )
    assert (folder / "deliverable.v1.md").exists() and (folder / "deliverable.v2.md").exists()

    # another title is another run; a card can be named
    other, is_error = _call(
        client,
        "save_deliverable",
        project_id=pid,
        title="Trust audit",
        markdown=good,
        card_key="eeat-trust-audit",
        module="content",
    )
    assert not is_error and other["run_id"] != run_id
    named = runner.runs.get(other["run_id"])
    assert named.card_id == "eeat-trust-audit" and named.card_version >= 1
    assert named.module == "content"
    assert len(client.get(f"/api/projects/{pid}/analyst/runs").json()) == 2


def test_a_bad_citation_needs_review_but_is_still_saved(themed: World, tmp_path: Path) -> None:
    app, runner = _app(themed, tmp_path)
    client = TestClient(app, base_url="http://127.0.0.1")
    pid = themed.project_id
    ref, words = _found(client, pid)
    text = (
        f'## Findings\n\nThe page says "{words}" [{ref}].\n\n'
        "It has 12 more pages [chunk:invented-1].\n"
    )
    saved, is_error = _call(
        client, "save_deliverable", project_id=pid, title="Draft", markdown=text
    )
    assert not is_error
    assert saved["status"] == "needs review" and saved["saved"] is True
    assert saved["unknown_refs"] == ["chunk:invented-1"]
    assert "save again with the same title" in saved["how_to_fix"]
    record = runner.runs.get(saved["run_id"])
    assert record.status == "finished" and record.verification is not None
    assert not record.verification.passed and "needs review" in record.message
    assert record.versions[0].passed is False
    stored = (runner.runs.folder(record.run_id) / record.files["deliverable"]).read_text(
        encoding="utf-8"
    )
    assert "Unknown citations" in stored and "unknown source (chunk:invented-1)" in stored


def test_a_confidential_project_can_save(themed: World, tmp_path: Path) -> None:
    app, runner = _app(themed, tmp_path)
    client = TestClient(app, base_url="http://127.0.0.1")
    pid = themed.project_id
    themed.store.update(pid, confidential=True)
    theme, is_error = _call(client, "get_theme_map", project_id=pid)
    assert not is_error
    theme_ref = theme["data"]["themes"][0]["ref"]
    saved, is_error = _call(
        client,
        "save_deliverable",
        project_id=pid,
        title="Themes in short",
        markdown=f"One theme stands out [{theme_ref}].\n",
    )
    assert not is_error and saved["status"] == "passed"
    assert runner.runs.get(saved["run_id"]).title == "Themes in short"
    # the other rules still hold: page text is refused for it
    refused, is_error = _call(client, "search_knowledge", project_id=pid, query="term life")
    assert is_error and "confidential" in refused["error"]


def test_a_scoped_connection_names_its_module_in_the_author(themed: World, tmp_path: Path) -> None:
    app, runner = _app(themed, tmp_path)
    client = TestClient(app, base_url="http://127.0.0.1")
    pid = themed.project_id

    def call(arguments: dict[str, Any], scope: list[str]) -> tuple[dict[str, Any], bool]:
        response = client.post(
            "/api/mcp/call",
            json={"tool": "save_deliverable", "arguments": arguments, "scope": scope},
            headers=WRITE,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        return json.loads(body["text"]), bool(body["is_error"])

    base = {"project_id": pid, "title": "Theme notes", "markdown": "## Notes\n\nShort.\n"}
    saved, is_error = call(base, ["themes"])
    assert not is_error and saved["author"] == "Claude Desktop (Themes)"
    record = runner.runs.get(saved["run_id"])
    assert record.author == "Claude Desktop (Themes)" and record.module == "themes"
    outside, is_error = call({**base, "module": "gaps"}, ["themes"])
    assert is_error and "limited to Themes" in outside["error"]


def test_saved_deliverables_are_in_the_downloads_report(themed: World, tmp_path: Path) -> None:
    app, runner = _app(themed, tmp_path)
    client = TestClient(app, base_url="http://127.0.0.1")
    pid = themed.project_id
    ref, words = _found(client, pid)
    text = f'## Findings\n\nThe page says "{words}" [{ref}].\n'
    for title in ("Report A", "Report A", "Report B"):
        saved, is_error = _call(
            client, "save_deliverable", project_id=pid, title=title, markdown=text
        )
        assert not is_error, saved

    def runs(project_id: str) -> list[object]:
        return [r for r in runner.runs.all() if project_id in r.project_ids]

    from src.modules.analyst.desktop import deliverable_text
    from src.modules.analyst.models import RunRecord

    report = build_report(
        "analyst",
        themed.store,
        pid,
        runs,
        lambda r: deliverable_text(runner.runs, r) if isinstance(r, RunRecord) else None,
    )
    assert report is not None
    assert [t.name for t in report.tables] == ["Analyst runs", "Written in Claude Desktop"]
    main = report.tables[0].rows
    assert {r["How"] for r in main} == {"Written in Claude Desktop"}
    assert {r["Card"] for r in main} == {"Report A", "Report B"}
    saved_rows = report.tables[1].rows
    assert [(r["Title"], r["Version"], r["Latest"]) for r in saved_rows] == [
        ("Report B", 1, "yes"),
        ("Report A", 2, "yes"),
        ("Report A", 1, ""),
    ]
    assert [h for h, _text in report.documents] == [
        f"Report B (version 1, {main[0]['Started']})",
        f"Report A (version 2, {main[1]['Started']})",
    ]
    assert all("[1]" in t for _h, t in report.documents)
    assert any(
        "2 deliverable(s) written in Claude Desktop, 3 version(s)" in s for s in report.summary
    )
    # the Word download carries the deliverables in full; Excel keeps the tables
    word = client.get(f"/api/projects/{pid}/exports/analyst.docx")
    assert word.status_code == 200
    doc_text = _doc_text(word.content)
    assert "Report A (version 2" in doc_text and "Report B (version 1" in doc_text
    assert "Sources" in doc_text and words.split()[0] in doc_text
    excel = client.get(f"/api/projects/{pid}/exports/analyst.xlsx")
    assert excel.status_code == 200 and excel.content[:2] == b"PK"


def test_over_the_protocol_the_tool_is_not_read_only(themed: World, tmp_path: Path) -> None:
    app, runner = _app(themed, tmp_path)

    async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        transport = httpx.ASGITransport(app=app)
        async with (
            httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1", headers=WRITE
            ) as http,
            Client(build_server(http)) as client,
        ):
            tools = await client.list_tools()
            hints = {
                t.name: (t.annotations.read_only_hint if t.annotations else None)
                for t in tools.tools
            }
            saved = await client.call_tool(
                "save_deliverable",
                {
                    "project_id": themed.project_id,
                    "title": "Over the wire",
                    "markdown": "## Notes\n\nShort.\n",
                },
            )
        content = saved.content[0]
        body = json.loads(content.text) if hasattr(content, "text") else {}
        return hints, body

    hints, body = anyio.run(scenario)
    assert hints["save_deliverable"] is False
    assert all(v is True for n, v in hints.items() if n != "save_deliverable")
    assert body["saved"] is True and body["status"] == "passed"
    assert runner.runs.get(body["run_id"]).title == "Over the wire"
