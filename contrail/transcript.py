"""Session transcript parsing and subagent tree reconstruction.

This is Gap 1 from docs/spec.md: traces carry no nested spans for subagent
work, so the tree has to be rebuilt from the transcripts Claude Code writes
to disk.

**This module is deliberately independent of the OTel ingest path.** It
imports nothing from `otlp.py` or `collector.py` and shares no code with
them. Trace export is behind a beta flag and span names can change; when that
happens this path has to keep working. The only shared surface is the store.

The on-disk format is documented in docs/spec.md under "Transcript format, as
verified", recorded from a real data directory rather than from docs. The
three facts that shape this module:

1. Subagents live in their own files under `<session>/subagents/`, never
   inline in the parent transcript.
2. The DAG edge is `uuid` / `parentUuid`. Tool calls and their results join on
   `tool_use_id`, with the result arriving on a `user` record.
3. There are two spawn mechanisms with different link paths, and the obvious
   one covers a small minority of files -- see `LINK_*` below.

No prompt or tool-argument text is captured unless the caller opts in. What
is stored instead is a *normalised signature*: the tool name plus its
canonicalised arguments, hashed. That is non-reversible, so it carries the
same privacy properties as storing nothing, but unlike a raw hash it is
still useful to Phase 4 -- two calls that differ only in whitespace or path
separators produce the same signature, which is what loop detection needs.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PureWindowsPath
from typing import Any

# --- how a subagent got linked to its parent ---------------------------------
#
# Recorded per node as `link_basis` so that a wrong tree is debuggable later
# rather than mysterious. Listed in the order they are attempted.
LINK_TOOL_USE_RESULT = "tool_use_result"    # parent's toolUseResult named the agentId
LINK_META_TOOL_USE_ID = "meta_tool_use_id"  # meta.json pointed back at the tool_use
LINK_WORKFLOW_JOURNAL = "workflow_journal"  # the run's journal.jsonl named the agentId
LINK_DIRECTORY = "directory"                # fallback: file sits under this session
LINK_NONE = "none"                          # structural nodes; nothing to link

# --- node kinds --------------------------------------------------------------
KIND_SESSION = "session"
KIND_TURN = "turn"
KIND_TOOL = "tool"
KIND_SUBAGENT = "subagent"
KIND_WORKFLOW = "workflow"

# --- node statuses -----------------------------------------------------------
STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_INCOMPLETE = "incomplete"          # tool call with no result on disk
STATUS_PENDING = "pending"                # async_launched; may still be running
STATUS_MISSING_TRANSCRIPT = "missing_transcript"  # meta present, .jsonl absent

# Record types that carry a `message`. The other ten types observed on disk
# are harness bookkeeping -- see docs/spec.md.
MESSAGE_TYPES = frozenset({"assistant", "user"})

# Tools that spawn a subagent. Both names are accepted because the tool was
# renamed across the CLI versions in the surveyed corpus.
AGENT_TOOLS = frozenset({"Agent", "Task"})
WORKFLOW_TOOLS = frozenset({"Workflow"})

CAPTURE_CONTENT_ENV = "CONTRAIL_CAPTURE_CONTENT"

# Arguments naming the thing a call acts on. Stored only as a hash, so
# "same target" comparisons work without keeping the path itself.
TARGET_KEYS = ("file_path", "notebook_path", "path")

# Keys on `toolUseResult` that vary per invocation without the *result*
# varying. Left in, two identical calls hash differently and a repeat stops
# being detected -- silently. tests/test_detectors.py hashes a known-identical
# pair and fails if they diverge, which is the guard against this list rotting.
VOLATILE_RESULT_KEYS = frozenset({
    "backgroundTaskId",
    "backgroundCwdHint",
    "timedOutAfterMs",
    "persistedOutputPath",
    "persistedOutputSize",
})

# A background task's output file. The id in the path is the same id the
# launching call reports as `backgroundTaskId`, which is what lets a re-read
# of a still-running task be told apart from a genuinely redundant one.
_TASK_OUTPUT = re.compile(r"/tasks/([A-Za-z0-9_-]+)\.output$")

_WHITESPACE = re.compile(r"\s+")

# A comma-separated run of bare tokens, with an optional `name:` prefix --
# the shape of a set passed as a string, e.g. ToolSearch's
# `select:WebFetch,WebSearch`. A token deliberately excludes space, `/`, `\`,
# `=` and `:`, so paths, assignments and prose never match.
_TOKEN_LIST = re.compile(
    r"^(?P<prefix>[A-Za-z_][A-Za-z0-9_.\-]*:)?"
    r"(?P<items>[A-Za-z0-9_.+\-]+(?:,[A-Za-z0-9_.+\-]+)+)$"
)


def capture_content_enabled() -> bool:
    """Content capture is opt-in and off by default. See CLAUDE.md."""
    return os.environ.get(CAPTURE_CONTENT_ENV, "").strip().lower() in {"1", "true", "yes"}


def default_projects_root() -> Path:
    """Where Claude Code keeps its per-project session transcripts."""
    override = os.environ.get("CONTRAIL_CLAUDE_DIR")
    base = Path(override) if override else Path.home() / ".claude"
    return base / "projects"


# ---------------------------------------------------------------- signatures

def _sort_token_list(text: str) -> str:
    """Sort a comma-separated run of bare tokens, preserving any prefix."""
    match = _TOKEN_LIST.match(text)
    if match is None:
        return text
    items = sorted(match.group("items").split(","))
    return (match.group("prefix") or "") + ",".join(items)


def normalise_argument(value: Any) -> Any:
    """Canonicalise one tool argument for signature hashing.

    Deliberately conservative. Two calls with the same *intent* rarely have
    the same bytes, but over-normalising collapses calls that genuinely
    differ, and a loop detector that fires on distinct work is worse than one
    that misses a repeat. So this only removes differences that cannot carry
    meaning: surrounding whitespace, internal whitespace runs, and path
    separator direction (the same file is read as both `a\b` and `a/b`
    depending on which tool wrote the call).

    Case is preserved -- it is significant in code, identifiers and paths on
    the platforms we care about.

    It also sorts a comma-separated run of bare tokens, because such a string
    is a *set* written inline and its order carries no meaning:
    `select:WebSearch,WebFetch` and `select:WebFetch,WebSearch` are the same
    call. Measured need for this -- two verifier agents given an identical
    task produced different signatures whose only difference was that
    ordering. The token pattern excludes anything containing a space, a
    forward or back slash, `=` or `:`, so paths, shell commands, assignments
    and prose are left alone.

    The trade-off is stated rather than hidden: an order-significant bare
    list (a CSV column order, say) collapses to one signature. For loop
    detection that is acceptable -- two calls differing only in list order
    are doing near-identical work -- but it is the one place here that can
    merge genuinely distinct calls, so extend it carefully.

    This function is the seam Phase 4 extends. docs/spec.md flags argument
    normalisation as the hard part of loop detection, and expects the first
    version to be wrong; adding a normaliser here should stay one place.
    """
    if isinstance(value, str):
        text = _WHITESPACE.sub(" ", value.replace("\\", "/")).strip()
        return _sort_token_list(text)
    if isinstance(value, dict):
        # Sorted so key order in the JSONL cannot change the signature.
        return {k: normalise_argument(value[k]) for k in sorted(value, key=str)}
    if isinstance(value, list):
        return [normalise_argument(v) for v in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    return normalise_argument(str(value))


def _short_hash(blob: str) -> str:
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def target_of(tool_input: Any) -> str | None:
    """The normalised path a call acts on, if its arguments name one."""
    if not isinstance(tool_input, dict):
        return None
    for key in TARGET_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value.replace("\\", "/").strip().lower()
    return None


def target_hash(tool_input: Any) -> str | None:
    """Non-reversible identity for a call's target. Never the path itself."""
    target = target_of(tool_input)
    return _short_hash(target) if target else None


