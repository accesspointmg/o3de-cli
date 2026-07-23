# SPDX-License-Identifier: Apache-2.0 OR MIT
"""Tests for gem binary packaging + remote binary download (core.gem_package)."""

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from o3de_cli.core.gem_package import (
    TargetInfo,
    download_remote_binary,
    download_remote_source,
    find_release_binary,
    find_release_source,
    generate_config_cmake,
    parse_gem_aliases,
)
from o3de_cli.core.solver import current_platform
from o3de_cli.core.store import IntegrityError


PLATFORM = current_platform()


def _make_package_zip(tmp_path: Path, name: str = "org.test.gem.demo") -> Path:
    """Create a minimal binary package zip with a Config.cmake."""
    zip_path = tmp_path / f"{name}-1.0.0-{PLATFORM}.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr(f"{name}Config.cmake", "# package config\n")
        zf.writestr("gem.json", json.dumps({"gem_name": "Demo", "version": "1.0.0"}))
        zf.writestr(f"bin/{PLATFORM}/profile/Demo.dll", b"\x00")
    return zip_path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _release_data(zip_path: Path, sha256: str | None = None, version: str = "1.0.0") -> dict:
    binary = {"platform": PLATFORM, "binary": zip_path.as_uri()}
    if sha256:
        binary["sha256"] = sha256
    return {"releases": [{"name": version, "binaries": [binary]}]}


class TestFindReleaseBinary:
    def test_exact_version_match(self):
        data = {
            "releases": [
                {"name": "1.0.0", "binaries": [{"platform": PLATFORM, "binary": "a.zip"}]},
                {"name": "2.0.0", "binaries": [{"platform": PLATFORM, "binary": "b.zip"}]},
            ]
        }
        assert find_release_binary(data, "2.0.0", PLATFORM)["binary"] == "b.zip"

    def test_fallback_to_any_release(self):
        data = {
            "releases": [
                {"name": "other", "binaries": [{"platform": PLATFORM, "binary": "x.zip"}]},
            ]
        }
        assert find_release_binary(data, "9.9.9", PLATFORM)["binary"] == "x.zip"

    def test_platform_mismatch(self):
        data = {
            "releases": [
                {"name": "1.0.0", "binaries": [{"platform": "NoSuchOS", "binary": "x.zip"}]},
            ]
        }
        assert find_release_binary(data, "1.0.0", PLATFORM) is None

    def test_legacy_bare_os_matches(self):
        bare_os = PLATFORM.split(".", 1)[0]
        data = {
            "releases": [
                {"name": "1.0.0", "binaries": [{"platform": bare_os, "binary": "legacy.zip"}]},
            ]
        }
        assert find_release_binary(data, "1.0.0", PLATFORM)["binary"] == "legacy.zip"

    def test_exact_arch_token_beats_legacy(self):
        bare_os = PLATFORM.split(".", 1)[0]
        data = {
            "releases": [
                {"name": "1.0.0", "binaries": [
                    {"platform": bare_os, "binary": "legacy.zip"},
                    {"platform": PLATFORM, "binary": "exact.zip"},
                ]},
            ]
        }
        assert find_release_binary(data, "1.0.0", PLATFORM)["binary"] == "exact.zip"

    def test_wrong_arch_rejected(self):
        bare_os = PLATFORM.split(".", 1)[0]
        other_arch = "ARM64" if PLATFORM.endswith("AMD64") else "AMD64"
        data = {
            "releases": [
                {"name": "1.0.0", "binaries": [
                    {"platform": f"{bare_os}.{other_arch}", "binary": "wrong.zip"},
                ]},
            ]
        }
        assert find_release_binary(data, "1.0.0", PLATFORM) is None


class TestPlatformAbi:
    def test_platform_matches_exact_and_legacy(self):
        from o3de_cli.core.solver import platform_matches
        assert platform_matches("Windows.AMD64", "Windows.AMD64")
        assert platform_matches("windows.amd64", "Windows.AMD64")
        assert platform_matches("Windows", "Windows.AMD64")  # legacy bare-OS
        assert not platform_matches("Windows.ARM64", "Windows.AMD64")
        assert not platform_matches("Linux.AMD64", "Windows.AMD64")
        assert not platform_matches("", "Windows.AMD64")

    def test_abi_compatible_non_linux_or_absent(self):
        from o3de_cli.core.solver import abi_compatible
        assert abi_compatible({"platform": "Windows.AMD64"})
        assert abi_compatible({"platform": "Linux.AMD64", "abi": None})
        assert abi_compatible({"platform": "Linux.AMD64", "abi": {}})

    def test_abi_glibc_floor(self, monkeypatch):
        from o3de_cli.core import solver
        monkeypatch.setattr(solver, "host_glibc", lambda: (2, 31))
        assert solver.abi_compatible({"abi": {"glibc": "2.28"}})
        assert solver.abi_compatible({"abi": {"glibc": "2.31"}})
        assert not solver.abi_compatible({"abi": {"glibc": "2.35"}})

    def test_glibc_preference_highest_compatible(self, monkeypatch):
        from o3de_cli.core import solver
        monkeypatch.setattr(solver, "host_glibc", lambda: (2, 31))
        data = {
            "releases": [
                {"name": "1.0.0", "binaries": [
                    {"platform": "Linux.AMD64", "binary": "old.zip", "abi": {"glibc": "2.17"}},
                    {"platform": "Linux.AMD64", "binary": "new.zip", "abi": {"glibc": "2.28"}},
                    {"platform": "Linux.AMD64", "binary": "too-new.zip", "abi": {"glibc": "2.35"}},
                ]},
            ]
        }
        result = find_release_binary(data, "1.0.0", "Linux.AMD64")
        assert result["binary"] == "new.zip"

    def test_current_platform_has_arch(self):
        assert "." in PLATFORM
        os_part, arch = PLATFORM.split(".", 1)
        assert os_part in ("Windows", "Linux", "Mac")
        assert arch in ("AMD64", "ARM64") or arch

    def test_no_releases(self):
        assert find_release_binary({}, "1.0.0", PLATFORM) is None


