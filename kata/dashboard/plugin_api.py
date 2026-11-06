"""Kata dashboard API, mounted by Hermes at ``/api/plugins/kata/`` behind the dashboard auth gate.

Approval from the dashboard goes through the same ``KataService.approve`` as ``/kata``:
validation re-runs, the decision is audited, and the agent process registers the tool
on its next turn (it watches the store's generation counter); the model sees it from the next session.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()

_PKG = "kata_dashboard_core"


def _core():
    """Load the plugin package that sits next to this file, under a private module name."""
    if _PKG in sys.modules:
        return sys.modules[_PKG]
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(_PKG, root / "__init__.py", submodule_search_locations=[str(root)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_PKG] = mod
    spec.loader.exec_module(mod)
    return mod


_service = None


def _svc():
    global _service
    if _service is None:
        core = _core()
        service_mod = importlib.import_module(f"{_PKG}.service")
        _service = service_mod.KataService(core.open_store())
    return _service


async def _run(fn, *a, **k) -> Any:
    try:
        return await asyncio.to_thread(fn, *a, **k)
    except Exception as e:
        if type(e).__name__ == "KataError":
            raise HTTPException(status_code=409, detail=str(e)) from e
        raise


class ApproveBody(BaseModel):
    name: str | None = None
    version: int | None = None


class ReasonBody(BaseModel):
    reason: str = ""


class RollbackBody(BaseModel):
    version: int | None = None


@router.get("/overview")
async def overview() -> dict:
    return await _run(lambda: _svc().overview())


@router.get("/katas")
async def katas() -> dict:
    def build():
        svc = _svc()
        out = []
        for k in svc.store.katas():
            spec, version, validation = svc.spec(k["id"])
            out.append({**k, "latest_version": version, "steps": [s.tool for s in spec.steps],
                        "params": [p.name for p in spec.params], "effect": spec.max_effect.value,
                        "support": spec.evidence.support, "confidence": spec.evidence.confidence,
                        "validation_passed": validation.get("passed"), "stats": svc.store.run_stats(k["id"])})
        return {"katas": out}
    return await _run(build)


@router.get("/katas/{kata_id}")
async def detail(kata_id: int) -> dict:
    return await _run(lambda: _svc().detail(kata_id))


@router.post("/katas/{kata_id}/approve")
async def approve(kata_id: int, body: ApproveBody) -> dict:
    return await _run(lambda: _svc().approve(kata_id, name=body.name or None, version=body.version,
                                             actor="user:dashboard"))


@router.post("/katas/{kata_id}/reject")
async def reject(kata_id: int, body: ReasonBody) -> dict:
    return await _run(lambda: _svc().reject(kata_id, body.reason, actor="user:dashboard"))


@router.post("/katas/{kata_id}/disable")
async def disable(kata_id: int, body: ReasonBody) -> dict:
    return await _run(lambda: _svc().disable(kata_id, body.reason, actor="user:dashboard"))


@router.post("/katas/{kata_id}/enable")
async def enable(kata_id: int) -> dict:
    return await _run(lambda: _svc().enable(kata_id, actor="user:dashboard"))


@router.post("/katas/{kata_id}/rollback")
async def rollback(kata_id: int, body: RollbackBody) -> dict:
    return await _run(lambda: _svc().rollback(kata_id, body.version, actor="user:dashboard"))


@router.post("/mine")
async def mine() -> dict:
    return await _run(lambda: _svc().mine().to_json())


@router.get("/audit")
async def audit() -> dict:
    return await _run(lambda: {"audit": _svc().store.audit_log(None, 100)})
