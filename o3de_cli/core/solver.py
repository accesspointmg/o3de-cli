# O3DE Pilot - Workspace Solver
# SPDX-License-Identifier: Apache-2.0 OR MIT

"""
Dependency solver for workspace creation using resolvelib.

The solver takes a root object (engine/project) and resolves its full
transitive dependency graph against both local (manifest) and remote
(store) objects.  It produces a SolveResult that can be inspected
before any files are downloaded or workspaces built.

resolvelib integration follows pip's pattern:
- Provider yields candidate versions for each requirement
- Reporter receives progress callbacks for GUI display
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional, Callable, Sequence, Any

from packaging.specifiers import SpecifierSet
from packaging.version import Version
from resolvelib import (
    AbstractProvider,
    AbstractResolver,
    BaseReporter,
    Resolver as RLResolver,
    RequirementsConflicted,
)
from resolvelib.resolvers import ResolutionImpossible

from .models import ObjectType
from .resolver import ObjectNameVersion, ResolvedObject, Resolver
from .store import RemoteObject, Store

logger = logging.getLogger("o3de_cli.solver")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


class CandidateStatus(str, Enum):
    """Where a candidate lives."""

    LOCAL = "local"  # Already on disk, registered in manifest
    REMOTE = "remote"  # Available in the store, needs download
    UNKNOWN = "unknown"  # Referenced but not found anywhere


@dataclass
class Candidate:
    """A specific version of an object that can satisfy a requirement."""

    name: str
    version: str
    object_type: ObjectType
    status: CandidateStatus = CandidateStatus.UNKNOWN

    # Populated for local candidates
    path: Optional[Path] = None
    resolved_object: Optional[ResolvedObject] = None

    # Populated for remote candidates
    remote_object: Optional[RemoteObject] = None

    # Artifact availability (populated by annotate_artifacts / list_candidates)
    # Path to a locally built + installed binary layout (contains *Config.cmake)
    local_binary_path: Optional[Path] = None
    # True when a release advertises a prebuilt binary for the current platform
    remote_binary: bool = False

    # Dependencies declared by this candidate (raw specifier strings)
    dependencies: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        return f"Candidate({self.name}@{self.version} [{self.status.value}])"

    def __hash__(self) -> int:
        return hash((self.name, self.version))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Candidate):
            return NotImplemented
        return self.name == other.name and self.version == other.version


@dataclass
class Requirement:
    """A dependency requirement: object name + version constraint."""

    name: str
    specifier: SpecifierSet = field(default_factory=SpecifierSet)

    @classmethod
    def from_specifier(cls, spec: str) -> "Requirement":
        """Create from a string like 'org.o3de.gem.atom>=1.0.0'."""
        parsed = ObjectNameVersion(spec)
        return cls(name=parsed.name, specifier=parsed.specifier)

    def __repr__(self) -> str:
        if self.specifier:
            return f"Requirement({self.name}{self.specifier})"
        return f"Requirement({self.name})"

    def __hash__(self) -> int:
        return hash(self.name)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Requirement):
            return NotImplemented
        return self.name == other.name


@dataclass
class OverlayEntry:
    """An overlay that applies to a resolved object."""

    name: str
    version: str
    extends: str  # Base object name
    extends_version: Optional[str]  # Version constraint on base
    precedence: int = 0
    path: Optional[Path] = None
    status: CandidateStatus = CandidateStatus.LOCAL
    # Platforms this overlay delivers (empty = platform-agnostic)
    platforms: list[str] = field(default_factory=list)
    # Overlay names this overlay depends on (from dependent.overlays)
    overlay_deps: list[str] = field(default_factory=list)


@dataclass
class SolveResult:
    """Complete output of the solver."""

    # Root object that was solved for
    root_name: str
    root_version: str

    # All resolved dependency candidates (name -> Candidate)
    candidates: dict[str, Candidate] = field(default_factory=dict)

    # Containment children of the root (not dependencies)
    children: dict[str, Candidate] = field(default_factory=dict)

    # Overlays grouped by base object name
    overlays: dict[str, list[OverlayEntry]] = field(default_factory=dict)

    # Conflict information (empty on success)
    conflict_message: str = ""

    @property
    def is_resolved(self) -> bool:
        """True if resolution succeeded without conflicts."""
        return not self.conflict_message

    @property
    def local_count(self) -> int:
        return sum(1 for c in self.candidates.values() if c.status == CandidateStatus.LOCAL)

    @property
    def remote_count(self) -> int:
        return sum(1 for c in self.candidates.values() if c.status == CandidateStatus.REMOTE)

    @property
    def unknown_count(self) -> int:
        return sum(1 for c in self.candidates.values() if c.status == CandidateStatus.UNKNOWN)


# ---------------------------------------------------------------------------
# resolvelib Provider
# ---------------------------------------------------------------------------


class O3DEProvider(AbstractProvider):
    """
    Feeds resolvelib with candidate versions from local manifest + remote store.

    For each requirement the provider:
    1. Checks the local resolver for matching objects
    2. Falls back to the remote store for additional versions
    3. Returns candidates sorted newest-first (latest-compatible strategy)
    """

    def __init__(
        self,
        resolver: Resolver,
        store: Optional[Store] = None,
    ):
        self._resolver = resolver
        self._store = store

        # Build an index of all local objects by name -> list[ResolvedObject].
        # Prefer the resolver's multi-version registry (objects_all) so that
        # alternate versions on disk remain selectable; fall back to the
        # newest-wins map for older Resolver instances.
        self._local_objects: dict[str, list[ResolvedObject]] = {}
        objects_all = getattr(resolver, "objects_all", None)
        if objects_all:
            for name, versions in objects_all.items():
                for obj in versions.values():
                    self._local_objects.setdefault(name, []).append(obj)
        else:
            for name, obj in resolver.objects.items():
                self._local_objects.setdefault(name, []).append(obj)

    # -- resolvelib API -------------------------------------------------------

    def identify(self, requirement_or_candidate: Requirement | Candidate) -> str:
        """Return a hashable identifier for a requirement/candidate."""
        return requirement_or_candidate.name

    def get_preference(
        self,
        identifier: str,
        resolutions: dict[str, Candidate],
        candidates: dict[str, Sequence[Candidate]],
        information: dict[str, Any],
        backtrack_causes: Sequence[Any],
    ) -> int:
        """
        Lower number = resolved first.

        Prefer objects with fewer candidates (tighter constraints resolve
        faster with less backtracking).
        """
        return sum(1 for _ in candidates.get(identifier, []))

    def find_matches(
        self,
        identifier: str,
        requirements: dict[str, Sequence[Requirement]],
        incompatibilities: dict[str, Sequence[Candidate]],
    ) -> list[Candidate]:
        """
        Return candidates matching *all* requirements for `identifier`,
        sorted newest-first.
        """
        reqs = list(requirements.get(identifier, []))
        bad = set(incompatibilities.get(identifier, []))

        candidates: list[Candidate] = []

        # 1. Local objects
        for obj in self._local_objects.get(identifier, []):
            cand = Candidate(
                name=obj.name,
                version=obj.version,
                object_type=obj.object_type,
                status=CandidateStatus.LOCAL,
                path=obj.path,
                resolved_object=obj,
                dependencies=[str(d) for d in obj.dependencies],
            )
            if cand not in bad and self._matches_all(cand, reqs):
                candidates.append(cand)

        # 2. Remote objects (store)
        if self._store:
            for type_name_key, versions_dict in self._store.versions.items():
                # Key format is "type:name"
                parts = type_name_key.split(":", 1)
                if len(parts) != 2:
                    continue
                obj_type_str, obj_name = parts
                if obj_name != identifier:
                    continue

                try:
                    obj_type = ObjectType(obj_type_str)
                except ValueError:
                    continue

                for ver, remote_obj in versions_dict.items():
                    remote_deps = getattr(remote_obj, "dependencies", None)
                    if not isinstance(remote_deps, list):
                        remote_deps = []
                    cand = Candidate(
                        name=obj_name,
                        version=ver,
                        object_type=obj_type,
                        status=CandidateStatus.REMOTE,
                        remote_object=remote_obj,
                        dependencies=remote_deps,
                    )
                    # Skip if already have this exact version locally
                    if any(c.version == ver for c in candidates):
                        continue
                    if cand not in bad and self._matches_all(cand, reqs):
                        candidates.append(cand)

        # Sort newest first (latest-compatible strategy)
        candidates.sort(key=lambda c: self._version_key(c.version), reverse=True)
        return candidates

    def is_satisfied_by(self, requirement: Requirement, candidate: Candidate) -> bool:
        """Check if a candidate satisfies a requirement."""
        if requirement.name != candidate.name:
            return False
        if not requirement.specifier:
            return True
        try:
            return Version(candidate.version) in requirement.specifier
        except Exception:
            return True  # Accept invalid versions

    def get_dependencies(self, candidate: Candidate) -> list[Requirement]:
        """Return requirements declared by a candidate."""
        reqs = []
        for dep_str in candidate.dependencies:
            reqs.append(Requirement.from_specifier(dep_str))
        return reqs

    # -- Helpers --------------------------------------------------------------

    @staticmethod
    def _matches_all(candidate: Candidate, reqs: list[Requirement]) -> bool:
        """Check if a candidate satisfies all requirements."""
        for req in reqs:
            if not req.specifier:
                continue
            try:
                if Version(candidate.version) not in req.specifier:
                    return False
            except Exception:
                pass
        return True

    @staticmethod
    def _version_key(version: str) -> tuple:
        """Sort key for version strings (semver-like)."""
        parts = version.split(".")
        result = []
        for part in parts:
            num = ""
            for c in part:
                if c.isdigit():
                    num += c
                else:
                    break
            result.append(int(num) if num else 0)
        while len(result) < 4:
            result.append(0)
        return tuple(result)


# ---------------------------------------------------------------------------
# resolvelib Reporter
# ---------------------------------------------------------------------------


class O3DEReporter(BaseReporter):
    """
    Receives progress callbacks from resolvelib.

    Forwards events to an optional callback for GUI display.
    """

    def __init__(
        self,
        callback: Optional[Callable[[str], None]] = None,
    ):
        self._callback = callback

    def starting(self) -> None:
        self._emit("Starting dependency resolution...")

    def starting_round(self, index: int) -> None:
        self._emit(f"Resolution round {index}")

    def ending_round(self, index: int, state: Any) -> None:
        pass

    def ending(self, state: Any) -> None:
        self._emit("Resolution complete")

    def adding_requirement(self, requirement: Any, parent: Any) -> None:
        parent_name = parent.name if parent else "root"
        self._emit(f"  {parent_name} requires {requirement}")

    def resolving_conflicts(self, causes: Any) -> None:
        self._emit("Resolving conflicts (backtracking)...")

    def _emit(self, message: str) -> None:
        logger.debug(message)
        if self._callback:
            self._callback(message)


# ---------------------------------------------------------------------------
# Top-level solve function
# ---------------------------------------------------------------------------


def solve_for_workspace(
    root_name: str,
    resolver: Resolver,
    store: Optional[Store] = None,
    progress_callback: Optional[Callable[[str], None]] = None,
    overrides: Optional[dict[str, str]] = None,
) -> SolveResult:
    """
    Solve the dependency graph for a workspace rooted at *root_name*.

    Args:
        root_name: Name of the root object (engine or project)
        resolver: Already-resolved manifest (resolver.resolve() called)
        store: Optional remote store for discovering remote candidates
        progress_callback: Optional callback receiving status messages
        overrides: Optional user pins {object name -> exact version}; each is
            injected as an ``==`` root requirement so transitive dependencies
            re-resolve consistently around the pinned choice

    Returns:
        SolveResult with all resolved candidates and matched overlays
    """
    root = resolver.objects.get(root_name)
    if root is None:
        return SolveResult(
            root_name=root_name,
            root_version="",
            conflict_message=f"Root object not found: {root_name}",
        )

    provider = O3DEProvider(resolver, store)
    reporter = O3DEReporter(progress_callback)

    # Build initial requirements from the root's dependencies
    root_requirements: list[Requirement] = []
    for dep_spec in root.dependencies:
        root_requirements.append(
            Requirement(name=dep_spec.name, specifier=dep_spec.specifier)
        )

    # Inject user override pins as == root requirements
    pinned_names: set[str] = set()
    if overrides:
        for pin_name, pin_version in overrides.items():
            if pin_name == root_name:
                continue  # cannot pin the root itself
            try:
                pin_spec = SpecifierSet(f"=={pin_version}")
            except Exception:
                logger.warning(f"Invalid override version for {pin_name}: {pin_version}")
                continue
            pinned_names.add(pin_name)
            # Replace any existing root requirement for the same name
            root_requirements = [r for r in root_requirements if r.name != pin_name]
            root_requirements.append(Requirement(name=pin_name, specifier=pin_spec))

    # Run resolvelib
    rl_resolver = RLResolver(provider, reporter)
    try:
        result = rl_resolver.resolve(root_requirements)
    except RequirementsConflicted as e:
        return SolveResult(
            root_name=root_name,
            root_version=root.version,
            conflict_message=str(e),
        )
    except ResolutionImpossible as e:
        lines = []
        for info in e.causes:
            req = info.requirement
            parent = info.parent.name if info.parent else root_name
            origin = "override pin" if req.name in pinned_names and info.parent is None else parent
            lines.append(f"{origin} requires {req.name}{req.specifier} — no candidate found")
        return SolveResult(
            root_name=root_name,
            root_version=root.version,
            conflict_message="; ".join(lines) or str(e),
        )

    # Build SolveResult from resolvelib's output
    candidates: dict[str, Candidate] = {}
    for name, candidate in result.mapping.items():
        candidates[name] = candidate

    # Always include the root itself
    root_candidate = Candidate(
        name=root.name,
        version=root.version,
        object_type=root.object_type,
        status=CandidateStatus.LOCAL,
        path=root.path,
        resolved_object=root,
        dependencies=[str(d) for d in root.dependencies],
    )
    candidates[root.name] = root_candidate

    # Collect containment children separately (not dependencies)
    children: dict[str, Candidate] = {}
    _add_children_recursive(root, children)
    # Remove any children that are already in the dependency graph
    for name in list(children):
        if name in candidates:
            del children[name]

    # Match overlays
    overlays: dict[str, list[OverlayEntry]] = {}
    for overlay_obj in resolver.overlays.values():
        extends = overlay_obj.data.get("extends", "")
        extends_version = overlay_obj.data.get("extends_version")
        if not extends:
            continue

        extends_spec = ObjectNameVersion(extends)
        target = candidates.get(extends_spec.name)
        if target is None:
            continue

        # Check version compatibility
        if extends_spec.specifier:
            try:
                if Version(target.version) not in extends_spec.specifier:
                    continue
            except Exception:
                pass

        entry = OverlayEntry(
            name=overlay_obj.name,
            version=overlay_obj.version,
            extends=extends_spec.name,
            extends_version=extends_version,
            precedence=overlay_obj.data.get("precedence", 0),
            path=overlay_obj.path,
            status=CandidateStatus.LOCAL,
            platforms=[
                p for p in overlay_obj.data.get("platforms", []) or []
                if isinstance(p, str)
            ],
            overlay_deps=[
                ObjectNameVersion(d).name
                for d in (overlay_obj.data.get("dependent", {}) or {}).get("overlays", []) or []
                if isinstance(d, str)
            ],
        )
        overlays.setdefault(extends_spec.name, []).append(entry)

    # Sort each overlay group by precedence
    for entries in overlays.values():
        entries.sort(key=lambda e: e.precedence)

    solve_result = SolveResult(
        root_name=root.name,
        root_version=root.version,
        candidates=candidates,
        children=children,
        overlays=overlays,
    )

    logger.info(
        f"Solved {root_name}: {solve_result.local_count} local, "
        f"{solve_result.remote_count} remote, "
        f"{solve_result.unknown_count} unknown, "
        f"{len(overlays)} overlay groups"
    )

    return solve_result


def _add_children_recursive(
    obj: ResolvedObject,
    candidates: dict[str, Candidate],
) -> None:
    """Add all children of an object to the candidate map.

    On name collisions (e.g. PhysX4 and PhysX5 both advertised as
    engine children under the same canonical name) the newest version
    wins, consistent with the resolver's latest-compatible strategy.
    """
    for child in obj.children:
        existing = candidates.get(child.name)
        if existing is not None:
            try:
                if Version(child.version) <= Version(existing.version):
                    continue
            except Exception:
                continue
        candidates[child.name] = Candidate(
            name=child.name,
            version=child.version,
            object_type=child.object_type,
            status=CandidateStatus.LOCAL,
            path=child.path,
            resolved_object=child,
            dependencies=[str(d) for d in child.dependencies],
        )
        _add_children_recursive(child, candidates)


# ---------------------------------------------------------------------------
# Candidate enumeration + artifact detection (override support)
# ---------------------------------------------------------------------------


def current_arch() -> str:
    """Canonical architecture token for this host (AMD64, ARM64, ...)."""
    import platform as _platform

    machine = _platform.machine().lower()
    if machine in ("amd64", "x86_64", "x64"):
        return "AMD64"
    if machine in ("arm64", "aarch64"):
        return "ARM64"
    return machine.upper() or "UNKNOWN"


def current_platform() -> str:
    """O3DE platform token for this host: ``<OS>.<ARCH>``.

    E.g. ``Windows.AMD64``, ``Linux.ARM64``, ``Mac.ARM64`` — matches
    ``Release.binaries[].platform``.
    """
    if sys.platform.startswith("win"):
        os_name = "Windows"
    elif sys.platform == "darwin":
        os_name = "Mac"
    else:
        os_name = "Linux"
    return f"{os_name}.{current_arch()}"


def host_glibc() -> Optional[tuple[int, int]]:
    """The host's glibc (major, minor) on Linux, else None."""
    if not sys.platform.startswith("linux"):
        return None
    import platform as _platform

    libc, ver = _platform.libc_ver()
    if libc != "glibc" or not ver:
        return None
    try:
        major, minor = ver.split(".")[:2]
        return int(major), int(minor)
    except (ValueError, IndexError):
        return None


