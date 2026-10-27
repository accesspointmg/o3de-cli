# O3DE CLI - hoist planning unit tests
# SPDX-License-Identifier: Apache-2.0 OR MIT

"""Unit tests for ``o3de_cli.core.hoist`` planning and its smaller steps.

The end-to-end hoist (real git + filter-repo) is covered by
``test_object_hoist.py``; these tests exercise the plan without git.
"""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from o3de_cli.core.hoist import (
    HoistSource,
    _lfs_pointer_oid,
    build_filter_args,
    build_repo_meta,
    carry_over_lfs,
    execute_hoist,
    locate_source,
    plan_hoist,
    platform_renames,
    register_family_repo,
    require_filter_repo,
)
from o3de_cli.core.split_platforms import ObjectOpError
from tests.conftest import _write_json

GEM_DATA = {
    "gem": {"name": "org.test.gem.widget", "version": "1.2.0"},
    "origin": {"name": "Test Org"},
}


def _source(tmp_path: Path, gem_rel: str = "Gems/Widget", with_platforms: bool = True):
    git_root = tmp_path / "engine"
    obj_root = git_root / gem_rel
    obj_root.mkdir(parents=True)
    if with_platforms:
        plat = obj_root / "Code" / "Source" / "Platform"
        (plat / "Windows").mkdir(parents=True)
        (plat / "Common").mkdir()
        (obj_root / "Code" / "Tests" / "Platform" / "Windows").mkdir(parents=True)
    return HoistSource(obj_root=obj_root, git_root=git_root, gem_rel=gem_rel, branch="dev")


class TestFilterArgs:
    def test_platform_renames_precede_object_rename(self):
        renames = [("A/G/Platform/Win", "Overlays/g.win/Overlay/Platform/Win")]
        assert build_filter_args("A/G", "Gems/G", renames) == [
            "--path",
            "A/G/",
            "--path-rename",
            "A/G/Platform/Win/:Overlays/g.win/Overlay/Platform/Win/",
            "--path-rename",
            "A/G/:Gems/G/",
        ]

    def test_no_object_rename_when_already_in_place(self):
        assert build_filter_args("Gems/G", "Gems/G", []) == ["--path", "Gems/G/"]

    def test_platform_renames(self, tmp_path):
        source = _source(tmp_path)
        plat = source.obj_root / "Code" / "Source" / "Platform"
        detected = {
            "Windows": [plat / "Windows", source.obj_root / "Code/Tests/Platform/Windows"],
            "Common": [plat / "Common"],
        }
        overlay_dirs, renames = platform_renames(detected, source, "widget")
        assert overlay_dirs == {"widget.common": "Common", "widget.windows": "Windows"}
        assert renames == [
            (
                "Gems/Widget/Code/Source/Platform/Common",
                "Overlays/widget.common/Overlay/Code/Source/Platform/Common",
            ),
            (
                "Gems/Widget/Code/Source/Platform/Windows",
                "Overlays/widget.windows/Overlay/Code/Source/Platform/Windows",
            ),
            (
                "Gems/Widget/Code/Tests/Platform/Windows",
                "Overlays/widget.windows/Overlay/Code/Tests/Platform/Windows",
            ),
        ]


