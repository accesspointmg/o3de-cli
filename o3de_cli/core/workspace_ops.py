# O3DE CLI - Workspace Operations
# SPDX-License-Identifier: Apache-2.0 OR MIT

"""Workspace operations behind the ``o3de workspace`` commands.

The Click commands in :mod:`o3de_cli.commands.workspace` parse options,
call the functions here, and render the result as Rich text or JSON.
Nothing in this module imports Click or prints.  Progress and warnings
are reported through callbacks, and failures raise
:class:`WorkspaceOpError`, which carries the ``E_*`` code the CLI
reports.  That keeps the logic reusable from the MCP server and the
o3de-pilot GUI.

Collaborators that the CLI tests patch on the command module
(``Resolver``, ``solve_for_workspace``, ``Workspace`` and the remote
overlay installer) are passed in as arguments rather than looked up
here.  The command module stays the one place they are resolved.
"""

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from o3de_cli.core.models import (
    SCHEMA_BASE_URL,
    SCHEMA_VERSION,
    ObjectOverride,
    ObjectType,
    WorkspaceMeta,
)
from o3de_cli.core.paths import rmtree_force
from o3de_cli.core.resolver import Resolver
from o3de_cli.core.solver import CandidateStatus, OverlayEntry, SolveResult
from o3de_cli.core.workspace import (
    Workspace,
    WorkspaceError,
    create_workspace,
    detect_root_type,
)

# name -> (source path, object type), the shape create_workspace consumes
ObjectMap = dict[str, tuple[Path, ObjectType]]
# (overlay path, precedence, extended base name or None)
OverlayTuple = tuple[Path, int, str | None]
# A resolver factory: ``Resolver`` itself, or a test double
ResolverFactory = Callable[[], Resolver]
# ``solve_for_workspace`` or a test double
SolveFn = Callable[..., SolveResult]
# Installs an overlay from the remote repos; returns its path or None
InstallOverlayFn = Callable[[str], Path | None]
ProgressFn = Callable[[str], None]


class WorkspaceOpError(Exception):
    """A workspace operation failed with a user-facing message and code."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


# ---------------------------------------------------------------------------
# Workspace metadata (workspace.json)
# ---------------------------------------------------------------------------

# Workspace metadata filename (visible, standard pattern)
WORKSPACE_META = "workspace.json"
# Legacy hidden filename for fallback reads
LEGACY_WORKSPACE_META = ".workspace.json"


def find_workspace_meta(ws_path: Path) -> Path | None:
    """Find workspace metadata file, preferring new name with legacy fallback."""
    meta = ws_path / WORKSPACE_META
    if meta.exists():
        return meta
    legacy = ws_path / LEGACY_WORKSPACE_META
    if legacy.exists():
        return legacy
    return None


def read_workspace_meta(ws_path: Path) -> WorkspaceMeta | None:
    """Read and validate workspace metadata via Pydantic model.

    Handles legacy `.workspace.json` files that lack `$schema`,
    `$schemaVersion`, and `workspace` header by injecting defaults.
    """
    meta_path = find_workspace_meta(ws_path)
    if meta_path is None:
        return None
    with open(meta_path) as f:
        data = json.load(f)
    # Inject defaults for legacy files missing required fields
    if "$schema" not in data:
        data["$schema"] = f"{SCHEMA_BASE_URL}/o3de-workspace-{SCHEMA_VERSION}.json"
    if "$schemaVersion" not in data:
        data["$schemaVersion"] = SCHEMA_VERSION
    if "workspace" not in data:
        data["workspace"] = {"name": data.get("name", ws_path.name)}
    if "created" not in data:
        data["created"] = ""
    return WorkspaceMeta.model_validate(data)


def write_workspace_meta(ws_path: Path, meta: WorkspaceMeta) -> None:
    """Write workspace metadata as workspace.json."""
    meta_path = ws_path / WORKSPACE_META
    data = meta.model_dump(by_alias=True, exclude_none=True)
    # Remove legacy fields that may linger from backward-compat loading
    for key in ("overlays", "file_owners", "resolved_candidates"):
        data.pop(key, None)
    with open(meta_path, "w") as f:
        json.dump(data, f, indent=2)


def build_workspace_meta(
    name: str,
    root_path: Path,
    root_type: str,
    sources: dict[str, dict[str, str]],
    file_links: dict[str, str] | None = None,
    platforms: list[str] | None = None,
    attributions: str | None = None,
) -> WorkspaceMeta:
    """Build a WorkspaceMeta model for a new workspace."""
    data = {
        "$schema": f"{SCHEMA_BASE_URL}/o3de-workspace-{SCHEMA_VERSION}.json",
        "$schemaVersion": SCHEMA_VERSION,
        "workspace": {"name": name},
        "created": datetime.now().isoformat(),
        "root_object": str(root_path),
        "root_type": root_type,
        "sources": sources,
        "file_links": file_links or {},
    }
    if platforms:
        data["platforms"] = platforms
    if attributions and attributions != "workspace":
        data["attributions"] = attributions
    return WorkspaceMeta.model_validate(data)


_SOURCE_BUCKETS: tuple[tuple[str, ObjectType], ...] = (
    ("engines", ObjectType.ENGINE),
    ("projects", ObjectType.PROJECT),
    ("gems", ObjectType.GEM),
    ("templates", ObjectType.TEMPLATE),
)


def sources_object_map(meta: WorkspaceMeta) -> ObjectMap:
    """Every linked object recorded in ``meta.sources`` (overlays excluded).

    Buckets are read engines, projects, gems, templates; a name appearing
    in two buckets keeps the later one.  Entries with an empty path are
    skipped.
    """
    objects: ObjectMap = {}
    for bucket, obj_type in _SOURCE_BUCKETS:
        for obj_name, obj_path in getattr(meta.sources, bucket).items():
            if obj_path:
                objects[obj_name] = (Path(obj_path), obj_type)
    return objects


def workspace_root_name(meta: WorkspaceMeta, resolver: Resolver) -> str | None:
    """Map the workspace's root_object path back to a manifest object name."""
    if not meta.root_object:
        return None
    root_path = Path(meta.root_object).resolve()
    for obj_name, obj in resolver.objects.items():
        if obj.path and obj.path.resolve() == root_path:
            return obj_name
    return root_path.name


def find_engine_path(meta: WorkspaceMeta) -> Path | None:
    """Find the engine path from workspace metadata."""
    for _name, path in meta.sources.engines.items():
        if path:
            return Path(path)
    # Fallback: if root_type is engine, use root_object
    if meta.root_type == "engine" and meta.root_object:
        return Path(meta.root_object)
    return None


def find_project_path(meta: WorkspaceMeta) -> Path | None:
    """Find the project path from workspace metadata."""
    for _name, path in meta.sources.projects.items():
        if path:
            return Path(path)
    # Fallback: if root_type is project, use root_object
    if meta.root_type == "project" and meta.root_object:
        return Path(meta.root_object)
    return None


def workspace_local_dir(
    ws_path: Path,
    folder: str,
    names: Iterable[str],
) -> Path | None:
    """Return the composed workspace directory for the first matching object.

    Workspaces link objects into ``<ws>/<folder>/<short_name>``.  Builds
    must run against these composed trees — not the original sources —
    so that overlays and workspace-level composition take effect.
    """
    from o3de_cli.core.workspace import _short_name

    for name in names:
        candidate = ws_path / folder / _short_name(name)
        if candidate.is_dir():
            return candidate
    return None


# ---------------------------------------------------------------------------
# Overlay selection (pure)
# ---------------------------------------------------------------------------

