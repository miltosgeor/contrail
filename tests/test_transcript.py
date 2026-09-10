"""Tests for the transcript parser and subagent tree reconstruction.

Fixtures here are synthetic and structure-only. They imitate the shapes
recorded in docs/spec.md ("Transcript format, as verified") without copying
anything out of a real session, so the suite carries no prompt or
tool-argument content and writes nothing outside `tmp_path`.
"""

from __future__ import annotations

import json

import pytest

from contrail.store import Store
from contrail.transcript import (
    KIND_SESSION,
    KIND_SUBAGENT,
    KIND_TOOL,
    KIND_TURN,
    KIND_WORKFLOW,
    LINK_DIRECTORY,
    LINK_META_TOOL_USE_ID,
    LINK_TOOL_USE_RESULT,
    LINK_WORKFLOW_JOURNAL,
    STATUS_ERROR,
    STATUS_INCOMPLETE,
    STATUS_MISSING_TRANSCRIPT,
    STATUS_OK,
    STATUS_PENDING,
    build_tree,
    discover_sessions,
    load_session,
    normalise_argument,
    parse_record,
    tool_signature,
)

SESSION = "11111111-2222-3333-4444-555555555555"


# --------------------------------------------------------------- fixtures

def ts(second: int) -> str:
    return f"2026-09-10T10:00:{second:02d}.000Z"


def human(uuid: str, *, parent: str | None = None, at: int = 0, **extra):
    return {
        "type": "user",
        "uuid": uuid,
        "parentUuid": parent,
        "sessionId": SESSION,
        "isSidechain": False,
        "timestamp": ts(at),
        "promptId": f"p-{uuid}",
        "origin": {"kind": "human"},
        "message": {"role": "user", "content": [{"type": "text", "text": "task"}]},
        **extra,
    }


def call(uuid: str, tool: str, tool_use_id: str, *, parent=None, at=0,
         tool_input=None, tokens=(0, 0, 0, 0), **extra):
    inp, out, cr, cc = tokens
    return {
        "type": "assistant",
        "uuid": uuid,
        "parentUuid": parent,
        "sessionId": SESSION,
        "isSidechain": extra.pop("sidechain", False),
        "timestamp": ts(at),
        "message": {
            "role": "assistant",
            "model": "claude-opus-5",
            "usage": {
                "input_tokens": inp,
                "output_tokens": out,
                "cache_read_input_tokens": cr,
                "cache_creation_input_tokens": cc,
            },
            "content": [{
                "type": "tool_use",
                "id": tool_use_id,
                "name": tool,
                "input": tool_input if tool_input is not None else {"a": 1},
            }],
        },
        **extra,
    }


def result(uuid: str, tool_use_id: str, *, parent=None, at=1, is_error=None,
           tool_use_result=None, **extra):
    block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": "ok"}
    if is_error is not None:
        block["is_error"] = is_error
    record = {
        "type": "user",
        "uuid": uuid,
        "parentUuid": parent,
        "sourceToolAssistantUUID": parent,
        "sessionId": SESSION,
        "isSidechain": extra.pop("sidechain", False),
        "timestamp": ts(at),
        "message": {"role": "user", "content": [block]},
        **extra,
    }
    if tool_use_result is not None:
        record["toolUseResult"] = tool_use_result
    return record


def agent_prompt(uuid: str, agent_id: str, *, at=0):
    """A subagent transcript's first record: a bare-string prompt."""
    return {
        "type": "user",
        "uuid": uuid,
        "parentUuid": None,
        "sessionId": SESSION,
        "agentId": agent_id,
        "isSidechain": True,
        "timestamp": ts(at),
        "message": {"role": "user", "content": "go"},
    }


def write_session(root, records, *, session_id=SESSION, project="proj-slug",
                  agents=None, journals=None):
    """Lay out one session the way Claude Code does on disk."""
    proj = root / project
    proj.mkdir(parents=True, exist_ok=True)
    path = proj / f"{session_id}.jsonl"
    path.write_text(
        "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8"
    )

    for agent_id, spec in (agents or {}).items():
        run_id = spec.get("run_id")
        base = proj / session_id / "subagents"
        if run_id:
            base = base / "workflows" / run_id
        base.mkdir(parents=True, exist_ok=True)
        if spec.get("records") is not None:
            (base / f"agent-{agent_id}.jsonl").write_text(
                "".join(json.dumps(r) + "\n" for r in spec["records"]),
                encoding="utf-8",
            )
        if spec.get("meta") is not None:
            (base / f"agent-{agent_id}.meta.json").write_text(
                json.dumps(spec["meta"]), encoding="utf-8"
            )

    for run_id, entries in (journals or {}).items():
        wf = proj / session_id / "subagents" / "workflows" / run_id
        wf.mkdir(parents=True, exist_ok=True)
        lines = []
        for agent_id, flags in entries.items():
            if flags.get("started", True):
                lines.append({"type": "started", "key": "v2:x", "agentId": agent_id})
            if flags.get("result", True):
                lines.append(
                    {"type": "result", "key": "v2:x", "agentId": agent_id,
                     "result": "done"}
                )
        (wf / "journal.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in lines), encoding="utf-8"
        )

    return path


