# O3DE Pilot CLI - Override / candidate enumeration tests
# SPDX-License-Identifier: Apache-2.0 OR MIT

"""Tests for per-object resolve overrides (version pins + artifact forms)."""

from pathlib import Path

import pytest

from o3de_cli.core.models import ObjectType, ObjectOverride, WorkspaceMeta
from o3de_cli.core.resolver import Resolver, ResolvedObject, ObjectNameVersion
from o3de_cli.core.solver import (
    CandidateStatus,
    list_candidates,
    solve_for_workspace,
)


def _obj(name: str, version: str, path: str, deps: list[str] | None = None,
         obj_type: ObjectType = ObjectType.GEM) -> ResolvedObject:
    resolved = ResolvedObject(
        path=Path(path),
        object_type=obj_type,
        name=name,
        version=version,
        data={},
    )
    for dep in deps or []:
        resolved.dependencies.append(ObjectNameVersion(dep))
    return resolved


@pytest.fixture
def resolver_with_versions(tmp_path):
    """Resolver with a root project + a gem present in two versions."""
    resolver = Resolver(manifest_path=tmp_path / "manifest.json")

    root = _obj("org.test.project.app", "1.0.0", tmp_path / "app",
                deps=["org.test.gem.physics>=1.0.0"],
                obj_type=ObjectType.PROJECT)
    physics5 = _obj("org.test.gem.physics", "5.0.0", tmp_path / "physics5")
    physics4 = _obj("org.test.gem.physics", "4.0.0", tmp_path / "physics4")
    debug5 = _obj("org.test.gem.physicsdebug", "5.0.0", tmp_path / "debug5",
                  deps=["org.test.gem.physics>=5.0.0"])

    # Newest-wins map (what resolve() would produce)
    resolver.objects = {
        root.name: root,
        physics5.name: physics5,
        debug5.name: debug5,
    }
    # Multi-version registry retains the alternate
    resolver.objects_all = {
        root.name: {root.version: root},
        physics5.name: {"5.0.0": physics5, "4.0.0": physics4},
        debug5.name: {debug5.version: debug5},
    }
    return resolver


class TestListCandidates:
    def test_all_versions_enumerated(self, resolver_with_versions):
        cands = list_candidates("org.test.gem.physics", resolver_with_versions)
        versions = [c.version for c in cands]
        assert versions == ["5.0.0", "4.0.0"]  # newest first
        assert all(c.status == CandidateStatus.LOCAL for c in cands)

    def test_specifier_filters(self, resolver_with_versions):
        cands = list_candidates(
            "org.test.gem.physics", resolver_with_versions, specifier=">=5.0.0",
        )
        assert [c.version for c in cands] == ["5.0.0"]

    def test_artifacts_annotated(self, resolver_with_versions):
        cands = list_candidates("org.test.gem.physics", resolver_with_versions)
        for c in cands:
            assert c.local_binary_path is None  # no install layout on disk
            assert c.remote_binary is False

    def test_local_binary_detected(self, resolver_with_versions, tmp_path):
        install = Path(resolver_with_versions.objects_all[
            "org.test.gem.physics"]["4.0.0"].path) / "install"
        install.mkdir(parents=True)
        (install / "org.test.gem.physicsConfig.cmake").write_text("# config")

        cands = list_candidates("org.test.gem.physics", resolver_with_versions)
        by_version = {c.version: c for c in cands}
        assert by_version["4.0.0"].local_binary_path == install
        assert by_version["5.0.0"].local_binary_path is None