HOST_PLATFORM_MAP = {"Windows": "Windows", "Linux": "Linux", "Darwin": "Mac"}


def expand_platform_selection(platforms: tuple[str, ...]) -> list[str] | None:
    """Expand a --platforms selection into canonical platform names.

    Accepts repeated and comma-separated values.  Special tokens:
    ``host`` expands to the host platform; ``all`` disables filtering
    (returns None, same as omitting --platforms entirely).
    """
    if not platforms:
        return None
    import platform as platform_mod

    names: list[str] = []
    for value in platforms:
        for token in value.split(","):
            token = token.strip()
            if not token:
                continue
            if token.lower() == "all":
                return None
            if token.lower() == "host":
                token = HOST_PLATFORM_MAP.get(platform_mod.system(), platform_mod.system())
            names.append(token)
    # De-dupe preserving order, case-insensitive
    seen: set[str] = set()
    result: list[str] = []
    for n in names:
        if n.lower() not in seen:
            seen.add(n.lower())
            result.append(n)
    return result or None


def parse_tag_selection(tags: tuple[str, ...]) -> list[str] | None:
    """Parse --tags values (repeatable, comma-separated) into a de-duplicated list.

    De-duplication is case-insensitive and keeps the first spelling.
    Returns None when no tags were given.
    """
    if not tags:
        return None
    selected: list[str] = []
    for value in tags:
        for token in value.split(","):
            token = token.strip()
            if token and token.lower() not in {t.lower() for t in selected}:
                selected.append(token)
    return selected or None


@dataclass
class OverlaySelection:
    """How solved overlays are picked for a workspace (see select_overlays_for_platforms)."""

    platforms: list[str] | None = None
    tags: list[str] | None = None
    include: set[str] = field(default_factory=set)
    exclude: set[str] = field(default_factory=set)


def select_overlays_for_platforms(
    overlay_groups: dict,
    selected_platforms: list[str] | None,
    include: set[str] | None = None,
    exclude: set[str] | None = None,
    selected_tags: list[str] | None = None,
) -> dict:
    """Filter solved overlay groups by criteria and explicit picks.

    Selection rules (per overlay-selection design):
    - No criteria (platforms and tags both None) → all overlays.
    - Criteria combine with OR: an overlay is auto-selected if it
      delivers ANY selected platform OR carries ANY selected user tag.
    - Family/common overlays (no ``platforms``) that other overlays
      depend on are NOT criteria-matched — they are pulled transitively
      via ``dependent.overlays`` of included overlays.
    - Platform-agnostic overlays that nothing depends on apply
      universally and are always included.
    - ``include`` names are force-included (with their dependencies);
      ``exclude`` names are always dropped and never traversed.
    """
    include = include or set()
    exclude = exclude or set()

    if selected_platforms is None and not selected_tags and not include and not exclude:
        return overlay_groups

    selected = {p.lower() for p in selected_platforms} if selected_platforms else None
    tags = {t.lower() for t in selected_tags} if selected_tags else None
    has_criteria = selected is not None or tags is not None

    # Index all entries by name and find dependency targets
    all_entries: dict[str, OverlayEntry] = {}
    dep_targets: set[str] = set()
    for entries in overlay_groups.values():
        for ov in entries:
            all_entries[ov.name] = ov
            dep_targets.update(ov.overlay_deps)

    if has_criteria:
        queue = _seed_overlay_selection(all_entries, dep_targets, selected, tags, include, exclude)
    else:
        # No criteria: everything (minus exclusions)
        queue = [name for name in all_entries if name not in exclude]
    included = _close_overlay_dependencies(queue, all_entries, exclude)

    filtered: dict = {}
    for base, entries in overlay_groups.items():
        kept = [ov for ov in entries if ov.name in included]
        if kept:
            filtered[base] = kept
    return filtered


def _seed_overlay_selection(
    all_entries: dict[str, OverlayEntry],
    dep_targets: set[str],
    selected: set[str] | None,
    tags: set[str] | None,
    include: set[str],
    exclude: set[str],
) -> list[str]:
    """Overlays picked directly: forced includes, criteria matches, universal ones."""
    queue: list[str] = []
    for name, ov in all_entries.items():
        if name in exclude:
            continue
        if name in include:
            queue.append(name)
            continue
        plat_match = bool(
            selected and ov.platforms and {p.lower() for p in ov.platforms} & selected
        )
        tag_match = bool(
            tags and getattr(ov, "user_tags", None) and {t.lower() for t in ov.user_tags} & tags
        )
        if plat_match or tag_match:
            queue.append(name)
        elif not ov.platforms and name not in dep_targets:
            # Platform-agnostic and not a dependency target: applies always
            queue.append(name)
    return queue


def _close_overlay_dependencies(
    queue: list[str],
    all_entries: dict[str, OverlayEntry],
    exclude: set[str],
) -> set[str]:
    """Transitively pull overlay dependencies of the seeded overlays."""
    included: set[str] = set()
    while queue:
        name = queue.pop()
        if name in included or name in exclude:
            continue
        included.add(name)
        ov = all_entries.get(name)
        if ov is not None:
            for dep in ov.overlay_deps:
                if dep not in included:
                    queue.append(dep)
    return included


# ---------------------------------------------------------------------------
# Overlay metadata and apply order
# ---------------------------------------------------------------------------


def read_overlay_info(path: Path) -> dict:
    """Read overlay metadata needed for composition from an overlay dir.

    Returns ``{name, extends, precedence, deps}`` where ``extends`` is the
    base object's bare name and ``deps`` are overlay names from
    ``dependent.overlays``.
    """
    from o3de_cli.core.resolver import ObjectNameVersion

    info: dict[str, Any] = {"name": path.name, "extends": None, "precedence": 0, "deps": []}
    for fname in ("overlay.2-0-0.json", "overlay.json"):
        candidate = path / fname
        if not candidate.exists():
            continue
        try:
            with open(candidate, encoding="utf-8-sig") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        header = data.get("overlay", {}) if isinstance(data.get("overlay"), dict) else {}
        info["name"] = header.get("name") or info["name"]
        extends = data.get("extends", "")
        if extends:
            info["extends"] = ObjectNameVersion(extends).name
        info["precedence"] = data.get("precedence", 0)
        info["deps"] = [
            ObjectNameVersion(d).name
            for d in (data.get("dependent", {}) or {}).get("overlays", []) or []
            if isinstance(d, str)
        ]
        break
    return info


def ordered_overlay_paths(
    entries: list[tuple[str, Path, int]],
    explicit: list[str],
) -> list[tuple[Path, int]]:
    """Final apply order for overlays extending one object.

    *entries* are ``(name, path, authored precedence)``.  They are sorted
    by precedence (then name); an *explicit* workspace order then moves
    the named overlays to the front in the given order.  Returns
    ``(path, sequence)`` tuples for :func:`relink_object`.
    """
    ordered = sorted(entries, key=lambda t: (t[2], t[0]))
    if explicit:
        named = [e for n in explicit for e in ordered if e[0] == n]
        rest = [e for e in ordered if e[0] not in set(explicit)]
        ordered = named + rest
    return [(p, i) for i, (_n, p, _prec) in enumerate(ordered)]


def group_overlays_by_base(overlays: dict[str, str]) -> dict[str, list[tuple[str, Path, int]]]:
    """Group ``name -> path`` overlays by the base object each one extends.

    Overlays that extend nothing are left out.  Values are
    ``(name, path, authored precedence)``.
    """
    by_base: dict[str, list[tuple[str, Path, int]]] = {}
    for ov_name, ov_path in overlays.items():
        info = read_overlay_info(Path(ov_path))
        if info["extends"]:
            by_base.setdefault(info["extends"], []).append(
                (ov_name, Path(ov_path), info["precedence"])
            )
    return by_base