def platform_matches(advertised: str, host: Optional[str] = None) -> bool:
    """Case-insensitive platform token match with legacy support.

    An advertised ``<OS>.<ARCH>`` must equal the host token exactly;
    a legacy bare-OS entry (``Windows``) matches any arch of that OS.
    """
    host = (host or current_platform()).lower()
    adv = (advertised or "").strip().lower()
    if not adv:
        return False
    if adv == host:
        return True
    return adv == host.split(".", 1)[0]  # legacy bare-OS entry


def abi_compatible(binary) -> bool:
    """True if a binary entry's ABI constraints are satisfied by this host.

    Understands ``{"abi": {"glibc": "2.28"}}``: compatible when the host
    glibc is >= the floor the binary was built against.  Absent or
    unknown constraints pass (assume compatible).
    """
    abi = (
        binary.get("abi") if isinstance(binary, dict)
        else getattr(binary, "abi", None)
    )
    if not isinstance(abi, dict):
        return True
    floor = abi.get("glibc")
    if floor:
        host = host_glibc()
        if host is not None:
            try:
                major, minor = str(floor).split(".")[:2]
                return host >= (int(major), int(minor))
            except (ValueError, IndexError):
                return True
    return True


def find_local_binary_install(
    name: str,
    version: str,
    source_path: Optional[Path] = None,
) -> Optional[Path]:
    """Locate a locally built + installed binary layout for an object.

    A local binary is an *install layout* containing a CMake package config
    (``*Config.cmake``).  Searched locations:
    - ``~/.o3de/BuiltPackages/<name>-<version>/`` and ``.../<name>/``
    - ``<source_path>/install/``
    """
    roots: list[Path] = []
    built_packages = Path.home() / ".o3de" / "BuiltPackages"
    roots.append(built_packages / f"{name}-{version}")
    roots.append(built_packages / name)
    if source_path:
        roots.append(Path(source_path) / "install")

    for root in roots:
        if not root.is_dir():
            continue
        try:
            next(root.rglob("*Config.cmake"))
        except StopIteration:
            continue
        return root
    return None