class TestPlanHoist:
    def test_defaults(self, tmp_path):
        source = _source(tmp_path, gem_rel="AutomatedTesting/Gem/Widget")
        plan = plan_hoist(source, GEM_DATA, "gem", tags=["a,b", "a"])
        assert plan.family == "widget"
        assert plan.repo_name == "org.test.repo.widget"
        assert plan.out_dir == source.git_root.parent / "o3de-widget"
        assert plan.gem_target == "Gems/Widget"
        assert plan.ov_version == "1.2.0"
        assert plan.user_tags == ["a", "b"]
        assert plan.common_name == "org.test.overlay.widget.common"
        assert [ov.dir_name for ov in plan.overlays] == ["widget.common", "widget.windows"]
        assert plan.filter_args[:2] == ["--path", "AutomatedTesting/Gem/Widget/"]
        assert plan.filter_args[-1] == "AutomatedTesting/Gem/Widget/:Gems/Widget/"
        assert len(plan.renames) == 3
        assert not plan.out_dir.exists()

    def test_overrides(self, tmp_path):
        source = _source(tmp_path)
        out = tmp_path / "fam"
        plan = plan_hoist(
            source,
            GEM_DATA,
            "gem",
            output=str(out),
            repo_name="custom.repo",
            overlay_version="3.0.0",
            precedence=4,
        )
        assert plan.out_dir == out.resolve()
        assert plan.repo_name == "custom.repo"
        assert plan.ov_version == "3.0.0"
        assert plan.precedence == 4

    @pytest.mark.parametrize(
        ("type_token", "subdir"),
        [("gem", "Gems"), ("template", "Templates"), ("project", "Projects"), ("engine", "Gems")],
    )
    def test_object_subdir_by_type(self, tmp_path, type_token, subdir):
        source = _source(tmp_path, with_platforms=False)
        plan = plan_hoist(source, {type_token: {"name": "org.t.x.widget"}}, type_token)
        assert plan.gem_target == f"{subdir}/Widget"

    def test_no_platforms(self, tmp_path):
        source = _source(tmp_path, with_platforms=False)
        plan = plan_hoist(source, GEM_DATA, "gem")
        assert plan.overlays == [] and plan.renames == [] and plan.common_name is None
        assert plan.filter_args == ["--path", "Gems/Widget/"]

    def test_dry_run_payload(self, tmp_path):
        source = _source(tmp_path)
        payload = plan_hoist(source, GEM_DATA, "gem", output=str(tmp_path / "f")).dry_run_payload()
        assert list(payload) == ["object", "repo", "path", "branch", "gem_dir", "overlays"]
        assert payload["branch"] == "dev"
        assert payload["overlays"] == [
            {
                "dir": "Overlays/widget.common",
                "platform": "Common",
                "overlay": "org.test.overlay.widget.common",
            },
            {
                "dir": "Overlays/widget.windows",
                "platform": "Windows",
                "overlay": "org.test.overlay.widget.windows",
            },
        ]


class TestRepoMeta:
    def test_children_and_remote(self, tmp_path):
        plan = plan_hoist(_source(tmp_path), GEM_DATA, "gem")
        lic = [{"license_identifier": "MIT", "relative_path": "LICENSE"}]
        meta = build_repo_meta(plan, lic, "gem.2-0-0.json", ["widget.windows"])
        assert meta["repo"]["name"] == "org.test.repo.widget"
        assert meta["repo"]["display_name"] == "Widget Family Repo"
        assert meta["origin"] == {"name": "Test Org"}
        assert meta["licenses"] == lic
        assert meta["children"]["gems"] == ["Gems/Widget/gem.2-0-0.json"]
        assert meta["children"]["overlays"] == ["Overlays/widget.windows/overlay.json"]
        assert list(meta["children"]) == [
            "engines",
            "projects",
            "gems",
            "templates",
            "repos",
            "overlays",
        ]
        assert all(v == [] for v in meta["remote"].values())

    def test_template_child_without_origin_or_licenses(self, tmp_path):
        source = _source(tmp_path, with_platforms=False)
        plan = plan_hoist(source, {"template": {"name": "org.t.template.widget"}}, "template")
        meta = build_repo_meta(plan, [], "template.json", [])
        assert meta["children"]["templates"] == ["Templates/Widget/template.json"]
        assert meta["children"]["gems"] == []
        assert "origin" not in meta and "licenses" not in meta


