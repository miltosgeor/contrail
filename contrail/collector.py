"""The OTLP receiver.

Exposes the endpoint Claude Code exports to, plus a small read API the
Phase 5 UI will consume. Nothing here interprets agent behaviour -- that is
Phase 4's job. This layer only accepts, normalises and persists.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse

from .cost import PriceTable, attribute_cost
from .detectors import run_all
from .otlp import HAS_PROTOBUF, OtlpDecodeError, decode_json, decode_protobuf
from .store import Store

DB_PATH = os.environ.get("CONTRAIL_DB", "contrail.db")

app = FastAPI(
    title="Contrail",
    description="Trace store for Claude agent runs.",
    version="0.1.0",
)
store = Store(DB_PATH)


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "db": DB_PATH,
        "protobuf": HAS_PROTOBUF,
        **store.counts(),
    }


@app.post("/v1/traces")
async def receive_traces(request: Request) -> Response:
    """OTLP/HTTP trace endpoint.

    Point Claude Code here:

        export CLAUDE_CODE_ENABLE_TELEMETRY=1
        export CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1
        export OTEL_TRACES_EXPORTER=otlp
        export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
        export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
    """
    body = await request.body()
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip()

    try:
        if content_type == "application/json":
            spans = decode_json(json.loads(body or b"{}"))
        else:
            spans = decode_protobuf(body)
    except OtlpDecodeError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"invalid JSON: {exc}") from exc

    # Spans without a trace id cannot be grouped into a run, so drop them
    # rather than silently corrupting the run table.
    usable = [s for s in spans if s.trace_id and s.span_id]
    store.add_spans(usable)

    # OTLP expects a (possibly partial) success envelope, not a bare 200.
    rejected = len(spans) - len(usable)
    payload: dict[str, Any] = {"partialSuccess": {}}
    if rejected:
        payload["partialSuccess"] = {
            "rejectedSpans": str(rejected),
            "errorMessage": "spans missing trace_id or span_id",
        }
    return Response(content=json.dumps(payload), media_type="application/json")


# ------------------------------------------------------------------ the page
#
# One static file, no build step. A read-only view over a JSON API needs no
# framework, and a node toolchain would make the UI likelier to become the
# project -- which docs/spec.md names as this repo's most likely failure mode.
# If index.html outgrows ~800 lines, that is the signal to stop, not to reach
# for a bundler.

PAGE = Path(__file__).with_name("index.html")


@app.get("/", response_class=HTMLResponse)
def page() -> HTMLResponse:
    return HTMLResponse(PAGE.read_text(encoding="utf-8"))


# ----------------------------------------------------------- traces (OTel)
#
# Keyed by trace id, sourced from the OTLP export. Distinct from
# /api/sessions, which is the transcript path keyed by session id.

@app.get("/api/traces")
def list_traces(limit: int = 25) -> dict[str, Any]:
    return {"traces": store.runs(limit=limit)}


@app.get("/api/traces/{trace_id}")
def get_trace(trace_id: str) -> dict[str, Any]:
    run = store.run(trace_id)
    if run is None:
        raise HTTPException(status_code=404, detail="no such trace")
    spans = store.spans_for(trace_id)
    return {
        "trace": run,
        "spans": [
            {
                "span_id": s.span_id,
                "parent_span_id": s.parent_span_id,
                "name": s.name,
                "duration_ms": round(s.duration_ms, 2),
                "tool_name": s.tool_name,
                "model": s.model,
                "agent_type": s.agent_type,
                "is_error": s.is_error,
                "input_tokens": s.input_tokens,
                "output_tokens": s.output_tokens,
                "cache_read_tokens": s.cache_read_tokens,
                "cache_creation_tokens": s.cache_creation_tokens,
            }
            for s in spans
        ],
    }


# --------------------------------------------------------- sessions (Phase 5)
#
# Two different objects, deliberately named apart: /api/traces is the OTel
# span path keyed by trace id, /api/sessions the transcript path keyed by
# session id. "run" is left free for the contiguous-segment idea -- a single
# session file can span months of resumes, so a run is not a session.


def _price_date(row: dict[str, Any], at: str | None) -> date:
    """The date to price a session at.

    Defaults to the session's own start, so a session recorded in March stays
    costed at March's prices. `at` is a deliberate counterfactual: what this
    would cost at some other date's rates.
    """
    if at:
        return date.fromisoformat(at)
    start_ns = row.get("start_ns") or 0
    if start_ns:
        return datetime.fromtimestamp(start_ns / 1e9, tz=timezone.utc).date()
    return datetime.now(tz=timezone.utc).date()


def _session_payload(row: dict[str, Any], at: str | None) -> dict[str, Any]:
    """Summary for one session: tokens always, dollars where known.

    Tokens lead because they are always known and never unpriced. USD is an
    enrichment -- a session older than the earliest confirmed price reports
    `unpriced_before` rather than a figure we cannot vouch for.
    """
    session_id = row["session_id"]
    records = store.transcript_records_for(session_id)
    scope = store.record_scope_for(session_id)
    nodes = store.tree_nodes(session_id)
    priced_at = _price_date(row, at)
    run = attribute_cost(nodes, records, scope, PriceTable(), priced_at, session_id)
    findings = run_all(records, scope, run_cost=run, session_id=session_id)

    tokens = run.total_tokens
    return {
        "session_id": session_id,
        "project": row.get("project_slug") or "",
        "priced_at": priced_at.isoformat(),
        "total_usd": run.total_usd,
        "unpriced_records": run.unpriced_records,
        "tokens": {
            "billable": tokens.billable_total,
            "input": tokens.input,
            "output": tokens.output,
            "cache_read": tokens.cache_read,
            "cache_write_5m": tokens.cache_write_5m,
            "cache_write_1h": tokens.cache_write_1h,
            "thinking": tokens.thinking,
        },
        "node_count": row.get("node_count") or 0,
        "agent_count": row.get("agent_count") or 0,
        "record_count": row.get("record_count") or 0,
        "parse_errors": row.get("parse_errors") or 0,
        "finding_count": len(findings),
    }, run, findings, nodes


@app.get("/api/sessions")
def list_sessions(limit: int = 25, at: str | None = None) -> dict[str, Any]:
    """Sessions ranked for a verdict-first front door.

    Ordered by finding count then tokens, not by recency: the point of the
    list is to say which session is worth opening.
    """
    rows = store.transcript_sessions(limit=limit)
    summaries = [_session_payload(row, at)[0] for row in rows]
    summaries.sort(
        key=lambda s: (-s["finding_count"], -s["tokens"]["billable"])
    )
    return {
        "sessions": summaries,
        "earliest_price": min(
            (p.effective_from.isoformat() for p in PriceTable().prices),
            default=None,
        ),
    }


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str, at: str | None = None) -> dict[str, Any]:
    """One session: summary, tree, per-node cost and findings, in one response.

    Composite on purpose -- the page would otherwise make one request per
    node to colour the tree.
    """
    row = store.transcript_session(session_id)
    if row is None:
        matches = [
            r for r in store.transcript_sessions(limit=500)
            if r["session_id"].startswith(session_id)
        ]
        if not matches:
            raise HTTPException(status_code=404, detail="no such session")
        row = matches[0]

    summary, run, findings, nodes = _session_payload(row, at)
    costs = run.nodes

    tree = []
    for node in nodes:
        cost = costs.get(node["node_id"])
        tree.append({
            "node_id": node["node_id"],
            "parent_node_id": node["parent_node_id"],
            "kind": node["kind"],
            "label": node["label"],
            "depth": node["depth"],
            "status": node["status"],
            "link_basis": node["link_basis"],
            "tool_name": node["tool_name"],
            "agent_type": node["agent_type"],
            "duration_ms": node["duration_ms"],
            "note": node["note"],
            "tokens": cost.total_tokens.billable_total if cost else 0,
            "self_tokens": cost.self_tokens.billable_total if cost else 0,
            "total_usd": cost.total_usd if cost else None,
            "unpriced_records": cost.unpriced_records if cost else 0,
        })

    return {
        "summary": summary,
        "tree": tree,
        "findings": [
            {
                "detector": f.detector,
                "subtype": f.subtype,
                "confidence": f.confidence,
                "summary": f.summary,
                "node_id": f.node_id,
                "evidence": {k: v for k, v in f.evidence.items()},
            }
            for f in findings
        ],
        "not_detected": NOT_DETECTED,
    }


# Stated in the product, not only in the docs. A tool that says what it
# cannot see is making a stronger claim than one that says it in a README.
NOT_DETECTED: list[dict[str, str]] = [
    {
        "what": "execution-path divergence between two runs of a task",
        "why": (
            "tested against 25 real verifier triples and dropped: mean "
            "pairwise path distance was 0.878 in the 3 that split on outcome "
            "against 0.839 in the 22 that agreed, with the ranges overlapping "
            "and the most path-divergent triple being one that agreed"
        ),
    },
    {
        "what": "outcome divergence, on this screen",
        "why": (
            "implemented and tested against those same 25 triples, but it "
            "needs groups of agents known to share a task -- which requires "
            "normalising a prompt template and cannot be derived generically"
        ),
    },
    {
        "what": "whether a tool result was correct",
        "why": "this says what happened and what it cost, never whether the output was good",
    },
    {
        "what": "cost from billing",
        "why": (
            "USD is computed from a dated price table, and Claude Code's own "
            "cost counter is a client-side estimate rather than a billing "
            "figure -- agreement between them checks the table, not the bill"
        ),
    },
]
