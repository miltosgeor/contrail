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

    runs = client.get("/api/traces").json()["traces"]
    assert len(runs) == 1
    assert runs[0]["root_name"] == "claude_code.interaction"
    assert runs[0]["output_tokens"] == 500


def test_run_detail(client):
    client.post("/v1/traces", json=PAYLOAD)
    trace_id = "a1b2c3d4e5f60718a1b2c3d4e5f60718"
    body = client.get(f"/api/traces/{trace_id}").json()
    assert body["trace"]["span_count"] == 1
    assert body["spans"][0]["duration_ms"] == 2000


def test_unknown_run_404(client):
    assert client.get("/api/traces/nope").status_code == 404


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


# --- Phase 5: the page and the sessions API ------------------------------

def test_the_page_is_served_with_no_build_step(client):
    """One static file from FastAPI, so `pip install -e .` stays the whole
    setup."""
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Contrail" in r.text


def test_the_page_needs_no_external_asset(client):
    """No CDN, no bundler: it must work offline."""
    body = client.get("/").text
    assert "<script src=" not in body
    assert "//cdn" not in body and "https://" not in body.split("<style>")[0]


def test_sessions_api_is_empty_but_valid_with_no_data(client):
    """The most likely first experience a stranger has."""
    r = client.get("/api/sessions")
    assert r.status_code == 200
    assert r.json()["sessions"] == []


def test_sessions_api_reports_the_earliest_confirmed_price(client):
    """The page needs it to explain why an old session reads `unpriced`."""
    assert r"2026" in str(client.get("/api/sessions").json()["earliest_price"])


def test_an_unknown_session_is_a_404(client):
    assert client.get("/api/sessions/nope").status_code == 404


def test_traces_and_sessions_are_separate_objects(client):
    """Two paths, two key spaces: /api/traces is keyed by trace id from the
    OTLP export, /api/sessions by session id from the transcripts. Naming
    them the same thing would imply a join that does not exist."""
    assert "traces" in client.get("/api/traces").json()
    assert "sessions" in client.get("/api/sessions").json()