class TestSteps:
    def test_lfs_pointer_oid(self, tmp_path):
        ptr = tmp_path / "p"
        ptr.write_text("version https://git-lfs.github.com/spec/v1\noid sha256:abc123\nsize 3\n")
        assert _lfs_pointer_oid(ptr) == "abc123"
        ptr.write_text("not a pointer")
        assert _lfs_pointer_oid(ptr) is None
        assert _lfs_pointer_oid(tmp_path / "missing") is None

    def test_carry_over_lfs_without_lfs_files(self, tmp_path):
        src, out = tmp_path / "src", tmp_path / "out"
        src.mkdir()
        out.mkdir()
        (src / ".gitattributes").write_text("*.bin filter=lfs\n")
        with patch("o3de_cli.core.hoist.run_git", return_value=(0, "")) as git:
            assert carry_over_lfs(src, out) == 0
        git.assert_called_once_with(["lfs", "ls-files", "--name-only"], cwd=out)
        assert (out / ".gitattributes").read_text() == "*.bin filter=lfs\n"

    def test_require_filter_repo_missing(self):
        with patch.dict(sys.modules, {"git_filter_repo": None}):
            with pytest.raises(ObjectOpError) as exc:
                require_filter_repo()
        assert exc.value.code == "E_NO_FILTER_REPO"

    def test_execute_refuses_existing_output_before_git(self, tmp_path):
        out = tmp_path / "fam"
        out.mkdir()
        plan = plan_hoist(_source(tmp_path), GEM_DATA, "gem", output=str(out))
        with patch("o3de_cli.core.hoist.run_git") as git:
            with pytest.raises(ObjectOpError) as exc:
                execute_hoist(plan)
        assert exc.value.code == "E_OUTPUT_EXISTS"
        git.assert_not_called()

    def test_locate_source_not_a_git_repo(self, tmp_path):
        with patch("o3de_cli.core.hoist.run_git", return_value=(128, "fatal")):
            with pytest.raises(ObjectOpError) as exc:
                locate_source(tmp_path, None)
        assert exc.value.code == "E_NOT_A_GIT_REPO"

    def test_locate_source_detached_head(self, tmp_path):
        obj = tmp_path / "Gems" / "W"
        obj.mkdir(parents=True)
        replies = iter([(0, str(tmp_path)), (0, "HEAD")])
        with patch("o3de_cli.core.hoist.run_git", side_effect=lambda *a, **k: next(replies)):
            with pytest.raises(ObjectOpError) as exc:
                locate_source(obj, None)
        assert exc.value.code == "E_NO_BRANCH"

    def test_locate_source_real_repo(self, tmp_path):
        subprocess.run(["git", "init", "-q", "-b", "trunk"], cwd=tmp_path, check=True)
        obj = tmp_path / "Gems" / "W"
        obj.mkdir(parents=True)
        source = locate_source(obj.resolve(), "given")
        assert source.gem_rel == "Gems/W"
        assert source.branch == "given"

    def test_register_family_repo(self, tmp_path):
        mp = tmp_path / "manifest.json"
        _write_json(mp, {"local": {}})
        with patch("o3de_cli.core.paths.get_manifest_path", return_value=mp):
            assert register_family_repo(tmp_path / "fam") is True
            assert register_family_repo(tmp_path / "fam") is False
        assert json.loads(mp.read_text())["local"]["repos"] == [(tmp_path / "fam").as_posix()]


# ── Regression tests for the hoist bug fixes ────────────────────────


def _git_repo_source(tmp_path: Path) -> HoistSource:
    """A real git repo where Windows/Common are tracked but Linux is only untracked."""
    source = _source(tmp_path)
    plat = source.obj_root / "Code" / "Source" / "Platform"
    (plat / "Windows" / "w.cmake").write_text("w")
    (plat / "Common" / "c.cpp").write_text("c")
    (source.obj_root / "Code" / "Tests" / "Platform" / "Windows" / "t.cmake").write_text("t")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q", "-b", "dev"], cwd=source.git_root, check=True)
    subprocess.run([*git, "add", "-A"], cwd=source.git_root, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "i"], cwd=source.git_root, check=True)
    (plat / "Linux").mkdir()
    (plat / "Linux" / "untracked.cmake").write_text("l")
    return source


