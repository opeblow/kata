"""Pattern discovery over recorded tool calls.

The miner works in four stages:

1. **Segment** history into *runs*: contiguous successful calls inside a single user
   turn, broken by failures and long pauses. A routine never spans two turns, so
   unrelated work is never glued together.
2. **Count** every contiguous window of 2..max_steps calls by its *signature* (tool
   names plus argument keys), once per turn, and keep windows seen in at least
   ``min_support`` distinct conversations. Windows contained in an equally frequent
   longer window are dropped, so the miner proposes whole routines, not fragments.
3. **Generalise** each window's arguments into templates (``templating.Inference``).
4. **Score** with leave-one-out validation: a template built from all-but-one
   occurrence must reproduce the held-out one exactly. ``confidence`` combines that
   fit rate with how often the routine was seen.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass

from .effects import classify_occurrences, compilable
from .models import Const, Evidence, Expr, KataSpec, Obj, Ref, StepSpec, ToolEvent
from .redact import REDACTED, looks_secret
from .templating import Inference, json_type, match, param_specs, parse_result

MAX_GAP_S = 300.0
MAX_EXAMPLES = 12
MAX_OCCURRENCES = 24   # most recent occurrences used to build and score a template


@dataclass
class MinerConfig:
    min_support: int = 3
    min_confidence: float = 0.75
    max_steps: int = 8
    max_gap_s: float = MAX_GAP_S


@dataclass
class Rejection:
    signature: str
    tools: list[str]
    reason: str


def signature_of(events: list[ToolEvent]) -> str:
    raw = "|".join(f"{e.tool_name}({','.join(sorted(e.args))})" for e in events)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def segment(events: list[ToolEvent], max_gap_s: float = MAX_GAP_S) -> list[list[ToolEvent]]:
    """Split history into runs of successful, compilable calls within one turn."""
    by_turn: dict[tuple[str, str], list[ToolEvent]] = defaultdict(list)
    for e in events:
        by_turn[(e.conversation_id, e.turn_id or e.session_id)].append(e)
    runs: list[list[ToolEvent]] = []
    for turn_events in by_turn.values():
        turn_events.sort(key=lambda e: e.id)
        cur: list[ToolEvent] = []
        for e in turn_events:
            gap_ok = not cur or (e.started_at - cur[-1].ended_at) <= max_gap_s
            if e.ok and compilable(e.tool_name) and gap_ok:
                cur.append(e)
                continue
            if len(cur) >= 2:
                runs.append(cur)
            cur = [e] if (e.ok and compilable(e.tool_name)) else []
        if len(cur) >= 2:
            runs.append(cur)
    return runs


def _windows(runs: list[list[ToolEvent]], max_steps: int) -> dict[str, list[list[ToolEvent]]]:
    found: dict[str, list[list[ToolEvent]]] = defaultdict(list)
    for run in runs:
        seen_in_run: set[str] = set()
        for n in range(2, min(max_steps, len(run)) + 1):
            for start in range(0, len(run) - n + 1):
                w = run[start:start + n]
                sig = signature_of(w)
                if sig in seen_in_run:
                    continue
                seen_in_run.add(sig)
                found[sig].append(w)
    return found


def _support(occ: list[list[ToolEvent]]) -> int:
    return len({o[0].conversation_id for o in occ})


def _one_per_conversation(occ: list[list[ToolEvent]]) -> list[list[ToolEvent]]:
    """Keep the most recent occurrence per conversation so one chat cannot dominate."""
    latest: dict[str, list[ToolEvent]] = {}
    for o in occ:
        cid = o[0].conversation_id
        if cid not in latest or o[0].id > latest[cid][0].id:
            latest[cid] = o
    return sorted(latest.values(), key=lambda o: o[0].id)


def _tool_seq(occ: list[list[ToolEvent]]) -> tuple[str, ...]:
    return tuple(e.tool_name for e in occ[0])


def _shape_key(events: list[ToolEvent]) -> tuple:
    return tuple((e.tool_name, tuple(sorted(e.args))) for e in events)


def _anchored(e: Expr) -> bool:
    """True when an expression ties the step to the routine: data flow or a fixed value."""
    if isinstance(e, Ref):
        return True
    if isinstance(e, Const):
        return e.value not in ("", None, [], {})
    if isinstance(e, Obj):
        return any(_anchored(v) for _, v in e.fields)
    for child in getattr(e, "parts", ()) or getattr(e, "items", ()) or ():
        if _anchored(child):
            return True
    return False


def _consts(e: Expr) -> list:
    if isinstance(e, Const):
        return [e.value]
    if isinstance(e, Obj):
        return [c for _, v in e.fields for c in _consts(v)]
    items = getattr(e, "items", None)
    if isinstance(items, tuple):
        return [c for v in items for c in _consts(v)]
    parts = getattr(e, "parts", None)
    if parts:
        return [c for p in parts for c in _consts(p)]
    return []


def _has_secret(values: list) -> bool:
    text = json.dumps(values, default=str)
    return REDACTED in text or any(looks_secret(v) for v in values if isinstance(v, str)) or looks_secret(text)


def build_templates(occ: list[list[ToolEvent]]) -> tuple[Inference, list[Expr]]:
    results = [[parse_result(e.result) for e in o] for o in occ]
    inf = Inference(results)
    exprs = [inf.infer(i, "args", [o[i].args for o in occ]) for i in range(len(occ[0]))]
    return inf, exprs


def fits(exprs: list[Expr], types: dict[str, str], occurrence: list[ToolEvent]) -> dict | None:
    """Return the inputs that reproduce ``occurrence`` under ``exprs``, or ``None``."""
    bindings: dict = {}
    results = []
    for expr, e in zip(exprs, occurrence, strict=False):
        if not match(expr, e.args, results, bindings, types):
            return None
        results.append(parse_result(e.result))
    return bindings


def leave_one_out(occ: list[list[ToolEvent]]) -> float:
    if len(occ) < 3:
        return 0.0
    hits = 0
    for k in range(len(occ)):
        train = occ[:k] + occ[k + 1:]
        inf, exprs = build_templates(train)
        types = {p.name: p.type for p in param_specs(inf)}
        if fits(exprs, types, occ[k]) is not None:
            hits += 1
    return hits / len(occ)


def _output_schema(occ: list[list[ToolEvent]]) -> dict:
    finals = [parse_result(o[-1].result) for o in occ]
    if all(isinstance(f, dict) for f in finals):
        keys = set(finals[0])
        for f in finals[1:]:
            keys &= set(f)
        return {"type": "object",
                "properties": {k: {"type": json_type([f[k] for f in finals]) if not isinstance(finals[0][k], (dict, list))
                                   else ("object" if isinstance(finals[0][k], dict) else "array")}
                               for k in sorted(keys)},
                "required": sorted(keys)}
    if all(isinstance(f, list) for f in finals):
        return {"type": "array"}
    return {"type": "string"}


def _name_for(tools: tuple[str, ...]) -> str:
    parts: list[str] = []
    for t in tools:
        short = re.sub(r"[^a-z0-9]+", "_", t.lower()).strip("_").split("_")[0] or "step"
        if not parts or parts[-1] != short:
            parts.append(short)
    return ("kata_" + "_".join(parts))[:56]


def _describe(tools: tuple[str, ...], support: int) -> str:
    return (f"Routine observed in {support} separate conversations: "
            + ", then ".join(f"`{t}`" for t in tools) + ".")


def measure_baseline(occ: list[list[ToolEvent]], api_lookup: Callable | None) -> dict:
    """What doing the routine by hand cost: tool calls, model round-trips, tokens and time."""
    spans, llm_calls, tokens, llm_time = [], [], [], []
    for o in occ:
        spans.append(max(0.0, o[-1].ended_at - o[0].started_at) * 1000)
        if api_lookup:
            calls = api_lookup(o[0].turn_id, o[0].seq, o[-1].seq)
            llm_calls.append(len(calls))
            tokens.append(sum(c[2] for c in calls))
            llm_time.append(sum(c[1] for c in calls) * 1000)
    def avg(xs):
        return (round(sum(xs) / len(xs), 1) if xs else None)
    return {
        "tool_calls": len(occ[0]),
        "avg_span_ms": avg(spans),
        "avg_model_round_trips": avg(llm_calls),
        "avg_model_tokens": avg(tokens),
        "avg_model_ms": avg(llm_time),
        "measured_occurrences": len(occ),
    }


def compile_candidate(occ: list[list[ToolEvent]], *, support: int,
                      api_lookup: Callable | None = None,
                      fingerprint: Callable[[str], str | None] | None = None) -> tuple[KataSpec | None, str]:
    tools = _tool_seq(occ)
    inf, exprs = build_templates(occ)
    loose = [i + 1 for i, e in enumerate(exprs) if i > 0 and not _anchored(e)]
    if loose:
        return None, (f"step {', '.join(map(str, loose))} shares no data or fixed arguments with the routine; "
                      "the sequence looks coincidental")
    if _has_secret([c for e in exprs for c in _consts(e)]):
        return None, "a constant argument contains a secret or a redacted value"
    params = param_specs(inf)
    if any(_has_secret([json.loads(x) for x in p.examples]) for p in params):
        return None, "a parameter's observed values contain secrets"
    fit = leave_one_out(occ)
    confidence = round(fit * (1 - 0.6 ** support), 3)
    steps = []
    for i, t in enumerate(tools):
        effect, reason = classify_occurrences(t, [o[i].args for o in occ])
        steps.append(StepSpec(tool=t, args=exprs[i], effect=effect, effect_reason=reason,
                              schema_fingerprint=(fingerprint(t) or "") if fingerprint else ""))
    evidence = Evidence(
        support=support, occurrences=len(occ), fit_rate=round(fit, 3), confidence=confidence,
        example_event_ids=[[e.id for e in o] for o in occ[-MAX_EXAMPLES:]],
        first_seen=occ[0][0].ended_at, last_seen=occ[-1][-1].ended_at,
        baseline=measure_baseline(occ, api_lookup),
    )
    spec = KataSpec(signature=signature_of(occ[0]), name=_name_for(tools), description=_describe(tools, support),
                    params=params, steps=steps, output_schema=_output_schema(occ), evidence=evidence)
    return spec, ""


def mine(events: list[ToolEvent], cfg: MinerConfig, *, api_lookup: Callable | None = None,
         fingerprint: Callable[[str], str | None] | None = None) -> tuple[list[KataSpec], list[Rejection]]:
    windows = _windows(segment(events, cfg.max_gap_s), cfg.max_steps)
    frequent = {s: _one_per_conversation(o) for s, o in windows.items() if _support(o) >= cfg.min_support}
    # Maximality, in linear time: a window is dropped when a window one call longer that contains
    # it (as prefix or suffix) is at least as frequent. Domination is transitive along the chain.
    support_by_key = {_shape_key(o[0]): _support(o) for o in frequent.values()}
    dominated: set[tuple] = set()
    for key, sup in support_by_key.items():
        if len(key) > 2:
            for sub in (key[:-1], key[1:]):
                if support_by_key.get(sub, -1) <= sup:
                    dominated.add(sub)
    keep = [(sig, occ) for sig, occ in frequent.items() if _shape_key(occ[0]) not in dominated]
    specs, rejected = [], []
    for sig, occ in sorted(keep, key=lambda x: (-_support(x[1]), -len(x[1][0]))):
        support = _support(occ)
        occ = occ[-MAX_OCCURRENCES:]  # recent behaviour wins; bounds leave-one-out cost
        spec, why = compile_candidate(occ, support=support, api_lookup=api_lookup, fingerprint=fingerprint)
        if spec is None:
            rejected.append(Rejection(sig, list(_tool_seq(occ)), why))
        elif spec.evidence.confidence < cfg.min_confidence:
            rejected.append(Rejection(sig, list(_tool_seq(occ)),
                                      f"confidence {spec.evidence.confidence:.2f} below {cfg.min_confidence:.2f} "
                                      f"(fit {spec.evidence.fit_rate:.2f})"))
        else:
            specs.append(spec)
    return specs, rejected


def occurrences_for(events: list[ToolEvent], signature: str, max_steps: int, max_gap_s: float = MAX_GAP_S
                    ) -> list[list[ToolEvent]]:
    return _windows(segment(events, max_gap_s), max_steps).get(signature, [])
