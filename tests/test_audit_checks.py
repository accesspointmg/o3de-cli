# O3DE CLI - audit check tests
# SPDX-License-Identifier: Apache-2.0 OR MIT

"""Unit tests for o3de_cli.core.audit_checks, without Click or the console."""

from types import SimpleNamespace

from o3de_cli.core.audit_checks import (
    ISSUE_CATEGORIES,
    collect_issues,
    conflict_issue,
    dependency_issues,
    deprecation_issue,
    integrity_issues,
    optional_issues,
    peer_issues,
)
from o3de_cli.core.models import ObjectType
from o3de_cli.core.resolver import DependencyConflict, ObjectNameVersion


def _obj(data=None, version="1.0.0", deps=(), peers=(), optional=()):
    return SimpleNamespace(
        object_type=ObjectType.GEM,
        data=data if data is not None else {"gem": {}},
        version=version,
        dependencies=[ObjectNameVersion(d) for d in deps],
        peer_dependencies=[ObjectNameVersion(d) for d in peers],
        optional_dependencies=[ObjectNameVersion(d) for d in optional],
    )


class TestDeprecation:
    def test_dict_with_replacement(self):
        obj = _obj({"gem": {"deprecated": {"message": "old", "replacement": "org.new"}}})
        assert deprecation_issue("org.a", obj) == {
            "object": "org.a",
            "message": "old",
            "replacement": "org.new",
        }

    def test_dict_without_message_uses_its_text(self):
        obj = _obj({"gem": {}, "deprecated": {"replacement": "x"}})
        issue = deprecation_issue("org.a", obj)
        assert issue == {"object": "org.a", "message": "{'replacement': 'x'}", "replacement": "x"}

    def test_plain_value(self):
        obj = _obj({"gem": "text", "deprecated": True})
        assert deprecation_issue("org.a", obj) == {
            "object": "org.a",
            "message": "True",
            "replacement": "",
        }

    def test_not_deprecated(self):
        assert deprecation_issue("org.a", _obj()) is None


class TestIntegrity:
    def test_fields_and_release_names(self):
        data = {
            "releases": [
                {"name": "1.0.0", "downloads": [{"source": "s", "lfs": "l"}]},
                "skip",
                {"binaries": [{"binary": "b"}, {"binary": "b", "sha256": "x"}]},
            ]
        }
        assert integrity_issues("org.a", data) == [
            {"object": "org.a", "release": "1.0.0", "field": "downloads[0].source_sha256"},
            {"object": "org.a", "release": "1.0.0", "field": "downloads[0].lfs_sha256"},
            {"object": "org.a", "release": "release[2]", "field": "binaries[0].sha256"},
        ]

    def test_releases_not_a_list(self):
        assert integrity_issues("org.a", {"releases": {"k": {}}}) == []


class TestDependencies:
    OBJECTS = {"org.b": _obj(version="1.0.0"), "org.c": _obj(version="")}

    def test_missing_and_mismatched(self):
        obj = _obj(deps=["org.b>=2.0", "org.nope", "org.b"])
        assert dependency_issues("org.a", obj, self.OBJECTS) == [
            {
                "object": "org.a",
                "dependency": "org.b>=2.0",
                "reason": "version mismatch (found 1.0.0)",
            },
            {"object": "org.a", "dependency": "org.nope", "reason": "not found"},
        ]

    def test_peers(self):
        obj = _obj(peers=["org.nope", "org.b>=5"])
        assert peer_issues("org.a", obj, self.OBJECTS) == [
            {"object": "org.a", "peer": "org.nope", "reason": "not installed"},
            {"object": "org.a", "peer": "org.b>=5", "reason": "version mismatch (found 1.0.0)"},
        ]

    def test_optional_only_reports_installed_mismatches(self):
        obj = _obj(optional=["org.nope", "org.b>=5", "org.b"])
        assert optional_issues("org.a", obj, self.OBJECTS) == [
            {
                "object": "org.a",
                "optional": "org.b>=5",
                "reason": "found 1.0.0 but constraint not met",
            }
        ]


def test_conflict_issue():
    conflict = DependencyConflict("org.s", "org.a", ">=1", "org.b", "<1", "1.5.0")
    assert conflict_issue(conflict) == {
        "dependency": "org.s",
        "requirer_a": "org.a",
        "constraint_a": ">=1",
        "requirer_b": "org.b",
        "constraint_b": "<1",
        "resolved_version": "1.5.0",
    }


class TestCollectIssues:
    def test_clean_manifest_has_no_categories(self):
        resolver = SimpleNamespace(objects={"org.a": _obj()}, conflicts=[])
        assert collect_issues(resolver) == {}

    def test_categories_in_report_order(self):
        resolver = SimpleNamespace(
            objects={
                "org.a": _obj(
                    {"gem": {"deprecated": "x"}, "releases": [{"binaries": [{"binary": "b"}]}]},
                    deps=["org.nope"],
                    peers=["org.nope"],
                    optional=["org.b>=9"],
                ),
                "org.b": _obj(),
            },
            conflicts=[DependencyConflict("org.s", "org.a", ">=1", "org.b", "<1", None)],
        )
        issues = collect_issues(resolver)
        assert tuple(issues) == ISSUE_CATEGORIES
        assert all(len(v) == 1 for v in issues.values())
