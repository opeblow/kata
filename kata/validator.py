"""Pre-activation validation.

Every check runs offline against recorded history: nothing here calls a real tool.
The executor is exercised with a stub dispatcher that replays recorded results, so we
prove the compiled path issues exactly the calls the agent made by hand, in the same
order, with the same arguments, and that it stops cleanly on errors and timeouts.
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable

from .effects import compilable
from .executor import Executor, validate_inputs
from .miner import fits
from .models import CheckResult, Effect, KataSpec, ToolEvent, ValidationReport
from .redact import REDACTED, looks_secret
from .templating import RenderError, normalized, parse_result, render


class _Replay:
    """Stub dispatcher: returns recorded results and records what was asked for."""

    def __init__(self, recorded: list[ToolEvent], fail_at: int | None = None, hang_at: int | None = None,
                 hang_s: float = 0.0):
        self.recorded = recorded
        self.calls: list[tuple[str, dict]] = []
        self.fail_at = fail_at
        self.hang_at = hang_at
        self.hang_s = hang_s

    def __call__(self, tool: str, args: dict) -> str:
        i = len(self.calls)
        self.calls.append((tool, args))
        if i == self.hang_at:
            time.sleep(self.hang_s)
        if i == self.fail_at:
            return json.dumps({"error": "injected failure"})
        if i < len(self.recorded):
            return self.recorded[i].result
        return json.dumps({"error": "unexpected extra call"})


def _conforms(value, schema: dict) -> bool:
    t = schema.get("type")
    if t == "object":
        if not isinstance(value, dict):
            return False
        return all(k in value for k in schema.get("required", []))
    if t == "array":
        return isinstance(value, list)
    if t == "string":
        return True  # non-JSON results are passed through as text
    return True


def validate(spec: KataSpec, examples: list[list[ToolEvent]], *,
             fingerprint: Callable[[str], str | None] | None = None,
             min_fit: float = 0.9) -> ValidationReport:
    checks: list[CheckResult] = []
    types = {p.name: p.type for p in spec.params}
    exprs = [s.args for s in spec.steps]

    # 1. Compilable tools only.
    bad = [s.tool for s in spec.steps if not compilable(s.tool)]
    checks.append(CheckResult("compilable_tools", not bad,
                              "all steps use compilable tools" if not bad else f"not compilable: {', '.join(bad)}"))

    # 2. Examples available.
    usable = [e for e in examples if len(e) == len(spec.steps)]
    checks.append(CheckResult("recorded_examples", len(usable) >= 2,
                              f"{len(usable)} recorded example(s) available"))

    # 3. Symbolic replay: inputs recovered from each example re-render its exact arguments.
    inputs_per_example: list[dict] = []
    mismatches = []
    for n, ex in enumerate(usable):
        bindings = fits(exprs, types, ex)
        if bindings is None:
            mismatches.append(f"example {n + 1} does not fit the template")
            continue
        results = []
        for i, (expr, ev) in enumerate(zip(exprs, ex, strict=False)):
            try:
                rendered = render(expr, bindings, results)
            except RenderError as e:
                mismatches.append(f"example {n + 1} step {i + 1}: {e}")
                break
            if json.dumps(normalized(rendered), sort_keys=True) != json.dumps(normalized(ev.args), sort_keys=True):
                mismatches.append(f"example {n + 1} step {i + 1}: rendered arguments differ from recorded")
                break
            results.append(parse_result(ev.result))
        else:
            inputs_per_example.append(bindings)
    checks.append(CheckResult("replay_matches_recorded", not mismatches and bool(inputs_per_example),
                              f"{len(inputs_per_example)}/{len(usable)} examples reproduced exactly"
                              + (f"; {mismatches[0]}" if mismatches else "")))

    # 4. Generalisation measured by the miner (leave-one-out).
    fit = spec.evidence.fit_rate
    checks.append(CheckResult("holdout_fit", fit >= min_fit,
                              f"held-out fit {fit:.0%} (minimum {min_fit:.0%})"))

    # 5. Input schema accepts every recorded input and rejects a broken call.
    schema_errors = [validate_inputs(spec, b) for b in inputs_per_example]
    schema_errors = [e for e in schema_errors if e]
    rejects_bad = True
    if spec.params:
        broken = dict(inputs_per_example[0]) if inputs_per_example else {}
        broken.pop(spec.params[0].name, None)
        rejects_bad = validate_inputs(spec, broken) is not None
    checks.append(CheckResult("input_schema", not schema_errors and rejects_bad,
                              "schema accepts recorded inputs and rejects missing ones"
                              if not schema_errors and rejects_bad else
                              (schema_errors[0] if schema_errors else "schema accepted an incomplete call")))

    # 6. Executor replay: same calls, same order, same final result.
    exec_ok, exec_detail = bool(inputs_per_example), "no reproducible example"
    for ex, inputs in zip(usable, inputs_per_example, strict=False):
        stub = _Replay(ex)
        run = Executor(stub, step_timeout_s=5, read_retries=0).run(spec, 0, inputs)
        expected = [(e.tool_name, e.args) for e in ex]
        same_calls = [(t, json.dumps(normalized(a), sort_keys=True)) for t, a in stub.calls] == \
                     [(t, json.dumps(normalized(a), sort_keys=True)) for t, a in expected]
        if not (run.ok and same_calls):
            exec_ok, exec_detail = False, f"executor diverged: {run.error or 'different call sequence'}"
            break
        exec_detail = f"executor reproduced {len(inputs_per_example)} recorded run(s) call-for-call"
    checks.append(CheckResult("executor_replay", exec_ok, exec_detail))

    # 7. Failure handling: an error mid-routine stops the run and nothing after it executes.
    fail_ok, fail_detail = False, "no example to test with"
    if inputs_per_example:
        at = min(1, len(spec.steps) - 1)
        stub = _Replay(usable[0], fail_at=at)
        run = Executor(stub, step_timeout_s=5, read_retries=0).run(spec, 0, inputs_per_example[0])
        fail_ok = (not run.ok and run.failed_step == at and len(stub.calls) == at + 1
                   and all(t.status == "skipped" for t in run.steps[at + 1:]))
        fail_detail = (f"injected failure at step {at + 1} stopped the run; later steps skipped"
                       if fail_ok else "run continued past an injected failure")
    checks.append(CheckResult("failure_handling", fail_ok, fail_detail))

    # 8. Timeout handling.
    to_ok, to_detail = False, "no example to test with"
    if inputs_per_example:
        stub = _Replay(usable[0], hang_at=0, hang_s=0.3)
        run = Executor(stub, step_timeout_s=0.05, read_retries=3).run(spec, 0, inputs_per_example[0])
        to_ok = (not run.ok and run.steps[0].status == "timeout" and run.steps[0].attempts == 1)
        to_detail = "a hung step times out, is not re-issued, and stops the run" if to_ok else "timeout not enforced"
    checks.append(CheckResult("timeout_handling", to_ok, to_detail))

    # 9. Output schema holds for every recorded final result.
    outs = [parse_result(ex[-1].result) for ex in usable]
    out_ok = all(_conforms(o, spec.output_schema) for o in outs)
    checks.append(CheckResult("output_schema", out_ok,
                              "recorded results match the output schema" if out_ok else "a recorded result breaks the schema"))

    # 10. No secrets anywhere in the spec.
    blob = json.dumps(spec.to_json()["steps"]) + json.dumps([p.examples for p in spec.params])
    secret = REDACTED in blob or looks_secret(blob)
    checks.append(CheckResult("no_secrets", not secret,
                              "no credentials embedded" if not secret else "spec contains a secret or redacted value"))

    # 11. Tools exist today and match what was recorded.
    if fingerprint is not None:
        missing, changed = [], []
        for s in spec.steps:
            fp = fingerprint(s.tool)
            if fp is None:
                missing.append(s.tool)
            elif s.schema_fingerprint and fp != s.schema_fingerprint:
                changed.append(s.tool)
        ok = not missing and not changed
        detail = "all tools available, schemas unchanged" if ok else \
            "; ".join(x for x in (f"missing: {', '.join(missing)}" if missing else "",
                                  f"schema changed: {', '.join(changed)}" if changed else "") if x)
        checks.append(CheckResult("tools_available", ok, detail))

    # 12. Side effects disclosed. Passes, but states the consequence.
    risky = [s for s in spec.steps if s.effect.rank >= Effect.EXTERNAL.rank]
    checks.append(CheckResult("side_effects", True,
                              "read/local only; runs without extra prompts" if not risky else
                              f"{len(risky)} step(s) with external or unknown effects; every call asks for approval"))
    return ValidationReport(checks)
