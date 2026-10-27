# O3DE CLI - Object Commands (per-gem build/install)
# SPDX-License-Identifier: Apache-2.0 OR MIT

"""Build and install individual objects (gems) as binary packages.

``o3de object build <name> --workspace <ws>``
    Build only the gem's CMake targets inside the workspace build tree.

``o3de object install <name> --workspace <ws>``
    Build (unless ``--skip-build``) and assemble a binary install layout
    with a generated ``<name>Config.cmake`` under
    ``~/.o3de/BuiltPackages/<name>-<version>/`` — consumable via
    ``o3de workspace override <ws> <name> --artifact local-binary``.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import NoReturn

import click
from rich.console import Console

from o3de_cli.core.cmake_manifest import CMAKE_MANIFEST_FILENAME
from o3de_cli.core.gem_package import (
    BUILDABLE_TYPES,
    TargetInfo,
    ensure_codemodel,
    filter_gem_targets,
    install_gem_package,
    load_codemodel_targets,
)
from o3de_cli.core.hoist import (
    HoistPlan,
    HoistResult,
    execute_hoist,
    locate_source,
    plan_hoist,
    register_family_repo,
    require_filter_repo,
    run_git,
)
from o3de_cli.core.split_platforms import (
    ObjectOpError,
    SplitPlan,
    create_overlays,
    ensure_manifest,
    find_platform_dirs,
    load_object_json,
    plan_split,
    register_overlays,
    remove_split_dirs,
    split_overlay_name,
)

console = Console()

_CMAKE_CONFIG = {"debug": "Debug", "profile": "Profile", "release": "Release"}


@click.group(name="object")
def object_group() -> None:
    """Build and install individual objects (gems) as binary packages."""
    pass


def _fail(msg: str, code: str, as_json: bool) -> NoReturn:
    from o3de_cli.core.json_output import emit_error

    if as_json:
        emit_error(msg, code=code)
    else:
        console.print(f"[red]{msg}[/red]")
    raise SystemExit(1)


def _locate_gem(ws_path: Path, object_name: str, as_json: bool) -> tuple[Path, str, str]:
    """Find a gem in the workspace's resolved manifest.

    Returns (gem_dir, canonical_name, version).
    """
    manifest_path = ws_path / CMAKE_MANIFEST_FILENAME
    if not manifest_path.exists():
        _fail(
            f"No {CMAKE_MANIFEST_FILENAME} in workspace: {ws_path}",
            "E_WS_NO_MANIFEST",
            as_json,
        )
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)

    names = manifest.get("all_gem_names", [])
    paths = manifest.get("all_gem_paths", [])
    for name_ver, json_path in zip(names, paths):
        name, _, version = name_ver.partition("==")
        gem_dir = Path(json_path).parent
        if object_name in (name, gem_dir.name):
            return gem_dir, name, version

    _fail(
        f"Gem '{object_name}' not found in workspace manifest ({len(names)} gems present).",
        "E_OBJECT_NOT_FOUND",
        as_json,
    )
    raise AssertionError  # unreachable


def _discover_targets(
    ws_path: Path,
    gem_dir: Path,
    config: str,
    as_json: bool,
    reconfigure: bool = False,
) -> tuple[Path, list[TargetInfo]]:
    """Locate the build dir and the gem's targets via the CMake File API."""
    from o3de_cli.commands.workspace import _PLATFORM_BUILD_DIR

    platform_dir = _PLATFORM_BUILD_DIR.get(sys.platform, sys.platform)
    build_dir = ws_path / "build" / platform_dir
    if not (build_dir / "CMakeCache.txt").exists():
        _fail(
            f"Workspace not configured (no CMakeCache.txt in {build_dir}). "
            f"Run: o3de workspace build {ws_path} --configure-only",
            "E_NOT_CONFIGURED",
            as_json,
        )

    def on_progress(msg: str) -> None:
        if not as_json:
            console.print(f"[dim]{msg}[/dim]")

    try:
        codemodel = ensure_codemodel(build_dir, on_progress, force=reconfigure)
    except RuntimeError as e:
        _fail(str(e), "E_CONFIGURE_FAILED", as_json)
    if codemodel is None:
        _fail(
            "Could not obtain a CMake File API codemodel reply.",
            "E_NO_CODEMODEL",
            as_json,
        )

    all_targets = load_codemodel_targets(codemodel, config)
    gem_targets = filter_gem_targets(all_targets, gem_dir)
    if not gem_targets:
        _fail(
            f"No CMake targets found under {gem_dir}. "
            "Is the gem enabled in the workspace build (artifact=source)?",
            "E_NO_TARGETS",
            as_json,
        )
    return build_dir, gem_targets


