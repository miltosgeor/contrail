# CLAUDE.md

Standing conventions for this repo. Prescriptive only — what to do and why.

The reasoning, the measurements, and the things we got wrong on the way live
in [`docs/spec.md`](docs/spec.md). When this file and that one disagree about
a fact, the spec wins; when they disagree about a rule, this file wins.

## What this is

Contrail is a trace store and analysis layer for Claude agent runs. Claude
Code already exports OpenTelemetry traces — collection is a solved problem
and we are not rebuilding it. The value is entirely downstream: turning that
telemetry into answers about where runs repeat work, leak cost, and fail
quietly.

**The screen is the front end of a data model, not the product.** If a change
makes the screen nicer without making the analysis better, it is the wrong
change. All five phases shipped and the UI came last on purpose; that
ordering still applies to changes.

## Layout

```
contrail/
  models.py      Span and Run dataclasses, plus the ALIASES attribute table
  otlp.py        OTLP/protobuf and OTLP/JSON decoding
  store.py       SQLite schema and queries
  collector.py   FastAPI app: OTLP ingest, JSON read API, the page
  transcript.py  JSONL session parser and subagent tree reconstruction
  cost.py        dated price lookup, cost attribution, reconciliation
  prices.json    the price table -- data with effective dates, not code
  detectors.py   redundant repeats, cost concentration, outcome divergence,
                 unhandled errors -- pure functions over a run tree
  index.html     the screen -- one static file, no build step
  cli.py         serve / traces / show / demo / parse / sessions / tree /
                 cost / reconcile / findings
tests/           mirrors the module names, one file each, plus test_canaries
docs/spec.md     why this exists, what was measured, what was corrected
```

**The two ingest paths stay separate.** `otlp.py` + `collector.py` read
Claude Code's OpenTelemetry export; `transcript.py` reads the JSONL it writes
to disk. `transcript.py` imports neither and must not start to — trace export
is behind a beta flag, and the transcript path is what keeps working if it
moves. They meet only at the store.

## Ingest and storage

**Attribute names go through `ALIASES` in `models.py`.** Two conventions
describe the same facts: OpenInference (`llm.*`, `tool.*`) and OTel GenAI
(`gen_ai.*`), and Claude Code emits bare names (`input_tokens`) alongside
both. Every field resolves through an ordered alias list. Never hard-code an
attribute key at a call site — add an alias instead. This is the project's
main hedge against a schema change and should stay one-line-cheap.

**Spans are upserted, never plain-inserted.** OTLP delivery is at-least-once,
so the same `span_id` legitimately arrives more than once. `INSERT OR REPLACE`
plus a run-summary rebuild. There is a test for this; do not "optimise" it away.

**Transcript records are upserted on `uuid`, and deduplicated on load.** A
live session file is appended to, and a resumed one can write the same `uuid`
twice. Same at-least-once reasoning as spans.

**Malformed input is rejected explicitly, never silently stored.** Spans
missing `trace_id`/`span_id` come back in an OTLP `partialSuccess` envelope
with a count. Corrupting the run table is worse than dropping a span.

**Materialised tables are rebuilt wholesale, in one place.** `runs` via
`_refresh_run`, `tree_nodes` per session. A partial rebuild can leave a node
pointing at a parent that no longer exists.

**Schema changes are additive, with a migration.** `CREATE TABLE IF NOT
EXISTS` will not add a column to a table that already exists, so new columns
go in `Store.MIGRATIONS` as nullable or defaulted. Indexes are created after
migration so an index can reference a column a migration just added.

## Content

**No content is captured by default.** Structure only: names, durations,
token counts. Prompt text and tool arguments arrive only when the user opts
in — `OTEL_LOG_TOOL_DETAILS` / `OTEL_LOG_USER_PROMPTS` for the trace path,
`CONTRAIL_CAPTURE_CONTENT` for the transcript path. Do not add anything that
captures content unconditionally.

