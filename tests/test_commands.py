"""The /kata command surface, end to end through the registered handler."""
from __future__ import annotations

import json

from conftest import USERS, triage_by_hand


def kata(host, raw=""):
    return host.commands["kata"](raw)


def test_empty_state_and_help(host):
    out = kata(host)
    assert "No katas yet" in out and "3 separate conversations" in out
    assert "/kata review" in kata(host, "help")
    assert "/kata review" in kata(host, "nonsense-subcommand")
    assert "Could not parse" in kata(host, 'review "unterminated')
    assert "No routine has repeated" in kata(host, "mine")


def test_list_review_runs_stats_and_errors(host):
    for u in USERS[:4]:
        triage_by_hand(host, u)
    out = kata(host, "mine")
    assert "Waiting for your review" in out or "New proposals" in out
    listing = kata(host, "list")
    assert "candidate" in listing and "kata_search_read_terminal" in listing

    assert "no kata named" in kata(host, "review does-not-exist")
    assert "names must match" in kata(host, "approve 1 as Bad-Name")
    assert "disable it instead" not in kata(host, "approve 1 as triage")
    assert "already active" in kata(host, "approve 1")
    assert "disable it instead" in kata(host, "reject 1")
    assert "No runs yet" in kata(host, "runs triage")

    json.loads(host.call(host.new_turn(), "kata_triage", {"pattern": "erin"}))
    runs = kata(host, "runs kata_triage")
    assert "ok" in runs and "search_files:ok" in runs
    stats = json.loads(kata(host, "stats"))
    assert stats["successful_runs"] == 1 and stats["tool_calls_saved"] == 2

    assert "no earlier approved version" in kata(host, "rollback triage")
    assert "Disabled" in kata(host, "disable triage")
    assert "only disabled katas" not in kata(host, "enable triage")
    assert "only disabled katas" in kata(host, "enable triage")


def test_kata_list_tool_for_the_agent(host):
    for u in USERS[:4]:
        triage_by_hand(host, u)
    host.runtime.service.mine()
    out = json.loads(host.dispatch_tool("kata_list", {}))
    assert out["katas"][0]["status"] == "candidate"
    assert out["katas"][0]["steps"] == ["search_files", "read_file", "terminal"]


def test_recording_can_be_paused(host):
    host.config["record"] = False
    triage_by_hand(host, "alice")
    assert host.runtime.store.event_count() == 0