def overlays_for_base(meta: WorkspaceMeta, base_name: str) -> list[tuple[Path, int]]:
    """Return the workspace's composed overlays extending *base_name* in
    final apply order (authored precedence, then meta.overlay_order),
    as ``(path, sequence)`` tuples for :func:`relink_object`."""
    entries: list[tuple[str, Path, int]] = []
    for ov_name, ov_path in meta.sources.overlays.items():
        if not ov_path:
            continue
        info = read_overlay_info(Path(ov_path))
        if info["extends"] == base_name:
            entries.append((ov_name, Path(ov_path), info["precedence"]))
    explicit = (meta.overlay_order or {}).get(base_name) or []
    return ordered_overlay_paths(entries, explicit)


def parse_overlay_order(
    values: Iterable[str],
    paths_by_name: dict[str, Path],
    not_found_message: str,
) -> dict[str, list[str]]:
    """Parse ``--overlay-order`` values into ``{base name: overlay names}``.

    Each value is a comma-separated list of at least two overlay names,
    all of which must be in *paths_by_name* and extend the same object.
    A later value for the same base replaces an earlier one.  Raises
    :class:`WorkspaceOpError` (``E_OVERLAY_ORDER``); an unknown name is
    reported as ``f"{not_found_message}: {name}"``.
    """
    order: dict[str, list[str]] = {}
    for value in values:
        names = [t.strip() for t in value.split(",") if t.strip()]
        if len(names) < 2:
            raise WorkspaceOpError(
                "--overlay-order needs at least two comma-separated overlay names",
                "E_OVERLAY_ORDER",
            )
        bases: set[str] = set()
        for n in names:
            if n not in paths_by_name:
                raise WorkspaceOpError(f"{not_found_message}: {n}", "E_OVERLAY_ORDER")
            bases.add(read_overlay_info(paths_by_name[n])["extends"] or "")
        if len(bases) != 1:
            raise WorkspaceOpError(
                "--overlay-order: overlays must all extend the same object",
                "E_OVERLAY_ORDER",
            )
        order[bases.pop()] = names
    return order


def clear_attribution_records(ws_path: Path, meta: WorkspaceMeta, names: Iterable[str]) -> None:
    """Delete the workspace attribution ledger (``Overlays/<name>/``) for *names*.

    Drops the matching ``meta.file_links`` entries too.
    """
    for ov_name in names:
        attr_dir = ws_path / "Overlays" / ov_name
        if attr_dir.exists():
            rmtree_force(attr_dir)
        attr_prefix = f"Overlays/{ov_name}/"
        meta.file_links = {
            src: rel for src, rel in meta.file_links.items() if not rel.startswith(attr_prefix)
        }


# ---------------------------------------------------------------------------
# Relinking one object
# ---------------------------------------------------------------------------

_SOURCE_BUCKET_BY_TYPE = {
    ObjectType.ENGINE: "engines",
    ObjectType.PROJECT: "projects",
    ObjectType.GEM: "gems",
    ObjectType.TEMPLATE: "templates",
}


def relink_object(
    ws_path: Path,
    meta: WorkspaceMeta,
    name: str,
    new_path: Path,
    obj_type: ObjectType,
    all_objects: ObjectMap,
    overlays: list[tuple[Path, int]] | None = None,
) -> None:
    """Replace one object's linked tree in the workspace with *new_path*.

    Removes the old destination directory, relinks from the new source,
    re-applies *overlays* (``(path, precedence)`` tuples, applied in
    precedence order INTO the object's tree), and updates meta.sources +
    meta.file_links in place.
    """
    from o3de_cli.core.workspace import (
        _ENGINE_SKIP_TOPDIRS,
        _TYPE_FOLDERS,
        _object_name_from_path,
        _short_name,
    )

    folder = _TYPE_FOLDERS.get(obj_type, "Gems")
    short = _short_name(name)
    dest_root = ws_path / folder / short
    dest_prefix = f"{folder}/{short}/"

    # Remove the old linked tree (hard links — sources unaffected)
    if dest_root.exists():
        rmtree_force(dest_root)

    # Drop stale file_links for this object
    meta.file_links = {
        src: rel for src, rel in meta.file_links.items() if not rel.startswith(dest_prefix)
    }

    # Relink from the new source using the standard composer (nested-object
    # boundary detection needs the full resolved object map)
    ws = Workspace(
        root_path=ws_path,
        root_object_path=Path(meta.root_object) if meta.root_object else new_path,
        root_object_type=obj_type,
    )
    ws.resolved_objects = dict(all_objects)
    ws.resolved_objects[name] = (new_path, obj_type)
    ws._link_object_files(
        source_root=new_path,
        dest_root=dest_root,
        owner_name=name,
        skip_topdirs=_ENGINE_SKIP_TOPDIRS if obj_type == ObjectType.ENGINE else None,
    )

    # Re-apply overlays into the freshly relinked tree, precedence order
    if overlays:
        attributions = meta.attributions or "workspace"
        for ov_path, _prec in sorted(overlays, key=lambda t: t[1]):
            ov_name = _object_name_from_path(ov_path, ov_path.name)
            ws._apply_overlay(
                ov_path,
                owner_name=ov_name,
                base_dest_root=dest_root,
                attributions=attributions,
            )

    meta.file_links.update(ws.get_file_links())

    # Update the sources bucket
    bucket_key = _SOURCE_BUCKET_BY_TYPE.get(obj_type, "gems")
    getattr(meta.sources, bucket_key)[name] = str(new_path)


# ---------------------------------------------------------------------------
# workspace create
# ---------------------------------------------------------------------------


def select_root(
    root_opt: str | None,
    project_path: str | None,
    engine_path: str | None,
) -> tuple[Path, ObjectType]:
    """Pick the workspace root object and detect its type.

    The root anchors the dependency solve (dependencies radiate from it),
    so when a project is given it is the root — its dependent.engines /
    dependent.gems pull everything else in.  Engine-only workspaces (no
    project) root at the engine, which is valid for engine development.
    ``--root`` wins over both and accepts any object type (gem/template
    development).  At least one of the three must be given.
    """
    chosen = root_opt or project_path or engine_path
    assert chosen is not None
    root_path = Path(chosen).resolve()
    return root_path, detect_root_type(root_path)


def secondary_engine_objects(engine_path: str | None, root_path: Path) -> ObjectMap:
    """An explicit ``--engine`` that is not the root, as a resolved object.

    The root's dependent.engines normally resolves an engine anyway; an
    explicit path wins if they differ, and gem/template roots have no
    engine dependency at all, so this is how their workspaces get an
    engine to build against.
    """
    if not engine_path or Path(engine_path).resolve() == root_path:
        return {}
    from o3de_cli.core.workspace import _object_name_from_path

    eng_path = Path(engine_path).resolve()
    return {_object_name_from_path(eng_path, eng_path.name): (eng_path, ObjectType.ENGINE)}


def explicit_overlay_tuples(overlays: Iterable[str], no_overlays: bool) -> list[OverlayTuple]:
    """``--overlay`` paths as overlay tuples, precedence in the order given."""
    if no_overlays:
        return []
    return [(Path(o).resolve(), i, None) for i, o in enumerate(overlays)]


def manifest_name_for_path(resolver: Resolver, path: Path) -> str:
    """The manifest object name registered at *path* (directory name if none)."""
    for obj_name, obj in resolver.objects.items():
        if obj.path and obj.path.resolve() == path:
            return obj_name
    return path.name


