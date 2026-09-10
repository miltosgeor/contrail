import base64

import pytest

from contrail.otlp import OtlpDecodeError, _hex_id, decode_json


def envelope(spans, resource_attrs=None):
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": resource_attrs
                    or [{"key": "service.name", "value": {"stringValue": "claude-code"}}]
                },
                "scopeSpans": [{"spans": spans}],
            }
        ]
    }


RAW_SPAN = {
    "traceId": "a1b2c3d4e5f60718a1b2c3d4e5f60718",
    "spanId": "0000000000000001",
    "name": "claude_code.llm_request",
    "kind": 3,
    "startTimeUnixNano": "1757000000000000000",
    "endTimeUnixNano": "1757000001500000000",
    "attributes": [
        {"key": "gen_ai.request.model", "value": {"stringValue": "claude-opus-4"}},
        {"key": "gen_ai.usage.input_tokens", "value": {"intValue": "1200"}},
    ],
}


def test_decodes_a_span():
    spans = decode_json(envelope([RAW_SPAN]))
    assert len(spans) == 1
    s = spans[0]
    assert s.name == "claude_code.llm_request"
    assert s.model == "claude-opus-4"
    assert s.input_tokens == 1200
    assert s.duration_ms == 1500
    assert s.service_name == "claude-code"


def test_resource_attributes_merge_into_span():
    spans = decode_json(envelope([RAW_SPAN]))
    assert spans[0].attributes["service.name"] == "claude-code"


def test_span_attributes_win_over_resource_attributes():
    spans = decode_json(
        envelope(
            [{**RAW_SPAN, "attributes": [
                {"key": "service.name", "value": {"stringValue": "override"}}]}],
        )
    )
    assert spans[0].attributes["service.name"] == "override"


def test_missing_parent_becomes_none():
    assert decode_json(envelope([RAW_SPAN]))[0].parent_span_id is None


def test_parent_is_preserved():
    raw = {**RAW_SPAN, "parentSpanId": "00000000000000ff"}
    assert decode_json(envelope([raw]))[0].parent_span_id == "00000000000000ff"


def test_status_is_read():
    raw = {**RAW_SPAN, "status": {"code": 2, "message": "boom"}}
    s = decode_json(envelope([raw]))[0]
    assert s.is_error and s.status_message == "boom"


def test_empty_envelope_is_not_an_error():
    assert decode_json({"resourceSpans": []}) == []


def test_non_object_payload_raises():
    with pytest.raises(OtlpDecodeError):
        decode_json([])  # type: ignore[arg-type]


# --- id normalisation ------------------------------------------------------

def test_hex_id_passes_hex_through_lowercased():
    assert _hex_id("A1B2") == "a1b2"


def test_hex_id_decodes_base64_bytes():
    raw = bytes.fromhex("a1b2c3d4e5f60718a1b2c3d4e5f60718")
    assert _hex_id(base64.b64encode(raw).decode()) == raw.hex()


def test_hex_id_accepts_raw_bytes():
    assert _hex_id(b"\x01\x02") == "0102"


def test_hex_id_of_empty_is_empty():
    assert _hex_id(None) == "" and _hex_id("") == ""


def test_array_and_nested_attribute_values():
    raw = {
        **RAW_SPAN,
        "attributes": [
            {"key": "tags", "value": {"arrayValue": {"values": [
                {"stringValue": "a"}, {"stringValue": "b"}]}}},
            {"key": "nested", "value": {"kvlistValue": {"values": [
                {"key": "k", "value": {"boolValue": True}}]}}},
        ],
    }
    attrs = decode_json(envelope([raw]))[0].attributes
    assert attrs["tags"] == ["a", "b"]
    assert attrs["nested"] == {"k": True}
