# O3DE CLI - object hoist tests
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
        # children point at the 2.0.0 sidecar (canonical name for the
        # remote crawler, which fetches the literal child path)
        assert repo_meta["children"]["gems"] == ["Gems/Widget/gem.2-0-0.json"]
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


def _commit_gem_json(engine_repo, **changes):
    """Rewrite the fixture gem's JSON with *changes* and commit it."""
    gem_json = engine_repo / "Gems" / "Widget" / "gem.2-0-0.json"
    meta = json.loads(gem_json.read_text(encoding="utf-8"))
    meta.update(changes)
    _write_json(gem_json, meta)
    _git(["add", "-A"], engine_repo)
    _git(["commit", "-m", "Change gem json"], engine_repo)


class TestHoistRegressions:
    def _run(self, gem_dir, out, extra=()):
        from o3de_cli.commands.object import object_group

        return CliRunner().invoke(object_group, ["hoist", str(gem_dir), "-o", str(out), *extra])

    def test_malformed_json_reports_error_code(self, engine_repo, tmp_path):
        """Regression: a malformed object JSON produced a traceback, even with --json."""
        gem = engine_repo / "Gems" / "Widget"
        (gem / "gem.2-0-0.json").write_text("{oops", encoding="utf-8")
        result = self._run(gem, tmp_path / "family", ["--json"])
        assert result.exit_code == 1, result.output
        assert isinstance(result.exception, SystemExit)
        payload = json.loads(result.output)
        assert payload["code"] == "E_INVALID_OBJECT"
        assert "gem.2-0-0.json" in payload["error"]

    def test_dry_run_matches_execution(self, engine_repo, tmp_path):
        """Regression: dry-run listed overlays for platforms without git history."""
        gem = engine_repo / "Gems" / "Widget"
        linux = gem / "Code" / "Source" / "Platform" / "Linux"
        linux.mkdir()
        (linux / "untracked.cmake").write_text("# not committed\n")
        out = tmp_path / "family"

        dry = self._run(gem, out, ["--dry-run", "--json"])
        assert dry.exit_code == 0, dry.output
        plan = json.loads(dry.output)["data"]
        assert [o["platform"] for o in plan["overlays"]] == ["Common", "Windows"]
        assert [s["platform"] for s in plan["skipped"]] == ["Linux"]
        assert "2.0.0" in plan["skipped"][0]["reason"]  # the fixture's branch

        console_dry = self._run(gem, out, ["--dry-run"])
        assert "Skipped" in console_dry.output and "widget.linux" in console_dry.output

        result = self._run(gem, out, ["--json"])
        assert result.exit_code == 0, result.output
        done = json.loads(result.output)["data"]
        assert done["overlays"] == [o["overlay"] for o in plan["overlays"]]

    def test_case_clash_refused_before_writing(self, engine_repo, tmp_path):
        """Regression: platform dirs differing only in case shared an overlay dir."""
        gem = engine_repo / "Gems" / "Widget"
        clash = gem / "Code" / "Tests" / "Platform" / "windows"
        clash.mkdir(parents=True)
        (clash / "t.cmake").write_text("t")
        out = tmp_path / "family"
        result = self._run(gem, out, ["--json"])
        assert result.exit_code == 1, result.output
        payload = json.loads(result.output)
        assert payload["code"] == "E_OVERLAY_NAME_CLASH"
        assert not out.exists()

    def test_commit_failure_removes_real_clone(self, engine_repo, tmp_path):
        """Regression: only a filter-repo failure removed the half-made family repo."""
        from unittest.mock import patch

        from o3de_cli.core.split_platforms import ObjectOpError

        out = tmp_path / "family"
        with patch(
            "o3de_cli.core.hoist.commit_metadata",
            side_effect=ObjectOpError("Metadata commit failed: x", "E_COMMIT_FAILED"),
        ):
            result = self._run(engine_repo / "Gems" / "Widget", out, ["--json"])
        assert result.exit_code == 1, result.output
        assert json.loads(result.output)["code"] == "E_COMMIT_FAILED"
        assert not out.exists()

    def test_escaping_license_skipped_with_warning(self, engine_repo, tmp_path):
        """Regression: a ``..`` license path copied files from outside the object."""
        (tmp_path / "secret.txt").write_text("secret")
        _commit_gem_json(
            engine_repo,
            licenses=[
                {"license_identifier": "MIT", "relative_path": "LICENSE_MIT.TXT"},
                {"license_identifier": "X", "relative_path": "../../../secret.txt"},
            ],
        )
        out = tmp_path / "family"
        result = self._run(engine_repo / "Gems" / "Widget", out, ["--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert any("../../../secret.txt" in w for w in payload["warnings"])
        assert not [p for p in out.rglob("secret.txt")]
        assert (out / "Overlays" / "widget.windows" / "LICENSE_MIT.TXT").is_file()
        assert (out / "LICENSE_MIT.TXT").is_file()

    def test_register_creates_missing_manifest(self, engine_repo, tmp_path):
        """Regression: ``--register`` silently did nothing without a manifest."""
        from unittest.mock import patch

        mp = tmp_path / "home" / ".o3de" / "o3de_manifest.json"
        out = tmp_path / "family"
        with patch("o3de_cli.core.paths.get_manifest_path", return_value=mp):
            result = self._run(engine_repo / "Gems" / "Widget", out, ["--register", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["data"]["registered"] is True
        assert json.loads(mp.read_text())["local"]["repos"] == [out.as_posix()]
