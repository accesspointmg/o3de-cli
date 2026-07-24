# O3DE Pilot CLI - Workspace Commands
# SPDX-License-Identifier: Apache-2.0 OR MIT

"""Workspace management commands.

Workspaces are symlinked build directories that combine:
- Engine source
- Project source
- Gem sources
- Overlay customizations

This allows efficient builds without copying files.
"""

import click
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable
from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.tree import Tree

from o3de_cli.core import (
    get_default_workspaces_path,
    get_resolved_manifest_path,
    Resolver,
    get_manifest_path,
    ObjectType,
)
from o3de_cli.core.workspace import Workspace, create_workspace, detect_root_type
from o3de_cli.core.models import (
    WorkspaceHeader,
    WorkspaceMeta,
    ResolvedCandidate,
    SCHEMA_VERSION,
    SCHEMA_BASE_URL,
)
from o3de_cli.core.solver import (
    solve_for_workspace,
    SolveResult,
    CandidateStatus,
)

console = Console()

# Workspace metadata filename (visible, standard pattern)
WORKSPACE_META = "workspace.json"
# Legacy hidden filename for fallback reads
_LEGACY_WORKSPACE_META = ".workspace.json"


def _find_workspace_meta(ws_path: Path) -> Path | None:
    """Find workspace metadata file, preferring new name with legacy fallback."""
    meta = ws_path / WORKSPACE_META
    if meta.exists():
        return meta
    legacy = ws_path / _LEGACY_WORKSPACE_META
    if legacy.exists():
        return legacy
    return None


def _register_workspace(ws_path: Path) -> None:
    """Register a workspace path in the global manifest."""
    manifest_path = get_manifest_path()
    with open(manifest_path) as f:
        manifest = json.load(f)
    workspaces = manifest.setdefault("workspaces", [])
    path_str = ws_path.resolve().as_posix()
    if path_str not in workspaces:
        workspaces.append(path_str)
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2)


def _unregister_workspace(ws_path: Path) -> None:
    """Remove a workspace path from the global manifest."""
    manifest_path = get_manifest_path()
    with open(manifest_path) as f:
        manifest = json.load(f)
    workspaces = manifest.get("workspaces", [])
    path_str = ws_path.resolve().as_posix()
    if path_str in workspaces:
        workspaces.remove(path_str)
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2)


def _get_registered_workspaces() -> list[Path]:
    """Return all workspace paths registered in the manifest."""
    manifest_path = get_manifest_path()
    try:
        with open(manifest_path) as f:
            manifest = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    return [Path(p) for p in manifest.get("workspaces", [])]


def _read_workspace_meta(ws_path: Path) -> WorkspaceMeta | None:
    """Read and validate workspace metadata via Pydantic model.

    Handles legacy `.workspace.json` files that lack `$schema`,
    `$schemaVersion`, and `workspace` header by injecting defaults.
    """
    meta_path = _find_workspace_meta(ws_path)
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


def _write_workspace_meta(ws_path: Path, meta: WorkspaceMeta) -> None:
    """Write workspace metadata as workspace.json."""
    meta_path = ws_path / WORKSPACE_META
    data = meta.model_dump(by_alias=True, exclude_none=True)
    # Remove legacy fields that may linger from backward-compat loading
    for key in ("overlays", "file_owners", "resolved_candidates"):
        data.pop(key, None)
    with open(meta_path, "w") as f:
        json.dump(data, f, indent=2)


