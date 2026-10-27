# O3DE CLI - Manifest Commands
# SPDX-License-Identifier: Apache-2.0 OR MIT

"""Manifest resolution and management commands."""

import json
from pathlib import Path

import click
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from o3de_cli.core import (
    ObjectType,
    Resolver,
    get_manifest_path,
    get_resolved_manifest_path,
    resolve_manifest,
)
from o3de_cli.core.json_output import emit_error, emit_response
from o3de_cli.core.paths import new_manifest_data
from o3de_cli.core.upgrade import (
    get_schema_version,
    needs_upgrade,
    upgrade_directory,
    upgrade_file,
)

console = Console()
# Diagnostics under --json, where stdout carries only the JSON document.
err_console = Console(stderr=True)


@click.group()
def manifest() -> None:
    """Manage the O3DE manifest."""
    pass


@manifest.command("resolve")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.option("--no-save", is_flag=True, help="Don't save resolved manifest")
@click.option("--dry-run", is_flag=True, help="Resolve but don't write anything to disk")
@click.option(
    "--install",
    is_flag=True,
    help="Auto-install missing dependencies (preview only, use --yes to confirm)",
)
@click.option("--yes", "-y", is_flag=True, help="Confirm auto-install of missing dependencies")
def resolve_command(as_json: bool, no_save: bool, dry_run: bool, install: bool, yes: bool) -> None:
    """Resolve the manifest and discover all objects.

    Descends all registered paths, reads object JSON files,
    resolves children and dependencies, and saves to
    resolved_o3de_manifest.json.

    Use --install to auto-fetch missing dependencies from remote registries.
    Use --install --yes to skip confirmation.
    """
    manifest_path = get_manifest_path()
    # Under --json, stdout carries only the JSON document: diagnostics go
    # to stderr and the spinners are disabled.
    ui = err_console if as_json else console

    if not manifest_path.exists():
        if as_json:
            emit_error(f"Manifest not found: {manifest_path}", code="E_NO_MANIFEST")
            raise SystemExit(1)
        console.print(f"[red]Manifest not found:[/red] {manifest_path}")
        console.print("Run 'o3de init' to create a new manifest.")
        raise SystemExit(1)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=ui,
        disable=as_json,
    ) as progress:
        task = progress.add_task("Resolving manifest...", total=None)

        def on_progress(msg: str, current: int, total: int):
            progress.update(task, description=msg, completed=current, total=total)

        resolver = Resolver(manifest_path, dry_run=dry_run)
        resolver.resolve(progress_callback=on_progress)

        if not no_save:
            resolved_path = resolver.save()
            if dry_run:
                progress.update(task, description="Dry-run: no files written")
            else:
                progress.update(task, description=f"Saved: {resolved_path}")

    # Report missing dependencies
    missing = resolver.get_missing_dependencies()
    if missing:
        ui.print(f"\n[yellow]Missing dependencies ({len(missing)}):[/yellow]")
        for requirer, dep_spec in missing:
            ui.print(f"  {requirer} → {dep_spec}")

        # Auto-install flow
        if install or yes:
            from o3de_cli.core import Store

            store = Store()

            # Refresh store from manifest remotes so search works
            if resolver.manifest_remotes:
                with Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    console=ui,
                    disable=as_json,
                    transient=True,
                ) as progress:
                    task = progress.add_task("Refreshing remote catalog...", total=None)
                    store.refresh_sync(resolver.manifest_remotes)

            if dry_run or (install and not yes):
                # Preview mode
                plan = resolver.auto_install_missing(store, confirm=False, dry_run=True)
                if plan:
                    ui.print(f"\n[bold]Would install {len(plan)} dependencies:[/bold]")
                    for item in plan:
                        ui.print(
                            f"  [cyan]{item['name']}[/cyan]@{item['version']} ({item['type']}) from {item['source']}"
                        )
                    if not yes:
                        ui.print("\n[dim]Run with --yes to install, or --dry-run to preview.[/dim]")
                else:
                    ui.print("[yellow]No remote objects found for missing deps.[/yellow]")
            else:
                # Actually install
                with Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    console=ui,
                    disable=as_json,
                ) as progress:
                    task = progress.add_task("Installing dependencies...", total=None)

                    def on_install(msg: str, current: int, total: int):
                        progress.update(task, description=msg, completed=current, total=total)

                    installed = resolver.auto_install_missing(
                        store,
                        confirm=True,
                        dry_run=False,
                        progress_callback=on_install,
                    )

                if installed:
                    ui.print(f"\n[green]Installed {len(installed)} dependencies:[/green]")
                    for item in installed:
                        ui.print(
                            f"  [cyan]{item['name']}[/cyan]@{item['version']} → {item['path']}"
                        )

                    # Re-resolve with newly installed deps
                    ui.print("\n[dim]Re-resolving with new dependencies...[/dim]")
                    resolver2 = Resolver(manifest_path, dry_run=dry_run)
                    resolver2.resolve()
                    if not no_save:
                        resolver2.save()
                    ui.print("[green]Resolution updated.[/green]")
                else:
                    ui.print("[yellow]No dependencies could be installed.[/yellow]")

    # Report conflicts
    if resolver.conflicts:
        ui.print(f"\n[red]Version conflicts ({len(resolver.conflicts)}):[/red]")
        for c in resolver.conflicts:
            ui.print(f"  {c}")

    # Report missing optional dependencies as suggestions
    missing_optional = resolver.get_missing_optional_dependencies()
    if missing_optional:
        # Deduplicate by dep name
        seen = set()
        unique = []
        for requirer, dep_spec in missing_optional:
            if dep_spec.name not in seen:
                seen.add(dep_spec.name)
                unique.append((requirer, dep_spec))
        ui.print(f"\n[dim]Optional dependencies not installed ({len(unique)}):[/dim]")
        for requirer, dep_spec in unique:
            ui.print(f"  [dim]{requirer} suggests {dep_spec}[/dim]")

    if as_json:
        output = {
            "engines": len(resolver.engines),
            "projects": len(resolver.projects),
            "gems": len(resolver.gems),
            "templates": len(resolver.templates),
            "repos": len(resolver.repos),
            "overlays": len(resolver.overlays),
            "total": len(resolver.objects),
        }
        warnings = [
            f"Missing dependency: {requirer} → {dep_spec}" for requirer, dep_spec in missing
        ]
        warnings += [f"Version conflict: {c}" for c in resolver.conflicts]
        emit_response(data=output, warnings=warnings)
    else:
        table = Table(title="Resolved Objects")
        table.add_column("Type", style="cyan")
        table.add_column("Count", style="green", justify="right")

        table.add_row("Engines", str(len(resolver.engines)))
        table.add_row("Projects", str(len(resolver.projects)))
        table.add_row("Gems", str(len(resolver.gems)))
        table.add_row("Templates", str(len(resolver.templates)))
        table.add_row("Repos", str(len(resolver.repos)))
        table.add_row("Overlays", str(len(resolver.overlays)))
        table.add_row("Total", str(len(resolver.objects)), style="bold")

        console.print(table)

        if not no_save:
            console.print(f"\n[dim]Saved:[/dim] {get_resolved_manifest_path()}")


