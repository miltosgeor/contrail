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

**Status: Phase 2 of 5.** Ingest, store, and subagent tree reconstruction
work. Cost attribution and the detectors do not exist yet.

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
  cli.py         serve / runs / show / demo / parse / sessions / tree
tests/           110 tests, no network, nothing written outside tmp_path
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
| 3 | Cost attribution per node | next |
| 4 | Loop, divergence and silent-failure detectors | |
| 5 | The screen | |

## Development

```bash
python -m pytest -q
ruff check .
```

## License

MIT
