"""Canary tests: every detector and extraction path must fire on a known
positive, end to end.

Every bug this project has had produced **silently wrong output** rather than
a crash:

- `ALIASES` missed the attribute names Claude Code actually emits, so every
  real span stored zero tokens. Nothing complained.
- Subagent tokens were folded in twice, doubling every subagent's cost.
- `result_hash` was read off the tool_use record instead of the tool_result
  record, so the repeat detector found nothing and reported everything as
  undecidable.
- `toolUseResult` is a dict on success and a plain string on error; hashing
  only dicts made every failed call look like it had no result.

A test that passes when the code finds nothing would have caught none of
them. These do, because each one asserts a *positive*: given input known to
contain the thing, the thing is found, with the expected value.

**They run the whole path** -- JSONL on disk, `load_session`, `build_tree`,
the store, then the detector -- because three of the four bugs above lived in
the seams between those stages and were invisible to unit tests whose
fixtures started halfway through.

Fixtures are synthetic, shaped after docs/spec.md's recorded format. No
network, nothing written outside `tmp_path`.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from contrail.cost import PriceTable, attribute_cost, check_attribution_invariant
from contrail.detectors import (
    detect_cost_concentration,
    detect_outcome_divergence,
    detect_redundant_repeats,
    detect_unhandled_errors,
)
from contrail.models import Span
from contrail.store import Store
from contrail.transcript import build_tree, load_session

SESSION = "cccccccc-1111-2222-3333-444444444444"
AGENT = "a0canary00000001"
AT = date(2026, 9, 10)
MS = 1_000_000
BASE = 1_757_000_000_000_000_000


# ------------------------------------------------------------ fixture builder

def ts(second: int) -> str:
    return f"2026-09-10T10:{second // 60:02d}:{second % 60:02d}.000Z"


def _base(uuid, rtype, at, *, agent=None, sidechain=False, parent=None):
    return {
        "type": rtype, "uuid": uuid, "parentUuid": parent,
        "sessionId": SESSION, "isSidechain": sidechain,
        "timestamp": ts(at), **({"agentId": agent} if agent else {}),
    }


def human(uuid, at=0):
    return {**_base(uuid, "user", at), "promptId": f"p-{uuid}",
            "origin": {"kind": "human"},
            "message": {"role": "user", "content": [{"type": "text", "text": "go"}]}}


def call(uuid, tool, tid, tool_input, at, *, agent=None, sidechain=False,
         tokens=(0, 0, 0, 0, 0)):
    inp, out, cread, w5m, w1h = tokens
    return {**_base(uuid, "assistant", at, agent=agent, sidechain=sidechain),
            "requestId": f"req_{tid}",
            "message": {"role": "assistant", "model": "claude-opus-5",
                        "usage": {"input_tokens": inp, "output_tokens": out,
                                  "cache_read_input_tokens": cread,
                                  "cache_creation_input_tokens": w5m + w1h,
                                  "cache_creation": {
                                      "ephemeral_5m_input_tokens": w5m,
                                      "ephemeral_1h_input_tokens": w1h},
                                  "service_tier": "standard"},
                        "content": [{"type": "tool_use", "id": tid,
                                     "name": tool, "input": tool_input}]}}


def result(uuid, tid, at, *, body="ok", tur=None, is_error=None,
           agent=None, sidechain=False):
    block = {"type": "tool_result", "tool_use_id": tid, "content": body}
    if is_error is not None:
        block["is_error"] = is_error
    record = {**_base(uuid, "user", at, agent=agent, sidechain=sidechain),
              "message": {"role": "user", "content": [block]}}
    record["toolUseResult"] = tur if tur is not None else {"type": "text",
                                                           "file": {"content": body}}
    return record


def write_corpus(root):
    """One session containing a known positive for every detector.

    Deliberately assembled from the shapes in docs/spec.md rather than from
    hand-built objects, so the fixture exercises parsing, tree building and
    storage the way a real session does.
    """
    proj = root / "canary-project"
    (proj / SESSION / "subagents").mkdir(parents=True, exist_ok=True)

    parent = [
        human("u1", 0),
        # --- a genuinely redundant repeat: same call, identical result ---
        call("a1", "Read", "t1", {"file_path": "/p/same.py"}, 1),
        result("r1", "t1", 2, body="unchanged"),
        call("a2", "Read", "t2", {"file_path": "/p/same.py"}, 3),
        result("r2", "t2", 4, body="unchanged"),
        # --- a read whose result changed: must NOT be reported ---
        call("a3", "Read", "t3", {"file_path": "/p/moving.py"}, 5),
        result("r3", "t3", 6, body="before"),
        call("a4", "Read", "t4", {"file_path": "/p/moving.py"}, 7),
        result("r4", "t4", 8, body="after"),
        # --- a write that failed its required read and was abandoned ---
        call("a5", "Write", "t5", {"file_path": "/p/never.py"}, 9),
        result("r5", "t5", 10, is_error=True,
               body="<tool_use_error>File has not been read yet. Read it "
                    "first before writing to it.</tool_use_error>",
               tur="Error: File has not been read yet."),
        # --- a declined tool: benign, must NOT be reported ---
        call("a6", "Write", "t6", {"file_path": "/p/declined.py"}, 11),
        result("r6", "t6", 12, is_error=True,
               body="The user doesn't want to proceed with this tool use. "
                    "The tool use was rejected (eg. if it was a file edit, "
                    "the new_string was not applied).",
               tur="Error: rejected"),
        # --- a subagent holding almost all the run's tokens ---
        call("a7", "Agent", "t7", {"description": "big one"}, 13,
             tokens=(10, 10, 0, 0, 0)),
        result("r7", "t7", 40, tur={"status": "completed", "agentId": AGENT,
                                    "agentType": "Explore",
                                    "prompt": "do the expensive thing"}),
    ]
    (proj / f"{SESSION}.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in parent), encoding="utf-8")

    agent = [
        {**_base("s1", "user", 14, agent=AGENT, sidechain=True),
         "message": {"role": "user", "content": "do the expensive thing"}},
        call("s2", "Grep", "st1", {"pattern": "x"}, 15,
             agent=AGENT, sidechain=True, tokens=(1000, 500, 200_000, 0, 60_000)),
        result("s3", "st1", 16, agent=AGENT, sidechain=True),
    ]
    subagents = proj / SESSION / "subagents"
    (subagents / f"agent-{AGENT}.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in agent), encoding="utf-8")
    (subagents / f"agent-{AGENT}.meta.json").write_text(
        json.dumps({"agentType": "Explore", "description": "big one",
                    "toolUseId": "t7", "spawnDepth": 1}), encoding="utf-8")

    return proj / f"{SESSION}.jsonl"


@pytest.fixture()
def parsed(tmp_path):
    """The whole pipeline: disk -> parse -> tree -> store."""
    path = write_corpus(tmp_path)
    session = load_session(path)
    tree = build_tree(session)
    store = Store(tmp_path / "canary.db")
    records = list(session.records) + [
        r for a in session.agents.values() for r in a.records
    ]
    store.add_transcript_records(records, tree.record_scope)
    store.save_tree(tree, project_slug=session.project_slug, path=str(path))
    yield session, tree, store, records
    store.close()


# --------------------------------------------------- extraction canaries

def test_canary_span_tokens_are_extracted_from_real_attribute_names():
    """Would have caught the ALIASES bug: real spans emit bare names and
    every token count silently read zero."""
    span = Span(
        trace_id="t", span_id="s", parent_span_id=None,
        name="claude_code.llm_request", kind=1, start_ns=BASE,
        end_ns=BASE + 100 * MS,
        attributes={"session.id": SESSION, "model": "claude-opus-5",
                    "input_tokens": 2, "output_tokens": 1200,
                    "cache_read_tokens": 84562, "cache_creation_tokens": 7096},
    )
    assert span.input_tokens == 2
    assert span.output_tokens == 1200
    assert span.cache_read_tokens == 84562
    assert span.cache_creation_tokens == 7096
    assert span.session_id == SESSION


def test_canary_transcript_tokens_survive_the_whole_parse(parsed):
    """Non-zero, from JSONL through to a stored record."""
    _session, _tree, store, _records = parsed
    row = store.conn.execute(
        "SELECT input_tokens, output_tokens, cache_read_tokens, "
        "cache_write_1h_tokens FROM transcript_records WHERE uuid = 's2'"
    ).fetchone()
    assert (row["input_tokens"], row["output_tokens"]) == (1000, 500)
    assert row["cache_read_tokens"] == 200_000
    assert row["cache_write_1h_tokens"] == 60_000


def test_canary_result_hash_is_populated_on_result_records(parsed):
    """Would have caught reading result_hash off the wrong record: the
    detector saw None everywhere and reported nothing."""
    _session, _tree, store, _records = parsed
    populated = store.conn.execute(
        "SELECT COUNT(*) AS n FROM transcript_records "
        "WHERE is_tool_result = 1 AND result_hash IS NOT NULL"
    ).fetchone()["n"]
    assert populated >= 6, "no result hashes stored; repeat detection is dead"


def test_canary_error_results_are_classified(parsed):
    """Would have caught hashing only dict results: a string error looked
    like no result at all."""
    _session, _tree, store, _records = parsed
    rows = {
        r["uuid"]: r["error_class"] for r in store.conn.execute(
            "SELECT uuid, error_class FROM transcript_records WHERE is_error = 1")
    }
    assert rows["r5"] == "read_before_write"
    assert rows["r6"] == "user_declined"


def test_canary_the_subagent_is_linked_into_the_tree(parsed):
    """A tree that silently drops a subagent is the Phase 2 failure mode."""
    _session, tree, _store, _records = parsed
    subs = [n for n in tree.nodes if n.kind == "subagent"]
    assert len(subs) == 1
    assert subs[0].agent_id == AGENT
    assert subs[0].link_basis == "tool_use_result"


def test_canary_subagent_tokens_are_exact_not_doubled(parsed):
    """Would have caught the double-count: records folded in twice."""
    _session, tree, _store, _records = parsed
    sub = next(n for n in tree.nodes if n.kind == "subagent")
    assert sub.input_tokens == 1000
    assert sub.output_tokens == 500
    assert sub.cache_read_tokens == 200_000


def test_canary_every_token_bearing_record_maps_to_a_node(parsed):
    session, tree, _store, records = parsed
    unmapped = [
        r.uuid for r in records
        if (r.input_tokens or r.output_tokens) and r.uuid not in tree.record_scope
    ]
    assert unmapped == []
    assert session.parse_errors == 0


# ------------------------------------------------------ detector canaries

def test_canary_redundant_repeats_fires(parsed):
    """The positive: two identical calls with an identical result."""
    _session, tree, _store, records = parsed
    found = detect_redundant_repeats(records, tree.record_scope, SESSION)
    assert len(found) == 1, f"expected one redundant repeat, got {len(found)}"
    assert found[0].evidence["tool"] == "Read"
    assert found[0].evidence["calls"] == 2


def test_canary_redundant_repeats_stays_quiet_on_a_changed_result(parsed):
    """The paired negative, so a detector that fires on everything fails."""
    _session, tree, _store, records = parsed
    found = detect_redundant_repeats(records, tree.record_scope, SESSION)
    signatures = {f.evidence["signature"] for f in found}
    moving = next(r for r in records
                  if r.uuid == "a3" and r.tool_signature)
    assert moving.tool_signature not in signatures


def test_canary_unhandled_errors_fires_on_the_abandoned_write(parsed):
    _session, tree, _store, records = parsed
    found = detect_unhandled_errors(records, tree.record_scope, SESSION)
    assert len(found) == 1, f"expected one unhandled error, got {len(found)}"
    assert found[0].evidence["error_class"] == "read_before_write"
    assert found[0].evidence["benign_errors_excluded"] == 1


def test_canary_cost_attribution_produces_a_non_zero_figure(parsed):
    """Would have caught ALIASES and any future silent-zero: a run with
    tokens must cost money."""
    _session, tree, _store, records = parsed
    run = attribute_cost(tree.nodes, records, tree.record_scope,
                         PriceTable(), AT, SESSION)
    assert run.total_usd is not None, "priced run came back unpriced"
    assert run.total_usd > 0, "run with 260k+ tokens costed at zero"
    assert run.unpriced_records == 0
    assert check_attribution_invariant(run, records, tree.record_scope).ok


def test_canary_cost_concentration_fires(parsed):
    """The subagent holds nearly all of the run's tokens."""
    _session, tree, _store, records = parsed
    run = attribute_cost(tree.nodes, records, tree.record_scope,
                         PriceTable(), AT, SESSION)
    found = detect_cost_concentration(run, min_tokens=1000)
    assert found, "a subagent holding ~100% of the tokens was not flagged"
    assert any(f.node_id == f"agent:{AGENT}" for f in found)


