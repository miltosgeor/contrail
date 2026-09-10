# CLAUDE.md

Project context for Claude Code. Read `docs/spec.md` for the full reasoning
behind any decision here.

## What this is

Contrail is a trace store and analysis layer for Claude agent runs. Claude
Code already exports OpenTelemetry traces — collection is a solved problem
and we are not rebuilding it. The value is entirely downstream: turning that
telemetry into answers about where runs loop, leak cost, and fail quietly.

**The UI is the front end of a data model, not the product.** If a change
makes the screen nicer without making the analysis better, it is the wrong
change.

## Current state

Phases 1-2 complete. 110 tests pass. Phase 2 was verified against a real
Claude Code data directory: 5 sessions and 148 subagent transcripts
reconstruct with zero orphans and zero unreadable lines.

| Phase | | Status |
| --- | --- | --- |
| 1 | Ingest and store | done |
| 2 | Subagent tree reconstruction | done |
| 3 | Cost attribution per node | next |
| 4 | Loop / divergence / silent-failure detectors | |
| 5 | The screen | |

**Phase order is a constraint, not a suggestion.** Phases 2–4 are the reason
this project exists. Starting Phase 5 early is the documented failure mode —
it turns the repo into a frontend with an AI costume. Do not begin UI work
until the detectors in Phase 4 run against real traces.

## Layout

```
contrail/
  models.py      Span and Run dataclasses, plus the ALIASES attribute table
  otlp.py        OTLP/protobuf and OTLP/JSON decoding
  store.py       SQLite schema and queries
  collector.py   FastAPI app: OTLP ingest + JSON read API
  transcript.py  JSONL session parser and subagent tree reconstruction
  cli.py         serve / runs / show / demo / parse / sessions / tree
tests/           mirrors the module names, one file each
docs/spec.md     why this exists, the three gaps, the phase plan
```

The two ingest paths are separate on purpose. `otlp.py` + `collector.py`
read Claude Code's OpenTelemetry export; `transcript.py` reads the JSONL it
writes to disk. `transcript.py` imports neither, and must not start to --
trace export is behind a beta flag, and the transcript path is what keeps
working if it moves. They meet only at the store.

## Conventions established — keep these

**Attribute names go through `ALIASES` in `models.py`.** Two conventions
describe the same facts: OpenInference (`llm.*`, `tool.*`) and OTel GenAI
(`gen_ai.*`). GenAI is not stable as of 2026. Every field resolves through an
ordered alias list, OpenInference first. Never hard-code an attribute key at a
call site — add an alias instead. This is the project's main hedge against a
schema change, and it should stay one-line-cheap.

**Spans are upserted, never plain-inserted.** OTLP delivery is at-least-once,
so the same `span_id` legitimately arrives more than once. `INSERT OR REPLACE`
plus a run-summary rebuild. There is a test for this; do not "optimise" it away.

**Malformed input is rejected explicitly, never silently stored.** Spans
missing `trace_id`/`span_id` come back in an OTLP `partialSuccess` envelope
with a count. Corrupting the run table is worse than dropping a span.

**No content is captured by default.** Structure only: span names, durations,
token counts. Tool arguments and prompt text arrive only when the user opts in
via `OTEL_LOG_TOOL_DETAILS` / `OTEL_LOG_USER_PROMPTS`. Do not add anything that
captures content unconditionally.

**The `runs` table is materialised, not a view.** It is rebuilt whenever a
trace gains spans. Keep `_refresh_run` the single place that happens.

**Cache tokens stay separate from input tokens.** They price very differently
(reads ~10%, writes ~125%), and Phase 3's cost attribution depends on the
split. Never sum them together.

## Conventions established by Phase 2 -- keep these

**The tree is joined in a fixed priority order, and the winning rule is
recorded.** `toolUseResult.agentId` first, then `meta.json.toolUseId`, then
workflow journal membership, then directory containment as a fallback that
never fires on real data. Every subagent node stores which rule linked it in
`link_basis`. A wrong tree must stay debuggable.

**Never store a raw hash of prompt or argument text.** Tool calls are
recorded as a *normalised* signature -- tool name plus canonicalised
arguments, hashed -- so that Phase 4's loop detector can compare calls that
differ only in whitespace or path separators. A raw hash cannot be
normalised retroactively and would be storage thrown away. Extend
`normalise_argument` rather than adding a second hashing scheme.

**Node token counts are self cost, never rolled up.** Attributing spend to
the node that caused it is Phase 3's decision; the parser must not
pre-empt it. There is a test that catches double-counting -- it caught a
real one.

**`tree_nodes` is rebuilt wholesale per session**, the same discipline as
`_refresh_run`. A partial rebuild can leave a node pointing at a parent that
no longer exists.

**Transcript records are upserted on `uuid`, and deduplicated on load.** A
live session file is appended to, and a resumed one can write the same
`uuid` twice -- observed on real data. Same at-least-once reasoning as spans.

**Partial data degrades explicitly.** A referenced-but-absent subagent
becomes a `missing_transcript` node, an unanswered tool call stays
`incomplete`, an async workflow stays `pending`, and unreadable lines are
counted. A tree that admits a gap is useful; one that silently omits it is
a lie.

## Working notes for Phase 2 -- done, kept for context

The goal was to nest subagent work under the run that spawned it.

- The format has now been read off disk and written up in
  `docs/spec.md` under **Transcript format, as verified** — 5 sessions and
  148 subagent transcripts, CLI 2.1.121–2.1.266. Trust that section over any
  community documentation, and over anything this file said before it.
- **No hook shim is needed.** The transcripts are self-sufficient: the parent
  records `toolUseResult.agentId` and workflow runs record membership in
  `journal.jsonl`. Hooks would add a moving part for no new information.
- The DAG edge is `uuid` / `parentUuid`. There is no `parent_message_id`.
- Subagents live in their **own files** under `<session>/subagents/`, with
  `isSidechain: true` and the parent's `sessionId`. Never inline.
- Joining on `tool_use_id` alone finds 15% of the tree — only 22 of 148
  `.meta.json` files carry it. Resolve `Agent` spawns by
  `toolUseResult.agentId` and `Workflow` spawns by journal membership; record
  which rule fired in `link_basis` so a wrong tree stays debuggable.
- Keep the transcript parser as an **independent path** from the OTel
  ingest. Trace export is behind a beta flag and span names can change; the
  tool must still work if that shifts.

## Testing

```
python -m pytest -q
ruff check .
```

Tests use no network and write no files outside `tmp_path`. Every bug fixed
gets a test that would have caught it. Detectors in Phase 4 must be pure
functions over a run tree, tested against recorded fixtures — that is what
makes this repo read as engineering rather than scripting.

## Environment

Windows, PowerShell 5.1. Chain commands with `;` — `&&` is PowerShell 7+ only.
Installed editable: `pip install -e ".[dev]"`.

## Scope boundaries

Not a hosted service. Not framework-agnostic yet (Claude-first, OTel
underneath so LangGraph is later work rather than a rewrite). Not evals — this
says what happened and what it cost, never whether the output was good. Not a
prompt playground. Read-only over runs that already happened.

If a request would cross one of these, say so before building it.

## One caution

This project overlaps with a multi-agent system the author works on
elsewhere. That overlap is deliberate and is the strongest argument for the
project. Keep the boundary explicit in the repo structure: everything here is
generic and open-source, and anything specific to that system belongs in its
own codebase, not in this one.