def local_objects(solve_result: SolveResult, exclude_path: Path | None = None) -> ObjectMap:
    """LOCAL candidates and children of a solve, as resolved objects.

    Only local candidates can be assembled into a workspace.  An object
    at *exclude_path* (the root, which create_workspace adds itself) is
    skipped.  Children are read after candidates and win on a name clash.
    """
    objects: ObjectMap = {}
    for cmap in (solve_result.candidates, solve_result.children):
        for cand_name, cand in cmap.items():
            if cand.status == CandidateStatus.LOCAL and cand.path:
                if exclude_path is not None and cand.path.resolve() == exclude_path:
                    continue
                objects[cand_name] = (cand.path, cand.object_type)
    return objects


def engines_needing_closure(resolved: ObjectMap, solve_result: SolveResult) -> list[str]:
    """Engines in *resolved* that the root's solve did not pull in.

    Gem/template roots have no engine dependency, so an explicit engine
    needs its own dependency closure: an engine without its
    default-enabled gems cannot configure.
    """
    return [
        n
        for n, (_p, t) in resolved.items()
        if t == ObjectType.ENGINE
        and n not in solve_result.candidates
        and n not in solve_result.children
    ]


def merge_engine_closure(
    resolved: ObjectMap,
    overlay_groups: dict[str, list[OverlayEntry]],
    eng_solve: SolveResult,
    root_path: Path,
) -> None:
    """Merge a secondary engine's solve into *resolved* and *overlay_groups*.

    Objects already resolved (and the root) are kept as they are.
    Overlays extending closure-pulled objects must compose too, so the
    engine solve's overlay groups are merged in by name and re-sorted
    by precedence.
    """
    for cmap in (eng_solve.candidates, eng_solve.children):
        for cand_name, cand in cmap.items():
            if (
                cand.status == CandidateStatus.LOCAL
                and cand.path
                and cand.path.resolve() != root_path
                and cand_name not in resolved
            ):
                resolved[cand_name] = (cand.path, cand.object_type)
    merge_overlay_groups(overlay_groups, eng_solve.overlays)


def merge_overlay_groups(
    overlay_groups: dict[str, list[OverlayEntry]],
    new_groups: dict[str, list[OverlayEntry]],
) -> None:
    """Merge *new_groups* into *overlay_groups* by overlay name.

    An overlay already in a base's group keeps its entry.  Each group
    touched is re-sorted by precedence.
    """
    for base, entries in new_groups.items():
        group = overlay_groups.setdefault(base, [])
        existing = {e.name for e in group}
        group.extend(e for e in entries if e.name not in existing)
        group.sort(key=lambda e: e.precedence)


def merge_new_objects(resolved: ObjectMap, solve_result: SolveResult, root_path: Path) -> None:
    """Add the LOCAL objects of *solve_result* that *resolved* lacks.

    An object is already there when its manifest name is, or when its
    resolved path is (the root counts: create_workspace adds it itself).
    What was selected first is kept, so an explicit ``--engine`` keeps
    its path and no directory is linked twice under two names.
    """
    seen = {path.resolve() for path, _type in resolved.values()}
    seen.add(root_path)
    for cand_name, (path, obj_type) in local_objects(solve_result).items():
        resolved_path = path.resolve()
        if cand_name in resolved or resolved_path in seen:
            continue
        resolved[cand_name] = (path, obj_type)
        seen.add(resolved_path)


def add_selected_overlays(
    overlay_tuples: list[OverlayTuple],
    overlay_groups: dict[str, list[OverlayEntry]],
    selection: OverlaySelection,
) -> None:
    """Append the selected LOCAL solved overlays to *overlay_tuples*.

    Overlays already present (by resolved path, e.g. given explicitly
    with ``--overlay``) are not added twice.
    """
    selected_groups = select_overlays_for_platforms(
        overlay_groups,
        selection.platforms,
        include=selection.include,
        exclude=selection.exclude,
        selected_tags=selection.tags,
    )
    for _base, entries in selected_groups.items():
        for ov in entries:
            if ov.path and ov.status == CandidateStatus.LOCAL:
                ov_resolved = ov.path.resolve()
                if not any(p.resolve() == ov_resolved for p, *_ in overlay_tuples):
                    overlay_tuples.append((ov_resolved, ov.precedence, ov.extends))


def apply_overlay_order(
    overlay_tuples: list[OverlayTuple],
    order_values: tuple[str, ...],
) -> tuple[list[OverlayTuple], dict[str, list[str]]]:
    """Apply ``--overlay-order`` to the overlays selected for a new workspace.

    Named overlays come first in the given order and the rest keep their
    precedence order.  The result is renumbered with sequential
    precedences, so the composer's precedence sort realizes the final
    order.  Returns the reordered tuples and the ``{base: names}`` map to
    persist.  With no values or no overlays, nothing changes.  Raises
    :class:`WorkspaceOpError` (``E_OVERLAY_ORDER``).
    """
    if not order_values or not overlay_tuples:
        return overlay_tuples, {}
    from o3de_cli.core.workspace import _object_name_from_path

    name_by_path = {
        p.resolve(): _object_name_from_path(p.resolve(), p.name) for p, *_ in overlay_tuples
    }
    paths_by_name: dict[str, Path] = {}
    for p, nm in name_by_path.items():
        paths_by_name.setdefault(nm, p)
    order_map = parse_overlay_order(
        order_values,
        paths_by_name,
        "--overlay-order: overlay not selected for this workspace",
    )
    if not order_map:
        return overlay_tuples, order_map

    explicit_pos: dict[Path, int] = {}
    for names in order_map.values():
        for i, n in enumerate(names):
            for p, nm in name_by_path.items():
                if nm == n:
                    explicit_pos[p] = i
    ordered = sorted(
        overlay_tuples,
        key=lambda t: (explicit_pos.get(t[0].resolve(), 10_000 + t[1]),),
    )
    return [(p, i, ext) for i, (p, _prec, ext) in enumerate(ordered)], order_map


@dataclass
class CreateRequest:
    """What ``workspace create`` was asked to compose (dependency phase)."""

    root_path: Path
    engine_path: str | None = None
    project_path: str | None = None
    explicit_overlays: tuple[str, ...] = ()
    no_overlays: bool = False
    selection: OverlaySelection = field(default_factory=OverlaySelection)
    include_store: bool = False
    auto_install: bool = False


@dataclass
class WorkspaceAssembly:
    """The objects and overlays a new workspace will be composed from."""

    resolved_objects: ObjectMap
    overlay_tuples: list[OverlayTuple]
    solve_result: SolveResult | None = None


@dataclass
class SolveDeps:
    """Collaborators of the dependency phase (injected so callers control them)."""

    resolver_cls: ResolverFactory
    solve: SolveFn
    install_overlay: InstallOverlayFn


class CreateReporter:
    """Receives progress and warnings from :func:`solve_create_dependencies`.

    Every method is a no-op here; the CLI subclasses it to drive its
    spinner and console.
    """

    def progress(self, message: str) -> None:
        """The current step changed."""

    def overlay_unavailable(self, name: str) -> None:
        """A force-included overlay is neither installed nor available remotely."""

    def remote_installed(self, count: int) -> None:
        """--auto-install installed *count* remote dependencies."""

    def auto_install_needs_store(self) -> None:
        """--auto-install was given without --include-store."""

    def root_not_registered(self) -> None:
        """The root is not in the manifest, so dependency resolution is skipped."""

    def unresolved(self, root_name: str, remote_count: int, unknown_count: int) -> None:
        """Some dependencies are remote-only or could not be found."""


