import pytest

from contrail.models import Span
from contrail.store import Store

MS = 1_000_000
BASE = 1_757_000_000_000_000_000


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


def span(span_id, trace="t1", parent=None, start=0, end=100, **attrs):
    return Span(
        trace_id=trace,
        span_id=span_id,
        parent_span_id=parent,
        name="claude_code.tool",
        kind=1,
        start_ns=BASE + start * MS,
        end_ns=BASE + end * MS,
        attributes=attrs,
    )


def test_add_and_read_back(store):
    assert store.add_spans([span("a")]) == 1
    spans = store.spans_for("t1")
    assert len(spans) == 1 and spans[0].span_id == "a"


def test_adding_nothing_is_safe(store):
    assert store.add_spans([]) == 0
    assert store.runs() == []


def test_run_summary_is_materialised(store):
    store.add_spans([span("a", start=0, end=200), span("b", parent="a", start=10, end=90)])
    run = store.run("t1")
    assert run is not None
    assert run["span_count"] == 2
    assert run["duration_ms"] == 200


def test_reexported_span_is_upserted_not_duplicated(store):
    """OTLP delivery is at-least-once, so the same span can arrive twice."""
    store.add_spans([span("a", end=100)])
    store.add_spans([span("a", end=300)])
    assert store.counts()["spans"] == 1
    assert store.run("t1")["duration_ms"] == 300


def test_attributes_survive_a_round_trip(store):
    store.add_spans([span("a", **{"tool.name": "Bash", "nested": {"k": [1, 2]}})])
    s = store.spans_for("t1")[0]
    assert s.tool_name == "Bash"
    assert s.attributes["nested"] == {"k": [1, 2]}


def test_error_count(store):
    bad = span("b")
    bad.status_code = 2
    store.add_spans([span("a"), bad])
    assert store.run("t1")["error_count"] == 1


def test_token_aggregation_keeps_cache_separate(store):
    store.add_spans([
        span("a", **{"gen_ai.usage.input_tokens": 100,
                     "gen_ai.usage.cache_read_input_tokens": 900}),
        span("b", **{"gen_ai.usage.input_tokens": 50}),
    ])
    run = store.run("t1")
    assert run["input_tokens"] == 150
    assert run["cache_read_tokens"] == 900


def test_runs_are_listed_newest_first(store):
    store.add_spans([span("a", trace="old", start=0, end=10)])
    store.add_spans([span("b", trace="new", start=5000, end=5010)])
    assert [r["trace_id"] for r in store.runs()] == ["new", "old"]


def test_traces_are_kept_apart(store):
    store.add_spans([span("a", trace="t1"), span("b", trace="t2")])
    assert store.counts()["runs"] == 2
    assert len(store.spans_for("t1")) == 1


def test_unknown_run_is_none(store):
    assert store.run("nope") is None