def has_remote_binary(candidate: Candidate, platform: Optional[str] = None) -> bool:
    """True if a release advertises a prebuilt binary for *platform*.

    Prefers the release whose name matches the candidate version; falls back
    to any release carrying a platform-matching binary.
    """
    platform = platform or current_platform()

    releases: list = []
    if candidate.resolved_object is not None:
        releases = candidate.resolved_object.data.get("releases", []) or []
    elif candidate.remote_object is not None:
        releases = getattr(candidate.remote_object, "releases", None) or []

    def _release_has_binary(release) -> bool:
        binaries = (
            release.get("binaries", []) if isinstance(release, dict)
            else getattr(release, "binaries", []) or []
        )
        for binary in binaries:
            bin_platform = (
                binary.get("platform", "") if isinstance(binary, dict)
                else getattr(binary, "platform", "")
            )
            if platform_matches(bin_platform, platform) and abi_compatible(binary):
                return True
        return False

    # Exact version-named release first
    for release in releases:
        rel_name = (
            release.get("name", "") if isinstance(release, dict)
            else getattr(release, "name", "")
        )
        if rel_name == candidate.version and _release_has_binary(release):
            return True
    # Fallback: any release with a platform binary
    return any(_release_has_binary(r) for r in releases)


def annotate_artifacts(candidate: Candidate) -> Candidate:
    """Populate artifact availability fields on *candidate* (in place)."""
    candidate.local_binary_path = find_local_binary_install(
        candidate.name, candidate.version, candidate.path,
    )
    candidate.remote_binary = has_remote_binary(candidate)
    return candidate


def list_candidates(
    name: str,
    resolver: Resolver,
    store: Optional[Store] = None,
    specifier: str = "",
) -> list[Candidate]:
    """Enumerate ALL candidates for *name* that satisfy *specifier*.

    Returns local (every registered version, via resolver.objects_all) and
    remote (store-advertised) candidates, newest-first, each annotated with
    artifact availability (source path, local binary install, remote binary).
    """
    provider = O3DEProvider(resolver, store)
    try:
        spec = SpecifierSet(specifier)
    except Exception:
        spec = SpecifierSet()
    requirement = Requirement(name=name, specifier=spec)
    candidates = provider.find_matches(name, {name: [requirement]}, {})
    for candidate in candidates:
        annotate_artifacts(candidate)
    return candidates
