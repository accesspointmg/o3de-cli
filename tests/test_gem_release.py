# O3DE CLI - gem release packaging tests
# SPDX-License-Identifier: Apache-2.0 OR MIT

"""Unit tests for the release helpers behind ``o3de object package``."""

import json
import zipfile
from pathlib import Path
from unittest.mock import patch

from o3de_cli.core.gem_package import (
    package_binary_release,
    package_source_release,
    release_entry_hint,
)


def _gem(tmp_path: Path) -> Path:
    gem = tmp_path / "MyGem"
    (gem / "Code").mkdir(parents=True)
    (gem / "Code" / "a.cpp").write_text("int a;\n")
    (gem / "gem.json").write_text(json.dumps({"gem": {"name": "org.my.gem", "version": "1.0.0"}}))
    return gem


def _releases(gem: Path) -> list:
    return json.loads((gem / "gem.json").read_text())["releases"]


class TestSourceRelease:
    def test_archive_only(self, tmp_path):
        gem = _gem(tmp_path)
        archive, sha, manifest = package_source_release(
            gem, "org.my.gem", "1.0.0", tmp_path / "out"
        )
        assert archive == tmp_path / "out" / "org.my.gem-1.0.0-Source.zip"
        assert len(sha) == 64
        assert manifest is None
        assert "releases" not in json.loads((gem / "gem.json").read_text())

    def test_updates_the_manifest(self, tmp_path):
        gem = _gem(tmp_path)
        archive, sha, manifest = package_source_release(
            gem, "org.my.gem", "1.0.0", tmp_path / "out", fmt="tar.gz", update_manifest=True
        )
        assert archive.name.endswith("-Source.tar.gz")
        assert manifest is not None
        downloads = _releases(gem)[0]["downloads"]
        assert downloads[0]["source"] == archive.resolve().as_uri()
        assert downloads[0]["source_sha256"] == sha


class TestBinaryRelease:
    def _staged(self, tmp_path: Path) -> Path:
        staged = tmp_path / "staged"
        (staged / "bin").mkdir(parents=True)
        (staged / "bin" / "x.dll").write_text("dll")
        return staged

    def test_archive_and_progress(self, tmp_path):
        gem = _gem(tmp_path)
        messages: list[str] = []
        with patch("o3de_cli.core.gem_package.current_platform", return_value="Linux"):
            zip_path, sha, manifest = package_binary_release(
                self._staged(tmp_path),
                gem,
                "org.my.gem",
                "1.0.0",
                tmp_path / "out",
                on_progress=messages.append,
            )
        assert zip_path.name == "org.my.gem-1.0.0-Linux.zip"
        assert zipfile.ZipFile(zip_path).namelist() == ["bin/x.dll"]
        assert manifest is None
        assert messages == ["Archived org.my.gem-1.0.0-Linux.zip"]

    def test_updates_the_manifest(self, tmp_path):
        gem = _gem(tmp_path)
        messages: list[str] = []
        zip_path, sha, manifest = package_binary_release(
            self._staged(tmp_path),
            gem,
            "org.my.gem",
            "1.0.0",
            tmp_path / "out",
            update_manifest=True,
            on_progress=messages.append,
        )
        binary = _releases(gem)[0]["binaries"][0]
        assert binary["binary"] == zip_path.resolve().as_uri()
        assert binary["sha256"] == sha
        assert messages[-1] == f"Updated releases[] in {manifest}"

    def test_no_progress_callback(self, tmp_path):
        zip_path, _sha, _manifest = package_binary_release(
            self._staged(tmp_path), _gem(tmp_path), "org.my.gem", "1.0.0", tmp_path / "out"
        )
        assert zip_path.exists()


def test_release_entry_hints():
    assert release_entry_hint("1.0.0", "abc", source=True) == (
        '{"name": "1.0.0", "downloads": [{"source": "<url>", "source_sha256": "abc"}]}'
    )
    assert release_entry_hint("1.0.0", "abc", source=False) == (
        '{"name": "1.0.0", "binaries": [{"platform": "<Platform>", "binary": "<url>", '
        '"sha256": "abc"}]}'
    )
