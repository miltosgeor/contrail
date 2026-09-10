"""Command line entry point.

    contrail serve                 start the collector
    contrail traces                list recent OTel traces
    contrail show <trace_id>       print one trace as a tree
    contrail demo                  load a synthetic run so the UI has content

    contrail parse                 read session transcripts from disk
    contrail sessions              list parsed sessions
    contrail tree <session_id>     print one reconstructed run tree
    contrail cost <session_id>     attribute cost across the run tree
    contrail reconcile <session>   check the attribution three ways
    contrail findings <session>    run the detectors

The two groups are separate paths on purpose: `parse`/`sessions`/`tree` read
the JSONL Claude Code already writes and need no collector and no telemetry
enabled, so they keep working if trace export shifts. See docs/spec.md.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .store import Store
from .transcript import KIND_SUBAGENT, KIND_TOOL


def _fmt_ms(ms: float) -> str:
    return f"{ms / 1000:.1f}s" if ms >= 1000 else f"{ms:.0f}ms"


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    os.environ.setdefault("CONTRAIL_DB", args.db)
    print(f"contrail -> {args.db}")
    print(f"  screen         http://{args.host}:{args.port}/")
    print(f"  OTLP ingest    http://{args.host}:{args.port}/v1/traces")
    print()
    print("The screen reads transcripts and needs no telemetry. If it is")
    print("empty, run: contrail parse")
    uvicorn.run(
        "contrail.collector:app", host=args.host, port=args.port, reload=args.reload
    )
    return 0


def cmd_traces(args: argparse.Namespace) -> int:
    store = Store(args.db)
    runs = store.runs(limit=args.limit)
    if not runs:
        print("no traces yet -- start the collector and run a Claude Code task")
        print("(transcripts need no collector: try `contrail parse`)")
        return 0

    print(f"{'TRACE':<18}{'ROOT':<34}{'SPANS':>6}{'ERR':>5}{'DURATION':>10}{'TOKENS':>10}")
    for r in runs:
        tokens = r["input_tokens"] + r["output_tokens"]
        root = r["root_name"][:32]
        print(
            f"{r['trace_id'][:16]:<18}{root:<34}{r['span_count']:>6}"
            f"{r['error_count']:>5}{_fmt_ms(r['duration_ms']):>10}{tokens:>10,}"
        )
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    store = Store(args.db)

    matches = [r for r in store.runs(limit=500) if r["trace_id"].startswith(args.trace_id)]
    if not matches:
        print(f"no trace matching {args.trace_id!r}", file=sys.stderr)
        return 1
    trace_id = matches[0]["trace_id"]

    run = store.run(trace_id)
    spans = store.spans_for(trace_id)
    assert run is not None

    errors = run["error_count"]
    print(f"{run['root_name']}  [{trace_id[:16]}]")
    print(
        f"  {_fmt_ms(run['duration_ms'])} - {run['span_count']} spans - "
        f"{errors} error{'' if errors == 1 else 's'} - "
        f"{run['input_tokens']:,} in / {run['output_tokens']:,} out "
        f"({run['cache_read_tokens']:,} cached)\n"
    )

    children: dict[str | None, list] = {}
    known = {s.span_id for s in spans}
    for s in spans:
        parent = s.parent_span_id if s.parent_span_id in known else None
        children.setdefault(parent, []).append(s)

    def walk(parent: str | None, prefix: str) -> None:
        kids = sorted(children.get(parent, []), key=lambda x: x.start_ns)
        for i, s in enumerate(kids):
            last = i == len(kids) - 1
            label = f"{s.name} {s.tool_name}" if s.tool_name else s.name
            flag = "  ERROR" if s.is_error else ""
            branch = f"{prefix}{'`- ' if last else '|- '}" if prefix or parent else ""
            print(f"{branch}{label:<{max(10, 46 - len(branch))}}"
                  f"{_fmt_ms(s.duration_ms):>9}{flag}")
            walk(s.span_id, prefix + ("   " if last else "|  ") if branch else "")

    walk(None, "")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """Insert a synthetic run. Useful before real telemetry is flowing."""
    from .models import Span

    store = Store(args.db)
    base = 1_757_000_000_000_000_000
    ms = 1_000_000
    trace = "a1b2c3d4e5f60718a1b2c3d4e5f60718"

    spans = [
        Span(trace, "0000000000000001", None, "claude_code.interaction", 1,
             base, base + 4200 * ms, service_name="claude-code",
             attributes={"session.id": "demo-session",
                         "gen_ai.request.model": "claude-opus-4",
                         "gen_ai.usage.input_tokens": 12400,
                         "gen_ai.usage.output_tokens": 830,
                         "gen_ai.usage.cache_read_input_tokens": 9100}),
        Span(trace, "0000000000000002", "0000000000000001", "claude_code.tool", 1,
             base + 200 * ms, base + 640 * ms, service_name="claude-code",
             attributes={"tool.name": "Read", "session.id": "demo-session"}),
        Span(trace, "0000000000000003", "0000000000000001", "claude_code.tool", 1,
             base + 700 * ms, base + 3900 * ms, status_code=2,
             status_message="exit status 1", service_name="claude-code",
             attributes={"tool.name": "Bash", "session.id": "demo-session"}),
    ]
    store.add_spans(spans)
    print(f"loaded demo run {trace[:16]} -- try: contrail show {trace[:8]}")
    return 0


def cmd_parse(args: argparse.Namespace) -> int:
    """Parse session transcripts from disk into the store.

    Independent of `serve`: this reads the files Claude Code already wrote
    and needs no collector running and no telemetry enabled.
    """
    from .transcript import build_tree, discover_sessions, load_session

    root = Path(args.root) if args.root else None
    paths = discover_sessions(root)
    if args.session:
        paths = [p for p in paths if p.stem.startswith(args.session)]
    paths = paths[: args.limit]

    if not paths:
        print("no session transcripts found", file=sys.stderr)
        return 1

    store = Store(args.db)
    totals = {"records": 0, "nodes": 0, "agents": 0, "errors": 0}

    for path in paths:
        session = load_session(path)
        tree = build_tree(session)

        records = store.add_transcript_records(session.records, tree.record_scope)
        for agent in session.agents.values():
            records += store.add_transcript_records(agent.records, tree.record_scope)
        nodes = store.save_tree(
            tree, project_slug=session.project_slug, path=str(path)
        )

        totals["records"] += records
        totals["nodes"] += nodes
        totals["agents"] += len(session.agents)
        totals["errors"] += session.parse_errors

        print(
            f"{session.session_id[:16]}  {records:>7,} records  {nodes:>6,} nodes  "
            f"{len(session.agents):>4} subagents"
            + (f"  {session.parse_errors} unreadable" if session.parse_errors else "")
        )
        for warning in tree.warnings[: args.max_warnings]:
            print(f"    ! {warning}")
        if len(tree.warnings) > args.max_warnings:
            print(f"    ! ... and {len(tree.warnings) - args.max_warnings} more")

    print(
        f"\n{len(paths)} session(s): {totals['records']:,} records, "
        f"{totals['nodes']:,} nodes, {totals['agents']} subagents, "
        f"{totals['errors']} unreadable lines"
    )
    return 0


def cmd_sessions(args: argparse.Namespace) -> int:
    """List parsed sessions. Reads the store, not the filesystem."""
    store = Store(args.db)
    rows = store.transcript_sessions(limit=args.limit)
    if not rows:
        print("no parsed sessions yet -- try: contrail parse")
        return 0

    print(f"{'SESSION':<18}{'PROJECT':<30}{'NODES':>7}{'AGENTS':>8}{'ERRORS':>8}")
    for r in rows:
        print(
            f"{r['session_id'][:16]:<18}{r['project_slug'][:28]:<30}"
            f"{r['node_count']:>7,}{r['agent_count']:>8}{r['parse_errors']:>8}"
        )
    return 0


def cmd_tree(args: argparse.Namespace) -> int:
    """Print one reconstructed session as a nested tree.

    This is the Phase 2 success condition from docs/spec.md: a run with
    subagents rendering as a correct nested tree rather than a flat list of
    disconnected sessions.
    """
    store = Store(args.db)
    matches = [
        r for r in store.transcript_sessions(limit=500)
        if r["session_id"].startswith(args.session)
    ]
    if not matches:
        print(f"no parsed session matching {args.session!r}", file=sys.stderr)
        return 1

    row = matches[0]
    nodes = store.tree_nodes(row["session_id"])
    if not nodes:
        print("session has no stored tree", file=sys.stderr)
        return 1

    children: dict[str | None, list] = {}
    for node in nodes:
        children.setdefault(node["parent_node_id"], []).append(node)

    tokens = sum(n["input_tokens"] + n["output_tokens"] for n in nodes)
    print(f"session {row['session_id']}  [{row['project_slug']}]")
    print(
        f"  {row['node_count']:,} nodes - {row['agent_count']} subagents - "
        f"{tokens:,} tokens"
        + (f" - {row['parse_errors']} unreadable lines" if row["parse_errors"] else "")
    )
    warnings = json.loads(row["warnings"] or "[]")
    for warning in warnings[: args.max_warnings]:
        print(f"  ! {warning}")
    print()

    def walk(parent_id: str | None, prefix: str, depth: int) -> None:
        kids = children.get(parent_id, [])
        for i, node in enumerate(kids):
            last = i == len(kids) - 1
            branch = f"{prefix}{'`- ' if last else '|- '}" if depth else ""

            label = node["label"] or node["kind"]
            if node["kind"] == KIND_TOOL:
                label = node["tool_name"] or label
            label = f"{node['kind']} {label}"[: max(20, 52 - len(branch))]

            dur = _fmt_ms(node["duration_ms"]) if node["duration_ms"] else ""
            tok = node["input_tokens"] + node["output_tokens"]
            flags = ""
            if node["status"] != "ok":
                flags += f"  <{node['status']}>"
            if node["kind"] == KIND_SUBAGENT:
                flags += f"  [{node['link_basis']}]"

            print(
                f"{branch}{label:<{max(20, 54 - len(branch))}}{dur:>9}"
                f"{('  ' + format(tok, ',') + 'tok') if tok else '':>14}{flags}"
            )
            if depth < args.depth:
                walk(node["node_id"], prefix + ("   " if last else "|  "), depth + 1)
            elif children.get(node["node_id"]):
                deeper = len(children[node["node_id"]])
                pad = prefix + ("   " if last else "|  ")
                print(f"{pad}`- ... {deeper} more node(s), raise --depth")

    root = children.get(None, [])
    for node in root:
        print(f"{node['kind']} {node['label'][:44]}")
        walk(node["node_id"], "", 1)
    return 0



def _usd(value: float | None) -> str:
    """Format money, or say plainly that we could not price it.

    An unpriced run must never render as $0.00 -- free and unpriced are
    different facts.
    """
    return "unpriced" if value is None else f"${value:,.4f}"


def _resolve_session(store: Store, prefix: str) -> dict[str, Any] | None:
    matches = [
        r for r in store.transcript_sessions(limit=500)
        if r["session_id"].startswith(prefix)
    ]
    return matches[0] if matches else None


def _load_run_cost(store: Store, session_id: str, priced_at):
    from .cost import PriceTable, attribute_cost

    nodes = store.tree_nodes(session_id)
    records = store.transcript_records_for(session_id)
    scope = store.record_scope_for(session_id)
    run = attribute_cost(
        nodes, records, scope, PriceTable(), priced_at, session_id
    )
    return run, nodes, records, scope


def _priced_at(args: argparse.Namespace, row: dict[str, Any]):
    """The date to price a run at.

    Defaults to the run's own start time, not today: a run from March is
    costed at March's prices, which is the entire reason the price table is
    dated. `--at` overrides it for asking what a past run would cost now.
    """
    from datetime import date, datetime, timezone

    if getattr(args, "at", None):
        return date.fromisoformat(args.at)
    start_ns = row.get("start_ns") or 0
    if start_ns:
        return datetime.fromtimestamp(start_ns / 1e9, tz=timezone.utc).date()
    # No usable start time: fall back to today in UTC, and say so, because
    # the price date silently changing the figure would be worse.
    print("  (run has no start time; pricing at today's UTC date)", file=sys.stderr)
    return datetime.now(tz=timezone.utc).date()


def cmd_cost(args: argparse.Namespace) -> int:
    """Attribute cost across one session's tree."""
    store = Store(args.db)
    row = _resolve_session(store, args.session)
    if row is None:
        print(f"no parsed session matching {args.session!r}", file=sys.stderr)
        print("try: contrail parse", file=sys.stderr)
        return 1

    priced_at = _priced_at(args, row)
    run, nodes, _records, _scope = _load_run_cost(store, row["session_id"], priced_at)
    if not nodes:
        print("session has no stored tree -- try: contrail parse", file=sys.stderr)
        return 1

    tokens = run.total_tokens
    print(f"session {row['session_id']}  [{row['project_slug']}]")
    print(f"  priced at {priced_at.isoformat()} prices")
    print(f"  {_usd(run.total_usd)}  -  {tokens.billable_total:,} billable tokens")
    print(
        f"  in {tokens.input:,} / out {tokens.output:,} / "
        f"cache read {tokens.cache_read:,} / "
        f"write 5m {tokens.cache_write_5m:,} / write 1h {tokens.cache_write_1h:,}"
    )
    if tokens.thinking:
        print(f"  thinking {tokens.thinking:,} (billed inside output, not added)")
    for warning in run.warnings:
        print(f"  ! {warning}")
    if run.unpriced_records:
        print(f"  ! unpriced_records: {run.unpriced_records} -- the figure above is a floor")
    print()

    subagents = [n for n in run.by_kind("subagent") if n.total_tokens.billable_total]
    if subagents:
        print(f"{'SUBAGENT':<44}{'COST':>13}{'TOKENS':>16}")
        for cost in subagents[: args.limit]:
            print(
                f"{cost.label[:42]:<44}{_usd(cost.total_usd):>13}"
                f"{cost.total_tokens.billable_total:>16,}"
            )
        if len(subagents) > args.limit:
            print(f"... and {len(subagents) - args.limit} more, raise --limit")
        print()

    turns = [n for n in run.by_kind("turn") if n.total_tokens.billable_total]
    if turns:
        print(f"{'TURN':<44}{'COST':>13}{'SELF':>13}")
        for cost in turns[: args.limit]:
            print(
                f"{cost.label[:42]:<44}{_usd(cost.total_usd):>13}"
                f"{_usd(cost.self_usd):>13}"
            )
        if len(turns) > args.limit:
            print(f"... and {len(turns) - args.limit} more, raise --limit")
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    """Run the three reconciliation layers over one session.

    Each layer prints what it proves, because they prove different things and
    only the first is a correctness test.
    """
    from .cost import (
        check_against_counter,
        check_attribution_invariant,
        check_cross_source,
    )

    store = Store(args.db)
    row = _resolve_session(store, args.session)
    if row is None:
        print(f"no parsed session matching {args.session!r}", file=sys.stderr)
        return 1

    priced_at = _priced_at(args, row)
    run, _nodes, records, scope = _load_run_cost(store, row["session_id"], priced_at)
    spans = store.llm_request_spans_for_session(row["session_id"])

    results = [
        check_attribution_invariant(run, records, scope),
        check_cross_source(records, spans),
    ]
    if args.counter:
        counter = json.loads(Path(args.counter).read_text(encoding="utf-8"))
        results.append(check_against_counter(run, counter, args.tolerance))

    print(f"session {row['session_id']}  priced at {priced_at.isoformat()}")
    print(f"computed {_usd(run.total_usd)}\n")

    failed = 0
    for i, result in enumerate(results, start=1):
        mark = "PASS" if result.ok else "FAIL"
        failed += not result.ok
        print(f"Layer {i} -- {result.layer}: {mark}")
        print(f"  proves: {result.proves}")
        for key, value in result.detail.items():
            print(f"    {key}: {value}")
        for note in result.notes:
            print(f"    note: {note}")
        print()

    if not args.counter:
        print("Layer 3 skipped: pass --counter with a JSON {model: usd} export of")
        print("claude_code.cost.usage. Note that counter is Claude Code's own")
        print("client-side estimate, not a billing figure.")
    # Only Layer 1 is a correctness test; a Layer 2/3 miss is information.
    return 1 if not results[0].ok else 0



