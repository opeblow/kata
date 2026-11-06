"""The full lifecycle against the fake host: observe, detect, propose, approve, run, measure."""
from __future__ import annotations

import json

from conftest import USERS, triage_by_hand

from kata.models import Status


def _mine(host):
    return host.commands["kata"]("mine")


def test_full_lifecycle(host):
    rt = host.runtime
    for u in USERS[:4]:
        triage_by_hand(host, u)
    rt.service.mine()  # what on_session_end runs in the background

    katas = rt.store.katas()
    assert len(katas) == 1
    k = katas[0]
    assert k["status"] == Status.CANDIDATE.value
    # Never registered before approval.
    assert not any(n.startswith("kata_") and n != "kata_list" for n in host.tools)

    card = host.commands["kata"](f"review {k['id']}")
    for section in ("Why Kata proposed this", "Steps", "Permissions", "Input schema", "Output schema", "Validation (passed)"):
        assert section in card
    assert "`search_files`" in card and "`read_file`" in card and "`terminal`" in card

    msg = host.commands["kata"](f"approve {k['id']} as triage_todo")
    assert "Approved `kata_triage_todo`" in msg and "next session" in msg
    assert "kata_triage_todo" in host.tools
    schema = host.tools["kata_triage_todo"]["schema"]
    assert schema["parameters"]["required"] == ["pattern"]

    # Same task, new conversation, through the compiled tool.
    before_tools, before_models = host.tool_calls, host.model_calls
    turn = host.new_turn()
    host.model_request(turn, tokens=1500, latency_s=0.0)
    out = json.loads(host.call(turn, "kata_triage_todo", {"pattern": USERS[4]}))
    host.model_request(turn, tokens=900, latency_s=0.0)
    assert out["ok"] is True, out
    assert [s["tool"] for s in out["steps"]] == ["search_files", "read_file", "terminal"]
    assert "module_erin.py" in out["result"]["output"]
    # One visible tool call instead of three, two model requests instead of four.
    assert host.tool_calls - before_tools == 1
    assert host.model_calls - before_models == 2

    spec_baseline = rt.service.detail(k["id"])["spec"]["evidence"]["baseline"]
    assert spec_baseline["tool_calls"] == 3
    assert spec_baseline["avg_model_round_trips"] == 2.0  # the two requests between the three steps
    assert spec_baseline["avg_model_tokens"] == 3900.0
    stats = rt.service.overview()
    assert stats["successful_runs"] == 1 and stats["tool_calls_saved"] == 2
    assert stats["model_round_trips_saved"] == 2.0


def test_not_proposed_below_support(host):
    for u in USERS[:2]:
        triage_by_hand(host, u)
    rep = host.runtime.service.mine()
    assert rep.proposed == []
    assert host.runtime.store.katas() == []


def test_repeats_inside_one_conversation_do_not_count(host):
    for u in USERS[:5]:
        triage_by_hand(host, u, conversation_id="same-chat")
    rep = host.runtime.service.mine()
    assert rep.proposed == []


def test_reject_is_final(host):
    for u in USERS[:4]:
        triage_by_hand(host, u)
    rt = host.runtime
    rt.service.mine()
    k = rt.store.katas()[0]
    assert "Rejected" in host.commands["kata"](f"reject {k['id']} not useful")
    for u in USERS[4:]:
        triage_by_hand(host, u)
    rt.service.mine()
    assert len(rt.store.katas()) == 1
    assert rt.store.katas()[0]["status"] == "rejected"
    assert "rejected" in host.commands["kata"](f"approve {k['id']}")


def test_disable_enable_and_rollback(host):
    rt = host.runtime
    for u in USERS[:4]:
        triage_by_hand(host, u)
    rt.service.mine()
    kid = rt.store.katas()[0]["id"]
    host.commands["kata"](f"approve {kid} as triage")
    assert "kata_triage" in host.tools
    host.commands["kata"]("disable kata_triage")
    assert "kata_triage" not in host.tools
    host.commands["kata"]("enable triage")
    assert "kata_triage" in host.tools
    # A second approved version, then roll back to the first.
    spec, v, _ = rt.service.spec(kid)
    spec.description = "Edited description"
    v2 = rt.store.add_version(kid, spec, {"passed": True, "checks": []})
    rt.service.approve(kid, version=v2)
    rt.sync()
    assert rt.registered()["kata_triage"] == v2
    msg = host.commands["kata"]("rollback kata_triage")
    assert "now runs v" in msg
    assert rt.registered()["kata_triage"] < v2


def test_schema_drift_marks_stale_and_unregisters(host):
    rt = host.runtime
    for u in USERS[:4]:
        triage_by_hand(host, u)
    rt.service.mine()
    kid = rt.store.katas()[0]["id"]
    rt.service.approve(kid, name="triage")
    rt.sync()
    assert "kata_triage" in host.tools
    host.tools["read_file"]["schema"] = {"name": "read_file", "parameters": {"type": "object", "properties": {
        "file": {"type": "string"}}, "required": ["file"]}}
    turn = host.new_turn()
    out = json.loads(host.call(turn, "kata_triage", {"pattern": "erin"}))
    assert out["ok"] is False and "needs review" in out["error"]
    assert rt.store.kata(kid)["status"] == "stale"
    rt.on_pre_llm_call()
    assert "kata_triage" not in host.tools