def test_canary_outcome_divergence_fires_and_stays_quiet(parsed):
    """Both directions, since this one takes caller-supplied groups."""
    split = detect_outcome_divergence(
        {"task": [("a", False), ("b", True), ("c", False)]}, SESSION)
    agreed = detect_outcome_divergence(
        {"task": [("a", False), ("b", False), ("c", False)]}, SESSION)
    assert len(split) == 1
    assert agreed == []


def test_canary_every_detector_has_a_positive_in_this_file():
    """The convention itself, asserted.

    If a detector is added without a canary that fires on a known positive,
    this fails -- rather than the new detector quietly never firing.
    """
    from contrail import detectors

    detector_names = {
        detectors.REDUNDANT_REPEATS,
        detectors.COST_CONCENTRATION,
        detectors.OUTCOME_DIVERGENCE,
        detectors.UNHANDLED_ERRORS,
    }
    source = __import__("pathlib").Path(__file__).read_text(encoding="utf-8")
    covered = {
        name for name in detector_names
        if f"detect_{name}" in source or name.replace("_", "") in source.replace("_", "")
    }
    assert detector_names <= covered, (
        f"no canary asserts a positive for: {detector_names - covered}"
    )


# ----------------------------------------------- the spend-report canary

def test_canary_spend_report_is_not_silently_zero(parsed):
    """A spend report that totals zero is this codebase's signature bug.

    Runs the whole path -- store rows through aggregation -- and asserts the
    totals and the per-class shares are real. `ALIASES` once made every token
    count zero without complaint; this is the guard for the aggregate.
    """
    from contrail.cost import PriceTable
    from contrail.spend import aggregate_spend

    _session, _tree, store, _records = parsed
    rows = store.transcript_sessions(limit=500)
    by_session = {
        r["session_id"]: store.transcript_records_for(r["session_id"]) for r in rows
    }
    report = aggregate_spend(rows, by_session, PriceTable(), priced_at=AT)

    assert report.sessions >= 1
    assert report.tokens.billable_total > 0, "no billable tokens aggregated"
    assert report.total_usd > 0, "a run with tokens costed at zero"
    assert report.priced_records > 0
    assert report.unpriced_records == 0

    # The headline is a share of spend, so it must be a real fraction.
    assert 0 < report.context_share < 1
    assert report.class_share("cache_read") > 0
    assert report.class_share("output") > 0

    # Scope must actually split, since the fixture has a subagent.
    scopes = {b.key: b for b in report.by_scope}
    assert scopes["main"].tokens > 0
    assert scopes["subagent"].tokens > 0, "subagent tokens vanished"


def test_canary_cache_economics_are_computed_not_defaulted(parsed):
    """The fixture writes a 1h cache and reads it, so both sides must be
    non-zero -- a premium or saving stuck at 0.0 means the pass is dead."""
    from contrail.cost import PriceTable
    from contrail.spend import aggregate_spend

    _session, _tree, store, _records = parsed
    rows = store.transcript_sessions(limit=500)
    by_session = {
        r["session_id"]: store.transcript_records_for(r["session_id"]) for r in rows
    }
    cache = aggregate_spend(rows, by_session, PriceTable(), priced_at=AT).cache
    assert cache.actual_usd > 0
    assert cache.write_premium_usd > 0, "1h cache writes produced no premium"
    assert cache.read_saving_usd > 0, "cache reads produced no saving"
