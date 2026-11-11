# O3DE CLI - manifest resolve report tests
# SPDX-License-Identifier: Apache-2.0 OR MIT

"""Unit tests for o3de_cli.core.resolve_report."""

from types import SimpleNamespace

from o3de_cli.core.resolve_report import object_counts, resolve_warnings, unique_by_dependency
from o3de_cli.core.resolver import ObjectNameVersion


def test_object_counts():
    resolver = SimpleNamespace(
        engines={"e": 1},
        projects={},
        gems={"a": 1, "b": 2},
        templates={},
        repos={"r": 1},
        overlays={},
        objects={"e": 1, "a": 1, "b": 2, "r": 1},
    )
    assert object_counts(resolver) == {
        "engines": 1,
        "projects": 0,
        "gems": 2,
        "templates": 0,
        "repos": 1,
        "overlays": 0,
        "total": 4,
    }


def test_resolve_warnings_lists_missing_then_conflicts():
    missing = [("org.a", ObjectNameVersion("org.x>=1.0"))]
    assert resolve_warnings(missing, ["c1"]) == [
        "Missing dependency: org.a → org.x>=1.0",
        "Version conflict: c1",
    ]


def test_unique_by_dependency_keeps_first_requirer():
    pairs = [
        ("org.a", ObjectNameVersion("org.x")),
        ("org.b", ObjectNameVersion("org.x>=2")),
        ("org.b", ObjectNameVersion("org.y")),
    ]
    assert unique_by_dependency(pairs) == [pairs[0], pairs[2]]
