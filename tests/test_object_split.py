# O3DE CLI - object split-platforms tests
# SPDX-License-Identifier: Apache-2.0 OR MIT

"""Tests for ``o3de object split-platforms``."""

import json
from pathlib import Path

from click.testing import CliRunner

from tests.conftest import _write_json


def _make_gem(tmp_path, name="org.test.gem.widget", version="1.2.0"):
    """A gem with Windows/Linux/Common PAL dirs, a license, and noise."""
    gdir = tmp_path / "widget"
    plat = gdir / "Code" / "Source" / "Platform"
    (plat / "Windows").mkdir(parents=True)
    (plat / "Windows" / "platform_windows.cmake").write_text("# win")
    (plat / "Linux").mkdir()
    (plat / "Linux" / "platform_linux.cmake").write_text("# linux")
    (plat / "Common" / "Unimplemented").mkdir(parents=True)
    (plat / "Common" / "Unimplemented" / "Widget_Unimplemented.cpp").write_text("//")
    # Noise that must be skipped
    (gdir / "build" / "Platform" / "Fake").mkdir(parents=True)
    (gdir / "build" / "Platform" / "Fake" / "x.cmake").write_text("# no")
    (gdir / "LICENSE_MIT.TXT").write_text("MIT")
    _write_json(gdir / "gem.2-0-0.json", {
        "$schemaVersion": "2.0.0",
        "gem": {"name": name, "version": version},
        "origin": {"name": "Test Org", "url": "https://example.test"},
        "licenses": [
            {"license_identifier": "MIT", "display_name": "MIT License",
             "relative_path": "LICENSE_MIT.TXT"},
            {"license_identifier": "Apache-2.0", "display_name": "Apache",
             "relative_path": "LICENSE_APACHE2.TXT"},  # file absent → dropped
        ],
    })
    return gdir


class TestHelpers:
    def test_find_platform_dirs(self, tmp_path):
        from o3de_cli.commands.object import _find_platform_dirs
        gdir = _make_gem(tmp_path)
        found = _find_platform_dirs(gdir)
        assert sorted(found) == ["Common", "Linux", "Windows"]
        # build/ noise skipped
        assert "Fake" not in found
        assert found["Windows"][0].name == "Windows"

    def test_split_overlay_name(self):
        from o3de_cli.commands.object import _split_overlay_name
        assert (_split_overlay_name("org.o3de.gem.achievementstest", "Windows")
                == "org.o3de.overlay.achievementstest.windows")
        assert (_split_overlay_name("org.test.project.demo", "Linux")
                == "org.test.overlay.demo.linux")
        # No type token → append
        assert (_split_overlay_name("mything", "Mac")
                == "mything.overlay.mac")


