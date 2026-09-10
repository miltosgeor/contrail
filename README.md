# Contrail

A trace store and analysis layer for Claude agent runs.

Claude Code already exports OpenTelemetry traces. What it doesn't do — and
what no agent observability tool does well yet — is answer the questions that
actually cost you an afternoon:

- Where did this agent **loop**, and on what?
- Which **subagent** burned the budget?
- Did a tool call fail and the agent carry on regardless?
- When the same task runs twice, **where do the runs diverge**?

Contrail is the layer that answers those. The dashboard is the front end of a
data model, not the product. Full reasoning in [`docs/spec.md`](docs/spec.md).

**Status: Phase 4 of 5.** Ingest, store, subagent tree reconstruction, cost
attribution and the detectors all work. The screen is next.

---

## Quick start

```bash
pip install -e ".[dev]"

contrail demo                # load a synthetic run
contrail runs                # list runs
contrail show a1b2c3d4       # print one run as a tree
```

```
claude_code.interaction  [a1b2c3d4e5f60718]
  4.2s · 3 spans · 1 error · 12,400 in / 830 out (9,100 cached)

claude_code.interaction                            4.2s
├─ claude_code.tool Read                          440ms
└─ claude_code.tool Bash                           3.2s  ERROR
```

## Collecting real traces

Start the collector:

```bash
contrail serve               # OTLP endpoint on http://127.0.0.1:4318/v1/traces
```

Then, in the shell where you run Claude Code:

```bash
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1    # trace export is still beta
export OTEL_TRACES_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
export OTEL_TRACES_EXPORT_INTERVAL=1000         # flush fast while developing
```

Run any Claude Code task, then `contrail runs`.

Nothing is captured by default beyond structure — span names, durations,
token counts. Tool arguments and prompt text only arrive if you opt in with
`OTEL_LOG_TOOL_DETAILS=1` / `OTEL_LOG_USER_PROMPTS=1`. See `.env.example`.

## Reconstructing subagent trees

This path reads the JSONL transcripts Claude Code already writes to disk. It
needs no collector and no telemetry enabled, and it is deliberately
independent of the OTLP path -- trace export is behind a beta flag, and this
keeps working if it moves.

```bash
contrail parse               # read session transcripts from disk
contrail sessions            # list parsed sessions
contrail tree cec62d6f       # print one reconstructed run tree
```

```
session 64fef8c7-…
|- turn 189
|  `- tool Workflow                     998.3s          <pending>
|     `- workflow wf_8c35c824-7c9       997.1s
|        |- subagent workflow-subagent   49.8s  11,874tok  [workflow_journal]
|        |- subagent workflow-subagent   26.5s  11,004tok  [workflow_journal]
```

Each subagent node records `link_basis` — which rule linked it to its parent
— so a wrong tree stays debuggable. Structure only: tool calls are stored as
a non-reversible normalised signature, never as argument text, unless you opt
in with `CONTRAIL_CAPTURE_CONTENT=1`.

## Attributing cost

```bash
contrail cost b2b42db2                 # cost per subagent and per turn
contrail cost b2b42db2 --at 2026-09-10 # what it would cost at those prices
contrail reconcile b2b42db2            # check the attribution three ways
```

```
session b2b42db2-...
  priced at 2026-09-10 prices
  $470.0769  -  541,296,859 billable tokens
  in 121,090 / out 7,816,998 / cache read 495,676,375 / write 5m 8,610,916 / write 1h 29,071,480

SUBAGENT                                             COST          TOKENS
Inventory all Python scripts in the projec        $4.2666       3,572,410
Q1: Why does canonical ordering behave dif        $3.4068       5,083,844
```

Cost is checked three ways, and only the first is a correctness test: the
attribution invariant (attributed tokens must equal source tokens), agreement
between the transcript and OTel paths joined on `request_id`, and agreement
with Claude Code's own cost counter.

Per-subagent and per-turn cost is measured. **Per-tool cost is not** — a tool
call makes no API call and has no cost of its own; what it causes is growth in
the next request's input tokens. Any per-tool figure is a labelled derived
attribution, never presented as measured.

## Cost, and what it is not

Tokens are the stored truth. **No dollar figure is written to the database**,
because prices change and a stored cost would silently falsify every
historical run at the next pricing update. Cost is computed at query time
from a price table with effective dates, so a run from March stays costed at
March's prices.

Claude Code's metrics stream also exports a `claude_code.cost.usage` counter
in USD. **That figure is Claude Code's own client-side estimate, computed from
a price table bundled in the CLI — it is not a billing figure**, and Contrail
does not treat it as ground truth. Comparing against it checks that our price
table has not gone stale relative to theirs; if the two disagree, either
table could be the wrong one. Reconciliation against actual billing is not
something any local tool can do.

## Finding what went wrong

```bash
contrail findings b2b42db2                 # all applicable detectors
contrail findings b2b42db2 --evidence      # with the reasoning behind each
contrail findings b2b42db2 --detector cost
```

