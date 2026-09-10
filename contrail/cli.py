"""Command line entry point.

    contrail serve                 start the collector
    contrail runs                  list recent runs
    contrail show <trace_id>       print one run as a tree
    contrail demo                  load a synthetic run so the UI has content
"""

from __future__ import annotations

import argparse
import os
import sys

from .store import Store


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

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
