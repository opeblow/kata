"""``/kata`` slash command and ``hermes kata`` CLI. Both are thin shells over KataService."""
from __future__ import annotations

import json
import shlex
from collections.abc import Callable
from datetime import datetime
from typing import Any

from .card import render_card
from .service import KataError, KataService

USAGE = """Kata: compiled routines from your own history.

/kata                       list katas and candidates
/kata review <id|name>      full proposal: steps, permissions, schemas, side effects, validation
/kata approve <id> [as <name>]
/kata reject <id> [reason]
/kata disable <name>        /kata enable <name>
/kata rollback <name> [version]
/kata runs <name>           recent executions with step traces
/kata mine                  analyse history now
/kata stats                 savings so far"""


def _ts(t: float | None) -> str:
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M") if t else "-"


def _list(svc: KataService) -> str:
    rows = svc.store.katas()
    if not rows:
        n = svc.store.event_count()
        return (f"No katas yet. Kata has observed {n} tool call(s); it proposes a routine once it has seen it in "
                f"{svc.cfg.min_support} separate conversations. Run `/kata mine` to check now.")
    out = ["id  status     name                                  steps  support  confidence"]
    for k in rows:
        spec, v, _ = svc.spec(k["id"])
        out.append(f"{k['id']:<3} {k['status']:<10} {k['name']:<37} {len(spec.steps):<6} "
                   f"{spec.evidence.support:<8} {spec.evidence.confidence:.2f}")
        if k["status_reason"] and k["status"] in ("stale", "suspended"):
            out.append(f"    reason: {k['status_reason']}")
    return "\n".join(out)


def handle(svc: KataService, raw: str, after_change: Callable[[], None] = lambda: None) -> str:
    try:
        argv = shlex.split(raw or "")
    except ValueError as e:
        return f"Could not parse arguments: {e}"
    if not argv:
        return _list(svc)
    cmd, rest = argv[0].lower(), argv[1:]
    try:
        if cmd in ("help", "-h", "--help"):
            return USAGE
        if cmd in ("list", "ls"):
            return _list(svc)
        if cmd in ("review", "show") and rest:
            return render_card(svc.detail(rest[0]))
        if cmd == "approve" and rest:
            name = rest[2] if len(rest) >= 3 and rest[1] == "as" else None
            res = svc.approve(rest[0], name=name, actor="user:/kata")
            after_change()
            return (f"Approved `{res['name']}` v{res['version']} and registered it. Hermes builds each session's "
                    f"tool list when the session starts, so the agent can call it from your next session.")
        if cmd == "reject" and rest:
            svc.reject(rest[0], " ".join(rest[1:]), actor="user:/kata")
            return "Rejected. Kata will not propose this routine again."
        if cmd == "disable" and rest:
            res = svc.disable(rest[0], " ".join(rest[1:]), actor="user:/kata")
            after_change()
            return f"Disabled `{res['name']}`. Its tool is unregistered."
        if cmd == "enable" and rest:
            res = svc.enable(rest[0], actor="user:/kata")
            after_change()
            return f"Re-enabled `{res['name']}` v{res['version']} after re-validation."
        if cmd == "rollback" and rest:
            res = svc.rollback(rest[0], int(rest[1]) if len(rest) > 1 else None, actor="user:/kata")
            after_change()
            return f"`{res['name']}` now runs v{res['version']}."
        if cmd == "runs" and rest:
            d = svc.detail(rest[0])
            if not d["runs"]:
                return "No runs yet."
            lines = []
            for r in d["runs"]:
                steps = " ".join(f"{s['tool']}:{s['status']}" for s in r["trace"]["steps"])
                lines.append(f"{_ts(r['started_at'])}  v{r['version']}  {'ok ' if r['ok'] else 'ERR'}  "
                             f"{r['duration_ms']} ms  {steps}" + (f"  ({r['error']})" if r["error"] else ""))
            return "\n".join(lines)
        if cmd == "mine":
            rep = svc.mine()
            after_change()
            parts = [f"Analysed {rep.events} recorded tool calls."]
            if rep.proposed:
                parts.append("New proposals: " + ", ".join(rep.proposed) + ". Review with `/kata review <name>`.")
            waiting = rep.pending
            if waiting:
                parts.append("Waiting for your review: " + ", ".join(waiting) + ". See `/kata review <name>`.")
            if rep.updated:
                parts.append("Revised: " + ", ".join(rep.updated))
            if rep.drift:
                parts.append("Workflow changed, flagged for review: " + ", ".join(rep.drift))
            if rep.rejected:
                parts.append(f"{len(rep.rejected)} pattern(s) seen but not proposed: "
                             + "; ".join(f"{' > '.join(r['tools'])} ({r['reason']})" for r in rep.rejected[:5]))
            if len(parts) == 1:
                parts.append("No routine has repeated often enough yet.")
            return "\n".join(parts)
        if cmd == "stats":
            return json.dumps(svc.overview(), indent=2)
    except KataError as e:
        return f"Kata: {e}"
    except ValueError as e:
        return f"Kata: {e}"
    return USAGE


def setup_cli(parser: Any) -> None:
    parser.add_argument("args", nargs="*", help="same subcommands as /kata (list, review, approve, mine, ...)")


def make_cli_handler(get_service: Callable[[], KataService]) -> Callable[[Any], int]:
    def run(ns: Any) -> int:
        print(handle(get_service(), " ".join(shlex.quote(a) for a in getattr(ns, "args", []) or [])))
        return 0
    return run