def _buildable(targets: list[TargetInfo]) -> list[TargetInfo]:
    return [t for t in targets if t.type in BUILDABLE_TYPES and ".Tests" not in t.name]


def _run_gem_build(
    build_dir: Path,
    targets: list[TargetInfo],
    config: str,
    as_json: bool,
) -> None:
    from o3de_cli.commands.workspace import _run_cmake

    names = [t.name for t in targets]
    cmd = [
        "cmake",
        "--build",
        str(build_dir),
        "--config",
        _CMAKE_CONFIG[config],
        "--target",
        *names,
        "--parallel",
    ]
    if not as_json:
        console.print(f"[dim]$ {' '.join(cmd)}[/dim]")
    rc = _run_cmake(cmd, cwd=build_dir)
    if rc != 0:
        _fail("Gem build failed.", "E_BUILD_FAILED", as_json)


@object_group.command("build")
@click.argument("object_name")
@click.option(
    "--workspace",
    "-w",
    "workspace_arg",
    required=True,
    help="Workspace name or path containing the gem",
)
@click.option(
    "--config",
    "-c",
    type=click.Choice(["debug", "profile", "release"]),
    default="profile",
    show_default=True,
    help="Build configuration",
)
@click.option(
    "--reconfigure", is_flag=True, help="Force a CMake reconfigure before target discovery"
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def build_command(
    object_name: str,
    workspace_arg: str,
    config: str,
    reconfigure: bool,
    as_json: bool,
) -> None:
    """Build a single gem's targets inside a workspace build tree.

    Example:
        o3de object build org.o3de.gem.stars -w F:/myWorkspace
    """
    from o3de_cli.commands.workspace import _resolve_workspace_path
    from o3de_cli.core.json_output import emit_response

    ws_path = _resolve_workspace_path(workspace_arg)
    if ws_path is None:
        _fail(f"Workspace not found: {workspace_arg}", "E_WS_NOT_FOUND", as_json)

    gem_dir, canonical, version = _locate_gem(ws_path, object_name, as_json)
    build_dir, gem_targets = _discover_targets(
        ws_path,
        gem_dir,
        config,
        as_json,
        reconfigure,
    )
    targets = _buildable(gem_targets)
    if not targets:
        _fail(
            f"Gem '{canonical}' has no buildable targets "
            "(it may be consumed as a prebuilt package — clear the override first).",
            "E_NO_TARGETS",
            as_json,
        )

    if not as_json:
        console.print(f"[bold]Building gem:[/bold] {canonical}=={version} ({config})")
        console.print(f"  Targets: {', '.join(t.name for t in targets)}")

    _run_gem_build(build_dir, targets, config, as_json)

    if as_json:
        emit_response(
            data={
                "object": canonical,
                "version": version,
                "config": config,
                "targets": [t.name for t in targets],
                "build_dir": str(build_dir),
            }
        )
    else:
        console.print(f"[green]Built {len(targets)} targets.[/green]")


@object_group.command("install")
@click.argument("object_name")
@click.option(
    "--workspace",
    "-w",
    "workspace_arg",
    required=True,
    help="Workspace name or path containing the gem",
)
@click.option(
    "--config",
    "-c",
    type=click.Choice(["debug", "profile", "release"]),
    default="profile",
    show_default=True,
    help="Configuration to build and package",
)
@click.option(
    "--skip-build", is_flag=True, help="Package existing build outputs without rebuilding"
)
@click.option(
    "--reconfigure", is_flag=True, help="Force a CMake reconfigure before target discovery"
)
@click.option("--force", "-f", is_flag=True, help="Overwrite an existing installed package")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def install_command(
    object_name: str,
    workspace_arg: str,
    config: str,
    skip_build: bool,
    reconfigure: bool,
    force: bool,
    as_json: bool,
) -> None:
    """Build a gem and install it as a binary package.

    Produces ~/.o3de/BuiltPackages/<name>-<version>/ with the gem's
    binaries, data, headers, and a generated <name>Config.cmake.
    Consume it with:

        o3de workspace override <ws> <name> --artifact local-binary

    Example:
        o3de object install org.o3de.gem.stars -w F:/myWorkspace
    """
    from o3de_cli.commands.workspace import _resolve_workspace_path
    from o3de_cli.core.json_output import emit_response

    ws_path = _resolve_workspace_path(workspace_arg)
    if ws_path is None:
        _fail(f"Workspace not found: {workspace_arg}", "E_WS_NOT_FOUND", as_json)

    gem_dir, canonical, version = _locate_gem(ws_path, object_name, as_json)
    build_dir, gem_targets = _discover_targets(
        ws_path,
        gem_dir,
        config,
        as_json,
        reconfigure,
    )
    targets = _buildable(gem_targets)
    if not targets:
        _fail(
            f"Gem '{canonical}' has no buildable targets "
            "(it may be consumed as a prebuilt package — clear the override first).",
            "E_NO_TARGETS",
            as_json,
        )

    if not as_json:
        console.print(f"[bold]Installing gem:[/bold] {canonical}=={version} ({config})")
        console.print(f"  Targets: {', '.join(t.name for t in targets)}")

    if not skip_build:
        _run_gem_build(build_dir, targets, config, as_json)

    def on_progress(msg: str) -> None:
        if not as_json:
            console.print(f"[dim]{msg}[/dim]")

    # Package all gem targets (interface targets contribute headers/aliases)
    try:
        dest = install_gem_package(
            gem_dir=gem_dir,
            canonical_name=canonical,
            version=version,
            targets=gem_targets,
            config=config,
            force=force,
            on_progress=on_progress,
        )
    except FileExistsError as e:
        _fail(
            f"Package already installed: {e} (use --force to overwrite)",
            "E_ALREADY_INSTALLED",
            as_json,
        )
    except FileNotFoundError as e:
        _fail(str(e), "E_ARTIFACT_MISSING", as_json)

    if as_json:
        emit_response(
            data={
                "object": canonical,
                "version": version,
                "config": config,
                "package_dir": str(dest),
                "targets": [t.name for t in targets],
            }
        )
    else:
        console.print(f"[green]Installed:[/green] {dest}")
        console.print(
            f"[dim]Consume with: o3de workspace override <ws> {canonical} "
            f"--version {version} --artifact local-binary[/dim]"
        )


@object_group.command("package")
@click.argument("object_name")
@click.option(
    "--workspace",
    "-w",
    "workspace_arg",
    required=True,
    help="Workspace name or path containing the gem",
)
@click.option(
    "--config",
    "-c",
    type=click.Choice(["debug", "profile", "release"]),
    default="release",
    show_default=True,
    help="Configuration to build and package",
)
@click.option(
    "--output",
    "-o",
    "output_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Directory for the release zip [default: current directory]",
)
@click.option(
    "--skip-build", is_flag=True, help="Package existing build outputs without rebuilding"
)
@click.option(
    "--reconfigure", is_flag=True, help="Force a CMake reconfigure before target discovery"
)
@click.option(
    "--code",
    "code_release",
    is_flag=True,
    help="Produce a source (code) release archive instead of a binary "
    "release — no build, platform-independent",
)
@click.option(
    "--format",
    "archive_format",
    type=click.Choice(["zip", "tar.gz"]),
    default="zip",
    show_default=True,
    help="Archive format (code releases only)",
)
@click.option(
    "--update-manifest",
    is_flag=True,
    help="Record the release archive (file:// URL + sha256) in the gem's "
    "manifest releases[] — replace the URL after uploading",
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def package_command(
    object_name: str,
    workspace_arg: str,
    config: str,
    output_dir: Path | None,
    skip_build: bool,
    reconfigure: bool,
    code_release: bool,
    archive_format: str,
    update_manifest: bool,
    as_json: bool,
) -> None:
    """Build a gem and produce a distributable release archive.

    By default builds the gem (release config), assembles the binary
    install layout, and zips it to <name>-<version>-<Platform>.zip
    alongside its SHA-256 — the exact shape consumed by
    `workspace override --artifact remote-binary` after the archive
    URL is advertised in the gem's releases[].binaries.

    With --code, archives the gem's source tree instead (no build) to
    <name>-<version>-Source.zip/.tar.gz, advertised under
    releases[].downloads as {source, source_sha256}.

    Example:
        o3de object package org.o3de.gem.stars -w F:/myWorkspace -o dist/
        o3de object package org.o3de.gem.stars -w F:/myWorkspace --code
    """
    from o3de_cli.commands.workspace import _resolve_workspace_path
    from o3de_cli.core.gem_package import (
        install_gem_package,
        package_gem_archive,
        package_gem_source_archive,
        update_release_manifest,
        update_release_manifest_source,
    )
    from o3de_cli.core.json_output import emit_response

    ws_path = _resolve_workspace_path(workspace_arg)
    if ws_path is None:
        _fail(f"Workspace not found: {workspace_arg}", "E_WS_NOT_FOUND", as_json)

    gem_dir, canonical, version = _locate_gem(ws_path, object_name, as_json)

    # ------------------------------------------------------------------
    # Code (source) release: archive the source tree — no build needed
    # ------------------------------------------------------------------
    if code_release:
        if not as_json:
            console.print(f"[bold]Packaging gem source:[/bold] {canonical}=={version}")
        out = output_dir if output_dir is not None else Path.cwd()
        archive_path, sha256 = package_gem_source_archive(
            gem_dir,
            canonical,
            version,
            out,
            fmt=archive_format,
        )

        manifest_path = None
        if update_manifest:
            manifest_path = update_release_manifest_source(
                gem_dir,
                version,
                archive_path.resolve().as_uri(),
                sha256,
            )

        if as_json:
            emit_response(
                data={
                    "object": canonical,
                    "version": version,
                    "release": "code",
                    "archive": str(archive_path),
                    "sha256": sha256,
                    "manifest": str(manifest_path) if manifest_path else None,
                }
            )
        else:
            console.print(f"[green]Source release archive:[/green] {archive_path}")
            console.print(f"  sha256: {sha256}")
            if manifest_path:
                console.print(f"  Manifest updated: {manifest_path}")
                console.print(
                    "[dim]Replace the file:// URL with the hosted archive URL after uploading[/dim]"
                )
            else:
                console.print(
                    "[dim]Advertise in the gem manifest under releases[] as:\n"
                    f'  {{"name": "{version}", "downloads": [{{"source": '
                    f'"<url>", "source_sha256": "{sha256}"}}]}}[/dim]'
                )
        return

    build_dir, gem_targets = _discover_targets(
        ws_path,
        gem_dir,
        config,
        as_json,
        reconfigure,
    )
    targets = _buildable(gem_targets)
    if not targets:
        _fail(
            f"Gem '{canonical}' has no buildable targets "
            "(it may be consumed as a prebuilt package — clear the override first).",
            "E_NO_TARGETS",
            as_json,
        )

    if not as_json:
        console.print(f"[bold]Packaging gem:[/bold] {canonical}=={version} ({config})")
        console.print(f"  Targets: {', '.join(t.name for t in targets)}")

    if not skip_build:
        _run_gem_build(build_dir, targets, config, as_json)

    def on_progress(msg: str) -> None:
        if not as_json:
            console.print(f"[dim]{msg}[/dim]")

    # Stage the install layout (also usable directly as local-binary)
    try:
        staged = install_gem_package(
            gem_dir=gem_dir,
            canonical_name=canonical,
            version=version,
            targets=gem_targets,
            config=config,
            force=True,
            on_progress=on_progress,
        )
    except FileNotFoundError as e:
        _fail(str(e), "E_ARTIFACT_MISSING", as_json)

    out = output_dir if output_dir is not None else Path.cwd()
    zip_path, sha256 = package_gem_archive(
        staged,
        canonical,
        version,
        out,
    )
    on_progress(f"Archived {zip_path.name}")

    manifest_path = None
    if update_manifest:
        manifest_path = update_release_manifest(
            gem_dir,
            version,
            zip_path.resolve().as_uri(),
            sha256,
        )
        on_progress(f"Updated releases[] in {manifest_path}")

    if as_json:
        emit_response(
            data={
                "object": canonical,
                "version": version,
                "config": config,
                "archive": str(zip_path),
                "sha256": sha256,
                "package_dir": str(staged),
                "manifest": str(manifest_path) if manifest_path else None,
            }
        )
    else:
        console.print(f"[green]Release archive:[/green] {zip_path}")
        console.print(f"  sha256: {sha256}")
        if manifest_path:
            console.print(f"  Manifest updated: {manifest_path}")
            console.print(
                "[dim]Replace the file:// URL with the hosted archive URL after uploading[/dim]"
            )
        else:
            console.print(
                "[dim]Advertise in the gem manifest under releases[] as:\n"
                f'  {{"name": "{version}", "binaries": [{{"platform": '
                f'"<Platform>", "binary": "<url>", "sha256": "{sha256}"}}]}}[/dim]'
            )


# ── split-platforms ─────────────────────────────────────────────────

# Historical private names, kept importable from this module.
_find_platform_dirs = find_platform_dirs
_split_overlay_name = split_overlay_name


@contextmanager
def _op_errors(as_json: bool) -> Iterator[None]:
    """Report an :class:`ObjectOpError` raised in the block as a CLI failure."""
    try:
        yield
    except ObjectOpError as e:
        _fail(str(e), e.code, as_json)


def _load_object_or_fail(obj_root: Path, as_json: bool) -> tuple[dict, str]:
    try:
        return load_object_json(obj_root)
    except FileNotFoundError as e:
        _fail(str(e), "E_NOT_AN_OBJECT", as_json)
    except ObjectOpError as e:  # E_INVALID_OBJECT: unparsable object JSON
        _fail(str(e), e.code, as_json)


def _ensure_manifest_for_register(as_json: bool) -> None:
    """Create the user manifest for ``--register`` when there is none yet."""
    created = ensure_manifest()
    if created and not as_json:
        console.print(f"[dim]Created manifest: {created}[/dim]")


def _print_warnings(warnings: list[str]) -> None:
    for warning in warnings:
        console.print(f"[yellow]Warning:[/yellow] {warning}")


@object_group.command("split-platforms")
@click.argument("object_path", type=click.Path(exists=True, file_okay=False))
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    help="Directory to create the overlay objects in (default: the object's parent directory)",
)
@click.option(
    "--platforms",
    "-P",
    "platforms_opt",
    multiple=True,
    help="Only split these platforms (repeatable or comma-separated; default: all detected)",
)
@click.option(
    "--overlay-version",
    "-V",
    "overlay_version",
    default=None,
    help="Version for the created overlays (default: the base object's version)",
)
@click.option(
    "--tags", "-T", "tags_opt", multiple=True, help="user_tags to stamp on the created overlays"
)
@click.option(
    "--precedence",
    type=int,
    default=10,
    show_default=True,
    help="Precedence for platform overlays (common overlay is 0)",
)
@click.option(
    "--remove",
    "do_remove",
    is_flag=True,
    help="Remove the split platform directories from the source object after creating the overlays",
)
@click.option(
    "--register", "do_register", is_flag=True, help="Register the created overlays in the manifest"
)
@click.option("--dry-run", is_flag=True, help="Only report what would be split")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def split_platforms_command(
    object_path: str,
    output: str | None,
    platforms_opt: tuple[str, ...],
    overlay_version: str | None,
    tags_opt: tuple[str, ...],
    precedence: int,
    do_remove: bool,
    do_register: bool,
    dry_run: bool,
    as_json: bool,
) -> None:
    """Split an object's PAL platform directories into overlay objects.

    Scans OBJECT_PATH for ``Platform/<Name>`` directories and creates
    one overlay object per platform, each carrying that platform's
    files as an ``Overlay/`` payload that composes back onto the base
    object at workspace compose time.  ``Platform/Common`` becomes a
    shared family overlay that the platform overlays depend on.

    Example:
        o3de object split-platforms ./Gems/MyGem -o ./overlays --remove
    """
    obj_root = Path(object_path).resolve()
    obj_data, type_token = _load_object_or_fail(obj_root, as_json)

    with _op_errors(as_json):
        plan = plan_split(
            obj_root,
            obj_data,
            type_token,
            platforms=platforms_opt,
            output=output,
            overlay_version=overlay_version,
            tags=tags_opt,
            precedence=precedence,
        )
    if dry_run:
        _render_split_plan(plan, as_json)
        return

    with _op_errors(as_json):
        created = create_overlays(plan)
    removed = remove_split_dirs(plan) if do_remove else []
    kept = plan.kept_common_dirs() if do_remove else []
    registered: list[str] = []
    if do_register:
        _ensure_manifest_for_register(as_json)
        registered = register_overlays(created)
    _render_split_result(plan, created, removed, kept, registered, as_json)


def _render_split_plan(plan: SplitPlan, as_json: bool) -> None:
    from o3de_cli.core.json_output import emit_response

    if as_json:
        emit_response(data=plan.dry_run_payload(), warnings=plan.warnings)
        return
    console.print(f"[bold]Would split {plan.base_name}:[/bold]")
    for ov in plan.overlays:
        console.print(f"  {ov.platform:<12} → {ov.name} ({ov.files} files)")
    _print_warnings(plan.warnings)


def _render_split_result(
    plan: SplitPlan,
    created: list[dict],
    removed: list[str],
    kept: list[str],
    registered: list[str],
    as_json: bool,
) -> None:
    from o3de_cli.core.json_output import emit_response

    if as_json:
        emit_response(
            data={
                "object": plan.base_name,
                "version": plan.base_version,
                "overlays": created,
                "removed": removed,
                "kept": kept,
                "registered": registered,
            },
            warnings=plan.warnings,
        )
        return
    console.print(f"[green]Split {plan.base_name}:[/green]")
    for entry in created:
        console.print(f"  {entry['platform']:<12} → {entry['path']}")
    if removed:
        console.print(f"[yellow]Removed from source:[/yellow] {', '.join(removed)}")
    if kept:
        console.print(
            f"[yellow]Kept in source:[/yellow] {', '.join(kept)} "
            f"(still used by {', '.join(plan.remaining_platforms)})"
        )
    _print_warnings(plan.warnings)
    if registered:
        console.print(f"[dim]Registered: {', '.join(registered)}[/dim]")


# ── hoist ───────────────────────────────────────────────────────────

# Historical private name, kept importable from this module.
_run_git = run_git


@object_group.command("hoist")
@click.argument("object_path", type=click.Path(exists=True, file_okay=False))
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    help="Path for the new family repo (default: sibling of the "
    "source git repo, named o3de-<family>)",
)
@click.option(
    "--repo-name",
    default=None,
    help="Family repo object name (default: derived, e.g. org.o3de.repo.<family>)",
)
@click.option(
    "--branch",
    default=None,
    help="Branch to hoist from (default: the source repo's current branch)",
)
@click.option(
    "--overlay-version",
    "-V",
    "overlay_version",
    default=None,
    help="Version for the created overlays (default: the base object's version)",
)
@click.option(
    "--tags", "-T", "tags_opt", multiple=True, help="user_tags to stamp on the created overlays"
)
@click.option(
    "--precedence",
    type=int,
    default=10,
    show_default=True,
    help="Precedence for platform overlays (common overlay is 0)",
)
@click.option(
    "--register", "do_register", is_flag=True, help="Register the family repo in the manifest"
)
@click.option("--dry-run", is_flag=True, help="Only report what would be hoisted")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def hoist_command(
    object_path: str,
    output: str | None,
    repo_name: str | None,
    branch: str | None,
    overlay_version: str | None,
    tags_opt: tuple[str, ...],
    precedence: int,
    do_register: bool,
    dry_run: bool,
    as_json: bool,
) -> None:
    """Hoist a gem out of its engine repo into a family repo, with history.

    Uses git filter-repo to project the gem's full git history into a
    new repo-rooted family repository:

    \b
        o3de-<family>/
          repo.json                      ← repo object (children below)
          Gems/<Gem>/                    ← gem history, platform dirs removed
          Overlays/<family>.<plat>/      ← per-platform overlay objects,
            Overlay/...                    platform file history preserved

    The split is deterministic: re-running it after upstream merges
    produces the same historical commits, so the family repo can fetch
    and merge upstream changes indefinitely.

    Example:
        o3de object hoist ./o3de/Gems/Achievements --register
    """
    obj_root = Path(object_path).resolve()
    obj_data, type_token = _load_object_or_fail(obj_root, as_json)

    with _op_errors(as_json):
        source = locate_source(obj_root, branch)
        require_filter_repo()
        plan = plan_hoist(
            source,
            obj_data,
            type_token,
            output=output,
            repo_name=repo_name,
            overlay_version=overlay_version,
            tags=tags_opt,
            precedence=precedence,
        )
    if dry_run:
        _render_hoist_plan(plan, as_json)
        return

    def on_progress(msg: str) -> None:
        console.print(f"[dim]{msg}[/dim]")

    with _op_errors(as_json):
        result = execute_hoist(plan, on_progress=None if as_json else on_progress)
    registered = False
    if do_register:
        _ensure_manifest_for_register(as_json)
        registered = register_family_repo(plan.out_dir)
    _render_hoist_result(plan, result, registered, as_json)