def tree_for(root, records, **kwargs):
    path = write_session(root, records, **kwargs)
    return build_tree(load_session(path))


def node_by(tree, kind, label=None):
    for n in tree.nodes:
        if n.kind == kind and (label is None or n.label == label):
            return n
    return None


# ------------------------------------------------------- signatures (Phase 4)

def test_signature_ignores_path_separator_direction():
    """The same file is written as a\\b by one tool and a/b by another."""
    assert tool_signature("Read", {"p": r"c:\a\b.py"}) == tool_signature(
        "Read", {"p": "c:/a/b.py"}
    )


def test_signature_ignores_surrounding_and_repeated_whitespace():
    assert tool_signature("Bash", {"c": "pytest  -q\n"}) == tool_signature(
        "Bash", {"c": "pytest -q"}
    )


def test_signature_ignores_argument_key_order():
    assert tool_signature("Grep", {"a": 1, "b": 2}) == tool_signature(
        "Grep", {"b": 2, "a": 1}
    )


def test_signature_distinguishes_different_arguments():
    assert tool_signature("Read", {"p": "a.py"}) != tool_signature("Read", {"p": "b.py"})


def test_signature_distinguishes_different_tools():
    assert tool_signature("Read", {"p": "a"}) != tool_signature("Write", {"p": "a"})


def test_signature_preserves_case():
    """Case is meaningful in code and identifiers; do not fold it."""
    assert tool_signature("Read", {"p": "A.py"}) != tool_signature("Read", {"p": "a.py"})


def test_signature_is_not_reversible():
    """A signature must not leak the argument text it was built from."""
    sig = tool_signature("Bash", {"command": "deploy --token hunter2"})
    assert "hunter2" not in sig
    assert len(sig) == 16


def test_normalise_recurses_into_nested_structures():
    assert normalise_argument({"x": [" a ", {"k": "b\n\nc"}]}) == {"x": ["a", {"k": "b c"}]}


# ---------------------------------------------------------------- records

def test_bookkeeping_record_types_are_skipped():
    for rtype in ("attachment", "queue-operation", "file-history-snapshot",
                  "ai-title", "mode", "system", "bridge-session", "atis-latch"):
        assert parse_record({"type": rtype, "uuid": "u"}) is None


def test_unknown_future_record_type_is_skipped_not_fatal():
    assert parse_record({"type": "something-new-in-2027", "uuid": "u"}) is None


def test_record_without_uuid_is_rejected():
    assert parse_record({"type": "assistant", "message": {}}) is None


def test_cache_tokens_are_kept_separate_from_input():
    rec = parse_record(call("u", "Read", "t1", tokens=(10, 20, 300, 40)))
    assert rec.input_tokens == 10
    assert rec.output_tokens == 20
    assert rec.cache_read_tokens == 300
    assert rec.cache_creation_tokens == 40


def test_tool_call_is_parsed_with_a_signature():
    rec = parse_record(call("u", "Bash", "t1", tool_input={"command": "ls"}))
    assert rec.tool_name == "Bash"
    assert rec.tool_use_id == "t1"
    assert rec.tool_signature == tool_signature("Bash", {"command": "ls"})
    assert not rec.is_tool_result


def test_tool_result_is_parsed_and_joins_on_tool_use_id():
    rec = parse_record(result("u", "t1"))
    assert rec.is_tool_result
    assert rec.tool_use_id == "t1"
    assert not rec.is_error


def test_absent_is_error_means_not_an_error():
    """is_error is absent on most results; absent must not read as failure."""
    assert parse_record(result("u", "t1"))            .is_error is False
    assert parse_record(result("u", "t1", is_error=False)).is_error is False
    assert parse_record(result("u", "t1", is_error=True)) .is_error is True


def test_human_turn_is_distinguished_from_a_replayed_result():
    assert parse_record(human("u")).is_human_turn
    assert not parse_record(result("u", "t1")).is_human_turn


