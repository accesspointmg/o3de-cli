# O3DE CLI - Solver step tests
# SPDX-License-Identifier: Apache-2.0 OR MIT

"""Unit tests for the named steps solve_for_workspace is built from."""

import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from packaging.specifiers import SpecifierSet
from resolvelib.resolvers import ResolutionImpossible

from o3de_cli.core.models import ObjectType
from o3de_cli.core.resolver import ObjectNameVersion, ResolvedObject
from o3de_cli.core.solver import (
    Candidate,
    CandidateStatus,
    Requirement,
    _conflict,
    _containment_children,
    _describe_impossible,
    _local_candidate,
    _match_overlays,
    _overlay_entry,
    _overlay_version_matches,
    _root_requirements,
    _solve_child_dependencies,
    _unmet_child_requirements,
    _with_root_candidate,
)


def _obj(name, version="1.0.0", deps=(), children=(), data=None, otype=ObjectType.GEM):
    obj = ResolvedObject(Path(f"/ws/{name}"), otype, name, version, data or {})
    obj.dependencies = [ObjectNameVersion(d) for d in deps]
    obj.children = list(children)
    return obj


def _cand(name, version="1.0.0"):
    return Candidate(name=name, version=version, object_type=ObjectType.GEM)


def _overlay(name, data, version="1.0.0"):
    return SimpleNamespace(name=name, version=version, path=Path(f"/ov/{name}"), data=data)


class TestRootRequirements:
    def test_root_dependencies_become_requirements(self):
        root = _obj("root", deps=["gem_a>=1.0.0", "gem_b"])
        reqs, pinned = _root_requirements(root, "root", None)
        assert [(r.name, str(r.specifier)) for r in reqs] == [("gem_a", ">=1.0.0"), ("gem_b", "")]
        assert pinned == set()

    def test_pin_replaces_requirement_and_moves_it_last(self):
        root = _obj("root", deps=["gem_a>=1.0.0", "gem_b"])
        reqs, pinned = _root_requirements(root, "root", {"gem_a": "2.0.0"})
        assert [(r.name, str(r.specifier)) for r in reqs] == [("gem_b", ""), ("gem_a", "==2.0.0")]
        assert pinned == {"gem_a"}

    def test_pin_for_an_object_the_root_does_not_require_is_added(self):
        reqs, pinned = _root_requirements(_obj("root"), "root", {"gem_z": "1.0.0"})
        assert [r.name for r in reqs] == ["gem_z"]
        assert pinned == {"gem_z"}

    def test_pin_on_the_root_itself_is_ignored(self):
        root = _obj("root", deps=["gem_a"])
        reqs, pinned = _root_requirements(root, "root", {"root": "1.0.0"})
        assert [r.name for r in reqs] == ["gem_a"]
        assert pinned == set()

    def test_invalid_pin_is_logged_and_skipped(self, caplog):
        root = _obj("root", deps=["gem_a"])
        with caplog.at_level(logging.WARNING, logger="o3de_cli.solver"):
            reqs, pinned = _root_requirements(root, "root", {"gem_a": "not a version!"})
        assert [(r.name, str(r.specifier)) for r in reqs] == [("gem_a", "")]
        assert pinned == set()
        assert "Invalid override version for gem_a: not a version!" in caplog.text


class TestDescribeImpossible:
    def _cause(self, name, spec="", parent=None):
        return SimpleNamespace(requirement=Requirement(name, SpecifierSet(spec)), parent=parent)

    def test_root_and_parent_blame(self):
        error = ResolutionImpossible(
            [self._cause("gem_a", ">=2.0.0"), self._cause("gem_b", "", _cand("gem_a"))]
        )
        assert _describe_impossible(error, "root", set()) == (
            "root requires gem_a>=2.0.0 — no candidate found; "
            "gem_a requires gem_b — no candidate found"
        )

    def test_unmet_pin_is_blamed_on_the_override(self):
        error = ResolutionImpossible([self._cause("gem_a", "==9.0.0")])
        assert _describe_impossible(error, "root", {"gem_a"}) == (
            "override pin requires gem_a==9.0.0 — no candidate found"
        )

    def test_pinned_name_required_by_a_dependency_blames_the_dependency(self):
        error = ResolutionImpossible([self._cause("gem_a", "<1.0.0", _cand("gem_b"))])
        assert _describe_impossible(error, "root", {"gem_a"}).startswith("gem_b requires")

    def test_no_causes_falls_back_to_exception_text(self):
        error = ResolutionImpossible([])
        assert _describe_impossible(error, "root", set()) == str(error)