def background_task_id(tool_input: Any, tool_use_result: Any) -> str | None:
    """The background task this call launched, or whose output it reads.

    Both sides are recorded so a poll can be recognised: the launching call
    reports the id in its result, and a read of that task's output carries the
    same id in its path.
    """
    if isinstance(tool_use_result, dict):
        value = tool_use_result.get("backgroundTaskId")
        if isinstance(value, str) and value:
            return value
    target = target_of(tool_input)
    if target:
        match = _TASK_OUTPUT.search(target)
        if match:
            return match.group(1)
    return None


def result_hash(tool_use_result: Any) -> str | None:
    """Non-reversible identity for a tool result. Never the result itself.

    `toolUseResult` is a dict on success and a plain string on error, and both
    must hash -- an earlier version only handled dicts and reported every
    failed call as having no result at all.

    Per-invocation keys are excluded, so two calls that did the same thing
    and got the same answer hash the same even when the harness stamped a
    fresh task id on each.
    """
    if tool_use_result is None:
        return None
    payload = tool_use_result
    if isinstance(payload, dict):
        payload = {
            k: v for k, v in payload.items() if k not in VOLATILE_RESULT_KEYS
        }
    return _short_hash(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    )


def tool_signature(tool_name: str | None, tool_input: Any) -> str:
    """A stable, non-reversible signature for one tool call.

    Identical normalised calls produce an identical signature, which is what
    Phase 4's loop detector keys on. The hash is truncated to 16 hex chars:
    ample against accidental collision at the scale of a single run, and short
    enough to read in a query result.
    """
    canonical = json.dumps(
        {"tool": tool_name or "", "input": normalise_argument(tool_input)},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- timestamps

def parse_ts_ns(value: Any) -> int:
    """ISO-8601 timestamp to nanoseconds, to match the span columns.

    Returns 0 rather than raising: a record with an unreadable timestamp is
    still a record, and ordering degrades to file order.
    """
    if not isinstance(value, str) or not value:
        return 0
    try:
        text = value.replace("Z", "+00:00")
        return int(datetime.fromisoformat(text).timestamp() * 1_000_000_000)
    except ValueError:
        return 0


# ------------------------------------------------------------------- records

@dataclass
class TranscriptRecord:
    """One `assistant` or `user` line from a transcript file.

    Derived fields are denormalised onto the record for the same reason
    `Span` does it: it keeps the store's queries simple.
    """

    uuid: str
    parent_uuid: str | None
    session_id: str
    agent_id: str | None
    type: str
    timestamp: str
    ts_ns: int
    is_sidechain: bool = False

    # --- assistant only ------------------------------------------------
    model: str | None = None
    request_id: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    # Cache writes price differently by TTL, so the split is kept.
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0
    # Billed inside output_tokens; recorded for analysis, never added to cost.
    thinking_tokens: int = 0
    service_tier: str | None = None

    # --- tool call (assistant) -----------------------------------------
    tool_use_id: str | None = None
    tool_name: str | None = None
    tool_signature: str | None = None
    # Hashes, never the values. See `target_hash` / `result_hash`.
    target_hash: str | None = None
    background_task_id: str | None = None

    # --- tool result (user) --------------------------------------------
    is_tool_result: bool = False
    is_error: bool = False
    result_hash: str | None = None
    result_status: str | None = None
    result_agent_id: str | None = None
    result_agent_type: str | None = None
    result_run_id: str | None = None
    result_transcript_dir: str | None = None
    source_tool_assistant_uuid: str | None = None

    # --- structure, not content ----------------------------------------
    text_len: int = 0
    is_meta: bool = False

    # Populated only when CONTRAIL_CAPTURE_CONTENT is set.
    content: str | None = None

    @property
    def is_human_turn(self) -> bool:
        """A user record that starts a turn, rather than replaying a result."""
        return self.type == "user" and not self.is_tool_result and not self.is_meta


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _usage_tokens(usage: dict[str, Any]) -> dict[str, int]:
    """Token counts from `message.usage`, kept apart by how they price.

    Cache reads and the two cache-write TTLs are returned separately and are
    never summed into input. Relative to base input they cost roughly 0.1x,
    1.25x (5m) and 2x (1h), so collapsing any of them makes correct pricing
    impossible -- which is exactly what the earlier schema did.

    `thinking_tokens` is billed *within* `output_tokens`, so it is reported
    for analysis and must never be added to a cost sum.

    `iterations` is a per-attempt breakdown that sums to the top level
    (verified 10,578/10,578 on real data), so the top level is authoritative
    and retries carry no double-count risk.
    """
    creation = usage.get("cache_creation")
    creation = creation if isinstance(creation, dict) else {}
    details = usage.get("output_tokens_details")
    details = details if isinstance(details, dict) else {}

    total_creation = _int(usage.get("cache_creation_input_tokens"))
    write_5m = _int(creation.get("ephemeral_5m_input_tokens"))
    write_1h = _int(creation.get("ephemeral_1h_input_tokens"))

    # The split sums to the total on 12,063/12,063 real records. When the
    # sub-dict is absent entirely, fall back to charging the whole amount at
    # the 5m rate: it is the cheaper of the two, so an unknown TTL cannot
    # silently inflate a cost figure.
    if not creation and total_creation:
        write_5m = total_creation

    return {
        "input_tokens": _int(usage.get("input_tokens")),
        "output_tokens": _int(usage.get("output_tokens")),
        "cache_read_tokens": _int(usage.get("cache_read_input_tokens")),
        "cache_creation_tokens": total_creation,
        "cache_write_5m_tokens": write_5m,
        "cache_write_1h_tokens": write_1h,
        "thinking_tokens": _int(details.get("thinking_tokens")),
    }


def parse_record(raw: dict[str, Any]) -> TranscriptRecord | None:
    """One raw JSONL object to a record, or None if it is not a message.

    Unknown record types are skipped rather than rejected: the corpus already
    contains ten bookkeeping types across CLI versions 2.1.121 to 2.1.266, and
    new ones appearing must not break the parse.
    """
    rtype = raw.get("type")
    if rtype not in MESSAGE_TYPES:
        return None
    uuid = raw.get("uuid")
    if not uuid:
        return None  # unaddressable; caller counts it as a parse error

    message = raw.get("message") or {}
    blocks = message.get("content")
    blocks = blocks if isinstance(blocks, list) else []

    rec = TranscriptRecord(
        uuid=uuid,
        parent_uuid=raw.get("parentUuid"),
        session_id=raw.get("sessionId") or "",
        agent_id=raw.get("agentId"),
        type=rtype,
        timestamp=raw.get("timestamp") or "",
        ts_ns=parse_ts_ns(raw.get("timestamp")),
        is_sidechain=bool(raw.get("isSidechain")),
        is_meta=bool(raw.get("isMeta")),
        model=message.get("model"),
        request_id=raw.get("requestId"),
        source_tool_assistant_uuid=raw.get("sourceToolAssistantUUID"),
    )

    usage = message.get("usage")
    if isinstance(usage, dict):
        for field_name, value in _usage_tokens(usage).items():
            setattr(rec, field_name, value)
        tier = usage.get("service_tier")
        rec.service_tier = tier if isinstance(tier, str) else None

    # Measured on the real corpus: exactly one tool_use per assistant record
    # and one tool_result per user record, so first-match is not a shortcut.
    # It is still written as a loop, because that is an observation about
    # today's harness rather than a guarantee.
    keep_content = capture_content_enabled()
    text_len = 0
    for block in blocks:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text_len += len(block.get("text") or "")
        elif btype == "tool_use" and rec.tool_use_id is None:
            rec.tool_use_id = block.get("id")
            rec.tool_name = block.get("name")
            tool_input = block.get("input")
            rec.tool_signature = tool_signature(block.get("name"), tool_input)
            rec.target_hash = target_hash(tool_input)
            rec.background_task_id = background_task_id(tool_input, None)
            if keep_content:
                rec.content = json.dumps(tool_input, default=str)
        elif btype == "tool_result" and not rec.is_tool_result:
            rec.is_tool_result = True
            rec.tool_use_id = block.get("tool_use_id")
            # `is_error` is absent on most results; absent means not an error.
            rec.is_error = block.get("is_error") is True
            body = block.get("content")
            text_len += len(body) if isinstance(body, str) else 0
            if keep_content and isinstance(body, str):
                rec.content = body

    # Subagent prompts arrive as a bare string rather than a block list.
    if isinstance(message.get("content"), str):
        text_len = len(message["content"])
        if keep_content:
            rec.content = message["content"]

    rec.text_len = text_len

    tur = raw.get("toolUseResult")
    if tur is not None:
        rec.result_hash = result_hash(tur)
        rec.background_task_id = rec.background_task_id or background_task_id(None, tur)
    if isinstance(tur, dict):
        rec.result_status = tur.get("status")
        rec.result_agent_id = tur.get("agentId")
        rec.result_agent_type = tur.get("agentType")
        rec.result_run_id = tur.get("runId")
        rec.result_transcript_dir = tur.get("transcriptDir")

    return rec


# --------------------------------------------------------------- file loading

@dataclass
class LoadedFile:
    """Records from one transcript file, plus what could not be read."""

    path: Path
    records: list[TranscriptRecord] = field(default_factory=list)
    parse_errors: int = 0
    skipped: int = 0
    duplicates: int = 0


def iter_json_lines(path: Path) -> Iterator[tuple[dict[str, Any] | None, bool]]:
    """Yield `(object, ok)` per line. A bad line yields `(None, False)`.

    Never raises on malformed content. A truncated final line is normal: the
    file may be open and appending while we read it.
    """
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                yield None, False
                continue
            yield (obj, True) if isinstance(obj, dict) else (None, False)


def load_file(path: Path) -> LoadedFile:
    """Parse one transcript file into records, counting what was dropped."""
    out = LoadedFile(path=path)
    seen: list[TranscriptRecord] = []
    for obj, ok in iter_json_lines(path):
        if not ok or obj is None:
            out.parse_errors += 1
            continue
        if obj.get("type") not in MESSAGE_TYPES:
            out.skipped += 1
            continue
        rec = parse_record(obj)
        if rec is None:
            out.parse_errors += 1  # a message record we could not address
            continue
        seen.append(rec)

    # A session file can carry the same `uuid` on more than one line: it is
    # appended to across resumes, and a line can be rewritten. Measured on
    # the real corpus at 2 duplicates in a 7,059-record session. Last write
    # wins, matching how the store upserts spans for the same reason.
    by_uuid: dict[str, TranscriptRecord] = {}
    for rec in seen:
        if rec.uuid in by_uuid:
            out.duplicates += 1
        by_uuid[rec.uuid] = rec

    out.records = sorted(by_uuid.values(), key=lambda r: (r.ts_ns, r.uuid))
    return out


def _read_json(path: Path) -> dict[str, Any]:
    """Small JSON sidecar, or `{}` if unreadable. Never raises."""
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


@dataclass
class AgentTranscript:
    """One subagent's own file, plus its `.meta.json` sidecar."""

    agent_id: str
    path: Path | None
    meta: dict[str, Any] = field(default_factory=dict)
    records: list[TranscriptRecord] = field(default_factory=list)
    parse_errors: int = 0
    workflow_run_id: str | None = None

    @property
    def agent_type(self) -> str | None:
        return self.meta.get("agentType")

    @property
    def description(self) -> str | None:
        """A short label written by the caller. Not prompt content."""
        return self.meta.get("description")

    @property
    def meta_tool_use_id(self) -> str | None:
        return self.meta.get("toolUseId")

    @property
    def exists(self) -> bool:
        return self.path is not None and bool(self.records)


@dataclass
class SessionTranscript:
    """A parent session file and every subagent transcript beneath it."""

    session_id: str
    path: Path
    project_slug: str
    records: list[TranscriptRecord] = field(default_factory=list)
    agents: dict[str, AgentTranscript] = field(default_factory=dict)
    # workflow run id -> {agentId: {"started": bool, "result": bool}}
    journals: dict[str, dict[str, dict[str, bool]]] = field(default_factory=dict)
    parse_errors: int = 0
    duplicates: int = 0


def _load_journal(path: Path) -> dict[str, dict[str, bool]]:
    """Read a workflow `journal.jsonl` into per-agent start/result flags.

    The journal is the authoritative membership list for a workflow run: on
    the surveyed corpus it named all 124 workflow-spawned agents and nothing
    that was not on disk. It carries no agent-to-agent edge, so workflow
    children nest under the workflow node rather than under each other.

    Pairing `started` with `result` also gives completion per child for free,
    which is more reliable than inferring it from the transcript tail.
    """
    agents: dict[str, dict[str, bool]] = {}
    for obj, ok in iter_json_lines(path):
        if not ok or obj is None:
            continue
        agent_id = obj.get("agentId")
        if not agent_id:
            continue
        entry = agents.setdefault(agent_id, {"started": False, "result": False})
        if obj.get("type") == "started":
            entry["started"] = True
        elif obj.get("type") == "result":
            entry["result"] = True
    return agents


def load_session(session_path: Path) -> SessionTranscript:
    """Load a session transcript and every subagent transcript beneath it.

    Layout is `<session-id>.jsonl` beside a `<session-id>/` directory. A
    session with no subagents simply has no directory.
    """
    session_id = session_path.stem
    loaded = load_file(session_path)
    session = SessionTranscript(
        session_id=session_id,
        path=session_path,
        project_slug=session_path.parent.name,
        records=loaded.records,
        parse_errors=loaded.parse_errors,
        duplicates=loaded.duplicates,
    )

    subagents_dir = session_path.parent / session_id / "subagents"
    if not subagents_dir.is_dir():
        return session

    # Direct subagents sit in subagents/; workflow children sit one level
    # deeper in subagents/workflows/wf_<runId>/.
    for agent_path in sorted(subagents_dir.rglob("agent-*.jsonl")):
        agent_id = agent_path.stem[len("agent-"):]
        parent_dir = agent_path.parent
        run_id = parent_dir.name if parent_dir.name.startswith("wf_") else None
        agent_loaded = load_file(agent_path)
        session.agents[agent_id] = AgentTranscript(
            agent_id=agent_id,
            path=agent_path,
            meta=_read_json(agent_path.with_suffix(".meta.json")),
            records=agent_loaded.records,
            parse_errors=agent_loaded.parse_errors,
            workflow_run_id=run_id,
        )
        session.parse_errors += agent_loaded.parse_errors
        session.duplicates += agent_loaded.duplicates

    # A sidecar with no transcript beside it. The subagent demonstrably ran
    # -- something wrote its metadata -- so it is registered with no records
    # and surfaces as `missing_transcript` rather than disappearing.
    for meta_path in sorted(subagents_dir.rglob("agent-*.meta.json")):
        agent_id = meta_path.name[len("agent-"):-len(".meta.json")]
        if agent_id in session.agents:
            continue
        parent_dir = meta_path.parent
        session.agents[agent_id] = AgentTranscript(
            agent_id=agent_id,
            path=None,
            meta=_read_json(meta_path),
            workflow_run_id=(
                parent_dir.name if parent_dir.name.startswith("wf_") else None
            ),
        )

    for journal_path in sorted(subagents_dir.rglob("journal.jsonl")):
        run_id = journal_path.parent.name
        session.journals[run_id] = _load_journal(journal_path)

    return session


def discover_sessions(projects_root: Path | None = None) -> list[Path]:
    """Every session transcript under the Claude Code data directory.

    Sorted newest first, so a caller that only wants recent runs can slice.
    """
    root = projects_root or default_projects_root()
    if not root.is_dir():
        return []
    paths = [p for p in root.glob("*/*.jsonl") if p.is_file()]
    return sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True)


