"""A minimal stand-in for the Hermes host, for tests and the offline demo.

It reproduces the contracts Kata relies on, as implemented in hermes-agent:

* ``register_tool`` returns a handle with ``dispose()`` (``PluginRegistration``)
* ``dispatch_tool`` calls the registered handler directly and does not fire hooks
  (``tools.registry.dispatch``)
* the agent loop fires ``pre_tool_call`` (``block`` / ``approve`` directives),
  then the tool, then ``post_tool_call`` with ``tool_name, args, result, status,
  duration_ms, session_id, conversation_id, turn_id`` (``model_tools``)
* each model request fires ``post_api_request`` with ``turn_id, started_at,
  api_duration, usage`` (``agent.turn_response_intake``)

The tools below are small but real: they read and write files in a sandbox directory
and run real ``git`` commands when git is available.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Handle:
    host: FakeHermes
    name: str

    def dispose(self) -> None:
        self.host.tools.pop(self.name, None)


@dataclass
class FakeHermes:
    sandbox: Path
    config: dict = field(default_factory=dict)
    tools: dict[str, dict] = field(default_factory=dict)
    hooks: dict[str, list[Callable]] = field(default_factory=dict)
    commands: dict[str, Callable] = field(default_factory=dict)
    approvals: list[str] = field(default_factory=list)
    approve_all: bool = True
    tool_calls: int = 0
    model_calls: int = 0
    model_tokens: int = 0
    model_latency_s: float = 0.0

    def __post_init__(self) -> None:
        for name, fn, schema in builtin_tools(self.sandbox):
            self.tools[name] = {"handler": fn, "schema": schema, "toolset": "builtin"}

    # -- PluginContext surface ---------------------------------------------
    def register_tool(self, name: str, toolset: str, schema: dict, handler: Callable, **_: Any) -> Handle:
        self.tools[name] = {"handler": handler, "schema": schema, "toolset": toolset}
        return Handle(self, name)

    def register_hook(self, name: str, cb: Callable, **_: Any) -> None:
        self.hooks.setdefault(name, []).append(cb)

    def register_command(self, name: str, handler: Callable, **_: Any) -> None:
        self.commands[name] = handler

    def register_cli_command(self, *a: Any, **k: Any) -> None:
        return None

    def get_config(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, default)

    def dispatch_tool(self, name: str, args: dict, **kwargs: Any) -> str:
        entry = self.tools.get(name)
        if not entry:
            return json.dumps({"error": f"Unknown tool: {name}"})
        try:
            return entry["handler"](args, **kwargs)
        except Exception as e:
            return json.dumps({"error": f"Tool execution failed: {e}"})

    def schema_fingerprint(self, tool: str) -> str | None:
        import hashlib
        entry = self.tools.get(tool)
        if not entry:
            return None
        return hashlib.sha256(json.dumps(entry["schema"], sort_keys=True).encode()).hexdigest()[:20]

    # -- agent-loop simulation ---------------------------------------------
    def fire(self, hook: str, **kw: Any) -> list[Any]:
        return [cb(**kw) for cb in self.hooks.get(hook, [])]

    def model_request(self, turn: Turn, tokens: int, latency_s: float) -> None:
        self.model_calls += 1
        self.model_tokens += tokens
        self.model_latency_s += latency_s
        self.fire("post_api_request", session_id=turn.session_id, turn_id=turn.turn_id,
                  started_at=time.time(), api_duration=latency_s,
                  usage={"total_tokens": tokens, "input_tokens": tokens - 40, "output_tokens": 40})

    def call(self, turn: Turn, name: str, args: dict) -> str:
        """One tool call exactly as the Hermes loop runs it."""
        for d in self.fire("pre_tool_call", tool_name=name, args=args, task_id=turn.turn_id,
                           session_id=turn.session_id, turn_id=turn.turn_id):
            if isinstance(d, dict) and d.get("action") == "block":
                return json.dumps({"error": d.get("message")})
            if isinstance(d, dict) and d.get("action") == "approve":
                self.approvals.append(d.get("message", ""))
                if not self.approve_all:
                    return json.dumps({"error": "denied by user"})
        self.tool_calls += 1
        t0 = time.monotonic()
        result = self.dispatch_tool(name, args, task_id=turn.turn_id, conversation_id=turn.conversation_id,
                                    session_id=turn.session_id)
        dur = int((time.monotonic() - t0) * 1000)
        status = "error" if '"error"' in result[:40] else "ok"
        self.fire("post_tool_call", tool_name=name, args=args, result=result, task_id=turn.turn_id,
                  session_id=turn.session_id, conversation_id=turn.conversation_id, turn_id=turn.turn_id,
                  tool_call_id=uuid.uuid4().hex, api_request_id="", duration_ms=dur, status=status,
                  error_type=None, error_message=None, middleware_trace=[])
        return result

    def new_turn(self, conversation_id: str | None = None) -> Turn:
        cid = conversation_id or f"conv-{uuid.uuid4().hex[:8]}"
        t = Turn(conversation_id=cid, session_id=f"sess-{cid}", turn_id=f"turn-{uuid.uuid4().hex[:8]}")
        return t

    def start_session(self, turn: Turn) -> None:
        self.fire("on_session_start", session_id=turn.session_id, model="fake", platform="cli")

    def end_session(self, turn: Turn) -> None:
        for cb in self.hooks.get("on_session_end", []):
            cb(session_id=turn.session_id, completed=True, interrupted=False, model="fake", platform="cli")


@dataclass
class Turn:
    conversation_id: str
    session_id: str
    turn_id: str


# ---------------------------------------------------------------------------
# Sandbox tools (subset of Hermes built-ins, same names and result shapes)
# ---------------------------------------------------------------------------

def _safe(sandbox: Path, p: str) -> Path:
    full = (sandbox / p).resolve()
    if not str(full).startswith(str(sandbox.resolve())):
        raise ValueError("path escapes sandbox")
    return full


def builtin_tools(sandbox: Path):
    def search_files(args: dict, **_: Any) -> str:
        pattern, root = args.get("pattern", ""), args.get("path", ".")
        matches = []
        for f in sorted(_safe(sandbox, root).rglob("*")):
            if f.is_file():
                for n, line in enumerate(f.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                    if pattern in line:
                        matches.append({"path": f.relative_to(sandbox).as_posix(), "line": n, "text": line.strip()})
        return json.dumps({"total": len(matches), "matches": matches[:50]})

    def read_file(args: dict, **_: Any) -> str:
        f = _safe(sandbox, args["path"])
        if not f.exists():
            return json.dumps({"error": f"File not found: {args['path']}"})
        text = f.read_text(encoding="utf-8")
        return json.dumps({"path": args["path"], "content": text, "lines": text.count("\n") + 1})

    def write_file(args: dict, **_: Any) -> str:
        f = _safe(sandbox, args["path"])
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(args["content"], encoding="utf-8")
        return json.dumps({"path": args["path"], "bytes": len(args["content"].encode())})

    def terminal(args: dict, **_: Any) -> str:
        cmd = args.get("command", "")
        parts = cmd.split()
        if parts and parts[0] == "wc" and shutil.which("wc") is None:
            # Portable stand-in so the suite runs on hosts without coreutils (Windows runners).
            target = _safe(sandbox, parts[-1])
            text = target.read_text(encoding="utf-8")
            n = text.count(chr(10)) if "-l" in parts else len(text.split())
            return json.dumps({"output": f"{n} {parts[-1]}" + chr(10), "exit_code": 0})
        try:
            p = subprocess.run(cmd, shell=True, cwd=sandbox, capture_output=True, text=True, timeout=30,
                               env={**os.environ, "GIT_PAGER": "cat"})
        except subprocess.TimeoutExpired:
            return json.dumps({"error": "command timed out"})
        return json.dumps({"output": (p.stdout + p.stderr)[-4000:], "exit_code": p.returncode})

    outbox = sandbox / ".outbox"

    def send_message(args: dict, **_: Any) -> str:
        """Records outgoing messages to a local outbox instead of sending them anywhere."""
        outbox.mkdir(exist_ok=True)
        n = len(list(outbox.iterdir())) + 1
        (outbox / f"{n:04d}.json").write_text(json.dumps(args), encoding="utf-8")
        return json.dumps({"delivered": True, "id": n})

    obj = lambda props, req: {"type": "object", "properties": props, "required": req}  # noqa: E731
    s = {"type": "string"}
    return [
        ("search_files", search_files, {"name": "search_files", "parameters": obj({"pattern": s, "path": s}, ["pattern"])}),
        ("read_file", read_file, {"name": "read_file", "parameters": obj({"path": s}, ["path"])}),
        ("write_file", write_file, {"name": "write_file", "parameters": obj({"path": s, "content": s}, ["path", "content"])}),
        ("terminal", terminal, {"name": "terminal", "parameters": obj({"command": s}, ["command"])}),
        ("send_message", send_message, {"name": "send_message", "parameters": obj({"to": s, "text": s}, ["to", "text"])}),
    ]