class TestCandidateHelpers:
    def test_local_candidate(self):
        obj = _obj("gem_a", "1.2.0", deps=["gem_b>=1.0.0"])
        cand = _local_candidate(obj)
        assert (cand.name, cand.version, cand.status) == ("gem_a", "1.2.0", CandidateStatus.LOCAL)
        assert cand.path == obj.path
        assert cand.resolved_object is obj
        assert cand.dependencies == ["gem_b>=1.0.0"]

    def test_with_root_candidate_keeps_mapping_order_and_adds_root(self):
        root = _cand("root")
        mapping = {"gem_b": _cand("gem_b"), "gem_a": _cand("gem_a")}
        result = _with_root_candidate(mapping, root)
        assert list(result) == ["gem_b", "gem_a", "root"]
        assert result["root"] is root
        assert "root" not in mapping

    def test_with_root_candidate_replaces_a_mapped_root_in_place(self):
        root = _cand("root", "2.0.0")
        result = _with_root_candidate({"root": _cand("root"), "gem_a": _cand("gem_a")}, root)
        assert list(result) == ["root", "gem_a"]
        assert result["root"] is root

    def test_conflict(self):
        result = _conflict("asked", _obj("root", "3.0.0"), "boom")
        assert (result.root_name, result.root_version) == ("asked", "3.0.0")
        assert result.conflict_message == "boom"
        assert not result.is_resolved


class TestContainmentChildren:
    def test_recursive_and_minus_dependency_candidates(self):
        grand = _obj("grand")
        child_a = _obj("child_a", children=[grand])
        child_b = _obj("child_b")
        root = _obj("root", children=[child_a, child_b])
        children = _containment_children(root, {"child_b": _cand("child_b")})
        assert list(children) == ["child_a", "grand"]
        assert children["grand"].resolved_object is grand


class TestUnmetChildRequirements:
    def test_skips_provided_and_already_requested(self):
        child = _local_candidate(_obj("child", deps=["gem_a", "gem_b>=1.0.0", "child", "gem_c"]))
        requested = {"gem_c"}
        reqs = _unmet_child_requirements({"child": child}, {"gem_a": _cand("gem_a")}, requested)
        assert [(r.name, str(r.specifier)) for r in reqs] == [("gem_b", ">=1.0.0")]
        assert requested == {"gem_b", "gem_c"}

    def test_same_dependency_from_two_children_requested_once(self):
        c1 = _local_candidate(_obj("c1", deps=["gem_a"]))
        c2 = _local_candidate(_obj("c2", deps=["gem_a>=2.0.0"]))
        reqs = _unmet_child_requirements({"c1": c1, "c2": c2}, {}, set())
        assert [str(r.specifier) for r in reqs] == [""]

    def test_child_without_resolved_object_is_ignored(self):
        assert _unmet_child_requirements({"x": _cand("x")}, {}, set()) == []


