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

**Status: Phase 1 of 5.** Ingest and store works. Analysis does not exist yet.

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
  cli.py         serve / runs / show / demo
tests/           41 tests, no network, no fixtures on disk
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
| 2 | Subagent tree reconstruction | next |
| 3 | Cost attribution per node | |
| 4 | Loop, divergence and silent-failure detectors | |
| 5 | The screen | |

## Development

```bash
python -m pytest -q
ruff check .
```

## License

MIT