def _render_hoist_plan(plan: HoistPlan, as_json: bool) -> None:
    from o3de_cli.core.json_output import emit_response

    if as_json:
        emit_response(data=plan.dry_run_payload())
        return
    console.print(f"[bold]Would hoist {plan.base_name}:[/bold]")
    console.print(f"  Family repo: {plan.repo_name} → {plan.out_dir}")
    console.print(f"  Branch: {plan.source.branch}")
    console.print(f"  Gem: {plan.source.gem_rel} → {plan.gem_target}")
    for src, dst in plan.renames:
        console.print(f"  {src} → {dst}")
    for ov in plan.skipped:
        console.print(
            f"  [yellow]Skipped:[/yellow] Overlays/{ov.dir_name} ({ov.platform}): {ov.skip_reason}"
        )


def _render_hoist_result(
    plan: HoistPlan, result: HoistResult, registered: bool, as_json: bool
) -> None:
    from o3de_cli.core.json_output import emit_response

    if as_json:
        emit_response(
            data={
                "object": plan.base_name,
                "repo": plan.repo_name,
                "path": str(plan.out_dir),
                "branch": plan.source.branch,
                "overlays": result.overlays,
                "registered": registered,
            },
            warnings=result.warnings,
        )
        return
    console.print(f"[green]Hoisted {plan.base_name}:[/green] {plan.out_dir}")
    console.print(f"  Repo object: {plan.repo_name}")
    console.print(f"  Gem: {plan.gem_target}")
    for ov in result.overlays:
        console.print(f"  Overlay: {ov}")
    for skipped in plan.skipped:
        console.print(f"  [yellow]Skipped:[/yellow] {skipped.name}: {skipped.skip_reason}")
    _print_warnings(result.warnings)
    if registered:
        console.print("[dim]Registered family repo in manifest[/dim]")
    console.print(
        "[dim]Upstream sync: re-run the same hoist to a scratch path "
        "after upstream merges, then fetch+merge from it[/dim]"
    )