class SolveFailedError(WorkspaceOpError):
    """A dependency solve during ``workspace create`` did not resolve.

    ``label`` names the stage ("Dependency resolution failed", ...) and
    ``detail`` is the solver's conflict message.
    """

    def __init__(self, label: str, detail: str) -> None:
        super().__init__(f"{label}: {detail}", "E_SOLVE_CONFLICT")
        self.label = label
        self.detail = detail


def _solve_or_raise(
    deps: SolveDeps,
    root_name: str,
    resolver: Resolver,
    store: Any,
    reporter: CreateReporter,
    failure_label: str,
) -> SolveResult:
    result = deps.solve(
        root_name=root_name,
        resolver=resolver,
        store=store,
        progress_callback=reporter.progress,
    )
    if not result.is_resolved:
        raise SolveFailedError(failure_label, result.conflict_message)
    return result


def _resolver_with_includes(
    includes: set[str], deps: SolveDeps, reporter: CreateReporter
) -> Resolver:
    """Resolve the manifest, first acquiring force-included overlays that
    aren't installed locally from the registered remote repos."""
    resolver = deps.resolver_cls()
    resolver.resolve()
    missing = [n for n in includes if n not in resolver.overlays]
    if not missing:
        return resolver
    reporter.progress("Downloading remote overlays...")
    installed_any = False
    for ov_name in missing:
        if deps.install_overlay(ov_name) is not None:
            installed_any = True
        else:
            reporter.overlay_unavailable(ov_name)
    if installed_any:
        resolver = deps.resolver_cls()
        resolver.resolve()
    return resolver


def _refresh_store(resolver: Resolver, reporter: CreateReporter) -> Any:
    from o3de_cli.core.store import Store

    reporter.progress("Refreshing store...")
    store = Store()
    store.refresh_sync(resolver.manifest_remotes)
    return store


def solve_create_dependencies(
    request: CreateRequest,
    assembly: WorkspaceAssembly,
    deps: SolveDeps,
    reporter: CreateReporter,
) -> None:
    """Resolve the root's dependency closure into *assembly* (create phase 1).

    Adds every LOCAL solved object, the closures of secondary engines,
    and the selected solved overlays.  With ``auto_install`` (and a
    store), missing remote dependencies are installed and the solve is
    redone.  Raises :class:`SolveFailedError` when a solve fails.
    """
    resolver = _resolver_with_includes(request.selection.include, deps, reporter)
    store = _refresh_store(resolver, reporter) if request.include_store else None
    reporter.progress("Solving dependencies...")

    # Only solve when the root is registered in the manifest.  For a path
    # that isn't registered yet the explicit --engine/--project paths are
    # all there is.
    root_name = manifest_name_for_path(resolver, request.root_path)
    if root_name not in resolver.objects:
        reporter.root_not_registered()
        return

    solve_result = _solve_or_raise(
        deps, root_name, resolver, store, reporter, "Dependency resolution failed"
    )
    assembly.solve_result = solve_result
    resolved = assembly.resolved_objects
    resolved.update(local_objects(solve_result, exclude_path=request.root_path))

    # Overlay groups accumulate across the root solve and any secondary
    # engine closure solve; selection happens once after all solves.
    overlay_groups = {base: list(entries) for base, entries in solve_result.overlays.items()}
    for eng_name in engines_needing_closure(resolved, solve_result):
        if eng_name not in resolver.objects:
            continue
        reporter.progress(f"Solving engine {eng_name}...")
        eng_solve = _solve_or_raise(
            deps, eng_name, resolver, store, reporter, "Engine dependency resolution failed"
        )
        merge_engine_closure(resolved, overlay_groups, eng_solve, request.root_path)

    if not request.no_overlays:
        add_selected_overlays(assembly.overlay_tuples, overlay_groups, request.selection)

    remote_count = solve_result.remote_count
    unknown_count = solve_result.unknown_count
    if remote_count and request.auto_install and store:
        installed = _auto_install_remote(
            request, assembly, deps, reporter, resolver, store, root_name, overlay_groups
        )
        if installed and assembly.solve_result is not None:
            remote_count = assembly.solve_result.remote_count
            unknown_count = assembly.solve_result.unknown_count
    elif remote_count and request.auto_install and not store:
        reporter.auto_install_needs_store()

    if remote_count or unknown_count:
        reporter.unresolved(root_name, remote_count, unknown_count)


def _auto_install_remote(
    request: CreateRequest,
    assembly: WorkspaceAssembly,
    deps: SolveDeps,
    reporter: CreateReporter,
    resolver: Resolver,
    store: Any,
    root_name: str,
    overlay_groups: dict[str, list[OverlayEntry]],
) -> bool:
    """Install missing remote dependencies, then re-solve into *assembly*.

    Returns True when anything was installed (and the solves were redone).
    The objects and overlays already selected are kept, and what the
    re-solves add is merged in (see :func:`merge_new_objects`): the
    root's re-solve, then the re-solve of each secondary engine closure,
    since the install may have completed those too.  *overlay_groups* are
    the overlays of the solves before the install.  Overlay selection
    runs over the overlays of every solve; overlays already selected are
    not added twice.
    """
    reporter.progress("Installing remote dependencies...")
    installed = resolver.auto_install_missing(
        store=store,
        confirm=True,
        progress_callback=lambda msg, _current, _total: reporter.progress(msg),
    )
    if not installed:
        return False

    reporter.remote_installed(len(installed))
    # Re-resolve and re-solve to pick up newly-local objects
    reporter.progress("Re-resolving after install...")
    resolver.resolve()
    reporter.progress("Re-solving dependencies...")
    solve_result = _solve_or_raise(
        deps, root_name, resolver, store, reporter, "Re-solve after install failed"
    )
    assembly.solve_result = solve_result

    resolved = assembly.resolved_objects
    merge_new_objects(resolved, solve_result, request.root_path)
    # The re-solves' overlay entries come first: an overlay that was
    # remote before the install is local now
    groups = {base: list(entries) for base, entries in solve_result.overlays.items()}
    for eng_name in engines_needing_closure(resolved, solve_result):
        if eng_name not in resolver.objects:
            continue
        reporter.progress(f"Solving engine {eng_name}...")
        eng_solve = _solve_or_raise(
            deps, eng_name, resolver, store, reporter, "Engine dependency resolution failed"
        )
        merge_new_objects(resolved, eng_solve, request.root_path)
        merge_overlay_groups(groups, eng_solve.overlays)
    merge_overlay_groups(groups, overlay_groups)

    if not request.no_overlays:
        add_selected_overlays(assembly.overlay_tuples, groups, request.selection)
    return True


def assemble_workspace(
    output_path: Path,
    name: str,
    root_path: Path,
    root_type: ObjectType,
    assembly: WorkspaceAssembly,
    attributions: str,
    platforms: list[str] | None,
    overlay_order: dict[str, list[str]],
) -> WorkspaceMeta:
    """Compose the workspace on disk and write its workspace.json (create phase 2)."""
    workspace_obj = create_workspace(
        target_path=output_path,
        root_object_path=root_path,
        resolved_objects=assembly.resolved_objects,
        overlays=assembly.overlay_tuples,
        attributions=attributions,
    )
    meta = build_workspace_meta(
        name=name,
        root_path=root_path,
        root_type=root_type.value,
        sources=workspace_obj.get_sources_dict(),
        file_links=workspace_obj.get_file_links(),
        platforms=platforms,
        attributions=attributions,
    )
    if overlay_order:
        meta.overlay_order = overlay_order
    write_workspace_meta(output_path, meta)
    return meta


# ---------------------------------------------------------------------------
# workspace update: re-sync
# ---------------------------------------------------------------------------


