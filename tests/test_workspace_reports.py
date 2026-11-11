# O3DE CLI - workspace solve/candidates report tests
# SPDX-License-Identifier: Apache-2.0 OR MIT

"""Unit tests for the solve and candidates reports in o3de_cli.core.workspace_ops."""

from pathlib import Path
from types import SimpleNamespace

from o3de_cli.core.models import ObjectOverride, ObjectType, WorkspaceMeta
from o3de_cli.core.solver import Candidate, CandidateStatus, OverlayEntry, SolveResult
from o3de_cli.core.workspace_ops import (
    candidate_entry,
    candidate_summary,
    candidates_report,
    chosen_objects,
    override_summary,
    solve_failure_message,
    solve_report,
    solve_workspace_root,
)


def _meta(root: str | None = None, overrides: dict | None = None) -> WorkspaceMeta:
    return WorkspaceMeta.model_validate(
        {
            "workspace": {"name": "ws"},
            "created": "2026-01-01T00:00:00",
            "root_object": root,
            "sources": {
                "engines": {"org.e": "/e"},
                "gems": {"org.g": "/g"},
                "templates": {"org.t": "/t"},
                "overlays": {"org.o": "/o"},
            },
            "overrides": overrides or {},
        }
    )


def _cand(name, version, status=CandidateStatus.LOCAL, path=None, t=ObjectType.GEM):
    return Candidate(name=name, version=version, object_type=t, status=status, path=path)


def _result(conflict: str = "") -> SolveResult:
    result = SolveResult(root_name="org.p", root_version="1.0.0", conflict_message=conflict)
    result.candidates = {"org.g": _cand("org.g", "2.0.0", path=Path("/g"))}
    result.children = {"org.c": _cand("org.c", "1.0.0")}
    result.overlays = {"org.p": [OverlayEntry("org.o", "1.0.0", "org.p", None, 3)]}
    return result


class TestSolveReport:
    def test_payload(self):
        report = solve_report(_result())
        assert report == {
            "root": "org.p",
            "root_version": "1.0.0",
            "resolved": True,
            "conflict": None,
            "candidates": {
                "org.g": {
                    "version": "2.0.0",
                    "type": "gem",
                    "status": "local",
                    "path": str(Path("/g")),
                }
            },
            "children": {"org.c": {"version": "1.0.0", "type": "gem", "path": None}},
            "overlays": {"org.p": [{"name": "org.o", "version": "1.0.0", "precedence": 3}]},
        }

    def test_failure_message(self):
        assert solve_failure_message(_result("no fit")) == "Resolution failed: no fit"
        assert solve_failure_message(_result()) == "Resolution failed"
        assert solve_report(_result("no fit"))["conflict"] == "no fit"


class TestSolveWorkspaceRoot:
    def test_solves_root_with_pins(self, tmp_path):
        root = tmp_path / "proj"
        root.mkdir()
        resolver = SimpleNamespace(objects={"org.p": SimpleNamespace(path=root)})
        meta = _meta(str(root), {"org.g": {"version": "1.2.0"}})
        calls = []

        def solve(**kwargs):
            calls.append(kwargs)
            return "solved"

        assert solve_workspace_root(meta, resolver, "store", solve) == "solved"
        assert calls == [
            {
                "root_name": "org.p",
                "resolver": resolver,
                "store": "store",
                "overrides": {"org.g": "1.2.0"},
            }
        ]

    def test_no_root_or_unknown_root(self, tmp_path):
        resolver = SimpleNamespace(objects={})

        def solve(**kwargs):
            raise AssertionError("must not solve")

        assert solve_workspace_root(_meta(None), resolver, None, solve) is None
        assert solve_workspace_root(_meta(str(tmp_path / "x")), resolver, None, solve) is None


class TestChosenObjects:
    def test_from_a_resolved_solve(self):
        chosen = chosen_objects(_meta(), _result())
        assert chosen == {"org.g": ("2.0.0", "gem", str(Path("/g"))), "org.c": ("1.0.0", "gem", "")}

    def test_falls_back_to_sources_without_overlays(self):
        expected = {
            "org.e": ("", "engine", "/e"),
            "org.g": ("", "gem", "/g"),
            "org.t": ("", "template", "/t"),
        }
        assert chosen_objects(_meta(), None) == expected
        assert chosen_objects(_meta(), _result("conflict")) == expected


class TestCandidatesReport:
    def test_entries(self):
        local = _cand("org.g", "2.0.0", path=Path("/g"))
        local.local_binary_path = Path("/bin")
        remote = _cand("org.g", "1.0.0", status=CandidateStatus.REMOTE)
        remote.remote_binary = True
        calls = []

        def list_fn(name, resolver, store):
            calls.append((name, resolver, store))
            return [local, remote] if name == "org.g" else []

        meta = _meta(overrides={"org.g": {"version": "2.0.0", "artifact": "local-binary"}})
        report = candidates_report(
            meta, {"org.g": ("2.0.0", "gem", "/g")}, ["org.g", "org.x"], "R", "S", list_fn
        )
        assert calls == [("org.g", "R", "S"), ("org.x", "R", "S")]
        assert (
            report[0]["override"]
            == ObjectOverride(version="2.0.0", artifact="local-binary").model_dump()
        )
        assert report[0]["candidates"] == [candidate_entry(local), candidate_entry(remote)]
        assert report[1] == {
            "name": "org.x",
            "type": "",
            "chosen_version": "",
            "chosen_path": "",
            "override": None,
            "candidates": [],
        }

    def test_summaries(self):
        local = candidate_entry(_cand("org.g", "2.0.0", path=Path("/g")))
        local["local_binary_path"] = "/bin"
        remote = candidate_entry(_cand("org.g", "1.0.0", status=CandidateStatus.REMOTE))
        remote["remote_binary"] = True
        bare = candidate_entry(_cand("org.g", "0.1.0", status=CandidateStatus.UNKNOWN))
        assert candidate_summary(local) == "2.0.0 (local; source/local-binary)"
        assert candidate_summary(remote) == "1.0.0 (remote; remote-binary)"
        assert candidate_summary(bare) == "0.1.0 (unknown; —)"
        assert override_summary({"version": "1.0.0", "artifact": "source"}) == "1.0.0 [source]"
        assert override_summary(None) == ""
