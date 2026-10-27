# O3DE CLI - workspace create/update regression tests
# SPDX-License-Identifier: Apache-2.0 OR MIT

"""End-to-end regressions for ``workspace create --auto-install`` and a
plain ``workspace update`` (re-sync), driven through the CLI."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from o3de_cli.core.models import ObjectType
from o3de_cli.core.solver import Candidate, CandidateStatus, OverlayEntry, SolveResult
from tests.conftest import _write_json


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


def _object(tmp_path: Path, dirname: str, kind: str, name: str) -> Path:
    d = tmp_path / dirname
    _write_json(d / f"{kind}.json", {
        "$schemaVersion": "2.0.0",
        kind: {"name": name, "version": "1.0.0"},
    })
    (d / f"{dirname}.txt").write_text(dirname)
    return d.resolve()


def _overlay(tmp_path: Path, dirname: str, name: str, extends: str) -> Path:
    d = tmp_path / dirname
    _write_json(d / "overlay.json", {
        "$schemaVersion": "2.0.0",
        "overlay": {"name": name, "version": "1.0.0"},
        "extends": extends,
        "precedence": 0,
    })
    (d / "Overlay").mkdir()
    (d / "Overlay" / "patched.txt").write_text("from overlay")
    return d.resolve()


@pytest.fixture
def env(tmp_path):
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


def _invoke(*args):
    from o3de_cli.commands.workspace import workspace

    return CliRunner().invoke(workspace, list(args))


def _local(name, path, obj_type):
    return Candidate(name, "1.0.0", obj_type, status=CandidateStatus.LOCAL, path=path)


def _remote(name):
    return Candidate(name, "1.0.0", ObjectType.GEM, status=CandidateStatus.REMOTE)


def _solved(root, *cands, overlays=None):
    return SolveResult(
        root_name=root,
        root_version="1.0.0",
        candidates={c.name: c for c in cands},
        overlays=overlays or {},
    )


def _resolver(objects: dict[str, Path]) -> MagicMock:
    resolver = MagicMock()
    resolver.objects = {n: SimpleNamespace(path=p, data={}) for n, p in objects.items()}
    resolver.overlays = {}
    resolver.manifest_remotes = ["https://example.invalid/repo.json"]
    resolver.auto_install_missing.return_value = [{"name": "org.far.extra"}]
    return resolver


# ---------------------------------------------------------------------------
# create --auto-install
# ---------------------------------------------------------------------------


class TestAutoInstallKeepsSelection:
    def test_gem_root_keeps_engine_closure_and_overlays(self, env):
        gem = _object(env, "mygem", "gem", "org.my.rootgem")
        eng = _object(env, "eng", "engine", "org.eng")
        engine_gem = _object(env, "egem", "gem", "org.eng.atom")
        far = _object(env, "far", "gem", "org.far.extra")
        ov = _overlay(env, "ov", "org.eng.atom.patch", "org.eng.atom")
        engine_closure = _solved(
            "org.eng",
            _local("org.eng", eng, ObjectType.ENGINE),
            _local("org.eng.atom", engine_gem, ObjectType.GEM),
            overlays={"org.eng.atom": [OverlayEntry(
                name="org.eng.atom.patch", version="1.0.0", extends="org.eng.atom",
                extends_version=None, precedence=0, path=ov,
            )]},
        )
        solves = [
            _solved("org.my.rootgem", _local("org.my.rootgem", gem, ObjectType.GEM),
                    _remote("org.far.extra")),
            engine_closure,
            _solved("org.my.rootgem", _local("org.my.rootgem", gem, ObjectType.GEM),
                    _local("org.far.extra", far, ObjectType.GEM)),
            engine_closure,
        ]
        resolver = _resolver({"org.my.rootgem": gem, "org.eng": eng})
        with patch("o3de_cli.commands.workspace.Resolver", return_value=resolver), \
             patch("o3de_cli.commands.workspace.solve_for_workspace", side_effect=solves), \
             patch("o3de_cli.core.store.Store"):
            result = _invoke("create", "ws", "--root", str(gem), "--engine", str(eng),
                             "--output", str(env / "out"), "--include-store",
                             "--auto-install", "--json")
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)["data"]
        assert data["objects"] == 4  # root gem, engine, engine gem, installed gem
        assert data["overlays"] == 1

        sources = json.loads((env / "out" / "ws" / "workspace.json").read_text())["sources"]
        assert sources["engines"] == {"org.eng": str(eng)}
        assert set(sources["gems"]) == {"org.my.rootgem", "org.eng.atom", "org.far.extra"}
        assert set(sources["overlays"]) == {"org.eng.atom.patch"}
        composed = env / "out" / "ws" / "Gems" / "atom" / "patched.txt"
        assert composed.read_text() == "from overlay"

    def test_engine_and_project_root_linked_once(self, env):
        proj = _object(env, "proj-dir", "project", "org.my.project")
        eng = _object(env, "eng", "engine", "org.eng")
        far = _object(env, "far", "gem", "org.far.extra")
        solves = [
            _solved("org.my.project",
                    _local("org.my.project", proj, ObjectType.PROJECT),
                    _local("org.eng", eng, ObjectType.ENGINE),
                    _remote("org.far.extra")),
            _solved("org.my.project",
                    _local("org.my.project", proj, ObjectType.PROJECT),
                    _local("org.eng", eng, ObjectType.ENGINE),
                    _local("org.far.extra", far, ObjectType.GEM)),
        ]
        resolver = _resolver({"org.my.project": proj, "org.eng": eng})
        with patch("o3de_cli.commands.workspace.Resolver", return_value=resolver), \
             patch("o3de_cli.commands.workspace.solve_for_workspace", side_effect=solves), \
             patch("o3de_cli.core.store.Store"):
            result = _invoke("create", "ws", "--engine", str(eng), "--project", str(proj),
                             "--output", str(env / "out"), "--include-store",
                             "--auto-install", "--json")
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["data"]["objects"] == 3  # project, engine, gem

        ws = env / "out" / "ws"
        sources = json.loads((ws / "workspace.json").read_text())["sources"]
        assert sources["projects"] == {"org.my.project": str(proj)}
        assert sources["engines"] == {"org.eng": str(eng)}
        assert sources["gems"] == {"org.far.extra": str(far)}
        assert sorted(p.name for p in (ws / "Projects").iterdir()) == ["project"]


# ---------------------------------------------------------------------------
# update (re-sync)
# ---------------------------------------------------------------------------


class TestResyncKeepsOverlays:
    def test_plain_update_reapplies_overlay(self, env):
        # Directory name differs from the engine's name and short name
        eng = _object(env, "engine-src", "engine", "org.test.engine.o3de")
        ov = _overlay(env, "ov-src", "org.test.overlay.patch", "org.test.engine.o3de")
        out = env / "out"
        created = _invoke("create", "ws", "--engine", str(eng), "--overlay", str(ov),
                          "--output", str(out), "--no-solve")
        assert created.exit_code == 0, created.output
        engine_tree = out / "ws" / "Engines" / "o3de"
        assert (engine_tree / "patched.txt").read_text() == "from overlay"

        # The overlay gains a file; a re-sync must compose it in
        (ov / "Overlay" / "added.txt").write_text("added later")
        updated = _invoke("update", str(out / "ws"))
        assert updated.exit_code == 0, updated.output
        assert "skipping" not in updated.output
        assert (engine_tree / "added.txt").read_text() == "added later"
        assert (engine_tree / "patched.txt").read_text() == "from overlay"
        assert not (out / "ws" / "Gems" / "engine-src").exists()
