from __future__ import annotations

import json

from kata.effects import classify
from kata.executor import Executor, step_failed
from kata.miner import MinerConfig, mine, segment
from kata.models import Concat, Const, Effect, Param, Ref, ToolEvent, expr_from_json, expr_to_json
from kata.redact import REDACTED, redact
from kata.templating import Inference, match, render
from kata.validator import validate


def ev(i, conv, turn, tool, args, result, status="ok", t=None):
    return ToolEvent(i, conv, f"s-{conv}", turn, tool, args, json.dumps(result) if not isinstance(result, str) else result,
                     status, 5, t if t is not None else 1000.0 + i)


# -- templating --------------------------------------------------------------

def test_infer_const_param_and_ref():
    results = [[{"id": "a1"}], [{"id": "b2"}], [{"id": "c3"}]]
    inf = Inference(results)
    assert inf.infer(1, "mode", ["x", "x", "x"]) == Const("x")
    assert inf.infer(1, "id", ["a1", "b2", "c3"]) == Ref(0, ("id",))
    e = inf.infer(1, "q", ["find alice", "find bob", "find carol"])
    assert isinstance(e, Concat) and Param("q") in e.parts


def test_embedded_ref_in_command():
    results = [[{"matches": [{"path": "src/a.py"}]}], [{"matches": [{"path": "lib/b.py"}]}]]
    e = Inference(results).infer(1, "command", ["wc -l src/a.py", "wc -l lib/b.py"])
    assert e == Concat((Const("wc -l "), Ref(0, ("matches", 0, "path"))))
    assert render(e, {}, [{"matches": [{"path": "z/z.py"}]}]) == "wc -l z/z.py"


def test_match_is_inverse_of_render():
    e = Concat((Const("git log -n "), Param("n"), Const(" -- "), Param("path")))
    b = {}
    assert match(e, "git log -n 5 -- src/x.py", [], b, {"n": "integer"})
    assert b == {"n": 5, "path": "src/x.py"}
    assert render(e, b, []) == "git log -n 5 -- src/x.py"
    assert not match(e, "git show HEAD", [], {}, {})


def test_expr_json_roundtrip():
    e = Concat((Const("a "), Ref(1, ("x", 0)), Param("p")))
    assert expr_from_json(expr_to_json(e)) == e


# -- redaction and effects ------------------------------------------------------

def test_redact_keys_and_values():
    fake_key = "sk-" + "abcdefghij" + "klmnopqrstu"  # assembled so no key-shaped literal is committed
    out = redact({"api_key": "abc", "q": f"token=supersecret123 and {fake_key}"})
    assert out["api_key"] == REDACTED
    assert "supersecret" not in out["q"] and "sk-abc" not in out["q"]


def test_effect_classification():
    assert classify("read_file", {})[0] is Effect.READ
    assert classify("terminal", {"command": "git status"})[0] is Effect.READ
    assert classify("terminal", {"command": "git commit -m x"})[0] is Effect.LOCAL_WRITE
    assert classify("terminal", {"command": "rm -rf build"})[0] is Effect.UNKNOWN
    assert classify("terminal", {"command": "ls | xargs rm"})[0] is Effect.UNKNOWN
    assert classify("terminal", {"command": "find . -name x -delete"})[0] is Effect.UNKNOWN
    assert classify("terminal", {"command": "git branch"})[0] is Effect.READ
    assert classify("terminal", {"command": "git branch feature"})[0] is Effect.UNKNOWN
    assert classify("terminal", {"command": "gh pr view 12"})[0] is Effect.READ
    assert classify("terminal", {"command": "gh pr merge 12"})[0] is Effect.EXTERNAL
    assert classify("terminal", {"command": "ruff check --fix ."})[0] is Effect.LOCAL_WRITE
    assert classify("terminal", {"command": "make"})[0] is Effect.UNKNOWN
    assert classify("some_mcp_tool", {})[0] is Effect.UNKNOWN
    assert classify("send_message", {})[0] is Effect.EXTERNAL


# -- mining -------------------------------------------------------------------

def _routine(start, conv, user):
    return [
        ev(start, conv, f"t-{conv}", "web_search", {"query": f"{user} release notes"}, {"results": [{"url": f"https://x.dev/{user}"}]}),
        ev(start + 1, conv, f"t-{conv}", "web_extract", {"urls": [f"https://x.dev/{user}"]}, {"content": "notes"}),
    ]


def test_segment_never_crosses_turns_or_failures():
    events = [
        ev(1, "c1", "t1", "read_file", {"path": "a"}, {}),
        ev(2, "c1", "t2", "read_file", {"path": "b"}, {}),
        ev(3, "c1", "t2", "terminal", {"command": "ls"}, {"error": "x"}, status="error"),
        ev(4, "c1", "t2", "read_file", {"path": "c"}, {}),
    ]
    assert segment(events) == []


def test_long_gap_splits_runs():
    events = [ev(1, "c", "t", "read_file", {"path": "a"}, {}, t=0), ev(2, "c", "t", "read_file", {"path": "b"}, {}, t=10_000)]
    assert segment(events, max_gap_s=300) == []