@object_group.command("compose")
@click.option(
    "--plan",
    "plan_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Composition plan describing the families to produce",
)
@click.option(
    "--family",
    "only_families",
    multiple=True,
    help="Limit to these family names (repeatable)",
)
@click.option("--apply", "do_apply", is_flag=True, help="Generate the families")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def compose_command(
    plan_path: str,
    only_families: tuple[str, ...],
    do_apply: bool,
    as_json: bool,
) -> None:
    """Compose object family repositories from a declarative plan (preview).

    Preview: this reports what a plan would produce and writes nothing;
    --apply (generation) is not implemented yet. Unlike ``hoist``, which
    computes its filter arguments
    from the working tree and records nothing, a plan states the mapping
    explicitly and pins the source commit, so the same plan reproduces the
    same result and a family can be deleted and recreated at will.

    Example:
        o3de object compose --plan chickenator.plan.json
    """
    import json as _json

    from pydantic import ValidationError

    from o3de_cli.core.compose import ComposeError, ComposePlan, analyze
    from o3de_cli.core.json_output import emit_response

    try:
        raw = _json.loads(Path(plan_path).read_text(encoding="utf-8"))
    except (OSError, _json.JSONDecodeError) as e:
        _fail(f"Cannot read plan: {e}", "E_PLAN_UNREADABLE", as_json)
        return

    try:
        plan = ComposePlan.model_validate(raw)
    except ValidationError as e:
        _fail(f"Invalid plan:\n{e}", "E_PLAN_INVALID", as_json)
        return

    only = set(only_families) if only_families else None
    if only:
        known = {f.name for f in plan.families}
        unknown = only - known
        if unknown:
            _fail(
                f"Plan has no such families: {', '.join(sorted(unknown))}",
                "E_NO_SUCH_FAMILY",
                as_json,
            )
            return

    try:
        report = analyze(plan, only=only)
    except ComposeError as e:
        _fail(str(e), "E_COMPOSE_FAILED", as_json)
        return

    if as_json:
        emit_response(data=report.model_dump())
    else:
        console.print(f"[bold]Source:[/bold] {report.source_repo} @ {report.source_commit[:10]}")
        console.print(
            f"[bold]Families:[/bold] {len(report.families)}  "
            f"[bold]Source files:[/bold] {report.total_source_files}\n"
        )
        for fam in report.families:
            flag = "" if fam.history else " [yellow](no history)[/yellow]"
            console.print(f"[bold cyan]{fam.name}[/bold cyan]{flag}")
            console.print(
                f"  rules {len(fam.rules)}   files {fam.mapped_files}   "
                f"created {len(fam.created)}   copies {fam.copies}"
            )
            for src, dst in fam.rules:
                arrow = "->" if src != dst else "=="
                console.print(f"    {src} {arrow} {dst}")
            for created in fam.created:
                console.print(f"    [green]+[/green] {created}")
            for missing in fam.missing_sources:
                console.print(f"    [red]missing source:[/red] {missing}")
            for warning in fam.warnings:
                console.print(f"    [yellow]warning:[/yellow] {warning}")
            console.print()

        # Unmapped files are dropped: --path is an allowlist, so this is the
        # number that matters most and it goes last where it will be read.
        if report.unmapped_files:
            console.print(
                f"[yellow]{report.unmapped_files} of {report.total_source_files} "
                f"source files are not mapped by any family and would be dropped."
                f"[/yellow]"
            )
        else:
            console.print("[green]Every source file is mapped by some family.[/green]")

    if do_apply:
        _fail(
            "Generation is not implemented yet; this reports only. Re-run without --apply.",
            "E_NOT_IMPLEMENTED",
            as_json,
        )
