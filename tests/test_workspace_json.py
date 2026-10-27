# O3DE CLI - workspace --json output tests
# SPDX-License-Identifier: Apache-2.0 OR MIT

"""Every ``workspace`` subcommand with ``--json`` writes exactly one JSON
document to stdout, on success and on error.

The MCP server and o3de-pilot parse stdout as JSON, so a spinner frame,
a status line or a CMake log line ahead of the document breaks them
(``E_INVALID_JSON``).  Errors exit 1.  Text meant for people (warnings,
progress, CMake output, prompts) may go to stderr.

The tests parse ``result.stdout``, which is what a subprocess caller
sees; ``result.output`` also carries stderr.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from o3de_cli.core.solver import Candidate, CandidateStatus, SolveResult
from o3de_cli.core.models import ObjectType
from o3de_cli.core.workspace_ops import WorkspaceOpError
from tests.conftest import _write_json


def _doc(result) -> dict:
    """The one JSON document on stdout (fails on anything else)."""
    return json.loads(result.stdout)


def _manifest(tmp_path: Path) -> Path:
    mp = tmp_path / "o3de_manifest.2-0-0.json"
    _write_json(mp, {
        "$schemaVersion": "2.0.0",
        "o3de_manifest": {"name": "test"},
        "local": {"engines": [], "projects": [], "gems": [],
                  "templates": [], "repos": [], "overlays": []},
        "remotes": [],
    })
    return mp


def _engine(tmp_path: Path, name="org.test.engine") -> Path:
    edir = tmp_path / "engine"
    edir.mkdir(exist_ok=True)
    data = {"$schemaVersion": "2.0.0", "engine": {"name": name, "version": "1.0.0"}}
    _write_json(edir / "engine.json", data)
    (edir / "README.txt").write_text("engine")
    return edir


def _overlay(tmp_path: Path, dirname: str, name: str, extends: str) -> Path:
    odir = tmp_path / dirname
    _write_json(odir / "overlay.json", {
        "$schemaVersion": "2.0.0",
        "overlay": {"name": name, "version": "1.0.0"},
        "extends": extends,
        "precedence": 0,
    })
    (odir / "Overlay").mkdir()
    (odir / "Overlay" / f"{dirname}.txt").write_text(dirname)
    return odir


@pytest.fixture
def env(tmp_path):
    """Manifest and workspace locations pointed into tmp_path."""
    mp = _manifest(tmp_path)
    resolved = tmp_path / "resolved.json"
    with patch("o3de_cli.commands.workspace.get_manifest_path", return_value=mp), \
         patch("o3de_cli.core.paths.get_manifest_path", return_value=mp), \
         patch("o3de_cli.commands.workspace.get_resolved_manifest_path",
               return_value=resolved), \
         patch("o3de_cli.commands.workspace.get_default_workspaces_path",
               return_value=tmp_path / "workspaces"), \
         patch("o3de_cli.core.resolver.get_manifest_path", return_value=mp), \
         patch("o3de_cli.core.resolver.get_resolved_manifest_path",
               return_value=resolved):
        yield tmp_path


def _invoke(*args, **kwargs):
    from o3de_cli.commands.workspace import workspace

    return CliRunner().invoke(workspace, list(args), **kwargs)


def _create(tmp_path: Path, name="ws", *extra) -> Path:
    """Create an engine workspace without solving; returns its path."""
    edir = _engine(tmp_path)
    out = tmp_path / "out"
    result = _invoke("create", name, "--engine", str(edir), "--output", str(out),
                     "--no-solve", *extra)
    assert result.exit_code == 0, result.output
    return out / name


def _registered_resolver(edir: Path) -> MagicMock:
    resolver = MagicMock()
    resolver.objects = {"org.test.engine": SimpleNamespace(path=edir.resolve(), data={})}
    resolver.overlays = {}
    resolver.manifest_remotes = []
    return resolver


def _solve_result(conflict: str = "", path: Path | None = None) -> SolveResult:
    candidates = {}
    if path is not None:
        candidates["org.test.engine"] = Candidate(
            "org.test.engine", "1.0.0", ObjectType.ENGINE,
            status=CandidateStatus.LOCAL, path=path,
        )
    return SolveResult(
        root_name="org.test.engine",
        root_version="1.0.0",
        candidates=candidates,
        conflict_message=conflict,
    )


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


class TestCreateJson:
    def test_success_is_one_document(self, env):
        edir = _engine(env)
        result = _invoke("create", "ws", "--engine", str(edir),
                         "--output", str(env / "out"), "--json")
        assert result.exit_code == 0, result.output
        data = _doc(result)
        assert data["status"] == "ok"
        assert data["data"]["action"] == "created"

    def test_solved_success_is_one_document(self, env):
        edir = _engine(env)
        with patch("o3de_cli.commands.workspace.Resolver",
                   return_value=_registered_resolver(edir)), \
             patch("o3de_cli.commands.workspace.solve_for_workspace",
                   return_value=_solve_result(path=edir.resolve())):
            result = _invoke("create", "ws", "--engine", str(edir),
                             "--output", str(env / "out"), "--json")
        assert result.exit_code == 0, result.output
        assert _doc(result)["status"] == "ok"

    def test_solve_failure_is_json_error(self, env):
        edir = _engine(env)
        with patch("o3de_cli.commands.workspace.Resolver",
                   return_value=_registered_resolver(edir)), \
             patch("o3de_cli.commands.workspace.solve_for_workspace",
                   return_value=_solve_result(conflict="no version of x fits")):
            result = _invoke("create", "ws", "--engine", str(edir),
                             "--output", str(env / "out"), "--json")
        assert result.exit_code == 1
        data = _doc(result)
        assert data["status"] == "error"
        assert data["code"] == "E_SOLVE_CONFLICT"
        assert "no version of x fits" in data["error"]

    def test_solve_failure_text_mode_unchanged(self, env):
        edir = _engine(env)
        with patch("o3de_cli.commands.workspace.Resolver",
                   return_value=_registered_resolver(edir)), \
             patch("o3de_cli.commands.workspace.solve_for_workspace",
                   return_value=_solve_result(conflict="no version of x fits")):
            result = _invoke("create", "ws", "--engine", str(edir),
                             "--output", str(env / "out"))
        assert result.exit_code == 1
        assert "Dependency resolution failed" in result.output
        assert "no version of x fits" in result.output

    def test_bad_overlay_order_is_json_error(self, env):
        edir = _engine(env)
        ov = _overlay(env, "ov1", "org.test.overlay.one", "org.test.engine")
        result = _invoke("create", "ws", "--engine", str(edir), "--output", str(env / "out"),
                         "--no-solve", "--overlay", str(ov),
                         "--overlay-order", "org.test.overlay.one", "--json")
        assert result.exit_code == 1
        data = _doc(result)
        assert data["status"] == "error"
        assert data["code"] == "E_OVERLAY_ORDER"

    def test_unknown_root_type_is_json_error(self, env):
        bare = env / "bare"
        bare.mkdir()
        result = _invoke("create", "ws", "--root", str(bare),
                         "--output", str(env / "out"), "--json")
        assert result.exit_code == 1
        data = _doc(result)
        assert data["status"] == "error"
        assert data["code"] == "E_NOT_AN_OBJECT"

    def test_warnings_ride_in_the_envelope(self, env):
        """Solve warnings go into ``warnings``, not ahead of the document."""
        edir = _engine(env)
        remote = Candidate("org.far.gem", "1.0.0", ObjectType.GEM,
                           status=CandidateStatus.REMOTE)
        result_with_remote = _solve_result(path=edir.resolve())
        result_with_remote.candidates["org.far.gem"] = remote
        with patch("o3de_cli.commands.workspace.Resolver",
                   return_value=_registered_resolver(edir)), \
             patch("o3de_cli.commands.workspace.solve_for_workspace",
                   return_value=result_with_remote):
            result = _invoke("create", "ws", "--engine", str(edir),
                             "--output", str(env / "out"), "--auto-install", "--json")
        assert result.exit_code == 0, result.output
        data = _doc(result)
        assert data["status"] == "ok"
        warnings = " ".join(data.get("warnings", []))
        assert "--auto-install requires --include-store" in warnings
        assert "remote dependencies not yet installed" in warnings


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


class TestUpdateJson:
    def test_resync_success_is_one_document(self, env):
        ws = _create(env)
        result = _invoke("update", str(ws), "--json")
        assert result.exit_code == 0, result.output
        data = _doc(result)
        assert data["status"] == "ok"
        assert data["data"]["action"] == "updated"

    def test_overlay_change_error_is_json(self, env):
        ws = _create(env)
        result = _invoke("update", str(ws), "--remove-overlay", "org.not.here", "--json")
        assert result.exit_code == 1
        data = _doc(result)
        assert data["status"] == "error"
        assert data["code"] == "E_OVERLAY_NOT_IN_WS"

    def test_not_found_is_json_error(self, env):
        result = _invoke("update", "nope", "--json")
        assert result.exit_code == 1
        assert _doc(result)["code"] == "E_WS_NOT_FOUND"


# ---------------------------------------------------------------------------
# override
# ---------------------------------------------------------------------------


class TestOverrideJson:
    def test_success_is_one_document(self, env):
        ws = _create(env)
        with patch("o3de_cli.commands.workspace.apply_override", return_value=["org.x"]), \
             patch("o3de_cli.commands.workspace._write_cmake_manifest"):
            result = _invoke("override", str(ws), "org.x", "--version", "1.0.0", "--json")
        assert result.exit_code == 0, result.output
        data = _doc(result)
        assert data["status"] == "ok"
        assert data["data"]["relinked"] == ["org.x"]

    def test_error_is_json(self, env):
        ws = _create(env)
        with patch("o3de_cli.commands.workspace.apply_override",
                   side_effect=WorkspaceOpError("nope", "E_SOLVE_CONFLICT")):
            result = _invoke("override", str(ws), "org.x", "--version", "1.0.0", "--json")
        assert result.exit_code == 1
        assert _doc(result)["code"] == "E_SOLVE_CONFLICT"


# ---------------------------------------------------------------------------
# candidates
# ---------------------------------------------------------------------------


class TestCandidatesJson:
    def test_success_is_one_document(self, env):
        ws = _create(env)
        resolver = MagicMock()
        resolver.objects = {}
        with patch("o3de_cli.commands.workspace.Resolver", return_value=resolver), \
             patch("o3de_cli.core.solver.list_candidates", return_value=[]):
            result = _invoke("candidates", str(ws), "--json")
        assert result.exit_code == 0, result.output
        data = _doc(result)
        assert data["status"] == "ok"
        assert [o["name"] for o in data["data"]["objects"]] == ["org.test.engine"]

    def test_not_found_is_json_error(self, env):
        result = _invoke("candidates", "nope", "--json")
        assert result.exit_code == 1
        assert _doc(result)["code"] == "E_WS_NOT_FOUND"


# ---------------------------------------------------------------------------
# list / show / delete
# ---------------------------------------------------------------------------


class TestListJson:
    def test_success_is_one_document(self, env):
        _create(env)
        result = _invoke("list", "--json")
        assert result.exit_code == 0, result.output
        data = _doc(result)
        assert [w["name"] for w in data] == ["ws"]


class TestShowJson:
    def test_success_is_one_document(self, env):
        ws = _create(env)
        result = _invoke("show", str(ws), "--json")
        assert result.exit_code == 0, result.output
        data = _doc(result)
        assert data["workspace"]["name"] == "ws"

    def test_not_found_is_json_error(self, env):
        result = _invoke("show", "nope", "--json")
        assert result.exit_code == 1
        data = _doc(result)
        assert data["status"] == "error"
        assert data["code"] == "E_WS_NOT_FOUND"

    def test_invalid_is_json_error(self, env):
        bad = env / "bad"
        bad.mkdir()
        result = _invoke("show", str(bad), "--json")
        assert result.exit_code == 1
        assert _doc(result)["code"] == "E_WS_INVALID"


class TestDeleteJson:
    def test_success_is_one_document(self, env):
        ws = _create(env)
        result = _invoke("delete", str(ws), "--force", "--json")
        assert result.exit_code == 0, result.output
        assert _doc(result)["data"]["action"] == "deleted"
        assert not ws.exists()

    def test_not_found_is_json_error(self, env):
        result = _invoke("delete", "nope", "--json")
        assert result.exit_code == 1
        assert _doc(result)["code"] == "E_WS_NOT_FOUND"

    def test_confirmation_prompt_stays_off_stdout(self, env):
        ws = _create(env)
        result = _invoke("delete", str(ws), "--json", input="n\n")
        assert result.exit_code == 0, result.output
        data = _doc(result)
        assert data["status"] == "ok"
        assert data["data"]["action"] == "cancelled"
        assert ws.exists()


# ---------------------------------------------------------------------------
# solve
# ---------------------------------------------------------------------------


class TestSolveJson:
    def test_success_is_one_document(self, env):
        edir = _engine(env)
        with patch("o3de_cli.commands.workspace.Resolver",
                   return_value=_registered_resolver(edir)), \
             patch("o3de_cli.commands.workspace.solve_for_workspace",
                   return_value=_solve_result(path=edir.resolve())):
            result = _invoke("solve", "org.test.engine", "--json")
        assert result.exit_code == 0, result.output
        data = _doc(result)
        assert data["resolved"] is True
        assert "org.test.engine" in data["candidates"]

    def test_failure_is_json_and_exits_1(self, env):
        edir = _engine(env)
        with patch("o3de_cli.commands.workspace.Resolver",
                   return_value=_registered_resolver(edir)), \
             patch("o3de_cli.commands.workspace.solve_for_workspace",
                   return_value=_solve_result(conflict="boom")):
            result = _invoke("solve", "org.test.engine", "--json")
        assert result.exit_code == 1
        data = _doc(result)
        assert data["resolved"] is False
        assert data["conflict"] == "boom"


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------


class _FakePopen:
    """subprocess.Popen double that prints CMake-like output."""

    returncodes: list[int] = []

    def __init__(self, cmd, **kwargs):
        self.pid = 1
        self.stdout = iter(["-- Configuring done\n", "-- Build files written\n"])
        self._rc = self.returncodes.pop(0) if self.returncodes else 0

    def wait(self, timeout=None):
        return self._rc


class TestBuildJson:
    def _ws(self, tmp_path: Path) -> Path:
        engine_dir = tmp_path / "engine"
        engine_dir.mkdir()
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        ws = tmp_path / "bws"
        ws.mkdir()
        _write_json(ws / "workspace.json", {
            "$schemaVersion": "2.0.0",
            "workspace": {"name": "bws"},
            "created": "2026-01-01T00:00:00",
            "root_object": str(project_dir),
            "root_type": "project",
            "sources": {
                "engines": {"org.e": str(engine_dir)},
                "projects": {"org.p": str(project_dir)},
            },
        })
        return ws

    def test_cmake_output_stays_off_stdout(self, tmp_path):
        ws = self._ws(tmp_path)
        _FakePopen.returncodes = [0, 0]
        with patch("o3de_cli.commands.workspace.subprocess.Popen", _FakePopen), \
             patch("o3de_cli.commands.workspace._find_third_party_path", return_value=None):
            result = _invoke("build", str(ws), "--json")
        assert result.exit_code == 0, result.output
        data = _doc(result)
        assert data["status"] == "ok"
        assert "-- Configuring done" in result.stderr

    def test_configure_failure_is_json_error(self, tmp_path):
        ws = self._ws(tmp_path)
        _FakePopen.returncodes = [2]
        with patch("o3de_cli.commands.workspace.subprocess.Popen", _FakePopen), \
             patch("o3de_cli.commands.workspace._find_third_party_path", return_value=None):
            result = _invoke("build", str(ws), "--json")
        assert result.exit_code != 0
        data = _doc(result)
        assert data["status"] == "error"
        assert data["code"] == "E_CONFIGURE_FAILED"

    def test_text_mode_still_streams_to_stdout(self, tmp_path):
        ws = self._ws(tmp_path)
        _FakePopen.returncodes = [0, 0]
        with patch("o3de_cli.commands.workspace.subprocess.Popen", _FakePopen), \
             patch("o3de_cli.commands.workspace._find_third_party_path", return_value=None):
            result = _invoke("build", str(ws))
        assert result.exit_code == 0, result.output
        assert "-- Configuring done" in result.stdout


# ---------------------------------------------------------------------------
# lock / verify-lock
# ---------------------------------------------------------------------------


class TestLockJson:
    def test_success_is_one_document(self, env):
        ws = _create(env)
        result = _invoke("lock", str(ws), "--json")
        assert result.exit_code == 0, result.output
        data = _doc(result)
        assert data["status"] == "ok"
        assert data["data"]["packages"] >= 1

    def test_not_found_is_json_error(self, env):
        result = _invoke("lock", "nope", "--json")
        assert result.exit_code == 1
        assert _doc(result)["status"] == "error"


class TestVerifyLockJson:
    def test_success_is_one_document(self, env):
        ws = _create(env)
        assert _invoke("lock", str(ws)).exit_code == 0
        result = _invoke("verify-lock", str(ws), "--json")
        assert result.exit_code == 0, result.output
        assert _doc(result)["verified"] is True

    def test_not_found_is_json_error(self, env):
        result = _invoke("verify-lock", "nope", "--json")
        assert result.exit_code == 1
        assert _doc(result)["status"] == "error"