@manifest.command("show")
@click.option("--resolved", is_flag=True, help="Show resolved manifest")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def show_command(resolved: bool, as_json: bool) -> None:
    """Show the manifest contents."""
    if resolved:
        path = get_resolved_manifest_path()
    else:
        path = get_manifest_path()

    if not path.exists():
        if as_json:
            emit_error(f"Not found: {path}", code="E_NOT_FOUND")
            raise SystemExit(1)
        console.print(f"[red]Not found:[/red] {path}")
        raise SystemExit(1)

    with open(path) as f:
        data = json.load(f)

    if as_json:
        emit_response(data=data)
    else:
        # Pretty print summary
        console.print(f"[bold]Manifest:[/bold] {path}\n")

        if resolved:
            console.print(f"[dim]Resolved at:[/dim] {data.get('resolved_at', 'unknown')}")
            console.print(f"[dim]Objects:[/dim] {len(data.get('objects', {}))}")
        else:
            local = data.get("local", {})
            console.print("[bold]Local objects:[/bold]")
            for key in ["engines", "projects", "gems", "templates", "repos", "overlays"]:
                paths = local.get(key, [])
                if paths:
                    console.print(f"  {key}: {len(paths)}")


@manifest.command("upgrade")
@click.argument("path", type=click.Path(exists=True), required=False)
@click.option("--recursive", "-r", is_flag=True, help="Upgrade recursively")
@click.option("--dry-run", is_flag=True, help="Show what would be upgraded")
def upgrade_command(
    path: str | None,
    recursive: bool,
    dry_run: bool,
) -> None:
    """Upgrade object JSON files to schema 2.0.0.

    Creates sidecar files (e.g. gem.2-0-0.json) without modifying originals.
    Without PATH, upgrades the manifest and all registered objects.
    With PATH, upgrades the specified file or directory.
    """
    if path:
        target = Path(path)
    else:
        target = get_manifest_path()

    if not target.exists():
        console.print(f"[red]Not found:[/red] {target}")
        raise SystemExit(1)

    if target.is_file():
        # Single file upgrade
        with open(target) as f:
            data = json.load(f)

        obj_type, version = get_schema_version(data)

        if not needs_upgrade(data):
            console.print(f"[green]Already at latest schema:[/green] {target}")
            return

        if dry_run:
            console.print(f"[yellow]Would upgrade:[/yellow] {target} ({version} → 2.0.0)")
            return

        result = upgrade_file(target)
        if result is None:
            # e.g. restricted objects: there is no upgrade path to 2.0.0
            console.print(
                f"[yellow]Skipped:[/yellow] {target} ({obj_type} objects have no upgrade path)"
            )
            return
        console.print(f"[green]Upgraded:[/green] {result[0]} ({result[1]} → {result[2]})")

    else:
        # Directory upgrade
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task = progress.add_task("Scanning...", total=None)

            def on_progress(msg: str, current: int, total: int):
                progress.update(task, description=msg, completed=current, total=total)

            if dry_run:
                # Just scan and report
                json_files = (
                    list(target.rglob("*.json")) if recursive else list(target.glob("*.json"))
                )
                upgradeable = []

                for json_file in json_files:
                    if json_file.name in [
                        "o3de_manifest.json",
                        "engine.json",
                        "project.json",
                        "gem.json",
                        "template.json",
                        "repo.json",
                        "overlay.json",
                    ]:
                        try:
                            with open(json_file) as f:
                                data = json.load(f)
                            if needs_upgrade(data):
                                _, version = get_schema_version(data)
                                upgradeable.append((json_file, version))
                        except Exception:
                            pass

                if upgradeable:
                    console.print(f"[yellow]Would upgrade {len(upgradeable)} files:[/yellow]")
                    for json_file, v in upgradeable[:20]:
                        console.print(f"  {json_file} ({v} → 2.0.0)")
                    if len(upgradeable) > 20:
                        console.print(f"  ... and {len(upgradeable) - 20} more")
                else:
                    console.print("[green]All files at latest schema.[/green]")
                return

            results = upgrade_directory(
                target,
                recursive=recursive,
                backup=True,
                progress_callback=on_progress,
            )

        if results:
            console.print(f"[green]Upgraded {len(results)} files:[/green]")
            for upgraded, old_v, new_v in results[:10]:
                console.print(f"  {upgraded.name} ({old_v} → {new_v})")
            if len(results) > 10:
                console.print(f"  ... and {len(results) - 10} more")
        else:
            console.print("[green]All files at latest schema.[/green]")


