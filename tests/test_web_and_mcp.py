import json
import os
import sys

from fastapi.testclient import TestClient

from ticketpilot.web.app import create_app


def sse(response):
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


def test_streams_a_run_through_approval_to_completion(settings):
    client = TestClient(create_app(settings, auto_resume=False))
    assert client.get("/healthz").json()["ok"]
    events = sse(client.post("/api/runs", json={"ticket_id": "T-2001"}))
    kinds = [e["type"] for e in events]
    assert kinds[0] == "run_started" and "draft_delta" in kinds and kinds[-1] == "awaiting_approval"
    run_id = events[0]["run_id"]
    approval = next(e for e in events if e["type"] == "approval_required")
    assert approval["preview"]["to"] == "daniel.okafor@example.com"  # humans see real values; the model doesn't

    done = sse(client.post(f"/api/approvals/{approval['approval_id']}", json={"approved": True}))
    assert done[-1]["type"] == "run_finished" and done[-1]["status"] == "completed"
    again = client.post(f"/api/approvals/{approval['approval_id']}", json={"approved": True})
    assert again.status_code == 409  # decisions are final

    trace = client.get(f"/api/runs/{run_id}/trace").json()
    assert {s["kind"] for s in trace} >= {"run", "llm", "tool", "guard", "decision", "approval"}
    snapshot = client.get(f"/api/runs/{run_id}").json()
    assert "vault" not in snapshot  # the PII vault never leaves the server

    fb = client.post(f"/api/runs/{run_id}/feedback", json={"rating": "down", "comment": "too long"}).json()
    assert os.path.exists(fb["eval_candidate"])
    metrics = client.get("/api/metrics").json()
    assert metrics["overall"]["resolved"] == 1 and metrics["feedback"] == {"down": 1}


def test_public_demo_rate_limits(settings):
    client = TestClient(create_app(settings.with_overrides(public_demo=True, rate_limit_per_min=1), auto_resume=False))
    assert client.post("/api/runs", json={"ticket_id": "T-2014"}).status_code == 200
    assert client.post("/api/runs", json={"ticket_id": "T-2014"}).status_code == 429


def test_carrier_portal_has_no_api_only_html(settings):
    client = TestClient(create_app(settings, auto_resume=False))
    page = client.get("/carrier/pacificparcel/track", params={"n": "PP200008"})
    assert page.status_code == 200 and "<table id=\"events\">" in page.text


def test_mcp_server_and_client_roundtrip(store):
    from ticketpilot.mcp_client import MCPToolSource

    env = {**os.environ, "TICKETPILOT_DB": str(store.path)}
    source = MCPToolSource("tp", sys.executable, ["-m", "ticketpilot.mcp_server"], env=env).start()
    try:
        assert {t.name for t in source.tools} == {"search_policy", "lookup_order", "track_shipment"}
        ok = source.call("lookup_order", {"customer_email": "daniel.okafor@example.com", "order_id": "A1002"})
        assert not ok.is_error and "A1002" in MCPToolSource._text(ok)
        denied = source.call("lookup_order", {"customer_email": "daniel.okafor@example.com", "order_id": "A1001"})
        assert denied.is_error and "not found" in MCPToolSource._text(denied)
    finally:
        source.close()
