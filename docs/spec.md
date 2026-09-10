# Contrail — build spec

**v0.1 · September 2026**

A trace store and analysis layer for Claude agent runs. Not another dashboard
— the part nobody built: turning agent telemetry into answers about where runs
loop, leak cost, and fail quietly.

- **Target:** 2–3 weeks part-time
- **Stack:** Python · OpenTelemetry · SQLite/DuckDB · React
- **Scope:** Claude-first, OTel underneath

---

## The finding that shapes this project

Claude Code already exports OpenTelemetry traces natively — spans for
interactions, LLM requests, tool calls and hooks, behind
`CLAUDE_CODE_ENABLE_TELEMETRY=1` and a beta flag.

So collection is solved, and building another collector would be wasted work.
What is *not* solved is everything downstream of collection. That is the
project, and it makes it smaller and sharper than it first looked.

## Situation

**The signal exists. The answers don't.**

Agent observability is crowded — Langfuse, LangSmith, Arize Phoenix,
Braintrust, AgentOps, Laminar, MLflow. All of them are good at showing you
*what happened*: here is the trace, here are the spans, here is the token
count.

None of them answer the questions that actually cost you an afternoon. Why did
this agent go round in circles? Which subagent burned the budget? Did that
tool call fail silently and poison everything after it? Those need analysis
over traces, not a prettier trace viewer.

Contrail is that analysis layer. The UI is the front end of a data model, not
the product.

## What Claude already gives you

Verified against current docs. Nothing here needs building — it needs joining.

| Surface | Status | Carries | Use for |
| --- | --- | --- | --- |
| OTel traces | Beta | `claude_code.interaction`, `.llm_request`, `.tool`, `.tool.execution`, `.hook` | Span skeleton and timings |
| OTel metrics + logs | Stable | Token counters, cost, tool decisions, API errors | Aggregates, error rates |
| Hooks | Stable | `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `SubagentStart`, `SubagentStop` | Tool I/O and subagent lifecycle |
| Session transcripts | Stable | JSONL: `uuid`/`parentUuid`, `agentId`, `tool_use_id`, per-message usage | Causal graph, replay |
| Agent SDK stream | Stable | `ResultMessage.total_cost_usd`, `model_usage`, cache token split | Ground-truth cost |
| Per-tool cost | **Absent** | — | Must be derived |
| Subagent internals | **Absent from traces** | Present in full as a separate transcript file per subagent | Must be reconstructed |

## The three gaps

This is the whole project. Everything else is plumbing. Each gap is a real
absence in current tooling, not a nicer rendering of something that already
exists. If Contrail closes these three it has a reason to exist; if it
doesn't, it's a dashboard.

### Gap 1 — Subagent tree reconstruction

Traces carry no nested spans for subagent work, so nothing in the telemetry
links a subagent back to the parent run. The transcripts do: each subagent
gets its own JSONL file, and the parent records the link. Rebuild the tree
from those files alone -- see *Transcript format, as verified* below.

No hook shim is required for this. That is a change from the original plan
and it removes a moving part rather than adding one.

*Output: a run renders as one nested tree, not a flat list of disconnected
sessions.*

### Gap 2 — Cost attribution

Cost surfaces per model and per run, never per tool or per subagent.
Attribute token deltas along the span tree so spend lands on the node that
caused it — with cache reads and writes kept separate, since they price
differently.

*Output: "this run cost $2.40, and $1.90 of it was one Explore subagent
re-reading the same files."*

### Gap 3 — Loop, divergence and silent failure

Hash tool-call signatures to catch repeated state. Diff two runs of the same
task to find where they split. Flag `is_error` tool results the agent never
acknowledged in the following message.

*Output: three detectors that fire on real runs. The hardest and most
interesting part — do it last, but do it.*

## Shape of a run

The target rendering. Every number is derived, not read off a single source.

```
run 7f3a·"reconcile monthly exports"          142.8s   $2.41
├─ interaction turn 1                            8.1s   $0.09
│  └─ tool Read  exports/2026-09.csv             0.4s
├─ interaction turn 2                           96.2s   $1.94
│  └─ subagent Explore "find schema mismatches" 94.0s   $1.90
│     ├─ tool Grep  "source_id"                  1.2s
│     ├─ tool Read  lib/normalise.py             0.3s
│     ├─ tool Read  lib/normalise.py             0.3s  ◀ loop: 4× identical
│     └─ tool Read  lib/normalise.py             0.3s
├─ interaction turn 3                           31.4s   $0.34
│  ├─ tool Bash  pytest -q                       4.9s  ◀ exit 1, unacknowledged
│  └─ tool Edit  lib/normalise.py                0.2s
└─ result  success · 3 turns · cache hit 71%