class TestSplitPlatformsCommand:
    def _run(self, gdir, out, extra=()):
        from o3de_cli.commands.object import object_group
        runner = CliRunner()
        return runner.invoke(object_group, [
            "split-platforms", str(gdir), "-o", str(out), *extra,
        ])

    def test_dry_run_creates_nothing(self, tmp_path):
        gdir = _make_gem(tmp_path)
        out = tmp_path / "out"
        result = self._run(gdir, out, ["--dry-run"])
        assert result.exit_code == 0, result.output
        assert "Would split" in result.output
        assert "org.test.overlay.widget.windows" in result.output
        assert not out.exists()

    def test_split_creates_overlays(self, tmp_path):
        gdir = _make_gem(tmp_path)
        out = tmp_path / "out"
        result = self._run(gdir, out, ["-T", "MyTag"])
        assert result.exit_code == 0, result.output

        win = out / "org.test.overlay.widget.windows"
        meta = json.loads((win / "overlay.json").read_text(encoding="utf-8"))
        assert meta["overlay"]["name"] == "org.test.overlay.widget.windows"
        assert meta["overlay"]["version"] == "1.2.0"
        assert meta["extends"] == "org.test.gem.widget>=1.2.0"
        assert meta["precedence"] == 10
        assert meta["platforms"] == ["Windows"]
        assert meta["dependent"]["overlays"] == [
            "org.test.overlay.widget.common>=1.2.0"]
        assert meta["user_tags"] == ["MyTag"]
        assert meta["origin"]["name"] == "Test Org"
        # Only the license whose file exists travels
        assert [lic["license_identifier"] for lic in meta["licenses"]] == ["MIT"]
        assert (win / "LICENSE_MIT.TXT").is_file()
        # Payload preserves the object-relative layout
        assert (win / "Overlay" / "Code" / "Source" / "Platform" /
                "Windows" / "platform_windows.cmake").is_file()

        common = out / "org.test.overlay.widget.common"
        cmeta = json.loads((common / "overlay.json").read_text(encoding="utf-8"))
        assert cmeta["precedence"] == 0
        assert "platforms" not in cmeta
        assert "dependent" not in cmeta
        assert (common / "Overlay" / "Code" / "Source" / "Platform" /
                "Common" / "Unimplemented" / "Widget_Unimplemented.cpp").is_file()

        # Source untouched without --remove
        assert (gdir / "Code" / "Source" / "Platform" / "Windows").is_dir()

    def test_platform_filter_keeps_common(self, tmp_path):
        gdir = _make_gem(tmp_path)
        out = tmp_path / "out"
        result = self._run(gdir, out, ["-P", "Windows"])
        assert result.exit_code == 0, result.output
        assert (out / "org.test.overlay.widget.windows").is_dir()
        assert (out / "org.test.overlay.widget.common").is_dir()
        assert not (out / "org.test.overlay.widget.linux").exists()

    def test_platform_filter_unknown(self, tmp_path):
        gdir = _make_gem(tmp_path)
        out = tmp_path / "out"
        result = self._run(gdir, out, ["-P", "Provo"])
        assert result.exit_code == 1
        assert "not present" in result.output

    def test_remove_strips_source(self, tmp_path):
        gdir = _make_gem(tmp_path)
        out = tmp_path / "out"
        result = self._run(gdir, out, ["--remove"])
        assert result.exit_code == 0, result.output
        plat = gdir / "Code" / "Source" / "Platform"
        # All platform dirs removed and the empty Platform dir pruned
        assert not plat.exists()

    def test_no_platform_dirs(self, tmp_path):
        from o3de_cli.commands.object import object_group
        gdir = tmp_path / "flat"
        gdir.mkdir()
        _write_json(gdir / "gem.2-0-0.json", {
            "$schemaVersion": "2.0.0",
            "gem": {"name": "org.test.gem.flat", "version": "1.0.0"},
        })
        runner = CliRunner()
        result = runner.invoke(object_group, ["split-platforms", str(gdir)])
        assert result.exit_code == 1
        assert "No Platform" in result.output

    def test_existing_overlay_dir_refused(self, tmp_path):
        gdir = _make_gem(tmp_path)
        out = tmp_path / "out"
        (out / "org.test.overlay.widget.common").mkdir(parents=True)
        result = self._run(gdir, out)
        assert result.exit_code == 1
        assert "already exists" in result.output


