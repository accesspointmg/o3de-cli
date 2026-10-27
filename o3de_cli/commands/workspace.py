# O3DE CLI - Workspace Commands
# SPDX-License-Identifier: Apache-2.0 OR MIT

"""Workspace management commands.

Workspaces are symlinked build directories that combine:
- Engine source
- Project source
- Gem sources
- Overlay customizations

This allows efficient builds without copying files.
"""

import contextlib
import json
import subprocess
import sys
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import Any, NoReturn

import click
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from o3de_cli.core import (
    ObjectType,
    Resolver,
    get_default_workspaces_path,
    get_manifest_path,
    get_resolved_manifest_path,
)
from o3de_cli.core.models import (
    SCHEMA_BASE_URL,
    SCHEMA_VERSION,
    ResolvedCandidate,
    WorkspaceHeader,
    WorkspaceMeta,
)
from o3de_cli.core.paths import rmtree_force
from o3de_cli.core.solver import (
    CandidateStatus,
    OverlayEntry,
    SolveResult,
    solve_for_workspace,
)
from o3de_cli.core.workspace import Workspace, WorkspaceError, create_workspace, detect_root_type
from o3de_cli.core.workspace_ops import (
    CMAKE_CONFIGS,
    WORKSPACE_META,
    BuildLayout,
    BuildOptions,
    CreateReporter,
    CreateRequest,
    OverlayChange,
    OverlayChangeResult,
    OverlaySelection,
    OverrideRequest,
    SolveDeps,
    SolveFailedError,
    WorkspaceAssembly,
    WorkspaceOpError,
    apply_overlay_order,
    apply_override,
    apply_resync,
    assemble_workspace,
    attributions_change,
    build_layout,
    build_source_paths,
    change_overlays,
    explicit_overlay_tuples,
    parse_tag_selection,
    plan_cmake_commands,
    plan_resync,
    root_gem_target,
    secondary_engine_objects,
    select_root,
    solve_create_dependencies,
)

# Helpers that moved to core.workspace_ops, kept under their old names
# for the other commands here and for callers that import them.
from o3de_cli.core.workspace_ops import HOST_PLATFORM_MAP as _HOST_PLATFORM_MAP
from o3de_cli.core.workspace_ops import LEGACY_WORKSPACE_META as _LEGACY_WORKSPACE_META
from o3de_cli.core.workspace_ops import build_workspace_meta as _build_workspace_meta
from o3de_cli.core.workspace_ops import expand_platform_selection as _expand_platform_selection
from o3de_cli.core.workspace_ops import find_engine_path as _find_engine_path
from o3de_cli.core.workspace_ops import find_project_path as _find_project_path
from o3de_cli.core.workspace_ops import find_workspace_meta as _find_workspace_meta
from o3de_cli.core.workspace_ops import overlays_for_base as _overlays_for_base
from o3de_cli.core.workspace_ops import read_overlay_info as _read_overlay_info
from o3de_cli.core.workspace_ops import read_workspace_meta as _read_workspace_meta
from o3de_cli.core.workspace_ops import relink_object as _relink_object
from o3de_cli.core.workspace_ops import (
    select_overlays_for_platforms as _select_overlays_for_platforms,
)
from o3de_cli.core.workspace_ops import workspace_local_dir as _workspace_local_dir
from o3de_cli.core.workspace_ops import workspace_root_name as _workspace_root_name
from o3de_cli.core.workspace_ops import write_workspace_meta as _write_workspace_meta

console = Console()


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


@click.group()
def workspace() -> None:
    """Manage build workspaces.

    Workspaces are symlinked directory structures that combine
    engine, project, gems, and overlays for building.
    """
    pass


def _exit_with_error(
    as_json: bool,
    message: str,
    code: str,
    *console_lines: str,
    exit_code: int = 1,
) -> NoReturn:
    """Report an error as a JSON error response or console text, then exit.

    *console_lines* are printed in console mode; by default the message
    is printed in red.
    """
    if as_json:
        from o3de_cli.core.json_output import emit_error

        emit_error(message, code=code)
    else:
        for line in console_lines or (f"[red]{message}[/red]",):
            console.print(line)
    raise SystemExit(exit_code)


def _spinner(disable: bool = False) -> Progress:
    """The status spinner the long-running commands show.

    Pass ``disable=as_json``: under ``--json`` stdout carries only the
    JSON document, and Rich prints the spinner's last frame to a
    non-terminal stdout when it stops.
    """
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        disable=disable,
    )


def _emit_json_document(data: Any) -> None:
    """Write *data* to stdout as one plain JSON document.

    For the commands whose ``--json`` output predates the response
    envelope.  ``console.print_json`` is not used: Rich adds colour codes
    when it thinks stdout is a terminal (e.g. ``FORCE_COLOR``), which
    breaks parsers.
    """
    from o3de_cli.core.json_output import emit

    emit(json.dumps(data, indent=2, default=str))


