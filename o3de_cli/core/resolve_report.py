# O3DE CLI - Resolve Report
# SPDX-License-Identifier: Apache-2.0 OR MIT

"""What ``o3de manifest resolve`` reports about a resolved manifest.

Nothing here imports Click or prints.
"""

from collections.abc import Iterable
from typing import Any

from o3de_cli.core.resolver import ObjectNameVersion, Resolver

# (requirer name, dependency specifier)
MissingDep = tuple[str, ObjectNameVersion]


def object_counts(resolver: Resolver) -> dict[str, int]:
    """How many objects of each type the manifest resolved to, and the total."""
    return {
        "engines": len(resolver.engines),
        "projects": len(resolver.projects),
        "gems": len(resolver.gems),
        "templates": len(resolver.templates),
        "repos": len(resolver.repos),
        "overlays": len(resolver.overlays),
        "total": len(resolver.objects),
    }


def resolve_warnings(missing: Iterable[MissingDep], conflicts: Iterable[Any]) -> list[str]:
    """The ``--json`` warnings: every missing dependency, then every version conflict."""
    warnings = [f"Missing dependency: {requirer} → {dep_spec}" for requirer, dep_spec in missing]
    warnings += [f"Version conflict: {c}" for c in conflicts]
    return warnings


def unique_by_dependency(pairs: Iterable[MissingDep]) -> list[MissingDep]:
    """*pairs* with only the first requirer of each dependency name kept."""
    seen: set[str] = set()
    unique: list[MissingDep] = []
    for requirer, dep_spec in pairs:
        if dep_spec.name not in seen:
            seen.add(dep_spec.name)
            unique.append((requirer, dep_spec))
    return unique
