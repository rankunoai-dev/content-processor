"""Group URLs by path family and DOM template (autonomous grouping, Bit 1).

Algorithm, per run:
1. Gate every page (gate.assess). Excluded pages are reported, never grouped.
2. Bucket eligible pages by path family (prefix.path_family).
3. Inside a family, link two template pages when their ordered skeleton similarity
   reaches the threshold (skeleton.skeleton_similarity). Identical fingerprints are
   similar by definition and skip the comparison. Groups are connected components.
4. Shell pages (client-rendered, skeleton not trusted) form one spa_shell group per
   family, with no template claim.

Deterministic: groups are ordered by family and first member, and group ids are
hashes of their family and sorted members, so the same input gives the same output.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Sequence

from src.modules.content_processor.grouping.gate import assess
from src.modules.content_processor.grouping.model import (
    ExcludedUrl,
    GroupingResult,
    PageSignal,
    UrlGroup,
)
from src.modules.content_processor.grouping.prefix import path_family
from src.modules.content_processor.grouping.skeleton import (
    skeleton_fingerprint,
    skeleton_similarity,
    skeleton_tokens,
)

__all__ = ["DEFAULT_SIMILARITY_THRESHOLD", "group_urls"]

# Ordered skeleton similarity. One extra aside in a 25-token skeleton scores 0.96, and a
# swapped main/footer scores 0.92, so both merge at 0.9. Different layouts fall well below.
DEFAULT_SIMILARITY_THRESHOLD = 0.9


class _Member:
    __slots__ = ("url", "tokens", "fingerprint")

    def __init__(self, url: str, tokens: list[str], fingerprint: str) -> None:
        self.url = url
        self.tokens = tokens
        self.fingerprint = fingerprint


def _find(parent: list[int], i: int) -> int:
    while parent[i] != i:
        parent[i] = parent[parent[i]]
        i = parent[i]
    return i


def _group_id(family: str, members: Sequence[str]) -> str:
    digest = hashlib.sha256(json.dumps([family, sorted(members)]).encode()).hexdigest()
    return f"grp-{digest[:12]}"


def group_urls(
    pages: Sequence[PageSignal],
    *,
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
) -> GroupingResult:
    """Group fetched pages into template clusters within their path families."""
    excluded: list[ExcludedUrl] = []
    templates: dict[str, list[_Member]] = defaultdict(list)
    shells: dict[str, list[str]] = defaultdict(list)

    for page in pages:
        verdict = assess(page)
        family = path_family(page.url)
        if verdict.kind == "excluded":
            excluded.append(ExcludedUrl(url=page.url, reason=verdict.reason or "excluded"))
        elif verdict.kind == "spa_shell":
            shells[family].append(page.url)
        else:
            tokens = skeleton_tokens(page.body)
            templates[family].append(_Member(page.url, tokens, skeleton_fingerprint(tokens)))

    groups: list[UrlGroup] = []
    for family in sorted(templates):
        members = sorted(templates[family], key=lambda m: m.url)
        groups.extend(_cluster_family(family, members, similarity_threshold))

    for family in sorted(shells):
        urls = sorted(shells[family])
        groups.append(
            UrlGroup(
                group_id=_group_id(family, urls),
                path_prefix=family,
                members=urls,
                reason="spa_shell",
                template_fingerprint=None,
                confidence=0.0,
            )
        )

    groups.sort(key=lambda g: (g.path_prefix, g.members[0]))
    excluded.sort(key=lambda e: e.url)
    return GroupingResult(
        groups=groups, excluded=excluded, similarity_threshold=similarity_threshold
    )


def _pair_similarity(a: _Member, b: _Member) -> float:
    if a.fingerprint == b.fingerprint:
        return 1.0
    return skeleton_similarity(a.tokens, b.tokens)


def _cluster_family(family: str, members: list[_Member], threshold: float) -> list[UrlGroup]:
    n = len(members)
    parent = list(range(n))
    pair_sim: dict[tuple[int, int], float] = {}
    for i in range(n):
        for j in range(i + 1, n):
            sim = _pair_similarity(members[i], members[j])
            pair_sim[(i, j)] = sim
            if sim >= threshold:
                parent[_find(parent, i)] = _find(parent, j)

    components: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        components[_find(parent, i)].append(i)

    out: list[UrlGroup] = []
    for idxs in components.values():
        idxs.sort(key=lambda k: members[k].url)
        urls = [members[k].url for k in idxs]
        if len(idxs) == 1:
            out.append(
                UrlGroup(
                    group_id=_group_id(family, urls),
                    path_prefix=family,
                    members=urls,
                    reason="singleton",
                    template_fingerprint=None,
                    confidence=1.0,
                )
            )
            continue
        sims = [
            pair_sim[(min(a, b), max(a, b))] for ai, a in enumerate(idxs) for b in idxs[ai + 1 :]
        ]
        out.append(
            UrlGroup(
                group_id=_group_id(family, urls),
                path_prefix=family,
                members=urls,
                reason="template_match",
                template_fingerprint=members[idxs[0]].fingerprint,
                confidence=round(sum(sims) / len(sims), 4),
            )
        )
    return out
