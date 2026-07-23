# O3DE Pilot CLI - Object Commands (per-gem build/install)
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
from pathlib import Path

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

console = Console()

_CMAKE_CONFIG = {"debug": "Debug", "profile": "Profile", "release": "Release"}


@click.group(name="object")
def object_group() -> None:
    """Build and install individual objects (gems) as binary packages."""
    pass


def _fail(msg: str, code: str, as_json: bool) -> None:
    from o3de_cli.core.json_output import emit_error

    if as_json:
        emit_error(msg, code=code)
    else:
        console.print(f"[red]{msg}[/red]")
    raise SystemExit(1)


def _locate_gem(
    ws_path: Path, object_name: str, as_json: bool
) -> tuple[Path, str, str]:
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
        f"Gem '{object_name}' not found in workspace manifest "
        f"({len(names)} gems present).",
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
        raise
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
    return [
        t for t in targets
        if t.type in BUILDABLE_TYPES and ".Tests" not in t.name
    ]


def _run_gem_build(
    build_dir: Path,
    targets: list[TargetInfo],
    config: str,
    as_json: bool,
) -> None:
    from o3de_cli.commands.workspace import _run_cmake

    names = [t.name for t in targets]
    cmd = [
        "cmake", "--build", str(build_dir),
        "--config", _CMAKE_CONFIG[config],
        "--target", *names,
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
    "--workspace", "-w", "workspace_arg", required=True,
    help="Workspace name or path containing the gem",
)
@click.option(
    "--config", "-c",
    type=click.Choice(["debug", "profile", "release"]),
    default="profile",
    show_default=True,
    help="Build configuration",
)
@click.option("--reconfigure", is_flag=True,
              help="Force a CMake reconfigure before target discovery")
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
        return

    gem_dir, canonical, version = _locate_gem(ws_path, object_name, as_json)
    build_dir, gem_targets = _discover_targets(
        ws_path, gem_dir, config, as_json, reconfigure,
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
        console.print(
            f"[bold]Building gem:[/bold] {canonical}=={version} ({config})"
        )
        console.print(f"  Targets: {', '.join(t.name for t in targets)}")

    _run_gem_build(build_dir, targets, config, as_json)

    if as_json:
        emit_response(data={
            "object": canonical,
            "version": version,
            "config": config,
            "targets": [t.name for t in targets],
            "build_dir": str(build_dir),
        })
    else:
        console.print(f"[green]Built {len(targets)} targets.[/green]")


@object_group.command("install")
@click.argument("object_name")
@click.option(
    "--workspace", "-w", "workspace_arg", required=True,
    help="Workspace name or path containing the gem",
)
@click.option(
    "--config", "-c",
    type=click.Choice(["debug", "profile", "release"]),
    default="profile",
    show_default=True,
    help="Configuration to build and package",
)
@click.option("--skip-build", is_flag=True, help="Package existing build outputs without rebuilding")
@click.option("--reconfigure", is_flag=True,
              help="Force a CMake reconfigure before target discovery")
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
        return

    gem_dir, canonical, version = _locate_gem(ws_path, object_name, as_json)
    build_dir, gem_targets = _discover_targets(
        ws_path, gem_dir, config, as_json, reconfigure,
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
        console.print(
            f"[bold]Installing gem:[/bold] {canonical}=={version} ({config})"
        )
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
        return
    except FileNotFoundError as e:
        _fail(str(e), "E_ARTIFACT_MISSING", as_json)
        return

    if as_json:
        emit_response(data={
            "object": canonical,
            "version": version,
            "config": config,
            "package_dir": str(dest),
            "targets": [t.name for t in targets],
        })
    else:
        console.print(f"[green]Installed:[/green] {dest}")
        console.print(
            f"[dim]Consume with: o3de workspace override <ws> {canonical} "
            f"--version {version} --artifact local-binary[/dim]"
        )


@object_group.command("package")
@click.argument("object_name")
@click.option(
    "--workspace", "-w", "workspace_arg", required=True,
    help="Workspace name or path containing the gem",
)
@click.option(
    "--config", "-c",
    type=click.Choice(["debug", "profile", "release"]),
    default="release",
    show_default=True,
    help="Configuration to build and package",
)
@click.option(
    "--output", "-o", "output_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Directory for the release zip [default: current directory]",
)
@click.option("--skip-build", is_flag=True, help="Package existing build outputs without rebuilding")
@click.option("--reconfigure", is_flag=True,
              help="Force a CMake reconfigure before target discovery")
@click.option(
    "--code", "code_release", is_flag=True,
    help="Produce a source (code) release archive instead of a binary "
         "release — no build, platform-independent",
)
@click.option(
    "--format", "archive_format",
    type=click.Choice(["zip", "tar.gz"]),
    default="zip",
    show_default=True,
    help="Archive format (code releases only)",
)
@click.option(
    "--update-manifest", is_flag=True,
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
        return

    gem_dir, canonical, version = _locate_gem(ws_path, object_name, as_json)

    # ------------------------------------------------------------------
    # Code (source) release: archive the source tree — no build needed
    # ------------------------------------------------------------------
    if code_release:
        if not as_json:
            console.print(
                f"[bold]Packaging gem source:[/bold] {canonical}=={version}"
            )
        out = output_dir if output_dir is not None else Path.cwd()
        archive_path, sha256 = package_gem_source_archive(
            gem_dir, canonical, version, out, fmt=archive_format,
        )

        manifest_path = None
        if update_manifest:
            manifest_path = update_release_manifest_source(
                gem_dir, version, archive_path.resolve().as_uri(), sha256,
            )

        if as_json:
            emit_response(data={
                "object": canonical,
                "version": version,
                "release": "code",
                "archive": str(archive_path),
                "sha256": sha256,
                "manifest": str(manifest_path) if manifest_path else None,
            })
        else:
            console.print(f"[green]Source release archive:[/green] {archive_path}")
            console.print(f"  sha256: {sha256}")
            if manifest_path:
                console.print(f"  Manifest updated: {manifest_path}")
                console.print(
                    "[dim]Replace the file:// URL with the hosted archive URL "
                    "after uploading[/dim]"
                )
            else:
                console.print(
                    "[dim]Advertise in the gem manifest under releases[] as:\n"
                    f'  {{"name": "{version}", "downloads": [{{"source": '
                    f'"<url>", "source_sha256": "{sha256}"}}]}}[/dim]'
                )
        return

    build_dir, gem_targets = _discover_targets(
        ws_path, gem_dir, config, as_json, reconfigure,
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
        console.print(
            f"[bold]Packaging gem:[/bold] {canonical}=={version} ({config})"
        )
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
        return

    out = output_dir if output_dir is not None else Path.cwd()
    zip_path, sha256 = package_gem_archive(
        staged, canonical, version, out,
    )
    on_progress(f"Archived {zip_path.name}")

    manifest_path = None
    if update_manifest:
        manifest_path = update_release_manifest(
            gem_dir, version, zip_path.resolve().as_uri(), sha256,
        )
        on_progress(f"Updated releases[] in {manifest_path}")

    if as_json:
        emit_response(data={
            "object": canonical,
            "version": version,
            "config": config,
            "archive": str(zip_path),
            "sha256": sha256,
            "package_dir": str(staged),
            "manifest": str(manifest_path) if manifest_path else None,
        })
    else:
        console.print(f"[green]Release archive:[/green] {zip_path}")
        console.print(f"  sha256: {sha256}")
        if manifest_path:
            console.print(f"  Manifest updated: {manifest_path}")
            console.print(
                "[dim]Replace the file:// URL with the hosted archive URL "
                "after uploading[/dim]"
            )
        else:
            console.print(
                "[dim]Advertise in the gem manifest under releases[] as:\n"
                f'  {{"name": "{version}", "binaries": [{{"platform": '
                f'"<Platform>", "binary": "<url>", "sha256": "{sha256}"}}]}}[/dim]'
            )
