"""OTLP decoding.

Claude Code exports over OTLP/HTTP. The default protocol is http/protobuf,
but OTLP/JSON is far easier to hand-craft in a test or a curl, so both are
supported here and both land on the same `Span` shape.

Protobuf support is optional: if `opentelemetry-proto` is not installed the
JSON path still works and the collector says so clearly rather than failing
with an import error at request time.
"""

from __future__ import annotations

import base64
from typing import Any

from .models import Span

try:  # pragma: no cover - exercised by the import, not by tests
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
        ExportTraceServiceRequest,
    )

    HAS_PROTOBUF = True
except ImportError:  # pragma: no cover
    ExportTraceServiceRequest = None  # type: ignore[assignment]
    HAS_PROTOBUF = False


class OtlpDecodeError(ValueError):
    """Raised when a payload is not decodable OTLP."""


# ------------------------------------------------------------------ helpers

def _hex_id(raw: Any) -> str:
    """Normalise a trace/span id to lowercase hex.

    OTLP/JSON specifies hex strings, but several exporters emit standard
    protobuf-JSON, which base64-encodes `bytes`. Accept either.
    """
    if raw in (None, "", b""):
        return ""
    if isinstance(raw, bytes):
        return raw.hex()
    text = str(raw)
    try:
        int(text, 16)
        return text.lower()
    except ValueError:
        pass
    try:
        return base64.b64decode(text + "=" * (-len(text) % 4)).hex()
    except Exception as exc:  # noqa: BLE001
        raise OtlpDecodeError(f"unrecognised id encoding: {raw!r}") from exc


def _json_any_value(value: dict[str, Any]) -> Any:
    """Unwrap an OTLP/JSON AnyValue into a plain Python value."""
    if not isinstance(value, dict):
        return value
    if "stringValue" in value:
        return value["stringValue"]
    if "intValue" in value:
        return int(value["intValue"])
    if "doubleValue" in value:
        return float(value["doubleValue"])
    if "boolValue" in value:
        return bool(value["boolValue"])
    if "bytesValue" in value:
        return value["bytesValue"]
    if "arrayValue" in value:
        return [_json_any_value(v) for v in value["arrayValue"].get("values", [])]
    if "kvlistValue" in value:
        return _json_attributes(value["kvlistValue"].get("values", []))
    return None


def _json_attributes(items: list[dict[str, Any]] | None) -> dict[str, Any]:
    return {
        item["key"]: _json_any_value(item.get("value", {}))
        for item in (items or [])
        if "key" in item
    }


def _proto_any_value(value: Any) -> Any:
    which = value.WhichOneof("value")
    if which is None:
        return None
    if which == "array_value":
        return [_proto_any_value(v) for v in value.array_value.values]
    if which == "kvlist_value":
        return {kv.key: _proto_any_value(kv.value) for kv in value.kvlist_value.values}
    return getattr(value, which)


def _proto_attributes(items: Any) -> dict[str, Any]:
    return {kv.key: _proto_any_value(kv.value) for kv in items}


# ------------------------------------------------------------------ decoders

def decode_json(payload: dict[str, Any]) -> list[Span]:
    """Decode an OTLP/JSON ExportTraceServiceRequest into Spans."""
    if not isinstance(payload, dict):
        raise OtlpDecodeError("payload must be a JSON object")

    spans: list[Span] = []
    for resource_span in payload.get("resourceSpans", []):
        resource_attrs = _json_attributes(
            resource_span.get("resource", {}).get("attributes", [])
        )
        service_name = str(resource_attrs.get("service.name", ""))

        for scope_span in resource_span.get("scopeSpans", []):
            for raw in scope_span.get("spans", []):
                attributes = {**resource_attrs, **_json_attributes(raw.get("attributes"))}
                status = raw.get("status") or {}
                spans.append(
                    Span(
                        trace_id=_hex_id(raw.get("traceId")),
                        span_id=_hex_id(raw.get("spanId")),
                        parent_span_id=_hex_id(raw.get("parentSpanId")) or None,
                        name=raw.get("name", ""),
                        kind=int(raw.get("kind", 0) or 0),
                        start_ns=int(raw.get("startTimeUnixNano", 0) or 0),
                        end_ns=int(raw.get("endTimeUnixNano", 0) or 0),
                        status_code=int(status.get("code", 0) or 0),
                        status_message=status.get("message", "") or "",
                        service_name=service_name,
                        attributes=attributes,
                    )
                )
    return spans


def decode_protobuf(data: bytes) -> list[Span]:
    """Decode a binary OTLP/protobuf ExportTraceServiceRequest into Spans."""
    if not HAS_PROTOBUF:
        raise OtlpDecodeError(
            "protobuf payload received but opentelemetry-proto is not installed. "
            "Install it, or set OTEL_EXPORTER_OTLP_PROTOCOL=http/json."
        )

    request = ExportTraceServiceRequest()
    try:
        request.ParseFromString(data)
    except Exception as exc:  # noqa: BLE001
        raise OtlpDecodeError(f"could not parse protobuf body: {exc}") from exc

    spans: list[Span] = []
    for resource_span in request.resource_spans:
        resource_attrs = _proto_attributes(resource_span.resource.attributes)
        service_name = str(resource_attrs.get("service.name", ""))

        for scope_span in resource_span.scope_spans:
            for raw in scope_span.spans:
                attributes = {**resource_attrs, **_proto_attributes(raw.attributes)}
                spans.append(
                    Span(
                        trace_id=_hex_id(raw.trace_id),
                        span_id=_hex_id(raw.span_id),
                        parent_span_id=_hex_id(raw.parent_span_id) or None,
                        name=raw.name,
                        kind=int(raw.kind),
                        start_ns=int(raw.start_time_unix_nano),
                        end_ns=int(raw.end_time_unix_nano),
                        status_code=int(raw.status.code),
                        status_message=raw.status.message or "",
                        service_name=service_name,
                        attributes=attributes,
                    )
                )
    return spans