class TestPlanSkipsPlatformsWithoutHistory:
    def test_untracked_platform_is_skipped_in_plan(self, tmp_path):
        """Regression: dry-run listed overlays that execution then skipped."""
        source = _git_repo_source(tmp_path)
        plan = plan_hoist(source, GEM_DATA, "gem")
        assert [ov.dir_name for ov in plan.overlays] == ["widget.common", "widget.windows"]
        assert [(ov.dir_name, ov.platform) for ov in plan.skipped] == [("widget.linux", "Linux")]
        assert "dev" in plan.skipped[0].skip_reason
        payload = plan.dry_run_payload()
        assert [o["platform"] for o in payload["overlays"]] == ["Common", "Windows"]
        assert payload["skipped"] == [
            {
                "dir": "Overlays/widget.linux",
                "platform": "Linux",
                "overlay": "org.test.overlay.widget.linux",
                "reason": plan.skipped[0].skip_reason,
            }
        ]

    def test_history_checked_on_the_hoisted_branch(self, tmp_path):
        source = _git_repo_source(tmp_path)
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run([*git, "checkout", "-q", "-b", "other"], cwd=source.git_root, check=True)
        subprocess.run([*git, "add", "-A"], cwd=source.git_root, check=True)
        subprocess.run([*git, "commit", "-q", "-m", "linux"], cwd=source.git_root, check=True)
        source.branch = "other"
        assert [ov.platform for ov in plan_hoist(source, GEM_DATA, "gem").overlays] == [
            "Common",
            "Linux",
            "Windows",
        ]
        source.branch = "dev"
        assert [ov.platform for ov in plan_hoist(source, GEM_DATA, "gem").skipped] == ["Linux"]

    def test_skipped_common_is_not_a_dependency(self, tmp_path):
        source = _git_repo_source(tmp_path)
        common = source.obj_root / "Code" / "Source" / "Platform" / "Common"
        subprocess.run(["git", "rm", "-q", "-r", "--cached", str(common)], cwd=source.git_root)
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "rm"],
            cwd=source.git_root,
            check=True,
        )
        plan = plan_hoist(source, GEM_DATA, "gem")
        assert [ov.platform for ov in plan.skipped] == ["Common", "Linux"]
        assert plan.common_name is None

    def test_renames_still_cover_skipped_platforms(self, tmp_path):
        """filter-repo arguments are unchanged, so re-hoists stay deterministic."""
        source = _git_repo_source(tmp_path)
        plan = plan_hoist(source, GEM_DATA, "gem")
        assert any("Platform/Linux" in src for src, _dst in plan.renames)


class TestPlanOverlayNameClash:
    def test_case_only_platform_names_refused(self, tmp_path):
        """Regression: ``Windows`` and ``windows`` mapped to one overlay dir."""
        source = _source(tmp_path)
        (source.obj_root / "Code" / "Editor" / "Platform" / "windows").mkdir(parents=True)
        with pytest.raises(ObjectOpError) as exc:
            plan_hoist(source, GEM_DATA, "gem", output=str(tmp_path / "fam"))
        assert exc.value.code == "E_OVERLAY_NAME_CLASH"
        assert str(Path("Code/Source/Platform/Windows")) in str(exc.value)
        assert str(Path("Code/Editor/Platform/windows")) in str(exc.value)
        assert not (tmp_path / "fam").exists()