def detect_source_type(path: Path) -> ObjectType:
    """Object type of a source directory from its legacy JSON (default: gem)."""
    if (path / "engine.json").exists():
        return ObjectType.ENGINE
    if (path / "project.json").exists():
        return ObjectType.PROJECT
    return ObjectType.GEM


@dataclass
class ResyncPlan:
    """What a plain ``workspace update`` relinks."""

    root_source: Path
    root_type: ObjectType
    sources: list[Path]
    overlays: list[Path]
    # Name and type workspace.json records for each source path
    recorded: dict[Path, tuple[str, ObjectType]] = field(default_factory=dict)


def plan_resync(ws_path: Path, meta: WorkspaceMeta, extra_overlays: Iterable[str]) -> ResyncPlan:
    """Reconstruct the workspace's sources, root and overlays from metadata.

    *extra_overlays* are appended after the recorded overlays.  The root
    is the object recorded at creation.  Guessing it from the sources
    would pick the engine (engines are listed first) and silently turn a
    project- or gem-rooted workspace into an engine-rooted one.

    Each source keeps the canonical name and type it was recorded under,
    so the re-sync links it where creation did and overlays find the
    object they extend.
    """
    sources: list[Path] = []
    recorded: dict[Path, tuple[str, ObjectType]] = {}
    for bucket, obj_type in _SOURCE_BUCKETS:
        for obj_name, path in getattr(meta.sources, bucket).items():
            if path:
                sources.append(Path(path))
                recorded[Path(path)] = (obj_name, obj_type)
    overlays = [Path(p) for p in meta.sources.overlays.values()]
    overlays += [Path(o).resolve() for o in extra_overlays]

    if meta.root_object:
        root_source = Path(meta.root_object)
    else:
        root_source = sources[0] if sources else ws_path
    try:
        root_type = ObjectType(meta.root_type) if meta.root_type else detect_root_type(root_source)
    except (ValueError, WorkspaceError):
        root_type = ObjectType.ENGINE  # fallback
    return ResyncPlan(root_source, root_type, sources, overlays, recorded)


def apply_resync(
    ws_path: Path,
    plan: ResyncPlan,
    workspace_cls: Callable[..., Workspace] = Workspace,
) -> None:
    """Relink every source and overlay of *plan* into the workspace.

    Sources use their recorded name and type (directory name and detected
    type for any not recorded).  Each overlay is given the base it
    extends, read from its overlay.json as at creation; without it the
    composer cannot place the overlay and skips it.
    """
    from o3de_cli.core.workspace import _overlay_extends_name

    workspace_obj = workspace_cls(
        root_path=ws_path,
        root_object_path=plan.root_source,
        root_object_type=plan.root_type,
    )
    for source in plan.sources:
        name, obj_type = plan.recorded.get(source) or (source.name, detect_source_type(source))
        workspace_obj.add_resolved_object(name, source, obj_type)
    for i, overlay_path in enumerate(plan.overlays):
        workspace_obj.add_overlay(
            overlay_path, precedence=i, extends=_overlay_extends_name(overlay_path)
        )
    workspace_obj.update()


# ---------------------------------------------------------------------------
# workspace update: overlay set / order / attribution changes
# ---------------------------------------------------------------------------


def attributions_change(meta: WorkspaceMeta, attributions: str | None) -> bool:
    """Whether *attributions* differs from the workspace's current mode."""
    return attributions is not None and attributions != (meta.attributions or "workspace")


@dataclass
class OverlayChange:
    """A requested change to a workspace's composed overlays."""

    add: tuple[str, ...] = ()
    remove: tuple[str, ...] = ()
    order: tuple[str, ...] = ()
    # New attribution mode, or None to keep the current one
    attributions: str | None = None


@dataclass
class OverlayChangeResult:
    """Outcome of :func:`change_overlays`."""

    added: dict[str, str]
    removed: set[str]
    ordered_bases: set[str]
    order_map: dict[str, list[str]]
    recomposed: list[str]
    skipped: list[str]
    overlays: dict[str, str]
    old_attributions: str
    attributions_changed: bool


def resolve_overlay_additions(
    names: Iterable[str],
    current: dict[str, str],
    resolver_cls: ResolverFactory,
    install_overlay: InstallOverlayFn,
    on_download: ProgressFn | None = None,
) -> dict[str, str]:
    """Resolve overlays to add (and their overlay dependencies) to paths.

    Overlays not installed locally are acquired from the registered
    remote repos via *install_overlay*.  Names already in *current* are
    skipped.  Raises :class:`WorkspaceOpError` (``E_OVERLAY_NOT_FOUND``).
    """
    resolver = resolver_cls()
    resolver.resolve()
    adds: dict[str, str] = {}
    pending = list(names)
    while pending:
        ov_name = pending.pop(0)
        if ov_name in current or ov_name in adds:
            continue
        ov = resolver.overlays.get(ov_name)
        if ov is None or not ov.path:
            if on_download is not None:
                on_download(ov_name)
            if install_overlay(ov_name) is None:
                raise WorkspaceOpError(
                    f"Overlay not installed locally and not found in remote repos: {ov_name}",
                    "E_OVERLAY_NOT_FOUND",
                )
            resolver = resolver_cls()
            resolver.resolve()
            ov = resolver.overlays.get(ov_name)
            if ov is None or not ov.path:
                raise WorkspaceOpError(f"Overlay install failed: {ov_name}", "E_OVERLAY_NOT_FOUND")
        adds[ov_name] = str(ov.path)
        # Pull overlay dependencies along
        for dep in read_overlay_info(ov.path)["deps"]:
            if dep not in current and dep not in adds:
                pending.append(dep)
    return adds


def check_overlay_removals(
    removes: set[str], current: dict[str, str], new_set: dict[str, str]
) -> None:
    """Every removal must be in the workspace, and no remaining overlay may
    depend on a removed one.  Raises :class:`WorkspaceOpError`."""
    for ov_name in removes:
        if ov_name not in current:
            raise WorkspaceOpError(f"Overlay not in workspace: {ov_name}", "E_OVERLAY_NOT_IN_WS")
    for ov_name, ov_path in new_set.items():
        info = read_overlay_info(Path(ov_path))
        broken = [d for d in info["deps"] if d in removes]
        if broken:
            raise WorkspaceOpError(
                f"Cannot remove {', '.join(broken)}: still required by "
                f"{ov_name} (remove it too or keep the dependency)",
                "E_OVERLAY_DEP",
            )


def prune_overlay_order(order_map: dict[str, list[str]], removes: set[str]) -> dict[str, list[str]]:
    """Drop removed overlays from a persisted order; bases left empty go too."""
    pruned: dict[str, list[str]] = {}
    for base, names in order_map.items():
        kept = [n for n in names if n not in removes]
        if kept:
            pruned[base] = kept
    return pruned


def affected_bases(
    ordered_bases: set[str],
    adds: dict[str, str],
    removes: set[str],
    current: dict[str, str],
) -> set[str]:
    """Base objects to recompose: reordered ones plus those whose overlays changed."""
    affected = set(ordered_bases)
    for ov_name in set(adds) | removes:
        src = adds.get(ov_name) or current.get(ov_name)
        if src:
            info = read_overlay_info(Path(src))
            if info["extends"]:
                affected.add(info["extends"])
    return affected


