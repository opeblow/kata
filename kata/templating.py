"""Argument templates: inference, rendering and matching.

``infer`` generalises the concrete arguments seen across several occurrences of one
step into an expression tree: values that never change become constants, values that
equal something an earlier step returned become references (data flow), and the rest
become parameters of the compiled tool.

``render`` evaluates a template for a real call. ``match`` runs the other way: given a
template and a concrete call it recovers the parameter values, or reports that the
call does not fit. ``match`` powers leave-one-out validation and drift detection.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from .models import Arr, Concat, Const, Expr, Obj, Param, ParamSpec, Ref

# Separators that are structure, never part of an input: whitespace, punctuation, path separators
# and the escape character, so a regex-escaped "TODO\(alice\)" yields the input "alice".
_TOKEN_SPLIT = re.compile(r"(\s+|[/\\=:,@#?&()\[\]{}\"'.\-])")
_MAX_LEAVES = 4000
_MISSING = object()


class RenderError(Exception):
    pass


def parse_result(result: Any) -> Any:
    if isinstance(result, str):
        try:
            return json.loads(result)
        except (ValueError, TypeError):
            return result
    return result


def lookup(root: Any, path: tuple[str | int, ...]) -> Any:
    node = root
    for key in path:
        if isinstance(key, int) and isinstance(node, list) and -len(node) <= key < len(node):
            node = node[key]
        elif isinstance(key, str) and isinstance(node, dict) and key in node:
            node = node[key]
        else:
            return _MISSING
    return node


def _leaves(node: Any, prefix: tuple = ()) -> Iterator[tuple[tuple, Any]]:
    stack = [(prefix, node)]
    seen = 0
    while stack and seen < _MAX_LEAVES:
        path, cur = stack.pop()
        seen += 1
        if isinstance(cur, dict):
            for k, v in cur.items():
                stack.append((path + (k,), v))
        elif isinstance(cur, list):
            for i, v in enumerate(cur[:200]):
                stack.append((path + (i,), v))
        else:
            yield path, cur


_BACKSLASH = "\\"
BS_CLASS = "\\\\"  # a backslash inside a regex character class


def _posix(s: str) -> str:
    return s.replace(_BACKSLASH, "/")


def _same(a: Any, b: Any) -> bool:
    """Equal values, treating Windows and POSIX path separators in strings as the same."""
    if json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str):
        return True
    return isinstance(a, str) and isinstance(b, str) and _posix(a) == _posix(b)


def normalized(value: Any) -> Any:
    """Separator-insensitive form of a value, for comparing rendered and recorded arguments."""
    if isinstance(value, str):
        return _posix(value)
    if isinstance(value, dict):
        return {k: normalized(v) for k, v in value.items()}
    if isinstance(value, list):
        return [normalized(v) for v in value]
    return value


def _fmt_for(leaves: list, written: list) -> str:
    """'posix' when the agent mostly rewrote backslashes in this value as forward slashes."""
    pairs = [(lv, w) for lv, w in zip(leaves, written, strict=False) if isinstance(lv, str) and _BACKSLASH in lv]
    if not pairs:
        return ""
    rewritten = sum(1 for lv, w in pairs if w != lv and w == _posix(lv))
    return "posix" if rewritten * 2 >= len(pairs) else ""


def json_type(values: list[Any]) -> str:
    if all(v is None for v in values):
        return "null"
    if all(isinstance(v, bool) for v in values):
        return "boolean"
    if all(isinstance(v, int) and not isinstance(v, bool) for v in values):
        return "integer"
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values):
        return "number"
    return "string"


def _tokens(s: str) -> list[str]:
    return [t for t in _TOKEN_SPLIT.split(s) if t != ""]


def _is_delim(tok: str) -> bool:
    return bool(_TOKEN_SPLIT.fullmatch(tok))


def _param_base(key: str, sample: Any) -> str:
    s = str(sample)
    if isinstance(sample, str):
        if s.startswith(("http://", "https://")):
            return "url"
        if s.isdigit():
            return "number"
        if re.fullmatch(r"[\w.\-]+\.[A-Za-z0-9]{1,6}", s):
            return "file"
    base = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_") or "value"
    return base[:40]


@dataclass
class Inference:
    """Mutable context while inferring one candidate's templates."""

    occurrence_results: list[list[Any]]           # [occurrence][step] -> parsed result
    params: dict[str, list[Any]] = field(default_factory=dict)  # name -> value per occurrence
    descriptions: dict[str, str] = field(default_factory=dict)

    def _new_param(self, base: str, values: list[Any], description: str) -> Param:
        for name, existing in self.params.items():
            if all(_same(a, b) for a, b in zip(existing, values, strict=False)):
                return Param(name)
        name, n = base, 2
        while name in self.params:
            name, n = f"{base}_{n}", n + 1
        self.params[name] = values
        self.descriptions[name] = description
        return Param(name)

    def _find_ref(self, step: int, values: list[Any]) -> Ref | None:
        if not values or isinstance(values[0], (dict, list)) or values[0] in ("", None):
            return None
        first = self.occurrence_results[0]
        for j in range(step - 1, -1, -1):
            for path, leaf in _leaves(first[j]):
                if not _same(leaf, values[0]):
                    continue
                if all(_same(lookup(occ[j], path), v) for occ, v in zip(self.occurrence_results, values, strict=False)):
                    leaves = [lookup(occ[j], path) for occ in self.occurrence_results]
                    return Ref(j, path, _fmt_for(leaves, values))
            if all(isinstance(occ[j], str) and _same(occ[j].strip(), v)
                   for occ, v in zip(self.occurrence_results, values, strict=False)):
                return Ref(j, ())
        return None

    def infer(self, step: int, key: str, values: list[Any]) -> Expr:
        if all(_same(v, values[0]) for v in values):
            return Const(values[0])
        ref = self._find_ref(step, values)
        if ref is not None:
            return ref
        if all(isinstance(v, dict) for v in values) and all(set(v) == set(values[0]) for v in values):
            return Obj(tuple((k, self.infer(step, k, [v[k] for v in values])) for k in sorted(values[0])))
        if all(isinstance(v, list) for v in values) and all(len(v) == len(values[0]) for v in values):
            return Arr(tuple(self.infer(step, key, [v[i] for v in values]) for i in range(len(values[0]))))
        if all(isinstance(v, str) for v in values):
            concat = self._infer_string(step, key, values)
            if concat is not None:
                return concat
        return self._new_param(_param_base(key, values[0]), values,
                               f"Value for `{key}` in step {step + 1}.")

    def _find_embedded_ref(self, step: int, key: str, values: list[str]) -> Expr | None:
        """A string that contains an earlier result verbatim, e.g. ``"git blame <path>"``."""
        first = self.occurrence_results[0]
        best: tuple[int, tuple, int] | None = None
        for j in range(step - 1, -1, -1):
            for path, leaf in _leaves(first[j]):
                if not isinstance(leaf, str) or len(leaf) < 3 or _same(leaf, values[0]) \
                        or _posix(leaf) not in _posix(values[0]):
                    continue
                ok = True
                for occ, v in zip(self.occurrence_results, values, strict=False):
                    lv = lookup(occ[j], path)
                    if not isinstance(lv, str) or _posix(lv) not in _posix(v):
                        ok = False
                        break
                if ok and (best is None or len(leaf) > best[2]):
                    best = (j, path, len(leaf))
        if best is None:
            return None
        j, path, _ = best
        pres, posts, leaves, written = [], [], [], []
        for occ, v in zip(self.occurrence_results, values, strict=False):
            lv = lookup(occ[j], path)
            i = _posix(v).index(_posix(lv))  # same length either way: one character per separator
            pres.append(v[:i])
            posts.append(v[i + len(lv):])
            leaves.append(lv)
            written.append(v[i:i + len(lv)])
        fmt = _fmt_for(leaves, written)
        parts: list[Expr] = []
        for side in (pres, None, posts):
            if side is None:
                parts.append(Ref(j, path, fmt))
            elif all(s == "" for s in side):
                continue
            elif any(s == "" for s in side):
                return None
            else:
                sub = self.infer(step, key, side)
                parts.extend(sub.parts if isinstance(sub, Concat) else [sub])
        return Concat(tuple(_merge_consts(parts)))

    def _infer_string(self, step: int, key: str, values: list[str]) -> Expr | None:
        embedded = self._find_embedded_ref(step, key, values)
        if embedded is not None:
            return embedded
        toks = [_tokens(v) for v in values]
        same_shape = all(len(t) == len(toks[0]) for t in toks) and all(
            _is_delim(toks[0][i]) == _is_delim(t[i]) and (not _is_delim(t[i]) or t[i] == toks[0][i])
            for t in toks for i in range(len(toks[0])))
        parts: list[Expr] = []
        if same_shape and len(toks[0]) > 1:
            for i in range(len(toks[0])):
                col = [t[i] for t in toks]
                if all(c == col[0] for c in col):
                    parts.append(Const(col[0]))
                    continue
                ref = self._find_ref(step, col)
                if ref is not None:
                    parts.append(ref)
                else:
                    sample = col[0]
                    vals: list[Any] = [int(c) for c in col] if all(c.isdigit() for c in col) else col
                    parts.append(self._new_param(_param_base(key, sample), vals,
                                                 f"Variable part of `{key}` in step {step + 1}."))
        else:
            prefix = _common_prefix(values)
            suffix = _common_suffix([v[len(prefix):] for v in values])
            if not prefix and not suffix:
                return None
            middles = [v[len(prefix):len(v) - len(suffix)] for v in values]
            if any(m == "" for m in middles):
                return None
            ref = self._find_ref(step, middles)
            mid = ref or self._new_param(_param_base(key, middles[0]), middles,
                                         f"Variable part of `{key}` in step {step + 1}.")
            parts = [p for p in (Const(prefix) if prefix else None, mid, Const(suffix) if suffix else None) if p]
        return Concat(tuple(_merge_consts(parts)))