class TestExecuteCleanup:
    """Regression: a failed hoist left its half-made output behind."""

    def _plan(self, tmp_path, out):
        return plan_hoist(_source(tmp_path), GEM_DATA, "gem", output=str(out))

    @staticmethod
    def _fake_clone(out: Path) -> None:
        (out / ".git" / "objects" / "pack").mkdir(parents=True)
        pack = out / ".git" / "objects" / "pack" / "pack-1.pack"
        pack.write_text("x")
        pack.chmod(0o444)  # git marks packs read-only (blocks rmtree on Windows)

    def test_clone_failure_removes_partial_clone(self, tmp_path):
        out = tmp_path / "fam"
        plan = self._plan(tmp_path, out)

        def git(args, cwd=None, env=None):
            self._fake_clone(out)
            return 128, "fatal: boom"

        with patch("o3de_cli.core.hoist.run_git", side_effect=git):
            with pytest.raises(ObjectOpError) as exc:
                execute_hoist(plan)
        assert exc.value.code == "E_CLONE_FAILED"
        assert not out.exists()

    def test_filter_failure_removes_read_only_clone(self, tmp_path):
        out = tmp_path / "fam"
        plan = self._plan(tmp_path, out)
        failed = subprocess.CompletedProcess([], 1, stdout="", stderr="bad")
        with (
            patch("o3de_cli.core.hoist.clone_source", side_effect=lambda p: self._fake_clone(out)),
            patch("o3de_cli.core.hoist.subprocess.run", return_value=failed),
        ):
            with pytest.raises(ObjectOpError) as exc:
                execute_hoist(plan)
        assert exc.value.code == "E_FILTER_FAILED"
        assert not out.exists()

    def test_commit_failure_removes_output(self, tmp_path):
        out = tmp_path / "fam"
        plan = self._plan(tmp_path, out)
        with (
            patch("o3de_cli.core.hoist.clone_source", side_effect=lambda p: self._fake_clone(out)),
            patch("o3de_cli.core.hoist.run_filter_repo"),
            patch("o3de_cli.core.hoist.carry_over_lfs", return_value=0),
            patch("o3de_cli.core.hoist.write_repo_metadata", return_value=[]),
            patch(
                "o3de_cli.core.hoist.commit_metadata",
                side_effect=ObjectOpError("Metadata commit failed: x", "E_COMMIT_FAILED"),
            ),
        ):
            with pytest.raises(ObjectOpError) as exc:
                execute_hoist(plan)
        assert exc.value.code == "E_COMMIT_FAILED"
        assert not out.exists()

    def test_unexpected_error_also_cleans_up(self, tmp_path):
        out = tmp_path / "fam"
        plan = self._plan(tmp_path, out)
        with (
            patch("o3de_cli.core.hoist.clone_source", side_effect=lambda p: self._fake_clone(out)),
            patch("o3de_cli.core.hoist.run_filter_repo"),
            patch("o3de_cli.core.hoist.carry_over_lfs", side_effect=OSError("disk full")),
        ):
            with pytest.raises(OSError):
                execute_hoist(plan)
        assert not out.exists()

    def test_created_parents_removed_existing_parent_kept(self, tmp_path):
        keep = tmp_path / "keep"
        keep.mkdir()
        (keep / "mine.txt").write_text("mine")
        out = keep / "new" / "deeper" / "fam"
        plan = self._plan(tmp_path, out)

        def clone(p):
            self._fake_clone(out)
            raise ObjectOpError("Clone failed: x", "E_CLONE_FAILED")

        with patch("o3de_cli.core.hoist.clone_source", side_effect=clone):
            with pytest.raises(ObjectOpError):
                execute_hoist(plan)
        assert sorted(p.name for p in keep.iterdir()) == ["mine.txt"]

    def test_success_keeps_output(self, tmp_path):
        out = tmp_path / "fam"
        plan = self._plan(tmp_path, out)
        with (
            patch("o3de_cli.core.hoist.clone_source", side_effect=lambda p: out.mkdir()),
            patch("o3de_cli.core.hoist.run_filter_repo"),
            patch("o3de_cli.core.hoist.carry_over_lfs", return_value=0),
            patch("o3de_cli.core.hoist.write_repo_metadata", return_value=["ov"]),
            patch("o3de_cli.core.hoist.commit_metadata"),
        ):
            result = execute_hoist(plan)
        assert result.overlays == ["ov"]
        assert out.is_dir()


class TestRegisterCreatesManifest:
    def test_register_family_repo_without_manifest(self, tmp_path):
        """Regression: ``--register`` did nothing when there was no manifest."""
        from o3de_cli.core.split_platforms import ensure_manifest

        mp = tmp_path / ".o3de" / "o3de_manifest.json"
        with patch("o3de_cli.core.paths.get_manifest_path", return_value=mp):
            ensure_manifest()
            assert register_family_repo(tmp_path / "fam") is True
        assert json.loads(mp.read_text())["local"]["repos"] == [(tmp_path / "fam").as_posix()]