def _load_workspace(
    name_or_path: str, as_json: bool, *invalid_hint: str
) -> tuple[Path, WorkspaceMeta]:
    """Find a workspace by path or by name under the default workspaces dir.

    Exits with ``E_WS_NOT_FOUND`` / ``E_WS_INVALID``; *invalid_hint*
    lines follow the console error for a directory without metadata.
    """
    ws_path = Path(name_or_path)
    if not ws_path.exists():
        ws_path = get_default_workspaces_path() / name_or_path
    if not ws_path.exists():
        _exit_with_error(
            as_json,
            f"Workspace not found: {name_or_path}",
            "E_WS_NOT_FOUND",
            f"[red]Workspace not found:[/red] {name_or_path}",
        )
    meta = _read_workspace_meta(ws_path)
    if meta is None:
        _exit_with_error(
            as_json,
            f"Not a valid workspace: {ws_path}",
            "E_WS_INVALID",
            f"[red]Not a valid workspace:[/red] {ws_path}",
            *invalid_hint,
        )
    return ws_path, meta


def _write_cmake_manifest(
    ws_path: Path, meta: WorkspaceMeta, overrides: dict | None = None
) -> None:
    """(Re)write the workspace-scoped CMake resolved manifest.

    The compose-time solve realized on disk: CMake consumes this instead
    of re-resolving against the user manifest.
    """
    from o3de_cli.core.cmake_manifest import generate_cmake_manifest

    tp = _find_third_party_path(meta=meta)
    generate_cmake_manifest(
        ws_path,
        third_party_path=str(tp) if tp else "",
        overrides=overrides,
    )


