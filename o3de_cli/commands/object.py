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


# ── split-platforms ─────────────────────────────────────────────────

_SPLIT_SKIP_DIRS = {
    ".git", ".svn", ".vs", "__pycache__", "build", "Cache", "External",
    "node_modules", "user",
}

#: Directory name that marks a common (platform-agnostic fallback) payload.
_COMMON_PLATFORM = "Common"


def _load_object_json(object_path: Path) -> tuple[dict, str]:
    """Load the object JSON at *object_path*.

    Returns (data, type_token).  Prefers 2.0.0 sidecars.  Raises
    ``FileNotFoundError`` when no object JSON exists.
    """
    for suffix in ("2-0-0.json", "json"):
        for type_token in ("engine", "project", "gem", "template"):
            candidate = object_path / f"{type_token}.{suffix}"
            if candidate.exists():
                with open(candidate, encoding="utf-8-sig") as f:
                    return json.load(f), type_token
    raise FileNotFoundError(
        f"No engine/project/gem/template JSON at: {object_path}"
    )


def _find_platform_dirs(object_root: Path) -> dict[str, list[Path]]:
    """Find PAL ``Platform/<Name>`` directories under *object_root*.

    Returns platform name → list of absolute platform directories.
    Walks the tree skipping VCS/build folders; every child directory of
    a directory literally named ``Platform`` is a platform payload.
    """
    found: dict[str, list[Path]] = {}

    def walk(directory: Path) -> None:
        for child in sorted(directory.iterdir()):
            if not child.is_dir() or child.name in _SPLIT_SKIP_DIRS:
                continue
            if child.name == "Platform":
                for plat_dir in sorted(child.iterdir()):
                    if plat_dir.is_dir():
                        found.setdefault(plat_dir.name, []).append(plat_dir)
            else:
                walk(child)

    walk(object_root)
    return found


def _split_overlay_name(base_name: str, suffix: str) -> str:
    """Derive an overlay name from *base_name* and a platform *suffix*.

    ``org.o3de.gem.achievementstest`` + ``windows`` →
    ``org.o3de.overlay.achievementstest.windows``.  Falls back to
    appending ``.overlay`` when no type token is present.
    """
    parts = base_name.split(".")
    for i, part in enumerate(parts):
        if part in ("gem", "project", "engine", "template"):
            parts[i] = "overlay"
            return ".".join(parts) + f".{suffix.lower()}"
    return base_name + f".overlay.{suffix.lower()}"


def _family_repo_name(base_name: str) -> str:
    """Derive a family repo object name from an object name.

    ``org.o3de.gem.achievements`` → ``org.o3de.repo.achievements``.
    """
    parts = base_name.split(".")
    for i, part in enumerate(parts):
        if part in ("gem", "project", "engine", "template"):
            parts[i] = "repo"
            return ".".join(parts)
    return base_name + ".repo"


def _build_overlay_meta(
    ov_name: str,
    plat: str,
    is_common: bool,
    base_name: str,
    base_version: str,
    ov_version: str,
    type_token: str,
    precedence: int,
    origin: dict | None,
    licenses: list[dict],
    user_tags: list[str],
    common_name: str | None,
) -> dict:
    """Build the overlay.json document for a split-out platform payload."""
    display_base = base_name.split(".")[-1]
    meta: dict = {
        "$schemaVersion": "2.0.0",
        "$schema": "https://canonical.o3de.org/o3de-overlay-2.0.0.json",
        "overlay": {
            "name": ov_name,
            "version": ov_version,
            "display_name": f"{display_base} {plat}",
            "description": (
                f"Shared platform-common payload for the {display_base} "
                f"{type_token}. Platform overlays depend on this overlay."
                if is_common else
                f"{plat} platform delivery for the {display_base} "
                f"{type_token}, composed into the {type_token} tree at "
                f"workspace compose time."
            ),
            "type": "code",
        },
        "extends": f"{base_name}>={base_version}",
        "precedence": 0 if is_common else precedence,
    }
    if origin:
        meta["origin"] = origin
    if licenses:
        meta["licenses"] = licenses
    meta["canonical_tags"] = ["Overlay"]
    if user_tags:
        meta["user_tags"] = user_tags
    if not is_common:
        meta["platforms"] = [plat]
        if common_name:
            meta["dependent"] = {
                "overlays": [f"{common_name}>={ov_version}"],
            }
    return meta


