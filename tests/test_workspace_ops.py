# O3DE CLI - workspace operation tests
# SPDX-License-Identifier: Apache-2.0 OR MIT

"""Unit tests for o3de_cli.core.workspace_ops.

These cover the logic behind ``workspace create/update/override/build``
directly, without Click or the console: selection rules, overlay
ordering, solve assembly, overlay-set changes, overrides and the CMake
command plan.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from o3de_cli.core.models import ObjectOverride, ObjectType
from o3de_cli.core.solver import Candidate, CandidateStatus, OverlayEntry, SolveResult
from o3de_cli.core.workspace_ops import (
    BuildLayout,
    BuildOptions,
    CreateReporter,
    CreateRequest,
    OverlayChange,
    OverlaySelection,
    OverrideRequest,
    ResyncPlan,
    SolveDeps,
    SolveFailedError,
    WorkspaceAssembly,
    WorkspaceOpError,
    add_selected_overlays,
    affected_bases,
    apply_overlay_order,
    apply_override,
    apply_resync,
    attributions_change,
    build_layout,
    build_source_paths,
    build_workspace_meta,
    change_overlays,
    check_overlay_removals,
    clear_attribution_records,
    cmake_build_command,
    cmake_configure_command,
    detect_source_type,
    engines_needing_closure,
    expand_platform_selection,
    explicit_overlay_tuples,
    group_overlays_by_base,
    local_objects,
    make_override,
    manifest_name_for_path,
    merge_engine_closure,
    ordered_overlay_paths,
    overlays_for_base,
    parse_overlay_order,
    parse_tag_selection,
    plan_cmake_commands,
    plan_resync,
    prune_overlay_order,
    read_workspace_meta,
    relink_changed_objects,
    resolve_overlay_additions,
    root_gem_target,
    secondary_engine_objects,
    select_overlays_for_platforms,
    select_root,
    solve_create_dependencies,
    solve_with_overrides,
    sources_object_map,
    workspace_root_name,
    write_workspace_meta,
)
from tests.conftest import _write_json

# ---------------------------------------------------------------------------
# Helpers and fakes
# ---------------------------------------------------------------------------


def _cand(name, path=None, status=CandidateStatus.LOCAL, obj_type=ObjectType.GEM):
    return Candidate(name, "1.0.0", obj_type, status=status, path=Path(path) if path else None)


def _result(candidates=(), children=(), overlays=None, conflict=""):
    return SolveResult(
        root_name="root",
        root_version="1.0.0",
        candidates={c.name: c for c in candidates},
        children={c.name: c for c in children},
        overlays=overlays or {},
        conflict_message=conflict,
    )


def _entry(name, extends="base", precedence=0, platforms=(), deps=(), tags=(), path=None):
    return OverlayEntry(
        name=name,
        version="1.0.0",
        extends=extends,
        extends_version=None,
        precedence=precedence,
        path=Path(path) if path else None,
        platforms=list(platforms),
        overlay_deps=list(deps),
        user_tags=list(tags),
    )


def _overlay_dir(root: Path, dirname: str, name: str, extends: str, precedence=0, deps=()):
    d = root / dirname
    data = {
        "$schemaVersion": "2.0.0",
        "overlay": {"name": name, "version": "1.0.0"},
        "extends": extends,
        "precedence": precedence,
    }
    if deps:
        data["dependent"] = {"overlays": list(deps)}
    _write_json(d / "overlay.json", data)
    return d


class _Obj:
    def __init__(self, path):
        self.path = Path(path) if path else None
        self.data: dict = {}


class FakeResolver:
    """Minimal stand-in for core.resolver.Resolver."""

    def __init__(self, objects=None, overlays=None):
        self.objects = {n: _Obj(p) for n, p in (objects or {}).items()}
        self.overlays = {n: _Obj(p) for n, p in (overlays or {}).items()}
        self.manifest_remotes: list = []
        self.resolve_calls = 0
        self.auto_installed: list = []

    def resolve(self):
        self.resolve_calls += 1

    def auto_install_missing(self, store, confirm, progress_callback):
        progress_callback("installing x", 1, 1)
        return self.auto_installed


class RecordingReporter(CreateReporter):
    def __init__(self):
        self.events: list = []

    def progress(self, message):
        self.events.append(("progress", message))

    def overlay_unavailable(self, name):
        self.events.append(("unavailable", name))

    def remote_installed(self, count):
        self.events.append(("installed", count))

    def auto_install_needs_store(self):
        self.events.append(("needs_store",))

    def root_not_registered(self):
        self.events.append(("not_registered",))

    def unresolved(self, root_name, remote_count, unknown_count):
        self.events.append(("unresolved", root_name, remote_count, unknown_count))


def _solver(*results):
    """A solve_for_workspace double returning *results* in order."""
    calls: list = []

    def solve(**kwargs):
        calls.append(kwargs)
        return results[len(calls) - 1]

    solve.calls = calls  # type: ignore[attr-defined]
    return solve


def _meta(tmp_path, **sources):
    meta = build_workspace_meta(
        name="ws",
        root_path=tmp_path / "root",
        root_type="project",
        sources={k: v for k, v in sources.items()},
    )
    return meta


# ---------------------------------------------------------------------------
# Selection parsing
# ---------------------------------------------------------------------------


class TestSelectionParsing:
    def test_tags_split_dedupe_case_insensitive(self):
        assert parse_tag_selection(("mobile,Mobile", " vr ,", "VR,x")) == ["mobile", "vr", "x"]

    def test_tags_empty(self):
        assert parse_tag_selection(()) is None
        assert parse_tag_selection((" , ",)) is None

    def test_platforms_all_disables_filtering(self):
        assert expand_platform_selection(("Windows", "all")) is None

    def test_platforms_host_and_dedupe(self):
        with patch("platform.system", return_value="Darwin"):
            assert expand_platform_selection(("host,mac", "Linux")) == ["Mac", "Linux"]

    def test_platforms_none(self):
        assert expand_platform_selection(()) is None


class TestSelectOverlays:
    def _groups(self):
        return {
            "ui": [
                _entry("common"),
                _entry("win", platforms=["Windows"], deps=["common"]),
                _entry("linux", platforms=["Linux"]),
                _entry("agnostic"),
            ],
            "phys": [_entry("mobile", extends="phys", platforms=["Android"], tags=["mobile"])],
        }

    def _names(self, groups):
        return {e.name for entries in groups.values() for e in entries}

    def test_no_criteria_returns_input(self):
        groups = self._groups()
        assert select_overlays_for_platforms(groups, None) is groups

    def test_platform_pulls_dependencies_and_agnostic(self):
        got = select_overlays_for_platforms(self._groups(), ["windows"])
        # common is a dependency target: only pulled via win
        assert self._names(got) == {"win", "common", "agnostic"}

    def test_dependency_target_not_selected_alone(self):
        got = select_overlays_for_platforms(self._groups(), ["Linux"])
        assert self._names(got) == {"linux", "agnostic"}
        assert "phys" not in got

    def test_tags_or_platforms(self):
        got = select_overlays_for_platforms(self._groups(), ["Linux"], selected_tags=["MOBILE"])
        assert self._names(got) == {"linux", "agnostic", "mobile"}

    def test_include_and_exclude(self):
        got = select_overlays_for_platforms(
            self._groups(), ["Linux"], include={"win"}, exclude={"common", "agnostic"}
        )
        # exclude wins even over a dependency of a forced include
        assert self._names(got) == {"linux", "win"}

    def test_exclude_without_criteria(self):
        got = select_overlays_for_platforms(self._groups(), None, exclude={"win"})
        assert self._names(got) == {"common", "linux", "agnostic", "mobile"}


# ---------------------------------------------------------------------------
# Overlay ordering
# ---------------------------------------------------------------------------


class TestOverlayOrdering:
    def test_ordered_paths_precedence_then_name(self):
        entries = [("b", Path("/b"), 1), ("a", Path("/a"), 1), ("z", Path("/z"), 0)]
        assert ordered_overlay_paths(entries, []) == [
            (Path("/z"), 0),
            (Path("/a"), 1),
            (Path("/b"), 2),
        ]

    def test_ordered_paths_explicit_first(self):
        entries = [("a", Path("/a"), 0), ("b", Path("/b"), 1), ("c", Path("/c"), 2)]
        got = ordered_overlay_paths(entries, ["c", "a", "missing"])
        assert [p.name for p, _ in got] == ["c", "a", "b"]
        assert [i for _, i in got] == [0, 1, 2]

    def test_parse_order_maps_to_base(self, tmp_path):
        a = _overlay_dir(tmp_path, "a", "ov.a", "org.gem.ui>=1.0")
        b = _overlay_dir(tmp_path, "b", "ov.b", "org.gem.ui")
        got = parse_overlay_order(["ov.b, ov.a"], {"ov.a": a, "ov.b": b}, "nf")
        assert got == {"org.gem.ui": ["ov.b", "ov.a"]}

    @pytest.mark.parametrize(
        ("value", "message"),
        [
            ("ov.a", "--overlay-order needs at least two comma-separated overlay names"),
            ("ov.a,ov.x", "not here: ov.x"),
            ("ov.a,ov.c", "--overlay-order: overlays must all extend the same object"),
        ],
    )
    def test_parse_order_errors(self, tmp_path, value, message):
        paths = {
            "ov.a": _overlay_dir(tmp_path, "a", "ov.a", "org.gem.ui"),
            "ov.c": _overlay_dir(tmp_path, "c", "ov.c", "org.gem.other"),
        }
        with pytest.raises(WorkspaceOpError) as exc:
            parse_overlay_order([value], paths, "not here")
        assert exc.value.message == message
        assert exc.value.code == "E_OVERLAY_ORDER"

    def test_prune_order(self):
        order = {"ui": ["a", "b"], "phys": ["c"]}
        assert prune_overlay_order(order, {"b", "c"}) == {"ui": ["a"]}
        assert order == {"ui": ["a", "b"], "phys": ["c"]}  # input untouched

    def test_apply_order_renumbers(self, tmp_path):
        a = _overlay_dir(tmp_path, "a", "ov.a", "org.gem.ui")
        b = _overlay_dir(tmp_path, "b", "ov.b", "org.gem.ui")
        other = _overlay_dir(tmp_path, "o", "ov.o", "org.gem.x")
        tuples = [(a, 5, "org.gem.ui"), (b, 9, "org.gem.ui"), (other, 1, None)]
        got, order_map = apply_overlay_order(tuples, ("ov.b,ov.a",))
        assert order_map == {"org.gem.ui": ["ov.b", "ov.a"]}
        assert got == [
            (b.resolve(), 0, "org.gem.ui"),
            (a.resolve(), 1, "org.gem.ui"),
            (other, 2, None),
        ]

    def test_apply_order_noop_without_overlays(self):
        assert apply_overlay_order([], ("x,y",)) == ([], {})

    def test_apply_order_error_message(self, tmp_path):
        a = _overlay_dir(tmp_path, "a", "ov.a", "org.gem.ui")
        with pytest.raises(WorkspaceOpError, match="not selected for this workspace: ov.zz"):
            apply_overlay_order([(a, 0, None)], ("ov.a,ov.zz",))

    def test_group_by_base_skips_non_extending(self, tmp_path):
        a = _overlay_dir(tmp_path, "a", "ov.a", "org.gem.ui", precedence=3)
        loose = tmp_path / "loose"
        loose.mkdir()
        got = group_overlays_by_base({"ov.a": str(a), "loose": str(loose)})
        assert got == {"org.gem.ui": [("ov.a", a, 3)]}

    def test_overlays_for_base_uses_meta_order(self, tmp_path):
        a = _overlay_dir(tmp_path, "a", "ov.a", "gem", precedence=1)
        b = _overlay_dir(tmp_path, "b", "ov.b", "gem", precedence=2)
        meta = _meta(tmp_path, overlays={"ov.a": str(a), "ov.b": str(b), "ov.none": ""})
        assert [p.name for p, _ in overlays_for_base(meta, "gem")] == ["a", "b"]
        meta.overlay_order = {"gem": ["ov.b"]}
        assert [p.name for p, _ in overlays_for_base(meta, "gem")] == ["b", "a"]


# ---------------------------------------------------------------------------
# workspace create: assembly pieces
# ---------------------------------------------------------------------------


class TestCreatePieces:
    def test_select_root_precedence(self, tmp_path):
        for kind in ("engine", "project", "gem"):
            _write_json(tmp_path / kind / f"{kind}.json", {})
        eng, proj, gem = (str(tmp_path / k) for k in ("engine", "project", "gem"))
        assert select_root(gem, proj, eng) == ((tmp_path / "gem").resolve(), ObjectType.GEM)
        assert select_root(None, proj, eng)[1] == ObjectType.PROJECT
        assert select_root(None, None, eng)[1] == ObjectType.ENGINE

    def test_secondary_engine(self, tmp_path):
        eng = tmp_path / "eng"
        _write_json(eng / "engine.json", {"engine": {"name": "org.e", "version": "1.0.0"}})
        assert secondary_engine_objects(None, tmp_path) == {}
        assert secondary_engine_objects(str(eng), eng.resolve()) == {}
        got = secondary_engine_objects(str(eng), tmp_path / "project")
        assert list(got.values()) == [(eng.resolve(), ObjectType.ENGINE)]

    def test_explicit_overlay_tuples(self, tmp_path):
        assert explicit_overlay_tuples([str(tmp_path / "a")], no_overlays=True) == []
        got = explicit_overlay_tuples([str(tmp_path / "a"), str(tmp_path / "b")], False)
        assert got == [((tmp_path / "a").resolve(), 0, None), ((tmp_path / "b").resolve(), 1, None)]

    def test_manifest_name_for_path(self, tmp_path):
        resolver = FakeResolver(objects={"org.x": tmp_path / "x", "org.np": None})
        assert manifest_name_for_path(resolver, (tmp_path / "x").resolve()) == "org.x"
        assert manifest_name_for_path(resolver, tmp_path / "other") == "other"

    def test_local_objects_skips_remote_and_root(self, tmp_path):
        result = _result(
            candidates=[
                _cand("root", tmp_path / "root"),
                _cand("gem", tmp_path / "gem"),
                _cand("far", status=CandidateStatus.REMOTE),
            ],
            children=[_cand("child", tmp_path / "child")],
        )
        got = local_objects(result, exclude_path=(tmp_path / "root").resolve())
        assert set(got) == {"gem", "child"}
        assert set(local_objects(result)) == {"root", "gem", "child"}

    def test_engine_closure(self, tmp_path):
        resolved = {
            "eng.a": (tmp_path / "a", ObjectType.ENGINE),
            "eng.b": (tmp_path / "b", ObjectType.ENGINE),
            "gem": (tmp_path / "g", ObjectType.GEM),
        }
        root_solve = _result(candidates=[_cand("eng.b", tmp_path / "b")])
        assert engines_needing_closure(resolved, root_solve) == ["eng.a"]

        groups = {"base": [_entry("ov1", precedence=5)]}
        eng_solve = _result(
            candidates=[
                _cand("gem", tmp_path / "other"),  # already resolved: kept
                _cand("new", tmp_path / "new"),
                _cand("root", tmp_path / "root"),  # the root: skipped
            ],
            overlays={"base": [_entry("ov1"), _entry("ov0", precedence=1)], "x": [_entry("ovx")]},
        )
        merge_engine_closure(resolved, groups, eng_solve, (tmp_path / "root").resolve())
        assert resolved["gem"] == (tmp_path / "g", ObjectType.GEM)
        assert "new" in resolved and "root" not in resolved
        assert [e.name for e in groups["base"]] == ["ov0", "ov1"]
        assert [e.name for e in groups["x"]] == ["ovx"]

    def test_add_selected_overlays_dedupes_explicit(self, tmp_path):
        tuples = [((tmp_path / "a").resolve(), 0, None)]
        groups = {
            "base": [
                _entry("a", path=tmp_path / "a", precedence=7),
                _entry("b", path=tmp_path / "b", precedence=3),
                _entry("c"),  # no path: skipped
            ]
        }
        add_selected_overlays(tuples, groups, OverlaySelection())
        assert tuples == [
            ((tmp_path / "a").resolve(), 0, None),
            ((tmp_path / "b").resolve(), 3, "base"),
        ]


# ---------------------------------------------------------------------------
# workspace create: dependency phase
# ---------------------------------------------------------------------------


class TestSolveCreateDependencies:
    def _deps(self, resolvers, solve, installed=None):
        it = iter(resolvers)
        installed = installed or {}
        return SolveDeps(
            resolver_cls=lambda: next(it),
            solve=solve,
            install_overlay=lambda name: installed.get(name),
        )

    def _run(self, tmp_path, request=None, resolvers=None, solve=None, installed=None):
        root = (tmp_path / "root").resolve()
        request = request or CreateRequest(root_path=root)
        assembly = WorkspaceAssembly(resolved_objects={}, overlay_tuples=[])
        reporter = RecordingReporter()
        resolvers = resolvers or [FakeResolver(objects={"org.root": root})]
        solve_create_dependencies(
            request, assembly, self._deps(resolvers, solve, installed), reporter
        )
        return assembly, reporter

    def test_unregistered_root_skips_solve(self, tmp_path):
        solve = _solver()
        assembly, reporter = self._run(tmp_path, resolvers=[FakeResolver()], solve=solve)
        assert solve.calls == []
        assert assembly.solve_result is None
        assert reporter.events[-1] == ("not_registered",)

    def test_resolves_local_objects_and_overlays(self, tmp_path):
        root = (tmp_path / "root").resolve()
        result = _result(
            candidates=[_cand("org.root", root), _cand("gem", tmp_path / "gem")],
            overlays={"gem": [_entry("ov", extends="gem", path=tmp_path / "ov")]},
        )
        solve = _solver(result)
        assembly, reporter = self._run(tmp_path, solve=solve)
        assert assembly.solve_result is result
        assert set(assembly.resolved_objects) == {"gem"}
        assert assembly.overlay_tuples == [((tmp_path / "ov").resolve(), 0, "gem")]
        assert solve.calls[0]["root_name"] == "org.root"
        assert solve.calls[0]["store"] is None
        assert ("progress", "Solving dependencies...") in reporter.events
        assert not any(e[0] == "unresolved" for e in reporter.events)

    def test_no_overlays_skips_solved_overlays(self, tmp_path):
        root = (tmp_path / "root").resolve()
        result = _result(overlays={"x": [_entry("ov", path=tmp_path / "ov")]})
        assembly, _ = self._run(
            tmp_path, request=CreateRequest(root_path=root, no_overlays=True), solve=_solver(result)
        )
        assert assembly.overlay_tuples == []

    def test_solve_failure_raises_with_label(self, tmp_path):
        with pytest.raises(SolveFailedError) as exc:
            self._run(tmp_path, solve=_solver(_result(conflict="boom")))
        assert exc.value.label == "Dependency resolution failed"
        assert exc.value.detail == "boom"
        assert exc.value.code == "E_SOLVE_CONFLICT"

    def test_secondary_engine_gets_own_closure(self, tmp_path):
        root = (tmp_path / "root").resolve()
        resolver = FakeResolver(objects={"org.root": root, "org.eng": tmp_path / "eng"})
        request = CreateRequest(root_path=root)
        assembly = WorkspaceAssembly(
            resolved_objects={"org.eng": (tmp_path / "eng", ObjectType.ENGINE)}, overlay_tuples=[]
        )
        solve = _solver(_result(), _result(candidates=[_cand("engine.gem", tmp_path / "eg")]))
        reporter = RecordingReporter()
        solve_create_dependencies(request, assembly, self._deps([resolver], solve), reporter)
        assert [c["root_name"] for c in solve.calls] == ["org.root", "org.eng"]
        assert "engine.gem" in assembly.resolved_objects
        assert ("progress", "Solving engine org.eng...") in reporter.events

    def test_secondary_engine_failure_label(self, tmp_path):
        root = (tmp_path / "root").resolve()
        resolver = FakeResolver(objects={"org.root": root, "org.eng": tmp_path / "eng"})
        assembly = WorkspaceAssembly(
            resolved_objects={"org.eng": (tmp_path / "eng", ObjectType.ENGINE)}, overlay_tuples=[]
        )
        solve = _solver(_result(), _result(conflict="nope"))
        with pytest.raises(SolveFailedError) as exc:
            solve_create_dependencies(
                CreateRequest(root_path=root),
                assembly,
                self._deps([resolver], solve),
                RecordingReporter(),
            )
        assert exc.value.label == "Engine dependency resolution failed"

    def test_missing_include_installed_then_reresolved(self, tmp_path):
        root = (tmp_path / "root").resolve()
        first, second = (
            FakeResolver(objects={"org.root": root}),
            FakeResolver(objects={"org.root": root}),
        )
        request = CreateRequest(
            root_path=root, selection=OverlaySelection(include={"ov.remote", "ov.gone"})
        )
        assembly, reporter = self._run(
            tmp_path,
            request=request,
            resolvers=[first, second],
            solve=_solver(_result()),
            installed={"ov.remote": tmp_path / "installed"},
        )
        assert ("unavailable", "ov.gone") in reporter.events
        assert second.resolve_calls == 1  # re-resolved after the install

    def test_remote_and_unknown_reported(self, tmp_path):
        result = _result(
            candidates=[
                _cand("far", status=CandidateStatus.REMOTE),
                _cand("lost", status=CandidateStatus.UNKNOWN),
            ]
        )
        request = CreateRequest(root_path=(tmp_path / "root").resolve(), auto_install=True)
        _, reporter = self._run(tmp_path, request=request, solve=_solver(result))
        assert ("needs_store",) in reporter.events
        assert reporter.events[-1] == ("unresolved", "org.root", 1, 1)

    def test_auto_install_resolves_again(self, tmp_path):
        root = (tmp_path / "root").resolve()
        resolver = FakeResolver(objects={"org.root": root})
        resolver.auto_installed = [{"name": "far"}]
        first = _result(candidates=[_cand("far", status=CandidateStatus.REMOTE)])
        second = _result(candidates=[_cand("far", tmp_path / "far")])
        request = CreateRequest(
            root_path=root,
            engine_path=str(tmp_path / "eng"),
            project_path=str(tmp_path / "proj"),
            include_store=True,
            auto_install=True,
        )
        assembly = WorkspaceAssembly(
            resolved_objects={"stale": (tmp_path, ObjectType.GEM)}, overlay_tuples=[]
        )
        reporter = RecordingReporter()

        class Store:
            def refresh_sync(self, remotes):
                pass

        with patch("o3de_cli.core.store.Store", Store):
            solve_create_dependencies(
                request, assembly, self._deps([resolver], _solver(first, second)), reporter
            )
        assert assembly.solve_result is second
        # The original selection plus what the install made local; the
        # project is not added again under its directory name
        assert set(assembly.resolved_objects) == {"stale", "far"}
        assert ("installed", 1) in reporter.events
        assert ("progress", "installing x") in reporter.events
        assert not any(e[0] == "unresolved" for e in reporter.events)

    def test_auto_install_resolve_failure(self, tmp_path):
        root = (tmp_path / "root").resolve()
        resolver = FakeResolver(objects={"org.root": root})
        resolver.auto_installed = [{"name": "far"}]
        first = _result(candidates=[_cand("far", status=CandidateStatus.REMOTE)])
        request = CreateRequest(root_path=root, include_store=True, auto_install=True)

        class Store:
            def refresh_sync(self, remotes):
                pass

        with patch("o3de_cli.core.store.Store", Store), pytest.raises(SolveFailedError) as exc:
            self._run(
                tmp_path,
                request=request,
                resolvers=[resolver],
                solve=_solver(first, _result(conflict="x")),
            )
        assert exc.value.label == "Re-solve after install failed"

    class _Store:
        def refresh_sync(self, remotes):
            pass

    def test_auto_install_keeps_explicit_engine_and_its_closure(self, tmp_path):
        """A gem-rooted workspace keeps its --engine, the engine's closure and
        the overlays of every solve after auto-installing."""
        root = (tmp_path / "gem").resolve()
        eng = (tmp_path / "eng").resolve()
        resolver = FakeResolver(objects={"org.gem": root, "org.eng": eng})
        resolver.auto_installed = [{"name": "far"}]
        request = CreateRequest(
            root_path=root, engine_path=str(eng), include_store=True, auto_install=True
        )
        assembly = WorkspaceAssembly(
            resolved_objects={"org.eng": (eng, ObjectType.ENGINE)}, overlay_tuples=[]
        )
        engine_closure = _result(
            candidates=[
                _cand("org.eng", eng, obj_type=ObjectType.ENGINE),
                _cand("engine.gem", tmp_path / "eg"),
            ],
            overlays={"engine.gem": [_entry("ov.eng", extends="engine.gem", path=tmp_path / "oe")]},
        )
        solve = _solver(
            _result(candidates=[_cand("org.gem", root), _cand("far", status=CandidateStatus.REMOTE)]),
            engine_closure,
            _result(
                candidates=[_cand("org.gem", root), _cand("far", tmp_path / "far")],
                overlays={"far": [_entry("ov.far", extends="far", path=tmp_path / "of")]},
            ),
            engine_closure,
        )
        with patch("o3de_cli.core.store.Store", self._Store):
            solve_create_dependencies(
                request, assembly, self._deps([resolver], solve), RecordingReporter()
            )
        assert set(assembly.resolved_objects) == {"org.eng", "engine.gem", "far"}
        assert assembly.resolved_objects["org.eng"] == (eng, ObjectType.ENGINE)
        assert {p for p, *_ in assembly.overlay_tuples} == {
            (tmp_path / "oe").resolve(),
            (tmp_path / "of").resolve(),
        }

    def test_auto_install_does_not_add_the_root_twice(self, tmp_path):
        """With -e and -p the project is the root: it is not re-added under
        its directory name, and objects already selected are not duplicated
        under another name."""
        proj = (tmp_path / "proj").resolve()
        eng = (tmp_path / "eng").resolve()
        resolver = FakeResolver(objects={"org.proj": proj, "org.eng": eng})
        resolver.auto_installed = [{"name": "far"}]
        request = CreateRequest(
            root_path=proj,
            engine_path=str(eng),
            project_path=str(proj),
            explicit_overlays=(str(tmp_path / "explicit"),),
            include_store=True,
            auto_install=True,
        )
        assembly = WorkspaceAssembly(
            resolved_objects={"org.eng": (eng, ObjectType.ENGINE)},
            overlay_tuples=explicit_overlay_tuples(request.explicit_overlays, False),
        )
        proj_cand = _cand("org.proj", proj, obj_type=ObjectType.PROJECT)
        eng_cand = _cand("org.eng", eng, obj_type=ObjectType.ENGINE)
        first = _result(
            candidates=[proj_cand, eng_cand, _cand("far", status=CandidateStatus.REMOTE)],
            overlays={"org.eng": [_entry("ov.e", extends="org.eng", path=tmp_path / "oe")]},
        )
        second = _result(
            candidates=[
                proj_cand,
                eng_cand,
                _cand("far", tmp_path / "far"),
                # The same directory under another name: not linked twice
                _cand("alias.eng", eng, obj_type=ObjectType.ENGINE),
            ]
        )
        with patch("o3de_cli.core.store.Store", self._Store):
            solve_create_dependencies(
                request, assembly, self._deps([resolver], _solver(first, second)), RecordingReporter()
            )
        assert set(assembly.resolved_objects) == {"org.eng", "far"}
        assert all(p.resolve() != proj for p, _t in assembly.resolved_objects.values())
        # The explicit overlay and the first solve's overlay are both kept
        assert [p for p, *_ in assembly.overlay_tuples] == [
            (tmp_path / "explicit").resolve(),
            (tmp_path / "oe").resolve(),
        ]


# ---------------------------------------------------------------------------
# workspace update
# ---------------------------------------------------------------------------


class TestResync:
    def test_detect_source_type(self, tmp_path):
        for kind in ("engine", "project"):
            _write_json(tmp_path / kind / f"{kind}.json", {})
        (tmp_path / "bare").mkdir()
        assert detect_source_type(tmp_path / "engine") == ObjectType.ENGINE
        assert detect_source_type(tmp_path / "project") == ObjectType.PROJECT
        assert detect_source_type(tmp_path / "bare") == ObjectType.GEM

    def test_plan_uses_recorded_root(self, tmp_path):
        meta = _meta(
            tmp_path,
            engines={"e": str(tmp_path / "e")},
            gems={"g": str(tmp_path / "g"), "empty": ""},
            overlays={"o": str(tmp_path / "o")},
        )
        plan = plan_resync(tmp_path, meta, [str(tmp_path / "extra")])
        assert plan.root_source == tmp_path / "root"
        assert plan.root_type == ObjectType.PROJECT
        assert plan.sources == [tmp_path / "e", tmp_path / "g"]
        assert plan.overlays == [tmp_path / "o", (tmp_path / "extra").resolve()]

    def test_plan_root_type_fallback(self, tmp_path):
        meta = _meta(tmp_path)
        meta.root_object = None
        meta.root_type = "bogus"
        plan = plan_resync(tmp_path, meta, [])
        assert plan.root_source == tmp_path
        assert plan.root_type == ObjectType.ENGINE

    def test_apply_resync_drives_workspace(self, tmp_path):
        _write_json(tmp_path / "e" / "engine.json", {})
        calls: list = []

        class FakeWorkspace:
            def __init__(self, **kwargs):
                calls.append(("init", kwargs))

            def add_resolved_object(self, name, path, obj_type):
                calls.append(("object", name, obj_type))

            def add_overlay(self, path, precedence, extends=None):
                calls.append(("overlay", path.name, precedence))

            def update(self):
                calls.append(("update",))

        plan = ResyncPlan(
            tmp_path / "e", ObjectType.ENGINE, [tmp_path / "e"], [Path("o1"), Path("o2")]
        )
        apply_resync(tmp_path, plan, workspace_cls=FakeWorkspace)
        assert calls == [
            (
                "init",
                {
                    "root_path": tmp_path,
                    "root_object_path": tmp_path / "e",
                    "root_object_type": ObjectType.ENGINE,
                },
            ),
            ("object", "e", ObjectType.ENGINE),
            ("overlay", "o1", 0),
            ("overlay", "o2", 1),
            ("update",),
        ]

    def test_resync_uses_recorded_names_types_and_overlay_bases(self, tmp_path):
        """Re-sync links objects under the names (and types) recorded in
        workspace.json, so overlays find the object they extend."""
        eng = tmp_path / "engine-src"  # directory name != object name
        _write_json(eng / "engine.2-0-0.json", {})  # no legacy engine.json
        ov = _overlay_dir(tmp_path, "ov-src", "org.ov", "org.e.o3de>=1.0.0")
        meta = _meta(tmp_path, engines={"org.e.o3de": str(eng)}, overlays={"org.ov": str(ov)})
        calls: list = []

        class FakeWorkspace:
            def __init__(self, **kwargs):
                pass

            def add_resolved_object(self, name, path, obj_type):
                calls.append(("object", name, obj_type))

            def add_overlay(self, path, precedence, extends=None):
                calls.append(("overlay", path.name, precedence, extends))

            def update(self):
                pass

        apply_resync(tmp_path, plan_resync(tmp_path, meta, []), workspace_cls=FakeWorkspace)
        assert calls == [
            ("object", "org.e.o3de", ObjectType.ENGINE),
            ("overlay", "ov-src", 0, "org.e.o3de"),
        ]


class TestOverlayChanges:
    def test_attributions_change(self, tmp_path):
        meta = _meta(tmp_path)
        assert not attributions_change(meta, None)
        assert not attributions_change(meta, "workspace")
        assert attributions_change(meta, "object")
        meta.attributions = "object"
        assert attributions_change(meta, "workspace")

    def test_additions_pull_dependencies(self, tmp_path):
        dep = _overlay_dir(tmp_path, "dep", "ov.dep", "gem")
        top = _overlay_dir(tmp_path, "top", "ov.top", "gem", deps=["ov.dep>=1.0", "ov.have"])
        resolver = FakeResolver(overlays={"ov.top": top, "ov.dep": dep})
        got = resolve_overlay_additions(
            ["ov.top"], {"ov.have": "x"}, lambda: resolver, lambda n: None
        )
        assert got == {"ov.top": str(top), "ov.dep": str(dep)}

    def test_additions_remote_install(self, tmp_path):
        ov = _overlay_dir(tmp_path, "r", "ov.r", "gem")
        resolvers = iter([FakeResolver(), FakeResolver(overlays={"ov.r": ov})])
        downloads: list = []
        got = resolve_overlay_additions(
            ["ov.r"], {}, lambda: next(resolvers), lambda n: ov, downloads.append
        )
        assert got == {"ov.r": str(ov)}
        assert downloads == ["ov.r"]

    @pytest.mark.parametrize(
        ("installed", "message"),
        [
            (None, "Overlay not installed locally and not found in remote repos: ov.x"),
            (Path("somewhere"), "Overlay install failed: ov.x"),
        ],
    )
    def test_additions_not_found(self, installed, message):
        with pytest.raises(WorkspaceOpError) as exc:
            resolve_overlay_additions(["ov.x"], {}, FakeResolver, lambda n: installed)
        assert exc.value.message == message
        assert exc.value.code == "E_OVERLAY_NOT_FOUND"

    def test_removal_checks(self, tmp_path):
        common = _overlay_dir(tmp_path, "c", "ov.c", "gem")
        win = _overlay_dir(tmp_path, "w", "ov.w", "gem", deps=["ov.c"])
        current = {"ov.c": str(common), "ov.w": str(win)}
        with pytest.raises(WorkspaceOpError) as exc:
            check_overlay_removals({"ov.zz"}, current, current)
        assert exc.value.code == "E_OVERLAY_NOT_IN_WS"
        with pytest.raises(WorkspaceOpError) as exc:
            check_overlay_removals({"ov.c"}, current, {"ov.w": str(win)})
        assert exc.value.code == "E_OVERLAY_DEP"
        assert "still required by ov.w" in exc.value.message
        check_overlay_removals({"ov.c", "ov.w"}, current, {})

    def test_affected_bases(self, tmp_path):
        a = _overlay_dir(tmp_path, "a", "ov.a", "gem.a")
        b = _overlay_dir(tmp_path, "b", "ov.b", "gem.b")
        got = affected_bases({"gem.x"}, {"ov.a": str(a)}, {"ov.b"}, {"ov.b": str(b)})
        assert got == {"gem.x", "gem.a", "gem.b"}

    def test_clear_attribution_records(self, tmp_path):
        ledger = tmp_path / "Overlays" / "ov.a"
        ledger.mkdir(parents=True)
        (ledger / "LICENSE").write_text("x")
        meta = _meta(tmp_path)
        meta.file_links = {"/s/1": "Overlays/ov.a/LICENSE", "/s/2": "Gems/g/x"}
        clear_attribution_records(tmp_path, meta, ["ov.a", "ov.missing"])
        assert not ledger.exists()
        assert meta.file_links == {"/s/2": "Gems/g/x"}

    def _composed_workspace(self, tmp_path):
        gem = tmp_path / "src" / "gem"
        _write_json(gem / "gem.json", {"gem": {"name": "org.gem", "version": "1.0.0"}})
        (gem / "a.txt").write_text("base")
        ovs = {}
        for name, prec in (("one", 1), ("two", 2)):
            d = _overlay_dir(tmp_path / "ovs", name, f"ov.{name}", "org.gem", precedence=prec)
            (d / "Overlay").mkdir()
            (d / "Overlay" / "a.txt").write_text(name)
            ovs[f"ov.{name}"] = str(d)
        ws = tmp_path / "ws"
        ws.mkdir()
        meta = build_workspace_meta(
            name="ws",
            root_path=gem,
            root_type="gem",
            sources={"gems": {"org.gem": str(gem)}, "overlays": ovs},
        )
        write_workspace_meta(ws, meta)
        return ws, meta

    def test_change_overlays_reorder_and_remove(self, tmp_path):
        ws, meta = self._composed_workspace(tmp_path)
        result = change_overlays(
            ws,
            meta,
            OverlayChange(order=("ov.two,ov.one",)),
            resolver_cls=FakeResolver,
            install_overlay=lambda n: None,
        )
        assert result.recomposed == ["org.gem"]
        assert result.ordered_bases == {"org.gem"}
        # ov.one now applies last and wins the file conflict
        assert (ws / "Gems" / "gem" / "a.txt").read_text() == "one"
        assert read_workspace_meta(ws).overlay_order == {"org.gem": ["ov.two", "ov.one"]}

        result = change_overlays(
            ws,
            meta,
            OverlayChange(remove=("ov.one",)),
            resolver_cls=FakeResolver,
            install_overlay=lambda n: None,
        )
        assert result.removed == {"ov.one"}
        assert (ws / "Gems" / "gem" / "a.txt").read_text() == "two"
        saved = read_workspace_meta(ws)
        assert set(saved.sources.overlays) == {"ov.two"}
        assert saved.overlay_order == {"org.gem": ["ov.two"]}

    def test_change_overlays_attribution_migration(self, tmp_path):
        ws, meta = self._composed_workspace(tmp_path)
        result = change_overlays(
            ws,
            meta,
            OverlayChange(attributions="off"),
            resolver_cls=FakeResolver,
            install_overlay=lambda n: None,
        )
        assert result.attributions_changed and result.old_attributions == "workspace"
        assert read_workspace_meta(ws).attributions == "off"

    def test_change_overlays_skips_unlinked_base(self, tmp_path):
        ws, meta = self._composed_workspace(tmp_path)
        stray = _overlay_dir(tmp_path, "s", "ov.s", "org.not.here")
        result = change_overlays(
            ws,
            meta,
            OverlayChange(add=("ov.s",)),
            resolver_cls=lambda: FakeResolver(overlays={"ov.s": stray}),
            install_overlay=lambda n: None,
        )
        assert result.skipped == ["org.not.here"]
        assert result.recomposed == []


# ---------------------------------------------------------------------------
# workspace override
# ---------------------------------------------------------------------------


class TestOverride:
    def test_root_name_and_sources_map(self, tmp_path):
        meta = _meta(
            tmp_path,
            engines={"e": str(tmp_path / "e")},
            gems={"g": str(tmp_path / "g"), "blank": ""},
        )
        assert sources_object_map(meta) == {
            "e": (tmp_path / "e", ObjectType.ENGINE),
            "g": (tmp_path / "g", ObjectType.GEM),
        }
        resolver = FakeResolver(objects={"org.root": tmp_path / "root"})
        assert workspace_root_name(meta, resolver) == "org.root"
        meta.root_object = None
        assert workspace_root_name(meta, resolver) is None

    def test_make_override_local_binary(self, tmp_path):
        with patch("o3de_cli.core.solver.find_local_binary_install", return_value=tmp_path):
            override, _ = make_override(
                "g", "1.0", "local-binary", FakeResolver(), FakeResolver, print
            )
        assert override == ObjectOverride(
            version="1.0", artifact="local-binary", path=str(tmp_path)
        )
        with patch("o3de_cli.core.solver.find_local_binary_install", return_value=None):
            with pytest.raises(WorkspaceOpError) as exc:
                make_override("g", "1.0", "local-binary", FakeResolver(), FakeResolver, print)
        assert exc.value.code == "E_NO_LOCAL_BINARY"

    def test_make_override_remote_binary_error(self):
        with patch(
            "o3de_cli.core.gem_package.download_remote_binary", side_effect=ValueError("no")
        ):
            with pytest.raises(WorkspaceOpError) as exc:
                make_override("g", "1.0", "remote-binary", FakeResolver(), FakeResolver, print)
        assert (exc.value.code, exc.value.message) == (
            "E_NO_REMOTE_BINARY",
            "Remote binary unavailable: no",
        )

    def test_make_override_remote_source_becomes_source(self, tmp_path):
        class Registering(FakeResolver):
            def _add_to_manifest(self, path, obj_type):
                self.added = (path, obj_type)

        first, fresh = Registering(), FakeResolver()
        with patch("o3de_cli.core.gem_package.download_remote_source", return_value=tmp_path):
            override, resolver = make_override(
                "g", "2.0", "remote-source", first, lambda: fresh, print
            )
        assert override == ObjectOverride(version="2.0", artifact="source", path=str(tmp_path))
        assert resolver is fresh and fresh.resolve_calls == 1
        assert first.added == (tmp_path, ObjectType.GEM)

    def test_solve_with_overrides(self, tmp_path):
        meta = _meta(tmp_path)
        resolver = FakeResolver(objects={"org.root": tmp_path / "root"})
        solve = _solver(_result())
        solve_with_overrides(meta, resolver, {"g": ObjectOverride(version="4.0")}, solve)
        assert solve.calls == [
            {"root_name": "org.root", "resolver": resolver, "overrides": {"g": "4.0"}}
        ]

        with pytest.raises(WorkspaceOpError) as exc:
            solve_with_overrides(meta, resolver, {}, _solver(_result(conflict="bad pin")))
        assert exc.value.code == "E_SOLVE_CONFLICT"
        assert exc.value.message == "Override rejected — resolution failed: bad pin"

        with pytest.raises(WorkspaceOpError) as exc:
            solve_with_overrides(meta, FakeResolver(), {}, solve)
        assert exc.value.code == "E_NO_ROOT"

    def test_relink_changed_objects(self, tmp_path):
        meta = _meta(tmp_path, gems={"same": str(tmp_path / "same"), "moved": str(tmp_path / "v1")})
        new_map = {
            "root": (tmp_path / "root", ObjectType.PROJECT),
            "same": (tmp_path / "same", ObjectType.GEM),
            "moved": (tmp_path / "v2", ObjectType.GEM),
            "added": (tmp_path / "added", ObjectType.GEM),
        }
        with patch("o3de_cli.core.workspace_ops.relink_object") as relink:
            changed = relink_changed_objects(tmp_path, meta, new_map)
        assert changed == ["moved", "added"]
        assert [c.args[2] for c in relink.call_args_list] == ["moved", "added"]

    def test_apply_override_clear_missing(self, tmp_path):
        with pytest.raises(WorkspaceOpError) as exc:
            apply_override(
                tmp_path,
                _meta(tmp_path),
                OverrideRequest("g", clear=True),
                resolver_cls=FakeResolver,
                solve=_solver(),
                on_progress=print,
            )
        assert exc.value.code == "E_NO_OVERRIDE"

    def test_apply_override_no_apply_persists_only(self, tmp_path):
        meta = _meta(tmp_path)
        resolver = FakeResolver(objects={"org.root": tmp_path / "root"})
        result = _result(candidates=[_cand("g", tmp_path / "g4")])
        with patch("o3de_cli.core.workspace_ops.relink_object") as relink:
            changed = apply_override(
                tmp_path,
                meta,
                OverrideRequest("g", version="4.0", apply=False),
                resolver_cls=lambda: resolver,
                solve=_solver(result),
                on_progress=print,
            )
        assert changed == []
        relink.assert_not_called()
        assert read_workspace_meta(tmp_path).overrides == {"g": ObjectOverride(version="4.0")}


# ---------------------------------------------------------------------------
# workspace build
# ---------------------------------------------------------------------------


class TestBuildPlan:
    def test_source_paths_prefer_composed_tree(self, tmp_path):
        meta = _meta(
            tmp_path,
            engines={"org.o3de.engine.o3de": str(tmp_path / "src" / "o3de")},
            projects={"org.x.project.game": str(tmp_path / "src" / "game")},
        )
        assert build_source_paths(tmp_path, meta) == (
            tmp_path / "src" / "o3de",
            tmp_path / "src" / "game",
        )
        (tmp_path / "Engines" / "o3de").mkdir(parents=True)
        assert build_source_paths(tmp_path, meta)[0] == tmp_path / "Engines" / "o3de"

    def test_root_gem_target(self, tmp_path):
        gem = tmp_path / "gem"
        _write_json(gem / "gem.json", {"gem": {"name": "org.g", "version": "1.0.0"}})
        meta = build_workspace_meta("ws", gem, "gem", {"gems": {"org.g.reg": str(gem)}})
        assert root_gem_target(meta) == "org.g.reg"
        _write_json(gem / "gem.json", {"gem_name": "Legacy"})
        assert root_gem_target(meta) == "Legacy"
        meta.root_type = "project"
        assert root_gem_target(meta) is None

    def test_root_gem_target_falls_back_to_object_name(self, tmp_path):
        gem = tmp_path / "gem"
        _write_json(gem / "gem.2-0-0.json", {"gem": {"name": "org.g", "version": "1.0.0"}})
        meta = build_workspace_meta("ws", gem, "gem", {})
        assert root_gem_target(meta) == "org.g"

    def _layout(self, tmp_path, engine_centric=False, project=True):
        return build_layout(
            tmp_path,
            tmp_path / "eng",
            tmp_path / "proj" if project else None,
            engine_centric,
            "linux",
        )

    def test_layout(self, tmp_path):
        layout = self._layout(tmp_path)
        assert (layout.source_dir, layout.build_dir, layout.mode) == (
            tmp_path / "proj",
            tmp_path / "build" / "linux",
            "project-centric",
        )
        assert self._layout(tmp_path, engine_centric=True).source_dir == tmp_path / "eng"

    def test_configure_command_project_centric(self, tmp_path):
        layout = self._layout(tmp_path)
        cmd = cmake_configure_command(
            layout,
            preset=None,
            generator="Ninja",
            third_party=Path("/3p"),
            resolved_manifest=tmp_path / "m.json",
        )
        assert cmd == [
            "cmake",
            "-B",
            str(tmp_path / "build" / "linux"),
            "-S",
            str(tmp_path / "proj"),
            "-GNinja",
            f"-DLY_3RDPARTY_PATH={Path('/3p')}",
            f"-DO3DE_RESOLVED_MANIFEST={(tmp_path / 'm.json').as_posix()}",
            f"-DO3DE_ENGINE_PATH={(tmp_path / 'eng').as_posix()}",
        ]

    def test_configure_command_engine_centric_preset(self, tmp_path):
        layout = self._layout(tmp_path, engine_centric=True)
        cmd = cmake_configure_command(
            layout, preset="p", generator="Ninja", third_party=None, resolved_manifest=None
        )
        assert cmd == [
            "cmake",
            "--preset",
            "p",
            "-S",
            str(tmp_path / "eng"),
            f"-DLY_PROJECTS={tmp_path / 'proj'}",
        ]

    def test_build_command(self, tmp_path):
        assert cmake_build_command(tmp_path, "debug", (), None) == [
            "cmake",
            "--build",
            str(tmp_path),
            "--config",
            "Debug",
            "--parallel",
        ]
        assert cmake_build_command(tmp_path, "release", ("A", "B"), 4)[-5:] == [
            "--target",
            "A",
            "B",
            "--parallel",
            "4",
        ]

    def test_plan_skips_configure_with_cache(self, tmp_path):
        layout = self._layout(tmp_path)
        configure, build = plan_cmake_commands(tmp_path, layout, BuildOptions())
        assert configure is not None and "-DO3DE_RESOLVED_MANIFEST" not in " ".join(configure)
        assert build is not None

        layout.build_dir.mkdir(parents=True)
        (layout.build_dir / "CMakeCache.txt").write_text("")
        (tmp_path / "resolved_o3de_manifest.json").write_text("{}")
        assert plan_cmake_commands(tmp_path, layout, BuildOptions())[0] is None
        configure, build = plan_cmake_commands(
            tmp_path, layout, BuildOptions(reconfigure=True, configure_only=True)
        )
        assert configure is not None and "-DO3DE_RESOLVED_MANIFEST" in " ".join(configure)
        assert build is None

    def test_layout_dataclass_mode(self, tmp_path):
        layout = BuildLayout(tmp_path, None, True, tmp_path, tmp_path / "b")
        assert layout.mode == "engine-centric"


def test_solve_failed_error_message():
    err = SolveFailedError("Stage failed", "why")
    assert (str(err), err.code) == ("Stage failed: why", "E_SOLVE_CONFLICT")
    assert isinstance(err, WorkspaceOpError)


def test_meta_round_trip(tmp_path):
    meta = build_workspace_meta(
        "ws",
        tmp_path,
        "engine",
        {"engines": {"e": "/e"}},
        platforms=["Linux"],
        attributions="object",
    )
    write_workspace_meta(tmp_path, meta)
    data = json.loads((tmp_path / "workspace.json").read_text())
    assert data["platforms"] == ["Linux"] and data["attributions"] == "object"
    assert read_workspace_meta(tmp_path).sources.engines == {"e": "/e"}