class TestDownloadRemoteBinary:
    def test_file_url_with_sha256(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))
        zip_path = _make_package_zip(tmp_path)
        data = _release_data(zip_path, _sha256(zip_path))

        dest = download_remote_binary("org.test.gem.demo", "1.0.0", data)
        assert dest == tmp_path / "home" / ".o3de" / "BuiltPackages" / "org.test.gem.demo-1.0.0"
        assert (dest / "org.test.gem.demoConfig.cmake").exists()
        assert (dest / f"bin/{PLATFORM}/profile/Demo.dll").exists()

    def test_sha256_mismatch_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))
        zip_path = _make_package_zip(tmp_path)
        data = _release_data(zip_path, "0" * 64)

        with pytest.raises(IntegrityError):
            download_remote_binary("org.test.gem.demo", "1.0.0", data)

    def test_archive_without_config_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))
        zip_path = tmp_path / "bad.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("readme.txt", "no config here")
        data = _release_data(zip_path)

        with pytest.raises(ValueError, match="Config.cmake"):
            download_remote_binary("org.test.gem.demo", "1.0.0", data)
        assert not (
            tmp_path / "home" / ".o3de" / "BuiltPackages" / "org.test.gem.demo-1.0.0"
        ).exists()

    def test_existing_install_short_circuits(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))
        pkg = tmp_path / "home" / ".o3de" / "BuiltPackages" / "org.test.gem.demo-1.0.0"
        pkg.mkdir(parents=True)
        (pkg / "org.test.gem.demoConfig.cmake").write_text("# existing")

        # No usable URL — must not be touched because the install exists
        dest = download_remote_binary("org.test.gem.demo", "1.0.0", {})
        assert dest == pkg
        assert (pkg / "org.test.gem.demoConfig.cmake").read_text() == "# existing"

    def test_no_release_binary_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))
        with pytest.raises(LookupError):
            download_remote_binary("org.test.gem.demo", "1.0.0", {})


def _make_source_zip(tmp_path: Path, name: str = "org.test.gem.demo") -> Path:
    zip_path = tmp_path / f"{name}-1.0.0-Source.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("gem.json", json.dumps({"gem_name": "Demo", "version": "1.0.0"}))
        zf.writestr("CMakeLists.txt", "# gem cmake\n")
        zf.writestr("Code/Source/Demo.cpp", "// code\n")
    return zip_path


def _source_release_data(zip_path: Path, sha256: str | None = None,
                         version: str = "1.0.0") -> dict:
    download = {"source": zip_path.as_uri()}
    if sha256:
        download["source_sha256"] = sha256
    return {"releases": [{"name": version, "downloads": [download]}]}