@workspace.command("create")
@click.argument("name")
@click.option("--engine", "-e", "engine_path", type=click.Path(exists=True), help="Engine path")
@click.option("--project", "-p", "project_path", type=click.Path(exists=True), help="Project path")
@click.option(
    "--root",
    "-r",
    "root_opt",
    type=click.Path(exists=True),
    help="Root object path of any type (gem, template, engine, "
    "project) — the object this workspace is for; its "
    "dependencies anchor the solve",
)
@click.option("--output", "-o", type=click.Path(), help="Output directory")
@click.option(
    "--overlay", multiple=True, type=click.Path(exists=True), help="Overlay path (can be repeated)"
)
@click.option("--no-overlays", is_flag=True, help="Don't apply overlays")
@click.option(
    "--platforms",
    "-P",
    "platforms_opt",
    multiple=True,
    help="Platforms to include when selecting overlays "
    "(repeatable or comma-separated; 'host' = this machine, "
    "'all' = no filtering). Default: all.",
)
@click.option(
    "--tags",
    "-T",
    "tags_opt",
    multiple=True,
    help="User tags to include when selecting overlays "
    "(repeatable or comma-separated; OR-combined with "
    "--platforms).",
)
@click.option(
    "--include-overlay",
    "include_overlay",
    multiple=True,
    help="Force-include a solved overlay by object name (overrides platform filtering; repeatable)",
)
@click.option(
    "--exclude-overlay",
    "exclude_overlay",
    multiple=True,
    help="Exclude a solved overlay by object name (repeatable)",
)
@click.option(
    "--overlay-order",
    "overlay_order_opt",
    multiple=True,
    help="Explicit apply order for overlays extending one object: "
    "comma-separated overlay names, first applied first "
    "(later entries win file conflicts). Overrides authored "
    "precedence; repeatable per base.",
)
@click.option(
    "--attributions",
    "attributions",
    type=click.Choice(["workspace", "object", "off"]),
    default="workspace",
    show_default=True,
    help="Where composed overlays' metadata (overlay.json, "
    "licenses, icon) is recorded: workspace ledger, inside "
    "the extended object, or not at all",
)
@click.option(
    "--no-solve",
    is_flag=True,
    help="Skip dependency resolution — only use explicitly provided paths",
)
@click.option(
    "--include-store", is_flag=True, help="Include remote store when resolving dependencies"
)
@click.option(
    "--auto-install",
    "-y",
    is_flag=True,
    help="Automatically download and install missing remote dependencies",
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def create_command(
    name: str,
    engine_path: str | None,
    project_path: str | None,
    root_opt: str | None,
    output: str | None,
    overlay: tuple[str, ...],
    no_overlays: bool,
    platforms_opt: tuple[str, ...],
    tags_opt: tuple[str, ...],
    include_overlay: tuple[str, ...],
    exclude_overlay: tuple[str, ...],
    overlay_order_opt: tuple[str, ...],
    attributions: str,
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
        o3de workspace create my-build -e ./o3de -p ./my-project
        o3de workspace create my-build -e ./o3de -p ./my-project --no-solve
    """
    if not engine_path and not project_path and not root_opt:
        _exit_with_error(as_json, "Must specify --root, --engine or --project", "E_INVALID_ARGS")

    if output:
        output_path = Path(output).resolve() / name
    else:
        output_path = get_default_workspaces_path() / name
    if output_path.exists():
        _exit_with_error(
            as_json,
            f"Workspace already exists: {output_path}",
            "E_WS_EXISTS",
            f"[red]Workspace already exists:[/red] {output_path}",
            "Use 'workspace update' to update, or delete first.",
        )

    try:
        root_path, root_type = select_root(root_opt, project_path, engine_path)
    except WorkspaceError as e:
        _exit_with_error(as_json, str(e), "E_NOT_AN_OBJECT")
    # No --platforms / --tags means all overlays (no filtering)
    selection = OverlaySelection(
        platforms=_expand_platform_selection(platforms_opt),
        tags=parse_tag_selection(tags_opt),
        include=set(include_overlay),
        exclude=set(exclude_overlay),
    )
    request = CreateRequest(
        root_path=root_path,
        engine_path=engine_path,
        project_path=project_path,
        explicit_overlays=overlay,
        no_overlays=no_overlays,
        selection=selection,
        include_store=include_store,
        auto_install=auto_install,
    )
    assembly = WorkspaceAssembly(
        resolved_objects=secondary_engine_objects(engine_path, root_path),
        overlay_tuples=explicit_overlay_tuples(overlay, no_overlays),
    )

    warnings: list[str] = []
    with _spinner(disable=as_json) as progress:
        if not no_solve:
            warnings = _create_solve_phase(progress, request, assembly, as_json)
        _create_assemble_phase(
            progress,
            name,
            output_path,
            root_type,
            request,
            assembly,
            overlay_order_opt,
            attributions,
            as_json,
        )

    _render_created(as_json, output_path, root_path, assembly, selection.platforms, warnings)


class _ConsoleCreateReporter(CreateReporter):
    """Drives the create spinner and prints solve warnings to the console.

    Under ``--json`` nothing is printed: warnings are collected in
    :attr:`warnings` (as plain text) for the response envelope, so stdout
    carries only the JSON document.
    """

    def __init__(self, progress: Progress, task: Any, as_json: bool = False) -> None:
        self._progress = progress
        self._task = task
        self._as_json = as_json
        self.warnings: list[str] = []

    def _print_paused(self, *lines: str) -> None:
        if self._as_json:
            self.warnings.extend(Text.from_markup(line).plain.lstrip("⚠ ") for line in lines)
            return
        # Stop the spinner so warnings aren't interleaved with it
        self._progress.stop()
        for line in lines:
            console.print(line)
        self._progress.start()

    def progress(self, message: str) -> None:
        self._progress.update(self._task, description=message)

    def overlay_unavailable(self, name: str) -> None:
        self._print_paused(
            f"[yellow]⚠ Overlay {name} not installed and "
            f"not found in remote repos — skipping[/yellow]"
        )

    def remote_installed(self, count: int) -> None:
        if not self._as_json:
            console.print(f"[green]Installed {count} remote dependencies[/green]")

    def auto_install_needs_store(self) -> None:
        self._print_paused("[yellow]⚠ --auto-install requires --include-store[/yellow]")

    def root_not_registered(self) -> None:
        message = "Root object not registered in manifest — skipping dependency resolution"
        if self._as_json:
            self.warnings.append(message)
            return
        console.print(f"[dim]{message}[/dim]")
        self._progress.start()

    def unresolved(self, root_name: str, remote_count: int, unknown_count: int) -> None:
        lines: list[str] = []
        if remote_count:
            lines.append(
                f"[yellow]⚠ {remote_count} remote dependencies not yet "
                f"installed — run 'workspace solve {root_name} "
                f"--include-store' for details[/yellow]"
            )
        if unknown_count:
            lines.append(f"[red]⚠ {unknown_count} dependencies could not be found anywhere[/red]")
        self._print_paused(*lines)


def _create_solve_phase(
    progress: Progress, request: CreateRequest, assembly: WorkspaceAssembly, as_json: bool
) -> list[str]:
    """Phase 1 of create: dependency resolution, reported on the spinner.

    Returns the warnings collected under ``--json`` (empty otherwise:
    in console mode they are printed as they happen).
    """
    task = progress.add_task("Resolving manifest...", total=None)
    deps = SolveDeps(
        resolver_cls=Resolver,
        solve=solve_for_workspace,
        install_overlay=_install_remote_overlay,
    )
    reporter = _ConsoleCreateReporter(progress, task, as_json)
    try:
        solve_create_dependencies(request, assembly, deps, reporter)
    except SolveFailedError as e:
        progress.stop()
        _exit_with_error(as_json, e.message, e.code, f"[red]{e.label}:[/red] {e.detail}")
    progress.update(task, description="Dependencies resolved")
    return reporter.warnings


def _create_assemble_phase(
    progress: Progress,
    name: str,
    output_path: Path,
    root_type: ObjectType,
    request: CreateRequest,
    assembly: WorkspaceAssembly,
    overlay_order_opt: tuple[str, ...],
    attributions: str,
    as_json: bool,
) -> None:
    """Phase 2 of create: order overlays, compose, write metadata and CMake manifest."""
    task = progress.add_task("Creating workspace...", total=None)
    try:
        assembly.overlay_tuples, order_map = apply_overlay_order(
            assembly.overlay_tuples, overlay_order_opt
        )
    except WorkspaceOpError as e:
        _exit_with_error(as_json, e.message, e.code)

    meta = assemble_workspace(
        output_path,
        name,
        request.root_path,
        root_type,
        assembly,
        attributions,
        request.selection.platforms,
        order_map,
    )
    progress.update(task, description="Writing CMake manifest...")
    _write_cmake_manifest(output_path, meta)
    progress.update(task, description="Done")


def _render_created(
    as_json: bool,
    output_path: Path,
    root_path: Path,
    assembly: WorkspaceAssembly,
    platforms: list[str] | None,
    warnings: list[str] | None = None,
) -> None:
    solve_result = assembly.solve_result
    objects = len(assembly.resolved_objects) + 1
    if as_json:
        from o3de_cli.core.json_output import emit_response

        data: dict[str, Any] = {
            "action": "created",
            "workspace": str(output_path),
            "root": str(root_path),
            "objects": objects,
            "overlays": len(assembly.overlay_tuples),
        }
        if solve_result:
            data["resolved_local"] = solve_result.local_count
            data["resolved_remote"] = solve_result.remote_count
        if platforms:
            data["platforms"] = platforms
        emit_response(data=data, warnings=warnings)
        return

    console.print(f"[green]Created workspace:[/green] {output_path}")
    console.print(f"  Root: {root_path}")
    console.print(f"  Objects: {objects}")
    if solve_result:
        console.print(f"  Resolved: {solve_result.local_count} local")
        if solve_result.remote_count:
            console.print(f"  Remote (not installed): {solve_result.remote_count}")
    console.print(f"  Overlays: {len(assembly.overlay_tuples)}")
    if platforms:
        console.print(f"  Platforms: {', '.join(platforms)}")


@workspace.command("candidates")
@click.argument("name_or_path")
@click.option("--name", "object_name", default=None, help="Limit output to one object")
@click.option("--include-store", is_flag=True, help="Include remote store candidates")
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
    from o3de_cli.core.json_output import emit_error, emit_response
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
            root_name=root_name,
            resolver=resolver,
            store=store,
            overrides=pins,
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
            (meta.sources.engines, "engine"),
            (meta.sources.projects, "project"),
            (meta.sources.gems, "gem"),
            (meta.sources.templates, "template"),
        ):
            for cname, cpath in bucket.items():
                chosen[cname] = ("", type_name, cpath)

    names = [object_name] if object_name else sorted(chosen)
    objects_out: list[dict[str, Any]] = []
    for cname in names:
        version, type_name, path = chosen.get(cname, ("", "", ""))
        cands = list_candidates(cname, resolver, store)
        override = meta.overrides.get(cname)
        objects_out.append(
            {
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
            }
        )

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
            entry["name"],
            entry["chosen_version"],
            ", ".join(cand_strs) or "—",
            ov_str,
        )
    console.print(table)


@workspace.command("override")
@click.argument("name_or_path")
@click.argument("object_name")
@click.option(
    "--version", "pin_version", default=None, help="Exact version to pin (required unless --clear)"
)
@click.option(
    "--artifact",
    type=click.Choice(["source", "local-binary", "remote-binary", "remote-source"]),
    default="source",
    help="Artifact form to use",
)
@click.option("--clear", is_flag=True, help="Remove the override for this object")
@click.option(
    "--no-apply", is_flag=True, help="Validate and persist only — don't relink the workspace"
)
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
    if not clear and not pin_version:
        _exit_with_error(as_json, "--version is required (or use --clear)", "E_INVALID_ARGS")

    ws_path = _resolve_workspace_path(name_or_path)
    if ws_path is None:
        _exit_with_error(as_json, f"Workspace not found: {name_or_path}", "E_WS_NOT_FOUND")
    meta = _read_workspace_meta(ws_path)
    if meta is None:
        _exit_with_error(as_json, f"Not a valid workspace: {ws_path}", "E_WS_INVALID")

    def _download_progress(msg: str) -> None:
        if not as_json:
            console.print(f"[dim]{msg}[/dim]")

    request = OverrideRequest(
        object_name=object_name,
        version=pin_version,
        artifact=artifact,
        clear=clear,
        apply=not no_apply,
    )
    try:
        changed = apply_override(
            ws_path,
            meta,
            request,
            resolver_cls=Resolver,
            solve=solve_for_workspace,
            on_progress=_download_progress,
        )
    except WorkspaceOpError as e:
        _exit_with_error(as_json, e.message, e.code)
    if request.apply:
        _write_cmake_manifest(ws_path, meta, overrides=meta.overrides)

    _render_override(as_json, request, changed)


def _render_override(as_json: bool, request: OverrideRequest, changed: list[str]) -> None:
    if as_json:
        from o3de_cli.core.json_output import emit_response

        emit_response(
            data={
                "action": "cleared" if request.clear else "overridden",
                "object": request.object_name,
                "version": request.version,
                "artifact": request.artifact,
                "relinked": changed,
                "applied": request.apply,
            }
        )
        return

    if request.clear:
        console.print(f"[green]Cleared override for[/green] {request.object_name}")
    else:
        console.print(
            f"[green]Pinned[/green] {request.object_name}=={request.version} [{request.artifact}]"
        )
    if changed:
        console.print(f"  Relinked: {', '.join(changed)}")
    elif request.apply:
        console.print("  No linked objects changed")
    if request.artifact in ("local-binary", "remote-binary") and not request.clear:
        console.print(
            "[dim]Prebuilt package will be imported on the next "
            "(re)configure instead of building the gem from source[/dim]"
        )
    elif request.artifact == "remote-source" and not request.clear:
        console.print(
            "[dim]Source release downloaded and registered — builds "
            "from source like any local gem[/dim]"
        )


def _install_remote_overlay(name: str) -> Path | None:
    """Install an overlay from the registered remote repos by object name.

    Returns the installed path, or None if the overlay is unknown remotely.
    """
    from o3de_cli.core.paths import get_default_path_for_type
    from o3de_cli.core.store import Store, get_manifest_remote_urls

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

    # Register in the manifest so the resolver can see it.  Its status
    # line goes to stderr: stdout is the calling command's (one JSON
    # document under --json).
    from o3de_cli.commands.manifest import add_command

    ctx = click.Context(add_command)
    with contextlib.redirect_stdout(sys.stderr):
        ctx.invoke(add_command, path=str(installed))
    return installed


@workspace.command("update")
@click.argument("name_or_path")
@click.option(
    "--overlay", multiple=True, type=click.Path(exists=True), help="Additional overlay path"
)
@click.option(
    "--add-overlay",
    "add_overlay",
    multiple=True,
    help="Add an installed overlay by object name (repeatable); "
    "its overlay dependencies are pulled in automatically",
)
@click.option(
    "--remove-overlay",
    "remove_overlay",
    multiple=True,
    help="Remove a composed overlay by object name (repeatable); "
    "the extended object is recomposed without it",
)
@click.option(
    "--overlay-order",
    "overlay_order",
    multiple=True,
    help="Explicit apply order for overlays extending one object: "
    "comma-separated overlay names, first applied first "
    "(later entries win file conflicts). Overrides authored "
    "precedence for this workspace; repeatable per base.",
)
@click.option(
    "--attributions",
    "attributions",
    type=click.Choice(["workspace", "object", "off"]),
    default=None,
    help="Change where composed overlays' metadata is recorded; "
    "existing attribution records are migrated",
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def update_command(
    name_or_path: str,
    overlay: tuple[str, ...],
    add_overlay: tuple[str, ...],
    remove_overlay: tuple[str, ...],
    overlay_order: tuple[str, ...],
    attributions: str | None,
    as_json: bool,
) -> None:
    """Update an existing workspace.

    Re-syncs symlinks and applies any new overlays.  With --add-overlay /
    --remove-overlay / --overlay-order / --attributions, changes the
    workspace's composed overlay set, apply order, or attribution mode:
    affected extended objects are recomposed and attribution records
    migrated.
    """
    workspace_path, meta = _load_workspace(
        name_or_path, as_json, "Missing workspace.json metadata file."
    )

    if add_overlay or remove_overlay or overlay_order or attributions_change(meta, attributions):
        change = OverlayChange(
            add=add_overlay,
            remove=remove_overlay,
            order=overlay_order,
            attributions=attributions,
        )
        _update_overlays(workspace_path, meta, change, as_json)
        return

    plan = plan_resync(workspace_path, meta, overlay)
    with _spinner(disable=as_json) as progress:
        task = progress.add_task("Updating workspace...", total=None)
        apply_resync(workspace_path, plan, workspace_cls=Workspace)
        progress.update(task, description="Done")

    if as_json:
        from o3de_cli.core.json_output import emit_response

        emit_response(
            data={
                "action": "updated",
                "workspace": str(workspace_path),
                "overlays": len(plan.overlays),
            }
        )
    else:
        console.print(f"[green]Updated workspace:[/green] {workspace_path}")


def _update_overlays(
    workspace_path: Path, meta: WorkspaceMeta, change: OverlayChange, as_json: bool
) -> None:
    """``workspace update`` with an overlay set / order / attribution change."""

    def _on_download(ov_name: str) -> None:
        if not as_json:
            console.print(f"[dim]Downloading remote overlay: {ov_name}[/dim]")

    try:
        result = change_overlays(
            workspace_path,
            meta,
            change,
            resolver_cls=Resolver,
            install_overlay=_install_remote_overlay,
            on_download=_on_download,
        )
    except WorkspaceOpError as e:
        _exit_with_error(as_json, e.message, e.code)
    _write_cmake_manifest(workspace_path, meta)
    _render_overlay_change(as_json, workspace_path, change.attributions, result)


def _render_overlay_change(
    as_json: bool,
    workspace_path: Path,
    attributions: str | None,
    result: OverlayChangeResult,
) -> None:
    if as_json:
        from o3de_cli.core.json_output import emit_response

        emit_response(
            data={
                "action": "overlays-updated",
                "workspace": str(workspace_path),
                "added": sorted(result.added),
                "removed": sorted(result.removed),
                "reordered": sorted(result.ordered_bases),
                "attributions": attributions if result.attributions_changed else None,
                "recomposed": result.recomposed,
                "skipped": result.skipped,
                "overlays": sorted(result.overlays),
            }
        )
        return

    console.print(f"[green]Updated overlays:[/green] {workspace_path}")
    if result.added:
        console.print(f"  Added: {', '.join(sorted(result.added))}")
    if result.removed:
        console.print(f"  Removed: {', '.join(sorted(result.removed))}")
    if result.attributions_changed:
        console.print(f"  Attributions: {result.old_attributions} → {attributions}")
    for base in sorted(result.ordered_bases):
        console.print(f"  Order for {base}: {' → '.join(result.order_map[base])}")
    console.print(f"  Recomposed: {', '.join(result.recomposed) or '(none)'}")
    if result.skipped:
        console.print(
            f"[yellow]  Skipped (base not in workspace): {', '.join(result.skipped)}[/yellow]"
        )
    console.print(f"  Overlays now: {len(result.overlays)}")


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

    workspaces: list[dict[str, Any]] = []
    for ws_dir in ws_dirs:
        meta = _read_workspace_meta(ws_dir)
        if meta is not None:
            src = meta.sources
            n_sources = len(src.engines) + len(src.projects) + len(src.gems) + len(src.templates)
            n_overlays = len(src.overlays)
            workspaces.append(
                {
                    "name": meta.workspace.name or ws_dir.name,
                    "path": str(ws_dir),
                    "sources": n_sources,
                    "overlays": n_overlays,
                    "created": meta.created,
                }
            )

    if as_json:
        _emit_json_document(workspaces)
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
    ws_path, meta = _load_workspace(name_or_path, as_json)

    if as_json:
        _emit_json_document(meta.model_dump(by_alias=True, exclude_none=True))
    else:
        console.print(f"[bold]Workspace:[/bold] {meta.workspace.name or ws_path.name}")
        console.print(f"[dim]Path:[/dim] {ws_path}")
        console.print(f"[dim]Created:[/dim] {meta.created}")

        console.print("\n[bold]Sources:[/bold]")
        src = meta.sources
        for type_label, items in [
            ("Engines", src.engines),
            ("Projects", src.projects),
            ("Gems", src.gems),
            ("Templates", src.templates),
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

    from o3de_cli.core.json_output import emit_error, emit_response

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
            emit_response(
                data={
                    "dry_run": True,
                    "action": "delete",
                    "workspace": str(ws_path),
                }
            )
        else:
            console.print(f"[dim]Would delete:[/dim] {ws_path}")
        return

    if not force:
        # Under --json the prompt goes to stderr, keeping stdout for the document
        if not click.confirm(f"Delete workspace '{ws_path.name}'?", err=as_json):
            if as_json:
                emit_response(data={"action": "cancelled", "workspace": str(ws_path)})
            else:
                console.print("[dim]Cancelled.[/dim]")
            return

    rmtree_force(ws_path)
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
        o3de workspace solve org.o3de.engine.o3de
        o3de workspace solve org.o3de.project.myproject --include-store
    """
    from o3de_cli.core.store import Store

    with _spinner(disable=as_json) as progress:
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
        _emit_json_document(data)
        # A failed solve is an error in either mode
        if not result.is_resolved:
            raise SystemExit(1)
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
                console.print(f"    {entry.name}@{entry.version} (precedence {entry.precedence})")

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


def _run_pre_build_hooks(
    ws_path: Path,
    meta: WorkspaceMeta,
    no_hooks: bool,
    yes_hooks: bool,
    as_json: bool,
) -> None:
    """Run the pre_build hook of every object composed into the workspace.

    The script is read from the object's source tree (where it was declared
    and is checked to live) and runs in the workspace's composed copy, so
    whatever it generates lands in the workspace, not in the shared source.
    A failing hook stops the build.
    """
    from o3de_cli.core.hooks import hook_policy, run_object_hooks
    from o3de_cli.core.json_output import emit_error

    policy = hook_policy(no_hooks, yes_hooks, as_json)
    folders = (
        ("Engines", meta.sources.engines),
        ("Projects", meta.sources.projects),
        ("Gems", meta.sources.gems),
    )
    for folder, objects in folders:
        for name, src in objects.items():
            if not src:
                continue
            run_dir = _workspace_local_dir(ws_path, folder, [name])
            outcomes = run_object_hooks(
                Path(src),
                "pre_build",
                policy,
                console=None if as_json else console,
                run_dir=run_dir,
            )
            failed = [o for o in outcomes if o.status in ("failed", "refused")]
            if failed:
                msg = f"pre_build hook of {failed[0].object_name} {failed[0].status}"
                if as_json:
                    emit_error(msg, code="E_HOOK_FAILED")
                else:
                    console.print(f"[red]{msg}; not building.[/red]")
                raise SystemExit(1)


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
        if on_line is not None:
            on_line("Build cancelled.")
        else:
            console.print("\n[yellow]Build cancelled.[/yellow]")
        return -15


@workspace.command("build")
@click.argument("name_or_path")
@click.option(
    "--config",
    "-c",
    type=click.Choice(["debug", "profile", "release"]),
    default="profile",
    help="Build configuration (default: profile)",
)
@click.option(
    "--target",
    "-t",
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
    "--parallel",
    "-j",
    type=int,
    default=None,
    help="Number of parallel build jobs",
)
@click.option(
    "--generator",
    "-G",
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
@click.option("--no-hooks", is_flag=True, help="Do not run objects' pre_build hooks")
@click.option("--yes-hooks", is_flag=True, help="Run objects' pre_build hooks without asking")
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
    no_hooks: bool = False,
    yes_hooks: bool = False,
) -> None:
    """Build an O3DE workspace using CMake.

    Reads workspace metadata to locate engine, project, and gems,
    then runs CMake configure + build.

    Objects may declare a pre_build hook script. Each one is shown and you
    are asked before it runs; with --json, or without a terminal to ask on,
    hooks are skipped unless --yes-hooks is given.

    Project-centric mode (default):
        cmake -S <project> -B <project>/build/<platform> -DLY_3RDPARTY_PATH=...

    Engine-centric mode (--engine-centric):
        cmake -S <engine> -B <build_dir> -DLY_PROJECTS=<project> -DLY_3RDPARTY_PATH=...

    Examples:
        o3de workspace build my-workspace
        o3de workspace build ./workspace --config debug --target Editor
        o3de workspace build my-workspace --engine-centric --preset windows-default
    """
    ws_path, meta = _load_workspace(name_or_path, as_json)
    engine_path, project_path = build_source_paths(ws_path, meta)
    if not engine_path:
        _exit_with_error(
            as_json,
            "No engine found in workspace metadata.",
            "E_NO_ENGINE",
            "[red]No engine found in workspace metadata.[/red]",
            "[dim]Hint: Re-create the workspace with a registered engine.[/dim]",
        )

    if not project_path and not engine_centric:
        engine_centric = True
        if not as_json:
            console.print(
                "[yellow]No project found in workspace — "
                "switching to engine-centric build.[/yellow]"
            )

    # Gem-rooted workspace with no explicit target: build the gem.  The
    # engine-centric configure registers the composed gem via the
    # workspace-scoped manifest, so its module target exists.
    if not target:
        gem_target = root_gem_target(meta)
        if gem_target is not None:
            target = (gem_target,)
            if not as_json:
                console.print(
                    f"[dim]Gem-rooted workspace — defaulting build target to {gem_target}[/dim]"
                )

    # Third-party path via the K6 resolution chain, generator per K5
    if third_party_path:
        tp_path: Path | None = Path(third_party_path)
    else:
        tp_path = _find_third_party_path(meta=meta, engine_path=engine_path)
    opts = BuildOptions(
        config=config,
        targets=target,
        configure_only=configure_only,
        reconfigure=reconfigure,
        preset=preset,
        parallel=parallel,
        generator=_select_generator(generator),
        third_party=tp_path,
    )
    platform_dir = _PLATFORM_BUILD_DIR.get(sys.platform, sys.platform)
    layout = build_layout(ws_path, engine_path, project_path, engine_centric, platform_dir)

    if not as_json:
        _print_build_header(meta, layout, opts)
    # Presets and pre_build hooks could change anything: not on --dry-run
    if not dry_run:
        _prepare_build(ws_path, meta, layout, no_hooks, yes_hooks, as_json)

    configure_cmd, build_cmd = plan_cmake_commands(ws_path, layout, opts)
    if dry_run:
        _render_build_dry_run(as_json, meta, layout, opts, configure_cmd, build_cmd)
        return
    _run_build_steps(as_json, meta, layout, opts, configure_cmd, build_cmd)


def _print_build_header(meta: WorkspaceMeta, layout: BuildLayout, opts: BuildOptions) -> None:
    console.print(f"[bold]Building workspace:[/bold] {meta.workspace.name}")
    console.print(f"  Mode: {layout.mode}")
    console.print(f"  Config: {opts.config}")
    console.print(f"  Source: {layout.source_dir}")
    console.print(f"  Build dir: {layout.build_dir}")
    if opts.generator:
        console.print(f"  Generator: {opts.generator}")
    if opts.third_party:
        console.print(f"  3rd-party: {opts.third_party}")


def _prepare_build(
    ws_path: Path,
    meta: WorkspaceMeta,
    layout: BuildLayout,
    no_hooks: bool,
    yes_hooks: bool,
    as_json: bool,
) -> None:
    """K2: point the project's CMakePresets.json at the engine; run pre_build hooks."""
    if not layout.engine_centric and layout.project_path:
        if _ensure_project_cmake_presets(layout.project_path, layout.engine_path):
            if not as_json:
                console.print("[dim]Updated project CMakePresets.json with engine include[/dim]")
    _run_pre_build_hooks(ws_path, meta, no_hooks, yes_hooks, as_json)


def _render_build_dry_run(
    as_json: bool,
    meta: WorkspaceMeta,
    layout: BuildLayout,
    opts: BuildOptions,
    configure_cmd: list[str] | None,
    build_cmd: list[str] | None,
) -> None:
    commands = [c for c in (configure_cmd, build_cmd) if c]
    if as_json:
        from o3de_cli.core.json_output import emit_response

        emit_response(
            data={
                "workspace": meta.workspace.name,
                "mode": layout.mode,
                "config": opts.config,
                "source_dir": str(layout.source_dir),
                "build_dir": str(layout.build_dir),
                "generator": opts.generator,
                "third_party_path": str(opts.third_party) if opts.third_party else None,
                "commands": [" ".join(c) for c in commands],
                "dry_run": True,
            }
        )
    else:
        console.print("\n[bold yellow]Dry run — commands that would be executed:[/bold yellow]")
        for cmd in commands:
            console.print(f"  [dim]$ {' '.join(cmd)}[/dim]")


def _print_to_stderr(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


def _run_build_steps(
    as_json: bool,
    meta: WorkspaceMeta,
    layout: BuildLayout,
    opts: BuildOptions,
    configure_cmd: list[str] | None,
    build_cmd: list[str] | None,
) -> None:
    """Run CMake configure (if planned) and build; exit with CMake's code on failure.

    Under ``--json`` CMake's output is streamed to stderr, so stdout
    carries only the JSON document.
    """
    from o3de_cli.core.json_output import emit_response

    on_line = _print_to_stderr if as_json else None
    if configure_cmd:
        if not as_json:
            console.print("\n[bold]Configuring...[/bold]")
            console.print(f"[dim]$ {' '.join(configure_cmd)}[/dim]")
        rc = _run_cmake(configure_cmd, cwd=layout.source_dir, on_line=on_line)
        if rc != 0:
            _exit_with_error(as_json, "CMake configure failed.", "E_CONFIGURE_FAILED", exit_code=rc)
        if not as_json:
            console.print("[green]Configure complete.[/green]")

    if opts.configure_only:
        if as_json:
            emit_response(
                data={
                    "workspace": meta.workspace.name,
                    "phase": "configure",
                    "status": "complete",
                }
            )
        return

    assert build_cmd is not None
    if not as_json:
        console.print("\n[bold]Building...[/bold]")
        console.print(f"[dim]$ {' '.join(build_cmd)}[/dim]")
    rc = _run_cmake(build_cmd, cwd=layout.source_dir, on_line=on_line)
    if rc != 0:
        _exit_with_error(as_json, "Build failed.", "E_BUILD_FAILED", exit_code=rc)

    cmake_config = CMAKE_CONFIGS[opts.config]
    if as_json:
        emit_response(
            data={
                "workspace": meta.workspace.name,
                "mode": layout.mode,
                "config": cmake_config,
                "source_dir": str(layout.source_dir),
                "build_dir": str(layout.build_dir),
            }
        )
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
        o3de workspace lock my-workspace
    """
    from o3de_cli.core.json_output import emit_error, emit_response
    from o3de_cli.core.lockfile import generate_lockfile, read_lockfile

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
        assert lockdata is not None  # generate_lockfile just wrote it
        emit_response(
            data={
                "lockfile": str(lockfile_path),
                "packages": len(lockdata.get("packages", {})),
                "contentHash": lockdata.get("contentHash", ""),
            }
        )
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
    from o3de_cli.core.json_output import emit_error
    from o3de_cli.core.lockfile import verify_lockfile

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
        _emit_json_document({"verified": matches, "mismatches": mismatches})
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
        "engines": "engine",
        "projects": "project",
        "gems": "gem",
        "templates": "template",
        "overlays": "overlay",
    }
    result: dict[str, dict] = {}
    for type_key, items in sources.items():
        if isinstance(items, dict):
            obj_type = _type_map.get(type_key, type_key)
            for name, path in items.items():
                result[name] = {"path": path, "object_type": obj_type, "version": "0.0.0"}
    return result
