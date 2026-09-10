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
