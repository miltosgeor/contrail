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
| Session transcripts | Stable | JSONL: message ids, `parent_message_id`, `tool_use_id`, per-message usage | Causal graph, replay |
| Agent SDK stream | Stable | `ResultMessage.total_cost_usd`, `model_usage`, cache token split | Ground-truth cost |
| Per-tool cost | **Absent** | — | Must be derived |
| Subagent internals | **Absent** | Start/stop only, no nested spans | Must be reconstructed |

## The three gaps

This is the whole project. Everything else is plumbing. Each gap is a real
absence in current tooling, not a nicer rendering of something that already
exists. If Contrail closes these three it has a reason to exist; if it
doesn't, it's a dashboard.

### Gap 1 — Subagent tree reconstruction

`SubagentStart` and `SubagentStop` fire, but nothing links a subagent's
internal work back to the parent run. Join hook events to transcript records
on `tool_use_id` and rebuild the real tree.

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

## Architecture

Four pieces, deliberately boring.

**Collector.** An OTLP endpoint that accepts Claude Code's native export, plus
a small hook shim (`PostToolUse`, `SubagentStart`/`Stop`) posting the detail
traces omit. Both write to the same store.

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
| 2 | **Reconstruct the tree** | A run with subagents renders as a correct nested tree. Hook shim + transcript parser, joined on `tool_use_id` and `parent_message_id`. | ~3 days |
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
