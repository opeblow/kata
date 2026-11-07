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
        host.end_session(turn)
    rt.wait_idle()
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
        host.end_session(turn)
    rt.wait_idle()
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
    # The agent keeps doing it by hand, but now counts words instead of lines.
    for u in USERS[:3]:
        turn = host.new_turn()
        res = json.loads(host.call(turn, "search_files", {"pattern": f"TODO({u})", "path": "src"}))
        p = res["matches"][0]["path"]
        host.call(turn, "read_file", {"path": p})
        host.call(turn, "terminal", {"command": f"wc -w {p}"})
        host.end_session(turn)
    rt.wait_idle()  # background mining after each session end detects the change
    k = rt.store.kata(kid)
    assert k["status"] == "stale"
    assert "no longer fit" in k["status_reason"]
    rt.on_pre_llm_call()
    assert "kata_triage" not in host.tools  # unregistered until reviewed
    latest = rt.service.spec(kid)[0]
    assert '"wc -w ' in json.dumps(latest.to_json()["steps"][2]["args"]).replace("\\", "")  # revision prepared


def test_fake_terminal_is_portable(host, monkeypatch):
    import devtools.fake_hermes as fh
    monkeypatch.setattr(fh.shutil, "which", lambda name: None)
    out = json.loads(host.dispatch_tool("terminal", {"command": "wc -l src/module_alice.py"}))
    assert out == {"output": "3 src/module_alice.py" + chr(10), "exit_code": 0}


def test_work_in_progress_is_never_mined(host):
    """Reproduces the race CI found: mining while another conversation is mid-routine."""
    rt = host.runtime
    for u in USERS[:3]:
        triage_by_hand(host, u)
    for u in USERS[3:6]:  # three conversations stopped after two of the three steps, still open
        turn = host.new_turn()
        res = json.loads(host.call(turn, "search_files", {"pattern": f"TODO({u})", "path": "src"}))
        host.call(turn, "read_file", {"path": res["matches"][0]["path"]})
    rt.service.mine()
    names = [[s.tool for s in rt.service.spec(k["id"])[0].steps] for k in rt.store.katas()]
    assert names == [["search_files", "read_file", "terminal"]]


def test_fragments_of_a_rejected_routine_are_not_proposed(host):
    rt = host.runtime
    for u in USERS[:3]:
        triage_by_hand(host, u)
    rt.service.mine()
    host.commands["kata"]("reject 1")
    for u in USERS[3:6]:  # now the agent stops after reading, in finished sessions
        turn = host.new_turn()
        res = json.loads(host.call(turn, "search_files", {"pattern": f"TODO({u})", "path": "src"}))
        host.call(turn, "read_file", {"path": res["matches"][0]["path"]})
        host.end_session(turn)
    rt.wait_idle()
    rep = rt.service.mine()
    assert len(rt.store.katas()) == 1
    assert any(r["reason"] == "part of a routine you rejected" for r in rep.rejected)


def test_host_accepts_an_aliased_sandbox_path(sandbox):
    from devtools.fake_hermes import FakeHermes
    aliased = sandbox / "src" / ".."  # same folder, different spelling
    host = FakeHermes(sandbox=aliased)
    out = json.loads(host.dispatch_tool("search_files", {"pattern": "TODO(alice)", "path": "src"}))
    assert out["matches"][0]["path"] == "src/module_alice.py"


def test_approval_from_a_process_without_tools_pins_schemas_in_the_agent(host):
    """`hermes kata approve` and the dashboard run where Hermes's tools are not loaded."""
    from kata.models import TOOLS_NOT_LOADED
    from kata.service import KataService
    for u in USERS[:3]:
        triage_by_hand(host, u)
    rt = host.runtime
    cli = KataService(rt.store, rt.service.cfg, fingerprint=lambda tool: TOOLS_NOT_LOADED)
    kid = rt.store.katas()[0]["id"]
    spec, version, _ = rt.service.spec(kid)  # as if proposed where tools are not loaded: no schemas yet
    for step in spec.steps:
        step.schema_fingerprint = ""
    rt.store.update_version_spec(kid, version, spec.to_json())
    res = cli.approve(kid, name="triage")
    checks = {c["name"]: c for c in res["validation"]["checks"]}
    assert checks["tools_available"]["passed"] and "not loaded" in checks["tools_available"]["detail"]
    rt.sync()  # the agent process, with tools loaded
    assert "kata_triage" in host.tools
    spec, _ = rt.service.active_spec(kid)
    assert all(s.schema_fingerprint for s in spec.steps)
    assert any(a["action"] == "pinned" for a in rt.store.audit_log(kid))


def test_agent_is_told_about_approved_routines(host):
    section = host.prompt_sections["kata.routines"]
    assert section({}) == ""  # nothing approved yet: no prompt text at all
    for u in USERS[:3]:
        triage_by_hand(host, u)
    kid = host.runtime.store.katas()[0]["id"]
    assert section({}) == ""  # proposals are not advertised before approval
    host.commands["kata"](f"approve {kid} as triage")
    text = section({})
    assert "kata_triage(pattern): search_files -> read_file -> terminal" in text
    assert len(text) <= 1500
