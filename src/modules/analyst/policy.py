"""What may go to Claude, and what it may cost.

Data policy (decision pending with the product owner, see
docs/app/ARCHITECTURE_PLATFORM_MODULES_AND_CLAUDE.md section 5.4): every project
has a data class, `public` by default (scraped public websites). A project marked
`client_confidential` is never sent to Claude; its cards can still export a
context pack for a person to use as the client's agreement allows.

Spend follows the paid-scraping pattern: Claude is off unless
`CP_CLAUDE_ENABLED=true`, a monthly cap must be set (0 means unset and refuses),
and every call is checked before it is made against the smallest of the run,
daily and monthly caps, using the worst case of that call (all input at the full
price plus the largest output allowed). The output limit of a call is lowered
to what the remaining budget can pay for, so no cap can be crossed. Actual cost
is written to an append-only log after each call.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal

from pydantic import Field

from src.core.schemas import StrictModel

__all__ = [
    "PRICES",
    "Budget",
    "DataClass",
    "DataPolicy",
    "Price",
    "SpendLog",
    "UsageCost",
    "cost_of",
    "price_for",
]

DataClass = Literal["public", "client_confidential"]

_LOCK = threading.Lock()


class Price(StrictModel):
    """US dollars per million tokens."""

    input: float
    output: float
    cache_read: float
    cache_write: float


def _p(input_: float, output: float, cache_read: float) -> Price:
    return Price(input=input_, output=output, cache_read=cache_read, cache_write=input_ * 1.25)


# First-party Claude API list prices (claude-api reference, cached 2026-10-06).
PRICES: Final[dict[str, Price]] = {
    "claude-fable-5-1": _p(10.0, 50.0, 0.25),
    "claude-fable-5": _p(10.0, 50.0, 1.0),
    "claude-opus-5-5": _p(4.0, 20.0, 0.20),
    "claude-opus-5": _p(5.0, 25.0, 0.50),
    "claude-opus-4-8": _p(5.0, 25.0, 0.50),
    "claude-sonnet-5-5": _p(2.0, 10.0, 0.20),
    "claude-sonnet-5": _p(2.0, 10.0, 0.20),
    "claude-haiku-5-5": _p(0.10, 0.50, 0.01),
}
_MOST_EXPENSIVE: Final = max(PRICES.values(), key=lambda p: p.output)


def price_for(model: str) -> Price:
    """The model's price; an unknown model is charged at the most expensive known price."""
    return PRICES.get(model, _MOST_EXPENSIVE)


class UsageCost(StrictModel):
    """Tokens of one call and what they cost."""

    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    usd: float = 0.0


def cost_of(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
) -> UsageCost:
    """The cost of one call's usage."""
    price = price_for(model)
    usd = (
        input_tokens * price.input
        + output_tokens * price.output
        + cache_read_input_tokens * price.cache_read
        + cache_creation_input_tokens * price.cache_write
    ) / 1_000_000
    return UsageCost(
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        usd=round(usd, 6),
    )


class SpendEntry(StrictModel):
    """One Claude call, as written to the spend log."""

    at: datetime
    run_id: str
    card_id: str
    model: str
    usd: float = Field(ge=0.0)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


class SpendLog:
    """Append-only JSON lines of every Claude call (`<root>/spend.jsonl`)."""

    def __init__(self, root: Path) -> None:
        """Bind to the analyst folder."""
        self.path = root / "spend.jsonl"

    def charge(self, entry: SpendEntry) -> None:
        """Append one call."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK, self.path.open("a", encoding="utf-8") as handle:
            handle.write(entry.model_dump_json() + "\n")

    def entries(self) -> list[SpendEntry]:
        """Every call so far."""
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as handle:
            return [SpendEntry.model_validate_json(line) for line in handle if line.strip()]

    def spent(self, *, since: datetime) -> float:
        """Dollars spent since a moment."""
        return round(sum(e.usd for e in self.entries() if e.at >= since), 6)


class Budget:
    """The money one run may spend: the smallest of its run, daily and monthly room."""

    def __init__(
        self,
        log: SpendLog,
        *,
        enabled: bool,
        run_cap: float,
        daily_cap: float,
        monthly_cap: float,
        now: datetime | None = None,
    ) -> None:
        """Read today's and this month's spend once, at the start of the run."""
        moment = now or datetime.now(UTC)
        day = moment.replace(hour=0, minute=0, second=0, microsecond=0)
        month = day.replace(day=1)
        self.enabled = enabled
        self.run_cap = run_cap
        self.daily_left = max(0.0, daily_cap - log.spent(since=day))
        self.monthly_left = max(0.0, monthly_cap - log.spent(since=month)) if monthly_cap else 0.0
        self.monthly_cap = monthly_cap
        self.spent = 0.0

    @property
    def cap(self) -> float:
        """What this run may spend in total."""
        return round(min(self.run_cap, self.daily_left, self.monthly_left), 6)

    @property
    def left(self) -> float:
        """What remains for this run."""
        return max(0.0, self.cap - self.spent)

    def refusal(self) -> str | None:
        """Why a run may not start, or None."""
        if not self.enabled:
            return (
                "Claude is switched off: confirm the data policy, then set CP_CLAUDE_ENABLED=true "
                "and a monthly cap (CP_CLAUDE_MONTHLY_CAP_USD) in .env"
            )
        if not self.monthly_cap:
            return "no monthly cap is set for Claude (CP_CLAUDE_MONTHLY_CAP_USD=0 means unset)"
        if self.cap <= 0:
            return "the Claude daily or monthly cap is used up"
        return None

    def max_output_tokens(self, model: str, input_tokens_estimate: int, wanted: int) -> int:
        """The largest output the remaining budget can pay for, counting input at full price."""
        price = price_for(model)
        input_usd = input_tokens_estimate * price.input / 1_000_000
        room = self.left - input_usd
        if room <= 0:
            return 0
        return max(0, min(wanted, int(room * 1_000_000 / price.output)))

    def add(self, usd: float) -> None:
        """Count a call's actual cost."""
        self.spent = round(self.spent + usd, 6)


class _PolicyFile(StrictModel):
    projects: dict[str, DataClass] = Field(default_factory=dict)


class DataPolicy:
    """Each project's data class (`<root>/data_policy.json`); `public` unless set."""

    def __init__(self, root: Path) -> None:
        """Bind to the analyst folder."""
        self.path = root / "data_policy.json"

    def _read(self) -> _PolicyFile:
        if not self.path.exists():
            return _PolicyFile()
        return _PolicyFile.model_validate_json(self.path.read_text(encoding="utf-8"))

    def data_class(self, project_id: str) -> DataClass:
        """The project's data class."""
        return self._read().projects.get(project_id, "public")

    def set(self, project_id: str, data_class: DataClass) -> None:
        """Set a project's data class."""
        policy = self._read()
        policy.projects[project_id] = data_class
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            self.path.write_text(
                json.dumps(policy.model_dump(), indent=2, sort_keys=True), encoding="utf-8"
            )

    def blocked(self, project_ids: list[str]) -> list[str]:
        """The projects that must not be sent to Claude."""
        return [p for p in project_ids if self.data_class(p) == "client_confidential"]
