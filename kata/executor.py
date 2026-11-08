"""Deterministic execution of a compiled kata.

No model calls happen here. Each step's arguments are rendered from the caller's
inputs and earlier results, dispatched through Hermes, and checked. The run stops at
the first failing step and returns a full trace so the agent can take over from
exactly where the routine broke. Only read-only steps are ever retried.
"""
from __future__ import annotations

import concurrent.futures
import contextvars
import json
import time
from collections.abc import Callable
from typing import Any

from .effects import classify
from .models import Effect, KataSpec, RunResult, StepTrace
from .templating import RenderError, parse_result, render_step

Dispatch = Callable[[str, dict], str]

_PREVIEW = 400


def validate_inputs(spec: KataSpec, inputs: Any) -> str | None:
    """Validate against the kata's input schema. Returns an error message or ``None``."""
    if not isinstance(inputs, dict):
        return "arguments must be an object"
    known = {p.name: p for p in spec.params}
    extra = sorted(set(inputs) - set(known))
    if extra:
        return f"unexpected argument(s): {', '.join(extra)}"
    for name, p in known.items():
        if name not in inputs:
            return f"missing required argument `{name}`"
        v = inputs[name]
        ok = {
            "string": isinstance(v, str),
            "integer": isinstance(v, int) and not isinstance(v, bool),
            "number": isinstance(v, (int, float)) and not isinstance(v, bool),
            "boolean": isinstance(v, bool),
        }.get(p.type, True)
        if not ok:
            return f"argument `{name}` must be {p.type}"
    return None


def step_failed(result: Any) -> str | None:
    """Hermes's own failure conventions: ``{"error": ...}`` payloads and ``[TOOL_ERROR]`` text."""
    if isinstance(result, str):
        if result.startswith("[TOOL_ERROR]"):
            return result[:_PREVIEW]
        parsed = parse_result(result)
    else:
        parsed = result
    if isinstance(parsed, dict):
        if parsed.get("error"):
            return str(parsed["error"])[:_PREVIEW]
        if parsed.get("success") is False:
            return str(parsed.get("message") or "step reported success=false")[:_PREVIEW]
        code = parsed.get("exit_code")
        if isinstance(code, int) and code != 0:
            return f"exit code {code}"
    return None


class Executor:
    def __init__(self, dispatch: Dispatch, *, step_timeout_s: float = 60.0, read_retries: int = 1,
                 sleep: Callable[[float], None] = time.sleep):
        self._dispatch = dispatch
        self.step_timeout_s = step_timeout_s
        self.read_retries = max(0, read_retries)
        self._sleep = sleep

    def _call(self, tool: str, args: dict) -> tuple[str, str | None]:
        """Dispatch with a timeout. Returns (raw_result, timeout_error)."""
        ctx = contextvars.copy_context()
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="kata-step")
        try:
            fut = pool.submit(ctx.run, self._dispatch, tool, args)
            try:
                raw = fut.result(timeout=self.step_timeout_s)
            except concurrent.futures.TimeoutError:
                return "", f"timed out after {self.step_timeout_s:g}s"
            if not isinstance(raw, str):
                raw = json.dumps(raw, default=str)
            return raw, None
        finally:
            # Never block on a hung tool; the thread finishes on its own.
            pool.shutdown(wait=False)

    def run(self, spec: KataSpec, version: int, inputs: dict) -> RunResult:
        started = time.monotonic()
        traces: list[StepTrace] = []
        results: list[Any] = []

        def done(ok: bool, *, failed: int | None = None, error: str | None = None) -> RunResult:
            for i in range(len(traces), len(spec.steps)):
                traces.append(StepTrace(i, spec.steps[i].tool, "skipped", 0, 0))
            return RunResult(ok=ok, kata=spec.name, version=version, steps=traces,
                             result=results[-1] if (ok and results) else None,
                             duration_ms=int((time.monotonic() - started) * 1000),
                             failed_step=failed, error=error)

        err = validate_inputs(spec, inputs)
        if err:
            return done(False, error=f"invalid arguments: {err}")

        for i, step in enumerate(spec.steps):
            try:
                args = render_step(step.tool, step.args, inputs, results)
            except RenderError as e:
                traces.append(StepTrace(i, step.tool, "error", 0, 0, error=f"could not build arguments: {e}"))
                return done(False, failed=i, error=f"step {i + 1} ({step.tool}): {e}")
            if not isinstance(args, dict):
                traces.append(StepTrace(i, step.tool, "error", 0, 0, error="arguments did not render to an object"))
                return done(False, failed=i, error=f"step {i + 1} ({step.tool}): bad arguments")
            # The approval covered this step's effect as mined. Re-check the real arguments, which
            # carry model input and earlier results, and refuse anything riskier than approved.
            effect, why = classify(step.tool, args)
            if effect.rank > step.effect.rank:
                msg = f"arguments are {effect.value}, but this step was approved as {step.effect.value} ({why})"
                traces.append(StepTrace(i, step.tool, "blocked", 0, 0, error=msg))
                return done(False, failed=i, error=f"step {i + 1} ({step.tool}): {msg}")

            attempts_allowed = 1 + (self.read_retries if step.effect is Effect.READ else 0)
            attempt, failure, status = 0, None, "ok"
            raw = ""
            t0 = time.monotonic()
            while attempt < attempts_allowed:
                attempt += 1
                raw, timeout_err = self._call(step.tool, args)
                if timeout_err:
                    failure, status = timeout_err, "timeout"
                    break  # a hung tool may still be running: never re-issue it
                failure = step_failed(raw)
                status = "error" if failure else "ok"
                if not failure:
                    break
                if attempt < attempts_allowed:
                    self._sleep(0.25 * attempt)
            dur = int((time.monotonic() - t0) * 1000)
            traces.append(StepTrace(i, step.tool, status, dur, attempt, error=failure,
                                    result_preview=raw[:_PREVIEW]))
            if failure:
                return done(False, failed=i, error=f"step {i + 1} ({step.tool}): {failure}")
            results.append(parse_result(raw))
        return done(True)
