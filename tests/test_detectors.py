"""Tests for the Phase 4 detectors.

Fixtures are synthetic and structure-only. Where a number from the real
corpus appears in an assertion it is quoted with its sample size, because the
labelled set is small: **22 repeat groups (6 positive, 15 negative, 1
undecidable)** and **14 hand-labelled unhandled-error findings**. Nothing here
should be read as validation at scale.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import pytest

from contrail import detectors as D
from contrail.cost import PriceTable, Tokens, attribute_cost
from contrail.transcript import (
    ERROR_TEMPLATES,
    VOLATILE_RESULT_KEYS,
    background_task_id,
    classify_error,
    result_hash,
    target_hash,
    tool_signature,
)

AT = date(2026, 9, 10)


# ------------------------------------------------------------------ helpers

@dataclass
class Rec:
    """Shaped like a TranscriptRecord, with only the fields detectors read."""

    uuid: str
    ts_ns: int = 0
    tool_use_id: str | None = None
    tool_name: str | None = None
    tool_signature: str | None = None
    target_hash: str | None = None
    background_task_id: str | None = None
    result_hash: str | None = None
    is_tool_result: bool = False
    is_error: bool = False
    error_class: str | None = None
    model: str = "claude-opus-5"
    service_tier: str = "standard"
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0
    thinking_tokens: int = 0


def call(uuid, tool, tid, *, args=None, at=0, target=None, task=None):
    args = {"file_path": target} if target else (args or {"a": 1})
    return Rec(
        uuid=uuid, ts_ns=at, tool_use_id=tid, tool_name=tool,
        tool_signature=tool_signature(tool, args),
        target_hash=target_hash(args), background_task_id=task,
    )


def outcome(uuid, tid, *, rhash="h1", err=False, at=1, task=None):
    return Rec(uuid=uuid, ts_ns=at, tool_use_id=tid, is_tool_result=True,
               result_hash=rhash, is_error=err, background_task_id=task)


def scope(*uuids, node="turn:t1"):
    return {u: node for u in uuids}


# ---------------------------------------------- the volatile-key guard
#
# The exclusion list rotting is a silent regression: a new per-invocation key
# appears, two identical calls stop hashing the same, repeats quietly stop
# being detected and nothing says so. These are the cheap insurance.

IDENTICAL_BACKGROUND_READS = (
    # Two reads of the same background task's output, byte-identical content,
    # differing only in the per-invocation ids the harness stamped on them.
    # Shapes taken from the real corpus; content is invented.
    {
        "type": "text",
        "file": {"filePath": "/p/tasks/b31c9xsrq.output",
                 "content": "step 1 done\n", "numLines": 1,
                 "startLine": 0, "totalLines": 1},
        "backgroundTaskId": "b31c9xsrq",
        "backgroundCwdHint": "/p",
    },
    {
        "type": "text",
        "file": {"filePath": "/p/tasks/b31c9xsrq.output",
                 "content": "step 1 done\n", "numLines": 1,
                 "startLine": 0, "totalLines": 1},
        "backgroundTaskId": "b31c9xsrq",
        "backgroundCwdHint": "/p",
        "timedOutAfterMs": 20000,
    },
)


def test_known_identical_results_hash_the_same():
    """If this fails, a per-invocation key has escaped VOLATILE_RESULT_KEYS
    and redundant repeats have silently stopped being detected."""
    first, second = IDENTICAL_BACKGROUND_READS
    assert result_hash(first) == result_hash(second)


def test_a_new_volatile_key_would_break_the_hash_and_this_proves_it():
    """The failure mode the guard above exists to catch, made explicit."""
    first, _ = IDENTICAL_BACKGROUND_READS
    with_new_key = {**first, "someNewPerCallId": "xyz789"}
    assert result_hash(first) != result_hash(with_new_key), (
        "an unknown per-invocation key changes the hash -- when one appears, "
        "add it to VOLATILE_RESULT_KEYS"
    )


def test_every_volatile_key_is_actually_excluded():
    base = {"file": {"content": "x"}}
    for key in VOLATILE_RESULT_KEYS:
        assert result_hash(base) == result_hash({**base, key: "anything"}), (
            f"{key} is listed as volatile but still changes the hash"
        )


def test_a_content_change_still_changes_the_hash():
    """The guard must not be so loose that real differences vanish."""
    first, _ = IDENTICAL_BACKGROUND_READS
    changed = {**first, "file": {**first["file"], "content": "step 2 done\n"}}
    assert result_hash(first) != result_hash(changed)


def test_error_results_are_strings_and_still_hash():
    """toolUseResult is a dict on success and a plain string on error. An
    earlier version only hashed dicts and called every failure unmeasurable."""
    assert result_hash("Error: File has not been read yet.") is not None
    assert result_hash("Error: a") != result_hash("Error: b")


def test_no_result_hashes_to_none_rather_than_a_constant():
    assert result_hash(None) is None


def test_hashes_do_not_leak_their_input():
    assert "hunter2" not in (result_hash({"secret": "hunter2"}) or "")
    assert "secret.py" not in (target_hash({"file_path": "/x/secret.py"}) or "")


def test_a_task_output_path_yields_the_launching_tasks_id():
    assert background_task_id({"file_path": "/p/tasks/b31c9xsrq.output"},
                              None) == "b31c9xsrq"
    assert background_task_id(None,
                              {"backgroundTaskId": "bip6ccady"}) == "bip6ccady"


# -------------------------------------------------- redundant repeats

def test_identical_result_across_repeats_is_redundant():
    recs = [
        call("c1", "Read", "t1", target="/a.py", at=0),
        outcome("r1", "t1", rhash="same", at=1),
        call("c2", "Read", "t2", target="/a.py", at=2),
        outcome("r2", "t2", rhash="same", at=3),
    ]
    found = D.detect_redundant_repeats(recs, scope("c1", "c2"))
    assert len(found) == 1
    assert found[0].subtype == D.SUBTYPE_REDUNDANT
    assert found[0].evidence["calls"] == 2


def test_a_changed_result_is_not_reported():
    """The rule that fixed 4 false positives: a shell command or a background
    process can rewrite a file with no write appearing in the trace."""
    recs = [
        call("c1", "Read", "t1", target="/plot.png", at=0),
        outcome("r1", "t1", rhash="before", at=1),
        call("c2", "Read", "t2", target="/plot.png", at=2),
        outcome("r2", "t2", rhash="after", at=3),
    ]
    assert D.detect_redundant_repeats(recs, scope("c1", "c2")) == []


def test_polling_a_background_task_is_its_own_subtype():
    """Genuine redundancy and unproductive polling are different phenomena
    with different remedies, so they are reported apart."""
    recs = [
        call("b1", "Bash", "t0", args={"command": "run"}, at=0),
        outcome("br", "t0", rhash="launched", at=1, task="b31c9xsrq"),
        call("c1", "Read", "t1", target="/p/tasks/b31c9xsrq.output", at=2,
             task="b31c9xsrq"),
        outcome("r1", "t1", rhash="same", at=3),
        call("c2", "Read", "t2", target="/p/tasks/b31c9xsrq.output", at=4,
             task="b31c9xsrq"),
        outcome("r2", "t2", rhash="same", at=5),
    ]
    found = D.detect_redundant_repeats(recs, scope("b1", "c1", "c2"))
    subtypes = {f.subtype for f in found}
    assert D.SUBTYPE_POLLING in subtypes
    polling = next(f for f in found if f.subtype == D.SUBTYPE_POLLING)
    assert polling.evidence["background_task_id"] == "b31c9xsrq"
    assert "wait was reasonable" in polling.evidence["note"]


def test_polling_still_fires_rather_than_being_suppressed():
    recs = [
        call("b1", "Bash", "t0", args={"command": "run"}, at=0),
        outcome("br", "t0", rhash="l", at=1, task="task9"),
        call("c1", "Read", "t1", target="/p/tasks/task9.output", at=2, task="task9"),
        outcome("r1", "t1", rhash="s", at=3),
        call("c2", "Read", "t2", target="/p/tasks/task9.output", at=4, task="task9"),
        outcome("r2", "t2", rhash="s", at=5),
    ]
    assert any(f.subtype == D.SUBTYPE_POLLING
               for f in D.detect_redundant_repeats(recs, scope("b1", "c1", "c2")))


def test_repeats_in_different_nodes_are_not_a_repeat():
    recs = [
        call("c1", "Read", "t1", target="/a.py", at=0),
        outcome("r1", "t1", rhash="same", at=1),
        call("c2", "Read", "t2", target="/a.py", at=2),
        outcome("r2", "t2", rhash="same", at=3),
    ]
    mixed = {"c1": "turn:t1", "c2": "turn:t2"}
    assert D.detect_redundant_repeats(recs, mixed) == []


def test_a_repeat_with_no_result_on_disk_is_counted_not_guessed():
    recs = [
        call("c1", "Read", "t1", target="/a.py", at=0),
        call("c2", "Read", "t2", target="/a.py", at=2),
        # a third, decidable group so there is a finding to carry the count
        call("c3", "Grep", "t3", args={"q": "x"}, at=4),
        outcome("r3", "t3", rhash="g", at=5),
        call("c4", "Grep", "t4", args={"q": "x"}, at=6),
        outcome("r4", "t4", rhash="g", at=7),
    ]
    found = D.detect_redundant_repeats(recs, scope("c1", "c2", "c3", "c4"))
    assert len(found) == 1
    assert found[0].evidence["undecidable_groups"] == 1


def test_a_repeated_write_that_failed_once_is_a_retry_not_a_loop():
    """All ten repeated writes in the real corpus were retries after a
    failure; is_error is the discriminator for writes, not result equality."""
    recs = [
        call("c1", "Edit", "t1", target="/a.py", at=0),
        outcome("r1", "t1", rhash="err", err=True, at=1),
        call("c2", "Edit", "t2", target="/a.py", at=2),
        outcome("r2", "t2", rhash="ok", at=3),
    ]
    assert D.detect_redundant_repeats(recs, scope("c1", "c2")) == []


def test_a_repeated_write_that_both_times_succeeded_identically_is_reported():
    recs = [
        call("c1", "Edit", "t1", target="/a.py", at=0),
        outcome("r1", "t1", rhash="same", at=1),
        call("c2", "Edit", "t2", target="/a.py", at=2),
        outcome("r2", "t2", rhash="same", at=3),
    ]
    found = D.detect_redundant_repeats(recs, scope("c1", "c2"))
    assert len(found) == 1
    assert found[0].subtype == D.SUBTYPE_WRITE_REDUNDANT


def test_a_single_call_is_never_a_repeat():
    recs = [call("c1", "Read", "t1", target="/a.py"), outcome("r1", "t1")]
    assert D.detect_redundant_repeats(recs, scope("c1")) == []


def test_findings_quote_the_labelled_sample_size():
    """Nobody should mistake 22 groups for a large sample."""
    recs = [
        call("c1", "Read", "t1", target="/a.py", at=0),
        outcome("r1", "t1", rhash="s", at=1),
        call("c2", "Read", "t2", target="/a.py", at=2),
        outcome("r2", "t2", rhash="s", at=3),
    ]
    found = D.detect_redundant_repeats(recs, scope("c1", "c2"))
    assert "6 positive, 15 negative, 1 undecidable" in found[0].evidence[
        "labelled_sample"]


# ------------------------------------------------- cost concentration

@dataclass
class FakeNode:
    node_id: str
    kind: str
    label: str
    parent_node_id: str | None
    total_tokens: Tokens = field(default_factory=Tokens)
    total_usd: float | None = 0.0
    unpriced_records: int = 0


@dataclass
class FakeRun:
    nodes: dict


def run_with(children_tokens, parent_tokens=None, kind="subagent"):
    total = parent_tokens or sum(children_tokens)
    nodes = {"turn:t1": FakeNode("turn:t1", "turn", "turn 1", None,
                                 Tokens(input=total))}
    for i, tok in enumerate(children_tokens):
        nid = f"agent:a{i}"
        nodes[nid] = FakeNode(nid, kind, f"agent {i}", "turn:t1",
                              Tokens(input=tok))
    return FakeRun(nodes=nodes)


def test_a_dominant_sibling_is_flagged():
    run = run_with([900_000, 50_000, 50_000])
    found = D.detect_cost_concentration(run)
    assert len(found) == 1
    assert found[0].node_id == "agent:a0"
    assert found[0].evidence["share_of_parent"] == pytest.approx(0.9)


def test_evenly_spread_siblings_are_not_flagged():
    assert D.detect_cost_concentration(run_with([300_000, 300_000, 300_000])) == []


def test_the_absolute_floor_stops_trivial_runs_firing():
    """90% of almost nothing is not worth saying."""
    assert D.detect_cost_concentration(run_with([900, 50, 50])) == []


def test_every_threshold_is_a_parameter_not_a_constant():
    run = run_with([600_000, 200_000, 200_000])
    assert D.detect_cost_concentration(run, share_threshold=0.9) == []
    assert D.detect_cost_concentration(run, share_threshold=0.5)
    assert D.detect_cost_concentration(run, min_siblings=4) == []
    assert D.detect_cost_concentration(run, min_times_median=99) == []


def test_two_siblings_is_arithmetic_not_concentration():
    """One of two children holding most of the parent is near-inevitable.

    Measured: share alone produced 11 findings on one real session, of which
    7 were two-sibling workflows whose "dominant" child was 1.01x to 1.91x
    its single sibling. Those buried the findings that meant something.
    """
    assert D.detect_cost_concentration(run_with([900_000, 100_000])) == []


def test_a_node_barely_above_its_siblings_is_not_flagged():
    """51% of three even-ish siblings is not disproportionate."""
    assert D.detect_cost_concentration(
        run_with([360_000, 340_000, 340_000])) == []


def test_a_node_far_above_the_typical_sibling_is_flagged():
    found = D.detect_cost_concentration(
        run_with([900_000, 60_000, 60_000, 60_000]))
    assert len(found) == 1
    assert found[0].evidence["times_sibling_median"] == pytest.approx(15.0)


def test_siblings_that_did_nothing_count_as_no_typical_sibling():
    """A zero median means most siblings did essentially nothing, which is
    concentration by any reading -- not a division to skip."""
    run = run_with([900_000, 0, 0, 0])
    run.nodes["agent:a1"].total_tokens = Tokens()
    found = D.detect_cost_concentration(run, min_tokens=1000)
    assert len(found) == 1
    assert found[0].evidence["times_sibling_median"] == "no typical sibling"


def test_the_floor_is_a_parameter_too():
    run = run_with([900, 50, 50])
    assert D.detect_cost_concentration(run, min_tokens=100)


def test_findings_state_that_the_defaults_are_corpus_tuned():
    """A tuned constant presented as a rule is what the negative result
    taught us to avoid."""
    found = D.detect_cost_concentration(run_with([900_000, 50_000, 50_000]))
    ev = found[0].evidence
    assert ev["share_threshold"] == D.DEFAULT_SHARE_THRESHOLD
    assert ev["min_tokens"] == D.DEFAULT_MIN_TOKENS
    assert "not derived" in ev["thresholds_note"]


def test_concentration_reports_the_sibling_median_for_context():
    found = D.detect_cost_concentration(run_with([900_000, 50_000, 50_000]))
    assert found[0].evidence["sibling_median_tokens"] == 50_000
    assert found[0].evidence["times_sibling_median"] == pytest.approx(18.0)


def test_a_lone_child_is_not_concentration():
    """One child always holds 100% of its parent; that says nothing."""
    assert D.detect_cost_concentration(run_with([900_000])) == []


def test_concentration_carries_the_unpriced_count_through():
    run = run_with([900_000, 50_000, 50_000])
    run.nodes["agent:a0"].total_usd = None
    run.nodes["agent:a0"].unpriced_records = 7
    found = D.detect_cost_concentration(run)
    assert found[0].evidence["total_usd"] is None
    assert found[0].evidence["unpriced_records"] == 7


def test_concentration_works_off_a_real_attribution():
    """End to end against Phase 3 rather than a hand-built tree."""
    nodes = [
        type("N", (), {"node_id": "session:s", "kind": "session", "label": "s",
                       "parent_node_id": None, "depth": 0, "input_tokens": 0,
                       "output_tokens": 0, "cache_read_tokens": 0,
                       "cache_creation_tokens": 0, "cache_write_5m_tokens": 0,
                       "cache_write_1h_tokens": 0, "thinking_tokens": 0})(),
    ]
    for i, tok in enumerate((900_000, 30_000, 30_000)):
        nodes.append(type("N", (), {
            "node_id": f"agent:a{i}", "kind": "subagent", "label": f"a{i}",
            "parent_node_id": "session:s", "depth": 1, "input_tokens": tok,
            "output_tokens": 0, "cache_read_tokens": 0,
            "cache_creation_tokens": 0, "cache_write_5m_tokens": 0,
            "cache_write_1h_tokens": 0, "thinking_tokens": 0})())
    recs = [Rec(uuid="r0", input_tokens=900_000),
            Rec(uuid="r1", input_tokens=30_000),
            Rec(uuid="r2", input_tokens=30_000)]
    run = attribute_cost(
        nodes, recs,
        {"r0": "agent:a0", "r1": "agent:a1", "r2": "agent:a2"},
        PriceTable(), AT, "s",
    )
    found = D.detect_cost_concentration(run)
    assert [f.node_id for f in found] == ["agent:a0"]
    assert found[0].evidence["total_usd"] == pytest.approx(4.5)


# ------------------------------------------------- outcome divergence

def test_a_split_group_is_flagged():
    groups = {"claim-1": [("a1", False), ("a2", True), ("a3", False)]}
    found = D.detect_outcome_divergence(groups)
    assert len(found) == 1
    assert found[0].evidence["outcome_counts"] == {"False": 2, "True": 1}


def test_an_agreeing_group_is_silent():
    assert D.detect_outcome_divergence(
        {"claim-1": [("a1", False), ("a2", False), ("a3", False)]}) == []


def test_the_real_verifier_triples_are_reproduced():
    """The corpus's natural experiment: 25 tasks each run three times, of
    which 3 split on outcome and 22 agreed. Must flag exactly the 3."""
    groups = {f"agree-{i}": [("a", False), ("b", False), ("c", False)]
              for i in range(22)}
    groups.update({
        "split-1": [("a", False), ("b", True), ("c", False)],
        "split-2": [("a", False), ("b", False), ("c", True)],
        "split-3": [("a", False), ("b", True), ("c", False)],
    })
    found = D.detect_outcome_divergence(groups)
    assert len(found) == 3
    assert {f.evidence["task_key"] for f in found} == {"split-1", "split-2", "split-3"}


def test_an_incomplete_group_is_not_compared():
    """A missing outcome cannot be honestly called agreement or divergence."""
    assert D.detect_outcome_divergence(
        {"c": [("a", False), ("b", None), ("c", False)]}) == []


def test_a_lone_member_is_not_a_comparison():
    assert D.detect_outcome_divergence({"c": [("a", False)]}) == []


def test_divergence_states_its_precondition():
    found = D.detect_outcome_divergence({"c": [("a", 1), ("b", 2)]})
    assert "does not generalise" in found[0].evidence["requires"]


# -------------------------------------------------- unhandled errors

def test_a_failed_call_never_followed_up_is_reported():
    recs = [
        call("c1", "Write", "t1", target="/a.py", at=0),
        outcome("r1", "t1", rhash="err", err=True, at=1),
    ]
    found = D.detect_unhandled_errors(recs, scope("c1"))
    assert len(found) == 1
    assert found[0].confidence == D.LOW


def test_a_failed_call_followed_up_on_the_same_target_is_silent():
    recs = [
        call("c1", "Write", "t1", target="/a.py", at=0),
        outcome("r1", "t1", rhash="err", err=True, at=1),
        call("c2", "Write", "t2", target="/a.py", at=2),
        outcome("r2", "t2", rhash="ok", at=3),
    ]
    assert D.detect_unhandled_errors(recs, scope("c1", "c2")) == []


def test_a_successful_call_is_never_reported():
    recs = [call("c1", "Write", "t1", target="/a.py", at=0),
            outcome("r1", "t1", at=1)]
    assert D.detect_unhandled_errors(recs, scope("c1")) == []


def test_an_error_with_no_identifiable_target_is_skipped_and_counted():
    """A failed shell command has no target to follow up on, so the proxy has
    nothing to say about it."""
    recs = [
        call("c1", "Bash", "t1", args={"command": "false"}, at=0),
        outcome("r1", "t1", rhash="err", err=True, at=1),
        call("c2", "Write", "t2", target="/a.py", at=2),
        outcome("r2", "t2", rhash="err", err=True, at=3),
    ]
    found = D.detect_unhandled_errors(recs, scope("c1", "c2"))
    assert len(found) == 1
    assert found[0].evidence["skipped_errors_without_a_target"] == 1


def test_the_finding_refuses_to_call_it_a_verdict():
    """Some errors are informative and moving on is correct. A detector that
    called those a failure would be wrong more often than right."""
    recs = [call("c1", "Write", "t1", target="/a.py", at=0),
            outcome("r1", "t1", rhash="err", err=True, at=1)]
    reading = D.detect_unhandled_errors(recs, scope("c1"))[0].evidence["reading"]
    assert "not a verdict" in reading
    assert "does not claim the error" in reading


def test_the_finding_carries_its_measured_precision():
    recs = [call("c1", "Write", "t1", target="/a.py", at=0),
            outcome("r1", "t1", rhash="err", err=True, at=1)]
    found = D.detect_unhandled_errors(recs, scope("c1"))
    assert "3 of 14" in found[0].evidence["measured_precision"]


# ------------------------------------------------------------- run_all

def test_run_all_is_stable_and_skips_what_it_lacks():
    recs = [
        call("c1", "Read", "t1", target="/a.py", at=0),
        outcome("r1", "t1", rhash="s", at=1),
        call("c2", "Read", "t2", target="/a.py", at=2),
        outcome("r2", "t2", rhash="s", at=3),
    ]
    found = D.run_all(recs, scope("c1", "c2"))
    assert [f.detector for f in found] == [D.REDUNDANT_REPEATS]


def test_run_all_passes_thresholds_through():
    run = run_with([900, 50, 50])
    found = D.run_all([], {}, run_cost=run, min_tokens=100)
    assert any(f.detector == D.COST_CONCENTRATION for f in found)


def test_detectors_are_pure_and_do_not_mutate_their_input():
    recs = [
        call("c1", "Read", "t1", target="/a.py", at=0),
        outcome("r1", "t1", rhash="s", at=1),
        call("c2", "Read", "t2", target="/a.py", at=2),
        outcome("r2", "t2", rhash="s", at=3),
    ]
    before = [(r.uuid, r.result_hash, r.tool_signature) for r in recs]
    D.run_all(recs, scope("c1", "c2"))
    assert [(r.uuid, r.result_hash, r.tool_signature) for r in recs] == before


# ------------------------------------- the error-template rot guard
#
# Same failure mode as VOLATILE_RESULT_KEYS: the harness rewords an error,
# our template stops matching, benign classes stop being recognised, and
# unhandled_errors quietly goes back to reporting mostly noise. Nothing
# crashes. These are the guard.
#
# The strings below are recorded verbatim from the real corpus. They are
# harness boilerplate -- fixed text the CLI emits -- not prompt text or tool
# arguments, which is why quoting them here does not carry content.

RECORDED_ERRORS = {
    "user_declined": (
        "The user doesn't want to proceed with this tool use. The tool use "
        "was rejected (eg. if it was a file edit, the new_string was not "
        "applied)."
    ),
    "file_not_found": (
        "File does not exist. Note: your current working directory is /p"
    ),
    "read_before_write": (
        "<tool_use_error>File has not been read yet. Read it first before "
        "writing to it.</tool_use_error>"
    ),
    "schema_mismatch": (
        "Output does not match required schema: root: must have required "
        "property 'findings'"
    ),
    "tool_unavailable": (
        "<tool_use_error>Error: No such tool available: Write. Write exists "
        "but is not enabled in this context.</tool_use_error>"
    ),
    "blocked_by_policy": (
        "Remove-Item on system path '/' is blocked. This path is protected "
        "from removal."
    ),
}


@pytest.mark.parametrize(("expected", "text"), sorted(RECORDED_ERRORS.items()))
def test_recorded_error_templates_still_classify(expected, text):
    """If this fails the harness has reworded an error and the template needs
    updating -- until then, that class silently stops being recognised."""
    assert classify_error(text) == expected


def test_an_unrecognised_error_is_other_not_none():
    """`other` and None mean different things: an error we have no template
    for, versus a call that did not fail at all."""
    assert classify_error("Exit code 1\nTraceback (most recent call last):") == "other"
    assert classify_error(None) is None
    assert classify_error("") is None


def test_every_benign_class_is_one_a_template_can_produce():
    """A rename in one place and not the other would silently stop the
    filtering, with no test failing and precision quietly dropping."""
    producible = {label for label, _needle in ERROR_TEMPLATES}
    unknown = D.BENIGN_ERROR_CLASSES - producible
    assert unknown == set(), (
        f"BENIGN_ERROR_CLASSES names {unknown}, which no template produces"
    )


def test_the_benign_classes_are_the_two_that_were_measured():
    """Widening this set changes a published precision figure, so changing it
    should require changing this test and re-measuring."""
    assert D.BENIGN_ERROR_CLASSES == {"user_declined", "file_not_found"}


def test_a_declined_tool_is_not_reported_as_unhandled():
    """The user said no. There is nothing for the agent to have handled."""
    recs = [
        call("c1", "Write", "t1", target="/a.py", at=0),
        Rec(uuid="r1", ts_ns=1, tool_use_id="t1", is_tool_result=True,
            result_hash="e", is_error=True, error_class="user_declined"),
    ]
    assert D.detect_unhandled_errors(recs, scope("c1")) == []


def test_a_probe_for_a_missing_file_is_not_reported_as_unhandled():
    recs = [
        call("c1", "Read", "t1", target="/nope.py", at=0),
        Rec(uuid="r1", ts_ns=1, tool_use_id="t1", is_tool_result=True,
            result_hash="e", is_error=True, error_class="file_not_found"),
    ]
    assert D.detect_unhandled_errors(recs, scope("c1")) == []


def test_a_genuinely_abandoned_write_is_still_reported():
    """The benign filter must not swallow the cases worth seeing."""
    recs = [
        call("c1", "Write", "t1", target="/a.py", at=0),
        Rec(uuid="r1", ts_ns=1, tool_use_id="t1", is_tool_result=True,
            result_hash="e", is_error=True, error_class="read_before_write"),
    ]
    found = D.detect_unhandled_errors(recs, scope("c1"))
    assert len(found) == 1
    assert found[0].evidence["error_class"] == "read_before_write"


def test_excluded_and_unclassified_counts_are_reported():
    """A rising unclassified share is the in-use signal that templates have
    drifted, so it travels on every finding."""
    recs = [
        call("c0", "Write", "t0", target="/x.py", at=0),
        Rec(uuid="r0", ts_ns=1, tool_use_id="t0", is_tool_result=True,
            result_hash="e", is_error=True, error_class="user_declined"),
        call("c1", "Write", "t1", target="/a.py", at=2),
        Rec(uuid="r1", ts_ns=3, tool_use_id="t1", is_tool_result=True,
            result_hash="e", is_error=True, error_class="other"),
    ]
    found = D.detect_unhandled_errors(recs, scope("c0", "c1"))
    assert len(found) == 1
    assert found[0].evidence["benign_errors_excluded"] == 1
    assert found[0].evidence["unclassified_errors"] == 1


def test_the_published_precision_travels_with_its_sample_size():
    recs = [
        call("c1", "Write", "t1", target="/a.py", at=0),
        Rec(uuid="r1", ts_ns=1, tool_use_id="t1", is_tool_result=True,
            result_hash="e", is_error=True, error_class="read_before_write"),
    ]
    text = D.detect_unhandled_errors(recs, scope("c1"))[0].evidence[
        "measured_precision"]
    assert "3 of 4" in text
    assert "sample size 4" in text
