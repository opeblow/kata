"""Mining must stay fast and correct on a realistic month of history."""
from __future__ import annotations

import json
import random
import time

from kata.miner import MinerConfig, mine
from kata.models import ToolEvent

TOOLS = ["read_file", "search_files", "web_search", "web_extract", "write_file", "patch", "terminal"]


def _history(n_conversations: int, seed: int = 7) -> list[ToolEvent]:
    rnd = random.Random(seed)
    events, i, t = [], 0, 1_700_000_000.0
    for c in range(n_conversations):
        conv = f"c{c}"
        for turn in range(rnd.randint(2, 6)):
            tid = f"{conv}-t{turn}"
            if c % 5 == 0:  # the routine: 1 in 5 conversations
                q = f"incident {c}"
                hit = f"https://status.example/{c}"
                for tool, args, res in (
                    ("web_search", {"query": q}, {"results": [{"url": hit}]}),
                    ("web_extract", {"urls": [hit]}, {"content": f"report {c}"}),
                    ("write_file", {"path": f"notes/{c}.md", "content": f"report {c}"}, {"ok": True}),
                ):
                    i += 1
                    t += 2
                    events.append(ToolEvent(i, conv, f"s{c}", tid, tool, args, json.dumps(res), "ok", 50, t))
            for _ in range(rnd.randint(3, 12)):  # unrelated noise
                i += 1
                t += rnd.randint(1, 20)
                tool = rnd.choice(TOOLS)
                events.append(ToolEvent(i, conv, f"s{c}", tid, tool, {"x": rnd.randint(0, 10_000)},
                                        json.dumps({"v": rnd.random()}), "ok", 30, t))
    return events


def test_mining_twenty_thousand_events_is_fast_and_finds_the_routine():
    events = _history(600)
    assert len(events) > 15_000
    t0 = time.perf_counter()
    specs, _ = mine(events, MinerConfig(min_support=3))
    elapsed = time.perf_counter() - t0
    routine = [s for s in specs if [st.tool for st in s.steps] == ["web_search", "web_extract", "write_file"]]
    assert routine, [[st.tool for st in s.steps] for s in specs]
    assert routine[0].evidence.support == 120
    assert routine[0].evidence.fit_rate == 1.0
    # Random sequences of the same tools recur by chance; none of them may be proposed.
    assert len(specs) == 1, [[st.tool for st in s.steps] for s in specs]
    assert elapsed < 30, f"mining took {elapsed:.1f}s"