# --------------------------------------------------------------------- tree

@dataclass
class TreeNode:
    """One node of a reconstructed run tree.

    Token counts here are the raw sums of the assistant records in this
    node's own scope -- self cost, not rolled up. Walking the tree to
    attribute cost to the node that caused it is Phase 3's job, and doing it
    here would bake in an attribution policy before that phase can choose one.
    """

    node_id: str
    parent_node_id: str | None
    kind: str
    label: str
    session_id: str
    depth: int = 0
    agent_id: str | None = None
    agent_type: str | None = None
    tool_use_id: str | None = None
    tool_name: str | None = None
    tool_signature: str | None = None
    record_uuid: str | None = None
    link_basis: str = LINK_NONE
    status: str = STATUS_OK
    note: str = ""
    start_ns: int = 0
    end_ns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0
    thinking_tokens: int = 0
    record_count: int = 0
    children: list[TreeNode] = field(default_factory=list)

    @property
    def duration_ms(self) -> float:
        if not self.end_ns or not self.start_ns:
            return 0.0
        return max(0.0, (self.end_ns - self.start_ns) / 1_000_000)


@dataclass
class SessionTree:
    """A reconstructed session, plus what could not be reconstructed."""

    session_id: str
    root: TreeNode
    nodes: list[TreeNode]
    parse_errors: int = 0
    warnings: list[str] = field(default_factory=list)
    # record uuid -> node_id whose self tokens that record contributed to.
    # The walk already decides this when it folds a record into a node;
    # exposing it means Phase 3 can price per record (a node's records can
    # span models) and can check that every record landed on exactly one
    # node, which is the attribution invariant.
    record_scope: dict[str, str] = field(default_factory=dict)

    def by_kind(self, kind: str) -> list[TreeNode]:
        return [n for n in self.nodes if n.kind == kind]

    @property
    def link_bases(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for node in self.nodes:
            if node.kind == KIND_SUBAGENT:
                counts[node.link_basis] = counts.get(node.link_basis, 0) + 1
        return counts


def _accumulate(
    node: TreeNode,
    rec: TranscriptRecord,
    scope: dict[str, str] | None = None,
) -> None:
    """Fold one record's timing and tokens into its enclosing node."""
    if scope is not None:
        scope[rec.uuid] = node.node_id
    if rec.ts_ns:
        node.start_ns = rec.ts_ns if not node.start_ns else min(node.start_ns, rec.ts_ns)
        node.end_ns = max(node.end_ns, rec.ts_ns)
    node.input_tokens += rec.input_tokens
    node.output_tokens += rec.output_tokens
    node.cache_read_tokens += rec.cache_read_tokens
    node.cache_creation_tokens += rec.cache_creation_tokens
    node.cache_write_5m_tokens += rec.cache_write_5m_tokens
    node.cache_write_1h_tokens += rec.cache_write_1h_tokens
    node.thinking_tokens += rec.thinking_tokens
    node.record_count += 1


def _extend_ancestors(node: TreeNode, index: dict[str, TreeNode]) -> None:
    """Widen every ancestor's time extent to contain `node`.

    Done as a separate pass rather than during the walk because a subagent's
    records are in another file and are not seen in parent order.
    """
    parent_id = node.parent_node_id
    seen: set[str] = {node.node_id}
    while parent_id and parent_id not in seen:
        seen.add(parent_id)
        parent = index.get(parent_id)
        if parent is None:
            return
        if node.start_ns:
            parent.start_ns = (
                node.start_ns if not parent.start_ns
                else min(parent.start_ns, node.start_ns)
            )
        parent.end_ns = max(parent.end_ns, node.end_ns)
        parent_id = parent.parent_node_id


class _TreeBuilder:
    """Builds one session's tree. One instance per build_tree() call.

    A class rather than a nest of closures because the walk recurses into
    subagent files and needs shared bookkeeping: which agents have been
    claimed, which tool node owns which tool_use_id, and a guard against an
    agent transcript being visited twice.
    """

    def __init__(self, session: SessionTranscript) -> None:
        self.session = session
        self.nodes: list[TreeNode] = []
        self.index: dict[str, TreeNode] = {}
        # Keyed by (agent_id, tool_use_id). A tool_use_id is unique within
        # one transcript but nothing guarantees it across files, and an
        # unscoped key let a subagent's result close its parent's tool call.
        self.tool_nodes: dict[tuple[str | None, str], TreeNode] = {}
        self.claimed: set[str] = set()
        self.warnings: list[str] = []
        self.record_scope: dict[str, str] = {}
        self.root = self._add(
            TreeNode(
                node_id=f"session:{session.session_id}",
                parent_node_id=None,
                kind=KIND_SESSION,
                label=session.session_id,
                session_id=session.session_id,
                depth=0,
            )
        )

    def _add(self, node: TreeNode) -> TreeNode:
        self.nodes.append(node)
        self.index[node.node_id] = node
        return node

    def _attach(self, parent: TreeNode, node: TreeNode) -> TreeNode:
        node.parent_node_id = parent.node_id
        node.depth = parent.depth + 1
        parent.children.append(node)
        return self._add(node)

    # ------------------------------------------------------------ the walk

    def build(self) -> SessionTree:
        self._walk(self.session.records, self.root, turns=True)
        self._link_by_meta()
        self._attach_unclaimed()
        self._finalise()
        return SessionTree(
            session_id=self.session.session_id,
            root=self.root,
            nodes=self.nodes,
            parse_errors=self.session.parse_errors,
            warnings=self.warnings,
            record_scope=self.record_scope,
        )

    def _walk(
        self, records: list[TranscriptRecord], scope: TreeNode, *, turns: bool
    ) -> None:
        """Attach the tool calls in `records` beneath `scope`.

        `turns` is True only for the parent session: a human prompt opens a
        turn node there. A subagent has exactly one prompt, so its tools hang
        directly off the subagent node and a turn level would be noise.
        """
        current = scope
        turn_n = 0

        for rec in records:
            if turns and rec.is_human_turn:
                turn_n += 1
                current = self._attach(
                    scope,
                    TreeNode(
                        node_id=f"turn:{rec.uuid}",
                        parent_node_id=None,
                        kind=KIND_TURN,
                        label=f"turn {turn_n}",
                        session_id=self.session.session_id,
                        record_uuid=rec.uuid,
                        start_ns=rec.ts_ns,
                        end_ns=rec.ts_ns,
                    ),
                )
                _accumulate(current, rec, self.record_scope)
                continue

            if rec.type == "assistant" and rec.tool_use_id and not rec.is_tool_result:
                self._open_tool(rec, current)
                _accumulate(current, rec, self.record_scope)
                continue

            if rec.is_tool_result and rec.tool_use_id:
                self._close_tool(rec)
                continue

            _accumulate(current, rec, self.record_scope)

    def _open_tool(self, rec: TranscriptRecord, scope: TreeNode) -> None:
        node = self._attach(
            scope,
            TreeNode(
                node_id=f"tool:{rec.agent_id or 'root'}:{rec.tool_use_id}",
                parent_node_id=None,
                kind=KIND_TOOL,
                label=rec.tool_name or "tool",
                session_id=self.session.session_id,
                agent_id=rec.agent_id,
                tool_use_id=rec.tool_use_id,
                tool_name=rec.tool_name,
                tool_signature=rec.tool_signature,
                record_uuid=rec.uuid,
                start_ns=rec.ts_ns,
                end_ns=rec.ts_ns,
                # Until a result is seen the call is unfinished. 5,282 calls
                # to 5,280 results on the real corpus, so this does happen.
                status=STATUS_INCOMPLETE,
            ),
        )
        if rec.tool_use_id:
            self.tool_nodes.setdefault((rec.agent_id, rec.tool_use_id), node)

    def _close_tool(self, rec: TranscriptRecord) -> None:
        node = self.tool_nodes.get((rec.agent_id, rec.tool_use_id or ""))
        if node is None:
            # A result whose call is not in this file. Recorded rather than
            # dropped: it means the transcript we have is partial.
            self.warnings.append(
                f"tool_result with no matching call: {rec.tool_use_id}"
            )
            return

        node.end_ns = max(node.end_ns, rec.ts_ns)
        if rec.is_error:
            node.status = STATUS_ERROR
        elif rec.result_status == "async_launched":
            # A workflow launched in the background. It may still be running,
            # so the node is explicitly pending rather than quietly "ok".
            node.status = STATUS_PENDING
        else:
            node.status = STATUS_OK

        self._spawn_from_result(rec, node)

    # ------------------------------------------------------------- spawning

    def _spawn_from_result(self, rec: TranscriptRecord, tool_node: TreeNode) -> None:
        """Create subagent or workflow children under a finished tool call."""
        if rec.result_agent_id:
            self._add_subagent(
                rec.result_agent_id,
                tool_node,
                LINK_TOOL_USE_RESULT,
                agent_type=rec.result_agent_type,
            )
            return

        run_id = rec.result_run_id or self._run_id_from_dir(rec.result_transcript_dir)
        if run_id:
            self._add_workflow(run_id, tool_node, rec)

    @staticmethod
    def _run_id_from_dir(transcript_dir: str | None) -> str | None:
        """transcriptDir ends in the run id; use it when runId was absent."""
        if not transcript_dir:
            return None
        # PureWindowsPath, not PurePath: the recorded value is a Windows path
        # and must still split correctly when parsed on another platform.
        name = PureWindowsPath(transcript_dir.replace("/", "\\")).name
        return name if name.startswith("wf_") else None

    def _add_workflow(
        self, run_id: str, tool_node: TreeNode, rec: TranscriptRecord
    ) -> None:
        node = self._attach(
            tool_node,
            TreeNode(
                node_id=f"workflow:{run_id}",
                parent_node_id=None,
                kind=KIND_WORKFLOW,
                label=run_id,
                session_id=self.session.session_id,
                tool_use_id=rec.tool_use_id,
                record_uuid=rec.uuid,
                link_basis=LINK_WORKFLOW_JOURNAL,
                status=tool_node.status,
                start_ns=rec.ts_ns,
                end_ns=rec.ts_ns,
            ),
        )

        journal = self.session.journals.get(run_id)
        if journal is None:
            # No journal: fall back to whichever agent files sit in that
            # directory. Recorded, because membership is then a guess.
            members = {
                aid: {"started": True, "result": True}
                for aid, agent in self.session.agents.items()
                if agent.workflow_run_id == run_id
            }
            if members:
                self.warnings.append(
                    f"workflow {run_id}: no journal, membership from directory"
                )
                node.note = "membership from directory, not journal"
        else:
            members = journal

        unfinished = 0
        for agent_id, flags in sorted(members.items()):
            child = self._add_subagent(agent_id, node, LINK_WORKFLOW_JOURNAL)
            if child is not None and not flags.get("result"):
                # Started but never returned: still running, or abandoned.
                child.status = STATUS_INCOMPLETE
                child.note = "journal recorded a start with no result"
                unfinished += 1

        # The tool call itself stays pending -- it really did return
        # async_launched. But the journal knows whether the run then
        # finished, which is better information than inheriting that pending.
        if members:
            node.status = STATUS_INCOMPLETE if unfinished else STATUS_OK
            if unfinished:
                node.note = f"{unfinished} of {len(members)} children unfinished"

    def _add_subagent(
        self,
        agent_id: str,
        parent: TreeNode,
        link_basis: str,
        agent_type: str | None = None,
    ) -> TreeNode | None:
        if agent_id in self.claimed:
            # Guards against one agent id appearing in two places, which
            # would otherwise recurse without end.
            self.warnings.append(f"agent {agent_id} referenced more than once")
            return None
        self.claimed.add(agent_id)

        agent = self.session.agents.get(agent_id)
        node = self._attach(
            parent,
            TreeNode(
                node_id=f"agent:{agent_id}",
                parent_node_id=None,
                kind=KIND_SUBAGENT,
                # description is a caller-written label, like a span name --
                # not prompt content. The prompt itself is never used here.
                label=(
                    (agent.description if agent else None)
                    or (agent.agent_type if agent else None)
                    or agent_type
                    or agent_id
                ),
                session_id=self.session.session_id,
                agent_id=agent_id,
                agent_type=(agent.agent_type if agent else None) or agent_type,
                link_basis=link_basis,
            ),
        )

        if agent is None or not agent.exists:
            # The parent says a subagent ran, but its transcript is not on
            # disk. Keep the node: a tree that says "one subagent
            # unresolved" is useful, one that silently omits it is a lie.
            node.status = STATUS_MISSING_TRANSCRIPT
            node.note = "referenced by parent but no transcript on disk"
            self.warnings.append(f"agent {agent_id}: no transcript on disk")
            return node

        # Records are folded in by the walk below, not here: doing both
        # double-counted every subagent's tokens, which Phase 3 would have
        # inherited as doubled cost.
        #
        # Recurse: a subagent may spawn its own. Not present in the surveyed
        # corpus (spawnDepth was only ever 1), but the format allows it and
        # the claimed set makes the recursion safe.
        self._walk(agent.records, node, turns=False)
        return node

    # ------------------------------------------------- fallbacks and cleanup

    def _link_by_meta(self) -> None:
        """Second join: meta.json toolUseId points back at a tool call.

        Only 22 of 148 sidecars carry it, so this corroborates the Agent path
        rather than driving it. It earns its place by resolving an agent whose
        parent record is missing while its sidecar survived.
        """
        for agent_id, agent in sorted(self.session.agents.items()):
            if agent_id in self.claimed:
                continue
            tool_use_id = agent.meta_tool_use_id
            if not tool_use_id:
                continue
            tool_node = self.tool_nodes.get((None, tool_use_id))
            if tool_node is not None:
                self._add_subagent(agent_id, tool_node, LINK_META_TOOL_USE_ID)

    def _attach_unclaimed(self) -> None:
        """Last resort: the file sits under this session's directory.

        On the surveyed corpus this never fires -- toolUseResult.agentId and
        journal membership together covered all 148 files. It exists so a
        subagent whose parent reference is missing still appears in the tree,
        flagged, instead of vanishing.
        """
        for agent_id, agent in sorted(self.session.agents.items()):
            if agent_id in self.claimed:
                continue
            parent = self.root
            if agent.workflow_run_id:
                parent = self.index.get(f"workflow:{agent.workflow_run_id}", self.root)
            node = self._add_subagent(agent_id, parent, LINK_DIRECTORY)
            if node is not None:
                node.note = "no parent reference found; linked by directory"
                self.warnings.append(f"agent {agent_id}: linked by directory only")

    def _finalise(self) -> None:
        """Propagate time extents upward and settle the root."""
        for node in self.nodes:
            if node.kind in (KIND_SUBAGENT, KIND_TOOL, KIND_WORKFLOW):
                _extend_ancestors(node, self.index)
        if not self.root.start_ns and self.session.records:
            self.root.start_ns = self.session.records[0].ts_ns
            self.root.end_ns = self.session.records[-1].ts_ns


def build_tree(session: SessionTranscript) -> SessionTree:
    """Reconstruct one session's run tree from its loaded transcripts.

    Pure over the loaded session: no filesystem access and no store. That
    keeps it testable against small fixtures, and keeps it on the same
    footing as Phase 4's detectors, which must be pure functions over a tree.
    """
    return _TreeBuilder(session).build()
