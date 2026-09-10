"""The OTLP receiver.

Exposes the endpoint Claude Code exports to, plus a small read API the
Phase 5 UI will consume. Nothing here interprets agent behaviour -- that is
Phase 4's job. This layer only accepts, normalises and persists.
"""

from __future__ import annotations

import json
import os
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response

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


# ------------------------------------------------------------------ read API

@app.get("/api/runs")
def list_runs(limit: int = 25) -> dict[str, Any]:
    return {"runs": store.runs(limit=limit)}


@app.get("/api/runs/{trace_id}")
def get_run(trace_id: str) -> dict[str, Any]:
    run = store.run(trace_id)
    if run is None:
        raise HTTPException(status_code=404, detail="no such run")
    spans = store.spans_for(trace_id)
    return {
        "run": run,
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
