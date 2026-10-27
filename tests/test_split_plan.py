# O3DE CLI - split-platforms planning/execution unit tests
# SPDX-License-Identifier: Apache-2.0 OR MIT

"""Unit tests for ``o3de_cli.core.split_platforms`` (no Click involved)."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from o3de_cli.core.split_platforms import (
    ObjectOpError,
    build_overlay_meta,
    copy_licenses,
    create_overlays,
    family_repo_name,
    object_licenses,
    parse_platform_selection,
    parse_user_tags,
    plan_split,
    platform_order,
    register_local_paths,
    register_overlays,
    remove_split_dirs,
    select_platforms,
)
from tests.conftest import _write_json

GEM_DATA = {
    "$schemaVersion": "2.0.0",
    "gem": {"name": "org.test.gem.widget", "version": "1.2.0"},
    "origin": {"name": "Test Org"},
    "licenses": [
        {"license_identifier": "MIT", "relative_path": "LICENSE_MIT.TXT"},
        {"license_identifier": "Apache-2.0", "relative_path": "LICENSE_APACHE2.TXT"},
    ],
}


def _make_gem(root: Path) -> Path:
    plat = root / "Code" / "Source" / "Platform"
    (plat / "Windows").mkdir(parents=True)
    (plat / "Windows" / "a.cmake").write_text("a")
    (plat / "Windows" / "b.cmake").write_text("b")
    (plat / "Linux").mkdir()
    (plat / "Linux" / "l.cmake").write_text("l")
    (plat / "Common" / "Unimplemented").mkdir(parents=True)
    (plat / "Common" / "Unimplemented" / "u.cpp").write_text("u")
    (root / "LICENSE_MIT.TXT").write_text("MIT")
    _write_json(root / "gem.2-0-0.json", GEM_DATA)
    return root


def _manifest(path: Path) -> Path:
    _write_json(path, {"$schemaVersion": "2.0.0", "local": {"overlays": ["x/y"]}})
    return path


class TestOptionParsing:
    def test_user_tags_flatten_and_dedupe(self):
        assert parse_user_tags(["a, b", "a", " ", "c,b"]) == ["a", "b", "c"]

    def test_platform_selection_keeps_duplicates(self):
        assert parse_platform_selection(["Windows,Linux", " Windows "]) == [
            "Windows",
            "Linux",
            "Windows",
        ]

    def test_select_platforms_case_insensitive_and_keeps_common(self):
        detected = {"Common": [Path("c")], "Linux": [Path("l")], "Windows": [Path("w")]}
        assert sorted(select_platforms(detected, ["windows"])) == ["Common", "Windows"]

    def test_select_platforms_unknown(self):
        with pytest.raises(ObjectOpError) as exc:
            select_platforms({"Linux": [Path("l")]}, ["Provo", "linux"])
        assert exc.value.code == "E_PLATFORM_NOT_FOUND"
        assert "Provo" in str(exc.value) and "detected: Linux" in str(exc.value)

    def test_platform_order_common_first(self):
        assert platform_order(["Windows", "Common", "Android"]) == ["Common", "Android", "Windows"]


class TestNaming:
    def test_family_repo_name(self):
        assert family_repo_name("org.o3de.gem.achievements") == "org.o3de.repo.achievements"
        assert family_repo_name("org.t.template.x") == "org.t.repo.x"
        assert family_repo_name("plain") == "plain.repo"


class TestLicenses:
    def test_object_licenses_only_existing_files(self, tmp_path):
        (tmp_path / "LICENSE_MIT.TXT").write_text("MIT")
        data = {"licenses": GEM_DATA["licenses"] + [{"license_identifier": "X"}]}
        assert [lic["license_identifier"] for lic in object_licenses(data, tmp_path)] == ["MIT"]

    def test_copy_licenses_into_subdirectory(self, tmp_path):
        """Regression: a license under a subdirectory used to crash the copy."""
        src = tmp_path / "src"
        (src / "Licenses").mkdir(parents=True)
        (src / "Licenses" / "MIT.txt").write_text("MIT")
        dst = tmp_path / "dst"
        dst.mkdir()
        copy_licenses([{"relative_path": "Licenses/MIT.txt"}], src, dst)
        assert (dst / "Licenses" / "MIT.txt").read_text() == "MIT"


class TestOverlayMeta:
    def _meta(self, **kw):
        args = dict(
            ov_name="org.t.overlay.w.windows",
            plat="Windows",
            is_common=False,
            base_name="org.t.gem.w",
            base_version="1.0.0",
            ov_version="2.0.0",
            type_token="gem",
            precedence=7,
            origin=None,
            licenses=[],
            user_tags=[],
            common_name="org.t.overlay.w.common",
        )
        args.update(kw)
        return build_overlay_meta(**args)

    def test_platform_overlay(self):
        meta = self._meta()
        assert meta["precedence"] == 7
        assert meta["platforms"] == ["Windows"]
        assert meta["dependent"] == {"overlays": ["org.t.overlay.w.common>=2.0.0"]}
        assert meta["extends"] == "org.t.gem.w>=1.0.0"
        assert "origin" not in meta and "licenses" not in meta and "user_tags" not in meta

    def test_common_overlay(self):
        meta = self._meta(plat="Common", is_common=True, user_tags=["t"])
        assert meta["precedence"] == 0
        assert "platforms" not in meta and "dependent" not in meta
        assert meta["user_tags"] == ["t"]

    def test_no_common_means_no_dependency(self):
        assert "dependent" not in self._meta(common_name=None)


class TestPlanSplit:
    def test_plan_contents(self, tmp_path):
        gdir = _make_gem(tmp_path / "widget")
        plan = plan_split(gdir, GEM_DATA, "gem", tags=["x,y"], precedence=3)
        assert [ov.platform for ov in plan.overlays] == ["Common", "Linux", "Windows"]
        assert [ov.files for ov in plan.overlays] == [1, 1, 2]
        assert plan.out_dir == gdir.parent  # default: the object's parent
        assert plan.ov_version == "1.2.0"
        assert plan.common_name == "org.test.overlay.widget.common"
        assert plan.user_tags == ["x", "y"]
        assert [lic["license_identifier"] for lic in plan.licenses] == ["MIT"]
        assert not (gdir.parent / "org.test.overlay.widget.windows").exists()

    def test_dry_run_payload_shape(self, tmp_path):
        gdir = _make_gem(tmp_path / "widget")
        out = tmp_path / "out"
        payload = plan_split(gdir, GEM_DATA, "gem", output=str(out)).dry_run_payload()
        assert payload["object"] == "org.test.gem.widget"
        assert payload["overlays"][0] == {
            "platform": "Common",
            "overlay": "org.test.overlay.widget.common",
            "path": str(out.resolve() / "org.test.overlay.widget.common"),
            "files": 1,
            "common": True,
        }

    def test_platform_filter_and_version_override(self, tmp_path):
        gdir = _make_gem(tmp_path / "widget")
        plan = plan_split(gdir, GEM_DATA, "gem", platforms=["linux"], overlay_version="9.0.0")
        assert [ov.platform for ov in plan.overlays] == ["Common", "Linux"]
        assert plan.ov_version == "9.0.0"

    def test_no_platform_dirs(self, tmp_path):
        with pytest.raises(ObjectOpError) as exc:
            plan_split(tmp_path, GEM_DATA, "gem")
        assert exc.value.code == "E_NO_PLATFORM_DIRS"

    def test_no_common(self, tmp_path):
        (tmp_path / "Platform" / "Mac").mkdir(parents=True)
        plan = plan_split(tmp_path, {"gem": {"name": "org.t.gem.m"}}, "gem")
        assert plan.common_name is None
        assert [ov.platform for ov in plan.overlays] == ["Mac"]


class TestExecuteSplit:
    def test_existing_later_overlay_refused_before_writing(self, tmp_path):
        """Regression: a refused split used to leave earlier overlays behind."""
        gdir = _make_gem(tmp_path / "widget")
        out = tmp_path / "out"
        (out / "org.test.overlay.widget.windows").mkdir(parents=True)
        plan = plan_split(gdir, GEM_DATA, "gem", output=str(out))
        with pytest.raises(ObjectOpError) as exc:
            create_overlays(plan)
        assert exc.value.code == "E_OVERLAY_EXISTS"
        assert sorted(p.name for p in out.iterdir()) == ["org.test.overlay.widget.windows"]

    def test_create_then_remove(self, tmp_path):
        gdir = _make_gem(tmp_path / "widget")
        plan = plan_split(gdir, GEM_DATA, "gem", output=str(tmp_path / "out"))
        created = create_overlays(plan)
        assert [c["platform"] for c in created] == ["Common", "Linux", "Windows"]
        for ov in plan.overlays:
            assert (ov.path / "overlay.json").is_file()
            assert (ov.path / "LICENSE_MIT.TXT").is_file()
        removed = remove_split_dirs(plan)
        assert len(removed) == 3
        assert not (gdir / "Code" / "Source" / "Platform").exists()

    def test_remove_keeps_nonempty_platform_dir(self, tmp_path):
        gdir = _make_gem(tmp_path / "widget")
        plan = plan_split(gdir, GEM_DATA, "gem", platforms=["Windows"])
        remove_split_dirs(plan)
        # Common stays too: Linux remains in the source and may depend on it
        assert sorted(p.name for p in (gdir / "Code" / "Source" / "Platform").iterdir()) == [
            "Common",
            "Linux",
        ]


class TestRegister:
    def test_register_overlays_once(self, tmp_path):
        mp = _manifest(tmp_path / "manifest.json")
        created = [{"platform": "Linux", "overlay": "o.linux", "path": str(tmp_path / "o")}]
        with patch("o3de_cli.core.paths.get_manifest_path", return_value=mp):
            assert register_overlays(created) == ["o.linux"]
            assert register_overlays(created) == []
        overlays = json.loads(mp.read_text())["local"]["overlays"]
        assert overlays == ["x/y", (tmp_path / "o").as_posix()]

    def test_no_manifest(self, tmp_path):
        with patch("o3de_cli.core.paths.get_manifest_path", return_value=tmp_path / "missing"):
            assert register_local_paths("repos", ["a"]) is None
            assert register_overlays([{"overlay": "o", "path": "p"}]) == []


class TestSplitCommandRegression:
    def test_license_in_subdirectory(self, tmp_path):
        """Regression: split-platforms crashed copying a license from a subdirectory."""
        from o3de_cli.commands.object import object_group

        gdir = _make_gem(tmp_path / "widget")
        (gdir / "Licenses").mkdir()
        (gdir / "Licenses" / "MIT.txt").write_text("MIT")
        data = dict(
            GEM_DATA, licenses=[{"license_identifier": "MIT", "relative_path": "Licenses/MIT.txt"}]
        )
        _write_json(gdir / "gem.2-0-0.json", data)
        out = tmp_path / "out"
        result = CliRunner().invoke(object_group, ["split-platforms", str(gdir), "-o", str(out)])
        assert result.exit_code == 0, result.output
        assert (out / "org.test.overlay.widget.linux" / "Licenses" / "MIT.txt").is_file()


# ── Regression tests for the hoist/split bug fixes ──────────────────


class TestInvalidObjectJson:
    def test_malformed_json_is_an_object_error(self, tmp_path):
        """Regression: a malformed object JSON escaped as a JSONDecodeError."""
        from o3de_cli.core.split_platforms import load_object_json

        (tmp_path / "gem.2-0-0.json").write_text('{"gem": {"name": ', encoding="utf-8")
        with pytest.raises(ObjectOpError) as exc:
            load_object_json(tmp_path)
        assert exc.value.code == "E_INVALID_OBJECT"
        assert "gem.2-0-0.json" in str(exc.value)

    def test_non_object_json_is_an_object_error(self, tmp_path):
        from o3de_cli.core.split_platforms import load_object_json

        (tmp_path / "gem.json").write_text("[1, 2]", encoding="utf-8")
        with pytest.raises(ObjectOpError) as exc:
            load_object_json(tmp_path)
        assert exc.value.code == "E_INVALID_OBJECT"
        assert "gem.json" in str(exc.value)

    def test_missing_json_still_file_not_found(self, tmp_path):
        from o3de_cli.core.split_platforms import load_object_json

        with pytest.raises(FileNotFoundError):
            load_object_json(tmp_path)


class TestLicenseContainment:
    def test_object_licenses_drops_escaping_paths_with_warning(self, tmp_path):
        """Regression: ``..`` license paths were accepted when the file existed."""
        root = tmp_path / "obj"
        root.mkdir()
        (root / "LICENSE_MIT.TXT").write_text("MIT")
        (tmp_path / "secret.txt").write_text("secret")
        data = {
            "licenses": [
                {"license_identifier": "MIT", "relative_path": "LICENSE_MIT.TXT"},
                {"license_identifier": "X", "relative_path": "../secret.txt"},
                {"license_identifier": "Y", "relative_path": str(tmp_path / "secret.txt")},
            ]
        }
        warnings: list[str] = []
        kept = object_licenses(data, root, warnings=warnings)
        assert [lic["license_identifier"] for lic in kept] == ["MIT"]
        assert len(warnings) == 2
        assert all("outside" in w for w in warnings)
        assert "../secret.txt" in warnings[0]

    def test_copy_licenses_never_writes_outside_destination(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "LICENSE").write_text("MIT")
        dst = tmp_path / "out" / "dst"
        dst.mkdir(parents=True)
        warnings = copy_licenses(
            [{"relative_path": "LICENSE"}, {"relative_path": "../../src/LICENSE"}], src, dst
        )
        assert (dst / "LICENSE").read_text() == "MIT"
        assert sorted(p.name for p in dst.iterdir()) == ["LICENSE"]
        assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["dst"]
        assert len(warnings) == 1 and "../../src/LICENSE" in warnings[0]

    def test_copy_licenses_rejects_destination_escape(self, tmp_path):
        """The source resolving inside its root is not enough: so must the destination."""
        src = tmp_path / "a" / "b"
        (src / "x").mkdir(parents=True)
        (tmp_path / "a" / "b" / "L").write_text("L")
        # ``x/../L`` resolves inside src, and under dst it stays inside too;
        # ``../b/L`` resolves inside src (a/b/L) but escapes dst.
        dst = tmp_path / "d" / "b2"
        dst.mkdir(parents=True)
        warnings = copy_licenses(
            [{"relative_path": "x/../L"}, {"relative_path": "../b/L"}], src, dst
        )
        assert (dst / "L").read_text() == "L"
        assert not (tmp_path / "d" / "b").exists()
        assert len(warnings) == 1 and "../b/L" in warnings[0]

    def test_plan_split_reports_escaping_license(self, tmp_path):
        gdir = _make_gem(tmp_path / "widget")
        (tmp_path / "secret.txt").write_text("secret")
        data = dict(
            GEM_DATA,
            licenses=GEM_DATA["licenses"]
            + [{"license_identifier": "X", "relative_path": "../secret.txt"}],
        )
        plan = plan_split(gdir, data, "gem", output=str(tmp_path / "out"))
        assert [lic["license_identifier"] for lic in plan.licenses] == ["MIT"]
        assert len(plan.warnings) == 1 and "../secret.txt" in plan.warnings[0]
        create_overlays(plan)
        assert not (tmp_path / "out" / "secret.txt").exists()


class TestRemoveKeepsCommon:
    def test_partial_split_keeps_common_in_source(self, tmp_path):
        """Regression: ``-P Windows --remove`` deleted Platform/Common too."""
        gdir = _make_gem(tmp_path / "widget")
        plan = plan_split(gdir, GEM_DATA, "gem", platforms=["Windows"])
        assert plan.remaining_platforms == ["Linux"]
        assert plan.removes_common_source is False
        removed = remove_split_dirs(plan)
        plat = gdir / "Code" / "Source" / "Platform"
        assert removed == [str(Path("Code/Source/Platform/Windows"))]
        assert sorted(p.name for p in plat.iterdir()) == ["Common", "Linux"]
        assert plan.kept_common_dirs() == [str(Path("Code/Source/Platform/Common"))]

    def test_full_split_removes_common(self, tmp_path):
        gdir = _make_gem(tmp_path / "widget")
        plan = plan_split(gdir, GEM_DATA, "gem", platforms=["Windows,Linux"])
        assert plan.remaining_platforms == []
        assert plan.removes_common_source is True
        assert plan.kept_common_dirs() == []
        assert len(remove_split_dirs(plan)) == 3
        assert not (gdir / "Code" / "Source" / "Platform").exists()

    def test_common_overlay_still_planned_for_partial_split(self, tmp_path):
        gdir = _make_gem(tmp_path / "widget")
        plan = plan_split(gdir, GEM_DATA, "gem", platforms=["Linux"])
        assert [ov.platform for ov in plan.overlays] == ["Common", "Linux"]


class TestOverlayNameClash:
    def test_case_only_platform_names_refused(self, tmp_path):
        """Regression: ``Windows`` and ``windows`` silently shared one overlay."""
        gdir = _make_gem(tmp_path / "widget")
        (gdir / "Code" / "Tests" / "Platform" / "windows").mkdir(parents=True)
        (gdir / "Code" / "Tests" / "Platform" / "windows" / "t.cmake").write_text("t")
        out = tmp_path / "out"
        with pytest.raises(ObjectOpError) as exc:
            plan_split(gdir, GEM_DATA, "gem", output=str(out))
        assert exc.value.code == "E_OVERLAY_NAME_CLASH"
        msg = str(exc.value)
        assert str(Path("Code/Source/Platform/Windows")) in msg
        assert str(Path("Code/Tests/Platform/windows")) in msg
        assert not out.exists()

    def test_clash_outside_selection_is_ignored(self, tmp_path):
        gdir = _make_gem(tmp_path / "widget")
        (gdir / "Code" / "Tests" / "Platform" / "windows").mkdir(parents=True)
        plan = plan_split(gdir, GEM_DATA, "gem", platforms=["Linux"])
        assert [ov.platform for ov in plan.overlays] == ["Common", "Linux"]


class TestEnsureManifest:
    def test_creates_manifest_like_manifest_add(self, tmp_path):
        """Regression: ``--register`` did nothing when there was no manifest."""
        from o3de_cli.core.split_platforms import ensure_manifest

        mp = tmp_path / ".o3de" / "o3de_manifest.json"
        with patch("o3de_cli.core.paths.get_manifest_path", return_value=mp):
            assert ensure_manifest() == mp
            assert ensure_manifest() is None  # already there
            assert register_overlays(
                [{"platform": "Linux", "overlay": "o.linux", "path": str(tmp_path / "o")}]
            ) == ["o.linux"]
        data = json.loads(mp.read_text())
        assert data["$schemaVersion"] == "2.0.0"
        assert data["local"]["overlays"] == [(tmp_path / "o").as_posix()]

    def test_matches_register_in_manifest_skeleton(self, tmp_path):
        from o3de_cli.commands.manifest import register_in_manifest
        from o3de_cli.core.split_platforms import ensure_manifest

        a, b = tmp_path / "a.json", tmp_path / "b.json"
        with patch("o3de_cli.core.paths.get_manifest_path", return_value=a):
            ensure_manifest()
        gem = tmp_path / "g"
        _write_json(gem / "gem.json", {"gem_name": "g"})
        with patch("o3de_cli.commands.manifest.get_manifest_path", return_value=b):
            register_in_manifest(gem, "gem")
        skeleton_b = json.loads(b.read_text())
        skeleton_b["local"] = {}
        assert json.loads(a.read_text()) == skeleton_b
