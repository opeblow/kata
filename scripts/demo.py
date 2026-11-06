"""Reproducible end-to-end demo of Kata, without a model or a Hermes install.

It drives the real plugin through a stand-in host (devtools/fake_hermes.py) that fires
the same hooks Hermes fires, with real tools working on a scratch repository:

  1. the "agent" triages TODO comments by hand in four separate conversations
  2. Kata records the calls, detects the routine and proposes a tool
  3. you see the proposal card, and the demo approves it (as a user would with /kata approve)
  4. a fifth conversation does the same task through the compiled tool
  5. the before/after cost is printed from what was measured

Usage:  python scripts/demo.py [--db PATH] [--keep]
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from devtools.fake_hermes import FakeHermes  # noqa: E402
from kata import install  # noqa: E402
from kata.store import Store  # noqa: E402

USERS = ["alice", "bob", "carol", "dave", "erin"]
RULE = "-" * 72


def make_repo(root: Path) -> Path:
    src = root / "repo" / "src"
    src.mkdir(parents=True)
    for i, u in enumerate(USERS):
        (src / f"billing_{u}.py").write_text(
            f'"""Billing adapter owned by {u}."""\n\n\ndef settle_{i}(invoice):\n'
            f"    # TODO({u}): reject invoices with a negative total\n    return invoice\n", encoding="utf-8")
    return root / "repo"


def by_hand(host: FakeHermes, user: str) -> dict:
    turn = host.new_turn()
    host.start_session(turn)
    t0, calls0, model0 = time.perf_counter(), host.tool_calls, host.model_calls
    host.model_request(turn, tokens=1640, latency_s=1.9)
    res = json.loads(host.call(turn, "search_files", {"pattern": f"TODO({user})", "path": "src"}))
    path = res["matches"][0]["path"]
    host.model_request(turn, tokens=1910, latency_s=2.2)
    host.call(turn, "read_file", {"path": path})
    host.model_request(turn, tokens=2280, latency_s=2.6)
    host.call(turn, "terminal", {"command": f"wc -l {path}"})
    host.model_request(turn, tokens=980, latency_s=1.1)
    host.end_session(turn)
    host.runtime_wait()
    return {"tool_calls": host.tool_calls - calls0, "model_calls": host.model_calls - model0,
            "elapsed_ms": int((time.perf_counter() - t0) * 1000)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=None, help="where to keep the Kata database")
    ap.add_argument("--keep", action="store_true", help="do not delete the scratch repository")
    a = ap.parse_args()

    work = Path(tempfile.mkdtemp(prefix="kata-demo-"))
    db = a.db or (work / "kata.db")
    if db.exists():
        db.unlink()
    host = FakeHermes(sandbox=make_repo(work))
    rt = install(host, Store.open_path(db), fingerprint=host.schema_fingerprint)
    host.runtime_wait = rt.wait_idle

    print(RULE + "\n1. The agent triages a TODO by hand, in four separate conversations\n" + RULE)
    for u in USERS[:4]:
        r = by_hand(host, u)
        print(f"   {u:<6} {r['tool_calls']} tool calls, {r['model_calls']} model requests")

    print(RULE + "\n2. Kata analyses the history (it already did, in the background, on session end)\n" + RULE)
    print("   " + host.commands["kata"]("mine").replace("\n", "\n   "))
    kata = rt.store.katas()[0]

    print(RULE + "\n3. Proposal card, exactly as /kata review shows it\n" + RULE)
    print(host.commands["kata"](f"review {kata['id']}"))
    if "kata_triage_todo" in host.tools:
        raise SystemExit("bug: a tool was registered before approval")

    print(RULE + "\n4. The user approves it\n" + RULE)
    print("   " + host.commands["kata"](f"approve {kata['id']} as triage_todo"))

    print(RULE + "\n5. A new conversation does the same task with the compiled tool\n" + RULE)
    turn = host.new_turn()
    calls0, model0, tok0 = host.tool_calls, host.model_calls, host.model_tokens
    host.model_request(turn, tokens=1640, latency_s=1.9)
    out = json.loads(host.call(turn, "kata_triage_todo", {"pattern": USERS[4]}))
    host.model_request(turn, tokens=980, latency_s=1.1)
    print(f"   ok={out['ok']}  steps={' > '.join(s['tool'] + ':' + s['status'] for s in out['steps'])}"
          f"  executed in {out['duration_ms']} ms")
    print(f"   result: {out['result']['output'].strip()}")

    detail = rt.service.detail(kata["id"])
    b = detail["spec"]["evidence"]["baseline"]
    print(RULE + "\n6. Measured cost, one triage\n" + RULE)
    print(f"   {'':<22}{'by hand':>12}{'with kata':>12}")
    print(f"   {'tool calls':<22}{b['tool_calls']:>12}{host.tool_calls - calls0:>12}")
    print(f"   {'model requests':<22}{int(b['avg_model_round_trips']) + 2:>12}{host.model_calls - model0:>12}")
    print(f"   {'model tokens':<22}{int(b['avg_model_tokens']) + 1640 + 980:>12}{host.model_tokens - tok0:>12}")
    print(f"   {'model time (s)':<22}{(b['avg_model_ms'] or 0) / 1000 + 3.0:>12.1f}{3.0:>12.1f}")
    print("\n   Tool calls and model requests are counted. Token and latency figures come from this")
    print("   script's simulated model; with a real model Kata measures them from post_api_request.")

    print(RULE + "\n7. Meanwhile, another routine repeats: back up a file before editing it\n" + RULE)
    for u in USERS[1:4]:
        turn = host.new_turn()
        src = json.loads(host.call(turn, "read_file", {"path": f"src/billing_{u}.py"}))
        host.call(turn, "write_file", {"path": f"src/billing_{u}.py.bak", "content": src["content"]})
        host.end_session(turn)
    rt.wait_idle()
    rt.service.mine()
    pending = [k["name"] for k in rt.store.katas("candidate")]
    print(f"   Proposed and waiting for review: {', '.join(pending) or 'none'}")
    print(f"\n   Database: {db}")
    if not a.keep:
        shutil.rmtree(work / "repo", ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