class TestSolveWithPins:
    def test_default_picks_newest(self, resolver_with_versions):
        result = solve_for_workspace("org.test.project.app", resolver_with_versions)
        assert result.is_resolved
        assert result.candidates["org.test.gem.physics"].version == "5.0.0"

    def test_pin_selects_alternate(self, resolver_with_versions):
        result = solve_for_workspace(
            "org.test.project.app", resolver_with_versions,
            overrides={"org.test.gem.physics": "4.0.0"},
        )
        assert result.is_resolved
        assert result.candidates["org.test.gem.physics"].version == "4.0.0"

    def test_impossible_pin_reports_conflict(self, resolver_with_versions):
        result = solve_for_workspace(
            "org.test.project.app", resolver_with_versions,
            overrides={"org.test.gem.physics": "9.9.9"},
        )
        assert not result.is_resolved
        assert "org.test.gem.physics" in result.conflict_message

    def test_pin_conflicting_with_dependent(self, resolver_with_versions):
        # Make the root depend on physicsdebug, which needs physics>=5
        root = resolver_with_versions.objects["org.test.project.app"]
        root.dependencies.append(ObjectNameVersion("org.test.gem.physicsdebug>=5.0.0"))

        result = solve_for_workspace(
            "org.test.project.app", resolver_with_versions,
            overrides={"org.test.gem.physics": "4.0.0"},
        )
        # physicsdebug 5.0.0 requires physics>=5.0.0 but the pin forces 4.0.0
        assert not result.is_resolved


class TestResolverObjectsAll:
    def test_duplicate_names_retained(self, tmp_path):
        resolver = Resolver(manifest_path=tmp_path / "manifest.json")

        for version, dirname in (("5.0.0", "v5"), ("4.0.0", "v4")):
            obj_dir = tmp_path / dirname
            obj_dir.mkdir()
            (obj_dir / "gem.json").write_text(
                '{"gem_name": "org.test.gem.dup", "version": "%s"}' % version
            )

        resolver._resolve_object(tmp_path / "v5", ObjectType.GEM)
        resolver._resolve_object(tmp_path / "v4", ObjectType.GEM)

        assert resolver.objects["org.test.gem.dup"].version == "5.0.0"
        assert set(resolver.objects_all["org.test.gem.dup"]) == {"5.0.0", "4.0.0"}

    def test_newer_replaces_but_keeps_old(self, tmp_path):
        resolver = Resolver(manifest_path=tmp_path / "manifest.json")

        for version, dirname in (("4.0.0", "v4"), ("5.0.0", "v5")):
            obj_dir = tmp_path / dirname
            obj_dir.mkdir()
            (obj_dir / "gem.json").write_text(
                '{"gem_name": "org.test.gem.dup", "version": "%s"}' % version
            )

        # Older first, newer replaces
        resolver._resolve_object(tmp_path / "v4", ObjectType.GEM)
        resolver._resolve_object(tmp_path / "v5", ObjectType.GEM)

        assert resolver.objects["org.test.gem.dup"].version == "5.0.0"
        assert set(resolver.objects_all["org.test.gem.dup"]) == {"5.0.0", "4.0.0"}


class TestWorkspaceMetaOverrides:
    def test_overrides_round_trip(self):
        meta = WorkspaceMeta.model_validate({
            "$schema": "https://canonical.o3de.org/o3de-workspace-2.0.0.json",
            "$schemaVersion": "2.0.0",
            "workspace": {"name": "ws"},
            "created": "2026-07-22T00:00:00",
            "overrides": {
                "org.test.gem.physics": {
                    "version": "4.0.0",
                    "artifact": "local-binary",
                    "path": "C:/installs/physics-4.0.0",
                },
            },
        })
        assert meta.overrides["org.test.gem.physics"].version == "4.0.0"
        assert meta.overrides["org.test.gem.physics"].artifact == "local-binary"

        dumped = meta.model_dump()
        again = WorkspaceMeta.model_validate(dumped)
        assert again.overrides["org.test.gem.physics"].path == "C:/installs/physics-4.0.0"

    def test_overrides_default_empty(self):
        meta = WorkspaceMeta.model_validate({
            "$schema": "https://canonical.o3de.org/o3de-workspace-2.0.0.json",
            "$schemaVersion": "2.0.0",
            "workspace": {"name": "ws"},
            "created": "2026-07-22T00:00:00",
        })
        assert meta.overrides == {}

    def test_override_default_artifact(self):
        override = ObjectOverride(version="1.2.3")
        assert override.artifact == "source"
        assert override.path is None