@object_group.command("split-platforms")
@click.argument("object_path", type=click.Path(exists=True, file_okay=False))
@click.option("--output", "-o", type=click.Path(),
              help="Directory to create the overlay objects in "
                   "(default: the object's parent directory)")
@click.option("--platforms", "-P", "platforms_opt", multiple=True,
              help="Only split these platforms (repeatable or "
                   "comma-separated; default: all detected)")
@click.option("--overlay-version", "-V", "overlay_version", default=None,
              help="Version for the created overlays "
                   "(default: the base object's version)")
@click.option("--tags", "-T", "tags_opt", multiple=True,
              help="user_tags to stamp on the created overlays")
@click.option("--precedence", type=int, default=10, show_default=True,
              help="Precedence for platform overlays (common overlay is 0)")
@click.option("--remove", "do_remove", is_flag=True,
              help="Remove the split platform directories from the "
                   "source object after creating the overlays")
@click.option("--register", "do_register", is_flag=True,
              help="Register the created overlays in the manifest")
@click.option("--dry-run", is_flag=True,
              help="Only report what would be split")
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
    import shutil

    from o3de_cli.core.json_output import emit_response
    from o3de_cli.core.models import get_object_name, get_object_version

    obj_root = Path(object_path).resolve()
    try:
        obj_data, type_token = _load_object_json(obj_root)
    except FileNotFoundError as e:
        _fail(str(e), "E_NOT_AN_OBJECT", as_json)
        raise

    base_name = get_object_name(obj_data) or obj_root.name
    base_version = get_object_version(obj_data)
    ov_version = overlay_version or base_version or "1.0.0"

    # Detect platform payloads
    detected = _find_platform_dirs(obj_root)
    if not detected:
        _fail(
            f"No Platform/<Name> directories found in {obj_root}",
            "E_NO_PLATFORM_DIRS",
            as_json,
        )

    # Filter selection
    selected: list[str] | None = None
    if platforms_opt:
        selected = []
        for value in platforms_opt:
            for token in value.split(","):
                token = token.strip()
                if token:
                    selected.append(token)
        unknown = [p for p in selected
                   if p.lower() not in {d.lower() for d in detected}]
        if unknown:
            _fail(
                f"Platforms not present in the object: {', '.join(unknown)} "
                f"(detected: {', '.join(sorted(detected))})",
                "E_PLATFORM_NOT_FOUND",
                as_json,
            )
        keep = {p.lower() for p in selected}
        # Common is always split when any platform is (dependency target)
        detected = {
            name: dirs for name, dirs in detected.items()
            if name.lower() in keep or name == _COMMON_PLATFORM
        }

    out_dir = Path(output).resolve() if output else obj_root.parent

    # Ordered so the common overlay is created first (dependency target)
    plat_names = sorted(detected, key=lambda n: (n != _COMMON_PLATFORM, n))

    user_tags: list[str] = []
    for value in tags_opt:
        for token in value.split(","):
            token = token.strip()
            if token and token not in user_tags:
                user_tags.append(token)

    common_name = (
        _split_overlay_name(base_name, "common")
        if _COMMON_PLATFORM in detected else None
    )

    plan: list[dict] = []
    for plat in plat_names:
        is_common = plat == _COMMON_PLATFORM
        ov_name = _split_overlay_name(base_name, plat)
        files = [
            f for d in detected[plat] for f in sorted(d.rglob("*")) if f.is_file()
        ]
        plan.append({
            "platform": plat,
            "overlay": ov_name,
            "path": str(out_dir / ov_name),
            "files": len(files),
            "common": is_common,
        })

    if dry_run:
        if as_json:
            emit_response(data={"object": base_name, "overlays": plan})
        else:
            console.print(f"[bold]Would split {base_name}:[/bold]")
            for entry in plan:
                console.print(
                    f"  {entry['platform']:<12} → {entry['overlay']} "
                    f"({entry['files']} files)"
                )
        return

    # Licenses travelling to each overlay: entries whose file exists at
    # the object root (relative_path licenses are object-root metadata)
    licenses = []
    for lic in obj_data.get("licenses", []):
        rel = lic.get("relative_path")
        if rel and (obj_root / rel).is_file():
            licenses.append(lic)

    origin = obj_data.get("origin")

    created: list[dict] = []
    for plat in plat_names:
        is_common = plat == _COMMON_PLATFORM
        ov_name = _split_overlay_name(base_name, plat)
        ov_root = out_dir / ov_name
        if ov_root.exists():
            _fail(f"Overlay path already exists: {ov_root}",
                  "E_OVERLAY_EXISTS", as_json)
        payload_root = ov_root / "Overlay"

        # Copy platform payload preserving the object-relative layout
        for plat_dir in detected[plat]:
            rel = plat_dir.relative_to(obj_root)
            shutil.copytree(plat_dir, payload_root / rel)

        # Copy license files
        for lic in licenses:
            shutil.copy2(obj_root / lic["relative_path"],
                         ov_root / lic["relative_path"])

        meta = _build_overlay_meta(
            ov_name=ov_name, plat=plat, is_common=is_common,
            base_name=base_name, base_version=base_version,
            ov_version=ov_version, type_token=type_token,
            precedence=precedence, origin=origin, licenses=licenses,
            user_tags=user_tags, common_name=common_name,
        )
        with open(ov_root / "overlay.json", "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
            f.write("\n")

        created.append({
            "platform": plat,
            "overlay": ov_name,
            "path": str(ov_root),
        })

    # Remove split payloads from the source object
    removed: list[str] = []
    if do_remove:
        for plat in plat_names:
            for plat_dir in detected[plat]:
                shutil.rmtree(plat_dir)
                removed.append(str(plat_dir.relative_to(obj_root)))
                # Prune the parent Platform dir when empty
                parent = plat_dir.parent
                if parent.name == "Platform" and not any(parent.iterdir()):
                    parent.rmdir()

    # Register created overlays in the manifest
    registered: list[str] = []
    if do_register:
        from o3de_cli.core.paths import get_manifest_path

        manifest_path = get_manifest_path()
        if manifest_path and manifest_path.exists():
            with open(manifest_path, encoding="utf-8-sig") as f:
                manifest = json.load(f)
            section = manifest.setdefault("local", {})
            overlays_list = section.setdefault("overlays", [])
            for entry in created:
                path_str = Path(entry["path"]).as_posix()
                if path_str not in overlays_list:
                    overlays_list.append(path_str)
                    registered.append(entry["overlay"])
            with open(manifest_path, "w", encoding="utf-8") as f:
                json.dump(manifest, f, indent=2)

    if as_json:
        emit_response(data={
            "object": base_name,
            "version": base_version,
            "overlays": created,
            "removed": removed,
            "registered": registered,
        })
    else:
        console.print(f"[green]Split {base_name}:[/green]")
        for entry in created:
            console.print(f"  {entry['platform']:<12} → {entry['path']}")
        if removed:
            console.print(
                f"[yellow]Removed from source:[/yellow] {', '.join(removed)}"
            )
        if registered:
            console.print(
                f"[dim]Registered: {', '.join(registered)}[/dim]"
            )


# ── hoist ───────────────────────────────────────────────────────────


def _run_git(args: list[str], cwd: Path | None = None,
             env: dict | None = None) -> tuple[int, str]:
    """Run a git command, returning (returncode, combined output)."""
    import os
    import subprocess

    full_env = {**os.environ, **(env or {})}
    result = subprocess.run(
        ["git", *args], cwd=cwd, env=full_env,
        capture_output=True, text=True,
    )
    return result.returncode, (result.stdout + result.stderr).strip()


@object_group.command("hoist")
@click.argument("object_path", type=click.Path(exists=True, file_okay=False))
@click.option("--output", "-o", type=click.Path(),
              help="Path for the new family repo (default: sibling of the "
                   "source git repo, named o3de-<family>)")
@click.option("--repo-name", default=None,
              help="Family repo object name "
                   "(default: derived, e.g. org.o3de.repo.<family>)")
@click.option("--branch", default=None,
              help="Branch to hoist from (default: the source repo's "
                   "current branch)")
@click.option("--overlay-version", "-V", "overlay_version", default=None,
              help="Version for the created overlays "
                   "(default: the base object's version)")
@click.option("--tags", "-T", "tags_opt", multiple=True,
              help="user_tags to stamp on the created overlays")
@click.option("--precedence", type=int, default=10, show_default=True,
              help="Precedence for platform overlays (common overlay is 0)")
@click.option("--register", "do_register", is_flag=True,
              help="Register the family repo in the manifest")
@click.option("--dry-run", is_flag=True,
              help="Only report what would be hoisted")
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
    import shutil
    import subprocess

    from o3de_cli.core.json_output import emit_response
    from o3de_cli.core.models import get_object_name, get_object_version

    obj_root = Path(object_path).resolve()
    try:
        obj_data, type_token = _load_object_json(obj_root)
    except FileNotFoundError as e:
        _fail(str(e), "E_NOT_AN_OBJECT", as_json)
        raise

    base_name = get_object_name(obj_data) or obj_root.name
    base_version = get_object_version(obj_data)
    ov_version = overlay_version or base_version or "1.0.0"
    family = base_name.split(".")[-1]
    family_repo_name = repo_name or _family_repo_name(base_name)

    # Locate the enclosing git repository
    rc, git_root_str = _run_git(
        ["rev-parse", "--show-toplevel"], cwd=obj_root,
    )
    if rc != 0:
        _fail(f"Not inside a git repository: {obj_root}",
              "E_NOT_A_GIT_REPO", as_json)
    git_root = Path(git_root_str)
    try:
        gem_rel = obj_root.relative_to(git_root).as_posix()
    except ValueError:
        _fail(f"{obj_root} is not under git root {git_root}",
              "E_NOT_IN_REPO", as_json)
        raise

    if branch is None:
        rc, branch = _run_git(
            ["rev-parse", "--abbrev-ref", "HEAD"], cwd=git_root,
        )
        if rc != 0 or branch == "HEAD":
            _fail("Cannot determine the source branch (detached HEAD?) — "
                  "pass --branch", "E_NO_BRANCH", as_json)

    # git filter-repo availability
    try:
        import git_filter_repo  # noqa: F401
    except ImportError:
        _fail("git-filter-repo is not installed. "
              "Install it with: pip install git-filter-repo",
              "E_NO_FILTER_REPO", as_json)

    # Detect platform payloads in the working tree
    detected = _find_platform_dirs(obj_root)

    # Build the filter-repo argument list: keep the gem's history and
    # rename each platform dir into its overlay's payload location.
    # Platform renames must precede the (optional) gem-root rename so a
    # renamed path no longer matches the gem prefix.
    gem_target = f"Gems/{obj_root.name}"
    filter_args = ["--path", f"{gem_rel}/"]
    overlay_dirs: dict[str, str] = {}  # overlay dir name → platform
    renames: list[tuple[str, str]] = []
    common_name = (
        _split_overlay_name(base_name, "common")
        if _COMMON_PLATFORM in detected else None
    )
    for plat, dirs in sorted(detected.items()):
        ov_dir = f"{family}.{plat.lower()}"
        overlay_dirs[ov_dir] = plat
        for plat_dir in dirs:
            src = plat_dir.relative_to(git_root).as_posix()
            obj_rel = plat_dir.relative_to(obj_root).as_posix()
            dst = f"Overlays/{ov_dir}/Overlay/{obj_rel}"
            renames.append((src, dst))
            filter_args += ["--path-rename", f"{src}/:{dst}/"]
    if gem_rel != gem_target:
        filter_args += ["--path-rename", f"{gem_rel}/:{gem_target}/"]

    out_dir = (
        Path(output).resolve() if output
        else git_root.parent / f"o3de-{family}"
    )

    if dry_run:
        plan = {
            "object": base_name,
            "repo": family_repo_name,
            "path": str(out_dir),
            "branch": branch,
            "gem_dir": gem_target,
            "overlays": [
                {"dir": f"Overlays/{d}", "platform": p,
                 "overlay": _split_overlay_name(base_name, p)}
                for d, p in sorted(overlay_dirs.items())
            ],
        }
        if as_json:
            emit_response(data=plan)
        else:
            console.print(f"[bold]Would hoist {base_name}:[/bold]")
            console.print(f"  Family repo: {family_repo_name} → {out_dir}")
            console.print(f"  Branch: {branch}")
            console.print(f"  Gem: {gem_rel} → {gem_target}")
            for src, dst in renames:
                console.print(f"  {src} → {dst}")
        return

    if out_dir.exists():
        _fail(f"Output path already exists: {out_dir}",
              "E_OUTPUT_EXISTS", as_json)

    # Fresh clone (filter-repo requirement); skip LFS smudge — pointers
    # filter fine and the gem may not have LFS content at all
    if not as_json:
        console.print(f"[dim]Cloning {git_root} (branch {branch})...[/dim]")
    rc, out = _run_git(
        ["clone", "--no-local", "--single-branch", "--branch", branch,
         f"file://{git_root.as_posix()}", str(out_dir)],
        env={"GIT_LFS_SKIP_SMUDGE": "1"},
    )
    if rc != 0:
        _fail(f"Clone failed: {out}", "E_CLONE_FAILED", as_json)

    if not as_json:
        console.print("[dim]Filtering history (git filter-repo)...[/dim]")
    result = subprocess.run(
        [sys.executable, "-m", "git_filter_repo", *filter_args],
        cwd=out_dir, capture_output=True, text=True,
    )
    if result.returncode != 0:
        shutil.rmtree(out_dir, ignore_errors=True)
        _fail(f"filter-repo failed: {result.stdout}{result.stderr}",
              "E_FILTER_FAILED", as_json)

    # ── Metadata commit on top of the split history ──────────────────
    gem_dir = out_dir / gem_target

    # Licenses travel from the gem to each overlay and to the repo root
    licenses = []
    for lic in obj_data.get("licenses", []):
        rel = lic.get("relative_path")
        if rel and (gem_dir / rel).is_file():
            licenses.append(lic)
    origin = obj_data.get("origin")

    user_tags: list[str] = []
    for value in tags_opt:
        for token in value.split(","):
            token = token.strip()
            if token and token not in user_tags:
                user_tags.append(token)

    created_overlays: list[str] = []
    for ov_dir, plat in sorted(overlay_dirs.items()):
        ov_root = out_dir / "Overlays" / ov_dir
        if not ov_root.is_dir():
            continue  # platform had no history (nothing to overlay)
        is_common = plat == _COMMON_PLATFORM
        ov_name = _split_overlay_name(base_name, plat)
        meta = _build_overlay_meta(
            ov_name=ov_name, plat=plat, is_common=is_common,
            base_name=base_name, base_version=base_version,
            ov_version=ov_version, type_token=type_token,
            precedence=precedence, origin=origin, licenses=licenses,
            user_tags=user_tags, common_name=common_name,
        )
        with open(ov_root / "overlay.json", "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
            f.write("\n")
        for lic in licenses:
            shutil.copy2(gem_dir / lic["relative_path"],
                         ov_root / lic["relative_path"])
        created_overlays.append(ov_name)

    # Repo root licenses
    for lic in licenses:
        shutil.copy2(gem_dir / lic["relative_path"],
                     out_dir / lic["relative_path"])

    # repo.json — the family repo object
    display_family = family.capitalize()
    repo_meta: dict = {
        "$schemaVersion": "2.0.0",
        "$schema": "https://canonical.o3de.org/o3de-repo-2.0.0.json",
        "repo": {
            "name": family_repo_name,
            "display_name": f"{display_family} Family Repo",
            "description": (
                f"Family repository for the {display_family} {type_token}: "
                f"the {type_token} plus one overlay per platform, hoisted "
                f"from the engine with full git history."
            ),
            "type": "repo",
        },
    }
    if origin:
        repo_meta["origin"] = origin
    if licenses:
        repo_meta["licenses"] = licenses
    repo_meta["canonical_tags"] = ["Repo"]
    # Point children at the 2.0.0 sidecar when the gem ships one — the
    # remote crawler fetches the literal child path, and the legacy
    # gem.json would resolve under its legacy short name
    gem_json_name = (
        "gem.2-0-0.json"
        if (gem_dir / "gem.2-0-0.json").is_file() else "gem.json"
    )
    repo_meta["children"] = {
        "engines": [],
        "projects": [],
        "gems": [f"{gem_target}/{gem_json_name}"],
        "templates": [],
        "repos": [],
        "overlays": [
            f"Overlays/{d}/overlay.json" for d in sorted(overlay_dirs)
            if (out_dir / "Overlays" / d).is_dir()
        ],
    }
    repo_meta["remote"] = {
        "engines": [], "projects": [], "gems": [],
        "templates": [], "repos": [], "overlays": [],
    }
    with open(out_dir / "repo.json", "w", encoding="utf-8") as f:
        json.dump(repo_meta, f, indent=2)
        f.write("\n")

    rc, out = _run_git(["add", "-A"], cwd=out_dir)
    if rc == 0:
        rc, out = _run_git(
            ["commit", "-m",
             f"Hoist {base_name} into family repo {family_repo_name}\n\n"
             f"repo.json + per-platform overlay objects laid over the\n"
             f"filter-repo split of the {type_token}'s git history."],
            cwd=out_dir,
        )
    if rc != 0:
        _fail(f"Metadata commit failed: {out}", "E_COMMIT_FAILED", as_json)

    # Register the family repo (repo-level registration only — the
    # resolver exposes the gem and overlays through repo.json children)
    registered = False
    if do_register:
        from o3de_cli.core.paths import get_manifest_path

        manifest_path = get_manifest_path()
        if manifest_path and manifest_path.exists():
            with open(manifest_path, encoding="utf-8-sig") as f:
                manifest = json.load(f)
            section = manifest.setdefault("local", {})
            repos_list = section.setdefault("repos", [])
            path_str = out_dir.as_posix()
            if path_str not in repos_list:
                repos_list.append(path_str)
                registered = True
            with open(manifest_path, "w", encoding="utf-8") as f:
                json.dump(manifest, f, indent=2)

    if as_json:
        emit_response(data={
            "object": base_name,
            "repo": family_repo_name,
            "path": str(out_dir),
            "branch": branch,
            "overlays": created_overlays,
            "registered": registered,
        })
    else:
        console.print(f"[green]Hoisted {base_name}:[/green] {out_dir}")
        console.print(f"  Repo object: {family_repo_name}")
        console.print(f"  Gem: {gem_target}")
        for ov in created_overlays:
            console.print(f"  Overlay: {ov}")
        if registered:
            console.print("[dim]Registered family repo in manifest[/dim]")
        console.print(
            "[dim]Upstream sync: re-run the same hoist to a scratch path "
            "after upstream merges, then fetch+merge from it[/dim]"
        )