vs. run 6b21 (same task)  ── diverged at turn 2, tool 3
```

## Transcript format, as verified

Recorded against a real Claude Code data directory in September 2026: 5
session transcripts and 148 subagent transcripts, CLI versions 2.1.121 to
2.1.266. Where this contradicts community documentation, this section is what
was actually on disk. It replaces an earlier description of this format that
was written from docs and was wrong in four places.

### Layout

One directory per project under `~/.claude/projects/<path-slug>/`, one
`<session-id>.jsonl` per session. `sessionId` is constant within a file, so
file and session are the same thing. Subagents are **separate files**, never
inline in the parent:

```
<session-id>.jsonl                       parent session
<session-id>/
  subagents/
    agent-<agentId>.jsonl                one direct subagent
    agent-<agentId>.meta.json            {agentType, description?, toolUseId?, spawnDepth?}
    workflows/wf_<runId>/
      agent-<agentId>.jsonl              workflow-spawned subagents
      journal.jsonl                      {type: started|result, key, agentId, result?}
  tool-results/                          overflow for large tool results
```

### Records

Newline-delimited JSON, one object per line, 12 observed `type` values. Only
`assistant` and `user` carry a `message`; the other ten (`attachment`,
`ai-title`, `last-prompt`, `mode`, `queue-operation`,
`file-history-snapshot`, `file-history-delta`, `atis-latch`,
`bridge-session`, `system`) are harness bookkeeping and are skipped.

| Field | On | Meaning |
| --- | --- | --- |
| `uuid` | all | record identity |
| `parentUuid` | all | previous record. **This is the DAG edge**, `null` at the root |
| `isSidechain` | all | `false` in a parent session, `true` in every subagent record |
| `agentId` | subagent records | equals the filename stem, on every line |
| `sessionId` | all | the **parent's** session id, even inside subagent files |
| `sourceToolAssistantUUID` | tool_result records | uuid of the assistant record that made the call |
| `toolUseResult` | tool_result records | harness-side result detail |
| `message.usage` | assistant | token counts incl. cache read/creation and a 5m/1h ephemeral split |
| `attributionAgent` | subagent assistant records | agent type, as a string |

There is no `parent_message_id` and no `message id` linking field; the earlier
spec named both. The edge is `uuid`/`parentUuid`.

### Tool calls and results

A call is a `tool_use` block on an `assistant` record. Its result is a
`tool_result` block on a **`user`** record -- the harness replays results as
user turns. They join on `tool_use_id`.

Measured: exactly one `tool_use` block per assistant record and one
`tool_result` per user record across all 5,282 calls. Parallel tool calls are
written as separate records, not batched into one. `parentUuid ==
sourceToolAssistantUUID` on all 5,280 tool_result records, so the uuid chain
and the tool-call chain agree. 5,282 calls to 5,280 results -- unmatched calls
are the interrupted case and must not be fatal.

### How a subagent links to its parent

Two spawn mechanisms, different link paths. This is the part most worth
getting right, because the obvious join covers a small minority of files.

**`Agent` tool.** The parent's `toolUseResult` carries `{status, agentId,
agentType, prompt}`, and `agentId` is the filename stem. The `.meta.json`
carries `toolUseId` pointing back at the `tool_use` block. Two redundant
joins.

**`Workflow` tool.** Returns `status: "async_launched"` with `runId` and an
absolute `transcriptDir`. Its children's `.meta.json` files contain only
`{"agentType": "workflow-subagent"}` -- **no `toolUseId` at all**. Membership
comes from `journal.jsonl`, which names every `agentId` in the run and pairs
`started` with `result`. Workflows fan out flat: the journal carries no
agent-to-agent edge, so children nest under the `Workflow` tool node, not
under each other.

Why this matters: only **22 of 148** `.meta.json` files carry `toolUseId`, so
joining on `tool_use_id` alone -- the original plan -- would have found 15% of
the tree. Measured coverage:

| Path | Files resolved |
| --- | --- |
| `toolUseResult.agentId` in parent (`Agent`) | 24 |
| `journal.jsonl` membership (`Workflow`) | 124 |
| **Total** | **148 of 148, zero orphans** |

Directory containment is kept as a last-resort fallback, but on this corpus it
never fires.

### Trace-side join keys

Verified against live OTLP export at `service.version 2.1.266`: real spans do
carry `session.id`, and `claude_code.tool` / `.tool.execution` spans also carry
`tool_use_id` and `gen_ai.tool.call.id`. So trace-to-transcript reconciliation
is available per tool call, not merely per session. Phase 3 can rely on it.

Note that real spans emit **bare** attribute names -- `tool_name`,
`input_tokens`, `output_tokens`, `cache_read_tokens`,
`cache_creation_tokens` -- alongside the `gen_ai.*` ones, and for tokens the
bare names are the only ones present. `ALIASES` must list them or every token
count reads zero.

Real spans also carry `user.email`, `user.id` and `organization.id` in
resource attributes. That is identity, not content, but it lands in the
database and is worth knowing before sharing one.

## Architecture

Four pieces, deliberately boring.

**Collector.** An OTLP endpoint that accepts Claude Code's native export, and
-- independently -- a transcript reader that walks the on-disk session files.
Both write to the same store, but neither imports the other: trace export is
behind a beta flag, and the transcript path has to keep working if it moves.

**Store.** SQLite for development, DuckDB when queries get analytical. Spans
table, runs table, one materialised tree per run. No graph database — the
trees are small.

**Analysis.** Detectors as pure functions over a run tree, each returning
findings with a span id. This is the part that gets unit tests, and the part
that makes the repo look like engineering.

**UI.** React, reading a JSON API. Run list, run tree, cost breakdown,
findings panel, run-vs-run diff. Deliberately last.

### On schema

Emit [OpenInference](https://arize-ai.github.io/openinference/spec/) attribute
names as primary and OTel `gen_ai.*` alongside them. The OTel GenAI
conventions are still not stable as of 2026, so betting on them alone is a
rewrite waiting to happen — and inventing a third schema means interoperating
with nothing.

Implemented as the `ALIASES` table in `contrail/models.py`: every field
resolves through an ordered alias list, so a convention change is one line.

## Build sequence

Each phase ends with something that runs. Genuinely ordered — every phase
depends on the one before it. Resist starting with the UI, which is the
tempting mistake and the reason these projects end up as frontends.

| Phase | | Success condition | Est. |
| --- | --- | --- | --- |
| 1 | **Ingest and store** | Point Claude Code at it, run a real task, see spans land in the database. | ~2 days |
| 2 | **Reconstruct the tree** | A run with subagents renders as a correct nested tree. Transcript parser only -- no hook shim -- joined on `toolUseResult.agentId`, the workflow journal, and `uuid`/`parentUuid`. | ~3 days |
| 3 | **Attribute cost** | Walk the tree assigning token deltas to nodes, cache reads and writes split. Reconcile the total against `ResultMessage.total_cost_usd` — that reconciliation is the correctness test. | ~2 days |
| 4 | **Detectors** | Loop detection, run-vs-run divergence, unacknowledged tool failures. Pure functions, unit tested against recorded fixtures. The intellectual core. | ~4 days |
| 5 | **The screen** | Run list, tree view, cost breakdown, findings, diff. Now it earns the name "command center" — because there is something behind it worth commanding. | ~4 days |

Timings assume part-time work alongside other commitments; treat them as
ordering, not deadlines.

## Not in scope

Scope creep is the main failure mode. Each of these is a defensible cut, and
each one saves a week.

- **Not a hosted service.** Runs locally against your own agents. No auth, no tenancy, no billing.
- **Not framework-agnostic yet.** Claude-first. OTel underneath means LangGraph support is later work, not a rewrite.
- **Not evals.** Contrail says what happened and what it cost. It does not score output quality.
- **Not a prompt playground.** No editing, no replay-with-changes. Read-only over runs that already happened.
- **Not real-time streaming at first.** Runs appear when they finish. Live tailing is a Phase 6 nice-to-have.

## Honest risks

**The beta flag moves.** Trace export is behind a beta flag and span names can
change. Mitigation: keep the transcript parser as an independent path, so the
tool still works if traces shift underneath. This is also why Phase 2 exists.

**It becomes a frontend project.** The single most likely outcome. The UI is
genuinely more fun than the data model. The phase order is the defence — if
Phase 5 starts before Phase 4 finishes, the project has already failed at
being what it claims to be.

**The detectors are harder than they look.** Loop detection over noisy tool
arguments is a real problem — identical intent rarely means identical bytes.
Expect to need normalisation before hashing, and expect the first version to
be wrong. That difficulty is also what makes it worth doing.

**Overlap with other work cuts both ways.** This exists partly because a
multi-agent system I work on elsewhere needs it, and building infrastructure
you actually need is the strongest version of this project. It's also how a
portfolio project quietly becomes unpaid company work. Decide up front which
parts are open-source generic and which belong to that other system, and keep
the boundary in the repo structure rather than in your head.

---

## Sources

- [Claude Code OpenTelemetry observability](https://code.claude.com/docs/en/agent-sdk/observability)
- [Hooks reference](https://code.claude.com/docs/en/hooks)
- [Cost tracking](https://code.claude.com/docs/en/agent-sdk/cost-tracking)
- [OpenInference spec](https://arize-ai.github.io/openinference/spec/)
- [OTel GenAI conventions](https://opentelemetry.io/blog/2026/genai-observability/)
- [Agent observability landscape](https://laminar.sh/article/2026-04-23-top-6-agent-observability-platforms)