**To compare content, store a hash — never the content.** `tool_signature`,
`result_hash` and `target_hash` are 16 hex chars and non-reversible.
`background_task_id` is a harness id, not content.

**Hash a *normalised* signature, never raw text.** A raw hash cannot be
normalised retroactively and would be storage thrown away. Extend
`normalise_argument` rather than adding a second hashing scheme.

**Harness error templates are boilerplate, not content.** The fixed strings
the CLI emits on failure are neither prompt text nor tool arguments, so
matching them is allowed where storing a message would not be — but only the
resulting short class label is stored, never the text.

## Cost

**Tokens are the stored truth. No dollar figure is ever persisted.** Cost is
computed at query time from `prices.json`, whose rows carry effective dates,
so a run from March stays costed at March's prices. There is a test that
walks the whole database schema and fails on any column named like a price.

**A price row is appended, never edited.** Set the old row's `effective_to`
and add a new one. `effective_from` is the date the figure was *confirmed*,
never an earlier date we would be guessing at — which does mean a run
predating the earliest confirmed price reports as unpriced, and that is the
honest answer rather than a bug. Every row cites its source URL and fetch
date, and holds the same ratios against base input (read 0.1x, 5m write
1.25x, 1h write 2x, output 5x); a test asserts that across the table.

**Unpriced is not free.** An unknown model yields `None` plus a reported
`unpriced_records` count. `<synthetic>` is excluded from cost entirely rather
than priced at zero. Rendering an unpriced run as $0.00 is a silent lie.

**Cache reads and the two cache-write TTLs are three separate numbers.**
Relative to base input: reads ~0.1x, 5-minute-TTL writes ~1.25x, 1-hour-TTL
writes ~2x. Never sum any of them together, and never collapse the two write
TTLs — `message.usage.cache_creation` splits them and both occur heavily.

**Thinking tokens are stored but never summed.** They are billed inside
`output_tokens`; adding them double-charges.

**Cost comes from transcripts, timing from OTel.** `llm_request` spans carry
no `tool_use_id` and no agent identity, so they cannot say which subagent
spent what. The two paths join on `request_id` / `requestId`, and that join
is for *verification*, not attribution.

**Node token counts are self cost; rolling up is the caller's decision.**
The parser must not pre-empt attribution.

**Reconciliation is three layers and only the first is a correctness test.**
The attribution invariant is arithmetic and must always hold. Cross-source
agreement is independent evidence. Agreement with `claude_code.cost.usage`
checks our price table against the one bundled in the CLI — that counter is a
client-side estimate, not a billing figure, and must never be called ground
truth.

## Tree reconstruction

**The tree is joined in a fixed priority order, and the winning rule is
recorded.** `toolUseResult.agentId` first, then `meta.json.toolUseId`, then
workflow journal membership, then directory containment as a fallback that
never fires on real data. Every subagent node stores which rule linked it in
`link_basis`. A wrong tree must stay debuggable.

**Partial data degrades explicitly.** A referenced-but-absent subagent
becomes a `missing_transcript` node, an unanswered tool call stays
`incomplete`, an async workflow stays `pending`, and unreadable lines are
counted. A tree that admits a gap is useful; one that silently omits it is a
lie.

## Detectors

**A repeat is redundant when the *result* did not change**, not when no write
appears in between. Comparing result hashes is right about redundancy
whatever caused it, including causes that leave no trace — background
processes and shell commands rewrite files without any write appearing.
Repeated writes take a separate branch keyed on `is_error`, because an
identical edit applied twice should fail the second time.

**Detectors are pure functions over a run tree.** No filesystem, no store, no
clock. That is what makes them testable and reproducible.

**Findings carry their own evidence and their own confidence.** These rules
are wrong often enough that a finding nobody can argue with is worse than no
finding. A detector reports a *shape*, not a verdict, where it cannot know
intent — `detect_unhandled_errors` says "this failed and nothing afterwards
touched the same target" and never claims the agent was wrong to continue.

