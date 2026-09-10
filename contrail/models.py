"""Core data shapes for Contrail.

A Span is one unit of work inside an agent run. A Run is everything that
shares a trace id.

Claude Code, the OpenTelemetry GenAI conventions and OpenInference all name
the same facts differently, so every attribute we care about is looked up
through an alias list rather than a single hard-coded key. That keeps the
store stable when a schema shifts underneath us -- see docs/spec.md.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Any

NS_PER_MS = 1_000_000

# Attribute aliases, most-preferred first. OpenInference names come first
# because they are the more stable of the two conventions today; the OTel
# gen_ai.* names are still not stable as of 2026.
#
# The bare names (`input_tokens`, `tool_name`, ...) come last but are not
# optional: they are what Claude Code actually emits today, and for the token
# counts they are the *only* names present on a real span. Verified against
# live export at service.version 2.1.266 -- without them every token count on
# a real span reads zero, which is silent and would have surfaced as Phase 3
# costing every run at $0. They rank last so that an explicit convention
# attribute still wins if one is ever emitted alongside.
ALIASES: dict[str, tuple[str, ...]] = {
    "session_id": (
        "session.id",
        "claude_code.session.id",
        "gen_ai.conversation.id",
    ),
    "model": (
        "llm.model_name",
        "gen_ai.request.model",
        "gen_ai.response.model",
        "claude_code.model",
    ),
    "tool_name": (
        "tool.name",
        "gen_ai.tool.name",
        "claude_code.tool.name",
        "tool_name",
    ),
    "agent_type": (
        "agent.name",
        "gen_ai.agent.name",
        "claude_code.agent.type",
    ),
    "input_tokens": (
        "llm.token_count.prompt",
        "gen_ai.usage.input_tokens",
        "input_tokens",
    ),
    "output_tokens": (
        "llm.token_count.completion",
        "gen_ai.usage.output_tokens",
        "output_tokens",
    ),
    "cache_read_tokens": (
        "gen_ai.usage.cache_read_input_tokens",
        "llm.token_count.cache_read",
        "cache_read_tokens",
    ),
    "cache_creation_tokens": (
        "gen_ai.usage.cache_creation_input_tokens",
        "llm.token_count.cache_write",
        "cache_creation_tokens",
    ),
}


def pick(attributes: dict[str, Any], key: str) -> Any:
    """Return the first alias of `key` present in `attributes`, else None."""
    for alias in ALIASES.get(key, ()):
        if alias in attributes and attributes[alias] is not None:
            return attributes[alias]
    return None


def pick_int(attributes: dict[str, Any], key: str) -> int:
    value = pick(attributes, key)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


@dataclass
class Span:
    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    kind: int
    start_ns: int
    end_ns: int
    status_code: int = 0
    status_message: str = ""
    service_name: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)

    # --- derived, denormalised so queries stay simple -----------------
    session_id: str | None = None
    model: str | None = None
    tool_name: str | None = None
    agent_type: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

    def __post_init__(self) -> None:
        a = self.attributes
        self.session_id = self.session_id or pick(a, "session_id")
        self.model = self.model or pick(a, "model")
        self.agent_type = self.agent_type or pick(a, "agent_type")
        self.tool_name = self.tool_name or pick(a, "tool_name") or self._tool_from_name()
        self.input_tokens = self.input_tokens or pick_int(a, "input_tokens")
        self.output_tokens = self.output_tokens or pick_int(a, "output_tokens")
        self.cache_read_tokens = self.cache_read_tokens or pick_int(a, "cache_read_tokens")
        self.cache_creation_tokens = (
            self.cache_creation_tokens or pick_int(a, "cache_creation_tokens")
        )

    def _tool_from_name(self) -> str | None:
        """`claude_code.tool.execution` style span names carry no tool.name."""
        if self.name.startswith("claude_code.tool"):
            return self.attributes.get("claude_code.tool") or None
        return None

    @property
    def duration_ms(self) -> float:
        return max(0.0, (self.end_ns - self.start_ns) / NS_PER_MS)

    @property
    def is_error(self) -> bool:
        # OTel StatusCode: 0 UNSET, 1 OK, 2 ERROR
        return self.status_code == 2

    def to_row(self) -> dict[str, Any]:
        row = asdict(self)
        row["attributes"] = json.dumps(self.attributes, default=str)
        row["duration_ms"] = self.duration_ms
        return row


@dataclass
class Run:
    """Everything sharing a trace id. Phase 1 keeps this flat.

    Phase 2 adds the reconstructed subagent tree; Phase 3 adds cost.
    """

    trace_id: str
    root_name: str
    session_id: str | None
    service_name: str
    start_ns: int
    end_ns: int
    span_count: int
    error_count: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int

    @property
    def duration_ms(self) -> float:
        return max(0.0, (self.end_ns - self.start_ns) / NS_PER_MS)

    @classmethod
    def from_spans(cls, spans: list[Span]) -> "Run":
        if not spans:
            raise ValueError("cannot build a Run from zero spans")

        ids = {s.span_id for s in spans}
        roots = [s for s in spans if not s.parent_span_id or s.parent_span_id not in ids]
        root = min(roots or spans, key=lambda s: s.start_ns)

        return cls(
            trace_id=spans[0].trace_id,
            root_name=root.name,
            session_id=next((s.session_id for s in spans if s.session_id), None),
            service_name=next((s.service_name for s in spans if s.service_name), ""),
            start_ns=min(s.start_ns for s in spans),
            end_ns=max(s.end_ns for s in spans),
            span_count=len(spans),
            error_count=sum(1 for s in spans if s.is_error),
            input_tokens=sum(s.input_tokens for s in spans),
            output_tokens=sum(s.output_tokens for s in spans),
            cache_read_tokens=sum(s.cache_read_tokens for s in spans),
            cache_creation_tokens=sum(s.cache_creation_tokens for s in spans),
        )