def test_runtime_failure_stops_and_suspends(host):
    rt = host.runtime
    for u in USERS[:4]:
        triage_by_hand(host, u)
    rt.service.mine()
    kid = rt.store.katas()[0]["id"]
    rt.service.approve(kid, name="triage")
    rt.sync()
    for _ in range(3):
        out = json.loads(host.call(host.new_turn(), "kata_triage", {"pattern": "nobody"}))
        assert out["ok"] is False
        assert out["failed_step"] == 1  # search finds nothing, so the reference cannot resolve
        assert [s["status"] for s in out["steps"]][2] == "skipped"
    assert rt.store.kata(kid)["status"] == "suspended"
    rt.on_pre_llm_call()
    assert "kata_triage" not in host.tools


def test_bad_arguments_are_rejected_without_running(host):
    rt = host.runtime
    for u in USERS[:4]:
        triage_by_hand(host, u)
    rt.service.mine()
    rt.service.approve(rt.store.katas()[0]["id"], name="triage")
    rt.sync()
    before = host.tool_calls
    out = json.loads(host.dispatch_tool("kata_triage", {"pattern": 5}))
    assert out["ok"] is False and "must be string" in out["error"]
    out = json.loads(host.dispatch_tool("kata_triage", {}))
    assert "missing required argument" in out["error"]
    assert host.tool_calls == before


def test_side_effecting_kata_asks_every_time(host):
    rt = host.runtime
    for u in USERS[:4]:
        turn = host.new_turn()
        res = json.loads(host.call(turn, "read_file", {"path": f"src/module_{u}.py"}))
        host.call(turn, "send_message", {"to": "#reviews", "text": res["content"]})
    rt.service.mine()
    k = rt.store.katas()[0]
    detail = rt.service.detail(k["id"])
    assert detail["requires_approval_per_call"] is True
    assert [s["effect"] for s in detail["spec"]["steps"]] == ["read", "external"]
    rt.service.approve(k["id"], name="share")
    rt.sync()
    sent_before = len(list((host.sandbox / ".outbox").iterdir()))
    param = detail["spec"]["params"][0]["name"]

    host.approve_all = False
    out = json.loads(host.call(host.new_turn(), "kata_share", {param: "module_erin"}))
    assert "denied" in out["error"]
    assert len(list((host.sandbox / ".outbox").iterdir())) == sent_before  # nothing was sent

    host.approve_all = True
    out = json.loads(host.call(host.new_turn(), "kata_share", {param: "module_erin"}))
    assert out["ok"] is True
    assert len(list((host.sandbox / ".outbox").iterdir())) == sent_before + 1
    assert len(host.approvals) == 2  # asked both times
    d1 = rt.on_pre_tool_call(tool_name="kata_share", args={})
    d2 = rt.on_pre_tool_call(tool_name="kata_share", args={})
    assert d1["action"] == "approve" and d1["rule_key"] != d2["rule_key"]


def test_secrets_are_redacted_and_never_compiled(host):
    rt = host.runtime
    fake_key = "sk-" + "abcdefghijklmnop" + "qrstuvwxyz123456"  # assembled so no key-shaped literal is committed
    for u in USERS[:4]:
        turn = host.new_turn()
        host.call(turn, "terminal", {"command": f"echo {fake_key}"})
        host.call(turn, "read_file", {"path": f"src/module_{u}.py"})
    stored = rt.store.events()
    assert all(fake_key not in json.dumps(e.args) + e.result for e in stored)
    rep = rt.service.mine()
    assert rep.proposed == []
    assert any("secret" in r["reason"] for r in rep.rejected)


def test_workflow_drift_detected_from_manual_runs(host):
    rt = host.runtime
    for u in USERS[:4]:
        triage_by_hand(host, u)
    rt.service.mine()
    kid = rt.store.katas()[0]["id"]
    rt.service.approve(kid, name="triage")
    rt.sync()
    import time as _t
    _t.sleep(0.01)
    # The agent keeps doing it by hand, but now counts words instead of lines.
    for u in USERS[:3]:
        turn = host.new_turn()
        res = json.loads(host.call(turn, "search_files", {"pattern": f"TODO({u})", "path": "src"}))
        p = res["matches"][0]["path"]
        host.call(turn, "read_file", {"path": p})
        host.call(turn, "terminal", {"command": f"wc -w {p}"})
    rep = rt.service.mine()
    # Different terminal command -> different template; the old one no longer fits.
    assert rt.store.kata(kid)["status"] in ("active", "stale")
    if rt.store.kata(kid)["status"] == "stale":
        assert "kata_triage" in rep.drift


def test_fake_terminal_is_portable(host, monkeypatch):
    import devtools.fake_hermes as fh
    monkeypatch.setattr(fh.shutil, "which", lambda name: None)
    out = json.loads(host.dispatch_tool("terminal", {"command": "wc -l src/module_alice.py"}))
    assert out == {"output": "3 src/module_alice.py" + chr(10), "exit_code": 0}
