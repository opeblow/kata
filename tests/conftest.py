from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from devtools.fake_hermes import FakeHermes  # noqa: E402
from kata import install  # noqa: E402
from kata.store import Store  # noqa: E402

USERS = ["alice", "bob", "carol", "dave", "erin", "frank"]


@pytest.fixture()
def sandbox(tmp_path: Path) -> Path:
    src = tmp_path / "repo" / "src"
    src.mkdir(parents=True)
    for i, u in enumerate(USERS):
        (src / f"module_{u}.py").write_text(
            f"def handler_{i}():\n    # TODO({u}): validate input\n    return {i}\n", encoding="utf-8")
    return tmp_path / "repo"


@pytest.fixture()
def host(sandbox: Path, tmp_path: Path):
    h = FakeHermes(sandbox=sandbox)
    store = Store.open_path(tmp_path / "kata.db")
    rt = install(h, store, fingerprint=h.schema_fingerprint)
    h.runtime = rt
    yield h
    store.close()


def triage_by_hand(host: FakeHermes, user: str, conversation_id: str | None = None) -> None:
    """What the agent does without Kata: one model round-trip before every tool call."""
    turn = host.new_turn(conversation_id)
    host.start_session(turn)
    host.model_request(turn, tokens=1500, latency_s=0.0)
    res = host.call(turn, "search_files", {"pattern": f"TODO({user})", "path": "src"})
    import json
    path = json.loads(res)["matches"][0]["path"]
    host.model_request(turn, tokens=1800, latency_s=0.0)
    host.call(turn, "read_file", {"path": path})
    host.model_request(turn, tokens=2100, latency_s=0.0)
    host.call(turn, "terminal", {"command": f"wc -l {path}"})
    host.model_request(turn, tokens=900, latency_s=0.0)
    host.end_session(turn)
    host.runtime.wait_idle()
