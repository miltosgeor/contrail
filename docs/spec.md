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
| OTel metrics + logs | Stable | `claude_code.cost.usage` (USD), `claude_code.token.usage`, session count, active time | Cross-checking our price table |
| Hooks | Stable | `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `SubagentStart`, `SubagentStop` | Tool I/O and subagent lifecycle |
| Session transcripts | Stable | JSONL: `uuid`/`parentUuid`, `agentId`, `tool_use_id`, per-message usage | Causal graph, replay |
| Agent SDK stream | Stable | `ResultMessage.total_cost_usd`, `model_usage`, cache token split | Not available on disk -- SDK-only, see below |
| Per-tool cost | **Not measurable** | A tool call makes no API call | Derived attribution only |
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

Cost surfaces per model and per run, never per subagent. Attribute tokens
along the tree so spend lands on the node that caused it, with cache reads
and the two cache-write TTLs kept separate, since all three price
differently.

**Node-level cost is measured. Per-tool cost is not, and the earlier version
of this section implied otherwise.** A tool call makes no API call and has no
cost of its own; what it causes is growth in the *next* request's input
tokens. So run, turn and subagent cost are computed from recorded token
counts, and per-tool cost is exposed as an explicitly-labelled *derived
attribution* -- the input-token delta on the following request -- never
presented as a measured figure.

**Tokens are the stored truth; no dollar figure is ever written to the
database.** Prices change, and a stored cost silently falsifies every
historical run at the next pricing update. Cost is computed at query time
from a price table with effective dates, so a run from March is still costed
at March's prices.

*Output: "this run cost $2.40, and $1.90 of it was one Explore subagent
re-reading the same files."*

### Gap 3 — Redundancy, cost concentration, divergence, unhandled errors

Four detectors, revised after measuring the corpus rather than assuming what
would be in it. One of the originally planned three was dropped on evidence —
see *Divergence: a documented negative result* below.

**Redundant repeats.** Hash tool-call signatures to find calls repeated within
one node, then split them on whether the *result* changed. Identical result
hash means the repeat genuinely gained nothing, whatever caused it; a
different hash means something changed, whether or not any write appears in
the trace. Repeated writes take a separate rule, since an identical edit
applied twice should fail the second time.

**Cost concentration.** Flag nodes consuming disproportionate tokens relative
to their siblings. Needs no ground truth beyond arithmetic over the Phase 3
attribution, and it is what actually answers "why did this run cost so much".

**Outcome divergence.** Compare the structured outputs of sibling agents given
the same task. Requires structured, comparable outputs and does not
generalise to arbitrary runs — stated plainly because the corpus only
supports it under that precondition.

**Unhandled errors.** Report `is_error` tool results where nothing
subsequently touched the same target. Deliberately framed as description, not
verdict: some errors are informative and moving on is correct behaviour, so
this reports a shape, never that the agent was wrong to continue. A
structural proxy, and labelled as one.

*Output: four detectors as pure functions over a run tree, each carrying the
evidence for its finding.*

### Divergence: a documented negative result

The original plan had a detector that diffs two runs of the same task to find
where they split, on the assumption that execution-path divergence is what
you want to see. **That assumption was tested against real data and does not
hold.** The finding is recorded here rather than quietly dropped, because a
negative result that changes the design is worth as much as a positive one.

The corpus contains a natural experiment. A `deep-research` workflow
dispatched adversarial claim verifiers under a "≥2/3 refutations kill it"
voting scheme, which sends the same claim to three independent agents:
**25 tasks, each run exactly three times, 75 agents.** Three of the 25 split
on outcome — same input, different verdict — and 22 agreed. So there are
labelled positives and negatives from a real run.

Mean pairwise path distance within a triple, over the sequence of
`(tool, normalised-argument-signature)` pairs:

| | n | mean | range |
| --- | --- | --- | --- |
| Split on outcome | 3 | 0.878 | 0.823 – 0.944 |
| Agreed | 22 | 0.839 | 0.738 – 0.961 |

The ranges overlap almost entirely, and the **most** path-divergent triple in
the corpus is one that *agreed* (0.961). Comparing tool names only, ignoring
arguments, gives 0.199 against 0.123 — directionally the same, and at n=3 the
gap is noise either way.

Two counter-examples, one in each direction. A triple that split on outcome
while running effectively the same path:

```
refuted=False   ToolSearch WebFetch WebSearch WebSearch WebSearch StructuredOutput
refuted=True    ToolSearch WebFetch WebSearch WebFetch  WebSearch StructuredOutput
refuted=False   ToolSearch WebFetch WebSearch WebSearch WebSearch StructuredOutput
```

And the most path-divergent triple, which agreed despite genuinely different
strategies — one member searching the web, two shelling out:

```
refuted=False   ToolSearch WebFetch WebSearch WebFetch WebSearch WebFetch StructuredOutput
refuted=False   ToolSearch WebFetch Bash Bash Bash Bash Bash Bash StructuredOutput
refuted=False   ToolSearch WebFetch Bash Bash Bash Bash Bash StructuredOutput
```

The metric is also **saturated**: every verifier is a web agent whose
`WebSearch` queries and `WebFetch` URLs are unique, so signatures almost
never match and the distance is pinned near 0.85 in both groups. A measure
that cannot separate its own control group has no discriminating power here.

**Consequences, and they are the point of writing this down.**

- Path-diff divergence is dropped. It is not merely unvalidated; the only
  real evidence available argues against its premise.
- Outcome divergence is kept, because the same 25 triples *do* give it ground
  truth — but only where outputs are structured and comparable, which is a
  precondition and not a general capability.
- This corpus can validate an outcome-diff detector and cannot validate a
  path-diff one. Those are different things, and conflating them would have
  produced a detector with nothing real to test against.

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

## Cost attribution, as verified

Recorded September 2026 against live OTLP export at `service.version 2.1.266`
and 148 real transcripts. As with the transcript format, this section is what
was measured, not what was documented -- the previous version of this spec
named a ground truth that does not exist on disk.

### Where the numbers come from

**Tokens come from transcripts. Timing and structure come from OTel.**

Cost lives on `claude_code.llm_request` spans, and those spans carry **no**
`tool_use_id` (0 of 103 measured) and no agent identity -- so the trace path
alone cannot say which subagent spent what. Every `llm_request` span does
carry `request_id`, and every transcript assistant record carries
`requestId`. That is the join.

| | Carries | Use for |
| --- | --- | --- |
| `llm_request` span | `request_id`, `model`, token counts, `duration_ms`, `ttft_ms` | timing |
| Transcript assistant record | `requestId`, `agentId`, `message.usage` incl. TTL split | **tokens** |

Transcripts win for tokens because they are scoped to an agent and traces are
not, and because export can start mid-session: on the measured session, 21
transcript records had no corresponding span, while only 1 span had no record.

### Cross-source agreement

Joining the two on `request_id` over one real session:

```
matched on request_id            104
token counts agreeing            104 / 104   (input, output, cache read, cache creation)
span-only  /  transcript-only      1  /  21
```

Two pipelines that share no code reporting identical counts is the strongest
evidence available that the token inputs are right.

### The usage record

`message.usage` on an assistant record, with the fields that matter for
pricing:

| Field | Note |
| --- | --- |
| `input_tokens`, `output_tokens` | base counts |
| `cache_read_input_tokens` | priced at ~0.1x input, same for both TTLs |
| `cache_creation.ephemeral_5m_input_tokens` | priced at ~1.25x input |
| `cache_creation.ephemeral_1h_input_tokens` | priced at ~2x input |
| `output_tokens_details.thinking_tokens` | billed *within* `output_tokens`; store, never add |
| `service_tier` | a price dimension; `standard` throughout the corpus |
| `iterations[]` | per-attempt breakdown; sums to top-level on 10,578/10,578 records |

**Cache creation is two numbers, not one.** Both TTLs occur heavily -- 1h on
9,879 records, 5m on 2,104 -- and they price differently. An earlier note
elsewhere in this repo said cache writes cost "~125%", which is true only of
the 5m half. Collapsing them, as Phase 2's schema did, makes correct pricing
impossible.

`<synthetic>` appears as a model on harness-generated records. It is **not a
real API call and is excluded from cost entirely**, not priced at zero.

### The three reconciliation layers

The earlier spec said to reconcile against `ResultMessage.total_cost_usd`.
That is Agent SDK streaming state and **is not on disk**: searching all 148
transcripts for `total_cost_usd`, `cost_usd`, `costUSD` and `totalCost` finds
prose only and zero structured fields. So reconciliation is layered, and each
layer is labelled with what it actually proves.

**Layer 1 -- attribution invariant.** The sum of per-node self tokens equals
the sum of tokens over the source records, per agent and per run. Pure
arithmetic over the tree, unit-testable against fixtures. Catches
double-counting, dropped orphans and mis-scoped records. **This is the
correctness test.**

**Layer 2 -- cross-source agreement.** Transcript tokens against
`llm_request` span tokens, joined on `request_id`, as measured above.
Independent evidence that the inputs are right.

**Layer 3 -- agreement with Claude Code's own estimate.** The metrics stream
carries `claude_code.cost.usage`, unit `USD`, a sum dimensioned by `model`,
`query_source` and `effort`, alongside `claude_code.token.usage` dimensioned
by `model`, `query_source` and `type` in {input, output, cacheRead,
cacheCreation}. Confirmed present by capturing a real export.

**This counter is Claude Code's own client-side estimate, computed from a
price table bundled in the CLI. It is not a billing figure and must never be
described as ground truth.** What Layer 3 proves is that *our* price table
agrees with *theirs*, which is a genuine and useful check -- it catches our
table going stale after a price change -- and nothing more. If the two
disagree, either table could be the wrong one.

Note also that the metrics stream's `cacheCreation` is a single number with
no TTL split, so it is coarser than the transcript. Transcripts remain the
token source; metrics are only the dollar cross-check.

### Two known residuals

Both are systematic, both are reported by name rather than absorbed into a
tolerance.

**Auxiliary model calls never appear in transcripts.** The cost counter
reports `query_source: auxiliary` spend -- small Haiku calls for things like
title generation -- against models that appear nowhere in the transcript. On
the measured session the transcript contained 348 `claude-opus-5` records and
zero Haiku records, while the counter billed Haiku. Transcript-derived cost
therefore *undercounts* by the auxiliary calls, on the order of a fraction of
a percent, and the gap is reported as `auxiliary_usd` rather than hidden.

**Model names differ between sources.** The metrics stream reports
`claude-opus-5[1m]`; the transcript reports `claude-opus-5` for the same
calls. The `[1m]` suffix marks the long-context variant, which the transcript
does not expose at all. Price lookup normalises the suffix away, so a
long-context run may be priced at base rates and diverge from the counter --
which is precisely the kind of disagreement Layer 3 exists to surface.

### The price table

Prices are **data with effective dates, not constants in code**: a
`prices` table seeded from `contrail/prices.json`.

- Cost is computed at query time from the row whose effective range contains
  the *run's* start time, so historical runs stay correctly priced forever
  and a price change is one appended row that rewrites nothing.
- Every row cites its source URL and the date the figure was fetched.
  `effective_from` is the date the figure was **confirmed**, never an earlier
  date we would be guessing at.
- An unknown model yields `NULL`, never `$0`, and the run reports
  `unpriced_records: N` alongside its cost. A silent zero is how this phase
  would lie, and the `ALIASES` bug already demonstrated the cost of one.
- Every row holds the same ratios against its base input price -- cache read
  0.1x, 5m write 1.25x, 1h write 2x, output 5x. A test asserts this across
  the whole table, so a typo in a future row fails loudly instead of
  quietly mispricing runs.

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

**The screen.** One static HTML file served by FastAPI over the JSON API --
no build step, so `pip install -e .` remains the entire setup. Four things:
session list, findings, cost breakdown, tree. Deliberately last, and
deliberately small: a node toolchain would make the UI likelier to become the
project, which is the failure mode this document names. If `index.html`
outgrows ~800 lines, that is the signal to stop rather than reach for a
bundler.

**It opens on findings and cost, not on a list of traces.** Every agent
observability tool opens on traces; opening there would make this one of
them. The tree is where you drill for evidence, reachable only from a finding
or a cost bar, and that ordering is the product claim made visible.

Run-vs-run diff does not appear: path-diff divergence was dropped on evidence
(see the negative result under Gap 3) and outcome divergence needs a task
grouping that cannot be derived generically.

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
| 3 | **Attribute cost** | Walk the tree assigning tokens to nodes, cache reads and both write TTLs split. Reconcile in three layers (see *Cost attribution, as verified*); the arithmetic invariant is the correctness test. | ~2 days |
| 4 | **Detectors** | Redundant repeats, cost concentration, outcome divergence, unhandled errors. Pure functions, tested against a hand-labelled corpus sample. Path-diff divergence dropped on evidence -- see the negative result under Gap 3. | ~4 days |
| 5 | **The screen** | Session list, findings, cost breakdown, tree — opening on findings and cost, with the tree reachable only from a finding. One static file, no build step. Diff dropped: see the Gap 3 negative result. | ~4 days |
| 6 | **Recommend, then enforce** | *Not started.* A recommendation, applied in paired A/B runs, moves its target metric by more than run-to-run variance across many runs; only then an enforcing form. See the roadmap below. | — |

Phases 1–5 shipped as v0.1.0. Timings assume part-time work alongside other
commitments; treat them as ordering, not deadlines.

## Roadmap: Phase 6 — observe, recommend, enforce

**Status: not started.** v0.1.0 observes. Everything below is a plan, and it
is written with its preconditions attached, because those are what separate
a roadmap from a wish list.

### The arc

1. **Observe** — shipped in v0.1.0. Reconstruct the run, attribute cost,
   report findings with their evidence.
2. **Recommend** — turn a finding into a concrete, reviewable change: a hook
   configuration, an agent definition, a setting.
3. **Enforce** — install that change so the pattern is prevented rather than
   reported after the fact.

### The mechanism: hooks are an actuator, not only a sensor

Hooks were dropped from Phase 2 because the transcripts made them unnecessary
as a *sensor*. They matter again here as an *actuator*. What each can do,
checked against the hooks and subagents references in September 2026 — this
spec has been wrong three times from describing capabilities from memory, so
re-check before building on any of it:

| Hook / setting | Can | Cannot |
| --- | --- | --- |
| `PreToolUse` | deny a call (`permissionDecision: "deny"` with a reason), rewrite its arguments (`updatedInput`), add context the model sees (`additionalContext`) | — |
| `PostToolUse` | add context the model sees (`additionalContext`), surface a message (`systemMessage`) | block — the tool already ran; rewrite or filter the result — no documented field |
| `UserPromptSubmit` | add context (`additionalContext`), block the prompt (exit code 2) | — |
| `SessionStart` | add context | — |
| Subagent model | set per agent in definition frontmatter (`model:`), per invocation (the `model` parameter), or by default (`CLAUDE_CODE_SUBAGENT_MODEL`, with `CLAUDE_CODE_SUBAGENT_MODEL_FORCE` to override the others) | be changed by any hook |

Two consequences for the design. `PostToolUse` cannot filter a result, so
anything that must act on output does it by adding context to the next turn,
not by editing what the tool returned. And model routing is emitted as agent
definitions and settings rather than hooks — still configuration Contrail can
generate, just not hook configuration.

### From finding to change

| Finding | Recommend | Enforce via |
| --- | --- | --- |
| Redundant repeat | "you read this file earlier and it has not changed" | `PreToolUse` — `additionalContext`, or `deny` with that reason |
| Unproductive polling | back off instead of re-reading unchanged output | `PreToolUse` — `additionalContext` |
| Unhandled error | make the failure impossible to scroll past | `PostToolUse` — `additionalContext` naming the failure, so the next turn sees it |
| Cost concentration | run that work on a cheaper model | agent definition `model:` or `CLAUDE_CODE_SUBAGENT_MODEL` |

**The redundant-repeat row has a hard problem, and it is the best argument
for recommend-before-enforce.** The detector is correct because it compares
the *result* of two calls. A `PreToolUse` hook runs before the call, when the
result does not exist yet. An enforcing hook therefore cannot use the rule
that makes the detector right, and falls back towards the rule it replaced —
"is this the same call?" — which was wrong on 8 of the 9 cases it flagged,
because files change without a write appearing in the trace. The workable
version for `Read` is narrower: the hook checks the file itself at call time
and only intervenes if its content is unchanged since the last read. For
shell commands and background tasks there is no equivalent check.

### What the data says the levers are

Measured over the same six sessions, priced at 2026-09-10 rates — a
counterfactual for the sessions that predate the price table.

**Loops are rare.** At labelling time the corpus held 5,461 tool calls. Only
22 groups repeated a call within one node, and under the result-hash rule
**2 were genuinely redundant** and 4 were unproductive polling of a background
task — 6 groups in 5,461 calls. The single case an earlier rule called a clear
loop is undecidable, because its results are not on disk. Loop prevention is
real but small here.

**The money is in context, and in the main conversation's model.**

| Where spend goes | Share |
| --- | --- |
| Cache reads | 45.2% |
| 1-hour-TTL cache writes | 41.3% |
| Output | 11.5% |
| 5-minute-TTL cache writes | 1.8% |
| Uncached input | 0.1% |

86.5% of spend is re-reading and re-writing context, not producing output.
And 85.1% of spend is Opus in the *main* conversation: pricing those same
tokens at Sonnet rates would cut total spend by 34%.

**These figures are a snapshot, not a fixed truth.** They were measured while
the corpus was still being added to, so a later run of `contrail spend`
reports different numbers -- 88% context and $3,554 within two days of the
above, because the session writing this document kept going. Treat the shares
as the shape of the bill, not as constants; `contrail spend` recomputes them. That figure is an upper
bound — it assumes a cheaper model uses the same tokens and does acceptable
work, and Contrail cannot judge the second part.

**Subagent routing is small on this corpus.** Subagents account for 2.4% of
spend. Moving every Opus-priced subagent call to Haiku would save at most $42
of $3,506, about 1.2%. That contradicts the obvious reading of cost
concentration — "route the expensive subagent to a cheaper model" — for these
sessions, which is exactly why it is measured rather than assumed.

So the larger lever is likely cost rather than loop prevention, and within
cost it is main-conversation model choice and context growth rather than
subagent routing.

The 1-hour TTL accounts for 41% of spend at 2x base input, against 1.8% for
the 1.25x 5-minute TTL, and that question has since been **checked rather
than left open**. Three findings, all verified:

- **The TTL is controllable.** `promptCacheTtl` (or
  `CLAUDE_CODE_PROMPT_CACHE_TTL`) sets it for the main conversation and
  `subagentPromptCacheTtl` for everything else; both take `5m` or `1h` and
  need Claude Code v2.1.242 or later. `FORCE_PROMPT_CACHING_5M=1` forces the
  short TTL for both. So the lever can be named, which is what a
  recommendation requires.
- **This corpus did not choose it.** Claude Code requests the one-hour TTL by
  default for the main conversation on a Claude subscription within plan
  usage, while everything else stays on five minutes. The corpus matches that
  split exactly -- 98.7% of main-conversation cache-write tokens are 1h,
  against 0% for subagents -- so the 41% is a default, not a decision.
- **Which makes the saving notional here.** Within plan usage there is no
  per-token bill to reduce, and Claude Code already drops to the five-minute
  TTL once a subscription starts drawing on usage credits. The comparison
  matters to someone billed per token by API key or credits, where the
  five-minute TTL is already the default.

That sequence is the point: the measurement pointed at a lever, checking the
lever changed what the measurement meant, and the recommendation that survived
is narrower than the one the number suggested.

These shares are dominated by one session, which alone is 77% of spend. That
is the first precondition, made concrete.

### Preconditions — what stops this being vapourware

**1. Volume.** Six sessions is noise. One session is 77% of the spend, so any
corpus-level share is mostly a description of that one session. A pattern is
not a finding until it recurs across many runs and several projects, and a
recommendation needs a base rate to compare against. The detectors can run on
six sessions; recommendations cannot be justified by them.

**2. Attribution needs A/B.** To say a change helped, run the same task with
and without it. Before-and-after on different tasks cannot separate the
change from the task. And the same task varies a lot on its own: among the 22
verifier triples that *agreed* on outcome, mean pairwise path distance was
0.839 — three runs of one task barely resembled each other. A single
comparison sits inside that variance. It takes enough paired runs to clear it.

**3. Recommend before enforce.** A hook that blocks a "redundant" read will
eventually block a legitimate one, and the redundant-repeat case above shows
the enforce-time rule is weaker than the detector's by construction. Every
change ships first as a recommendation, is validated by A/B, and only then is
offered in enforcing form. `unhandled_errors` — measured at 3 of 4 on a sample
of four — is not a candidate for enforcement on that evidence at all.

**4. Emitted, never installed.** Contrail generates configuration for a
person to read and install. It does not write to `.claude/settings.json`,
does not modify a running session, and does not install hooks itself. That
keeps the tool read-only over runs that already happened, as the scope says,
and keeps a human between a heuristic and an agent's permissions.

### Boundaries

**Process, not output quality.** Contrail can see that an agent re-read a file,
ignored a failure, or spent most of a session's budget on context. It
cannot see that the code it wrote was wrong. Evals stay out of scope. This
bites hardest on model routing: Contrail can say what a cheaper model *would
have cost* and never whether it *would have done the job* — so routing stays
recommend-only unless paired with the user's own evals.

**Say what the data does not support.** Loops were rare in this corpus. A
roadmap that led with loop prevention would promise the feature the
measurements back least.

### Success condition

A recommendation, applied in paired A/B runs on the same task, moves the
metric it targets by more than the run-to-run variance of that task — across
enough runs and projects that the result is not one session's. No enforcing
form of any recommendation ships before its recommend form has cleared that
bar.


## Not in scope

Scope creep is the main failure mode. Each of these is a defensible cut, and
each one saves a week.

- **Not a hosted service.** Runs locally against your own agents. No auth, no tenancy, no billing.
- **Not framework-agnostic yet.** Claude-first. OTel underneath means LangGraph support is later work, not a rewrite.
- **Not evals.** Contrail says what happened and what it cost. It does not score output quality.
- **Not a prompt playground.** No editing, no replay-with-changes. Read-only over runs that already happened.
- **Not real-time streaming at first.** Runs appear when they finish. Live tailing is a later nice-to-have, separate from the Phase 6 roadmap.

## Corrections

What this project got wrong, what each error cost, and how it was found. Kept
because the pattern across them is the most useful thing here: **every one
produced silently wrong output rather than a crash**, and that is the entire
argument for the canary convention in `CLAUDE.md`.

Two other records belong beside this: the *documented negative result* under
Gap 3, where a planned detector was tested against real data and dropped, and
the corrections to this spec's own description of the transcript format and of
cost attribution, recorded in place in those sections.

### Spec errors — things this document asserted that were not true

| Claimed | Actually | Found by |
| --- | --- | --- |
| The DAG edge is `parent_message_id` | `uuid` / `parentUuid`; no such field exists | reading a real transcript before writing the parser |
| Subagent work is inline in the parent transcript | one separate file per subagent, `isSidechain: true`, parent's `sessionId` | same |
| Join subagents on `tool_use_id` | finds 15% of the tree — only 22 of 148 sidecars carry it | counting them |
| `SubagentStart`/`SubagentStop` hooks are needed | transcripts are self-sufficient; the hook shim was dropped entirely | same |
| Reconcile cost against `ResultMessage.total_cost_usd` | SDK streaming state, absent from disk — zero structured cost fields in 148 transcripts | searching for it |
| Per-tool cost is measurable | a tool call makes no API call; only derivable as an attribution | thinking about what a tool call is |
| Cache writes cost ~125% of input | true of the 5-minute TTL only; the 1-hour TTL is ~2x, and both occur heavily (1h on 9,879 records, 5m on 2,104) | reading `message.usage.cache_creation` |
| Run-vs-run diff compares execution paths | path divergence does not predict outcome divergence — see the negative result | measuring it across 25 verifier triples |

The pattern: every one came from describing a format or a capability from
documentation rather than from the artefact. Reading one real file first would
have caught all of them, which is why *Transcript format, as verified* exists
and why `CLAUDE.md` says to trust it over any community documentation.

### Implementation errors — silently wrong output

| Bug | What it produced | Why nothing complained |
| --- | --- | --- |
| `ALIASES` missing the attribute names Claude Code actually emits | every real span stored `tool_name` NULL and all four token counts 0 | the attributes were present, just never looked for; Phase 3 would have costed every run at $0 |
| Subagent records folded into their node twice | every subagent's cost doubled | arithmetic, no error path |
| `result_hash` read off the `tool_use` record instead of the `tool_result` record | the repeat detector found nothing and reported every group undecidable | a detector finding nothing looks the same as a clean run |
| `toolUseResult` hashed only when a dict | every *failed* call looked like it had no result at all; 11 of 22 groups spuriously undecidable | it is a dict on success and a plain string on error |
| A node id built from `tool_use_id` alone | ids collided across agent files, creating a parent cycle that hung the tree walk | a hang, not a wrong answer — the one exception to the pattern |
| A run whose every record was unpriced | totalled `$0.00`, because the session node had no tokens of its own and counted as "genuinely free" | free and unpriced rendered identically |
| `contrail show` printing box-drawing characters | `UnicodeEncodeError` on a clean Windows install — step three of the README quick start | the dev shell happened to be UTF-8 |
| The `serve` banner not flushed | the one message telling a first-time user where to go arrived after uvicorn's logging | stdout is block-buffered when not a tty |

Two of those were found only by a **clean-clone smoke test** — fresh clone,
fresh virtualenv, no cached dependencies and no existing database. An editable
install with a warm cache exercises none of the path a stranger takes.

### Detector rules that were wrong first time

**"Was there an intervening write to the same target?"** — the first
redundant-repeat rule. Wrong on **8 of the 9** cases it flagged, because files
are also rewritten by background processes and by shell commands that leave no
write in the trace. Replaced by comparing result hashes, which is right about
redundancy whatever caused it. That took the labelled set from 1 decided of 22
to 21 of 22.

**Share alone as cost concentration.** Produced 11 findings on one session, of
which 7 were two-sibling workflows whose "dominant" child was only 1.01x to
1.91x its single sibling. One of two children holding half the parent is
arithmetic, not insight, and the noise buried the findings that meant
something. Now also requires enough siblings for "typical" to mean anything
and a multiple of the sibling median: 11 findings became 3. Found by building
the screen — the verdict-first layout made the noise obvious in a way the CLI
never did.

**Reporting every unhandled error.** Precision was 3 of 14 hand-labelled
findings, dominated by two classes where continuing is correct behaviour: a
tool the user declined, and a probe for a file that does not exist. Excluding
those by their harness error template took precision to **3 of 4** — on a
sample of four, which is why the detector is still marked low confidence
rather than declared fixed.

## Honest risks

**The beta flag moves.** Trace export is behind a beta flag and span names can
change. Mitigation: keep the transcript parser as an independent path, so the
tool still works if traces shift underneath. This is also why Phase 2 exists.

**It becomes a frontend project.** The single most likely outcome. The UI is
genuinely more fun than the data model. The phase order is the defence — if
Phase 5 starts before Phase 4 finishes, the project has already failed at
being what it claims to be.

**The detectors are harder than they look.** Confirmed, and more sharply
than expected. Loop detection over noisy tool arguments is a real problem —
identical intent rarely means identical bytes, and the first rule tried here
("was there an intervening write to the same target?") was wrong on 8 of the
9 cases it flagged, because files are also rewritten by background processes
and by shell commands that leave no write in the trace. Comparing result
hashes instead fixes that. Expect the first version of any detector here to
be wrong, and expect to need a labelled sample to find out.

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
