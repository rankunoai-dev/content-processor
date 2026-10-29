"""Exploration records (B-1; kinds for B-4 to B-12 and B-14).

Every discovered control ends in exactly one terminal outcome, so the control
count is an honest denominator: visited, initially open/selected, excluded by
design (site navigation menus), or unvisited with a reason.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from src.core.schemas import StrictModel

__all__ = [
    "ControlKind",
    "ControlRecord",
    "ControlStatus",
    "ExplorationResult",
    "ExploredState",
    "mark_responsive_duplicates",
]

ControlKind = Literal[
    "disclosure",
    "accordion",
    "tab",
    "find_reveal",
    "menu",
    "scroll_surface",
    "carousel",
    "hover",
    "keyboard",
    "dialog",
    "consent",
    "load_more",
    "pagination",
    "frame",
    "shadow_root",
    "variant",
    "facet",
]
ControlStatus = Literal["pending", "visited", "initially_open", "excluded", "unvisited"]


class ControlRecord(StrictModel):
    """One interactive control found in the page."""

    control_id: str
    kind: ControlKind
    label: str = ""
    status: ControlStatus = "pending"
    reason: str | None = None
    state_index: int | None = Field(default=None, description="Captured state this click produced")


class ExploredState(StrictModel):
    """The page after one successful, side-effect-free action."""

    index: int = Field(ge=1)
    control_id: str
    kind: ControlKind
    label: str = ""
    inner_text: str
    dom_html: bytes
    text_delta_chars: int = Field(
        description="Visible text length change versus the previous state"
    )
    frame_path: str = Field(
        default="main", description="Frame the state was read from (iframe URL or srcdoc:<n>)"
    )


class ExplorationResult(StrictModel):
    """Outcome of exploring one page within the declared policy and budgets."""

    controls: list[ControlRecord] = Field(default_factory=list)
    states: list[ExploredState] = Field(default_factory=list)
    actions_used: int = Field(default=0, ge=0)
    scroll_surfaces: list[dict[str, Any]] = Field(
        default_factory=list, description="Per-surface scroll results (B-8, B-10)"
    )
    action_budget: int = Field(default=0, ge=0)
    elapsed_s: float = Field(default=0.0, ge=0.0)
    stop_reason: str | None = None
    unsafe_requests: list[str] = Field(default_factory=list)
    background_requests_blocked: int = Field(
        default=0,
        ge=0,
        description="Non-GET requests to endpoints that also fire with no action running "
        "(sensor beacons, analytics): blocked, not charged to the control being clicked",
    )
    followup_urls: list[str] = Field(
        default_factory=list,
        description="Pagination links that are separate URLs; the frontier owns them (B-7)",
    )
    composition: dict[str, Any] = Field(
        default_factory=dict,
        description="Shadow roots and frames read into the captured states (B-11, B-12)",
    )
    variant_dimensions: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Product option pickers (select_variant): name, kind, options, states",
    )
    facets: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Listing filter options (apply_facet): group, label, count, url, outcome",
    )
    unexplored_action_kinds: list[str] = Field(
        default_factory=list,
        description="Allowed by policy but not implemented yet; discovery cannot close",
    )
    error: str | None = None

    @property
    def unvisited(self) -> list[ControlRecord]:
        """Controls that stayed unvisited (each one is a gap)."""
        return [c for c in self.controls if c.status == "unvisited"]

    @property
    def controls_closed(self) -> bool:
        """Every discovered control reached a terminal, non-gap outcome."""
        return self.error is None and not self.unvisited

    @property
    def discovery_closed(self) -> bool:
        """Controls closed and no allowed action kind was left unimplemented."""
        return self.controls_closed and not self.unexplored_action_kinds

    def counts(self) -> dict[str, int]:
        """Control outcomes, for summaries."""
        out: dict[str, int] = {}
        for c in self.controls:
            out[c.status] = out.get(c.status, 0) + 1
        return out


def _label_key(label: str) -> str:
    return " ".join(label.casefold().split())


def mark_responsive_duplicates(controls: list[ControlRecord]) -> int:
    """Exclude never-visible controls that duplicate a visited one; return how many.

    Responsive pages ship the same tabs or accordions twice (a mobile and a
    desktop copy) and hide one with CSS, so the hidden copy can never be
    clicked. A `never_visible` control is excluded as
    `responsive_duplicate_of:<control_id>` when a control of the same kind with
    the same normalized, non-empty label was visited or was open from the start.
    Without such a twin it stays an unvisited gap.

    Assumption: a twin with the same kind and label reveals the same content.
    A mobile copy can share a label and differ in its panel; its text is still
    extracted as hidden-layer blocks, so nothing is lost, but that panel is not
    separately verified as a visible state.
    """
    twins: dict[tuple[str, str], str] = {}
    for c in controls:
        key = _label_key(c.label)
        if key and c.status in ("visited", "initially_open"):
            twins.setdefault((c.kind, key), c.control_id)
    marked = 0
    for c in controls:
        if c.status != "unvisited" or c.reason != "never_visible":
            continue
        twin = twins.get((c.kind, _label_key(c.label)))
        if twin is not None:
            c.status, c.reason = "excluded", f"responsive_duplicate_of:{twin}"
            marked += 1
    return marked