**Thresholds are named parameters with corpus-tuned defaults, and say so.**
They come from looking at one corpus, not from a principle, and every finding
repeats that in its evidence. A tuned constant presented as a rule is the
mistake the divergence negative result exists to prevent.

**Quote the sample size wherever precision is reported.** The labelled sets
here are small — 22 repeat groups, 4 labelled error findings, 25 verifier
triples — and must never be presented as validation at scale.

**Distinct phenomena get distinct subtypes.** Genuine redundancy and
unproductive polling have different remedies; lumping them makes a detector
look noisier than it is.

## The screen

**No build step, and no dependency the page fetches at runtime.** One static
`index.html` served by FastAPI, so `pip install -e .` is the whole setup and
the page works offline. A node toolchain would make the UI likelier to become
the project. There is a test asserting the page pulls no external script.

**If `index.html` outgrows ~800 lines, stop.** That is a tripwire, not a
target — currently ~510. Reaching for a bundler is the wrong response;
cutting scope is the right one.

**It opens on findings and cost, never on a list of traces.** Every agent
observability tool opens on traces. The tree is reachable only from a finding
or a cost bar, and that ordering is the product claim made visible.

**Tokens lead, dollars enrich.** Tokens are always known and never unpriced,
so rank and compare on them; USD appears where the price table covers the
session's own date and reads `unpriced` where it does not. The
at-today's-prices toggle is labelled a counterfactual.

**A wide level is a picture, not a list.** Above 12 children, siblings render
as a cost-proportional strip. Fan-out is the normal shape of this data — 465
turns under one session, 100 subagents under one workflow — so this is the
general rule, not a special case.

**The screen states its own limits.** "What this does not detect, and why" is
in the product, open by default. A tool that says what it cannot see makes a
stronger claim than one that says it in a README.

**All CLI and page output is ASCII.** The target console is Windows
PowerShell at cp1252, which cannot encode box-drawing characters or a middot;
printing one raises and takes the command down. `main()` also reconfigures
stdout with `errors="replace"` as a backstop, and there is a test that
encodes every command's output to cp1252.

**`traces` and `sessions` are different objects.** `/api/traces` is the OTLP
span path keyed by trace id; `/api/sessions` is the transcript path keyed by
session id. Never name them the same thing — it implies a join that does not
exist. "Run" is deliberately unused, left for the contiguous-segment idea: a
session file can span months of resumes, so a run is not a session.

## Testing

```
python -m pytest -q
ruff check .
```

Both must pass. Tests use no network and write no files outside `tmp_path`.

**Every bug fixed gets a test that would have caught it.**

**Every detector and extraction path needs a canary that fires on a known
positive, end to end.** A test that passes when the code finds nothing is not
a test. Every bug this project has had produced silently wrong output rather
than a crash — see *Corrections* in `docs/spec.md` for the list and what each
one cost. Canaries live in `tests/test_canaries.py`, build JSONL on disk and
run `load_session` → `build_tree` → the store → the detector, because the
seams between those stages are where the silent failures lived. Unit tests
whose fixtures start halfway through miss them by construction. Pair each
canary with a negative where one exists, so a detector that fires on
everything fails too.

**Fixtures are captured from real data, not invented.** Shapes come from what
was actually on disk; values are synthetic so no session content is committed.
A fixture invented from a doc tests the doc.

**Guard the lists that go stale silently.** `VOLATILE_RESULT_KEYS` and
`ERROR_TEMPLATES` both match strings the harness emits, so both rot when it
changes wording — and both fail quietly, with repeats no longer detected and
benign errors no longer recognised. Each has a guard that hashes or classifies
a recorded pair and fails on divergence, plus a cross-check that every name
referenced elsewhere is one the list can produce. `unclassified_errors` rides
on every finding as the in-use drift signal.

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