def test_injected_meta_record_does_not_open_a_turn():
    assert not parse_record(human("u", isMeta=True)).is_human_turn


def test_unreadable_timestamp_degrades_to_zero():
    rec = parse_record(call("u", "Read", "t1", at=0) | {"timestamp": "not-a-date"})
    assert rec.ts_ns == 0


# ------------------------------------------------------- content is opt-in

def test_no_content_is_captured_by_default(monkeypatch):
    monkeypatch.delenv("CONTRAIL_CAPTURE_CONTENT", raising=False)
    call_rec = parse_record(call("u", "Bash", "t1", tool_input={"command": "secret"}))
    result_rec = parse_record(result("u2", "t1"))
    assert call_rec.content is None
    assert result_rec.content is None


def test_content_is_captured_only_when_opted_in(monkeypatch):
    monkeypatch.setenv("CONTRAIL_CAPTURE_CONTENT", "1")
    rec = parse_record(call("u", "Bash", "t1", tool_input={"command": "ls"}))
    assert rec.content is not None and "ls" in rec.content


def test_structure_is_still_recorded_when_content_is_not(monkeypatch):
    """Lengths and signatures are structure, and survive the default."""
    monkeypatch.delenv("CONTRAIL_CAPTURE_CONTENT", raising=False)
    rec = parse_record(result("u", "t1"))
    assert rec.content is None
    assert rec.text_len > 0


# ------------------------------------------------------------ file loading

