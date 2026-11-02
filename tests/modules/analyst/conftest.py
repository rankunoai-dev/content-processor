"""A small project with two indexed websites, and a fake Claude client."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from src.modules.analyst.runs import AnalystSettings, CardRunner
from src.modules.content_processor.knowledge.catalog_models import (
    Catalog,
    Evidence,
    Offering,
    OfferingLevel,
    PageLink,
)
from src.modules.content_processor.knowledge.embedders import HashingEmbedder
from src.modules.content_processor.knowledge.federated import Space, space_namespace
from src.modules.content_processor.knowledge.indexing import RunInputs, index_run
from src.modules.content_processor.knowledge.inventory_models import Issue, PageInventory
from src.modules.content_processor.knowledge.labels import Labeller
from src.modules.content_processor.knowledge.models import Chunk, Cta, PageStamp, SchemaFacts
from src.modules.content_processor.knowledge.vector_store import SqliteVectorStore, VectorStore
from src.modules.content_processor.projects.models import RunState
from src.modules.content_processor.projects.store import ProjectStore, SiteSpec
from src.modules.content_processor.webapi.projects_api import KnowledgeAccess

WHEN = datetime(2026, 11, 1, tzinfo=UTC)
CLIENT = "https://www.example.com"
RIVAL = "https://www.rival.com"

TERM = (
    "Term life insurance covers you for 10, 20 or 30 years. You can convert your term policy "
    "to permanent coverage before age 70 without a medical exam."
)
PROOF = "Example Life is rated A+ by A.M. Best for financial strength since 1990."
RIVAL_TERM = (
    "Rival term life starts at $25 per month. Rated A++ by A.M. Best. Compare plans and get a "
    "quote online in minutes."
)
RIVAL_FAQ = "Can I convert my term policy?\nYes, before age 75, with no medical exam."


def stamp(base: str, path: str, title: str, run_id: str, **extra: Any) -> PageStamp:
    return PageStamp(
        run_id=run_id,
        url=base + path,
        page_key=f"pg-{base[-11:-4]}{path.replace('/', '-')}",
        site=base.split("//")[1],
        host=base.split("//")[1],
        stamped_at=WHEN,
        knowledge_eligible=True,
        badge="VERIFIED",
        title=title,
        h1=title,
        path_segments=[p for p in path.split("/") if p],
        **extra,
    )


def chunk(
    st: PageStamp, cid: str, text: str, order: int, headings: list[str], **extra: Any
) -> Chunk:
    return Chunk(
        chunk_id=cid,
        run_id=st.run_id,
        url=st.url,
        page_key=st.page_key,
        order=order,
        kind=extra.pop("kind", "text"),
        heading_path=headings,
        parent_section_id=f"sec-{cid}",
        text=text,
        context_header=" > ".join(headings),
        embed_text=" > ".join(headings) + "\n" + text,
        block_ids=[f"blk-{cid}"],
        evidence_ids=[f"evidence:{cid}{'0' * 40}"],
        ui_regions=["main_content"],
        word_count=len(text.split()),
        token_estimate=len(text.split()) * 2,
        **extra,
    )


def _jsonl(path: Path, items: list[Any]) -> None:
    path.write_text("".join(i.model_dump_json() + "\n" for i in items), encoding="utf-8")


@dataclass
class World:
    """The fixture: a project, its store, knowledge access and runner."""

    root: Path
    store: ProjectStore
    access: KnowledgeAccess
    project_id: str
    site_ids: dict[str, str]
    knowledge_root: Path
    clients: list[Any] = field(default_factory=list)

    def runner(self, **settings: Any) -> CardRunner:
        base: dict[str, Any] = {"enabled": True, "monthly_cap_usd": 50.0, "run_cap_usd": 2.0}
        base.update(settings)
        return CardRunner(
            self.store,
            self.access,
            self.root / "analyst",
            settings=AnalystSettings(**base),
            client_factory=lambda: self.clients.pop(0),
        )

    def add_run(
        self,
        role: str,
        run_id: str,
        stamps: list[PageStamp],
        chunks: list[Chunk],
        catalog: Catalog | None = None,
        *,
        offset_h: int = 0,
    ) -> Path:
        site_id = self.site_ids[role]
        folder = self.store.run_dir(self.project_id, site_id, run_id)
        folder.mkdir(parents=True, exist_ok=True)
        _jsonl(folder / f"{run_id}.stamps.jsonl", stamps)
        _jsonl(folder / f"{run_id}.chunks.jsonl", chunks)
        if catalog is not None:
            (folder / f"{run_id}.catalog.json").write_text(catalog.model_dump_json(), "utf-8")
        namespace = space_namespace("rankuno-dev", self.project_id, site_id)
        db = SqliteVectorStore(self.knowledge_root / namespace / "vectors.sqlite")
        try:
            index_run(
                RunInputs(run_id, folder, chunks, {s.page_key: s for s in stamps}, catalog),
                db,
                HashingEmbedder(),
                Labeller({s.page_key: s for s in stamps}, catalog),
            )
        finally:
            db.close()
        moment = WHEN + timedelta(hours=offset_h)
        self.store.save_run(
            RunState(
                run_id=run_id,
                project_id=self.project_id,
                site_id=site_id,
                status="finished",
                created_at=moment,
                started_at=moment,
                finished_at=moment,
                started_by="test",
                url_count=len(stamps),
                batch_size=25,
                batches_total=1,
                batches_done=1,
                reports_ready=True,
                knowledge_status="ready",
            )
        )
        return folder


def _catalog(run_id: str) -> Catalog:
    def offering(oid: str, name: str, level: str, parent: str | None, href: str) -> Offering:
        return Offering(
            offering_id=oid,
            name=name,
            level=cast(OfferingLevel, level),
            parent_id=parent,
            href=CLIENT + href,
            signals=["header_menu"],
            evidence=[Evidence(kind="menu", ref="menu-h", detail=name)],
            pages=[PageLink(url=CLIENT + href, basis="same_url")],
        )

    return Catalog(
        run_id=run_id,
        site="example.com",
        site_name="Example Life",
        built_at=WHEN,
        offerings=[
            offering("off-life", "Life insurance", "family", None, "/life"),
            offering("off-term", "Term life insurance", "offering", "off-life", "/life/term"),
        ],
    )


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    store = ProjectStore(tmp_path / "app_data")
    project = store.create(
        name="Test project",
        owner_name="Tester",
        client_name="Example Life",
        password="password-123",
        client_site=SiteSpec(name="Example Life", website=CLIENT),
        competitors=[SiteSpec(name="Rival", website=RIVAL)],
    )
    knowledge_root = tmp_path / "knowledge"

    def open_store(space: Space) -> VectorStore:
        return SqliteVectorStore(knowledge_root / space.namespace / "vectors.sqlite")

    access = KnowledgeAccess(open_store=open_store, embedder_for=lambda _m: HashingEmbedder())
    w = World(
        root=tmp_path / "app_data",
        store=store,
        access=access,
        project_id=project.project_id,
        site_ids={s.role: s.site_id for s in project.sites},
        knowledge_root=knowledge_root,
    )
    c1 = stamp(
        CLIENT,
        "/life/term",
        "Term life insurance",
        "run-c1",
        ctas=[Cta(text="Get a quote", url=CLIENT + "/quote", strength="strong")],
    )
    c2 = stamp(CLIENT, "/about", "About Example Life", "run-c1")
    c3 = stamp(CLIENT, "/life/term-copy", "Term life insurance", "run-c1")
    client_chunks = [
        chunk(c1, "c-term", TERM, 0, ["Term life insurance"]),
        chunk(c2, "c-proof", PROOF, 0, ["About Example Life", "Ratings"]),
        chunk(c3, "c-term-copy", TERM, 0, ["Term life insurance"]),
    ]
    folder = w.add_run("client", "run-c1", [c1, c2, c3], client_chunks, _catalog("run-c1"))
    inventory = [
        PageInventory(
            run_id="run-c1",
            url=c1.url,
            page_key=c1.page_key,
            inventoried_at=WHEN,
            status="inventoried",
            issues=[Issue(code="title_too_long", severity="warning", message="Long title")],
        ),
        PageInventory(
            run_id="run-c1",
            url=c2.url,
            page_key=c2.page_key,
            inventoried_at=WHEN,
            status="inventoried",
            issues=[
                Issue(code="missing_canonical", severity="error", message="No canonical"),
                Issue(code="title_too_long", severity="warning", message="Long title"),
            ],
        ),
    ]
    _jsonl(folder / "run-c1.inventory.jsonl", inventory)
    r1 = stamp(
        RIVAL,
        "/term-life",
        "Rival term life",
        "run-r1",
        schema_org=SchemaFacts(types=["FAQPage"], faq_questions=["Can I convert my term policy?"]),
    )
    w.add_run(
        "competitor",
        "run-r1",
        [r1],
        [
            chunk(r1, "r-term", RIVAL_TERM, 0, ["Rival term life"]),
            chunk(r1, "r-faq", RIVAL_FAQ, 1, ["Rival term life", "FAQ"], kind="faq"),
        ],
    )
    yield w


# -- a fake Claude client ----------------------------------------------------------------------


def text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def tool_block(tid: str, name: str, args: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=tid, name=name, input=args)


def reply(
    content: list[SimpleNamespace],
    stop: str,
    *,
    inp: int = 1000,
    out: int = 500,
    model: str = "claude-opus-5-5",
    details: Any = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        content=content,
        stop_reason=stop,
        stop_details=details,
        model=model,
        usage=SimpleNamespace(
            input_tokens=inp,
            output_tokens=out,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )


class _Stream:
    def __init__(self, item: Any) -> None:
        self.item = item

    def __enter__(self) -> _Stream:
        if isinstance(self.item, BaseException):
            raise self.item
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    @property
    def text_stream(self) -> Iterator[str]:
        for block in self.item.content:
            if block.type == "text":
                yield block.text

    def get_final_message(self) -> Any:
        return self.item


class FakeClaude:
    """Replies (or raises) in order; records every request's parameters."""

    def __init__(self, items: list[Any]) -> None:
        self.items = list(items)
        self.requests: list[dict[str, Any]] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **params: Any) -> _Stream:
        self.requests.append(json.loads(json.dumps(params, default=lambda o: vars(o))))
        return _Stream(self.items.pop(0))