def _common_prefix(values: list[str]) -> str:
    if not values:
        return ""
    lo = min(values)
    hi = max(values)
    i = 0
    while i < len(lo) and lo[i] == hi[i]:
        i += 1
    # Never split inside a word: back off to the last delimiter.
    while i > 0 and i < len(lo) and not _is_delim(lo[i - 1]):
        i -= 1
    return lo[:i]


def _common_suffix(values: list[str]) -> str:
    if not values:
        return ""
    n = min(len(v) for v in values)
    k = 0
    while k < n and len({v[len(v) - 1 - k] for v in values}) == 1:
        k += 1
    # Never split inside a word: shrink to start at a delimiter.
    first = values[0]
    while k > 0 and k < len(first) and not _is_delim(first[len(first) - k]):
        k -= 1
    return first[len(first) - k:] if k else ""


def _merge_consts(parts: list[Expr]) -> list[Expr]:
    out: list[Expr] = []
    for p in parts:
        if isinstance(p, Const) and out and isinstance(out[-1], Const):
            out[-1] = Const(str(out[-1].value) + str(p.value))
        else:
            out.append(p)
    return out


def param_specs(inf: Inference) -> list[ParamSpec]:
    return [ParamSpec(name=n, type=json_type(v), description=inf.descriptions.get(n, ""),
                      examples=list(dict.fromkeys(json.dumps(x, default=str) for x in v))[:3])
            for n, v in inf.params.items()]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render(expr: Expr, inputs: dict[str, Any], results: list[Any]) -> Any:
    if isinstance(expr, Const):
        return expr.value
    if isinstance(expr, Param):
        if expr.name not in inputs:
            raise RenderError(f"missing input `{expr.name}`")
        return inputs[expr.name]
    if isinstance(expr, Ref):
        if expr.step >= len(results):
            raise RenderError(f"step {expr.step + 1} has not run yet")
        root = results[expr.step]
        value = root.strip() if expr.path == () and isinstance(root, str) else lookup(root, expr.path)
        if value is _MISSING:
            raise RenderError(f"step {expr.step + 1} result has no field {'.'.join(map(str, expr.path))}")
        if expr.fmt == "posix" and isinstance(value, str):
            value = _posix(value)
        return value
    if isinstance(expr, Concat):
        return "".join(_stringify(render(p, inputs, results)) for p in expr.parts)
    if isinstance(expr, Obj):
        return {k: render(v, inputs, results) for k, v in expr.fields}
    if isinstance(expr, Arr):
        return [render(v, inputs, results) for v in expr.items]
    raise RenderError(f"unknown expression {expr!r}")