def test_malformed_line_is_counted_not_fatal(tmp_path):
    path = write_session(tmp_path, [human("a")])
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{not json at all\n")
        fh.write(json.dumps(call("b", "Read", "t1", parent="a")) + "\n")
    session = load_session(path)
    assert session.parse_errors == 1
    assert len(session.records) == 2  # the good records either side survive


def test_truncated_final_line_is_tolerated(tmp_path):
    """The file may be appending while we read it."""
    path = write_session(tmp_path, [human("a")])
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"type": "assistant", "uuid": "b", "mess')
    session = load_session(path)
    assert session.parse_errors == 1
    assert len(session.records) == 1


def test_duplicate_uuid_is_deduplicated_last_write_wins(tmp_path):
    """A resumed session can write the same uuid twice; seen on real data."""
    first = call("dup", "Read", "t1", at=1)
    second = call("dup", "Write", "t2", at=2)
    path = write_session(tmp_path, [human("a"), first, second])
    session = load_session(path)
    assert session.duplicates == 1
    assert len(session.records) == 2
    assert [r.tool_name for r in session.records if r.tool_name] == ["Write"]


def test_records_are_sorted_by_timestamp(tmp_path):
    path = write_session(tmp_path, [
        call("late", "Read", "t2", at=9),
        call("early", "Read", "t1", at=1),
    ])
    session = load_session(path)
    assert [r.uuid for r in session.records] == ["early", "late"]


def test_a_file_spanning_cli_versions_still_parses(tmp_path):
    """One real session spanned 22 CLI versions; fields drift across them."""
    old_style = human("a", at=0) | {"version": "2.1.121"}
    new_style = call("b", "Read", "t1", parent="a", at=1) | {
        "version": "2.1.266", "effort": "high", "slug": "some-slug",
    }
    path = write_session(tmp_path, [old_style, new_style])
    session = load_session(path)
    assert session.parse_errors == 0
    assert len(session.records) == 2


def test_discover_sessions_finds_transcripts(tmp_path):
    write_session(tmp_path, [human("a")], session_id="aaa")
    write_session(tmp_path, [human("b")], session_id="bbb", project="other")
    assert {p.stem for p in discover_sessions(tmp_path)} == {"aaa", "bbb"}


def test_discover_sessions_on_a_missing_directory_is_empty(tmp_path):
    assert discover_sessions(tmp_path / "nope") == []


# ------------------------------------------------------------------- tree

def test_tools_nest_under_the_turn_that_issued_them(tmp_path):
    tree = tree_for(tmp_path, [
        human("a", at=0),
        call("b", "Read", "t1", parent="a", at=1),
        result("c", "t1", parent="b", at=2),
    ])
    turn = node_by(tree, KIND_TURN)
    tool = node_by(tree, KIND_TOOL, "Read")
    assert tree.root.kind == KIND_SESSION
    assert turn.parent_node_id == tree.root.node_id
    assert tool.parent_node_id == turn.node_id


def test_turns_are_numbered_in_order(tmp_path):
    tree = tree_for(tmp_path, [human("a", at=0), human("b", at=5)])
    assert [n.label for n in tree.by_kind(KIND_TURN)] == ["turn 1", "turn 2"]


def test_completed_tool_call_is_ok(tmp_path):
    tree = tree_for(tmp_path, [
        human("a"), call("b", "Read", "t1", parent="a", at=1),
        result("c", "t1", parent="b", at=2),
    ])
    assert node_by(tree, KIND_TOOL).status == STATUS_OK


def test_tool_call_with_no_result_is_incomplete(tmp_path):
    """5,282 calls to 5,280 results on the real corpus: this happens."""
    tree = tree_for(tmp_path, [human("a"), call("b", "Read", "t1", parent="a", at=1)])
    assert node_by(tree, KIND_TOOL).status == STATUS_INCOMPLETE


def test_failed_tool_call_is_flagged_as_error(tmp_path):
    tree = tree_for(tmp_path, [
        human("a"), call("b", "Bash", "t1", parent="a", at=1),
        result("c", "t1", parent="b", at=2, is_error=True),
    ])
    assert node_by(tree, KIND_TOOL).status == STATUS_ERROR


def test_result_without_a_matching_call_is_warned_not_dropped(tmp_path):
    tree = tree_for(tmp_path, [human("a"), result("c", "t-unknown", parent="a", at=1)])
    assert any("no matching call" in w for w in tree.warnings)


def test_tool_tokens_land_on_the_enclosing_turn(tmp_path):
    tree = tree_for(tmp_path, [
        human("a", at=0),
        call("b", "Read", "t1", parent="a", at=1, tokens=(5, 7, 11, 13)),
        result("c", "t1", parent="b", at=2),
    ])
    turn = node_by(tree, KIND_TURN)
    assert (turn.input_tokens, turn.output_tokens) == (5, 7)
    assert (turn.cache_read_tokens, turn.cache_creation_tokens) == (11, 13)


# ------------------------------------------------- join 1: Agent tool spawns

AGENT_ID = "a0ea8d272b7957e60"


def agent_records(agent_id=AGENT_ID):
    return [
        agent_prompt("s1", agent_id, at=3),
        call("s2", "Grep", "st1", parent="s1", at=4, sidechain=True) | {"agentId": agent_id},
        result("s3", "st1", parent="s2", at=5, sidechain=True) | {"agentId": agent_id},
    ]


def test_agent_spawn_links_by_tool_use_result_agent_id(tmp_path):
    tree = tree_for(
        tmp_path,
        [
            human("a", at=0),
            call("b", "Agent", "t1", parent="a", at=1),
            result("c", "t1", parent="b", at=6, tool_use_result={
                "status": "completed", "agentId": AGENT_ID, "agentType": "Explore",
            }),
        ],
        agents={AGENT_ID: {
            "records": agent_records(),
            "meta": {"agentType": "Explore", "description": "find things",
                     "toolUseId": "t1", "spawnDepth": 1},
        }},
    )
    sub = node_by(tree, KIND_SUBAGENT)
    assert sub.link_basis == LINK_TOOL_USE_RESULT
    assert sub.agent_type == "Explore"
    assert sub.label == "find things"
    assert sub.parent_node_id == node_by(tree, KIND_TOOL, "Agent").node_id


def test_a_subagents_own_tools_nest_beneath_it(tmp_path):
    tree = tree_for(
        tmp_path,
        [
            human("a", at=0), call("b", "Agent", "t1", parent="a", at=1),
            result("c", "t1", parent="b", at=6,
                   tool_use_result={"agentId": AGENT_ID, "agentType": "Explore"}),
        ],
        agents={AGENT_ID: {"records": agent_records(), "meta": {"agentType": "Explore"}}},
    )
    sub = node_by(tree, KIND_SUBAGENT)
    inner = node_by(tree, KIND_TOOL, "Grep")
    assert inner.parent_node_id == sub.node_id
    assert inner.depth == sub.depth + 1


def test_task_is_accepted_as_well_as_agent(tmp_path):
    """The spawn tool was renamed across the surveyed CLI versions."""
    tree = tree_for(
        tmp_path,
        [
            human("a"), call("b", "Task", "t1", parent="a", at=1),
            result("c", "t1", parent="b", at=6,
                   tool_use_result={"agentId": AGENT_ID, "agentType": "Explore"}),
        ],
        agents={AGENT_ID: {"records": agent_records(), "meta": {"agentType": "Explore"}}},
    )
    assert node_by(tree, KIND_SUBAGENT).link_basis == LINK_TOOL_USE_RESULT


# --------------------------------------- join 2: meta.json toolUseId fallback

def test_meta_tool_use_id_links_when_the_parent_result_is_missing(tmp_path):
    """The call is on disk but its result never landed; the sidecar saves it."""
    tree = tree_for(
        tmp_path,
        [human("a", at=0), call("b", "Agent", "t1", parent="a", at=1)],
        agents={AGENT_ID: {
            "records": agent_records(),
            "meta": {"agentType": "Explore", "toolUseId": "t1"},
        }},
    )
    sub = node_by(tree, KIND_SUBAGENT)
    assert sub.link_basis == LINK_META_TOOL_USE_ID
    assert sub.parent_node_id == node_by(tree, KIND_TOOL, "Agent").node_id


# ------------------------------- join 3: workflow journal (the 124-file case)

RUN = "wf_8c35c824-7c9"
W1, W2 = "ad6dad243cdb3e339", "a751720330cb680ec"


def workflow_fixture(journal=None, agents=None):
    return {
        "records": [
            human("a", at=0),
            call("b", "Workflow", "t1", parent="a", at=1),
            result("c", "t1", parent="b", at=2, tool_use_result={
                "status": "async_launched",
                "taskId": "wk8gd597e",
                "runId": RUN,
                "workflowName": "deep-research",
                "transcriptDir": rf"C:\Users\x\.claude\projects\p\{SESSION}\subagents\workflows\{RUN}",
            }),
        ],
        "agents": agents if agents is not None else {
            W1: {"records": agent_records(W1), "run_id": RUN,
                 "meta": {"agentType": "workflow-subagent"}},
            W2: {"records": agent_records(W2), "run_id": RUN,
                 "meta": {"agentType": "workflow-subagent"}},
        },
        "journals": journal if journal is not None else {
            RUN: {W1: {"started": True, "result": True},
                  W2: {"started": True, "result": True}}
        },
    }


def test_workflow_children_nest_under_the_workflow_not_the_session(tmp_path):
    """The point of using the journal: these must not attach flat to the root."""
    fx = workflow_fixture()
    tree = tree_for(tmp_path, fx["records"], agents=fx["agents"], journals=fx["journals"])

    wf = node_by(tree, KIND_WORKFLOW)
    tool = node_by(tree, KIND_TOOL, "Workflow")
    subs = tree.by_kind(KIND_SUBAGENT)

    assert wf.parent_node_id == tool.node_id
    assert len(subs) == 2
    for sub in subs:
        assert sub.parent_node_id == wf.node_id
        assert sub.link_basis == LINK_WORKFLOW_JOURNAL
        assert sub.parent_node_id != tree.root.node_id


def test_workflow_subagents_are_never_linked_by_directory_when_a_journal_exists(tmp_path):
    fx = workflow_fixture()
    tree = tree_for(tmp_path, fx["records"], agents=fx["agents"], journals=fx["journals"])
    assert tree.link_bases == {LINK_WORKFLOW_JOURNAL: 2}
    assert LINK_DIRECTORY not in tree.link_bases


def test_async_launched_leaves_the_tool_call_pending(tmp_path):
    """The tool really did return before the work finished; say so."""
    fx = workflow_fixture()
    tree = tree_for(tmp_path, fx["records"], agents=fx["agents"], journals=fx["journals"])
    assert node_by(tree, KIND_TOOL, "Workflow").status == STATUS_PENDING


def test_workflow_status_comes_from_the_journal_not_the_async_tool_call(tmp_path):
    fx = workflow_fixture()
    tree = tree_for(tmp_path, fx["records"], agents=fx["agents"], journals=fx["journals"])
    assert node_by(tree, KIND_WORKFLOW).status == STATUS_OK


def test_journal_start_without_result_marks_that_child_incomplete(tmp_path):
    fx = workflow_fixture(journal={
        RUN: {W1: {"started": True, "result": True},
              W2: {"started": True, "result": False}}
    })
    tree = tree_for(tmp_path, fx["records"], agents=fx["agents"], journals=fx["journals"])
    statuses = {n.agent_id: n.status for n in tree.by_kind(KIND_SUBAGENT)}
    assert statuses[W1] == STATUS_OK
    assert statuses[W2] == STATUS_INCOMPLETE
    assert node_by(tree, KIND_WORKFLOW).status == STATUS_INCOMPLETE


def test_run_id_is_recovered_from_transcript_dir_when_absent(tmp_path):
    """Only transcriptDir survives; the run id is its last path segment."""
    fx = workflow_fixture()
    fx["records"][2]["toolUseResult"].pop("runId")
    tree = tree_for(tmp_path, fx["records"], agents=fx["agents"], journals=fx["journals"])
    wf = node_by(tree, KIND_WORKFLOW)
    assert wf is not None and wf.label == RUN
    assert len(tree.by_kind(KIND_SUBAGENT)) == 2


def test_workflow_without_a_journal_falls_back_to_the_directory(tmp_path):
    fx = workflow_fixture(journal={})
    tree = tree_for(tmp_path, fx["records"], agents=fx["agents"], journals=fx["journals"])
    assert len(tree.by_kind(KIND_SUBAGENT)) == 2
    assert any("no journal" in w for w in tree.warnings)
    assert "directory" in node_by(tree, KIND_WORKFLOW).note


# ------------------------------------------------------- degradation paths

def test_subagent_referenced_but_absent_from_disk_is_kept_and_flagged(tmp_path):
    """Omitting it silently would make the tree a lie."""
    tree = tree_for(
        tmp_path,
        [
            human("a"), call("b", "Agent", "t1", parent="a", at=1),
            result("c", "t1", parent="b", at=2,
                   tool_use_result={"agentId": "aGONE", "agentType": "Explore"}),
        ],
    )
    sub = node_by(tree, KIND_SUBAGENT)
    assert sub is not None
    assert sub.status == STATUS_MISSING_TRANSCRIPT
    assert sub.agent_type == "Explore"
    assert any("no transcript on disk" in w for w in tree.warnings)


def test_meta_present_but_transcript_missing_is_flagged(tmp_path):
    tree = tree_for(
        tmp_path,
        [human("a"), call("b", "Agent", "t1", parent="a", at=1)],
        agents={AGENT_ID: {"records": None, "meta": {"agentType": "Explore",
                                                     "toolUseId": "t1"}}},
    )
    sub = node_by(tree, KIND_SUBAGENT)
    assert sub.status == STATUS_MISSING_TRANSCRIPT


def test_orphan_agent_file_is_linked_by_directory_as_a_last_resort(tmp_path):
    """Never fires on the real corpus; it stops a subagent vanishing."""
    tree = tree_for(
        tmp_path,
        [human("a", at=0)],
        agents={AGENT_ID: {"records": agent_records(), "meta": {"agentType": "Explore"}}},
    )
    sub = node_by(tree, KIND_SUBAGENT)
    assert sub.link_basis == LINK_DIRECTORY
    assert sub.parent_node_id == tree.root.node_id
    assert any("directory only" in w for w in tree.warnings)


def test_an_agent_referenced_twice_does_not_recurse_forever(tmp_path):
    tree = tree_for(
        tmp_path,
        [
            human("a"),
            call("b", "Agent", "t1", parent="a", at=1),
            result("c", "t1", parent="b", at=2,
                   tool_use_result={"agentId": AGENT_ID, "agentType": "Explore"}),
            call("d", "Agent", "t2", parent="c", at=3),
            result("e", "t2", parent="d", at=4,
                   tool_use_result={"agentId": AGENT_ID, "agentType": "Explore"}),
        ],
        agents={AGENT_ID: {"records": agent_records(), "meta": {"agentType": "Explore"}}},
    )
    assert len(tree.by_kind(KIND_SUBAGENT)) == 1
    assert any("more than once" in w for w in tree.warnings)


def test_a_session_with_no_subagents_yields_a_flat_tree(tmp_path):
    tree = tree_for(tmp_path, [
        human("a"), call("b", "Read", "t1", parent="a", at=1),
        result("c", "t1", parent="b", at=2),
    ])
    assert tree.by_kind(KIND_SUBAGENT) == []
    assert tree.link_bases == {}


def test_nested_subagent_reaches_depth_two(tmp_path):
    """spawnDepth was only ever 1 on the corpus, but the format allows more."""
    inner_id = "aINNER"
    outer = [
        agent_prompt("s1", AGENT_ID, at=3),
        call("s2", "Agent", "st1", parent="s1", at=4, sidechain=True) | {"agentId": AGENT_ID},
        result("s3", "st1", parent="s2", at=5, sidechain=True) | {
            "agentId": AGENT_ID,
            "toolUseResult": {"agentId": inner_id, "agentType": "Plan"},
        },
    ]
    tree = tree_for(
        tmp_path,
        [
            human("a"), call("b", "Agent", "t1", parent="a", at=1),
            result("c", "t1", parent="b", at=6,
                   tool_use_result={"agentId": AGENT_ID, "agentType": "Explore"}),
        ],
        agents={
            AGENT_ID: {"records": outer, "meta": {"agentType": "Explore",
                                                  "spawnDepth": 1}},
            inner_id: {"records": agent_records(inner_id),
                       "meta": {"agentType": "Plan", "spawnDepth": 2}},
        },
    )
    subs = {n.agent_id: n for n in tree.by_kind(KIND_SUBAGENT)}
    assert set(subs) == {AGENT_ID, inner_id}
    assert subs[inner_id].depth > subs[AGENT_ID].depth


def test_unreadable_meta_json_does_not_break_the_parse(tmp_path):
    path = write_session(
        tmp_path, [human("a", at=0)],
        agents={AGENT_ID: {"records": agent_records(), "meta": {"agentType": "Explore"}}},
    )
    meta = path.parent / SESSION / "subagents" / f"agent-{AGENT_ID}.meta.json"
    meta.write_text("{ truncated", encoding="utf-8")
    tree = build_tree(load_session(path))
    sub = node_by(tree, KIND_SUBAGENT)
    assert sub is not None
    assert sub.label == AGENT_ID  # no metadata to label it with


# -------------------------------------------------------------------- store

@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def loaded(tmp_path):
    fx = workflow_fixture()
    path = write_session(tmp_path, fx["records"], agents=fx["agents"],
                         journals=fx["journals"])
    session = load_session(path)
    return session, build_tree(session)


def test_store_round_trips_a_tree(store, tmp_path):
    session, tree = loaded(tmp_path)
    store.save_tree(tree, project_slug=session.project_slug, path=str(session.path))
    rows = store.tree_nodes(tree.session_id)
    assert len(rows) == len(tree.nodes)
    assert rows[0]["kind"] == KIND_SESSION  # build order is preserved
    kinds = {r["kind"] for r in rows}
    assert {KIND_TURN, KIND_TOOL, KIND_WORKFLOW, KIND_SUBAGENT} <= kinds


def test_store_records_are_upserted_not_duplicated(store, tmp_path):
    session, _ = loaded(tmp_path)
    first = store.add_transcript_records(session.records)
    store.add_transcript_records(session.records)
    held = store.conn.execute("SELECT COUNT(*) FROM transcript_records").fetchone()[0]
    assert held == first


def test_saving_a_tree_twice_replaces_it_wholesale(store, tmp_path):
    """A half-rebuilt tree could point a node at a parent that is gone."""
    _, tree = loaded(tmp_path)
    store.save_tree(tree)
    store.save_tree(tree)
    assert len(store.tree_nodes(tree.session_id)) == len(tree.nodes)


def test_a_shrinking_tree_leaves_no_stale_nodes(store, tmp_path):
    _, tree = loaded(tmp_path)
    store.save_tree(tree)
    smaller = build_tree(load_session(write_session(
        tmp_path / "second", [human("a", at=0)], session_id=SESSION,
    )))
    store.save_tree(smaller)
    rows = store.tree_nodes(SESSION)
    assert len(rows) == len(smaller.nodes)
    assert {r["kind"] for r in rows} == {KIND_SESSION, KIND_TURN}


def test_stored_link_basis_survives_the_round_trip(store, tmp_path):
    _, tree = loaded(tmp_path)
    store.save_tree(tree)
    rows = [r for r in store.tree_nodes(tree.session_id) if r["kind"] == KIND_SUBAGENT]
    assert {r["link_basis"] for r in rows} == {LINK_WORKFLOW_JOURNAL}


def test_session_summary_records_what_could_not_be_read(store, tmp_path):
    path = write_session(tmp_path, [human("a")])
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{bad\n")
    session = load_session(path)
    store.save_tree(build_tree(session), project_slug=session.project_slug,
                    path=str(path))
    row = store.transcript_session(SESSION)
    assert row["parse_errors"] == 1
    assert row["project_slug"] == "proj-slug"
    assert json.loads(row["warnings"]) == []


def test_repeated_signatures_surface_identical_calls(store, tmp_path):
    """The substrate Phase 4's loop detector builds on."""
    same = {"file_path": "lib/normalise.py"}
    path = write_session(tmp_path, [
        human("a", at=0),
        call("b", "Read", "t1", parent="a", at=1, tool_input=same),
        call("c", "Read", "t2", parent="b", at=2, tool_input=dict(same)),
        call("d", "Read", "t3", parent="c", at=3, tool_input={"file_path": "other.py"}),
    ])
    store.add_transcript_records(load_session(path).records)
    repeats = store.repeated_signatures(SESSION)
    assert len(repeats) == 1
    assert repeats[0]["n"] == 2
    assert repeats[0]["tool_name"] == "Read"


def test_no_content_reaches_the_database_by_default(store, tmp_path, monkeypatch):
    monkeypatch.delenv("CONTRAIL_CAPTURE_CONTENT", raising=False)
    session, _ = loaded(tmp_path)
    store.add_transcript_records(session.records)
    held = store.conn.execute(
        "SELECT COUNT(*) FROM transcript_records WHERE content IS NOT NULL"
    ).fetchone()[0]
    assert held == 0


def test_transcript_tables_do_not_disturb_the_span_tables(store, tmp_path):
    """The two ingest paths share a store but must not share state."""
    session, tree = loaded(tmp_path)
    store.add_transcript_records(session.records)
    store.save_tree(tree)
    assert store.counts() == {"spans": 0, "runs": 0}


def test_subagent_tokens_are_not_double_counted(tmp_path):
    """The walk folds records in; accumulating them again doubled every
    subagent's cost, which Phase 3 would have inherited."""
    agent = [
        agent_prompt("s1", AGENT_ID, at=3),
        call("s2", "Grep", "st1", parent="s1", at=4, tokens=(100, 7, 11, 13),
             sidechain=True) | {"agentId": AGENT_ID},
        result("s3", "st1", parent="s2", at=5, sidechain=True) | {"agentId": AGENT_ID},
    ]
    tree = tree_for(
        tmp_path,
        [
            human("a", at=0), call("b", "Agent", "t1", parent="a", at=1),
            result("c", "t1", parent="b", at=9,
                   tool_use_result={"agentId": AGENT_ID, "agentType": "Explore"}),
        ],
        agents={AGENT_ID: {"records": agent, "meta": {"agentType": "Explore"}}},
    )
    sub = node_by(tree, KIND_SUBAGENT)
    assert (sub.input_tokens, sub.output_tokens) == (100, 7)
    assert (sub.cache_read_tokens, sub.cache_creation_tokens) == (11, 13)


def test_a_turn_does_not_absorb_its_subagents_tokens(tmp_path):
    """Self cost per node. Rolling up is Phase 3's decision, not the parser's."""
    agent = [
        agent_prompt("s1", AGENT_ID, at=3),
        call("s2", "Grep", "st1", parent="s1", at=4, tokens=(100, 0, 0, 0),
             sidechain=True) | {"agentId": AGENT_ID},
    ]
    tree = tree_for(
        tmp_path,
        [
            human("a", at=0),
            call("b", "Agent", "t1", parent="a", at=1, tokens=(5, 0, 0, 0)),
            result("c", "t1", parent="b", at=9,
                   tool_use_result={"agentId": AGENT_ID, "agentType": "Explore"}),
        ],
        agents={AGENT_ID: {"records": agent, "meta": {"agentType": "Explore"}}},
    )
    assert node_by(tree, KIND_TURN).input_tokens == 5
    assert node_by(tree, KIND_SUBAGENT).input_tokens == 100


# --- Regression: a set written inline as a comma-separated string --------
#
# Two verifier agents given an identical task produced different tool
# signatures whose only difference was the order of a comma-separated list.
# That is the same call, and a loop detector must see it as one.

def test_signature_ignores_the_order_of_an_inline_token_set():
    assert tool_signature("ToolSearch", {"query": "select:WebSearch,WebFetch"}) == (
        tool_signature("ToolSearch", {"query": "select:WebFetch,WebSearch"})
    )


def test_the_prefix_of_an_inline_token_set_is_preserved():
    assert normalise_argument("select:b,a") == "select:a,b"


def test_a_bare_token_list_is_sorted():
    assert normalise_argument("c,a,b") == "a,b,c"


@pytest.mark.parametrize(
    "text",
    [
        "pytest -q, --verbose",      # spaces: a shell command, not a set
        "a/b.py,c/d.py",             # paths: order may matter
        "x=1,y=2",                   # assignments
        "Hello, world, how are you",  # prose
        "a, b",                      # spaced list, left alone deliberately
    ],
)
def test_comma_sorting_leaves_everything_else_alone(text):
    """Over-normalising merges genuinely different calls, which is worse
    than missing a repeat."""
    assert normalise_argument(text) == text


def test_sorting_a_token_list_can_merge_an_ordered_list():
    """The documented trade-off, asserted so it stays a known choice.

    An order-significant bare list collapses to one signature. Acceptable for
    loop detection -- two calls differing only in list order are doing
    near-identical work -- but it is the one normaliser here that can merge
    distinct calls.
    """
    assert normalise_argument("id,name") == normalise_argument("name,id")
