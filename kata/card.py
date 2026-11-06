"""Human-readable proposal card shown before approval (slash command and CLI)."""
from __future__ import annotations

import json
from typing import Any

from .models import Arr, Concat, Const, Effect, Expr, Obj, Param, Ref, expr_from_json


def show_expr(e: Expr) -> str:
    if isinstance(e, Const):
        return json.dumps(e.value)
    if isinstance(e, Param):
        return "{" + e.name + "}"
    if isinstance(e, Ref):
        path = ".".join(str(p) for p in e.path) or "output"
        return f"<step {e.step + 1}.{path}>"
    if isinstance(e, Concat):
        inner = "".join(p.value if isinstance(p, Const) and isinstance(p.value, str) else show_expr(p)
                        for p in e.parts)
        return json.dumps(inner).replace("\\\"", "'")
    if isinstance(e, Obj):
        return ", ".join(f"{k}={show_expr(v)}" for k, v in e.fields)
    if isinstance(e, Arr):
        return "[" + ", ".join(show_expr(v) for v in e.items) + "]"
    return "?"


def render_card(detail: dict[str, Any]) -> str:
    k, spec, val = detail["kata"], detail["spec"], detail["validation"]
    ev = spec["evidence"]
    b = ev.get("baseline", {})
    lines = [
        f"## {k['name']}  ({k['status']}, v{detail['latest_version']})",
        "",
        spec["description"],
        "",
        "### Why Kata proposed this",
        f"- Seen in {ev['support']} separate conversations ({ev['occurrences']} matching runs)",
        f"- Held-out fit {ev['fit_rate']:.0%}, confidence {ev['confidence']:.2f}",
        f"- By hand it took {b.get('tool_calls')} tool calls"
        + (f", {b['avg_model_round_trips']} model round-trips" if b.get("avg_model_round_trips") is not None else "")
        + (f", ~{int(b['avg_model_tokens'])} tokens" if b.get("avg_model_tokens") else "")
        + (f", {int(b['avg_span_ms'])} ms" if b.get("avg_span_ms") else ""),
        "",
        "### Steps (executed in this order, no model calls in between)",
    ]
    for i, s in enumerate(spec["steps"]):
        lines.append(f"{i + 1}. `{s['tool']}`({show_expr(expr_from_json(s['args']))})  [{s['effect']}: {s['effect_reason']}]")
    lines += ["", "### Permissions",
              f"- May call only: {', '.join(sorted({s['tool'] for s in spec['steps']}))}",
              f"- Highest effect: {detail['max_effect']}"]
    if detail["requires_approval_per_call"]:
        lines.append("- Contains external or unknown effects: every invocation asks you first; side-effecting steps are never retried")
    else:
        lines.append("- Read-only or local steps; read-only steps may be retried once on error")
    lines += ["", "### Input schema", "```json", json.dumps(detail["input_schema"], indent=2), "```",
              "### Output schema", "```json", json.dumps(spec["output_schema"], indent=2), "```",
              "### Validation (" + ("passed" if val.get("passed") else "FAILED") + ")"]
    for c in val.get("checks", []):
        lines.append(f"- [{'x' if c['passed'] else ' '}] {c['name']}: {c['detail']}")
    lines += ["", f"Approve with `/kata approve {k['id']}` (optionally `as <name>`), or `/kata reject {k['id']}`."]
    return "\n".join(lines)


def effect_label(e: str) -> str:
    return {"read": "read only", "write": "local write", "external": "external", "unknown": "unknown"}.get(e, e)


__all__ = ["render_card", "show_expr", "effect_label", "Effect"]
