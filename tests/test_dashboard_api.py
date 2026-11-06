"""Dashboard approval travels through the store to the agent process, like a real cross-process approval."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from conftest import USERS, triage_by_hand  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

API = Path(__file__).resolve().parents[1] / "kata" / "dashboard" / "plugin_api.py"


def _client(db: Path, monkeypatch) -> TestClient:
    monkeypatch.setenv("KATA_DB_PATH", str(db))
    for name in [m for m in sys.modules if m.startswith("kata_dashboard_core")]:
        del sys.modules[name]
    spec = importlib.util.spec_from_file_location("kata_api_under_test", API)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    app = FastAPI()
    app.include_router(mod.router, prefix="/api/plugins/kata")
    return TestClient(app)


def test_dashboard_approve_registers_in_agent_process(host, tmp_path, monkeypatch):
    for u in USERS[:4]:
        triage_by_hand(host, u)
    host.runtime.service.mine()
    client = _client(tmp_path / "kata.db", monkeypatch)

    katas = client.get("/api/plugins/kata/katas").json()["katas"]
    assert len(katas) == 1 and katas[0]["status"] == "candidate"
    detail = client.get(f"/api/plugins/kata/katas/{katas[0]['id']}").json()
    assert detail["validation"]["passed"] is True
    assert [s["tool"] for s in detail["spec"]["steps"]] == ["search_files", "read_file", "terminal"]

    bad = client.post(f"/api/plugins/kata/katas/{katas[0]['id']}/approve", json={"name": "Bad Name!"})
    assert bad.status_code == 409

    ok = client.post(f"/api/plugins/kata/katas/{katas[0]['id']}/approve", json={"name": "triage"})
    assert ok.status_code == 200 and ok.json()["name"] == "kata_triage"

    assert "kata_triage" not in host.tools          # agent process has not looked yet
    host.runtime.on_pre_llm_call()                  # next turn: generation changed
    assert "kata_triage" in host.tools

    audit = client.get("/api/plugins/kata/audit").json()["audit"]
    assert any(a["actor"] == "user:dashboard" and a["action"] == "active" for a in audit)
    assert client.get("/api/plugins/kata/overview").json()["katas"] == {"active": 1}