def cmd_findings(args: argparse.Namespace) -> int:
    """Run the detectors over one parsed session.

    Three of the four detectors work off stored data. Outcome divergence is
    not among them: it needs groups of agents known to share a task, and
    identifying those requires normalising a prompt template -- the corpus's
    verifier triples differ only by a voter index -- which is workflow-specific
    and cannot be constructed generically from the store. It is available as
    `detectors.detect_outcome_divergence` for a caller that can supply the
    grouping, and docs/spec.md states that precondition.
    """
    from .cost import PriceTable, attribute_cost
    from .detectors import (
        COST_CONCENTRATION,
        detect_cost_concentration,
        detect_redundant_repeats,
        detect_unhandled_errors,
    )

    store = Store(args.db)
    row = _resolve_session(store, args.session)
    if row is None:
        print(f"no parsed session matching {args.session!r}", file=sys.stderr)
        print("try: contrail parse", file=sys.stderr)
        return 1

    session_id = row["session_id"]
    records = store.transcript_records_for(session_id)
    scope = store.record_scope_for(session_id)
    nodes = store.tree_nodes(session_id)
    if not records:
        print("session has no stored records -- try: contrail parse",
              file=sys.stderr)
        return 1

    findings = []
    wanted = args.detector
    if wanted in (None, "repeats"):
        findings += detect_redundant_repeats(records, scope, session_id)
    if wanted in (None, "cost"):
        priced_at = _priced_at(args, row)
        run = attribute_cost(nodes, records, scope, PriceTable(),
                             priced_at, session_id)
        findings += detect_cost_concentration(
            run, share_threshold=args.share_threshold,
            min_tokens=args.min_tokens, session_id=session_id,
        )
    if wanted in (None, "errors"):
        findings += detect_unhandled_errors(records, scope, session_id)

    print(f"session {session_id}  [{row['project_slug']}]")
    print(f"  {len(findings)} finding(s) from {len(records):,} records\n")
    if not findings:
        print("  nothing flagged")
        return 0

    labels = {n["node_id"]: f"{n['kind']} {n['label']}" for n in nodes}
    for finding in findings:
        mark = "" if finding.confidence == "high" else f" ({finding.confidence} confidence)"
        subtype = f"/{finding.subtype}" if finding.subtype else ""
        print(f"[{finding.detector}{subtype}]{mark}")
        print(f"  {finding.summary}")
        if finding.node_id:
            print(f"  in: {labels.get(finding.node_id, finding.node_id)[:70]}")
        if args.evidence:
            for key, value in finding.evidence.items():
                text = str(value)
                if len(text) > 96:
                    text = text[:93] + "..."
                print(f"    {key}: {text}")
        print()

    by_detector: dict[str, int] = {}
    for finding in findings:
        key = finding.detector + (f"/{finding.subtype}" if finding.subtype else "")
        by_detector[key] = by_detector.get(key, 0) + 1
    print("summary: " + ", ".join(f"{k} x{v}" for k, v in sorted(by_detector.items())))
    if any(f.detector == COST_CONCENTRATION for f in findings):
        print("note: cost-concentration thresholds are corpus-tuned defaults; "
              "see --share-threshold / --min-tokens")
    return 0


