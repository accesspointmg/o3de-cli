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
