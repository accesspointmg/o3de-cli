# O3DE Pilot - object split-platforms tests
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
