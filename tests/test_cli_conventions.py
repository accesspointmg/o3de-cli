# O3DE CLI - Conventions every command must follow
# SPDX-License-Identifier: Apache-2.0 OR MIT

"""Cross-cutting CLI conventions, checked over the whole command surface."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

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
    data = json.loads(result.output)
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
