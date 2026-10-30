"""MCP tools in-process, HTTP bearer auth, signed viewer links, and no secrets in output."""

from __future__ import annotations

import asyncio
import dataclasses
import json

import pytest

from ch_accounts_mcp import server
from ch_accounts_mcp.config import override_settings, settings
from ch_accounts_mcp.server_auth import sign

from .conftest import make_pdf


def _call(name, args):
    res = asyncio.run(server.mcp.call_tool(name, args))
    content = res[0] if isinstance(res, tuple) else res
    return [json.loads(c.text) for c in content if c.type == "text"], [c for c in content if c.type == "image"]


@pytest.fixture()
def loaded(tmp_settings, tmp_path):
    pdf = tmp_path / "accounts.pdf"
    pdf.write_bytes(make_pdf())
    (body,), _ = _call("load_accounts", {"pdf_path": str(pdf), "company_number": "1234567"})
    return body


def test_load_accounts_from_pdf_path(loaded):
    assert len(loaded["doc_id"]) == 64
    assert loaded["company_number"] == "01234567"
    assert loaded["page_count"] == 3 and loaded["document_type"] == "text_layer"
    assert {r["kind"] for r in loaded["primary_statement_pages"]} == {"income_statement", "balance_sheet"}
    assert "timings_ms" in loaded and "cache_hit" in loaded


def test_get_primary_statements_and_citation(loaded):
    (body,), imgs = _call("get_primary_statements", {"doc_id": loaded["doc_id"], "statements": ["balance"]})
    assert [s["statement"] for s in body["statements"]] == ["balance_sheet"]
    assert body["verification"]["summary"]["unverified"] == 0 and not imgs
    row = next(r for r in body["statements"][0]["rows"] if r["label"] == "Net assets")
    v = row["values"]["2024"]
    assert v["status"] == "verified" and v["provenance"] == "extracted_verified"
    (cit,), imgs = _call("get_citation", {"citation_id": v["citation_id"]})
    assert cit["raw"] == "5,000" and cit["pdf_page"] == 3 and cit["viewer_url"].startswith("http://testserver/view/")
    assert len(imgs) == 1 and imgs[0].mimeType == "image/png"
    (meta,), imgs = _call("get_page_image", {"doc_id": loaded["doc_id"], "page": 3, "dpi": 72})
    assert meta["kind"] == "balance_sheet" and len(imgs) == 1


def test_errors_are_json_and_never_leak_secrets(tmp_settings):
    secret = "sk-test-SECRET-123456"
    override_settings(dataclasses.replace(settings(), companies_house_api_key=secret))
    (body,), _ = _call("load_accounts", {"pdf_path": f"/nonexistent/{secret}.pdf"})
    assert "error" in body and secret not in json.dumps(body)
    (body,), _ = _call("get_primary_statements", {"doc_id": "not-a-hash"})
    assert "error" in body


def test_settings_repr_hides_secrets():
    s = dataclasses.replace(settings(), companies_house_api_key="abc-secret", mcp_bearer_token="tok-secret")
    assert "abc-secret" not in repr(s) and "tok-secret" not in repr(s)
    assert "abc-secret" not in json.dumps(s.redacted())


def test_http_bearer_auth_and_signed_viewer(loaded, tmp_settings):
    from starlette.testclient import TestClient

    override_settings(dataclasses.replace(settings(), mcp_bearer_token="test-token"))
    sha = loaded["doc_id"]
    _call("get_primary_statements", {"doc_id": sha})
    app = server.http_app("127.0.0.1", 8000)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        assert client.get("/healthz").status_code == 200
        assert client.post("/mcp", json={}).status_code == 401
        assert client.post("/mcp", json={}, headers={"Authorization": "Bearer wrong"}).status_code == 401
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
        r = client.post("/mcp", json=init, headers={"Authorization": "Bearer test-token", "Accept": "application/json, text/event-stream"})
        assert r.status_code == 200
        assert client.get(f"/view/{sha}").status_code == 403
        assert client.get(f"/api/doc/{sha}?s=bad").status_code == 403
        sig = sign(sha)
        r = client.get(f"/view/{sha}?s={sig}")
        assert r.status_code == 200 and "VIEWER_CONFIG" in r.text and "test-token" not in r.text
        doc = client.get(f"/api/doc/{sha}?s={sig}").json()
        assert doc["page_count"] == 3
        cits = client.get(f"/api/citations/{sha}?s={sig}").json()["citations"]
        cid = cits[0]["citation_id"]
        assert all("test-token" not in json.dumps(c) for c in cits)
        r = client.get(f"/crop/{sha}/{cid}.png?s={sig}")
        assert r.status_code == 200 and r.headers["content-type"] == "image/png"
        assert client.get(f"/tile/{sha}/1.png?s={sig}").status_code == 200
        assert client.get(f"/crop/{sha}/{cid}.png", headers={"Authorization": "Bearer test-token"}).status_code == 200