def recompose_bases(
    ws_path: Path,
    meta: WorkspaceMeta,
    bases: Iterable[str],
    overlays_by_base: dict[str, list[tuple[str, Path, int]]],
    order_map: dict[str, list[str]],
) -> tuple[list[str], list[str]]:
    """Relink each base object with its overlays in final apply order.

    Returns ``(recomposed, skipped)``; a base that is not linked in the
    workspace is skipped.
    """
    all_objects = sources_object_map(meta)
    recomposed: list[str] = []
    skipped: list[str] = []
    for base_name in bases:
        entry = all_objects.get(base_name)
        if entry is None:
            skipped.append(base_name)
            continue
        base_path, base_type = entry
        relink_object(
            ws_path,
            meta,
            base_name,
            base_path,
            base_type,
            all_objects,
            overlays=ordered_overlay_paths(
                overlays_by_base.get(base_name, []), order_map.get(base_name) or []
            ),
        )
        recomposed.append(base_name)
    return recomposed, skipped


def change_overlays(
    ws_path: Path,
    meta: WorkspaceMeta,
    change: OverlayChange,
    *,
    resolver_cls: ResolverFactory,
    install_overlay: InstallOverlayFn,
    on_download: ProgressFn | None = None,
) -> OverlayChangeResult:
    """Change a workspace's overlay set, apply order and/or attribution mode.

    Affected extended objects are recomposed, attribution records are
    migrated, and workspace.json is rewritten.  The CMake manifest is
    left to the caller.  Raises :class:`WorkspaceOpError`.
    """
    current: dict[str, str] = dict(meta.sources.overlays)
    adds = resolve_overlay_additions(
        change.add, current, resolver_cls, install_overlay, on_download
    )
    removes = set(change.remove)
    new_set: dict[str, str] = {n: p for n, p in current.items() if n not in removes}
    new_set.update(adds)
    check_overlay_removals(removes, current, new_set)

    overlays_by_base = group_overlays_by_base(new_set)
    order_map = prune_overlay_order(dict(meta.overlay_order or {}), removes)
    requested_order = parse_overlay_order(
        change.order,
        {n: Path(p) for n, p in new_set.items()},
        "--overlay-order: overlay not in workspace",
    )
    order_map.update(requested_order)
    ordered_bases = set(requested_order)
    affected = affected_bases(ordered_bases, adds, removes, current)

    # Attribution mode change: recomposing every base with overlays
    # re-places attribution at the new location (and wipes object-mode
    # records); the workspace ledger must be cleared explicitly when
    # leaving workspace mode.
    old_mode = meta.attributions or "workspace"
    attr_change = attributions_change(meta, change.attributions)
    if attr_change:
        affected |= set(overlays_by_base)
        meta.attributions = None if change.attributions == "workspace" else change.attributions
        if old_mode == "workspace":
            clear_attribution_records(ws_path, meta, new_set)

    recomposed, skipped = recompose_bases(
        ws_path, meta, sorted(affected), overlays_by_base, order_map
    )
    # Object-mode attribution of removed overlays is wiped by the recompose
    clear_attribution_records(ws_path, meta, removes)

    # Persist the new frozen overlay set and apply order
    meta.sources.overlays = new_set
    meta.overlay_order = order_map or None
    write_workspace_meta(ws_path, meta)
    return OverlayChangeResult(
        added=adds,
        removed=removes,
        ordered_bases=ordered_bases,
        order_map=order_map,
        recomposed=recomposed,
        skipped=skipped,
        overlays=new_set,
        old_attributions=old_mode,
        attributions_changed=attr_change,
    )


# ---------------------------------------------------------------------------
# workspace override
# ---------------------------------------------------------------------------


@dataclass
class OverrideRequest:
    """Pin (or clear the pin of) one object in a workspace."""

    object_name: str
    version: str | None = None
    artifact: str = "source"
    clear: bool = False
    apply: bool = True


def _release_data(resolver: Resolver, name: str, version: str) -> dict:
    """Manifest data of *name* at *version* (or its newest version)."""
    versions = getattr(resolver, "objects_all", {}).get(name, {})
    resolved = versions.get(version) or resolver.objects.get(name)
    return resolved.data if resolved is not None else {}


def make_override(
    object_name: str,
    version: str,
    artifact: str,
    resolver: Resolver,
    resolver_cls: ResolverFactory,
    on_progress: ProgressFn,
) -> tuple[ObjectOverride, Resolver]:
    """Build the override for one artifact form, acquiring it if needed.

    ``local-binary`` locates an install layout, ``remote-binary``
    downloads the release package, and ``remote-source`` downloads,
    registers and re-resolves the source release, which then becomes a
    plain ``source`` override.  Returns the override and the resolver to
    solve with (a fresh one after ``remote-source``).  Raises
    :class:`WorkspaceOpError`.
    """
    override = ObjectOverride(version=version, artifact=artifact)
    if artifact == "local-binary":
        from o3de_cli.core.solver import find_local_binary_install

        binary = find_local_binary_install(object_name, version)
        if binary is None:
            raise WorkspaceOpError(
                f"No local binary install found for "
                f"{object_name}@{version} (expected an install layout "
                f"with *Config.cmake under ~/.o3de/BuiltPackages)",
                "E_NO_LOCAL_BINARY",
            )
        override.path = str(binary)
    elif artifact == "remote-binary":
        from o3de_cli.core.gem_package import download_remote_binary

        data = _release_data(resolver, object_name, version)
        try:
            pkg = download_remote_binary(object_name, version, data, on_progress=on_progress)
        except Exception as e:
            raise WorkspaceOpError(f"Remote binary unavailable: {e}", "E_NO_REMOTE_BINARY") from e
        override.path = str(pkg)
    elif artifact == "remote-source":
        from o3de_cli.core.gem_package import download_remote_source

        data = _release_data(resolver, object_name, version)
        try:
            src = download_remote_source(object_name, version, data, on_progress=on_progress)
        except Exception as e:
            raise WorkspaceOpError(f"Remote source unavailable: {e}", "E_NO_REMOTE_SOURCE") from e
        # Register the extracted source and re-resolve so the pinned
        # version becomes a LOCAL source candidate; once materialized it
        # is a plain source override.
        resolver._add_to_manifest(src, ObjectType.GEM)
        resolver = resolver_cls()
        resolver.resolve()
        override = ObjectOverride(version=version, artifact="source")
        override.path = str(src)
    return override, resolver


def solve_with_overrides(
    meta: WorkspaceMeta,
    resolver: Resolver,
    overrides: dict[str, ObjectOverride],
    solve: SolveFn,
) -> SolveResult:
    """Re-solve the workspace root with *overrides* as ``==`` pins.

    Raises :class:`WorkspaceOpError` (``E_NO_ROOT``, ``E_SOLVE_CONFLICT``).
    """
    root_name = workspace_root_name(meta, resolver)
    if not root_name or root_name not in resolver.objects:
        raise WorkspaceOpError(
            "Workspace root object is not registered in the manifest", "E_NO_ROOT"
        )
    pins = {n: o.version for n, o in overrides.items()}
    result = solve(root_name=root_name, resolver=resolver, overrides=pins)
    if not result.is_resolved:
        raise WorkspaceOpError(
            f"Override rejected — resolution failed: {result.conflict_message}",
            "E_SOLVE_CONFLICT",
        )
    return result


def relink_changed_objects(ws_path: Path, meta: WorkspaceMeta, new_map: ObjectMap) -> list[str]:
    """Relink every object whose solved source differs from the linked one.

    The root object itself is never relinked.  Overlays composed onto a
    relinked object are re-applied; relinking without them would
    silently strip the composition.  Returns the relinked names.
    """
    current = sources_object_map(meta)
    root_resolved = Path(meta.root_object).resolve() if meta.root_object else None
    changed: list[str] = []
    for cname, (cpath, ctype) in new_map.items():
        if root_resolved and cpath.resolve() == root_resolved:
            continue
        old = current.get(cname)
        if old and old[0].resolve() == cpath.resolve():
            continue
        relink_object(
            ws_path,
            meta,
            cname,
            cpath,
            ctype,
            new_map,
            overlays=overlays_for_base(meta, cname),
        )
        changed.append(cname)
    return changed


