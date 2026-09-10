import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTRAIL_DB", str(tmp_path / "test.db"))
    import contrail.collector as collector

    importlib.reload(collector)
    return TestClient(collector.app)


PAYLOAD = {
    "resourceSpans": [{
        "resource": {"attributes": [
            {"key": "service.name", "value": {"stringValue": "claude-code"}}]},
        "scopeSpans": [{"spans": [{
            "traceId": "a1b2c3d4e5f60718a1b2c3d4e5f60718",
            "spanId": "0000000000000001",
            "name": "claude_code.interaction",
            "startTimeUnixNano": "1757000000000000000",
            "endTimeUnixNano": "1757000002000000000",
            "attributes": [
                {"key": "gen_ai.usage.output_tokens", "value": {"intValue": "500"}}],
        }]}],
    }]
}


def test_health(client):
    body = client.get("/health").json()
    assert body["status"] == "ok" and body["spans"] == 0


def test_ingest_json_and_list(client):
    r = client.post("/v1/traces", json=PAYLOAD)
    assert r.status_code == 200
    assert r.json() == {"partialSuccess": {}}

    runs = client.get("/api/runs").json()["runs"]
    assert len(runs) == 1
    assert runs[0]["root_name"] == "claude_code.interaction"
    assert runs[0]["output_tokens"] == 500


def test_run_detail(client):
    client.post("/v1/traces", json=PAYLOAD)
    trace_id = "a1b2c3d4e5f60718a1b2c3d4e5f60718"
    body = client.get(f"/api/runs/{trace_id}").json()
    assert body["run"]["span_count"] == 1
    assert body["spans"][0]["duration_ms"] == 2000


def test_unknown_run_404(client):
    assert client.get("/api/runs/nope").status_code == 404


def test_spans_without_ids_are_rejected_not_stored(client):
    bad = {"resourceSpans": [{"resource": {"attributes": []},
                              "scopeSpans": [{"spans": [{"name": "orphan"}]}]}]}
    r = client.post("/v1/traces", json=bad)
    assert r.status_code == 200
    assert r.json()["partialSuccess"]["rejectedSpans"] == "1"
    assert client.get("/health").json()["spans"] == 0


def test_malformed_json_is_400(client):
    r = client.post(
        "/v1/traces",
        content=b"{not json",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 400


def test_repeated_export_does_not_duplicate(client):
    client.post("/v1/traces", json=PAYLOAD)
    client.post("/v1/traces", json=PAYLOAD)
    assert client.get("/health").json()["spans"] == 1