class TestDownloadRemoteSource:
    @pytest.fixture(autouse=True)
    def _gems_path(self, tmp_path, monkeypatch):
        from o3de_cli.core import paths
        self.gems_root = tmp_path / "gems"
        monkeypatch.setattr(
            paths, "get_default_gems_path", lambda: self.gems_root,
        )

    def test_file_url_with_sha256(self, tmp_path):
        zip_path = _make_source_zip(tmp_path)
        data = _source_release_data(zip_path, _sha256(zip_path))

        dest = download_remote_source("org.test.gem.demo", "1.0.0", data)
        assert dest == self.gems_root / "org.test.gem.demo-1.0.0"
        assert (dest / "gem.json").is_file()
        assert (dest / "Code/Source/Demo.cpp").is_file()

    def test_sha256_mismatch_raises(self, tmp_path):
        zip_path = _make_source_zip(tmp_path)
        data = _source_release_data(zip_path, "0" * 64)
        with pytest.raises(IntegrityError):
            download_remote_source("org.test.gem.demo", "1.0.0", data)

    def test_archive_without_gem_json_rejected(self, tmp_path):
        zip_path = tmp_path / "bad.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("readme.txt", "not a gem")
        data = _source_release_data(zip_path)
        with pytest.raises(ValueError, match="gem.json"):
            download_remote_source("org.test.gem.demo", "1.0.0", data)
        assert not (self.gems_root / "org.test.gem.demo-1.0.0").exists()

    def test_existing_download_short_circuits(self):
        dest = self.gems_root / "org.test.gem.demo-1.0.0"
        dest.mkdir(parents=True)
        (dest / "gem.json").write_text("{}")
        assert download_remote_source("org.test.gem.demo", "1.0.0", {}) == dest

    def test_no_release_source_raises(self):
        with pytest.raises(LookupError):
            download_remote_source("org.test.gem.demo", "1.0.0", {})

    def test_tar_gz_archive(self, tmp_path):
        import tarfile
        src = tmp_path / "src"
        (src / "Code").mkdir(parents=True)
        (src / "gem.json").write_text("{}")
        (src / "Code" / "a.cpp").write_text("// a")
        tgz = tmp_path / "org.test.gem.demo-1.0.0-Source.tar.gz"
        with tarfile.open(tgz, "w:gz") as tf:
            tf.add(src / "gem.json", "gem.json")
            tf.add(src / "Code" / "a.cpp", "Code/a.cpp")
        data = _source_release_data(tgz)
        dest = download_remote_source("org.test.gem.demo", "1.0.0", data)
        assert (dest / "Code" / "a.cpp").is_file()


class TestFindReleaseSource:
    def test_prefers_version_named_release(self):
        data = {
            "releases": [
                {"name": "1.0.0", "downloads": [{"source": "a.zip"}]},
                {"name": "2.0.0", "downloads": [{"source": "b.zip"}]},
            ]
        }
        assert find_release_source(data, "2.0.0")["source"] == "b.zip"

    def test_fallback_and_none(self):
        data = {"releases": [{"name": "x", "downloads": [{"source": "y.zip"}]}]}
        assert find_release_source(data, "9.9.9")["source"] == "y.zip"
        assert find_release_source({}, "1.0.0") is None
        no_src = {"releases": [{"name": "1.0.0", "downloads": [{"lfs": "z.zip"}]}]}
        assert find_release_source(no_src, "1.0.0") is None


class TestParseGemAliases:
    def test_gem_name_substitution(self, tmp_path):
        (tmp_path / "CMakeLists.txt").write_text(
            "o3de_create_alias(NAME ${gem_name}.Clients NAMESPACE Gem TARGETS Gem::${gem_name})\n"
            "o3de_create_alias(NAME ${gem_name}.Tools NAMESPACE Gem TARGETS Gem::${gem_name}.Editor)\n"
        )
        aliases = parse_gem_aliases(tmp_path, "Demo")
        assert aliases == {"Demo.Clients": "Demo", "Demo.Tools": "Demo.Editor"}


class TestGenerateConfigCmake:
    def _targets(self, tmp_path):
        return [
            TargetInfo("Demo.API", "INTERFACE_LIBRARY", tmp_path),
            TargetInfo("Demo.Static", "STATIC_LIBRARY", tmp_path,
                       [tmp_path / "Demo.Static.lib"]),
            TargetInfo("Demo", "MODULE_LIBRARY", tmp_path, [tmp_path / "Demo.dll"]),
        ]

    def test_uppercase_config_mapping(self, tmp_path):
        text = generate_config_cmake(
            "org.test.gem.demo", self._targets(tmp_path),
            {"Demo.Clients": "Demo"}, "profile", has_include=True,
        )
        # Fork's RuntimeDependencies emulation requires UPPERCASE mapped names
        assert "MAP_IMPORTED_CONFIG_DEBUG PROFILE" in text
        assert "MAP_IMPORTED_CONFIG_RELEASE PROFILE" in text
        assert "MAP_IMPORTED_CONFIG_DEBUG profile" not in text

    def test_structure(self, tmp_path):
        text = generate_config_cmake(
            "org.test.gem.demo", self._targets(tmp_path),
            {"Demo.Clients": "Demo", "Demo.Ghost": "NotATarget"},
            "profile", has_include=True,
        )
        assert "if(TARGET Demo)" in text  # idempotence guard on the module
        assert "NAME Demo.API HEADERONLY IMPORTED" in text
        assert "NAME Demo.Static STATIC IMPORTED" in text
        assert "NAME Demo MODULE IMPORTED" in text
        assert "GEM_MODULE TRUE" in text
        assert 'IMPORTED_LOCATION_PROFILE "${_pkg_bin}/Demo.dll"' in text
        assert "o3de_create_alias(NAME Demo.Clients NAMESPACE Gem TARGETS Gem::Demo)" in text
        # aliases pointing at undefined targets are dropped
        assert "Demo.Ghost" not in text
        assert "set(org.test.gem.demo_FOUND TRUE)" in text