```
[redundant_repeats/unproductive_polling]
  Read called 3x, each returning identical output while waiting on a background task
  in: turn turn 188

[cost_concentration]
  subagent 'workflow-subagent' holds 95% of its parent's tokens (1,066,165 of 1,118,866)

[unhandled_errors] (low confidence)
  Read failed and nothing afterwards touched the same target
```

Four detectors, as pure functions over a run tree:

| Detector | Rule | Confidence |
| --- | --- | --- |
| Redundant repeats | Same call, same **result** hash — so it is right about redundancy whatever caused it | high |
| Cost concentration | A node holding a disproportionate share of its parent's tokens | high |
| Outcome divergence | Agents given one task returning different structured outputs | high, where outputs are comparable |
| Unhandled errors | A failed call after which nothing touched the same target | **low** — a structural proxy |

Every finding carries its own evidence and its own confidence, because these
rules are wrong often enough that a finding you cannot argue with is worse
than no finding.

**What is measured, and on how little.** The labelled sets are small and are
quoted with their size everywhere they appear: 22 repeat groups (6 positive,
15 negative, 1 undecidable), 4 hand-labelled unhandled-error findings
(precision **3 of 4**, up from 3 of 14 before two benign error classes were
excluded), 25 verifier triples (3 split, 22 agreed). None of that is
validation at scale, and 75% of four proves very little.

`unhandled_errors` reports a *shape*, never a verdict — it does not claim the
agent ignored anything. It excludes two classes where continuing is correct:
a tool the user declined, and a probe for a file that does not exist. The two
thresholds on cost concentration are corpus-tuned defaults exposed as
`--share-threshold` and `--min-tokens`, not rules.

Every bug this project has had produced silently wrong output rather than a
crash, so every detector and extraction path carries a **canary** asserting it
fires on a known positive, end to end — a test that passes when the code finds
nothing is not a test.

## A negative result we kept

One of the three originally planned detectors diffed two runs of the same
task to find where their **execution paths** split. Before building it, the
premise was tested against real data — and it does not hold.

The corpus contained a natural experiment: a research workflow that sends the
same claim to three independent verifier agents under a "≥2/3 refutations
kill it" vote. That gives **25 tasks, each run three times**, of which 3 split
on outcome and 22 agreed. Mean pairwise path distance within a triple:

| | n | mean | range |
| --- | --- | --- | --- |
| Split on outcome | 3 | 0.878 | 0.823 – 0.944 |
| Agreed | 22 | 0.839 | 0.738 – 0.961 |

The ranges overlap almost entirely, and the single most path-divergent triple
is one that *agreed*. One triple split on outcome while running an almost
identical path; the most divergent triple agreed despite one member searching
the web and two shelling out. Path divergence does not predict outcome
divergence here.

So path-diff divergence was dropped, outcome divergence was kept with its
precondition stated, and the numbers live in
[`docs/spec.md`](docs/spec.md) rather than being deleted quietly. A negative
result that changes the design is worth as much as a positive one — and the
first loop-detection rule tried here was wrong on 8 of the 9 cases it flagged,
which is the same lesson twice.

## API

| Route | Purpose |
| --- | --- |
| `POST /v1/traces` | OTLP/HTTP ingest — protobuf or JSON |
| `GET /health` | Status and row counts |
| `GET /api/runs?limit=25` | Recent runs |
| `GET /api/runs/{trace_id}` | One run with its spans |

## Layout

```
contrail/
  models.py      Span and Run, plus the attribute alias table
  otlp.py        OTLP/protobuf and OTLP/JSON decoding
  store.py       SQLite schema and queries
  collector.py   FastAPI app: ingest + read API
  transcript.py  JSONL session parser and subagent tree reconstruction
  cost.py        dated price lookup, cost attribution, reconciliation
  prices.json    the price table -- data with effective dates, not code
  detectors.py   repeats, cost concentration, divergence, unhandled errors
  cli.py         serve / runs / show / demo / parse / sessions / tree /
                 cost / reconcile / findings
tests/           241 tests, including a canary per detector
docs/spec.md     Why this exists and what the remaining phases are
```

## On attribute names

Two competing conventions describe the same facts: OpenTelemetry's
[GenAI semantic conventions](https://opentelemetry.io/blog/2026/genai-observability/)
(`gen_ai.*`) and [OpenInference](https://arize-ai.github.io/openinference/spec/)
(`llm.*`, `tool.*`). GenAI is not stable as of 2026.

Rather than pick one and rewrite later, every field is resolved through an
alias list in `models.py`, OpenInference first. A new convention means adding
one line, not migrating a database.

## Roadmap

| Phase | | Status |
| --- | --- | --- |
| 1 | Ingest and store | done |
| 2 | Subagent tree reconstruction | done |
| 3 | Cost attribution per node | done |
| 4 | Detectors | done |
| 5 | The screen | next |

## Development

```bash
python -m pytest -q
ruff check .
```

## License

MIT
