"""What a lite scan keeps: the sitemap inventory, its sections and the sample scraped."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from src.core.schemas import StrictModel

__all__ = ["LiteInventory", "LitePick", "LiteSection", "PickReason"]

PickReason = Literal["pinned", "ranking", "core", "section"]


class LitePick(StrictModel):
    """One page of the sample, and why it was chosen."""

    url: str
    reason: PickReason
    detail: str = Field(default="", description="For example 'ranks for 12 keywords'")
    section: str = Field(default="", description="The address section it belongs to")


class LiteSection(StrictModel):
    """A section of the website's addresses (for example /blog/guides)."""

    section: str
    pages: int = Field(ge=0, description="Addresses the sitemap lists in it")
    sampled: int = Field(default=0, ge=0)


class LiteInventory(StrictModel):
    """One lite scan of one website."""

    project_id: str
    site_id: str
    website: str = Field(description="The domain whose sitemaps were read")
    created_at: datetime
    sitemap_files: list[str] = Field(default_factory=list)
    sitemap_note: str = ""
    inventory: list[str] = Field(
        default_factory=list, description="Every page address the sitemaps list (HTML pages)"
    )
    sections: list[LiteSection] = Field(default_factory=list)
    sample: list[LitePick] = Field(default_factory=list)
    run_id: str = Field(default="", description="The scrape queued for the sample")
    max_pages: int = Field(ge=1)

    @property
    def pages(self) -> int:
        """Pages the website publishes as far as we know (sitemap, or the sample when none)."""
        return max(len(self.inventory), len(self.sample))

    def basis(self) -> str:
        """Plain words for what competitor figures are based on."""
        if not self.inventory:
            return f"based on a sample of {len(self.sample):,} pages (no sitemap was readable)"
        return f"based on {len(self.sample):,} of about {self.pages:,} pages (lite scan)"
