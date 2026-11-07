"""Typed data model shared by every Kata layer.

A *Kata* is a compiled routine: an ordered list of steps, each a tool name plus an
argument template. Templates are trees of ``Expr`` nodes that resolve against the
caller's inputs and the results of earlier steps. Everything here is plain data and
round-trips through JSON so specs can be stored, diffed, versioned and audited.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Union

SPEC_VERSION = 1

# Fingerprint value meaning "this process has not loaded Hermes's tools", as in the
# `hermes kata` CLI or the dashboard backend. Distinct from None, which means "tool missing".
TOOLS_NOT_LOADED = "~not-loaded"


class Effect(StrEnum):
    """How much a step can change the world. Ordered from safest to riskiest."""

    READ = "read"            # observes only; safe to retry
    LOCAL_WRITE = "write"    # changes local state (files, todo, memory)
    EXTERNAL = "external"    # reaches outside the machine or cannot be undone
    UNKNOWN = "unknown"      # tool Kata cannot classify; treated as EXTERNAL

    @property
    def rank(self) -> int:
        return {"read": 0, "write": 1, "external": 2, "unknown": 3}[self.value]

    @property
    def retryable(self) -> bool:
        return self is Effect.READ


class Status(StrEnum):
    CANDIDATE = "candidate"   # proposed, waiting for a human decision
    ACTIVE = "active"         # approved and registered as a tool
    REJECTED = "rejected"     # a human declined it; never re-proposed for the same signature
    DISABLED = "disabled"     # a human turned it off
    STALE = "stale"           # an underlying tool changed; needs review before it can run again
    SUSPENDED = "suspended"   # failed repeatedly at runtime; needs review


# ---------------------------------------------------------------------------
# Expressions
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Const:
    value: Any
    kind: str = "const"


@dataclass(frozen=True)
class Param:
    name: str
    kind: str = "param"


@dataclass(frozen=True)
class Ref:
    """A value read from an earlier step's parsed JSON result."""

    step: int
    path: tuple[str | int, ...]
    fmt: str = ""          # "posix": render path separators as "/" (how the agent wrote them)
    kind: str = "ref"


@dataclass(frozen=True)
class Concat:
    """A string assembled from literal and dynamic parts, e.g. ``"git log -n 5 {path}"``."""

    parts: tuple[Expr, ...]
    kind: str = "concat"


@dataclass(frozen=True)
class Obj:
    fields: tuple[tuple[str, Expr], ...]
    kind: str = "obj"


@dataclass(frozen=True)
class Arr:
    items: tuple[Expr, ...]
    kind: str = "arr"


Expr = Union[Const, Param, Ref, Concat, Obj, Arr]


def expr_to_json(e: Expr) -> dict:
    if isinstance(e, Const):
        return {"kind": "const", "value": e.value}
    if isinstance(e, Param):
        return {"kind": "param", "name": e.name}
    if isinstance(e, Ref):
        d = {"kind": "ref", "step": e.step, "path": list(e.path)}
        if e.fmt:
            d["fmt"] = e.fmt
        return d
    if isinstance(e, Concat):
        return {"kind": "concat", "parts": [expr_to_json(p) for p in e.parts]}
    if isinstance(e, Obj):
        return {"kind": "obj", "fields": {k: expr_to_json(v) for k, v in e.fields}}
    if isinstance(e, Arr):
        return {"kind": "arr", "items": [expr_to_json(v) for v in e.items]}
    raise TypeError(f"not an expression: {e!r}")


def expr_from_json(d: dict) -> Expr:
    kind = d.get("kind")
    if kind == "const":
        return Const(d.get("value"))
    if kind == "param":
        return Param(str(d["name"]))
    if kind == "ref":
        return Ref(int(d["step"]), tuple(d.get("path") or ()), str(d.get("fmt") or ""))
    if kind == "concat":
        return Concat(tuple(expr_from_json(p) for p in d["parts"]))
    if kind == "obj":
        return Obj(tuple((k, expr_from_json(v)) for k, v in d["fields"].items()))
    if kind == "arr":
        return Arr(tuple(expr_from_json(v) for v in d["items"]))
    raise ValueError(f"unknown expression kind: {kind!r}")


def expr_params(e: Expr) -> set[str]:
    if isinstance(e, Param):
        return {e.name}
    if isinstance(e, Concat):
        return set().union(*(expr_params(p) for p in e.parts)) if e.parts else set()
    if isinstance(e, Obj):
        return set().union(*(expr_params(v) for _, v in e.fields)) if e.fields else set()
    if isinstance(e, Arr):
        return set().union(*(expr_params(v) for v in e.items)) if e.items else set()
    return set()


def expr_refs(e: Expr) -> list[Ref]:
    if isinstance(e, Ref):
        return [e]
    if isinstance(e, Concat):
        return [r for p in e.parts for r in expr_refs(p)]
    if isinstance(e, Obj):
        return [r for _, v in e.fields for r in expr_refs(v)]
    if isinstance(e, Arr):
        return [r for v in e.items for r in expr_refs(v)]
    return []


# ---------------------------------------------------------------------------
# Observed history
# ---------------------------------------------------------------------------