def _build_workspace_meta(
    name: str,
    root_path: Path,
    root_type: str,
    sources: dict[str, dict[str, str]],
    file_links: dict[str, str] | None = None,
    platforms: list[str] | None = None,
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
    return WorkspaceMeta.model_validate(data)


@click.group()
def workspace() -> None:
    """Manage build workspaces.
    
    Workspaces are symlinked directory structures that combine
    engine, project, gems, and overlays for building.
    """
    pass


_HOST_PLATFORM_MAP = {"Windows": "Windows", "Linux": "Linux", "Darwin": "Mac"}


def _expand_platform_selection(platforms: tuple[str, ...]) -> list[str] | None:
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
                token = _HOST_PLATFORM_MAP.get(platform_mod.system(), platform_mod.system())
            names.append(token)
    # De-dupe preserving order, case-insensitive
    seen: set[str] = set()
    result: list[str] = []
    for n in names:
        if n.lower() not in seen:
            seen.add(n.lower())
            result.append(n)
    return result or None


def _select_overlays_for_platforms(
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
    all_entries: dict[str, object] = {}
    dep_targets: set[str] = set()
    for entries in overlay_groups.values():
        for ov in entries:
            all_entries[ov.name] = ov
            dep_targets.update(ov.overlay_deps)

    included: set[str] = set()
    queue: list[str] = []
    for name, ov in all_entries.items():
        if name in exclude:
            continue
        if name in include:
            queue.append(name)
            continue
        if not has_criteria:
            # No criteria: everything (minus exclusions)
            queue.append(name)
            continue
        plat_match = bool(
            selected and ov.platforms
            and {p.lower() for p in ov.platforms} & selected
        )
        tag_match = bool(
            tags and getattr(ov, "user_tags", None)
            and {t.lower() for t in ov.user_tags} & tags
        )
        if plat_match or tag_match:
            queue.append(name)
        elif not ov.platforms and name not in dep_targets:
            # Platform-agnostic and not a dependency target: applies always
            queue.append(name)

    # Transitively pull overlay dependencies of included overlays
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

    filtered: dict = {}
    for base, entries in overlay_groups.items():
        kept = [ov for ov in entries if ov.name in included]
        if kept:
            filtered[base] = kept
    return filtered


@workspace.command("create")
@click.argument("name")
@click.option("--engine", "-e", "engine_path", type=click.Path(exists=True), 
              help="Engine path")
@click.option("--project", "-p", "project_path", type=click.Path(exists=True),
              help="Project path")  
@click.option("--output", "-o", type=click.Path(), help="Output directory")
@click.option("--overlay", multiple=True, type=click.Path(exists=True),
              help="Overlay path (can be repeated)")
@click.option("--no-overlays", is_flag=True, help="Don't apply overlays")
@click.option("--platforms", "-P", "platforms_opt", multiple=True,
              help="Platforms to include when selecting overlays "
                   "(repeatable or comma-separated; 'host' = this machine, "
                   "'all' = no filtering). Default: all.")
@click.option("--tags", "-T", "tags_opt", multiple=True,
              help="User tags to include when selecting overlays "
                   "(repeatable or comma-separated; OR-combined with "
                   "--platforms).")
@click.option("--include-overlay", "include_overlay", multiple=True,
              help="Force-include a solved overlay by object name "
                   "(overrides platform filtering; repeatable)")
@click.option("--exclude-overlay", "exclude_overlay", multiple=True,
              help="Exclude a solved overlay by object name (repeatable)")
@click.option("--no-solve", is_flag=True,
              help="Skip dependency resolution — only use explicitly provided paths")
@click.option("--include-store", is_flag=True,
              help="Include remote store when resolving dependencies")
@click.option("--auto-install", "-y", is_flag=True,
              help="Automatically download and install missing remote dependencies")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def create_command(
    name: str,
    engine_path: str | None,
    project_path: str | None,
    output: str | None,
    overlay: tuple[str, ...],
    no_overlays: bool,
    platforms_opt: tuple[str, ...],
    tags_opt: tuple[str, ...],
    include_overlay: tuple[str, ...],
    exclude_overlay: tuple[str, ...],
    no_solve: bool,
    include_store: bool,
    auto_install: bool,
    as_json: bool,
) -> None:
    """Create a new workspace.
    
    Creates a structured workspace with symlinked files organised by
    object type (Engines/, Projects/, Gems/, etc.).

    By default, runs the dependency solver to resolve all transitive
    dependencies from the manifest.  Use --no-solve for explicit-only mode.
    
    Example:
        o3de-pilot workspace create my-build -e ./o3de -p ./my-project
        o3de-pilot workspace create my-build -e ./o3de -p ./my-project --no-solve
    """
    if not engine_path and not project_path:
        if as_json:
            from o3de_cli.core.json_output import emit_error
            emit_error("Must specify --engine or --project (or both)", code="E_INVALID_ARGS")
        else:
            console.print("[red]Must specify --engine or --project (or both)[/red]")
        raise SystemExit(1)
    
    # Determine output path
    if output:
        output_path = Path(output).resolve() / name
    else:
        output_path = get_default_workspaces_path() / name
    
    if output_path.exists():
        if as_json:
            from o3de_cli.core.json_output import emit_error
            emit_error(f"Workspace already exists: {output_path}", code="E_WS_EXISTS")
        else:
            console.print(f"[red]Workspace already exists:[/red] {output_path}")
            console.print("Use 'workspace update' to update, or delete first.")
        raise SystemExit(1)
    
    # Determine root object
    if engine_path:
        root_path = Path(engine_path).resolve()
        root_type = detect_root_type(root_path)
    elif project_path:
        root_path = Path(project_path).resolve()
        root_type = detect_root_type(root_path)
    else:
        root_path = Path(engine_path).resolve()
        root_type = ObjectType.ENGINE
    
    root_type_str = root_type.value
    
    # Expand platform selection (None = all platforms, no filtering)
    selected_platforms = _expand_platform_selection(platforms_opt)
    selected_tags: list[str] | None = None
    if tags_opt:
        selected_tags = []
        for value in tags_opt:
            for token in value.split(","):
                token = token.strip()
                if token and token.lower() not in {t.lower() for t in selected_tags}:
                    selected_tags.append(token)
        selected_tags = selected_tags or None
    overlay_includes = set(include_overlay)
    overlay_excludes = set(exclude_overlay)
    
    # Build resolved_objects: name → (path, ObjectType)
    resolved_objects: dict[str, tuple[Path, ObjectType]] = {}
    solve_result: SolveResult | None = None
    
    if engine_path and project_path:
        # Both provided — secondary goes in too
        proj_path = Path(project_path).resolve()
        resolved_objects[proj_path.name] = (proj_path, ObjectType.PROJECT)
    
    # Collect overlays with precedence
    overlay_tuples = [(Path(o).resolve(), i, None) for i, o in enumerate(overlay)] if not no_overlays else []
    
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        # ------------------------------------------------------------------
        # Phase 1: Dependency resolution (unless --no-solve)
        # ------------------------------------------------------------------
        if not no_solve:
            task = progress.add_task("Resolving manifest...", total=None)

            resolver = Resolver()
            resolver.resolve()

            # Force-included overlays that aren't installed locally are
            # acquired from registered remote repos before solving
            missing_includes = [
                n for n in overlay_includes if n not in resolver.overlays
            ]
            if missing_includes:
                progress.update(task, description="Downloading remote overlays...")
                installed_any = False
                for ov_name in missing_includes:
                    if _install_remote_overlay(ov_name) is not None:
                        installed_any = True
                    else:
                        progress.stop()
                        console.print(
                            f"[yellow]⚠ Overlay {ov_name} not installed and "
                            f"not found in remote repos — skipping[/yellow]"
                        )
                        progress.start()
                if installed_any:
                    resolver = Resolver()
                    resolver.resolve()

            store = None
            if include_store:
                from o3de_cli.core.store import Store

                progress.update(task, description="Refreshing store...")
                store = Store()
                store.refresh_sync(resolver.manifest_remotes)

            progress.update(task, description="Solving dependencies...")

            # Determine the root object name in the manifest
            root_name = root_path.name
            # Try to find a better name from the resolver's object index
            for obj_name, obj in resolver.objects.items():
                if obj.path and obj.path.resolve() == root_path:
                    root_name = obj_name
                    break

            # Only run the solver if the root is known to the resolver
            # (i.e. registered in the manifest).  If the user points at a
            # path that hasn't been registered yet, skip solving — the
            # explicit --engine / --project paths are sufficient.
            if root_name in resolver.objects:
                def on_progress(msg: str) -> None:
                    progress.update(task, description=msg)

                solve_result = solve_for_workspace(
                    root_name=root_name,
                    resolver=resolver,
                    store=store,
                    progress_callback=on_progress,
                )

                if not solve_result.is_resolved:
                    progress.stop()
                    console.print(
                        f"[red]Dependency resolution failed:[/red] "
                        f"{solve_result.conflict_message}"
                    )
                    raise SystemExit(1)

                # Convert solved candidates + children → resolved_objects
                # (only LOCAL candidates can be assembled into the workspace)
                for cand_name, cand in solve_result.candidates.items():
                    if cand.status == CandidateStatus.LOCAL and cand.path:
                        if cand.path.resolve() == root_path:
                            continue  # Root is added by create_workspace itself
                        resolved_objects[cand_name] = (cand.path, cand.object_type)

                for child_name, child in solve_result.children.items():
                    if child.status == CandidateStatus.LOCAL and child.path:
                        if child.path.resolve() == root_path:
                            continue
                        resolved_objects[child_name] = (child.path, child.object_type)

                # Add solved overlays (unless --no-overlays), filtered by
                # the workspace platform selection
                if not no_overlays:
                    selected_groups = _select_overlays_for_platforms(
                        solve_result.overlays, selected_platforms,
                        include=overlay_includes, exclude=overlay_excludes,
                        selected_tags=selected_tags,
                    )
                    for _base, entries in selected_groups.items():
                        for ov in entries:
                            if ov.path and ov.status == CandidateStatus.LOCAL:
                                # Avoid duplicating explicitly-provided overlays
                                ov_resolved = ov.path.resolve()
                                if not any(p.resolve() == ov_resolved for p, *_ in overlay_tuples):
                                    overlay_tuples.append((ov_resolved, ov.precedence, ov.extends))

                # Report remote/unknown candidates
                remote_count = solve_result.remote_count
                unknown_count = solve_result.unknown_count

                # ----------------------------------------------------------
                # J2: Auto-install missing remote dependencies
                # ----------------------------------------------------------
                if remote_count and auto_install and store:
                    progress.update(task, description="Installing remote dependencies...")

                    def on_install_progress(msg: str, current: int, total: int) -> None:
                        progress.update(task, description=msg)

                    installed = resolver.auto_install_missing(
                        store=store,
                        confirm=True,
                        progress_callback=on_install_progress,
                    )

                    if installed:
                        console.print(
                            f"[green]Installed {len(installed)} remote dependencies[/green]"
                        )
                        # Re-resolve and re-solve to pick up newly-local objects
                        progress.update(task, description="Re-resolving after install...")
                        resolver.resolve()

                        progress.update(task, description="Re-solving dependencies...")
                        solve_result = solve_for_workspace(
                            root_name=root_name,
                            resolver=resolver,
                            store=store,
                            progress_callback=on_progress,
                        )

                        if not solve_result.is_resolved:
                            progress.stop()
                            console.print(
                                f"[red]Re-solve after install failed:[/red] "
                                f"{solve_result.conflict_message}"
                            )
                            raise SystemExit(1)

                        # Rebuild resolved_objects from the fresh solve
                        resolved_objects.clear()
                        if engine_path and project_path:
                            proj_path = Path(project_path).resolve()
                            resolved_objects[proj_path.name] = (proj_path, ObjectType.PROJECT)

                        for cand_name, cand in solve_result.candidates.items():
                            if cand.status == CandidateStatus.LOCAL and cand.path:
                                if cand.path.resolve() == root_path:
                                    continue
                                resolved_objects[cand_name] = (cand.path, cand.object_type)

                        for child_name, child in solve_result.children.items():
                            if child.status == CandidateStatus.LOCAL and child.path:
                                if child.path.resolve() == root_path:
                                    continue
                                resolved_objects[child_name] = (child.path, child.object_type)

                        # Re-process overlays (same platform selection)
                        if not no_overlays:
                            overlay_tuples = [(Path(o).resolve(), i, None) for i, o in enumerate(overlay)]
                            selected_groups = _select_overlays_for_platforms(
                                solve_result.overlays, selected_platforms,
                                include=overlay_includes, exclude=overlay_excludes,
                                selected_tags=selected_tags,
                            )
                            for _base, entries in selected_groups.items():
                                for ov in entries:
                                    if ov.path and ov.status == CandidateStatus.LOCAL:
                                        ov_resolved = ov.path.resolve()
                                        if not any(p.resolve() == ov_resolved for p, *_ in overlay_tuples):
                                            overlay_tuples.append((ov_resolved, ov.precedence, ov.extends))

                        # Update counts after re-solve
                        remote_count = solve_result.remote_count
                        unknown_count = solve_result.unknown_count

                elif remote_count and auto_install and not store:
                    progress.stop()
                    console.print(
                        "[yellow]⚠ --auto-install requires --include-store[/yellow]"
                    )
                    progress.start()

                if remote_count or unknown_count:
                    progress.stop()
                    if remote_count:
                        console.print(
                            f"[yellow]⚠ {remote_count} remote dependencies not yet "
                            f"installed — run 'workspace solve {root_name} "
                            f"--include-store' for details[/yellow]"
                        )
                    if unknown_count:
                        console.print(
                            f"[red]⚠ {unknown_count} dependencies could not be "
                            f"found anywhere[/red]"
                        )
                    progress.start()
            else:
                console.print(
                    f"[dim]Root object not registered in manifest — "
                    f"skipping dependency resolution[/dim]"
                )
                progress.start()

            progress.update(task, description="Dependencies resolved")

        # ------------------------------------------------------------------
        # Phase 2: Assemble the workspace
        # ------------------------------------------------------------------
        task2 = progress.add_task("Creating workspace...", total=None)
        
        workspace_obj = create_workspace(
            target_path=output_path,
            root_object_path=root_path,
            resolved_objects=resolved_objects,
            overlays=overlay_tuples,
        )
        
        # Save workspace metadata via Pydantic model
        meta = _build_workspace_meta(
            name=name,
            root_path=root_path,
            root_type=root_type_str,
            sources=workspace_obj.get_sources_dict(),
            file_links=workspace_obj.get_file_links(),
            platforms=selected_platforms,
        )
        _write_workspace_meta(output_path, meta)

        # Write the workspace-scoped CMake resolved manifest — the
        # compose-time solve realized on disk; CMake consumes this
        # instead of re-resolving against the user manifest.
        progress.update(task2, description="Writing CMake manifest...")
        from o3de_cli.core.cmake_manifest import generate_cmake_manifest
        tp = _find_third_party_path(meta=meta)
        generate_cmake_manifest(
            output_path,
            third_party_path=str(tp) if tp else "",
        )

        progress.update(task2, description="Done")

    if as_json:
        from o3de_cli.core.json_output import emit_response
        data = {
            "action": "created",
            "workspace": str(output_path),
            "root": str(root_path),
            "objects": len(resolved_objects) + 1,
            "overlays": len(overlay_tuples),
        }
        if solve_result:
            data["resolved_local"] = solve_result.local_count
            data["resolved_remote"] = solve_result.remote_count
        if selected_platforms:
            data["platforms"] = selected_platforms
        emit_response(data=data)
    else:
        console.print(f"[green]Created workspace:[/green] {output_path}")
        console.print(f"  Root: {root_path}")
        console.print(f"  Objects: {len(resolved_objects) + 1}")
        if solve_result:
            console.print(f"  Resolved: {solve_result.local_count} local")
            if solve_result.remote_count:
                console.print(f"  Remote (not installed): {solve_result.remote_count}")
        console.print(f"  Overlays: {len(overlay_tuples)}")
        if selected_platforms:
            console.print(f"  Platforms: {', '.join(selected_platforms)}")


def _resolve_workspace_path(name_or_path: str) -> Path | None:
    """Locate a workspace directory by path or registered name."""
    ws_path = Path(name_or_path)
    if not ws_path.exists():
        ws_path = get_default_workspaces_path() / name_or_path
    return ws_path if ws_path.exists() else None


def _workspace_root_name(meta: WorkspaceMeta, resolver: Resolver) -> str | None:
    """Map the workspace's root_object path back to a manifest object name."""
    if not meta.root_object:
        return None
    root_path = Path(meta.root_object).resolve()
    for obj_name, obj in resolver.objects.items():
        if obj.path and obj.path.resolve() == root_path:
            return obj_name
    return root_path.name


def _relink_object(
    ws_path: Path,
    meta: WorkspaceMeta,
    name: str,
    new_path: Path,
    obj_type: ObjectType,
    all_objects: dict[str, tuple[Path, ObjectType]],
    overlays: list[tuple[Path, int]] | None = None,
) -> None:
    """Replace one object's linked tree in the workspace with *new_path*.

    Removes the old destination directory, relinks from the new source,
    re-applies *overlays* (``(path, precedence)`` tuples, applied in
    precedence order INTO the object's tree), and updates meta.sources +
    meta.file_links in place.
    """
    import shutil
    from o3de_cli.core.workspace import (
        _TYPE_FOLDERS, _short_name, _ENGINE_SKIP_TOPDIRS,
    )

    folder = _TYPE_FOLDERS.get(obj_type, "Gems")
    short = _short_name(name)
    dest_root = ws_path / folder / short
    dest_prefix = f"{folder}/{short}/"

    # Remove the old linked tree (hard links — sources unaffected)
    if dest_root.exists():
        def _clear_readonly(func, path, _exc_info):
            import os, stat
            os.chmod(path, stat.S_IWRITE)
            func(path)
        shutil.rmtree(dest_root, onexc=_clear_readonly)

    # Drop stale file_links for this object
    meta.file_links = {
        src: rel for src, rel in meta.file_links.items()
        if not rel.startswith(dest_prefix)
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
        from o3de_cli.core.workspace import _object_name_from_path
        for ov_path, _prec in sorted(overlays, key=lambda t: t[1]):
            ov_name = _object_name_from_path(ov_path, ov_path.name)
            ws._apply_overlay(
                ov_path,
                owner_name=ov_name,
                base_dest_root=dest_root,
            )

    meta.file_links.update(ws.get_file_links())

    # Update the sources bucket
    bucket_key = {
        ObjectType.ENGINE: "engines", ObjectType.PROJECT: "projects",
        ObjectType.GEM: "gems", ObjectType.TEMPLATE: "templates",
    }.get(obj_type, "gems")
    getattr(meta.sources, bucket_key)[name] = str(new_path)


@workspace.command("candidates")
@click.argument("name_or_path")
@click.option("--name", "object_name", default=None,
              help="Limit output to one object")
@click.option("--include-store", is_flag=True,
              help="Include remote store candidates")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def candidates_command(
    name_or_path: str,
    object_name: str | None,
    include_store: bool,
    as_json: bool,
) -> None:
    """List all candidates that could satisfy each resolved object.

    For every object in the workspace, enumerates every registered local
    version and (with --include-store) remote advertised versions, annotated
    with artifact availability: source path, locally built binary install,
    and remote prebuilt binary for this platform.
    """
    from o3de_cli.core.json_output import emit_response, emit_error
    from o3de_cli.core.solver import list_candidates

    ws_path = _resolve_workspace_path(name_or_path)
    if ws_path is None:
        if as_json:
            emit_error(f"Workspace not found: {name_or_path}", code="E_WS_NOT_FOUND")
        else:
            console.print(f"[red]Workspace not found:[/red] {name_or_path}")
        raise SystemExit(1)

    meta = _read_workspace_meta(ws_path)
    if meta is None:
        if as_json:
            emit_error(f"Not a valid workspace: {ws_path}", code="E_WS_INVALID")
        else:
            console.print(f"[red]Not a valid workspace:[/red] {ws_path}")
        raise SystemExit(1)

    resolver = Resolver()
    resolver.resolve()

    store = None
    if include_store:
        from o3de_cli.core.store import Store
        store = Store()
        store.refresh_sync(resolver.manifest_remotes)

    root_name = _workspace_root_name(meta, resolver)
    pins = {n: o.version for n, o in meta.overrides.items()}
    solve_result = None
    if root_name and root_name in resolver.objects:
        solve_result = solve_for_workspace(
            root_name=root_name, resolver=resolver, store=store, overrides=pins,
        )

    # Collect the object set to report: solved candidates + children,
    # falling back to workspace sources
    chosen: dict[str, tuple[str, str, str]] = {}  # name -> (version, type, path)
    if solve_result and solve_result.is_resolved:
        for cand_map in (solve_result.candidates, solve_result.children):
            for cname, cand in cand_map.items():
                chosen[cname] = (
                    cand.version,
                    cand.object_type.value,
                    str(cand.path) if cand.path else "",
                )
    else:
        for bucket, type_name in (
            (meta.sources.engines, "engine"), (meta.sources.projects, "project"),
            (meta.sources.gems, "gem"), (meta.sources.templates, "template"),
        ):
            for cname, cpath in bucket.items():
                chosen[cname] = ("", type_name, cpath)

    names = [object_name] if object_name else sorted(chosen)
    objects_out = []
    for cname in names:
        version, type_name, path = chosen.get(cname, ("", "", ""))
        cands = list_candidates(cname, resolver, store)
        override = meta.overrides.get(cname)
        objects_out.append({
            "name": cname,
            "type": type_name,
            "chosen_version": version,
            "chosen_path": path,
            "override": override.model_dump() if override else None,
            "candidates": [
                {
                    "version": c.version,
                    "status": c.status.value,
                    "path": str(c.path) if c.path else None,
                    "local_binary_path": (
                        str(c.local_binary_path) if c.local_binary_path else None
                    ),
                    "remote_binary": c.remote_binary,
                }
                for c in cands
            ],
        })

    if as_json:
        emit_response(data={"workspace": str(ws_path), "objects": objects_out})
        return

    table = Table(title=f"Candidates — {meta.workspace.name}")
    table.add_column("Object")
    table.add_column("Chosen")
    table.add_column("Candidates")
    table.add_column("Override")
    for entry in objects_out:
        cand_strs = []
        for c in entry["candidates"]:
            forms = ["source"] if c["path"] else []
            if c["local_binary_path"]:
                forms.append("local-binary")
            if c["remote_binary"]:
                forms.append("remote-binary")
            cand_strs.append(f"{c['version']} ({c['status']}; {'/'.join(forms) or '—'})")
        ov = entry["override"]
        ov_str = f"{ov['version']} [{ov['artifact']}]" if ov else ""
        table.add_row(
            entry["name"], entry["chosen_version"],
            ", ".join(cand_strs) or "—", ov_str,
        )
    console.print(table)


@workspace.command("override")
@click.argument("name_or_path")
@click.argument("object_name")
@click.option("--version", "pin_version", default=None,
              help="Exact version to pin (required unless --clear)")
@click.option("--artifact",
              type=click.Choice(["source", "local-binary", "remote-binary",
                                 "remote-source"]),
              default="source", help="Artifact form to use")
@click.option("--clear", is_flag=True, help="Remove the override for this object")
@click.option("--no-apply", is_flag=True,
              help="Validate and persist only — don't relink the workspace")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def override_command(
    name_or_path: str,
    object_name: str,
    pin_version: str | None,
    artifact: str,
    clear: bool,
    no_apply: bool,
    as_json: bool,
) -> None:
    """Override the resolved candidate for one object.

    Pins OBJECT_NAME to an exact version (fed to the solver as an ``==``
    constraint), re-solves the full graph, and relinks any objects whose
    chosen source changed.

    Example:
        o3de workspace override my-ws org.o3de.gem.physx --version 4.0.0
        o3de workspace override my-ws org.o3de.gem.physx --clear
    """
    from o3de_cli.core.json_output import emit_response, emit_error
    from o3de_cli.core.models import ObjectOverride
    from o3de_cli.core.solver import find_local_binary_install

    def _fail(msg: str, code: str) -> None:
        if as_json:
            emit_error(msg, code=code)
        else:
            console.print(f"[red]{msg}[/red]")
        raise SystemExit(1)

    if not clear and not pin_version:
        _fail("--version is required (or use --clear)", "E_INVALID_ARGS")

    ws_path = _resolve_workspace_path(name_or_path)
    if ws_path is None:
        _fail(f"Workspace not found: {name_or_path}", "E_WS_NOT_FOUND")
    meta = _read_workspace_meta(ws_path)
    if meta is None:
        _fail(f"Not a valid workspace: {ws_path}", "E_WS_INVALID")

    # Resolve local objects up front (needed for remote-binary release
    # metadata and for the validation re-solve below)
    resolver = Resolver()
    resolver.resolve()

    # Compute the new override set
    new_overrides = dict(meta.overrides)
    if clear:
        if object_name not in new_overrides:
            _fail(f"No override present for {object_name}", "E_NO_OVERRIDE")
        del new_overrides[object_name]
    else:
        override = ObjectOverride(version=pin_version, artifact=artifact)
        if artifact == "local-binary":
            binary = find_local_binary_install(object_name, pin_version)
            if binary is None:
                _fail(
                    f"No local binary install found for "
                    f"{object_name}@{pin_version} (expected an install layout "
                    f"with *Config.cmake under ~/.o3de/BuiltPackages)",
                    "E_NO_LOCAL_BINARY",
                )
            override.path = str(binary)
        elif artifact == "remote-binary":
            from o3de_cli.core.gem_package import download_remote_binary

            versions = getattr(resolver, "objects_all", {}).get(object_name, {})
            resolved = versions.get(pin_version) or resolver.objects.get(object_name)
            data = resolved.data if resolved is not None else {}

            def _dl_progress(msg: str) -> None:
                if not as_json:
                    console.print(f"[dim]{msg}[/dim]")

            try:
                pkg = download_remote_binary(
                    object_name, pin_version, data, on_progress=_dl_progress,
                )
            except Exception as e:
                _fail(f"Remote binary unavailable: {e}", "E_NO_REMOTE_BINARY")
                return
            override.path = str(pkg)
        elif artifact == "remote-source":
            from o3de_cli.core.gem_package import download_remote_source
            from o3de_cli.core.models import ObjectType

            versions = getattr(resolver, "objects_all", {}).get(object_name, {})
            resolved = versions.get(pin_version) or resolver.objects.get(object_name)
            data = resolved.data if resolved is not None else {}

            def _src_progress(msg: str) -> None:
                if not as_json:
                    console.print(f"[dim]{msg}[/dim]")

            try:
                src = download_remote_source(
                    object_name, pin_version, data, on_progress=_src_progress,
                )
            except Exception as e:
                _fail(f"Remote source unavailable: {e}", "E_NO_REMOTE_SOURCE")
                return
            # Register the extracted source and re-resolve so the pinned
            # version becomes a LOCAL source candidate; once materialized
            # it is a plain source override.
            resolver._add_to_manifest(src, ObjectType.GEM)
            resolver = Resolver()
            resolver.resolve()
            override = ObjectOverride(version=pin_version, artifact="source")
            override.path = str(src)
        new_overrides[object_name] = override

    # Re-solve with the new pins — validate before persisting anything
    root_name = _workspace_root_name(meta, resolver)
    if not root_name or root_name not in resolver.objects:
        _fail("Workspace root object is not registered in the manifest", "E_NO_ROOT")

    pins = {n: o.version for n, o in new_overrides.items()}
    solve_result = solve_for_workspace(
        root_name=root_name, resolver=resolver, overrides=pins,
    )
    if not solve_result.is_resolved:
        _fail(
            f"Override rejected — resolution failed: {solve_result.conflict_message}",
            "E_SOLVE_CONFLICT",
        )

    # Persist the override set
    meta.overrides = new_overrides

    # Determine which linked objects changed and relink them
    new_map: dict[str, tuple[Path, ObjectType]] = {}
    for cand_map in (solve_result.candidates, solve_result.children):
        for cname, cand in cand_map.items():
            if cand.status == CandidateStatus.LOCAL and cand.path:
                new_map[cname] = (cand.path, cand.object_type)

    changed: list[str] = []
    if not no_apply:
        current: dict[str, str] = {}
        for bucket in (meta.sources.engines, meta.sources.projects,
                       meta.sources.gems, meta.sources.templates):
            current.update(bucket)

        root_resolved = Path(meta.root_object).resolve() if meta.root_object else None
        for cname, (cpath, ctype) in new_map.items():
            if root_resolved and cpath.resolve() == root_resolved:
                continue  # the root object itself is never relinked here
            old_path = current.get(cname)
            if old_path and Path(old_path).resolve() == cpath.resolve():
                continue
            _relink_object(ws_path, meta, cname, cpath, ctype, new_map)
            changed.append(cname)

        _write_workspace_meta(ws_path, meta)

        # Regenerate the workspace-scoped CMake manifest
        from o3de_cli.core.cmake_manifest import generate_cmake_manifest
        tp = _find_third_party_path(meta=meta)
        generate_cmake_manifest(
            ws_path,
            third_party_path=str(tp) if tp else "",
            overrides=meta.overrides,
        )
    else:
        _write_workspace_meta(ws_path, meta)

    if as_json:
        emit_response(data={
            "action": "cleared" if clear else "overridden",
            "object": object_name,
            "version": pin_version,
            "artifact": artifact,
            "relinked": changed,
            "applied": not no_apply,
        })
    else:
        if clear:
            console.print(f"[green]Cleared override for[/green] {object_name}")
        else:
            console.print(
                f"[green]Pinned[/green] {object_name}=={pin_version} [{artifact}]"
            )
        if changed:
            console.print(f"  Relinked: {', '.join(changed)}")
        elif not no_apply:
            console.print("  No linked objects changed")
        if artifact in ("local-binary", "remote-binary") and not clear:
            console.print(
                "[dim]Prebuilt package will be imported on the next "
                "(re)configure instead of building the gem from source[/dim]"
            )
        elif artifact == "remote-source" and not clear:
            console.print(
                "[dim]Source release downloaded and registered — builds "
                "from source like any local gem[/dim]"
            )


def _install_remote_overlay(name: str) -> Path | None:
    """Install an overlay from the registered remote repos by object name.

    Returns the installed path, or None if the overlay is unknown remotely.
    """
    from o3de_cli.core.store import Store, get_manifest_remote_urls
    from o3de_cli.core.paths import get_default_path_for_type

    urls = get_manifest_remote_urls()
    if not urls:
        return None
    store = Store()
    try:
        store.refresh_sync(urls)
    except Exception:
        return None
    remote_obj = store.objects.get(f"overlay:{name}")
    if remote_obj is None:
        return None

    target = get_default_path_for_type(ObjectType.OVERLAY)
    installed = store.download_sync(remote_obj, target)

    # Register in the manifest so the resolver can see it
    from o3de_cli.commands.manifest import add_command
    ctx = click.Context(add_command)
    ctx.invoke(add_command, path=str(installed))
    return installed


def _read_overlay_info(path: Path) -> dict:
    """Read overlay metadata needed for composition from an overlay dir.

    Returns ``{name, extends, precedence, deps}`` where ``extends`` is the
    base object's bare name and ``deps`` are overlay names from
    ``dependent.overlays``.
    """
    import json as json_mod
    from o3de_cli.core.resolver import ObjectNameVersion

    info = {"name": path.name, "extends": None, "precedence": 0, "deps": []}
    for fname in ("overlay.2-0-0.json", "overlay.json"):
        candidate = path / fname
        if not candidate.exists():
            continue
        try:
            with open(candidate, encoding="utf-8-sig") as f:
                data = json_mod.load(f)
        except (json_mod.JSONDecodeError, IOError):
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


@workspace.command("update")
@click.argument("name_or_path")
@click.option("--overlay", multiple=True, type=click.Path(exists=True),
              help="Additional overlay path")
@click.option("--add-overlay", "add_overlay", multiple=True,
              help="Add an installed overlay by object name (repeatable); "
                   "its overlay dependencies are pulled in automatically")
@click.option("--remove-overlay", "remove_overlay", multiple=True,
              help="Remove a composed overlay by object name (repeatable); "
                   "the extended object is recomposed without it")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def update_command(
    name_or_path: str,
    overlay: tuple[str, ...],
    add_overlay: tuple[str, ...],
    remove_overlay: tuple[str, ...],
    as_json: bool,
) -> None:
    """Update an existing workspace.
    
    Re-syncs symlinks and applies any new overlays.  With --add-overlay /
    --remove-overlay, changes the workspace's composed overlay set: the
    affected extended objects are recomposed (base relinked, remaining
    overlays re-applied in precedence order).
    """
    from o3de_cli.core.json_output import emit_response, emit_error
    
    # Find workspace
    workspace_path = Path(name_or_path)
    if not workspace_path.exists():
        workspace_path = get_default_workspaces_path() / name_or_path
    
    if not workspace_path.exists():
        if as_json:
            emit_error(f"Workspace not found: {name_or_path}", code="E_WS_NOT_FOUND")
        else:
            console.print(f"[red]Workspace not found:[/red] {name_or_path}")
        raise SystemExit(1)
    
    # Load workspace metadata
    meta = _read_workspace_meta(workspace_path)
    if meta is None:
        if as_json:
            emit_error(f"Not a valid workspace: {workspace_path}", code="E_WS_INVALID")
        else:
            console.print(f"[red]Not a valid workspace:[/red] {workspace_path}")
            console.print("Missing workspace.json metadata file.")
        raise SystemExit(1)

    # ------------------------------------------------------------------
    # Overlay set change: --add-overlay / --remove-overlay
    # ------------------------------------------------------------------
    if add_overlay or remove_overlay:
        def _fail(msg: str, code: str) -> None:
            if as_json:
                emit_error(msg, code=code)
            else:
                console.print(f"[red]{msg}[/red]")
            raise SystemExit(1)

        current: dict[str, str] = dict(meta.sources.overlays)

        # Resolve additions against installed overlays
        resolver = Resolver()
        resolver.resolve()

        adds: dict[str, str] = {}
        pending = list(add_overlay)
        while pending:
            ov_name = pending.pop(0)
            if ov_name in current or ov_name in adds:
                continue
            ov = resolver.overlays.get(ov_name)
            if ov is None or not ov.path:
                # Not installed locally — try acquiring from registered
                # remote repos before failing
                if not as_json:
                    console.print(f"[dim]Downloading remote overlay: {ov_name}[/dim]")
                installed = _install_remote_overlay(ov_name)
                if installed is None:
                    _fail(
                        f"Overlay not installed locally and not found in "
                        f"remote repos: {ov_name}",
                        "E_OVERLAY_NOT_FOUND",
                    )
                resolver = Resolver()
                resolver.resolve()
                ov = resolver.overlays.get(ov_name)
                if ov is None or not ov.path:
                    _fail(f"Overlay install failed: {ov_name}", "E_OVERLAY_NOT_FOUND")
            adds[ov_name] = str(ov.path)
            # Pull overlay dependencies along
            info = _read_overlay_info(ov.path)
            for dep in info["deps"]:
                if dep not in current and dep not in adds:
                    pending.append(dep)

        removes = set(remove_overlay)
        for ov_name in removes:
            if ov_name not in current:
                _fail(f"Overlay not in workspace: {ov_name}", "E_OVERLAY_NOT_IN_WS")

        # New frozen set
        new_set: dict[str, str] = {
            n: p for n, p in current.items() if n not in removes
        }
        new_set.update(adds)

        # Dependency guard: a remaining overlay must not depend on a removed one
        for ov_name, ov_path in new_set.items():
            info = _read_overlay_info(Path(ov_path))
            broken = [d for d in info["deps"] if d in removes]
            if broken:
                _fail(
                    f"Cannot remove {', '.join(broken)}: still required by "
                    f"{ov_name} (remove it too or keep the dependency)",
                    "E_OVERLAY_DEP",
                )

        # Group the NEW overlay set by extended base object
        overlays_by_base: dict[str, list[tuple[Path, int]]] = {}
        for ov_name, ov_path in new_set.items():
            info = _read_overlay_info(Path(ov_path))
            if info["extends"]:
                overlays_by_base.setdefault(info["extends"], []).append(
                    (Path(ov_path), info["precedence"])
                )

        # Bases affected by the change
        affected_bases: set[str] = set()
        for ov_name in set(adds) | removes:
            src = adds.get(ov_name) or current.get(ov_name)
            if src:
                info = _read_overlay_info(Path(src))
                if info["extends"]:
                    affected_bases.add(info["extends"])

        # Build the full object map from meta.sources
        all_objects: dict[str, tuple[Path, ObjectType]] = {}
        bucket_types = [
            ("engines", ObjectType.ENGINE), ("projects", ObjectType.PROJECT),
            ("gems", ObjectType.GEM), ("templates", ObjectType.TEMPLATE),
        ]
        for bucket, btype in bucket_types:
            for obj_name, obj_path in getattr(meta.sources, bucket).items():
                if obj_path:
                    all_objects[obj_name] = (Path(obj_path), btype)

        recomposed: list[str] = []
        skipped: list[str] = []
        for base_name in sorted(affected_bases):
            entry = all_objects.get(base_name)
            if entry is None:
                skipped.append(base_name)
                continue
            base_path, base_type = entry
            _relink_object(
                workspace_path, meta, base_name, base_path, base_type,
                all_objects, overlays=overlays_by_base.get(base_name, []),
            )
            recomposed.append(base_name)

        # Persist the new frozen overlay set
        meta.sources.overlays = new_set
        _write_workspace_meta(workspace_path, meta)

        # Regenerate the workspace-scoped CMake manifest
        from o3de_cli.core.cmake_manifest import generate_cmake_manifest
        tp = _find_third_party_path(meta=meta)
        generate_cmake_manifest(
            workspace_path, third_party_path=str(tp) if tp else "",
        )

        if as_json:
            emit_response(data={
                "action": "overlays-updated",
                "workspace": str(workspace_path),
                "added": sorted(adds),
                "removed": sorted(removes),
                "recomposed": recomposed,
                "skipped": skipped,
                "overlays": sorted(new_set),
            })
        else:
            console.print(f"[green]Updated overlays:[/green] {workspace_path}")
            if adds:
                console.print(f"  Added: {', '.join(sorted(adds))}")
            if removes:
                console.print(f"  Removed: {', '.join(sorted(removes))}")
            console.print(f"  Recomposed: {', '.join(recomposed) or '(none)'}")
            if skipped:
                console.print(
                    f"[yellow]  Skipped (base not in workspace): "
                    f"{', '.join(skipped)}[/yellow]"
                )
            console.print(f"  Overlays now: {len(new_set)}")
        return
    
    # Reconstruct workspace from metadata
    sources: list[Path] = []
    for type_dict in [meta.sources.engines, meta.sources.projects,
                      meta.sources.gems, meta.sources.templates]:
        for _name, path in type_dict.items():
            if path:
                sources.append(Path(path))
    existing_overlays = [Path(p) for p in meta.sources.overlays.values()]
    new_overlays = [Path(o).resolve() for o in overlay]
    all_overlays = existing_overlays + new_overlays
    
    # Determine root object path and type
    root_source = sources[0] if sources else workspace_path
    try:
        root_type = detect_root_type(root_source)
    except Exception:
        root_type = ObjectType.ENGINE  # fallback
    
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Updating workspace...", total=None)
        
        workspace_obj = Workspace(
            root_path=workspace_path,
            root_object_path=root_source,
            root_object_type=root_type,
        )
        
        # Add resolved objects from sources with their types
        for i, source in enumerate(sources):
            if (source / "engine.json").exists():
                stype = ObjectType.ENGINE
            elif (source / "project.json").exists():
                stype = ObjectType.PROJECT
            elif (source / "gem.json").exists():
                stype = ObjectType.GEM
            else:
                stype = ObjectType.GEM
            workspace_obj.add_resolved_object(source.name, source, stype)
        
        # Add overlays
        for i, overlay_path in enumerate(all_overlays):
            workspace_obj.add_overlay(overlay_path, precedence=i)
        
        workspace_obj.update()
        
        progress.update(task, description="Done")
    
    if as_json:
        emit_response(data={
            "action": "updated",
            "workspace": str(workspace_path),
            "overlays": len(all_overlays),
        })
    else:
        console.print(f"[green]Updated workspace:[/green] {workspace_path}")


@workspace.command("list")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def list_command(as_json: bool) -> None:
    """List all workspaces."""
    # Collect workspace dirs from default folder + manifest-registered paths
    seen: set[Path] = set()
    ws_dirs: list[Path] = []

    workspaces_path = get_default_workspaces_path()
    if workspaces_path.exists():
        for d in workspaces_path.iterdir():
            if d.is_dir():
                resolved = d.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    ws_dirs.append(d)

    for rp in _get_registered_workspaces():
        if rp.is_dir():
            resolved = rp.resolve()
            if resolved not in seen:
                seen.add(resolved)
                ws_dirs.append(rp)

    workspaces = []
    for ws_dir in ws_dirs:
        meta = _read_workspace_meta(ws_dir)
        if meta is not None:
            src = meta.sources
            n_sources = (len(src.engines) + len(src.projects)
                         + len(src.gems) + len(src.templates))
            n_overlays = len(src.overlays)
            workspaces.append({
                "name": meta.workspace.name or ws_dir.name,
                "path": str(ws_dir),
                "sources": n_sources,
                "overlays": n_overlays,
                "created": meta.created,
            })
    
    if as_json:
        console.print_json(json.dumps(workspaces))
    else:
        if not workspaces:
            console.print("[dim]No workspaces found.[/dim]")
            return
        
        table = Table(title="Workspaces")
        table.add_column("Name", style="cyan")
        table.add_column("Sources", style="green", justify="right")
        table.add_column("Overlays", style="yellow", justify="right")
        table.add_column("Path", style="dim")
        
        for ws in workspaces:
            table.add_row(
                ws["name"],
                str(ws["sources"]),
                str(ws["overlays"]),
                ws["path"],
            )
        
        console.print(table)


@workspace.command("show")
@click.argument("name_or_path")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def show_command(name_or_path: str, as_json: bool) -> None:
    """Show workspace details."""
    # Find workspace
    ws_path = Path(name_or_path)
    if not ws_path.exists():
        ws_path = get_default_workspaces_path() / name_or_path
    
    if not ws_path.exists():
        console.print(f"[red]Workspace not found:[/red] {name_or_path}")
        raise SystemExit(1)
    
    meta = _read_workspace_meta(ws_path)
    if meta is None:
        console.print(f"[red]Not a valid workspace:[/red] {ws_path}")
        raise SystemExit(1)
    
    if as_json:
        console.print_json(json.dumps(
            meta.model_dump(by_alias=True, exclude_none=True), indent=2
        ))
    else:
        console.print(f"[bold]Workspace:[/bold] {meta.workspace.name or ws_path.name}")
        console.print(f"[dim]Path:[/dim] {ws_path}")
        console.print(f"[dim]Created:[/dim] {meta.created}")
        
        console.print("\n[bold]Sources:[/bold]")
        src = meta.sources
        for type_label, items in [
            ("Engines", src.engines), ("Projects", src.projects),
            ("Gems", src.gems), ("Templates", src.templates),
        ]:
            for name, path in items.items():
                console.print(f"  • [cyan]{type_label[:-1]}[/cyan] {name}  [dim]{path}[/dim]")
        
        if src.overlays:
            console.print("\n[bold]Overlays:[/bold]")
            for name, path in src.overlays.items():
                console.print(f"  • {name}  [dim]{path}[/dim]")


@workspace.command("delete")
@click.argument("name_or_path")
@click.option("--force", "-f", is_flag=True, help="Delete without confirmation")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.option("--dry-run", is_flag=True, help="Show what would be deleted without deleting")
def delete_command(name_or_path: str, force: bool, as_json: bool, dry_run: bool) -> None:
    """Delete a workspace.
    
    Removes the workspace directory and all symlinks.
    Does not delete the original source files.
    """
    import shutil
    from o3de_cli.core.json_output import emit_response, emit_error
    
    # Find workspace
    ws_path = Path(name_or_path)
    if not ws_path.exists():
        ws_path = get_default_workspaces_path() / name_or_path
    
    if not ws_path.exists():
        if as_json:
            emit_error(f"Workspace not found: {name_or_path}", code="E_WS_NOT_FOUND")
        else:
            console.print(f"[red]Workspace not found:[/red] {name_or_path}")
        raise SystemExit(1)
    
    if dry_run:
        if as_json:
            emit_response(data={
                "dry_run": True,
                "action": "delete",
                "workspace": str(ws_path),
            })
        else:
            console.print(f"[dim]Would delete:[/dim] {ws_path}")
        return
    
    if not force:
        if not click.confirm(f"Delete workspace '{ws_path.name}'?"):
            console.print("[dim]Cancelled.[/dim]")
            return
    
    def _clear_readonly(func, path, _exc_info):
        """Windows: build artifacts (e.g. FetchContent git pack files)
        are read-only; clear the attribute and retry."""
        import os
        import stat
        os.chmod(path, stat.S_IWRITE)
        func(path)

    shutil.rmtree(ws_path, onexc=_clear_readonly)
    _unregister_workspace(ws_path)
    if as_json:
        emit_response(data={"action": "deleted", "workspace": str(ws_path)})
    else:
        console.print(f"[green]Deleted:[/green] {ws_path}")


@workspace.command("tree")
@click.argument("name_or_path")
@click.option("--depth", "-d", default=2, help="Tree depth")
def tree_command(name_or_path: str, depth: int) -> None:
    """Show workspace directory tree."""
    # Find workspace
    ws_path = Path(name_or_path)
    if not ws_path.exists():
        ws_path = get_default_workspaces_path() / name_or_path
    
    if not ws_path.exists():
        console.print(f"[red]Workspace not found:[/red] {name_or_path}")
        raise SystemExit(1)
    
    def add_tree_items(tree: Tree, path: Path, current_depth: int):
        if current_depth >= depth:
            return
        
        try:
            items = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name))
        except PermissionError:
            return
        
        for item in items:
            if item.name.startswith("."):
                continue
            
            if item.is_symlink():
                target = item.resolve()
                subtree = tree.add(f"[cyan]{item.name}[/cyan] → [dim]{target}[/dim]")
            elif item.is_dir():
                subtree = tree.add(f"[bold blue]{item.name}/[/bold blue]")
                add_tree_items(subtree, item, current_depth + 1)
            else:
                tree.add(item.name)
    
    tree = Tree(f"[bold]{ws_path.name}[/bold]")
    add_tree_items(tree, ws_path, 0)
    console.print(tree)


@workspace.command("solve")
@click.argument("root_name")
@click.option("--include-store", is_flag=True, help="Include remote store objects")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.option("--dry-run", is_flag=True, help="Show what would be resolved")
def solve_command(
    root_name: str,
    include_store: bool,
    as_json: bool,
    dry_run: bool,
) -> None:
    """Solve dependencies for a workspace root object.

    Resolves the full transitive dependency graph for ROOT_NAME
    (an engine or project registered in the manifest), showing
    which objects are local, remote, or unknown.

    Example:
        o3de-pilot workspace solve org.o3de.engine.o3de
        o3de-pilot workspace solve org.o3de.project.myproject --include-store
    """
    from o3de_cli.core.store import Store

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Resolving manifest...", total=None)

        resolver = Resolver()
        resolver.resolve()

        store = None
        if include_store:
            progress.update(task, description="Refreshing store...")
            store = Store()
            store.refresh_sync(resolver.manifest_remotes)

        progress.update(task, description="Solving dependencies...")

        def on_progress(msg: str) -> None:
            progress.update(task, description=msg)

        result = solve_for_workspace(
            root_name=root_name,
            resolver=resolver,
            store=store,
            progress_callback=on_progress,
        )

        progress.update(task, description="Done")

    if as_json:
        import json as json_mod
        data = {
            "root": result.root_name,
            "root_version": result.root_version,
            "resolved": result.is_resolved,
            "conflict": result.conflict_message or None,
            "candidates": {
                name: {
                    "version": c.version,
                    "type": c.object_type.value,
                    "status": c.status.value,
                    "path": str(c.path) if c.path else None,
                }
                for name, c in result.candidates.items()
            },
            "children": {
                name: {
                    "version": c.version,
                    "type": c.object_type.value,
                    "path": str(c.path) if c.path else None,
                }
                for name, c in result.children.items()
            },
            "overlays": {
                base: [
                    {
                        "name": o.name,
                        "version": o.version,
                        "precedence": o.precedence,
                    }
                    for o in entries
                ]
                for base, entries in result.overlays.items()
            },
        }
        console.print_json(json_mod.dumps(data, indent=2))
        return

    if not result.is_resolved:
        console.print(f"[red]Resolution failed:[/red] {result.conflict_message}")
        raise SystemExit(1)

    console.print(f"[bold]Workspace: {result.root_name}@{result.root_version}[/bold]")
    console.print()

    # Build table
    table = Table(title="Resolved Dependencies")
    table.add_column("Name", style="cyan")
    table.add_column("Version")
    table.add_column("Type")
    table.add_column("Status")
    table.add_column("Path", style="dim")

    status_style = {
        CandidateStatus.LOCAL: "green",
        CandidateStatus.REMOTE: "blue",
        CandidateStatus.UNKNOWN: "red",
    }

    for name, cand in sorted(result.candidates.items()):
        style = status_style.get(cand.status, "white")
        table.add_row(
            name,
            cand.version,
            cand.object_type.value,
            f"[{style}]{cand.status.value}[/{style}]",
            str(cand.path) if cand.path else "",
        )

    console.print(table)

    # Contained objects (not dependencies)
    if result.children:
        console.print()
        console.print(f"[dim]Contained objects ({len(result.children)}):[/dim]")
        for name, cand in sorted(result.children.items()):
            console.print(f"  [dim]{name}@{cand.version} ({cand.object_type.value})[/dim]")

    # Overlays
    if result.overlays:
        console.print()
        console.print("[bold]Overlays:[/bold]")
        for base_name, entries in result.overlays.items():
            console.print(f"  [cyan]{base_name}[/cyan]:")
            for entry in entries:
                console.print(
                    f"    {entry.name}@{entry.version} "
                    f"(precedence {entry.precedence})"
                )

    console.print()
    console.print(
        f"  [green]{result.local_count} local[/green]  "
        f"[blue]{result.remote_count} remote[/blue]  "
        f"[red]{result.unknown_count} unknown[/red]"
    )


# ---------------------------------------------------------------------------
# workspace build
# ---------------------------------------------------------------------------

def _find_third_party_path(
    meta: WorkspaceMeta | None = None,
    engine_path: Path | None = None,
) -> Path | None:
    """Resolve LY_3RDPARTY_PATH via the full resolution chain.

    Resolution order:
    1. Workspace metadata (``meta.third_party_path``)
    2. Engine settings (``engine.json`` / ``engine.2-0-0.json``)
    3. User config (``~/.o3de/pilot/config.yaml`` ``build.third_party_path``)
    4. Manifest default (``o3de_manifest.json`` ``default.third_party_path``)
    5. Default path (``~/.o3de/3rdParty``)

    Returns the first existing path, or ``None``.
    """
    candidates: list[str] = []

    # 1. Workspace metadata
    if meta is not None:
        ws_tp = getattr(meta, "third_party_path", None)
        if ws_tp:
            candidates.append(ws_tp)

    # 2. Engine settings
    if engine_path is not None:
        for name in ("engine.2-0-0.json", "engine.json"):
            ej = engine_path / name
            if ej.exists():
                try:
                    edata = json.loads(ej.read_text())
                    nested = edata.get("engine", edata)
                    tp = nested.get("third_party_path", "")
                    if tp:
                        candidates.append(tp)
                except Exception:
                    pass
                break

    # 3. User config
    try:
        from o3de_cli.core.config import get_config
        cfg = get_config()
        cfg_tp = cfg.get("build.third_party_path")
        if cfg_tp:
            candidates.append(str(cfg_tp))
    except Exception:
        pass

    # 4. Manifest default
    try:
        manifest_path = get_manifest_path()
        if manifest_path and manifest_path.exists():
            data = json.loads(manifest_path.read_text())
            tp = data.get("default", {}).get("third_party_path", "")
            if tp:
                candidates.append(tp)
    except Exception:
        pass

    # 5. Default path (~/.o3de/3rdParty)
    from o3de_cli.core.paths import get_third_party_path
    candidates.append(str(get_third_party_path()))

    for tp_str in candidates:
        p = Path(tp_str)
        if p.exists():
            return p
    return None


def _find_engine_path(meta: WorkspaceMeta) -> Path | None:
    """Find the engine path from workspace metadata."""
    for _name, path in meta.sources.engines.items():
        if path:
            return Path(path)
    # Fallback: if root_type is engine, use root_object
    if meta.root_type == "engine" and meta.root_object:
        return Path(meta.root_object)
    return None


def _workspace_local_dir(
    ws_path: Path, folder: str, names: "Iterable[str]",
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


def _find_project_path(meta: WorkspaceMeta) -> Path | None:
    """Find the project path from workspace metadata."""
    for _name, path in meta.sources.projects.items():
        if path:
            return Path(path)
    # Fallback: if root_type is project, use root_object
    if meta.root_type == "project" and meta.root_object:
        return Path(meta.root_object)
    return None


_PLATFORM_BUILD_DIR = {
    "win32": "windows",
    "linux": "linux",
    "darwin": "mac",
}


# Default generator per platform (used when --generator auto)
_PLATFORM_DEFAULT_GENERATOR: dict[str, str] = {
    "win32": "Visual Studio 17 2022",
    "linux": "Ninja Multi-Config",
    "darwin": "Xcode",
}

# Generator name aliases for the CLI
_GENERATOR_ALIASES: dict[str, str] = {
    "vs": "Visual Studio 17 2022",
    "ninja": "Ninja Multi-Config",
    "xcode": "Xcode",
    "makefiles": "Unix Makefiles",
}


def _select_generator(choice: str | None) -> str | None:
    """Resolve the ``--generator`` option to a CMake generator string.

    Returns ``None`` when the platform default should be used (no ``-G``
    flag emitted) or an explicit generator string.

    ``auto`` picks the platform default.
    """
    if choice is None or choice == "auto":
        return _PLATFORM_DEFAULT_GENERATOR.get(sys.platform)
    alias = _GENERATOR_ALIASES.get(choice.lower())
    if alias:
        return alias
    # Pass through verbatim (user typed a full CMake generator name)
    return choice


_CMAKE_PRESETS_TEMPLATE = {
    "version": 4,
    "cmakeMinimumRequired": {"major": 3, "minor": 23, "patch": 0},
    "include": [],
}


def _ensure_project_cmake_presets(
    project_path: Path,
    engine_path: Path,
) -> bool:
    """Ensure the project's CMakePresets.json includes the engine's presets.

    If the file doesn't exist, creates it from a template.
    If it exists but doesn't include the engine, adds the include.
    Returns True if the file was created or modified.
    """
    preset_path = project_path / "CMakePresets.json"
    engine_presets = engine_path / "CMakePresets.json"

    if not engine_presets.exists():
        return False

    engine_include = engine_presets.as_posix()

    if preset_path.exists():
        try:
            data = json.loads(preset_path.read_text())
        except (json.JSONDecodeError, OSError):
            data = {}
    else:
        data = {}

    if not data:
        data = dict(_CMAKE_PRESETS_TEMPLATE)
        data["include"] = [engine_include]
        _write_unlinked(preset_path, json.dumps(data, indent=4) + "\n")
        return True

    includes = data.get("include", [])
    # Check if engine is already included (compare as posix paths)
    for inc in includes:
        if Path(inc).as_posix() == engine_include:
            return False

    # Replace includes to point at current engine only
    data["include"] = [engine_include]
    _write_unlinked(preset_path, json.dumps(data, indent=4) + "\n")
    return True


def _write_unlinked(path: Path, content: str) -> None:
    """Write *content* to *path*, breaking any hard/sym link first.

    Workspace files are hard links (or symlinks) to the original
    sources.  Writing through a hard link would modify the source
    file, so remove the link and create an independent file.
    """
    if path.exists() or path.is_symlink():
        try:
            path.unlink()
        except OSError:
            pass
    path.write_text(content)


def _run_cmake(
    cmd: list[str],
    *,
    cwd: Path,
    on_line: "Callable[[str], None] | None" = None,
) -> int:
    """Run a CMake command with line-by-line output streaming.

    Uses ``subprocess.Popen`` so that each output line is emitted
    immediately (for GUI progress callbacks) and ``KeyboardInterrupt``
    (Ctrl+C) cleanly terminates the process tree.

    Parameters
    ----------
    cmd:
        The command list to execute.
    cwd:
        Working directory for the subprocess.
    on_line:
        Optional callback invoked for every stdout/stderr line.
        If *None*, lines are printed to stdout.

    Returns
    -------
    int
        The process return code.  ``-15`` (SIGTERM) on cancel.
    """
    import signal

    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            stripped = line.rstrip("\n\r")
            if on_line is not None:
                on_line(stripped)
            else:
                print(stripped, flush=True)
        return proc.wait()
    except KeyboardInterrupt:
        # Graceful cancel — try SIGTERM first, then kill
        try:
            if sys.platform == "win32":
                # Windows: taskkill /T kills entire process tree
                subprocess.run(
                    ["taskkill", "/pid", str(proc.pid), "/f", "/t"],
                    capture_output=True,
                )
            else:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except Exception:
            proc.kill()
        console.print("\n[yellow]Build cancelled.[/yellow]")
        return -15


@workspace.command("build")
@click.argument("name_or_path")
@click.option(
    "--config", "-c",
    type=click.Choice(["debug", "profile", "release"]),
    default="profile",
    help="Build configuration (default: profile)",
)
@click.option(
    "--target", "-t",
    multiple=True,
    help="CMake build targets (e.g. Editor, MyProject.GameLauncher). "
         "If omitted, builds all default targets.",
)
@click.option(
    "--engine-centric",
    is_flag=True,
    help="Use engine-centric build mode (-S engine -DLY_PROJECTS=project)",
)
@click.option(
    "--configure-only",
    is_flag=True,
    help="Run CMake configure but skip the build step",
)
@click.option(
    "--reconfigure",
    is_flag=True,
    help="Force CMake reconfigure even if build directory exists",
)
@click.option(
    "--preset",
    default=None,
    help="CMake configure preset name (e.g. windows-default)",
)
@click.option(
    "--third-party-path",
    type=click.Path(exists=True),
    default=None,
    help="Override LY_3RDPARTY_PATH",
)
@click.option(
    "--parallel", "-j",
    type=int,
    default=None,
    help="Number of parallel build jobs",
)
@click.option(
    "--generator", "-G",
    default=None,
    help="CMake generator (auto, vs, ninja, xcode, makefiles, or full name). "
         "Default: auto-detect per platform.",
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be done without executing",
)
def build_command(
    name_or_path: str,
    config: str,
    target: tuple[str, ...],
    engine_centric: bool,
    configure_only: bool,
    reconfigure: bool,
    preset: str | None,
    third_party_path: str | None,
    parallel: int | None,
    generator: str | None,
    as_json: bool,
    dry_run: bool,
) -> None:
    """Build an O3DE workspace using CMake.

    Reads workspace metadata to locate engine, project, and gems,
    then runs CMake configure + build.

    Project-centric mode (default):
        cmake -S <project> -B <project>/build/<platform> -DLY_3RDPARTY_PATH=...

    Engine-centric mode (--engine-centric):
        cmake -S <engine> -B <build_dir> -DLY_PROJECTS=<project> -DLY_3RDPARTY_PATH=...

    Examples:
        o3de workspace build my-workspace
        o3de workspace build ./workspace --config debug --target Editor
        o3de workspace build my-workspace --engine-centric --preset windows-default
    """

    from o3de_cli.core.json_output import emit_response, emit_error

    # Find workspace
    ws_path = Path(name_or_path)
    if not ws_path.exists():
        ws_path = get_default_workspaces_path() / name_or_path

    if not ws_path.exists():
        if as_json:
            emit_error(f"Workspace not found: {name_or_path}", code="E_WS_NOT_FOUND")
        else:
            console.print(f"[red]Workspace not found:[/red] {name_or_path}")
        raise SystemExit(1)

    meta = _read_workspace_meta(ws_path)
    if meta is None:
        if as_json:
            emit_error(f"Not a valid workspace: {ws_path}", code="E_WS_INVALID")
        else:
            console.print(f"[red]Not a valid workspace:[/red] {ws_path}")
        raise SystemExit(1)

    # Resolve paths — prefer the composed workspace trees over the
    # original source paths, so builds use the workspace composition
    # (engine without embedded gems, workspace-level Gems/, overlays).
    engine_path = _workspace_local_dir(
        ws_path, "Engines", meta.sources.engines.keys(),
    ) or _find_engine_path(meta)
    project_path = _workspace_local_dir(
        ws_path, "Projects", meta.sources.projects.keys(),
    ) or _find_project_path(meta)

    if not engine_path:
        if as_json:
            emit_error("No engine found in workspace metadata.", code="E_NO_ENGINE")
        else:
            console.print("[red]No engine found in workspace metadata.[/red]")
            console.print("[dim]Hint: Re-create the workspace with a registered engine.[/dim]")
        raise SystemExit(1)

    if not project_path and not engine_centric:
        engine_centric = True
        if not as_json:
            console.print(
                "[yellow]No project found in workspace — "
                "switching to engine-centric build.[/yellow]"
            )

    # Resolve third-party path (K6 resolution chain)
    tp_path: Path | None = None
    if third_party_path:
        tp_path = Path(third_party_path)
    else:
        tp_path = _find_third_party_path(meta=meta, engine_path=engine_path)

    # K5: Resolve generator
    cmake_generator = _select_generator(generator)

    # Platform build subdirectory
    platform_dir = _PLATFORM_BUILD_DIR.get(sys.platform, sys.platform)

    # Determine source and build directories.
    # The build dir lives at the workspace root (not inside the engine or
    # project trees) to keep paths short — deep autogen/moc output paths
    # can otherwise exceed Windows MAX_PATH (260 chars).
    if engine_centric:
        source_dir = engine_path
    else:
        assert project_path is not None
        source_dir = project_path
    build_dir = ws_path / "build" / platform_dir

    mode = "engine-centric" if engine_centric else "project-centric"

    if not as_json:
        console.print(f"[bold]Building workspace:[/bold] {meta.workspace.name}")
        console.print(f"  Mode: {mode}")
        console.print(f"  Config: {config}")
        console.print(f"  Source: {source_dir}")
        console.print(f"  Build dir: {build_dir}")
        if cmake_generator:
            console.print(f"  Generator: {cmake_generator}")
        if tp_path:
            console.print(f"  3rd-party: {tp_path}")

    # ------------------------------------------------------------------
    # K2: Ensure project CMakePresets.json includes engine presets
    # (skipped on --dry-run: must not mutate any files)
    # ------------------------------------------------------------------
    if not dry_run and not engine_centric and project_path:
        if _ensure_project_cmake_presets(project_path, engine_path):
            if not as_json:
                console.print(
                    "[dim]Updated project CMakePresets.json with engine include[/dim]"
                )

    # ------------------------------------------------------------------
    # Build configure + build commands
    # ------------------------------------------------------------------
    needs_configure = reconfigure or not (build_dir / "CMakeCache.txt").exists()
    configure_cmd: list[str] | None = None
    build_cmd_list: list[str] | None = None

    if needs_configure:
        if preset:
            configure_cmd = [
                "cmake",
                "--preset", preset,
                "-S", str(source_dir),
            ]
        else:
            configure_cmd = [
                "cmake",
                "-B", str(build_dir),
                "-S", str(source_dir),
            ]
            if cmake_generator:
                configure_cmd.append(f"-G{cmake_generator}")

        if tp_path:
            configure_cmd.append(f"-DLY_3RDPARTY_PATH={tp_path}")
        if engine_centric and project_path:
            configure_cmd.append(f"-DLY_PROJECTS={project_path}")

        # Workspace-scoped resolved manifest: point CMake at the
        # compose-time solution instead of ~/.o3de's user manifest.
        ws_cmake_manifest = ws_path / "resolved_o3de_manifest.json"
        if ws_cmake_manifest.exists():
            configure_cmd.append(
                f"-DO3DE_RESOLVED_MANIFEST={ws_cmake_manifest.as_posix()}"
            )
        # Tell the project bootstrap where the composed engine lives
        if engine_path and not engine_centric:
            configure_cmd.append(
                f"-DO3DE_ENGINE_PATH={Path(engine_path).as_posix()}"
            )

    if not configure_only:
        cmake_config = {"debug": "Debug", "profile": "Profile", "release": "Release"}[config]
        build_cmd_list = [
            "cmake",
            "--build", str(build_dir),
            "--config", cmake_config,
        ]
        if target:
            build_cmd_list.append("--target")
            build_cmd_list.extend(target)
        if parallel:
            build_cmd_list.extend(["--parallel", str(parallel)])
        else:
            build_cmd_list.append("--parallel")

    # ------------------------------------------------------------------
    # Dry-run: show commands and exit
    # ------------------------------------------------------------------
    if dry_run:
        commands = []
        if configure_cmd:
            commands.append(configure_cmd)
        if build_cmd_list:
            commands.append(build_cmd_list)

        if as_json:
            emit_response(data={
                "workspace": meta.workspace.name,
                "mode": mode,
                "config": config,
                "source_dir": str(source_dir),
                "build_dir": str(build_dir),
                "generator": cmake_generator,
                "third_party_path": str(tp_path) if tp_path else None,
                "commands": [" ".join(c) for c in commands],
                "dry_run": True,
            })
        else:
            console.print("\n[bold yellow]Dry run — commands that would be executed:[/bold yellow]")
            for cmd in commands:
                console.print(f"  [dim]$ {' '.join(cmd)}[/dim]")
        return

    # ------------------------------------------------------------------
    # Execute: CMake configure
    # ------------------------------------------------------------------
    if configure_cmd:
        if not as_json:
            console.print("\n[bold]Configuring...[/bold]")
            console.print(f"[dim]$ {' '.join(configure_cmd)}[/dim]")

        rc = _run_cmake(configure_cmd, cwd=source_dir)
        if rc != 0:
            if as_json:
                emit_error("CMake configure failed.", code="E_CONFIGURE_FAILED")
            else:
                console.print("[red]CMake configure failed.[/red]")
            raise SystemExit(rc)

        if not as_json:
            console.print("[green]Configure complete.[/green]")

    if configure_only:
        if as_json:
            emit_response(data={
                "workspace": meta.workspace.name,
                "phase": "configure",
                "status": "complete",
            })
        return

    # ------------------------------------------------------------------
    # Execute: CMake build
    # ------------------------------------------------------------------
    assert build_cmd_list is not None
    if not as_json:
        console.print("\n[bold]Building...[/bold]")
        console.print(f"[dim]$ {' '.join(build_cmd_list)}[/dim]")

    rc = _run_cmake(build_cmd_list, cwd=source_dir)
    if rc != 0:
        if as_json:
            emit_error("Build failed.", code="E_BUILD_FAILED")
        else:
            console.print("[red]Build failed.[/red]")
        raise SystemExit(rc)

    cmake_config = {"debug": "Debug", "profile": "Profile", "release": "Release"}[config]
    if as_json:
        emit_response(data={
            "workspace": meta.workspace.name,
            "mode": mode,
            "config": cmake_config,
            "source_dir": str(source_dir),
            "build_dir": str(build_dir),
        })
    else:
        console.print(f"\n[green]Build complete:[/green] {cmake_config}")


@workspace.command("lock")
@click.argument("name_or_path")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def lock_command(name_or_path: str, as_json: bool) -> None:
    """Generate a lockfile for a workspace's resolved dependencies.

    Creates workspace-lock.json in the workspace directory, recording
    the exact versions of all resolved dependencies. Use with
    `workspace create --frozen` to reproduce exact builds.

    Example:
        o3de-pilot workspace lock my-workspace
    """
    from o3de_cli.core.lockfile import generate_lockfile, read_lockfile
    from o3de_cli.core.json_output import emit_response, emit_error

    ws_path = _resolve_workspace_path(name_or_path)
    if not ws_path or not ws_path.exists():
        if as_json:
            emit_error(f"Workspace not found: {name_or_path}", code="E_NOT_FOUND")
        else:
            console.print(f"[red]Workspace not found: {name_or_path}[/red]")
        raise SystemExit(1)

    # Read workspace metadata
    meta_path = ws_path / "workspace.json"
    if not meta_path.exists():
        if as_json:
            emit_error("No workspace.json found", code="E_NOT_FOUND")
        else:
            console.print("[red]No workspace.json found in workspace[/red]")
        raise SystemExit(1)

    with open(meta_path) as f:
        meta = json.load(f)

    # Extract resolved candidates from workspace metadata
    # New format: sources is categorised {type: {name: path}}
    # Flatten into {name: {path: ..., type: ...}} for lockfile
    raw_sources = meta.get("sources", {})
    if isinstance(raw_sources, dict) and "engines" in raw_sources:
        candidates = _flatten_sources_for_lockfile(raw_sources)
    else:
        # Legacy format fallback
        candidates = meta.get("resolved_candidates", meta.get("sources", {}))
    root_name = meta.get("root", meta.get("name", name_or_path))
    root_version = meta.get("rootVersion", meta.get("version", "0.0.0"))

    lockfile_path = generate_lockfile(
        workspace_path=ws_path,
        resolved_candidates=candidates,
        root_name=root_name,
        root_version=root_version,
    )

    if as_json:
        lockdata = read_lockfile(ws_path)
        emit_response(data={
            "lockfile": str(lockfile_path),
            "packages": len(lockdata.get("packages", {})),
            "contentHash": lockdata.get("contentHash", ""),
        })
    else:
        console.print(f"[green]Lockfile written:[/green] {lockfile_path}")


@workspace.command("verify-lock")
@click.argument("name_or_path")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def verify_lock_command(name_or_path: str, as_json: bool) -> None:
    """Verify the workspace's current state matches its lockfile.

    Checks that the resolved dependencies match what's recorded
    in workspace-lock.json.
    """
    from o3de_cli.core.lockfile import verify_lockfile
    from o3de_cli.core.json_output import emit_error

    ws_path = _resolve_workspace_path(name_or_path)
    if not ws_path or not ws_path.exists():
        if as_json:
            emit_error(f"Workspace not found: {name_or_path}", code="E_NOT_FOUND")
        else:
            console.print(f"[red]Workspace not found: {name_or_path}[/red]")
        raise SystemExit(1)

    meta_path = ws_path / "workspace.json"
    if not meta_path.exists():
        if as_json:
            emit_error("No workspace.json found", code="E_NOT_FOUND")
        else:
            console.print("[red]No workspace.json found[/red]")
        raise SystemExit(1)

    with open(meta_path) as f:
        meta = json.load(f)

    candidates = meta.get("resolved_candidates", meta.get("sources", {}))
    if isinstance(meta.get("sources"), dict) and "engines" in meta.get("sources", {}):
        candidates = _flatten_sources_for_lockfile(meta["sources"])
    matches, mismatches = verify_lockfile(ws_path, candidates)

    if as_json:
        console.print_json(json.dumps({
            "verified": matches,
            "mismatches": mismatches,
        }))
    else:
        if matches:
            console.print("[green]Lockfile verified — all packages match.[/green]")
        else:
            console.print("[red]Lockfile mismatch:[/red]")
            for m in mismatches:
                console.print(f"  • {m}")

    if not matches:
        raise SystemExit(1)


def _resolve_workspace_path(name_or_path: str) -> Path | None:
    """Resolve a workspace name or path to an actual directory."""
    p = Path(name_or_path)
    if p.is_dir():
        return p
    # Try under default workspaces directory
    ws_path = get_default_workspaces_path() / name_or_path
    if ws_path.is_dir():
        return ws_path
    return None


def _flatten_sources_for_lockfile(sources: dict) -> dict:
    """Flatten categorised sources into {name: {path, type}} for lockfile."""
    _type_map = {
        "engines": "engine", "projects": "project",
        "gems": "gem", "templates": "template", "overlays": "overlay",
    }
    result: dict[str, dict] = {}
    for type_key, items in sources.items():
        if isinstance(items, dict):
            obj_type = _type_map.get(type_key, type_key)
            for name, path in items.items():
                result[name] = {"path": path, "object_type": obj_type, "version": "0.0.0"}
    return result
