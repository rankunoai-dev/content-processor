"""The Claude tool loop for one card run (manual loop, so every call is budgeted).

Per call: estimate the input, lower the output limit to what the budget can pay
for, stream the reply, charge the actual cost, then either run the requested
tools (all results back in one message) or finish. When the budget or the tool-
call limit gets close, the next call is told to stop using tools and write the
deliverable from what it has (`tool_choice: none`), so a run ends with a
deliverable rather than being cut off.

Claude Opus 5.5 always thinks; thinking blocks are passed back unchanged with
the rest of each reply. Server-side refusal fallbacks are on (`fallbacks:
"default"`), so a declined request is retried on a fallback model in the same
call; a refusal of the whole chain ends the run with its category.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import anthropic

from src.modules.analyst.policy import Budget, SpendEntry, SpendLog, cost_of

__all__ = ["AgentError", "AgentResult", "ClaudeAgent", "FALLBACK_BETA"]

FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_OUTPUT_TOKENS = 32_000
MIN_OUTPUT_TOKENS = 4_000
FINAL_RESERVE_TOKENS = 16_000
_CHARS_PER_TOKEN = 3.0
_STOP_TOOLS = (
    "Stop calling tools now: this run's budget or tool-call limit is nearly reached. Write the "
    "complete deliverable from the data you already have, following the format, and say in "
    '"Limits of this analysis" what you could not check.'
)


class AgentError(RuntimeError):
    """The run could not finish (credentials, refusal, API failure, budget)."""


@dataclass
class AgentResult:
    """What the loop produced."""

    text: str
    model: str
    turns: int = 0
    tool_calls: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    cost_usd: float = 0.0
    stop: str = ""
    truncated: bool = False


Execute = Callable[[str, Any], tuple[str, bool]]
Emit = Callable[[dict[str, Any]], None]


def _api_error(exc: anthropic.APIError) -> str:
    """A plain message for an API failure (most specific class first)."""
    if isinstance(exc, anthropic.AuthenticationError):
        return "Claude rejected the credentials: set ANTHROPIC_API_KEY or run `ant auth login`"
    if isinstance(exc, anthropic.PermissionDeniedError):
        return "this API key may not use the requested model or feature"
    if isinstance(exc, anthropic.NotFoundError):
        return "the model was not found: check CP_CLAUDE_MODEL"
    if isinstance(exc, anthropic.RateLimitError):
        return "Claude's rate limit was reached and retries ran out; try again in a minute"
    if isinstance(exc, anthropic.BadRequestError):
        return f"Claude refused the request as invalid: {exc.message[:300]}"
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code >= 500:
            return f"Claude is unavailable or overloaded ({exc.status_code}); try again later"
        return f"Claude API error {exc.status_code}: {exc.message[:300]}"
    if isinstance(exc, anthropic.APITimeoutError):
        return "the request to Claude timed out"
    if isinstance(exc, anthropic.APIConnectionError):
        return "could not reach Claude (network)"
    return f"Claude API error: {exc}"[:300]


class ClaudeAgent:
    """Runs one card's conversation with Claude within its budget."""

    def __init__(
        self,
        client: anthropic.Anthropic,
        *,
        model: str,
        effort: str,
        budget: Budget,
        spend: SpendLog,
        run_id: str,
        card_id: str,
        max_tool_calls: int,
        fallbacks: bool = True,
        emit: Emit | None = None,
    ) -> None:
        """Bind to a client, a model and a run's budget."""
        self.client = client
        self.model = model
        self.effort = effort
        self.budget = budget
        self.spend = spend
        self.run_id = run_id
        self.card_id = card_id
        self.max_tool_calls = max_tool_calls
        self.fallbacks = fallbacks
        self.emit = emit or (lambda _event: None)

    def _charge(self, response: Any, result: AgentResult) -> None:
        usage = response.usage
        cost = cost_of(
            str(response.model or self.model),
            input_tokens=int(usage.input_tokens or 0),
            output_tokens=int(usage.output_tokens or 0),
            cache_read_input_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
            cache_creation_input_tokens=int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
        )
        self.budget.add(cost.usd)
        result.cost_usd = round(result.cost_usd + cost.usd, 6)
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        ):
            result.usage[key] = result.usage.get(key, 0) + int(getattr(cost, key))
        result.model = cost.model
        self.spend.charge(
            SpendEntry(
                at=datetime.now(UTC),
                run_id=self.run_id,
                card_id=self.card_id,
                model=cost.model,
                usd=cost.usd,
                input_tokens=cost.input_tokens,
                output_tokens=cost.output_tokens,
                cache_read_input_tokens=cost.cache_read_input_tokens,
                cache_creation_input_tokens=cost.cache_creation_input_tokens,
            )
        )
        self.emit({"type": "cost", "usd": result.cost_usd, "turn": result.turns})

    def _request(
        self,
        system: str,
        tools: list[dict[str, Any]],
        messages: list[Any],
        max_tokens: int,
        final: bool,
    ) -> Any:
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": [{"type": "text", "text": system}],
            "messages": messages,
            "tools": tools,
            "tool_choice": {"type": "none"} if final else {"type": "auto"},
            "output_config": {"effort": self.effort},
            "cache_control": {"type": "ephemeral"},
        }
        if self.fallbacks:
            params["betas"] = [FALLBACK_BETA]
            params["fallbacks"] = "default"
        for attempt in range(2):
            try:
                with self.client.beta.messages.stream(**params) as stream:
                    for text in stream.text_stream:
                        self.emit({"type": "text", "text": text})
                    return stream.get_final_message()
            except anthropic.APIError as exc:
                raise AgentError(_api_error(exc)) from exc
            except ValueError as exc:
                # A tool input Claude streamed could not be parsed at all: ask again once.
                if attempt == 1:
                    msg = f"Claude sent a tool request that could not be read twice: {exc}"
                    raise AgentError(msg) from exc
        raise AssertionError("unreachable")  # pragma: no cover

    def run(
        self, system: str, first_message: str, tools: list[dict[str, Any]], execute: Execute
    ) -> AgentResult:
        """The conversation until Claude writes the deliverable (or a limit stops it)."""
        refusal = self.budget.refusal()
        if refusal:
            raise AgentError(refusal)
        result = AgentResult(text="", model=self.model)
        messages: list[Any] = [{"role": "user", "content": first_message}]
        estimate = int(
            (len(system) + len(json.dumps(tools)) + len(first_message)) / _CHARS_PER_TOKEN
        )
        final = False
        while True:
            wanted = MAX_OUTPUT_TOKENS
            max_tokens = self.budget.max_output_tokens(self.model, estimate, wanted)
            if max_tokens < MIN_OUTPUT_TOKENS:
                msg = (
                    f"the budget left for this run (${self.budget.left:.2f} of "
                    f"${self.budget.cap:.2f}) cannot pay for another Claude call"
                )
                if result.text:
                    result.truncated = True
                    result.stop = "budget"
                    return result
                raise AgentError(msg)
            response = self._request(system, tools, messages, max_tokens, final)
            result.turns += 1
            self._charge(response, result)
            usage = response.usage
            estimate = int(
                (usage.input_tokens or 0)
                + (getattr(usage, "cache_read_input_tokens", 0) or 0)
                + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
                + (usage.output_tokens or 0)
            )
            stop = response.stop_reason
            texts = [b.text for b in response.content if getattr(b, "type", "") == "text"]
            if stop == "refusal":
                details = getattr(response, "stop_details", None)
                category = getattr(details, "category", None) if details else None
                msg = f"Claude declined this request (category: {category or 'unspecified'})"
                raise AgentError(msg)
            if stop == "pause_turn":
                messages.append({"role": "assistant", "content": response.content})
                continue
            tool_uses = [b for b in response.content if getattr(b, "type", "") == "tool_use"]
            if stop == "tool_use" and tool_uses:
                messages.append({"role": "assistant", "content": response.content})
                results: list[dict[str, Any]] = []
                added_chars = 0
                for block in tool_uses:
                    if result.tool_calls >= self.max_tool_calls:
                        content, is_error = (
                            json.dumps({"error": "tool-call limit reached for this run"}),
                            True,
                        )
                    else:
                        result.tool_calls += 1
                        content, is_error = execute(block.name, block.input)
                    self.emit(
                        {
                            "type": "tool",
                            "name": block.name,
                            "input": block.input,
                            "error": is_error,
                        }
                    )
                    added_chars += len(content)
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": content,
                            "is_error": is_error,
                        }
                    )
                estimate += int(added_chars / _CHARS_PER_TOKEN)
                reserve = self.budget.max_output_tokens(
                    self.model, estimate, FINAL_RESERVE_TOKENS * 2
                )
                if result.tool_calls >= self.max_tool_calls or reserve < FINAL_RESERVE_TOKENS * 2:
                    final = True
                    results.append({"type": "text", "text": _STOP_TOOLS})
                messages.append({"role": "user", "content": results})
                continue
            result.text = "\n".join(texts).strip()
            result.stop = str(stop)
            if stop == "max_tokens":
                result.truncated = True
            if not result.text:
                msg = f"Claude finished without writing a deliverable (stop: {stop})"
                raise AgentError(msg)
            return result
