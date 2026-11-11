# O3DE CLI - Audit Checks
# SPDX-License-Identifier: Apache-2.0 OR MIT

"""The checks behind ``o3de audit``.

Each check looks at one resolved object (or the resolver's conflicts) and
returns issue records: plain dicts that go straight into the JSON report
and the console tables.  Nothing here imports Click or prints.
"""

from typing import Any

from o3de_cli.core.publish_checks import deprecation_of, missing_checksums
from o3de_cli.core.resolver import (
    DependencyConflict,
    ObjectNameVersion,
    ResolvedObject,
    Resolver,
)

# Issue categories, in report order
ISSUE_CATEGORIES = (
    "deprecated",
    "missing_integrity",
    "missing_dependencies",
    "missing_peer_dependencies",
    "unresolvable_optional",
    "conflicts",
)


def deprecation_issue(name: str, obj: ResolvedObject) -> dict | None:
    """The issue for a deprecated object, or None when it is not deprecated."""
    deprecated: Any = deprecation_of(obj.data, obj.object_type)
    if not deprecated:
        return None
    if isinstance(deprecated, dict):
        msg = deprecated.get("message", str(deprecated))
        replacement = deprecated.get("replacement", "")
    else:
        msg = str(deprecated)
        replacement = ""
    return {"object": name, "message": msg, "replacement": replacement}


def integrity_issues(name: str, data: dict) -> list[dict]:
    """One issue per release artifact published without its checksum."""
    issues: list[dict] = []
    releases = data.get("releases", [])
    if not isinstance(releases, list):
        return issues
    for i, release in enumerate(releases):
        if not isinstance(release, dict):
            continue
        rel_name = release.get("name", f"release[{i}]")
        for list_key, j, _artifact, checksum_field in missing_checksums(release):
            issues.append(
                {"object": name, "release": rel_name, "field": f"{list_key}[{j}].{checksum_field}"}
            )
    return issues


def _unmet(spec: ObjectNameVersion, objects: dict[str, ResolvedObject]) -> tuple[bool, str] | None:
    """Whether *spec* is unmet: ``None`` when met, else ``(installed, found version)``."""
    candidate = objects.get(spec.name)
    if candidate is None:
        return False, ""
    if not spec.matches(candidate.version):
        return True, candidate.version
    return None


def dependency_issues(
    name: str, obj: ResolvedObject, objects: dict[str, ResolvedObject]
) -> list[dict]:
    """Required dependencies that are absent or at a non-matching version."""
    issues: list[dict] = []
    for dep_spec in obj.dependencies:
        unmet = _unmet(dep_spec, objects)
        if unmet is None:
            continue
        installed, found = unmet
        reason = f"version mismatch (found {found})" if installed else "not found"
        issues.append({"object": name, "dependency": str(dep_spec), "reason": reason})
    return issues


def peer_issues(name: str, obj: ResolvedObject, objects: dict[str, ResolvedObject]) -> list[dict]:
    """Peer dependencies that are not installed or at a non-matching version."""
    issues: list[dict] = []
    for peer_spec in obj.peer_dependencies:
        unmet = _unmet(peer_spec, objects)
        if unmet is None:
            continue
        installed, found = unmet
        reason = f"version mismatch (found {found})" if installed else "not installed"
        issues.append({"object": name, "peer": str(peer_spec), "reason": reason})
    return issues


def optional_issues(
    name: str, obj: ResolvedObject, objects: dict[str, ResolvedObject]
) -> list[dict]:
    """Optional dependencies that are installed but at a non-matching version.

    An absent optional dependency is not an issue.
    """
    issues: list[dict] = []
    for opt_spec in obj.optional_dependencies:
        unmet = _unmet(opt_spec, objects)
        if unmet is None or not unmet[0]:
            continue
        issues.append(
            {
                "object": name,
                "optional": str(opt_spec),
                "reason": f"found {unmet[1]} but constraint not met",
            }
        )
    return issues


def conflict_issue(conflict: DependencyConflict) -> dict:
    """The issue record for one version conflict between siblings."""
    return {
        "dependency": conflict.dependency_name,
        "requirer_a": conflict.requirer_a,
        "constraint_a": conflict.constraint_a,
        "requirer_b": conflict.requirer_b,
        "constraint_b": conflict.constraint_b,
        "resolved_version": conflict.resolved_version,
    }


def collect_issues(resolver: Resolver) -> dict[str, list[dict]]:
    """Collect all audit issues from a resolved manifest.

    Returns a dict keyed by issue category; empty categories are left out.
    """
    issues: dict[str, list[dict]] = {category: [] for category in ISSUE_CATEGORIES}
    objects = resolver.objects
    for name, obj in objects.items():
        deprecated = deprecation_issue(name, obj)
        if deprecated:
            issues["deprecated"].append(deprecated)
        issues["missing_integrity"] += integrity_issues(name, obj.data)
        issues["missing_dependencies"] += dependency_issues(name, obj, objects)
        issues["missing_peer_dependencies"] += peer_issues(name, obj, objects)
        issues["unresolvable_optional"] += optional_issues(name, obj, objects)
    issues["conflicts"] = [conflict_issue(c) for c in resolver.conflicts]
    return {k: v for k, v in issues.items() if v}
