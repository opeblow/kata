"""Static mirror of the Hermes plugin catalog admission checks (``hermes plugins validate``).

It does not replace running Hermes's validator; it keeps the rules we know about from
regressing between runs of it. Sources: hermes_cli/plugin_validate*.py and
plugin-catalog/README.md in NousResearch/hermes-agent.
"""
from __future__ import annotations

import ast
import inspect
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "kata"

# hermes_cli.plugins.VALID_HOOKS (subset Kata may use; all names verified in source).
VALID_HOOKS = {
    "pre_tool_call", "post_tool_call", "transform_terminal_output", "transform_tool_result",
    "transform_llm_output", "pre_llm_call", "post_llm_call", "on_stream_start", "on_stream_delta",
    "on_stream_end", "on_interim_message", "pre_verify", "pre_api_request", "post_api_request",
    "api_request_error", "on_session_start", "on_session_end", "on_session_finalize", "on_session_reset",
}
# hermes_cli/data/plugin_api.json module entries Kata imports.
PUBLIC_MODULES = {"plugins.plugin_storage", "tools.registry"}
HERMES_TOP_LEVEL = {"agent", "hermes_cli", "tools", "plugins", "gateway", "model_tools", "run_agent",
                    "hermes_constants", "hermes_state", "utils", "pm"}
# Terms the install-time scanner (tools/plugin_guard + skills_guard) rates high or critical
# in code. Kata's own source must not contain them, even inside lists or regexes.
SCANNER_TERMS = [r"\bmkfs\b", r"\bprintenv\b", r"\bsudo\b", r"curl\s.*\|\s*(ba)?sh",
                 r"\bos\.system\b", r"\beval\(", r"\bexec\(", r"\bsubprocess\b", r"base64\.b64decode"]


def _manifest() -> dict:
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load((PLUGIN / "plugin.yaml").read_text(encoding="utf-8"))


class Recorder:
    def __init__(self):
        self.tools, self.hooks, self.commands, self.cli = [], [], [], []
        self.callbacks = []

    def register_tool(self, name, toolset, schema, handler, **_):
        self.tools.append(name)

    def register_hook(self, name, cb, **_):
        self.hooks.append(name)
        self.callbacks.append((name, cb))

    def register_command(self, name, *a, **k):
        self.commands.append(name)

    def register_cli_command(self, name, *a, **k):
        self.cli.append(name)

    def get_config(self, key, default=None):
        return default


def _register(tmp_path) -> Recorder:
    from kata import install
    from kata.store import Store
    rec = Recorder()
    store = Store.open_path(tmp_path / "probe.db")
    install(rec, store, fingerprint=lambda t: None)
    store.close()
    return rec


def test_manifest_required_fields():
    m = _manifest()
    for f in ("name", "version", "description"):
        assert m.get(f), f
    assert re.fullmatch(r">=\d+\.\d+(\.\d+)?", m["requires_hermes"])
    for key, spec in m["config_schema"].items():
        assert spec["type"] in {"str", "int", "float", "bool", "list", "dict", "secret"}, key
        assert "default" in spec and spec.get("description"), key
    dash = json.loads((PLUGIN / "dashboard" / "manifest.json").read_text(encoding="utf-8"))
    assert dash["name"] == m["name"] and dash["version"] == m["version"]
    assert dash["tab"]["path"].startswith("/")
    import kata
    assert kata.__version__ == m["version"]


def test_declared_capabilities_match_registrations(tmp_path):
    m = _manifest()
    rec = _register(tmp_path)
    assert set(rec.tools) == set(m["provides_tools"])
    assert set(rec.hooks) == set(m["provides_hooks"])
    assert set(rec.hooks) <= VALID_HOOKS
    assert rec.commands == ["kata"] and rec.cli == ["kata"]


def test_hook_callbacks_accept_kwargs(tmp_path):
    for name, cb in _register(tmp_path).callbacks:
        params = inspect.signature(cb).parameters.values()
        assert any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params), name


def _py_files():
    return [p for p in PLUGIN.rglob("*.py") if "__pycache__" not in p.parts]


def test_only_public_hermes_imports_and_no_core_rebinds():
    for path in _py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                top = node.module.split(".")[0]
                if top in HERMES_TOP_LEVEL:
                    assert node.module in PUBLIC_MODULES, f"{path.name}: {node.module}"
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in HERMES_TOP_LEVEL, f"{path.name}: {alias.name}"
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") in {"setattr", "delattr"}:
                raise AssertionError(f"{path.name}: {ast.unparse(node)}")
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Subscript) and ast.unparse(t.value) == "sys.modules":
                        assert not isinstance(t.slice, ast.Constant) or \
                            t.slice.value.split(".")[0] not in HERMES_TOP_LEVEL


def test_no_scanner_flagged_terms_in_plugin_source():
    hits = []
    for path in [*_py_files(), *PLUGIN.rglob("*.js"), *PLUGIN.rglob("*.md"), *PLUGIN.rglob("*.yaml")]:
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for term in SCANNER_TERMS:
                if re.search(term, line):
                    hits.append(f"{path.relative_to(ROOT)}:{n}: {term}")
    assert not hits, hits


def test_no_compiled_artifacts_shipped():
    assert not [p for p in PLUGIN.rglob("*") if p.suffix in {".pyc", ".pyo", ".so", ".dll", ".exe", ".whl", ".zip"}
                and "__pycache__" not in p.parts]


def test_catalog_entry_matches_manifest():
    yaml = pytest.importorskip("yaml")
    entry = yaml.safe_load((ROOT / "docs" / "catalog-entry.yaml").read_text(encoding="utf-8"))
    m = _manifest()
    assert entry["name"] == m["name"] and entry["version"] == m["version"]
    assert entry["requires_hermes"] == m["requires_hermes"]
    assert entry["subdir"] == "kata" and entry["tier"] == "community"
    assert set(entry["capabilities"]["provides_tools"]) == set(m["provides_tools"])
    assert set(entry["capabilities"]["provides_hooks"]) == set(m["provides_hooks"])
    assert "Disclosure" in entry["description"]
