"""Autonomous URL grouping by path family and DOM template (Stage 1 interlink reuse)."""

from src.modules.content_processor.grouping.cluster import DEFAULT_SIMILARITY_THRESHOLD, group_urls
from src.modules.content_processor.grouping.model import (
    ExcludedUrl,
    GroupingResult,
    PageSignal,
    UrlGroup,
)

__all__ = [
    "DEFAULT_SIMILARITY_THRESHOLD",
    "ExcludedUrl",
    "GroupingResult",
    "PageSignal",
    "UrlGroup",
    "group_urls",
]