class TestSplitPlatformsRegressions:
    def _run(self, gdir, out, extra=()):
        from o3de_cli.commands.object import object_group

        return CliRunner().invoke(
            object_group, ["split-platforms", str(gdir), "-o", str(out), *extra]
        )

    def test_malformed_json_reports_error_code(self, tmp_path):
        """Regression: a malformed object JSON produced a traceback, even with --json."""
        gdir = _make_gem(tmp_path)
        (gdir / "gem.2-0-0.json").write_text('{"gem": ', encoding="utf-8")
        result = self._run(gdir, tmp_path / "out", ["--json"])
        assert result.exit_code == 1, result.output
        assert isinstance(result.exception, SystemExit)
        payload = json.loads(result.output)
        assert payload["status"] == "error"
        assert payload["code"] == "E_INVALID_OBJECT"
        assert "gem.2-0-0.json" in payload["error"]

    def test_malformed_json_console(self, tmp_path):
        gdir = _make_gem(tmp_path)
        (gdir / "gem.2-0-0.json").write_text("not json", encoding="utf-8")
        result = self._run(gdir, tmp_path / "out")
        assert result.exit_code == 1
        assert isinstance(result.exception, SystemExit)
        assert "gem.2-0-0.json" in result.output.replace("\n", "")  # console wraps

    def test_escaping_license_skipped_with_warning(self, tmp_path):
        """Regression: a ``..`` license path copied files from outside the object."""
        gdir = _make_gem(tmp_path)
        (tmp_path / "SECRET.txt").write_text("secret")
        meta = json.loads((gdir / "gem.2-0-0.json").read_text(encoding="utf-8"))
        meta["licenses"].append({"license_identifier": "X", "relative_path": "../SECRET.txt"})
        _write_json(gdir / "gem.2-0-0.json", meta)
        out = tmp_path / "out"
        result = self._run(gdir, out, ["--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert any("../SECRET.txt" in w for w in payload["warnings"])
        assert not (out / "SECRET.txt").exists()
        win = out / "org.test.overlay.widget.windows"
        lic_ids = [
            lic["license_identifier"]
            for lic in json.loads((win / "overlay.json").read_text(encoding="utf-8"))["licenses"]
        ]
        assert lic_ids == ["MIT"]

    def test_escaping_license_warning_console(self, tmp_path):
        gdir = _make_gem(tmp_path)
        (tmp_path / "SECRET.txt").write_text("secret")
        meta = json.loads((gdir / "gem.2-0-0.json").read_text(encoding="utf-8"))
        meta["licenses"].append({"license_identifier": "X", "relative_path": "../SECRET.txt"})
        _write_json(gdir / "gem.2-0-0.json", meta)
        out = tmp_path / "out"
        result = self._run(gdir, out)
        assert result.exit_code == 0, result.output
        assert "../SECRET.txt" in result.output
        assert not (out / "SECRET.txt").exists()

    def test_partial_remove_keeps_common(self, tmp_path):
        """Regression: ``-P Windows --remove`` removed Platform/Common from the source."""
        gdir = _make_gem(tmp_path)
        out = tmp_path / "out"
        result = self._run(gdir, out, ["-P", "Windows", "--remove", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)["data"]
        plat = gdir / "Code" / "Source" / "Platform"
        assert sorted(p.name for p in plat.iterdir()) == ["Common", "Linux"]
        assert payload["removed"] == [str(Path("Code/Source/Platform/Windows"))]
        assert payload["kept"] == [str(Path("Code/Source/Platform/Common"))]
        # The common overlay is still created
        assert (out / "org.test.overlay.widget.common" / "overlay.json").is_file()

    def test_partial_remove_says_common_kept(self, tmp_path):
        gdir = _make_gem(tmp_path)
        result = self._run(gdir, tmp_path / "out", ["-P", "Windows", "--remove"])
        assert result.exit_code == 0, result.output
        assert "Kept in source" in result.output
        assert "Linux" in result.output

    def test_case_clash_refused_before_writing(self, tmp_path):
        """Regression: platform dirs differing only in case shared an overlay dir."""
        gdir = _make_gem(tmp_path)
        (gdir / "Code" / "Tests" / "Platform" / "windows").mkdir(parents=True)
        (gdir / "Code" / "Tests" / "Platform" / "windows" / "t.cmake").write_text("t")
        out = tmp_path / "out"
        result = self._run(gdir, out, ["--json"])
        assert result.exit_code == 1, result.output
        payload = json.loads(result.output)
        assert payload["code"] == "E_OVERLAY_NAME_CLASH"
        assert str(Path("Code/Tests/Platform/windows")) in payload["error"]
        assert str(Path("Code/Source/Platform/Windows")) in payload["error"]
        assert not out.exists()

    def test_register_creates_missing_manifest(self, tmp_path):
        """Regression: ``--register`` silently did nothing without a manifest."""
        from unittest.mock import patch

        mp = tmp_path / "home" / ".o3de" / "o3de_manifest.json"
        gdir = _make_gem(tmp_path)
        out = tmp_path / "out"
        with patch("o3de_cli.core.paths.get_manifest_path", return_value=mp):
            result = self._run(gdir, out, ["--register", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)["data"]
        assert sorted(payload["registered"]) == [
            "org.test.overlay.widget.common",
            "org.test.overlay.widget.linux",
            "org.test.overlay.widget.windows",
        ]
        manifest = json.loads(mp.read_text())
        assert len(manifest["local"]["overlays"]) == 3
