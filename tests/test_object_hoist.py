# O3DE Pilot - object hoist tests
# SPDX-License-Identifier: Apache-2.0 OR MIT

"""Tests for ``o3de object hoist`` (git history split into a family repo)."""

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from tests.conftest import _write_json


def _git(args, cwd):
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True,
    )
    assert result.returncode == 0, f"git {args}: {result.stdout}{result.stderr}"
    return result.stdout.strip()


@pytest.fixture()
def engine_repo(tmp_path):
    """A tiny git repo shaped like an engine containing one gem."""
    root = tmp_path / "engine"
    gem = root / "Gems" / "Widget"
    plat = gem / "Code" / "Source" / "Platform"
    (plat / "Windows").mkdir(parents=True)
    (plat / "Common" / "Unimplemented").mkdir(parents=True)
    (gem / "Code" / "Source").joinpath("Widget.cpp").write_text("// v1\n")
    (plat / "Windows" / "platform_windows.cmake").write_text("# win v1\n")
    (plat / "Common" / "Unimplemented" / "Widget_Unimpl.cpp").write_text("//\n")
    (gem / "LICENSE_MIT.TXT").write_text("MIT")
    _write_json(gem / "gem.2-0-0.json", {
        "$schemaVersion": "2.0.0",
        "gem": {"name": "org.test.gem.widget", "version": "1.2.0"},
        "origin": {"name": "Test Org", "url": "https://example.test"},
        "licenses": [
            {"license_identifier": "MIT", "display_name": "MIT License",
             "relative_path": "LICENSE_MIT.TXT"},
        ],
    })
    (root / "engine.json").write_text("{}")

    _git(["init", "-b", "main"], tmp_path)  # placeholder to ensure git works
    _git(["init", "-b", "2.0.0"], root)
    _git(["config", "user.email", "t@t"], root)
    _git(["config", "user.name", "t"], root)
    _git(["add", "-A"], root)
    _git(["commit", "-m", "Initial engine"], root)
    # Second commit touching gem + platform file (history depth)
    (gem / "Code" / "Source" / "Widget.cpp").write_text("// v2\n")
    (plat / "Windows" / "platform_windows.cmake").write_text("# win v2\n")
    _git(["add", "-A"], root)
    _git(["commit", "-m", "Update Widget"], root)
    return root


class TestHoist:
    def _run(self, gem_dir, out, extra=()):
        from o3de_cli.commands.object import object_group
        runner = CliRunner()
        return runner.invoke(object_group, [
            "hoist", str(gem_dir), "-o", str(out), *extra,
        ])

    def test_dry_run(self, engine_repo, tmp_path):
        out = tmp_path / "family"
        result = self._run(engine_repo / "Gems" / "Widget", out, ["--dry-run"])
        assert result.exit_code == 0, result.output
        assert "org.test.repo.widget" in result.output
        assert "Overlays/widget.windows" in result.output
        assert not out.exists()

    def test_hoist_family_repo(self, engine_repo, tmp_path):
        out = tmp_path / "family"
        result = self._run(engine_repo / "Gems" / "Widget", out)
        assert result.exit_code == 0, result.output

        # Layout: repo.json root, gem without platform dirs, overlays
        repo_meta = json.loads((out / "repo.json").read_text(encoding="utf-8"))
        assert repo_meta["repo"]["name"] == "org.test.repo.widget"
        assert repo_meta["children"]["gems"] == ["Gems/Widget/gem.json"]
        assert sorted(repo_meta["children"]["overlays"]) == [
            "Overlays/widget.common/overlay.json",
            "Overlays/widget.windows/overlay.json",
        ]
        assert (out / "Gems" / "Widget" / "Code" / "Source" / "Widget.cpp").is_file()
        assert not (out / "Gems" / "Widget" / "Code" / "Source" / "Platform").exists()
        # gem.json survives the split (the children path resolves)
        assert (out / "Gems" / "Widget" / "gem.2-0-0.json").is_file()

        # Overlay payloads at object-relative locations, with history
        win = out / "Overlays" / "widget.windows"
        assert (win / "Overlay" / "Code" / "Source" / "Platform" /
                "Windows" / "platform_windows.cmake").read_text() == "# win v2\n"
        ov_meta = json.loads((win / "overlay.json").read_text(encoding="utf-8"))
        assert ov_meta["extends"] == "org.test.gem.widget>=1.2.0"
        assert ov_meta["platforms"] == ["Windows"]
        assert ov_meta["dependent"]["overlays"] == [
            "org.test.overlay.widget.common>=1.2.0"]
        assert (win / "LICENSE_MIT.TXT").is_file()

        # History: 2 source commits + 1 metadata commit
        log = _git(["log", "--oneline"], out)
        assert len(log.splitlines()) == 3
        # File history followed across the split
        file_log = _git(
            ["log", "--oneline", "--",
             "Overlays/widget.windows/Overlay/Code/Source/Platform/Windows/platform_windows.cmake"],
            out,
        )
        assert len(file_log.splitlines()) == 2

    def test_hoist_registers_repo_only(self, engine_repo, tmp_path, monkeypatch):
        mp = tmp_path / "o3de_manifest.2-0-0.json"
        _write_json(mp, {
            "$schemaVersion": "2.0.0",
            "o3de_manifest": {"name": "test"},
            "local": {"engines": [], "projects": [], "gems": [],
                      "templates": [], "repos": [], "overlays": []},
            "remotes": [],
        })
        out = tmp_path / "family"
        from unittest.mock import patch
        with patch("o3de_cli.core.paths.get_manifest_path", return_value=mp):
            result = self._run(engine_repo / "Gems" / "Widget", out,
                               ["--register"])
        assert result.exit_code == 0, result.output
        manifest = json.loads(mp.read_text())
        assert manifest["local"]["repos"] == [out.as_posix()]
        assert manifest["local"]["gems"] == []      # never individual
        assert manifest["local"]["overlays"] == []  # never individual

    def test_output_exists_refused(self, engine_repo, tmp_path):
        out = tmp_path / "family"
        out.mkdir()
        result = self._run(engine_repo / "Gems" / "Widget", out)
        assert result.exit_code == 1
        assert "already exists" in result.output

    def test_not_a_git_repo(self, tmp_path):
        gdir = tmp_path / "loose-gem"
        gdir.mkdir()
        _write_json(gdir / "gem.2-0-0.json", {
            "$schemaVersion": "2.0.0",
            "gem": {"name": "org.test.gem.loose", "version": "1.0.0"},
        })
        out = tmp_path / "family"
        result = self._run(gdir, out)
        assert result.exit_code == 1