class TestSolveChildDependencies:
    def test_no_unmet_dependencies_returns_inputs_without_solving(self):
        rl = MagicMock()
        root = _obj("root")
        cands, children = {"root": _local_candidate(root)}, {}
        assert _solve_child_dependencies(rl, root, cands["root"], [], cands, children) == (
            cands,
            children,
        )
        rl.resolve.assert_not_called()

    def test_re_solves_with_child_requirements_until_fixpoint(self):
        child = _obj("child", deps=["gem_x"])
        root = _obj("root", deps=["gem_a"], children=[child])
        root_cand = _local_candidate(root)
        base_req = Requirement("gem_a")
        rl = MagicMock()
        rl.resolve.return_value = SimpleNamespace(
            mapping={"gem_a": _cand("gem_a"), "gem_x": _cand("gem_x")}
        )
        cands = {"gem_a": _cand("gem_a"), "root": root_cand}
        children = _containment_children(root, cands)

        cands, children = _solve_child_dependencies(
            rl, root, root_cand, [base_req], cands, children
        )

        rl.resolve.assert_called_once()
        assert [r.name for r in rl.resolve.call_args.args[0]] == ["gem_a", "gem_x"]
        assert list(cands) == ["gem_a", "gem_x", "root"]
        assert list(children) == ["child"]

    def test_resolution_errors_propagate(self):
        child = _obj("child", deps=["gem_x"])
        root = _obj("root", children=[child])
        root_cand = _local_candidate(root)
        rl = MagicMock()
        rl.resolve.side_effect = ResolutionImpossible([])
        cands = {"root": root_cand}
        with pytest.raises(ResolutionImpossible):
            _solve_child_dependencies(
                rl, root, root_cand, [], cands, _containment_children(root, cands)
            )


class TestOverlays:
    def test_version_matches(self):
        target = _cand("gem_a", "2.0.0")
        assert _overlay_version_matches(target, ObjectNameVersion("gem_a"))
        assert _overlay_version_matches(target, ObjectNameVersion("gem_a>=1.0.0"))
        assert not _overlay_version_matches(target, ObjectNameVersion("gem_a<2.0.0"))
        # an unparseable candidate version is accepted
        assert _overlay_version_matches(_cand("gem_a", "latest"), ObjectNameVersion("gem_a<1.0"))

    def test_entry_drops_non_strings_and_null_lists(self):
        ov = _overlay(
            "ov",
            {
                "extends_version": ">=1.0.0",
                "precedence": 4,
                "platforms": ["Windows", 3, "Linux"],
                "dependent": {"overlays": ["ov_base>=1.0.0", 7]},
                "user_tags": None,
            },
        )
        entry = _overlay_entry(ov, "gem_a")
        assert (entry.name, entry.extends, entry.extends_version) == ("ov", "gem_a", ">=1.0.0")
        assert entry.precedence == 4
        assert entry.status == CandidateStatus.LOCAL
        assert entry.path == Path("/ov/ov")
        assert entry.platforms == ["Windows", "Linux"]
        assert entry.overlay_deps == ["ov_base"]
        assert entry.user_tags == []

    def test_entry_defaults(self):
        entry = _overlay_entry(_overlay("ov", {"dependent": None}), "gem_a")
        assert entry.precedence == 0
        assert entry.extends_version is None
        assert (entry.platforms, entry.overlay_deps, entry.user_tags) == ([], [], [])

    def test_match_groups_filters_and_sorts(self):
        resolver = SimpleNamespace(
            overlays={
                "late": _overlay("late", {"extends": "gem_a", "precedence": 9}),
                "early": _overlay("early", {"extends": "gem_a>=1.0.0", "precedence": 1}),
                "child": _overlay("child", {"extends": "child_x"}),
                "too_new": _overlay("too_new", {"extends": "gem_a>=5.0.0"}),
                "missing": _overlay("missing", {"extends": "nowhere"}),
                "blank": _overlay("blank", {"extends": ""}),
                "none": _overlay("none", {}),
            }
        )
        overlays = _match_overlays(
            resolver, {"gem_a": _cand("gem_a", "2.0.0")}, {"child_x": _cand("child_x")}
        )
        assert list(overlays) == ["gem_a", "child_x"]
        assert [e.name for e in overlays["gem_a"]] == ["early", "late"]
        assert [e.name for e in overlays["child_x"]] == ["child"]
