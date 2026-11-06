"""Side-effect classification for tool calls.

Kata needs to know which steps it may retry, which ones need a human in the loop and
which permissions to show at approval time. Classification is deliberately
conservative: any tool or command Kata does not recognise is ``UNKNOWN`` and is
handled like an external, irreversible action.
"""
from __future__ import annotations

import re
import shlex

from .models import Effect

READ_TOOLS = {
    "read_file", "search_files", "web_search", "web_extract", "x_search", "session_search",
    "skill_view", "skills_list", "vision_analyze", "video_analyze", "read_terminal",
    "read_window_below", "feishu_doc_read", "todo_list", "kata_list",
}
LOCAL_WRITE_TOOLS = {"write_file", "patch", "memory", "todo"}
EXTERNAL_TOOLS = {
    "send_message", "delegate_task", "cronjob_manage", "browser_cdp", "browser_exec",
    "browser_vault", "computer_use", "image_generate", "video_generate", "text_to_speech",
    "skill_manage", "process_manage", "execute_code", "react_to_message",
}
# Tools that only make sense inside a live conversation and must never be compiled.
UNCOMPILABLE_TOOLS = {"clarify", "gui_tour", "show_tip", "setup_beat", "focus_pane", "set_theme"}

# Shell commands are classified with an allowlist. Kata never tries to recognise harmful
# commands: anything not listed here is UNKNOWN, which is handled exactly like an external,
# irreversible action (approval on every call, never retried).
_READ_COMMANDS = {
    "ls", "cat", "head", "tail", "wc", "grep", "rg", "find", "pwd", "echo", "stat", "du", "df",
    "which", "whoami", "date", "tree", "file", "sort", "uniq", "cut", "jq", "diff", "uname",
    "ps", "uptime", "hostname", "md5sum", "sha256sum",
}
# Flags that turn an otherwise read-only program into one that writes or runs other programs.
_WRITE_FLAGS = {
    "find": {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"},
    "sort": {"-o", "--output"},
}
_READ_SUBCOMMANDS = {
    "git": {"status", "log", "diff", "show", "rev-parse", "ls-files", "blame", "describe", "shortlog"},
    "docker": {"ps", "images", "logs", "inspect", "version", "info"},
    "kubectl": {"get", "describe", "logs", "version"},
    "npm": {"ls", "list", "view", "outdated", "--version"},
    "pip": {"list", "show", "freeze", "--version"},
    "python": {"--version", "-V"},
    "node": {"--version", "-v"},
}
_GIT_LISTING = {"branch", "tag", "remote"}               # read only when they only list
_LISTING_FLAGS = {"-a", "-r", "-l", "-v", "-vv", "--list", "--all", "--verbose"}
_GH_READ = {("pr", "list"), ("pr", "view"), ("pr", "status"), ("pr", "diff"), ("pr", "checks"),
            ("issue", "list"), ("issue", "view"), ("issue", "status"), ("repo", "view"),
            ("run", "list"), ("run", "view")}
_GIT_LOCAL_WRITE = {"add", "commit", "checkout", "switch", "stash", "restore", "merge", "rebase",
                    "fetch", "pull", "init"}
_CHECKERS = {"pytest", "ruff", "mypy", "tsc", "eslint", "prettier", "black", "flake8"}
_CHECKER_WRITE_FLAGS = {"--fix", "-w", "--write", "--in-place"}
_SHELL_META = re.compile(r"[;&|`$<>]")


def _classify_command(command: str) -> tuple[Effect, str]:
    if not command.strip():
        return Effect.UNKNOWN, "empty command"
    if _SHELL_META.search(command):
        return Effect.UNKNOWN, "compound shell command (pipes, redirects or substitution)"
    try:
        argv = shlex.split(command)
    except ValueError:
        return Effect.UNKNOWN, "command could not be parsed"
    prog = argv[0].rsplit("/", 1)[-1]
    sub = argv[1] if len(argv) > 1 else ""
    rest = argv[2:]
    if prog in _READ_COMMANDS:
        if _WRITE_FLAGS.get(prog, set()) & set(argv[1:]):
            return Effect.UNKNOWN, f"`{prog}` with a flag that writes or runs programs"
        return Effect.READ, f"`{prog}` only reads"
    if prog == "gh":
        verb = rest[0] if rest else ""
        if (sub, verb) in _GH_READ:
            return Effect.READ, f"`gh {sub} {verb}` only reads"
        return Effect.EXTERNAL, f"`gh {sub} {verb}`".replace("  ", " ").strip() + " can change remote state"
    if prog == "git" and sub in _GIT_LISTING and set(rest) <= _LISTING_FLAGS:
        return Effect.READ, f"`git {sub}` only lists"
    if prog in _READ_SUBCOMMANDS and sub in _READ_SUBCOMMANDS[prog]:
        return Effect.READ, f"`{prog} {sub}` only reads"
    if prog == "git" and sub in _GIT_LOCAL_WRITE:
        return Effect.LOCAL_WRITE, f"`git {sub}` changes the local repository"
    if prog in {"mkdir", "touch", "cp"}:
        return Effect.LOCAL_WRITE, f"`{prog}` writes local files"
    if prog in _CHECKERS:
        if _CHECKER_WRITE_FLAGS & set(argv[1:]):
            return Effect.LOCAL_WRITE, f"`{prog}` rewrites files"
        return Effect.READ, f"`{prog}` checks without writing"
    return Effect.UNKNOWN, f"`{prog}` is not a command Kata recognises; treated as external"


def classify(tool: str, args: dict | None) -> tuple[Effect, str]:
    """Classify one concrete tool call."""
    args = args or {}
    if tool in READ_TOOLS:
        return Effect.READ, f"`{tool}` only reads"
    if tool in LOCAL_WRITE_TOOLS:
        return Effect.LOCAL_WRITE, f"`{tool}` changes local files or state"
    if tool in EXTERNAL_TOOLS:
        return Effect.EXTERNAL, f"`{tool}` acts outside the conversation"
    if tool == "terminal":
        cmd = args.get("command")
        if not isinstance(cmd, str):
            return Effect.UNKNOWN, "terminal call without a literal command"
        if args.get("background"):
            return Effect.EXTERNAL, "background terminal process"
        return _classify_command(cmd)
    if tool.startswith("browser_"):
        return Effect.EXTERNAL, "browser actions can submit forms"
    return Effect.UNKNOWN, f"`{tool}` is not a tool Kata recognises"


def classify_occurrences(tool: str, arg_samples: list[dict]) -> tuple[Effect, str]:
    """The riskiest classification across every observed call of one step."""
    worst: tuple[Effect, str] = (Effect.READ, "")
    for a in arg_samples:
        e = classify(tool, a)
        if e[0].rank > worst[0].rank or not worst[1]:
            worst = e
    return worst


def compilable(tool: str) -> bool:
    return tool not in UNCOMPILABLE_TOOLS and not tool.startswith("kata_")
