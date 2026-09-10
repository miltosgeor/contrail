from contrail.models import Run, Span

MS = 1_000_000
BASE = 1_757_000_000_000_000_000


def span(span_id, parent=None, name="claude_code.tool", start=0, end=100, **attrs):
    return Span(
        trace_id="t1",
        span_id=span_id,
        parent_span_id=parent,
        name=name,
        kind=1,
        start_ns=BASE + start * MS,
        end_ns=BASE + end * MS,
        attributes=attrs,
    )


def test_duration_is_milliseconds():
    assert span("a", start=0, end=250).duration_ms == 250


def test_duration_never_negative():
    assert span("a", start=500, end=100).duration_ms == 0


def test_openinference_attributes_are_read():
    s = span("a", **{"llm.token_count.prompt": 120, "llm.model_name": "claude-opus-4"})
    assert s.input_tokens == 120
    assert s.model == "claude-opus-4"


def test_genai_attributes_are_read():
    s = span("a", **{"gen_ai.usage.output_tokens": 42, "gen_ai.request.model": "sonnet"})
    assert s.output_tokens == 42
    assert s.model == "sonnet"


def test_openinference_wins_when_both_present():
    """Both conventions get emitted; the more stable one should win."""
    s = span("a", **{"llm.model_name": "oi", "gen_ai.request.model": "genai"})
    assert s.model == "oi"


def test_missing_tokens_default_to_zero():
    s = span("a")
    assert (s.input_tokens, s.output_tokens, s.cache_read_tokens) == (0, 0, 0)


def test_non_numeric_tokens_do_not_crash():
    s = span("a", **{"gen_ai.usage.input_tokens": "not-a-number"})
    assert s.input_tokens == 0


def test_error_status():
    s = span("a")
    s.status_code = 2
    assert s.is_error


def test_run_picks_earliest_root():
    spans = [
        span("child", parent="root", start=10, end=20),
        span("root", start=0, end=50, name="claude_code.interaction"),
    ]
    run = Run.from_spans(spans)
    assert run.root_name == "claude_code.interaction"
    assert run.span_count == 2


def test_run_treats_orphan_parent_as_root():
    """A span whose parent was never exported is still a root."""
    spans = [span("only", parent="missing-parent", start=5, end=15)]
    run = Run.from_spans(spans)
    assert run.root_name == "claude_code.tool"


def test_run_aggregates_tokens_and_span_extent():
    spans = [
        span("a", start=0, end=100, **{"gen_ai.usage.input_tokens": 10}),
        span("b", parent="a", start=20, end=400, **{"gen_ai.usage.input_tokens": 5}),
    ]
    run = Run.from_spans(spans)
    assert run.input_tokens == 15
    assert run.duration_ms == 400


# --- Regression: the attribute names Claude Code actually emits ----------
#
# Phase 1 was verified against a real protobuf payload, which proved the
# transport but not the extraction. The fixtures above use OpenInference and
# gen_ai.* names; real spans emit bare names, and for token counts the bare
# name is the only one present. The result was a silent zero on every real
# span -- see docs/spec.md, "Trace-side join keys".

# Copied from a live export at service.version 2.1.266, identity attributes
# and resource noise removed.
REAL_LLM_REQUEST = {
    "session.id": "cec62d6f-60f8-43e8-a67b-133b2a138a83",
    "span.type": "llm_request",
    "model": "claude-opus-5",
    "gen_ai.system": "anthropic",
    "gen_ai.request.model": "claude-opus-5",
    "input_tokens": 2,
    "output_tokens": 1200,
    "cache_read_tokens": 84562,
    "cache_creation_tokens": 7096,
    "success": True,
    "stop_reason": "tool_use",
}

REAL_TOOL = {
    "session.id": "cec62d6f-60f8-43e8-a67b-133b2a138a83",
    "span.type": "tool",
    "tool_name": "Bash",
    "tool_use_id": "toolu_01FtRNK8XRAQVMTwbVegwQFv",
    "gen_ai.tool.call.id": "toolu_01FtRNK8XRAQVMTwbVegwQFv",
    "duration_ms": 1831,
}


def test_real_span_token_counts_are_not_silently_zero():
    s = span("a", name="claude_code.llm_request", **REAL_LLM_REQUEST)
    assert s.input_tokens == 2
    assert s.output_tokens == 1200
    assert s.cache_read_tokens == 84562
    assert s.cache_creation_tokens == 7096


def test_real_span_cache_tokens_stay_separate_from_input():
    """Cache reads price ~10% of input; summing them would hide the split."""
    s = span("a", name="claude_code.llm_request", **REAL_LLM_REQUEST)
    assert s.input_tokens == 2, "cache tokens must not be folded into input"
    assert s.cache_read_tokens != s.cache_creation_tokens


def test_real_tool_span_resolves_tool_name():
    s = span("a", name="claude_code.tool", **REAL_TOOL)
    assert s.tool_name == "Bash"


def test_real_span_resolves_session_id():
    s = span("a", name="claude_code.tool", **REAL_TOOL)
    assert s.session_id == "cec62d6f-60f8-43e8-a67b-133b2a138a83"


def test_explicit_convention_still_beats_the_bare_name():
    """Bare names rank last, so a real convention attribute keeps priority."""
    s = span("a", **{"gen_ai.usage.input_tokens": 99, "input_tokens": 1})
    assert s.input_tokens == 99
    s = span("b", **{"tool.name": "Read", "tool_name": "Bash"})
    assert s.tool_name == "Read"