def apply_override(
    ws_path: Path,
    meta: WorkspaceMeta,
    request: OverrideRequest,
    *,
    resolver_cls: ResolverFactory,
    solve: SolveFn,
    on_progress: ProgressFn,
) -> list[str]:
    """Pin or clear one object, re-solve, relink and persist workspace.json.

    The new override set is validated by a full re-solve before anything
    is persisted.  With ``request.apply`` false the overrides are saved
    but nothing is relinked.  The CMake manifest is left to the caller.
    Returns the relinked object names.  Raises :class:`WorkspaceOpError`.
    """
    # Resolve local objects up front (needed for remote-binary release
    # metadata and for the validation re-solve)
    resolver = resolver_cls()
    resolver.resolve()

    new_overrides = dict(meta.overrides)
    if request.clear:
        if request.object_name not in new_overrides:
            raise WorkspaceOpError(
                f"No override present for {request.object_name}", "E_NO_OVERRIDE"
            )
        del new_overrides[request.object_name]
    else:
        assert request.version is not None
        override, resolver = make_override(
            request.object_name,
            request.version,
            request.artifact,
            resolver,
            resolver_cls,
            on_progress,
        )
        new_overrides[request.object_name] = override

    solve_result = solve_with_overrides(meta, resolver, new_overrides, solve)
    meta.overrides = new_overrides

    changed: list[str] = []
    if request.apply:
        changed = relink_changed_objects(ws_path, meta, local_objects(solve_result))
    write_workspace_meta(ws_path, meta)
    return changed


# ---------------------------------------------------------------------------
# workspace build
# ---------------------------------------------------------------------------

CMAKE_CONFIGS = {"debug": "Debug", "profile": "Profile", "release": "Release"}


def build_source_paths(ws_path: Path, meta: WorkspaceMeta) -> tuple[Path | None, Path | None]:
    """The engine and project to build, as ``(engine, project)``.

    The composed workspace trees are preferred over the original source
    paths, so builds use the workspace composition (engine without
    embedded gems, workspace-level Gems/, overlays).
    """
    engine_path = workspace_local_dir(
        ws_path, "Engines", meta.sources.engines.keys()
    ) or find_engine_path(meta)
    project_path = workspace_local_dir(
        ws_path, "Projects", meta.sources.projects.keys()
    ) or find_project_path(meta)
    return engine_path, project_path


def root_gem_target(meta: WorkspaceMeta) -> str | None:
    """CMake target of a gem-rooted workspace's root gem (None if not gem-rooted).

    A legacy ``gem.json`` ``gem_name`` names the targets when present
    (2.0.0-native gems target their canonical name instead).
    """
    if meta.root_type != "gem" or not meta.root_object:
        return None
    root_resolved = Path(meta.root_object).resolve()
    for json_name in ("gem.json", "gem.2-0-0.json"):
        gem_json = root_resolved / json_name
        if gem_json.exists():
            try:
                with open(gem_json, encoding="utf-8-sig") as f:
                    legacy = json.load(f).get("gem_name")
                if legacy:
                    return legacy
            except Exception:
                pass
    for gname, gpath in meta.sources.gems.items():
        if gpath and Path(gpath).resolve() == root_resolved:
            return gname
    from o3de_cli.core.workspace import _object_name_from_path

    return _object_name_from_path(root_resolved, root_resolved.name)


@dataclass
class BuildLayout:
    """Where a workspace build runs."""

    engine_path: Path
    project_path: Path | None
    engine_centric: bool
    source_dir: Path
    build_dir: Path

    @property
    def mode(self) -> str:
        return "engine-centric" if self.engine_centric else "project-centric"


def build_layout(
    ws_path: Path,
    engine_path: Path,
    project_path: Path | None,
    engine_centric: bool,
    platform_dir: str,
) -> BuildLayout:
    """Source and build directories for a build.

    The build dir lives at the workspace root (not inside the engine or
    project trees) to keep paths short: deep autogen/moc output paths can
    otherwise exceed Windows MAX_PATH (260 chars).
    """
    if engine_centric:
        source_dir = engine_path
    else:
        assert project_path is not None
        source_dir = project_path
    return BuildLayout(
        engine_path=engine_path,
        project_path=project_path,
        engine_centric=engine_centric,
        source_dir=source_dir,
        build_dir=ws_path / "build" / platform_dir,
    )


def cmake_configure_command(
    layout: BuildLayout,
    *,
    preset: str | None,
    generator: str | None,
    third_party: Path | None,
    resolved_manifest: Path | None,
) -> list[str]:
    """The ``cmake`` configure command line for *layout*.

    *resolved_manifest* is the workspace-scoped resolved manifest, when
    it exists: CMake then uses the compose-time solution instead of
    ~/.o3de's user manifest.
    """
    if preset:
        cmd = ["cmake", "--preset", preset, "-S", str(layout.source_dir)]
    else:
        cmd = ["cmake", "-B", str(layout.build_dir), "-S", str(layout.source_dir)]
        if generator:
            cmd.append(f"-G{generator}")
    if third_party:
        cmd.append(f"-DLY_3RDPARTY_PATH={third_party}")
    if layout.engine_centric and layout.project_path:
        cmd.append(f"-DLY_PROJECTS={layout.project_path}")
    if resolved_manifest is not None:
        cmd.append(f"-DO3DE_RESOLVED_MANIFEST={resolved_manifest.as_posix()}")
    # Tell the project bootstrap where the composed engine lives
    if layout.engine_path and not layout.engine_centric:
        cmd.append(f"-DO3DE_ENGINE_PATH={Path(layout.engine_path).as_posix()}")
    return cmd


def cmake_build_command(
    build_dir: Path,
    config: str,
    targets: tuple[str, ...],
    parallel: int | None,
) -> list[str]:
    """The ``cmake --build`` command line (``--parallel`` with no count by default)."""
    cmd = ["cmake", "--build", str(build_dir), "--config", CMAKE_CONFIGS[config]]
    if targets:
        cmd.append("--target")
        cmd.extend(targets)
    if parallel:
        cmd.extend(["--parallel", str(parallel)])
    else:
        cmd.append("--parallel")
    return cmd


@dataclass
class BuildOptions:
    """CMake-related ``workspace build`` options."""

    config: str = "profile"
    targets: tuple[str, ...] = ()
    configure_only: bool = False
    reconfigure: bool = False
    preset: str | None = None
    parallel: int | None = None
    generator: str | None = None
    third_party: Path | None = None


def plan_cmake_commands(
    ws_path: Path,
    layout: BuildLayout,
    opts: BuildOptions,
) -> tuple[list[str] | None, list[str] | None]:
    """The configure and build commands to run, as ``(configure, build)``.

    Configure is skipped (None) when a CMake cache exists and no
    reconfigure was asked for; build is None with ``configure_only``.
    """
    configure_cmd: list[str] | None = None
    if opts.reconfigure or not (layout.build_dir / "CMakeCache.txt").exists():
        ws_cmake_manifest = ws_path / "resolved_o3de_manifest.json"
        configure_cmd = cmake_configure_command(
            layout,
            preset=opts.preset,
            generator=opts.generator,
            third_party=opts.third_party,
            resolved_manifest=ws_cmake_manifest if ws_cmake_manifest.exists() else None,
        )
    build_cmd = None
    if not opts.configure_only:
        build_cmd = cmake_build_command(layout.build_dir, opts.config, opts.targets, opts.parallel)
    return configure_cmd, build_cmd
