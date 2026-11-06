"""Hermes wiring: hooks, tool registration and the compiled-tool handler.

Only documented ``PluginContext`` surfaces are used: ``register_tool`` (and the
``PluginRegistration.dispose`` handle it returns), ``register_hook``,
``register_command``, ``register_cli_command``, ``dispatch_tool`` and ``get_config``.
Tool schemas for drift detection come from ``tools.registry.registry.get_schema``,
which is on Hermes's public plugin API list.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

from .card import show_expr
from .executor import Executor
from .miner import MinerConfig
from .models import Effect, KataSpec, Status
from .redact import redact, redact_text
from .service import KataService
from .store import Store

log = logging.getLogger("kata")

MAX_RESULT_CHARS = 64_000
TOOLSET = "kata"


def schema_fingerprint(tool: str) -> str | None:
    """sha256 of a tool's current schema, or ``None`` when the tool is not registered."""
    try:
        from tools.registry import registry
    except Exception:
        return None
    schema = registry.get_schema(tool)
    if schema is None:
        return None
    return hashlib.sha256(json.dumps(schema, sort_keys=True, default=str).encode()).hexdigest()[:20]


class KataRuntime:
    def __init__(self, ctx: Any, store: Store, *, fingerprint: Callable[[str], str | None] = schema_fingerprint):
        self.ctx = ctx
        self.store = store
        self.fingerprint = fingerprint
        cfg = MinerConfig(
            min_support=int(self._cfg("min_support", 3)),
            min_confidence=float(self._cfg("min_confidence", 0.75)),
            max_steps=int(self._cfg("max_steps", 8)),
        )
        self.service = KataService(store, cfg, fingerprint, retention_days=int(self._cfg("retention_days", 30)))
        self._registered: dict[str, tuple[int, int, Any]] = {}  # tool name -> (kata_id, version, handle)
        self._generation = -1
        self._lock = threading.RLock()
        self._mining = threading.Lock()
        self._background: list[threading.Thread] = []

    def _cfg(self, key: str, default: Any) -> Any:
        try:
            v = self.ctx.get_config(key, default=default)
            return default if v is None else v
        except Exception:
            return default

    # -- observation hooks ---------------------------------------------------
    def on_post_tool_call(self, tool_name: str = "", args: Any = None, result: Any = "", duration_ms: int = 0,
                          status: str = "ok", session_id: str = "", conversation_id: str = "",
                          turn_id: str = "", **_: Any) -> None:
        if not self._cfg("record", True) or not tool_name:
            return
        try:
            text = result if isinstance(result, str) else json.dumps(result, default=str)
            self.store.add_event(
                conversation_id=conversation_id or session_id or "unknown", session_id=session_id or "",
                turn_id=turn_id or "", tool_name=tool_name,
                args=redact(args if isinstance(args, dict) else {"_": args}),
                result=redact_text(text[:MAX_RESULT_CHARS]), status=status or "ok",
                duration_ms=int(duration_ms or 0), ended_at=time.time())
        except Exception:
            log.exception("kata: failed to record tool call")

    def on_post_api_request(self, session_id: str = "", turn_id: str = "", started_at: float | None = None,
                            api_duration: float | None = None, usage: Any = None, **_: Any) -> None:
        if not self._cfg("record", True):
            return
        try:
            total = 0
            if isinstance(usage, dict):
                total = int(usage.get("total_tokens") or
                            (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0))
            self.store.add_api_call(session_id=session_id or "", turn_id=turn_id or "",
                                    started_at=float(started_at or time.time()),
                                    duration_s=float(api_duration or 0), total_tokens=total)
        except Exception:
            log.exception("kata: failed to record api call")

    def on_session_start(self, **_: Any) -> None:
        self.sync()

    def on_pre_llm_call(self, **_: Any) -> None:
        # Cheap: one SELECT. Picks up approvals made from the dashboard or another process.
        if self.store.generation() != self._generation:
            self.sync()
        return None

    def on_session_end(self, session_id: str = "", **_: Any) -> None:
        try:
            self.store.end_session(session_id)
        except Exception:
            log.exception("kata: failed to record session end")
        t = threading.Thread(target=self.mine_quietly, name="kata-miner", daemon=True)
        self._background.append(t)
        t.start()

    def wait_idle(self, timeout: float = 30.0) -> None:
        """Block until background mining started by session ends has finished."""
        for t in list(self._background):
            t.join(timeout)
        self._background = [t for t in self._background if t.is_alive()]

    def mine_quietly(self) -> None:
        if not self._mining.acquire(blocking=False):
            return
        try:
            self.service.mine()
            self.sync()
        except Exception:
            log.exception("kata: mining failed")
        finally:
            self._mining.release()

    def on_pre_tool_call(self, tool_name: str = "", args: Any = None, **_: Any) -> dict | None:
        entry = self._registered.get(tool_name)
        if not entry:
            return None
        kata_id, version, _ = entry
        found = self.service.active_spec(kata_id)
        if not found:
            return {"action": "block", "message": f"{tool_name} is no longer active. Do the steps by hand."}
        spec, _v = found
        if spec.max_effect.rank < Effect.EXTERNAL.rank:
            return None
        steps = "; ".join(f"{i + 1}. {s.tool}({show_expr(s.args)})" for i, s in enumerate(spec.steps))
        # Fresh rule key so a remembered "always allow" never silently repeats side effects.
        return {"action": "approve", "rule_key": f"kata:{tool_name}:{uuid.uuid4().hex}",
                "message": f"Kata `{tool_name}` v{version} will run {len(spec.steps)} steps with side effects "
                           f"using {json.dumps(args, default=str)[:300]}: {steps[:900]}"}

    # -- registration ----------------------------------------------------------
    def _drift_reason(self, spec: KataSpec) -> str | None:
        for s in spec.steps:
            fp = self.fingerprint(s.tool)
            if fp is None:
                return f"tool `{s.tool}` is no longer available"
            if s.schema_fingerprint and fp != s.schema_fingerprint:
                return f"the schema of `{s.tool}` changed since approval"
        return None

    def sync(self) -> None:
        """Make registered tools match the approved, healthy katas in the store."""
        with self._lock:
            self._generation = self.store.generation()
            wanted: dict[str, tuple[int, int, KataSpec]] = {}
            for k in self.store.katas(Status.ACTIVE.value):
                found = self.service.active_spec(k["id"])
                if not found:
                    continue
                spec, version = found
                reason = self._drift_reason(spec)
                if reason:
                    self.store.set_status(k["id"], Status.STALE, actor="kata", reason=reason)
                    continue
                wanted[spec.name] = (k["id"], version, spec)
            self._generation = self.store.generation()
            for name in list(self._registered):
                kid, ver, handle = self._registered[name]
                if name not in wanted or wanted[name][1] != ver:
                    self._dispose(handle)
                    del self._registered[name]
            for name, (kid, ver, spec) in wanted.items():
                if name in self._registered:
                    continue
                handle = self.ctx.register_tool(
                    name=name, toolset=TOOLSET, schema=spec.tool_schema(name),
                    handler=self._make_handler(kid, ver),
                    description=f"Kata v{ver}: {spec.description}")
                self._registered[name] = (kid, ver, handle)
                log.info("kata: registered %s v%s", name, ver)

    @staticmethod
    def _dispose(handle: Any) -> None:
        try:
            if handle is not None and hasattr(handle, "dispose"):
                handle.dispose()
        except Exception:
            log.exception("kata: failed to unregister tool")

    def registered(self) -> dict[str, int]:
        return {n: v for n, (_, v, _) in self._registered.items()}

    def _make_handler(self, kata_id: int, version: int) -> Callable[..., str]:
        def handler(args: dict, task_id: str = "", conversation_id: str = "", session_id: str = "", **_: Any) -> str:
            started = time.time()
            try:
                found = self.service.active_spec(kata_id)
                if not found or found[1] != version:
                    return json.dumps({"ok": False, "error": "this kata was disabled or changed; do the steps by hand"})
                spec, _ = found
                reason = self._drift_reason(spec)
                if reason:
                    self.store.set_status(kata_id, Status.STALE, actor="kata", reason=reason)
                    return json.dumps({"ok": False, "error": f"kata needs review: {reason}"})

                def dispatch(tool: str, a: dict) -> str:
                    kw = {"task_id": task_id} if task_id else {}
                    return self.ctx.dispatch_tool(tool, a, **kw)

                executor = Executor(dispatch, step_timeout_s=float(self._cfg("step_timeout_s", 60)),
                                    read_retries=int(self._cfg("read_retries", 1)))
                run = executor.run(spec, version, args or {}).to_json()
                self.service.record_run(kata_id, version, conversation_id or session_id or "", run, started)
                if not run["ok"]:
                    run["hint"] = ("The routine stopped at the failed step; nothing after it ran. "
                                   "Continue by hand from that step.")
                return json.dumps(run, default=str)
            except Exception as e:  # never raise into Hermes
                log.exception("kata: run failed")
                return json.dumps({"ok": False, "error": f"kata internal error: {type(e).__name__}: {e}"})
        handler.__name__ = f"kata_handler_{kata_id}_v{version}"
        return handler

    # -- read-only listing tool for the agent ----------------------------------
    def kata_list_handler(self, args: dict, **_: Any) -> str:
        try:
            items = []
            for k in self.store.katas():
                if k["status"] not in (Status.ACTIVE.value, Status.CANDIDATE.value):
                    continue
                spec, version, _ = self.service.spec(k["id"])
                items.append({"name": k["name"], "status": k["status"], "steps": [s.tool for s in spec.steps],
                              "params": [p.name for p in spec.params], "effect": spec.max_effect.value,
                              "support": spec.evidence.support})
            return json.dumps({"katas": items, "note": "Candidates need the user's approval (/kata review <id>)."})
        except Exception as e:
            return json.dumps({"error": str(e)})


KATA_LIST_SCHEMA = {
    "name": "kata_list",
    "description": ("List compiled routines (katas) the user approved, plus pending candidates. Active katas are "
                    "also exposed as their own kata_* tools; prefer them over repeating the same steps by hand."),
    "parameters": {"type": "object", "properties": {}, "required": []},
}
