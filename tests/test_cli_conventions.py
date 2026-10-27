# O3DE CLI - Conventions every command must follow
# SPDX-License-Identifier: Apache-2.0 OR MIT

"""Cross-cutting CLI conventions, checked over the whole command surface."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

COMMANDS_DIR = Path(__file__).resolve().parent.parent / "o3de_cli" / "commands"


def _emit_error_then_bare_return(tree: ast.AST) -> list[int]:
    """Line numbers where an emit_error(...) statement is followed by a bare return."""
    hits: list[int] = []
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        for block in (body, getattr(node, "orelse", None)):
            if not isinstance(block, list):
                continue
            for stmt, nxt in zip(block, block[1:]):
                is_emit = (
                    isinstance(stmt, ast.Expr)
                    and isinstance(stmt.value, ast.Call)
                    and getattr(stmt.value.func, "id", None) == "emit_error"
                )
                if is_emit and isinstance(nxt, ast.Return) and nxt.value is None:
                    hits.append(stmt.lineno)
    return hits


def test_json_errors_exit_nonzero():
    """A --json error must exit 1, or scripts and the MCP server see success.

    emit_error() only prints; the command still has to exit. A bare return
    after it exits 0.
    """
    offenders = []
    for path in sorted(COMMANDS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenders += [f"{path.name}:{ln}" for ln in _emit_error_then_bare_return(tree)]
    assert offenders == [], "emit_error() followed by a bare return: " + ", ".join(offenders)


def test_config_get_missing_key_json_exits_1():
    from o3de_cli.commands.config import config

    cfg = MagicMock()
    cfg.get.return_value = None
    with patch("o3de_cli.core.config.get_config", return_value=cfg):
        result = CliRunner().invoke(config, ["get", "--json", "no.such.key"])
    assert result.exit_code == 1
    data = json.loads(result.stdout)
    assert data["status"] == "error"
    assert data["code"] == "E_KEY_NOT_FOUND"


def test_config_get_missing_key_text_exits_1():
    from o3de_cli.commands.config import config

    cfg = MagicMock()
    cfg.get.return_value = None
    with patch("o3de_cli.core.config.get_config", return_value=cfg):
        result = CliRunner().invoke(config, ["get", "no.such.key"])
    assert result.exit_code == 1
    assert "Key not found" in result.output


def test_mcp_server_reports_package_version():
    from o3de_cli import __version__
    from o3de_cli.mcp_server import _SERVER_INFO

    assert _SERVER_INFO == {"name": "o3de-cli", "version": __version__}


# ---------------------------------------------------------------------------
# The --json contract (o3de_cli/core/json_output.py)
#
# Success: stdout is one {"status": "ok", "data": ..., "warnings": [...]}
# document and the exit code is 0.  Error: stdout is one {"status":
# "error", "error": ..., "code": "E_..."} document and the exit code is 1.
# ---------------------------------------------------------------------------


def _calls(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            yield node


def _func_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _is_dumps(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and _func_name(node) == "dumps"


def test_no_hand_rolled_json_output_in_commands():
    """--json output goes through emit_response()/emit_error().

    console.print_json() can add ANSI colour codes, and click.echo(json.dumps(...))
    or console.print(json.dumps(...)) bypass the envelope.
    """
    offenders = []
    for path in sorted(COMMANDS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in _calls(tree):
            name = _func_name(call)
            if name == "print_json":
                offenders.append(f"{path.name}:{call.lineno} print_json")
            elif name in ("echo", "print", "emit") and call.args and _is_dumps(call.args[0]):
                offenders.append(f"{path.name}:{call.lineno} {name}(json.dumps(...))")
    assert offenders == [], "JSON written outside the envelope: " + ", ".join(offenders)


def test_json_errors_carry_an_error_code():
    """Every emit_error() in a command passes code="E_..." (or a variable)."""
    offenders = []
    for path in sorted(COMMANDS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in _calls(tree):
            if _func_name(call) != "emit_error":
                continue
            code = next((k.value for k in call.keywords if k.arg == "code"), None)
            literal_ok = (
                isinstance(code, ast.Constant)
                and isinstance(code.value, str)
                and code.value.startswith("E_")
            )
            if code is None or (isinstance(code, ast.Constant) and not literal_ok):
                offenders.append(f"{path.name}:{call.lineno}")
    assert offenders == [], "emit_error() without an E_* code: " + ", ".join(offenders)


def _emit_error_without_exit(tree: ast.AST) -> list[int]:
    """emit_error(...) statements whose next statement is not a raise.

    The statement after the last one of an if/else branch is the one after
    the whole if statement.
    """
    # (block, index) of every statement, and the statement owning each block
    where: dict[int, tuple[list, int]] = {}
    owner: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if not isinstance(block, list):
                continue
            owner[id(block)] = node
            for i, stmt in enumerate(block):
                where[id(stmt)] = (block, i)

    def next_stmt(stmt: ast.AST) -> ast.AST | None:
        while id(stmt) in where:
            block, i = where[id(stmt)]
            if i + 1 < len(block):
                return block[i + 1]
            parent = owner.get(id(block))
            if not isinstance(parent, (ast.If, ast.With)):
                return None
            stmt = parent
        return None

    hits = []
    for node in ast.walk(tree):
        is_emit = (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and _func_name(node.value) == "emit_error"
        )
        if is_emit and not isinstance(next_stmt(node), ast.Raise):
            hits.append(node.lineno)
    return hits


def test_json_errors_are_followed_by_an_exit():
    """emit_error() is followed by a raise (normally SystemExit(1)).

    Stricter than the bare-return check: falling off the end of the
    command after emit_error() also exits 0.
    """
    offenders = []
    for path in sorted(COMMANDS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenders += [f"{path.name}:{ln}" for ln in _emit_error_without_exit(tree)]
    assert offenders == [], "emit_error() not followed by raise: " + ", ".join(offenders)


# One cheap invocation per --json command: (argv, expected status, expected
# code).  Placeholders: {empty} an empty directory, {cmake} a directory
# holding only a CMakeLists.txt, {plan} a file that is not a valid plan,
# {new} a path that does not exist yet.
_JSON_CASES: list[tuple[list[str], str, str | None]] = [
    (["audit", "--json"], "ok", None),
    (["config", "get", "--json"], "ok", None),
    (["config", "get", "--json", "no.such.key"], "error", "E_KEY_NOT_FOUND"),
    (["config", "list", "--json"], "ok", None),
    (["config", "path", "--json"], "ok", None),
    (["config", "set", "--json", "some.key", "v"], "ok", None),
    (["config", "unset", "--json", "some.key"], "ok", None),
    (["deps", "list", "org.no.such", "--json"], "error", "E_NOT_FOUND"),
    (["deps", "tree", "--json"], "ok", None),
    (["deps", "tree", "org.no.such", "--json"], "error", "E_NOT_FOUND"),
    (["deps", "why", "org.a", "org.b", "--json"], "error", "E_NOT_FOUND"),
    (["engine", "create", "org.test.engine", "--path", "{new}", "--json"], "ok", None),
    (["engine", "create", "org.test.engine", "--path", "{empty}", "--json"], "error", "E_EXISTS"),
    (["engine", "list", "--json"], "ok", None),
    (["engine", "register", "{empty}", "--json"], "error", "E_NOT_AN_ENGINE"),
    (["engine", "unregister", "org.no.such", "--json"], "error", "E_NOT_FOUND"),
    (["gem", "info", "org.no.such", "--json"], "error", "E_NOT_FOUND"),
    (["gem", "list", "--json"], "ok", None),
    (["gem", "register", "{empty}", "--json"], "error", "E_NOT_A_GEM"),
    (["gem", "search", "nothing-matches", "--json"], "ok", None),
    (["gem", "unregister", "org.no.such", "--json"], "error", "E_NOT_FOUND"),
    (["list", "gems", "--json"], "ok", None),
    (["manifest", "add", "{empty}", "--json"], "error", "E_DETECT_FAILED"),
    (["manifest", "get", "--json"], "ok", None),
    (["manifest", "remove", "{new}", "--json"], "error", "E_NOT_FOUND"),
    (["manifest", "resolve", "--no-save", "--json"], "ok", None),
    (["manifest", "set", "nodots", "v", "--json"], "error", "E_INVALID_KEY"),
    (["manifest", "show", "--json"], "ok", None),
    (["manifest", "show", "--resolved", "--json"], "error", "E_NOT_FOUND"),
    (["object", "build", "org.x", "-w", "{new}", "--json"], "error", "E_WS_NOT_FOUND"),
    (["object", "compose", "--plan", "{plan}", "--json"], "error", "E_PLAN_UNREADABLE"),
    (["object", "hoist", "{empty}", "--json"], "error", "E_NOT_AN_OBJECT"),
    (["object", "install", "org.x", "-w", "{new}", "--json"], "error", "E_WS_NOT_FOUND"),
    (["object", "package", "org.x", "-w", "{new}", "--json"], "error", "E_WS_NOT_FOUND"),
    (["object", "split-platforms", "{empty}", "--json"], "error", "E_NOT_AN_OBJECT"),
    (["overlay", "list", "--json"], "ok", None),
    (
        ["project", "add", "gem", "org.x", "--path", "{empty}", "--json"],
        "error",
        "E_NO_PROJECT_JSON",
    ),
    (["project", "build", "--path", "{cmake}", "--dry-run", "--json"], "ok", None),
    (["project", "build", "--path", "{empty}", "--json"], "error", "E_NO_CMAKELISTS"),
    (["project", "init", "org.test.project", "--path", "{new}", "--json"], "ok", None),
    (["project", "list", "--json"], "ok", None),
    (["publish", "pack", "{empty}", "--json"], "error", "E_VALIDATION"),
    (["publish", "push", "{empty}", "--dry-run", "--json"], "error", "E_VALIDATION"),
    (["publish", "validate", "{empty}", "--json"], "error", "E_VALIDATION"),
    (["registry", "install", "org.no.such", "--json"], "error", "E_NOT_FOUND"),
    (["registry", "list", "engines", "--json"], "ok", None),
    (["registry", "list-remotes", "--json"], "ok", None),
    (["registry", "search", "nothing-matches", "--json"], "ok", None),
    (["registry", "uninstall", "org.no.such", "--json"], "error", "E_NOT_FOUND"),
    (["repo", "list", "--json"], "ok", None),
    (["search", "nothing-matches", "--json"], "ok", None),
    (["template", "create", "org.test.template", "--path", "{new}", "--json"], "ok", None),
    (["template", "info", "org.no.such", "--json"], "error", "E_NOT_FOUND"),
    (
        ["template", "instance", "org.no.such", "x", "--path", "{new}", "--json"],
        "error",
        "E_NOT_FOUND",
    ),
    (["template", "list", "--json"], "ok", None),
    (["workspace", "build", "{new}", "--json"], "error", "E_WS_NOT_FOUND"),
    (["workspace", "candidates", "{new}", "--json"], "error", "E_WS_NOT_FOUND"),
    (["workspace", "create", "ws", "--root", "{empty}", "--json"], "error", "E_NOT_AN_OBJECT"),
    (["workspace", "delete", "{new}", "--force", "--json"], "error", "E_WS_NOT_FOUND"),
    (["workspace", "list", "--json"], "ok", None),
    (["workspace", "lock", "{new}", "--json"], "error", "E_NOT_FOUND"),
    (
        ["workspace", "override", "{new}", "org.x", "--version", "1.0.0", "--json"],
        "error",
        "E_WS_NOT_FOUND",
    ),
    (["workspace", "show", "{new}", "--json"], "error", "E_WS_NOT_FOUND"),
    (["workspace", "solve", "org.no.such", "--json"], "error", "E_SOLVE_CONFLICT"),
    (["workspace", "update", "{new}", "--json"], "error", "E_WS_NOT_FOUND"),
    (["workspace", "verify-lock", "{new}", "--json"], "error", "E_NOT_FOUND"),
]


def _json_commands() -> set[str]:
    """The space-joined path of every command that takes --json."""
    from o3de_cli.__main__ import cli

    found: set[str] = set()

    def walk(cmd: click.Command, path: list[str]) -> None:
        if isinstance(cmd, click.Group):
            for name, sub in cmd.commands.items():
                walk(sub, path + [name])
        elif any("--json" in p.opts for p in cmd.params):
            found.add(" ".join(path))

    walk(cli, [])
    return found


def _case_command(argv: list[str]) -> str:
    known = _json_commands()
    for n in (2, 1):
        if " ".join(argv[:n]) in known:
            return " ".join(argv[:n])
    raise AssertionError(f"not a --json command: {argv}")


def test_every_json_command_has_a_contract_case():
    covered = {_case_command(argv) for argv, _, _ in _JSON_CASES}
    missing = sorted(_json_commands() - covered)
    assert missing == [], "add a _JSON_CASES entry for: " + ", ".join(missing)


@pytest.fixture
def o3de_home(tmp_path, monkeypatch):
    """A fresh home with a manifest that has no remotes (so no network)."""
    from o3de_cli.core import paths

    home = tmp_path / "home"
    home.mkdir()
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(home))
    monkeypatch.setattr("o3de_cli.core.config._config_instance", None)
    monkeypatch.setattr("o3de_cli.core.store.get_manifest_remote_urls", lambda *a, **k: [])
    paths.initialize_user_directories()
    manifest = paths.get_default_manifest_data()
    manifest["remote"] = {key: [] for key in manifest.get("remote", {})}
    (home / ".o3de" / "o3de_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    work = tmp_path / "work"
    (work / "empty").mkdir(parents=True)
    (work / "cmake").mkdir()
    (work / "cmake" / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.22)\n")
    (work / "plan.json").write_text("not json", encoding="utf-8")
    return {
        "empty": str(work / "empty"),
        "cmake": str(work / "cmake"),
        "plan": str(work / "plan.json"),
        "new": str(work / "new"),
    }


@pytest.mark.parametrize(
    ("argv", "status", "code"),
    _JSON_CASES,
    ids=[" ".join(a for a in argv if not a.startswith("{")) for argv, _, _ in _JSON_CASES],
)
def test_json_output_follows_the_contract(o3de_home, argv, status, code):
    """stdout is one envelope, and the exit code matches its status."""
    from o3de_cli.__main__ import cli

    args = [a.format(**o3de_home) for a in argv]
    result = CliRunner().invoke(cli, args)
    assert result.exception is None or isinstance(result.exception, SystemExit), repr(
        result.exception
    )
    try:
        doc = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"stdout is not one JSON document: {result.stdout!r}") from exc

    assert isinstance(doc, dict)
    assert doc["status"] in ("ok", "error")
    assert result.exit_code == (0 if doc["status"] == "ok" else 1), result.output
    if doc["status"] == "ok":
        assert "data" in doc
        assert isinstance(doc["warnings"], list)
    else:
        assert isinstance(doc["error"], str) and doc["error"]
        assert doc["code"].startswith("E_")
    assert (doc["status"], doc.get("code")) == (status, code), doc


# ---------------------------------------------------------------------------
# Unexpected exceptions under --json (o3de_cli.__main__.main)
# ---------------------------------------------------------------------------


def _run_main(argv, capsys):
    from o3de_cli.__main__ import main

    with patch("o3de_cli.core.config.get_config", side_effect=RuntimeError("kaboom")):
        with pytest.raises(SystemExit) as exc:
            main(argv)
    return exc.value.code, capsys.readouterr()


def test_unexpected_exception_under_json_is_e_internal(capsys, monkeypatch):
    monkeypatch.delenv("O3DE_DEBUG", raising=False)
    code, out = _run_main(["config", "list", "--json"], capsys)
    assert code == 1
    assert json.loads(out.out) == {"status": "error", "error": "kaboom", "code": "E_INTERNAL"}
    assert "Traceback" not in out.err


def test_unexpected_exception_traceback_with_o3de_debug(capsys, monkeypatch):
    monkeypatch.setenv("O3DE_DEBUG", "1")
    code, out = _run_main(["config", "list", "--json"], capsys)
    assert code == 1
    assert json.loads(out.out)["code"] == "E_INTERNAL"
    assert "Traceback" in out.err and "kaboom" in out.err


def test_unexpected_exception_in_text_mode_is_unchanged():
    from o3de_cli.__main__ import main

    with patch("o3de_cli.core.config.get_config", side_effect=RuntimeError("kaboom")):
        with pytest.raises(RuntimeError, match="kaboom"):
            main(["config", "list"])


def test_click_usage_error_under_json_stays_with_click(capsys):
    """Usage errors exit 2 with Click's message on stderr and nothing on stdout."""
    from o3de_cli.__main__ import main

    with pytest.raises(SystemExit) as exc:
        main(["config", "list", "--json", "--no-such-option"])
    out = capsys.readouterr()
    assert exc.value.code == 2
    assert out.out == ""
    assert "No such option" in out.err


import pytest as _pytest


@_pytest.mark.parametrize("group", ["engine", "gem", "template", "project", "repo", "overlay"])
def test_unregister_missing_exits_1_in_text_mode(group, tmp_path):
    """Text mode agrees with --json: "not found" is a failure."""
    import json as _json
    from unittest.mock import patch as _patch

    from o3de_cli.__main__ import cli

    manifest = tmp_path / "o3de_manifest.json"
    manifest.write_text(_json.dumps({"local": {}, "remote": {}}))
    with _patch("o3de_cli.core.paths.get_manifest_path", return_value=manifest):
        result = CliRunner().invoke(cli, [group, "unregister", "org.nobody.missing"])
    assert result.exit_code == 1, result.output
    assert "not found" in result.output.lower()


def test_audit_has_no_fix_flag():
    """--fix was accepted and silently did nothing."""
    from o3de_cli.__main__ import cli

    result = CliRunner().invoke(cli, ["audit", "--fix"])
    assert result.exit_code == 2