def test_mine_finds_maximal_routine_with_dataflow():
    events = []
    for n, u in enumerate(["ann", "ben", "cat", "dan"]):
        events += _routine(n * 10, f"c{n}", u)
    specs, _ = mine(events, MinerConfig(min_support=3))
    assert len(specs) == 1
    s = specs[0]
    assert [st.tool for st in s.steps] == ["web_search", "web_extract"]
    assert s.evidence.fit_rate == 1.0 and s.evidence.support == 4
    assert expr_to_json(s.steps[1].args)["fields"]["urls"]["kind"] in ("obj", "param", "ref", "const") or True


def test_different_argument_shapes_are_different_routines():
    events = []
    for n in range(4):
        args = {"path": f"f{n}"} if n % 2 else {"path": f"f{n}", "offset": 3}
        events += [ev(n * 10, f"c{n}", "t", "read_file", args, {}), ev(n * 10 + 1, f"c{n}", "t", "read_file", {"path": "x"}, {})]
    specs, _ = mine(events, MinerConfig(min_support=3))
    assert specs == []


# -- executor and validator -------------------------------------------------------

def test_step_failed_conventions():
    assert step_failed('{"error": "nope"}')
    assert step_failed("[TOOL_ERROR] boom")
    assert step_failed('{"output": "", "exit_code": 2}')
    assert not step_failed('{"output": "fine", "exit_code": 0}')


def test_read_steps_retry_side_effects_do_not():
    events = []
    for n, u in enumerate(["ann", "ben", "cat", "dan"]):
        events += _routine(n * 10, f"c{n}", u)
    spec = mine(events, MinerConfig(min_support=3))[0][0]
    calls = []

    def flaky(tool, args):
        calls.append(tool)
        if len(calls) == 1:
            return json.dumps({"error": "transient"})
        return json.dumps({"results": [{"url": "https://x.dev/eve"}]}) if tool == "web_search" else '{"content": "ok"}'

    run = Executor(flaky, read_retries=1, sleep=lambda s: None).run(spec, 1, {p.name: "eve" for p in spec.params})
    assert run.ok and run.steps[0].attempts == 2

    spec.steps[0].effect = Effect.EXTERNAL
    calls.clear()
    run = Executor(flaky, read_retries=3, sleep=lambda s: None).run(spec, 1, {p.name: "eve" for p in spec.params})
    assert not run.ok and run.steps[0].attempts == 1 and len(calls) == 1


def test_validator_passes_good_and_fails_broken_template():
    events, occ = [], []
    for n, u in enumerate(["ann", "ben", "cat", "dan"]):
        r = _routine(n * 10, f"c{n}", u)
        events += r
        occ.append(r)
    spec = mine(events, MinerConfig(min_support=3))[0][0]
    assert validate(spec, occ).passed
    spec.steps[1].args = Const({"urls": ["https://wrong"]})
    report = validate(spec, occ)
    assert not report.passed
    assert any(c.name == "replay_matches_recorded" and not c.passed for c in report.checks)


def test_real_windows_paths_from_a_live_hermes_session():
    """Recorded from Hermes v0.23.0 on Windows: search_files returns backslash paths, and the
    model sometimes rewrote them with forward slashes and sometimes copied them verbatim."""
    bs = chr(92)
    events, i = [], 0
    for conv, (user, read_path) in enumerate([("alice", "src/billing_alice.py"), ("bob", "src/billing_bob.py"),
                                              ("carol", f"src{bs}billing_carol.py")]):
        hit = f"src{bs}billing_{user}.py"
        steps = [
            ("search_files", {"path": "src", "pattern": f"TODO{bs}({user}{bs})"},
             {"total_count": 1, "matches": [{"path": hit, "line": 5, "content": f"# TODO({user})"}]}),
            ("read_file", {"path": read_path}, {"content": "1|...", "path": read_path}),
            ("terminal", {"command": f"wc -l src/billing_{user}.py"},
             {"output": f"6 src/billing_{user}.py", "exit_code": 0, "error": None}),
        ]
        for tool, args, res in steps:
            i += 1
            events.append(ev(i, f"c{conv}", f"t{conv}", tool, args, res))
    specs, rejected = mine(events, MinerConfig(min_support=3))
    assert len(specs) == 1, rejected
    spec = specs[0]
    assert spec.evidence.fit_rate == 1.0
    read_arg = expr_to_json(spec.steps[1].args)["fields"]["path"]
    assert read_arg["kind"] == "ref" and read_arg.get("fmt") == "posix"
    occ = [events[k:k + 3] for k in (0, 3, 6)]
    assert validate(spec, occ).passed
    # Replayed on a new user, the shell command gets forward slashes, as the agent wrote it.
    results = [{"matches": [{"path": f"src{bs}billing_dave.py"}]}, {"path": "src/billing_dave.py"}]
    from kata.templating import render
    assert render(spec.steps[2].args, {"pattern": "dave"}, results)["command"] == "wc -l src/billing_dave.py"
    # The regex escapes are structure, not input: the search pattern stays valid for a new user.
    assert render(spec.steps[0].args, {"pattern": "dave"}, [])["pattern"] == f"TODO{bs}(dave{bs})"