def _make_stdout_safe() -> None:
    """Never let an unencodable character crash the CLI.

    The target environment is Windows PowerShell, whose console codepage is
    cp1252 and cannot encode box-drawing characters or a middot. Printing one
    raises UnicodeEncodeError and takes the command down -- which is exactly
    how `contrail show` failed on a clean install. Output glyphs are ASCII for
    that reason; this is the belt to that braces.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="replace")
        except (ValueError, OSError):  # a stream that cannot be reconfigured
            pass


def main(argv: list[str] | None = None) -> int:
    _make_stdout_safe()
    parser = argparse.ArgumentParser(prog="contrail", description=__doc__)
    parser.add_argument("--db", default=os.environ.get("CONTRAIL_DB", "contrail.db"))
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="start the OTLP collector")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4318)
    p.add_argument("--reload", action="store_true")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("traces", help="list recent OTel traces")
    p.add_argument("--limit", type=int, default=25)
    p.set_defaults(func=cmd_traces)

    p = sub.add_parser("show", help="print one trace as a tree")
    p.add_argument("trace_id", help="full or partial trace id")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("demo", help="insert a synthetic run")
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("parse", help="read session transcripts from disk")
    p.add_argument("--root", help="Claude Code projects dir (default: ~/.claude/projects)")
    p.add_argument("--session", help="only sessions whose id starts with this")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--max-warnings", type=int, default=5)
    p.set_defaults(func=cmd_parse)

    p = sub.add_parser("sessions", help="list parsed sessions")
    p.add_argument("--limit", type=int, default=25)
    p.set_defaults(func=cmd_sessions)

    p = sub.add_parser("tree", help="print one reconstructed run tree")
    p.add_argument("session", help="full or partial session id")
    p.add_argument("--depth", type=int, default=3)
    p.add_argument("--max-warnings", type=int, default=5)
    p.set_defaults(func=cmd_tree)

    p = sub.add_parser("cost", help="attribute cost across one run tree")
    p.add_argument("session", help="full or partial session id")
    p.add_argument("--at", help="price at this ISO date instead of the run's own")
    p.add_argument("--limit", type=int, default=15)
    p.set_defaults(func=cmd_cost)

    p = sub.add_parser("reconcile", help="run the three reconciliation layers")
    p.add_argument("session", help="full or partial session id")
    p.add_argument("--at", help="price at this ISO date instead of the run's own")
    p.add_argument("--counter", help="JSON {model: usd} from claude_code.cost.usage")
    p.add_argument("--tolerance", type=float, default=0.02)
    p.set_defaults(func=cmd_reconcile)

    from .detectors import DEFAULT_MIN_TOKENS, DEFAULT_SHARE_THRESHOLD

    p = sub.add_parser("findings", help="run the detectors over one session")
    p.add_argument("session", help="full or partial session id")
    p.add_argument("--detector", choices=("repeats", "cost", "errors"),
                   help="run only one detector (default: all applicable)")
    p.add_argument("--at", help="price at this ISO date instead of the run's own")
    p.add_argument("--share-threshold", type=float,
                   default=DEFAULT_SHARE_THRESHOLD,
                   help="cost concentration: a child's share of its parent "
                        "(corpus-tuned default, not a rule)")
    p.add_argument("--min-tokens", type=int, default=DEFAULT_MIN_TOKENS,
                   help="cost concentration: absolute floor below which "
                        "concentration is not reported")
    p.add_argument("--evidence", action="store_true",
                   help="print the evidence behind each finding")
    p.set_defaults(func=cmd_findings)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
