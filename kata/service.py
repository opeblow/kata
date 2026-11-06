"""Kata lifecycle: mine, propose, approve, reject, disable, enable, roll back.

``KataService`` owns all state transitions and is shared by the slash command, the CLI
and the dashboard API, so the rules are identical everywhere. Nothing in this module
registers tools; that is the runtime's job (``runtime.py``) and it only ever registers
katas whose status is ``active``, which only ``approve`` can set.
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .miner import (
    MinerConfig,
    _one_per_conversation,
    _support,
    compile_candidate,
    fits,
    mine,
    occurrences_for,
    settled,
)
from .models import Effect, KataSpec, Status, ToolEvent
from .store import Store
from .validator import validate

NAME_RE = re.compile(r"^kata_[a-z0-9_]{1,48}$")
Fingerprint = Callable[[str], str | None]


class KataError(Exception):
    pass


@dataclass
class MineReport:
    proposed: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    drift: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    events: int = 0

    def to_json(self) -> dict:
        return {"proposed": self.proposed, "updated": self.updated, "rejected": self.rejected,
                "drift": self.drift, "pending": self.pending, "events": self.events}


def _contains(longer: tuple, shorter: tuple) -> bool:
    n = len(shorter)
    return any(longer[i:i + n] == shorter for i in range(len(longer) - n + 1))


def _shape(spec: KataSpec) -> str:
    """What a revision must change to be worth a new version: steps, params or support."""
    d = spec.to_json()
    return json.dumps([d["steps"], d["params"], d["evidence"]["support"]], sort_keys=True)


class KataService:
    def __init__(self, store: Store, cfg: MinerConfig | None = None, fingerprint: Fingerprint | None = None,
                 retention_days: int = 30, max_events: int = 50_000):
        self.store = store
        self.cfg = cfg or MinerConfig()
        self.fingerprint = fingerprint
        self.retention_days = retention_days
        self.max_events = max_events
        self._mine_lock = threading.Lock()

    # -- helpers -----------------------------------------------------------
    def _resolve(self, ref: str | int) -> dict:
        k = None
        if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
            k = self.store.kata(int(ref))
        if k is None and isinstance(ref, str):
            k = self.store.kata_by_name(ref) or self.store.kata_by_name(f"kata_{ref}")
        if k is None:
            raise KataError(f"no kata named or numbered `{ref}`")
        return k

    def spec(self, kata_id: int, version: int | None = None) -> tuple[KataSpec, int, dict]:
        v = self.store.version(kata_id, version) if version else self.store.latest_version(kata_id)
        if v is None:
            raise KataError("kata has no versions")
        return KataSpec.from_json(v["spec"]), v["version"], v["validation"]

    def active_spec(self, kata_id: int) -> tuple[KataSpec, int] | None:
        k = self.store.kata(kata_id)
        if not k or k["status"] != Status.ACTIVE.value or not k["active_version"]:
            return None
        spec, version, _ = self.spec(kata_id, k["active_version"])
        return spec, version

    def examples(self, spec: KataSpec) -> list[list[ToolEvent]]:
        out = []
        for ids in spec.evidence.example_event_ids:
            evs = self.store.events_by_ids(ids)
            if len(evs) == len(spec.steps):
                out.append(evs)
        return out

    def _unique_name(self, base: str, kata_id: int | None = None) -> str:
        name, n = base, 2
        while (k := self.store.kata_by_name(name)) and k["id"] != kata_id:
            name, n = f"{base}_{n}", n + 1
        return name

    # -- mining ------------------------------------------------------------
    def mine(self) -> MineReport:
        with self._mine_lock:  # background (session end) and explicit runs must not interleave
            return self._mine()

    def _mine(self) -> MineReport:
        report = MineReport()
        since = time.time() - self.retention_days * 86400
        self.store.prune(since)
        events = settled(self.store.events(since, limit=self.max_events), self.store.ended_sessions(), time.time())
        report.events = len(events)
        specs, rejections = mine(events, self.cfg, api_lookup=self.store.api_calls_between,
                                 fingerprint=self.fingerprint)
        report.rejected = [{"tools": r.tools, "reason": r.reason} for r in rejections]
        rejected_seqs = [tuple(s.tool for s in self.spec(k["id"])[0].steps)
                         for k in self.store.katas(Status.REJECTED.value)]
        for spec in specs:
            seq = tuple(s.tool for s in spec.steps)
            if any(_contains(r, seq) for r in rejected_seqs) and not self.store.kata_by_signature(spec.signature):
                report.rejected.append({"tools": list(seq), "reason": "part of a routine you rejected"})
                continue
            existing = self.store.kata_by_signature(spec.signature)
            if existing is None:
                spec.name = self._unique_name(spec.name)
                validation = validate(spec, self.examples_from(events, spec), fingerprint=self.fingerprint)
                if not validation.passed:
                    failed = [c.name for c in validation.checks if not c.passed]
                    report.rejected.append({"tools": spec.tools, "reason": f"failed validation: {', '.join(failed)}"})
                    continue
                self.store.add_candidate(spec, validation.to_json())
                report.proposed.append(spec.name)
            elif existing["status"] == Status.STALE.value:
                # A stale kata is revised only from what the agent did after the approved version.
                report.pending.append(existing["name"])
                if self._revise_from_recent(existing, events):
                    report.updated.append(existing["name"])
            elif existing["status"] == Status.CANDIDATE.value:
                report.pending.append(existing["name"])
                latest, _, _ = self.spec(existing["id"])
                if _shape(latest) != _shape(spec):
                    spec.name = existing["name"]
                    validation = validate(spec, self.examples_from(events, spec), fingerprint=self.fingerprint)
                    self.store.add_version(existing["id"], spec, validation.to_json())
                    self.store.audit(existing["id"], "revised", "kata",
                                     f"support={spec.evidence.support} confidence={spec.evidence.confidence:.2f}")
                    report.updated.append(existing["name"])
        report.drift = self.detect_drift(events)
        self.store.bump_generation()
        return report

    def examples_from(self, events: list[ToolEvent], spec: KataSpec) -> list[list[ToolEvent]]:
        wanted = {tuple(ids) for ids in spec.evidence.example_event_ids}
        occ = occurrences_for(events, spec.signature, self.cfg.max_steps, self.cfg.max_gap_s)
        return [o for o in occ if tuple(e.id for e in o) in wanted]

    def _approved_at(self, k: dict) -> float:
        v = self.store.version(k["id"], k["active_version"]) if k["active_version"] else None
        return (v or {}).get("approved_at") or k["created_at"]

    def _recent(self, k: dict, spec: KataSpec, events: list[ToolEvent]) -> list[list[ToolEvent]]:
        since = self._approved_at(k)
        occ = occurrences_for(events, spec.signature, self.cfg.max_steps, self.cfg.max_gap_s)
        return [o for o in occ if o[0].ended_at > since]

    def _revise_from_recent(self, k: dict, events: list[ToolEvent]) -> bool:
        """Prepare a new version from manual runs made after the approved one. Returns True if added."""
        spec, _, _ = self.spec(k["id"], k["active_version"] or None)
        fresh = _one_per_conversation(self._recent(k, spec, events))
        if _support(fresh) < 2:
            return False
        new, _ = compile_candidate(fresh, support=_support(fresh), api_lookup=self.store.api_calls_between,
                                   fingerprint=self.fingerprint)
        if new is None or _shape(new) == _shape(self.spec(k["id"])[0]):
            return False
        new.name = k["name"]
        rep = validate(new, fresh, fingerprint=self.fingerprint)
        version = self.store.add_version(k["id"], new, rep.to_json())
        self.store.audit(k["id"], "revised", "kata", f"v{version} prepared from {len(fresh)} recent manual runs")
        return True

    def detect_drift(self, events: list[ToolEvent]) -> list[str]:
        """Flag active katas whose routine the agent now performs differently by hand."""
        flagged = []
        for k in self.store.katas(Status.ACTIVE.value):
            spec, version, _ = self.spec(k["id"], k["active_version"])
            recent = self._recent(k, spec, events)
            if len(recent) < 2:
                continue
            types = {p.name: p.type for p in spec.params}
            misfits = [o for o in recent if fits([s.args for s in spec.steps], types, o) is None]
            if len(misfits) >= 2 and len(misfits) * 2 >= len(recent):
                self.store.set_status(k["id"], Status.STALE, actor="kata",
                                      reason=f"{len(misfits)} of {len(recent)} recent manual runs no longer fit v{version}")
                self._revise_from_recent(self.store.kata(k["id"]), events)
                flagged.append(k["name"])
        return flagged

    # -- decisions ---------------------------------------------------------
    def approve(self, ref: str | int, *, name: str | None = None, version: int | None = None,
                actor: str = "user") -> dict:
        k = self._resolve(ref)
        if k["status"] in (Status.ACTIVE.value,) and not version:
            raise KataError(f"`{k['name']}` is already active (v{k['active_version']})")
        if k["status"] == Status.REJECTED.value:
            raise KataError(f"`{k['name']}` was rejected; it will not be activated")
        spec, v, _ = self.spec(k["id"], version)
        if name:
            name = name if name.startswith("kata_") else f"kata_{name}"
            if not NAME_RE.match(name):
                raise KataError("names must match kata_[a-z0-9_] and be at most 53 characters")
            if (other := self.store.kata_by_name(name)) and other["id"] != k["id"]:
                raise KataError(f"`{name}` is already taken")
        changed = False
        if self.fingerprint:
            for s in spec.steps:
                if not s.schema_fingerprint and (fp := self.fingerprint(s.tool)):
                    s.schema_fingerprint, changed = fp, True
        report = validate(spec, self.examples(spec), fingerprint=self.fingerprint)
        if not report.passed:
            failed = "; ".join(f"{c.name}: {c.detail}" for c in report.checks if not c.passed)
            self.store.audit(k["id"], "approval_refused", actor, failed)
            raise KataError(f"validation failed, not activated: {failed}")
        if name and name != k["name"]:
            self.store.rename(k["id"], name)
            spec.name, changed = name, True
        if changed:
            v = self.store.add_version(k["id"], spec, report.to_json())
        self.store.mark_approved(k["id"], v, actor)
        self.store.reset_failures(k["id"])
        return {"name": spec.name, "version": v, "validation": report.to_json()}

    def reject(self, ref: str | int, reason: str = "", actor: str = "user") -> dict:
        k = self._resolve(ref)
        if k["status"] == Status.ACTIVE.value:
            raise KataError(f"`{k['name']}` is active; disable it instead")
        self.store.set_status(k["id"], Status.REJECTED, actor=actor, reason=reason or "rejected")
        return {"name": k["name"], "status": "rejected"}

    def disable(self, ref: str | int, reason: str = "", actor: str = "user") -> dict:
        k = self._resolve(ref)
        self.store.set_status(k["id"], Status.DISABLED, actor=actor, reason=reason or "disabled")
        return {"name": k["name"], "status": "disabled"}

    def enable(self, ref: str | int, actor: str = "user") -> dict:
        k = self._resolve(ref)
        if k["status"] != Status.DISABLED.value or not k["active_version"]:
            raise KataError(f"`{k['name']}` is {k['status']}; only disabled katas that were approved can be enabled")
        return self.approve(k["id"], version=k["active_version"], actor=actor)

    def rollback(self, ref: str | int, version: int | None = None, actor: str = "user") -> dict:
        k = self._resolve(ref)
        approved = [v["version"] for v in self.store.versions(k["id"]) if v["approved_at"]]
        current = k["active_version"]
        if version is None:
            older = [v for v in approved if current is None or v < current]
            if not older:
                raise KataError(f"`{k['name']}` has no earlier approved version")
            version = older[-1]
        if version not in approved:
            raise KataError(f"v{version} of `{k['name']}` was never approved")
        report = validate(self.spec(k["id"], version)[0], self.examples(self.spec(k["id"], version)[0]),
                          fingerprint=self.fingerprint)
        if not report.passed:
            raise KataError(f"v{version} no longer validates: "
                            + "; ".join(c.detail for c in report.checks if not c.passed))
        self.store.set_status(k["id"], Status.ACTIVE, actor=actor, reason=f"rolled back to v{version}",
                              active_version=version)
        return {"name": k["name"], "version": version}

    # -- runtime feedback --------------------------------------------------
    def record_run(self, kata_id: int, version: int, conversation_id: str, run: dict, started_at: float,
                   suspend_after: int = 3) -> None:
        self.store.add_run(kata_id=kata_id, version=version, conversation_id=conversation_id, ok=run["ok"],
                           duration_ms=run["duration_ms"],
                           steps_run=sum(1 for s in run["steps"] if s["status"] != "skipped"),
                           failed_step=run.get("failed_step"), error=run.get("error"), trace=run,
                           started_at=started_at)
        if run["ok"]:
            self.store.reset_failures(kata_id)
            return
        if run.get("error", "").startswith("invalid arguments"):
            return  # caller error, not a broken routine
        if self.store.record_failure(kata_id) >= suspend_after:
            self.store.set_status(kata_id, Status.SUSPENDED, actor="kata",
                                  reason=f"{suspend_after} consecutive failed runs; last: {run.get('error')}")

    # -- reporting ---------------------------------------------------------
    def overview(self) -> dict:
        katas = self.store.katas()
        by_status: dict[str, int] = {}
        for k in katas:
            by_status[k["status"]] = by_status.get(k["status"], 0) + 1
        saved_calls = saved_trips = saved_tokens = 0.0
        runs_total = 0
        for k in katas:
            if not k["active_version"]:
                continue
            spec, _, _ = self.spec(k["id"], k["active_version"])
            stats = self.store.run_stats(k["id"])
            runs_total += stats["ok"]
            b = spec.evidence.baseline
            saved_calls += stats["ok"] * max(0, b.get("tool_calls", 0) - 1)
            saved_trips += stats["ok"] * (b.get("avg_model_round_trips") or 0)
            saved_tokens += stats["ok"] * (b.get("avg_model_tokens") or 0)
        return {"events": self.store.event_count(), "katas": by_status, "successful_runs": runs_total,
                "tool_calls_saved": int(saved_calls), "model_round_trips_saved": round(saved_trips, 1),
                "model_tokens_saved": int(saved_tokens)}

    def detail(self, ref: str | int) -> dict:
        k = self._resolve(ref)
        spec, version, validation = self.spec(k["id"])
        active = self.active_spec(k["id"])
        return {"kata": k, "latest_version": version, "spec": spec.to_json(), "validation": validation,
                "max_effect": spec.max_effect.value, "input_schema": spec.input_schema(),
                "requires_approval_per_call": spec.max_effect.rank >= Effect.EXTERNAL.rank,
                "active_version": active[1] if active else None,
                "versions": [{"version": v["version"], "created_at": v["created_at"],
                              "approved_at": v["approved_at"], "passed": v["validation"].get("passed")}
                             for v in self.store.versions(k["id"])],
                "stats": self.store.run_stats(k["id"]), "runs": self.store.runs(k["id"], 20),
                "audit": self.store.audit_log(k["id"], 50)}
