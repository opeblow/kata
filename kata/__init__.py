"""Kata: compiled routines for Hermes Agent.

Kata observes the tool calls your agent makes, finds multi-step routines it repeats
across conversations, and proposes each one as a single deterministic tool. Nothing is
registered until you review and approve it.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

__version__ = "0.1.0"

log = logging.getLogger("kata")


def open_store():
    """Open the plugin database in Hermes's per-profile plugin data directory."""
    import os

    from .store import Store
    override = os.environ.get("KATA_DB_PATH")  # development and the offline demo only
    if override:
        return Store.open_path(Path(override))
    try:
        from plugins.plugin_storage import plugin_db
    except ImportError:  # running outside Hermes (tests, scripts)
        return Store.open_path(Path.home() / ".hermes" / "plugin-data" / "kata" / "kata.db")
    # Opened on first use, so loading the plugin never creates or locks files.
    return Store.lazy(lambda: plugin_db("kata", "kata.db"))


def register(ctx: Any) -> None:
    """Hermes entrypoint."""
    install(ctx, open_store())


def install(ctx: Any, store: Any, fingerprint: Any = None) -> Any:
    """Wire Kata into a host. Split from ``register`` so tests can inject a store."""
    from . import commands
    from .runtime import KATA_LIST_SCHEMA, KataRuntime, schema_fingerprint

    rt = KataRuntime(ctx, store, fingerprint=fingerprint or schema_fingerprint)
    ctx.register_hook("post_tool_call", rt.on_post_tool_call)
    ctx.register_hook("post_api_request", rt.on_post_api_request)
    ctx.register_hook("pre_tool_call", rt.on_pre_tool_call)
    ctx.register_hook("pre_llm_call", rt.on_pre_llm_call)
    ctx.register_hook("on_session_start", rt.on_session_start)
    ctx.register_hook("on_session_end", rt.on_session_end)
    ctx.register_tool(name="kata_list", toolset="kata", schema=KATA_LIST_SCHEMA, handler=rt.kata_list_handler,
                      description="List approved katas and pending candidates.")
    if hasattr(ctx, "register_system_prompt_section"):
        try:
            ctx.register_system_prompt_section("kata.routines", rt.prompt_section, position="after_memory",
                                               max_chars=1500)
        except Exception:  # never block loading over an optional hint
            log.debug("kata: system prompt section not registered", exc_info=True)
    ctx.register_command("kata", lambda raw="": commands.handle(rt.service, raw, rt.sync),
                         description="Review, approve and manage compiled routines", args_hint="[subcommand]")
    try:
        ctx.register_cli_command("kata", "Review, approve and manage compiled routines", commands.setup_cli,
                                 commands.make_cli_handler(lambda: rt.service))
    except Exception:  # older hosts without CLI registration
        log.debug("kata: CLI command not registered", exc_info=True)
    rt.sync()        # register approved katas before Hermes snapshots the session's tools
    store.release()  # then let go of the file; it reopens on first use
    return rt