@dataclass
class ToolEvent:
    """One tool call as Hermes reported it through ``post_tool_call``."""

    id: int
    conversation_id: str
    session_id: str
    turn_id: str
    tool_name: str
    args: dict
    result: str
    status: str
    duration_ms: int
    ended_at: float
    seq: int = 0  # position in the shared order of tool calls and model requests

    @property
    def started_at(self) -> float:
        return self.ended_at - self.duration_ms / 1000.0

    @property
    def ok(self) -> bool:
        return self.status == "ok"


# ---------------------------------------------------------------------------
# Compiled spec
# ---------------------------------------------------------------------------

@dataclass
class ParamSpec:
    name: str
    type: str                    # JSON Schema primitive: string | integer | number | boolean
    description: str
    examples: list[Any] = field(default_factory=list)


def _param_schema(p: ParamSpec) -> dict:
    """JSON Schema for one input. Recorded values show the agent exactly what to pass, so it
    sends just the variable part (e.g. "alice") rather than a whole search pattern."""
    out: dict[str, Any] = {"type": p.type, "description": p.description}
    examples = []
    for raw in p.examples[:3]:  # stored JSON-encoded by the miner
        try:
            value = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            continue
        if isinstance(value, (str, int, float, bool)):
            examples.append(value)
    if examples:
        shown = ", ".join(json.dumps(e) for e in examples)
        out["description"] = f"{p.description} Pass only the value itself, as in past runs: {shown}."
        out["examples"] = examples
    return out


@dataclass
class StepSpec:
    tool: str
    args: Expr
    effect: Effect
    effect_reason: str
    schema_fingerprint: str = ""   # sha256 of the tool schema at compile time; drift detection


@dataclass
class Evidence:
    support: int                       # distinct conversations the routine was observed in
    occurrences: int                   # total matching occurrences
    fit_rate: float                    # share of occurrences the template reproduces exactly
    confidence: float
    example_event_ids: list[list[int]]  # event ids per occurrence (bounded)
    first_seen: float
    last_seen: float
    baseline: dict = field(default_factory=dict)  # measured cost of doing it by hand


@dataclass
class KataSpec:
    signature: str
    name: str
    description: str
    params: list[ParamSpec]
    steps: list[StepSpec]
    output_schema: dict
    evidence: Evidence
    spec_version: int = SPEC_VERSION

    @property
    def max_effect(self) -> Effect:
        return max((s.effect for s in self.steps), key=lambda e: e.rank, default=Effect.READ)

    @property
    def tools(self) -> list[str]:
        return sorted({s.tool for s in self.steps})

    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {p.name: _param_schema(p) for p in self.params},
            "required": [p.name for p in self.params],
            "additionalProperties": False,
        }

    def tool_schema(self, tool_name: str) -> dict:
        effect = self.max_effect
        note = ""
        if effect.rank >= Effect.EXTERNAL.rank:
            note = " Contains steps with external side effects; each call asks the user for approval."
        steps = " -> ".join(s.tool for s in self.steps)
        return {
            "name": tool_name,
            "description": (
                f"{self.description} Runs {len(self.steps)} steps deterministically in one call "
                f"({steps}). Approved by the user via Kata.{note}"
            ),
            "parameters": self.input_schema(),
        }

    def to_json(self) -> dict:
        return {
            "spec_version": self.spec_version,
            "signature": self.signature,
            "name": self.name,
            "description": self.description,
            "params": [asdict(p) for p in self.params],
            "steps": [
                {
                    "tool": s.tool,
                    "args": expr_to_json(s.args),
                    "effect": s.effect.value,
                    "effect_reason": s.effect_reason,
                    "schema_fingerprint": s.schema_fingerprint,
                }
                for s in self.steps
            ],
            "output_schema": self.output_schema,
            "evidence": asdict(self.evidence),
        }

    @classmethod
    def from_json(cls, d: dict) -> KataSpec:
        return cls(
            spec_version=int(d.get("spec_version", SPEC_VERSION)),
            signature=d["signature"],
            name=d["name"],
            description=d["description"],
            params=[ParamSpec(**p) for p in d["params"]],
            steps=[
                StepSpec(
                    tool=s["tool"],
                    args=expr_from_json(s["args"]),
                    effect=Effect(s["effect"]),
                    effect_reason=s.get("effect_reason", ""),
                    schema_fingerprint=s.get("schema_fingerprint", ""),
                )
                for s in d["steps"]
            ],
            output_schema=d.get("output_schema") or {},
            evidence=Evidence(**d["evidence"]),
        )


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str


@dataclass
class ValidationReport:
    checks: list[CheckResult]

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    def to_json(self) -> dict:
        return {"passed": self.passed, "checks": [asdict(c) for c in self.checks]}


@dataclass
class StepTrace:
    index: int
    tool: str
    status: str            # ok | error | timeout | skipped
    duration_ms: int
    attempts: int
    error: str | None = None
    result_preview: str = ""


@dataclass
class RunResult:
    ok: bool
    kata: str
    version: int
    steps: list[StepTrace]
    result: Any
    duration_ms: int
    failed_step: int | None = None
    error: str | None = None

    def to_json(self) -> dict:
        return {
            "ok": self.ok,
            "kata": self.kata,
            "version": self.version,
            "duration_ms": self.duration_ms,
            "failed_step": self.failed_step,
            "error": self.error,
            "steps": [asdict(s) for s in self.steps],
            "result": self.result,
        }
