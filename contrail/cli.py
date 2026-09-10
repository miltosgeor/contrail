"""Command line entry point.

    contrail serve                 start the collector
    contrail runs                  list recent runs (OTel spans)
    contrail show <trace_id>       print one run as a tree (OTel spans)
    contrail demo                  load a synthetic run so the UI has content

    contrail parse                 read session transcripts from disk
    contrail sessions              list parsed sessions
    contrail tree <session_id>     print one reconstructed run tree

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

from .store import Store
from .transcript import KIND_SUBAGENT, KIND_TOOL


def _fmt_ms(ms: float) -> str:
    return f"{ms / 1000:.1f}s" if ms >= 1000 else f"{ms:.0f}ms"


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    os.environ.setdefault("CONTRAIL_DB", args.db)
    print(f"contrail -> {args.db}")
    print(f"OTLP endpoint: http://{args.host}:{args.port}/v1/traces")
    uvicorn.run(
        "contrail.collector:app", host=args.host, port=args.port, reload=args.reload
    )
    return 0


def cmd_runs(args: argparse.Namespace) -> int:
    store = Store(args.db)
    runs = store.runs(limit=args.limit)
    if not runs:
        print("no runs yet -- start the collector and run a Claude Code task")
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
        print(f"no run matching {args.trace_id!r}", file=sys.stderr)
        return 1
    trace_id = matches[0]["trace_id"]

    run = store.run(trace_id)
    spans = store.spans_for(trace_id)
    assert run is not None

    errors = run["error_count"]
    print(f"{run['root_name']}  [{trace_id[:16]}]")
    print(
        f"  {_fmt_ms(run['duration_ms'])} · {run['span_count']} spans · "
        f"{errors} error{'' if errors == 1 else 's'} · "
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
            branch = f"{prefix}{'└─ ' if last else '├─ '}" if prefix or parent else ""
            print(f"{branch}{label:<{max(10, 46 - len(branch))}}"
                  f"{_fmt_ms(s.duration_ms):>9}{flag}")
            walk(s.span_id, prefix + ("   " if last else "│  ") if branch else "")

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

        records = store.add_transcript_records(session.records)
        for agent in session.agents.values():
            records += store.add_transcript_records(agent.records)
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
        + (f" · {row['parse_errors']} unreadable lines" if row["parse_errors"] else "")
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="contrail", description=__doc__)
    parser.add_argument("--db", default=os.environ.get("CONTRAIL_DB", "contrail.db"))
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="start the OTLP collector")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4318)
    p.add_argument("--reload", action="store_true")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("runs", help="list recent runs")
    p.add_argument("--limit", type=int, default=25)
    p.set_defaults(func=cmd_runs)

    p = sub.add_parser("show", help="print one run as a tree")
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

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