def detect_object_type(target: Path) -> str | None:
    """The object type of the directory *target*, from the JSON it holds."""
    for type_name in ["engine", "project", "gem", "template", "repo", "overlay"]:
        if (target / f"{type_name}.json").exists() or (target / f"{type_name}.2-0-0.json").exists():
            return type_name
    return None


def register_in_manifest(target: Path, obj_type: str | None = None) -> tuple[str, str, bool]:
    """Add the object at *target* to the local manifest.

    Returns (posix path, object type, whether it was newly added).

    Raises:
        ValueError: if *obj_type* is not given and cannot be detected.
    """
    obj_type = obj_type or detect_object_type(target)
    if not obj_type:
        raise ValueError(f"Could not detect object type: {target}")

    manifest_path = get_manifest_path()
    if manifest_path.exists():
        with open(manifest_path) as f:
            manifest_data = json.load(f)
    else:
        manifest_data = new_manifest_data()

    local = manifest_data.setdefault("local", {})
    type_list = local.setdefault(f"{obj_type}s", [])

    # POSIX paths for cross-platform manifests; compare resolved paths
    path_str = target.as_posix()
    if target.resolve() in [Path(p).resolve() for p in type_list]:
        return path_str, obj_type, False

    type_list.append(path_str)
    with open(manifest_path, "w") as f:
        json.dump(manifest_data, f, indent=2)
    return path_str, obj_type, True