def _stringify(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return v if isinstance(v, str) else json.dumps(v) if isinstance(v, (dict, list)) else str(v)


# ---------------------------------------------------------------------------
# Matching (inverse of render)
# ---------------------------------------------------------------------------

def match(expr: Expr, value: Any, results: list[Any], bindings: dict[str, Any],
          types: dict[str, str]) -> bool:
    """Unify ``expr`` with a concrete ``value``; fills ``bindings`` with parameter values."""
    if isinstance(expr, Const):
        return _same(expr.value, value)
    if isinstance(expr, Param):
        return _bind(expr.name, value, bindings)
    if isinstance(expr, Ref):
        try:
            return _same(render(expr, {}, results), value)
        except RenderError:
            return False
    if isinstance(expr, Obj):
        if not isinstance(value, dict) or set(value) != {k for k, _ in expr.fields}:
            return False
        return all(match(v, value[k], results, bindings, types) for k, v in expr.fields)
    if isinstance(expr, Arr):
        if not isinstance(value, list) or len(value) != len(expr.items):
            return False
        return all(match(e, v, results, bindings, types) for e, v in zip(expr.items, value, strict=False))
    if isinstance(expr, Concat):
        if not isinstance(value, str):
            return False
        pattern, groups = "", []
        for p in expr.parts:
            if isinstance(p, Const):
                pattern += re.escape(_stringify(p.value))
            elif isinstance(p, Param) and p.name in bindings:
                pattern += re.escape(_stringify(bindings[p.name]))
            elif isinstance(p, Param):
                pattern += "(.+?)"
                groups.append(p.name)
            else:
                try:
                    piece = re.escape(_posix(_stringify(render(p, bindings, results))))
                    pattern += piece.replace("/", "[/" + BS_CLASS + "]")
                except RenderError:
                    return False
        m = re.fullmatch(pattern, value, flags=re.DOTALL)
        if not m:
            return False
        for name, raw in zip(groups, m.groups(), strict=False):
            v: Any = raw
            if types.get(name) == "integer":
                if not raw.isdigit():
                    return False
                v = int(raw)
            if not _bind(name, v, bindings):
                return False
        return True
    return False


def _bind(name: str, value: Any, bindings: dict[str, Any]) -> bool:
    if name in bindings:
        return _same(bindings[name], value)
    bindings[name] = value
    return True
