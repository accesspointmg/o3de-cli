# O3DE CLI - CMake generator step tests
# SPDX-License-Identifier: Apache-2.0 OR MIT

"""Unit tests for the steps of generate_config_cmake (gem_package) and
generate_cmake_manifest (cmake_manifest)."""

import json
import logging
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from o3de_cli.core import cmake_manifest
from o3de_cli.core.cmake_manifest import (
    _apply_artifact_override,
    _manifest_header,
    _type_specific_fields,
    _workspace_objects,
)
from o3de_cli.core.gem_package import (
    TargetInfo,
    _alias_lines,
    _config_guard_target,
    _include_directory_lines,
    _interface_target_lines,
    _link_artifact,
    _module_target_lines,
    _runtime_artifact,
    _static_target_lines,
    generate_config_cmake,
)

MAP = ["    MAP_IMPORTED_CONFIG_DEBUG PROFILE", "    MAP_IMPORTED_CONFIG_RELEASE PROFILE"]


def _t(name, ttype, *artifacts):
    return TargetInfo(name, ttype, Path("/src"), [Path("/build") / a for a in artifacts])


# ---------------------------------------------------------------------------
# generate_config_cmake
# ---------------------------------------------------------------------------


class TestConfigGuard:
    def test_prefers_module_then_static_then_package_name(self):
        mod, static = _t("Mod", "MODULE_LIBRARY"), _t("Lib", "STATIC_LIBRARY")
        assert _config_guard_target("pkg", [static], [mod]) == "Mod"
        assert _config_guard_target("pkg", [static], []) == "Lib"
        assert _config_guard_target("pkg", [], []) == "pkg"


class TestArtifacts:
    def test_first_matching_suffix_case_insensitive(self):
        t = _t("X", "SHARED_LIBRARY", "X.pdb", "X.DLL", "X.so", "X.LIB", "X.a")
        assert _runtime_artifact(t) == "X.DLL"
        assert _link_artifact(t) == "X.LIB"

    def test_none_when_absent(self):
        t = _t("X", "MODULE_LIBRARY", "X.pdb")
        assert _runtime_artifact(t) is None
        assert _link_artifact(t) is None


class TestTargetBlocks:
    def test_include_directories(self):
        assert _include_directory_lines(False) == []
        assert _include_directory_lines(True)[-1] == "            ${_pkg_root}/Include"

    def test_interface_target(self):
        lines = _interface_target_lines(_t("Api", "INTERFACE_LIBRARY"), has_include=False)
        assert lines == [
            "o3de_add_target(",
            "    NAME Api HEADERONLY IMPORTED",
            "    NAMESPACE Gem",
            ")",
            "",
        ]

    def test_static_target_needs_a_link_artifact(self):
        assert _static_target_lines(_t("S", "STATIC_LIBRARY", "S.pdb"), "PROFILE", MAP, True) == []

    def test_static_target(self):
        lines = _static_target_lines(_t("S", "STATIC_LIBRARY", "S.a"), "PROFILE", MAP, True)
        assert "    NAME S STATIC IMPORTED" in lines
        assert "    INCLUDE_DIRECTORIES" in lines
        assert '    IMPORTED_LOCATION_PROFILE "${_pkg_lib}/S.a"' in lines
        assert lines[-4:] == [*MAP, ")", ""]

    def test_module_target_needs_a_runtime_artifact(self):
        assert _module_target_lines(_t("M", "MODULE_LIBRARY", "M.lib"), "PROFILE", MAP) == []

    def test_module_target_has_no_implib(self):
        lines = _module_target_lines(_t("M", "MODULE_LIBRARY", "M.dll", "M.lib"), "PROFILE", MAP)
        assert "    NAME M MODULE IMPORTED" in lines
        assert "        GEM_MODULE TRUE" in lines
        assert not any("IMPORTED_IMPLIB" in line for line in lines)
        assert lines[-4:] == [*MAP, ")", ""]

    def test_shared_target_gets_implib(self):
        lines = _module_target_lines(_t("D", "SHARED_LIBRARY", "D.dll", "D.lib"), "DEBUG", [])
        assert "    NAME D SHARED IMPORTED" in lines
        assert '    IMPORTED_LOCATION_DEBUG "${_pkg_bin}/D.dll"' in lines
        assert '    IMPORTED_IMPLIB_DEBUG "${_pkg_lib}/D.lib"' in lines


class TestAliasLines:
    def test_no_aliases_no_section(self):
        assert _alias_lines({}, []) == []

    def test_sorted_and_undefined_targets_dropped(self):
        targets = [_t("Stars", "MODULE_LIBRARY")]
        lines = _alias_lines(
            {"Stars.Tools": "Ghost", "Stars.Clients": "Stars", "A": "Stars"}, targets
        )
        assert lines == [
            "# Variant aliases (what o3de_enable_gems looks up per project)",
            "o3de_create_alias(NAME A NAMESPACE Gem TARGETS Gem::Stars)",
            "o3de_create_alias(NAME Stars.Clients NAMESPACE Gem TARGETS Gem::Stars)",
            "",
        ]

    def test_header_kept_when_every_alias_is_dropped(self):
        assert len(_alias_lines({"X": "Ghost"}, [])) == 2