@manifest.command("add")
@click.argument("path", type=click.Path(exists=True))
@click.option(
    "--type",
    "-t",
    "obj_type",
    type=click.Choice(["engine", "project", "gem", "template", "repo", "overlay"]),
    help="Object type (auto-detected if not specified)",
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def add_command(path: str, obj_type: str | None, as_json: bool) -> None:
    """Add an object to the manifest.

    Registers the object at PATH in the local manifest.
    """
    target = Path(path).resolve()
    try:
        path_str, obj_type, added = register_in_manifest(target, obj_type)
    except ValueError:
        if as_json:
            emit_error("Could not detect object type", code="E_DETECT_FAILED")
            raise SystemExit(1)
        console.print("[red]Could not detect object type.[/red]")
        console.print("Use --type to specify explicitly.")
        raise SystemExit(1)

    if added:
        if as_json:
            emit_response(data={"path": path_str, "type": obj_type, "added": True})
        else:
            console.print(f"[green]Added {obj_type}:[/green] {target.name}")
    else:
        if as_json:
            emit_response(data={"path": path_str, "type": obj_type, "already_present": True})
        else:
            console.print(f"[yellow]Already registered:[/yellow] {target.name}")


@manifest.command("remove")
@click.argument("path", type=click.Path())
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def remove_command(path: str, as_json: bool) -> None:
    """Remove an object from the manifest.

    Unregisters the object. Does not delete files.
    """
    target = Path(path).resolve()
    manifest_path = get_manifest_path()

    if not manifest_path.exists():
        if as_json:
            emit_error("No manifest found", code="E_NO_MANIFEST")
            raise SystemExit(1)
        console.print("[red]No manifest found.[/red]")
        raise SystemExit(1)

    with open(manifest_path) as f:
        manifest_data = json.load(f)

    local = manifest_data.get("local", {})
    removed = False
    target_posix = target.as_posix()

    for type_list in local.values():
        if isinstance(type_list, list):
            # Compare resolved posix paths for cross-platform compatibility
            to_remove = [p for p in type_list if Path(p).resolve().as_posix() == target_posix]
            for p in to_remove:
                type_list.remove(p)
                removed = True

    if removed:
        with open(manifest_path, "w") as f:
            json.dump(manifest_data, f, indent=2)
        if as_json:
            emit_response(data={"path": target_posix, "removed": True})
        else:
            console.print(f"[green]Removed:[/green] {target.name}")
    else:
        if as_json:
            emit_error(f"Not found in manifest: {path}", code="E_NOT_FOUND")
            raise SystemExit(1)
        console.print(f"[yellow]Not found in manifest:[/yellow] {path}")


@manifest.command("set")
@click.argument("key")
@click.argument("value")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def set_command(key: str, value: str, as_json: bool) -> None:
    """Set a manifest preference.

    Supported keys:
    - country.code: Country code (e.g., US, CA, GB)
    - default.engines_path: Default engines folder
    - default.projects_path: Default projects folder
    - default.gems_path: Default gems folder
    - default.templates_path: Default templates folder
    - default.repos_path: Default repos folder
    - default.overlays_path: Default overlays folder
    - default.third_party_path: Default third party folder

    Examples:
        o3de manifest set country.code CA
        o3de manifest set default.gems_path C:/O3DE/Gems
    """
    manifest_path = get_manifest_path()

    if not manifest_path.exists():
        if as_json:
            emit_error("No manifest found", code="E_NO_MANIFEST")
            raise SystemExit(1)
        console.print("[red]No manifest found.[/red]")
        raise SystemExit(1)

    with open(manifest_path) as f:
        manifest_data = json.load(f)

    # Parse the dotted key
    parts = key.split(".")
    if len(parts) != 2:
        if as_json:
            emit_error(f"Invalid key: {key}", code="E_INVALID_KEY")
            raise SystemExit(1)
        console.print(f"[red]Invalid key:[/red] {key}")
        console.print("Keys should be in format: section.field (e.g., country.code)")
        raise SystemExit(1)

    section, field = parts

    # Validate section exists
    if section not in ["country", "default"]:
        if as_json:
            emit_error(f"Unknown section: {section}", code="E_UNKNOWN_SECTION")
            raise SystemExit(1)
        console.print(f"[red]Unknown section:[/red] {section}")
        console.print("Valid sections: country, default")
        raise SystemExit(1)

    # Ensure section exists
    if section not in manifest_data:
        manifest_data[section] = {}

    # For paths, normalize to POSIX format
    if section == "default" and field.endswith("_path"):
        value = value.replace("\\", "/")

    # Set the value
    old_value = manifest_data[section].get(field)
    manifest_data[section][field] = value

    # Save
    with open(manifest_path, "w") as f:
        json.dump(manifest_data, f, indent=4)

    if as_json:
        emit_response(data={"key": key, "value": value, "previous": old_value})
    elif old_value is None:
        console.print(f"[green]Set {key}:[/green] {value}")
    else:
        console.print(f"[green]Updated {key}:[/green] {old_value} -> {value}")


@manifest.command("get")
@click.argument("key", required=False)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def get_command(key: str | None, as_json: bool) -> None:
    """Get a manifest preference.

    Without KEY, shows all preferences.
    With KEY, shows the specific value.

    Examples:
        o3de manifest get
        o3de manifest get country.code
        o3de manifest get default.gems_path
    """
    manifest_path = get_manifest_path()

    if not manifest_path.exists():
        if as_json:
            emit_error("No manifest found", code="E_NO_MANIFEST")
            raise SystemExit(1)
        console.print("[red]No manifest found.[/red]")
        raise SystemExit(1)

    with open(manifest_path) as f:
        manifest_data = json.load(f)

    if key is None:
        # Show all preferences
        prefs = {
            "country": manifest_data.get("country", {}),
            "default": manifest_data.get("default", {}),
        }
        if as_json:
            emit_response(data=prefs)
            return
        console.print("[bold]Country:[/bold]")
        country = manifest_data.get("country", {})
        for k, v in country.items():
            console.print(f"  country.{k}: {v}")

        console.print("\n[bold]Default Paths:[/bold]")
        defaults = manifest_data.get("default", {})
        for k, v in defaults.items():
            console.print(f"  default.{k}: {v}")
    else:
        # Get specific value
        parts = key.split(".")
        if len(parts) != 2:
            if as_json:
                emit_error(f"Invalid key: {key}", code="E_INVALID_KEY")
                raise SystemExit(1)
            console.print(f"[red]Invalid key:[/red] {key}")
            raise SystemExit(1)

        section, field = parts
        section_data = manifest_data.get(section, {})
        value = section_data.get(field)

        if as_json:
            emit_response(data={"key": key, "value": value})
        elif value is None:
            console.print(f"[yellow]Not set:[/yellow] {key}")
        else:
            console.print(value)