class TestGenerateConfigCmake:
    def test_mixed_case_config_maps_uppercase(self):
        text = generate_config_cmake(
            "pkg", [_t("M", "MODULE_LIBRARY", "M.dll")], {}, "Profile", has_include=False
        )
        assert "    MAP_IMPORTED_CONFIG_DEBUG PROFILE" in text
        assert "    MAP_IMPORTED_CONFIG_RELEASE PROFILE" in text
        assert "MAP_IMPORTED_CONFIG_PROFILE" not in text
        assert 'set(_pkg_bin "${_pkg_root}/bin/${_pkg_platform}/Profile")' in text

    def test_blocks_in_type_order_and_footer(self):
        text = generate_config_cmake(
            "pkg",
            [
                _t("M", "MODULE_LIBRARY", "M.dll"),
                _t("S", "STATIC_LIBRARY", "S.lib"),
                _t("I", "INTERFACE_LIBRARY"),
                _t("E", "EXECUTABLE", "E.exe"),
            ],
            {},
            "profile",
            has_include=False,
        )
        assert text.index("NAME I ") < text.index("NAME S ") < text.index("NAME M ")
        assert "NAME E " not in text
        assert text.endswith(
            'set(pkg_FOUND TRUE)\nmessage(STATUS "Using PREBUILT gem pkg from ${_pkg_root}")\n'
        )


# ---------------------------------------------------------------------------
# generate_cmake_manifest
# ---------------------------------------------------------------------------


class TestApplyArtifactOverride:
    def _entry(self):
        return {"artifact": "source", "binary_config_path": ""}

    def test_dict_override_with_path(self):
        entry = self._entry()
        _apply_artifact_override(entry, {"artifact": "local-binary", "path": "/pkgs/Foo"})
        assert entry == {"artifact": "local-binary", "binary_config_path": "/pkgs/Foo"}

    def test_object_override(self):
        entry = self._entry()
        _apply_artifact_override(
            entry, SimpleNamespace(artifact="remote-binary", path=Path("/opt/x"))
        )
        assert entry == {"artifact": "remote-binary", "binary_config_path": "/opt/x"}

    def test_missing_artifact_means_source_and_ignores_path(self):
        for override in (
            {"path": "/p"},
            {"artifact": None, "path": "/p"},
            SimpleNamespace(path="/p"),
        ):
            entry = self._entry()
            _apply_artifact_override(entry, override)
            assert entry == {"artifact": "source", "binary_config_path": ""}

    def test_binary_without_path_keeps_empty_config_path(self):
        entry = self._entry()
        _apply_artifact_override(entry, SimpleNamespace(artifact="local-binary"))
        assert entry == {"artifact": "local-binary", "binary_config_path": ""}


class TestTypeSpecificFields:
    def test_engine(self):
        fields = _type_specific_fields({"api_versions": {"tools": "1.1"}, "build": 3}, "engine")
        assert fields["api_version_tools"] == "1.1"
        assert fields["api_version_editor"] == ""
        assert fields["build"] == 3

    def test_project_and_overlay(self):
        assert _type_specific_fields({"engine": "o3de"}, "project") == {
            "product_name": "",
            "executable_name": "",
            "engine": "o3de",
        }
        assert _type_specific_fields({}, "overlay")["precedence"] == 0

    def test_other_types_add_nothing(self):
        assert _type_specific_fields({"api_versions": "not a dict"}, "gem") == {}


class TestWorkspaceObjects:
    def _write(self, path: Path, data) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")

    def test_walk_order_and_skips(self, tmp_path, caplog):
        self._write(tmp_path / "Gems/b/gem.json", {"gem_name": "b"})
        self._write(tmp_path / "Gems/a/gem.2-0-0.json", {"gem": {"name": "a", "version": "2.0.0"}})
        self._write(tmp_path / "Gems/c/gem.json", "{ broken")
        self._write(tmp_path / "Gems/d/gem.json", {})
        self._write(tmp_path / "Gems/e/gem.json", {"summary": "no name"})
        (tmp_path / "Gems/f").mkdir()
        self._write(tmp_path / "Gems/loose.json", {"gem_name": "loose"})
        self._write(tmp_path / "Engines/o3de/engine.json", {"engine_name": "o3de"})
        self._write(tmp_path / "Repos/r/repo.json", {"repo_name": "r"})

        with caplog.at_level(logging.WARNING, logger="o3de_cli.cmake_manifest"):
            found = list(_workspace_objects(tmp_path))

        assert [(t, d.name, h["name"]) for t, d, _, h in found] == [
            ("engine", "o3de", "o3de"),
            ("gem", "a", "a"),
            ("gem", "b", "b"),
        ]
        assert "No name in" in caplog.text and "gem.json" in caplog.text


class TestManifestHeader:
    def test_keys_and_paths(self, tmp_path):
        class _Fixed:
            @staticmethod
            def now():
                return datetime(2026, 1, 2, 3, 4, 5)

        paths = {t: [f"{t}-path"] for t in ("engine", "project", "gem", "template", "overlay")}
        names = {t: [f"{t}==1.0.0"] for t in paths}
        with patch.object(cmake_manifest, "datetime", _Fixed):
            header = _manifest_header(tmp_path, "/3p", paths, names)
        assert header["resolved_at"] == "2026-01-02T03:04:05"
        assert header["workspace_path"] == tmp_path.as_posix()
        assert header["default_gems_path"] == (tmp_path / "Gems").as_posix()
        assert header["default_third_party_path"] == "/3p"
        assert header["all_repo_paths"] == [] and header["all_repo_names"] == []
        assert header["all_overlay_paths"] is paths["overlay"]
        assert header["all_gem_names"] == ["gem==1.0.0"]
        assert ";" not in header["$comment"]
