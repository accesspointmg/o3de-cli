# O3DE Pilot CLI - Template Commands
# SPDX-License-Identifier: Apache-2.0 OR MIT

"""Template management commands."""

import click
import os
import shutil
from pathlib import Path
from rich.console import Console
from rich.table import Table

from o3de_cli.core.json_output import emit_response, emit_error

console = Console()


# Binary extensions that should be copied without content replacement
_BINARY_EXTENSIONS = frozenset({
    ".pak", ".bin", ".png", ".tga", ".bmp", ".dat", ".trp", ".fbx", ".mb",
    ".tif", ".tiff", ".dds", ".dds0", ".dds1", ".dds2", ".dds3", ".dds4",
    ".dds5", ".dds6", ".dds7", ".dds8", ".dds9", ".ctc", ".bnk", ".wav",
    ".akd", ".cgf", ".wem", ".assetinfo", ".animgraph", ".motionset",
    ".jpg", ".jpeg", ".gif", ".ico", ".hdr", ".exr", ".psd",
    ".mp3", ".ogg", ".flac",
    ".glb", ".gltf", ".obj", ".stl",
    ".zip", ".7z", ".tar", ".gz",
    ".dll", ".so", ".dylib", ".exe",
    ".ttf", ".otf", ".woff", ".woff2",
})


def _build_replacements(name: str, *, version: str = "1.0.0", project_path: str = "", engine_path: str = "") -> dict[str, str]:
    """Build the token → value map for template instantiation."""
    import uuid

    # Sanitize for C++ identifiers
    sanitized = "".join(c if c.isalnum() or c == "_" else "_" for c in name)
    return {
        "${Name}": name,
        "${NameLower}": name.lower(),
        "${NameUpper}": name.upper(),
        "${SanitizedCppName}": sanitized,
        "${ModuleClassId}": "{" + str(uuid.uuid4()).upper() + "}",
        "${SysCompClassId}": "{" + str(uuid.uuid4()).upper() + "}",
        "${EditorSysCompClassId}": "{" + str(uuid.uuid4()).upper() + "}",
        "${Version}": version,
        "${ProjectId}": "{" + str(uuid.uuid4()).upper() + "}",
        "${ProjectPath}": project_path,
        "${EnginePath}": engine_path,
    }


def _replace_tokens(text: str, replacements: dict[str, str]) -> str:
    """Replace all template tokens in a string.

    Handles ${Random_Uuid} specially — each occurrence gets a unique UUID.
    """
    import uuid

    for token, value in replacements.items():
        text = text.replace(token, value)
    # Each ${Random_Uuid} gets a fresh UUID
    while "${Random_Uuid}" in text:
        text = text.replace("${Random_Uuid}", str(uuid.uuid4()).upper(), 1)
    return text


def _load_template_manifest(template_path: Path) -> dict | None:
    """Load the template manifest (2.0.0 preferred, fallback to legacy).

    Returns the parsed JSON dict with copyFiles/createDirectories, or None
    if no manifest exists.
    """
    import json

    # Prefer 2.0.0
    manifest_20 = template_path / "template.2-0-0.json"
    if manifest_20.exists():
        with open(manifest_20, "r", encoding="utf-8") as f:
            return json.load(f)

    # Fallback to legacy template.json (schema 0 or 1.0 — same copyFiles structure)
    manifest_legacy = template_path / "template.json"
    if manifest_legacy.exists():
        with open(manifest_legacy, "r", encoding="utf-8") as f:
            return json.load(f)

    return None


def instantiate_template(
    template_path: Path,
    dest_path: Path,
    name: str,
) -> list[str]:
    """Instantiate a schema 2.0 template into dest_path.

    Reads the template manifest (template.2-0-0.json or template.json),
    creates directories, and copies files with token replacement according
    to the manifest's copyFiles/createDirectories.

    Source files are read from template_path/Template/<file>.

    Args:
        template_path: Root of the template (contains template.json and Template/).
        dest_path: Where to create the instance.
        name: Instance name used for token replacement.

    Returns:
        List of created file paths (relative to dest_path).
    """
    manifest = _load_template_manifest(template_path)
    if manifest is None:
        raise FileNotFoundError(
            f"No template manifest (template.2-0-0.json or template.json) found in {template_path}"
        )

    replacements = _build_replacements(name)
    created: list[str] = []
    source_root = template_path / "Template"

    if not source_root.is_dir():
        raise FileNotFoundError(
            f"Template source directory not found: {source_root}"
        )

    # 1. Create directories (with token replacement in names)
    for entry in manifest.get("createDirectories", []):
        dir_rel = entry["dir"] if isinstance(entry, dict) else entry
        dir_rel = _replace_tokens(dir_rel, replacements)
        (dest_path / dir_rel).mkdir(parents=True, exist_ok=True)

    # 2. Copy files according to manifest
    for entry in manifest.get("copyFiles", []):
        file_rel = entry["file"]
        is_templated = entry.get("isTemplated", True)

        src_file = source_root / file_rel
        # Replace tokens in the destination path
        dest_rel = _replace_tokens(file_rel, replacements)
        dst_file = dest_path / dest_rel

        # Ensure parent directory exists
        dst_file.parent.mkdir(parents=True, exist_ok=True)

        if not src_file.exists():
            # Skip missing files (may be platform-specific)
            continue

        if not is_templated:
            # Plain copy — no content transformation
            shutil.copy2(src_file, dst_file)
        else:
            # Token replacement in content (binary files just get copied)
            ext = src_file.suffix.lower()
            if ext in _BINARY_EXTENSIONS:
                shutil.copy2(src_file, dst_file)
            else:
                try:
                    content = src_file.read_text(encoding="utf-8")
                    content = _replace_tokens(content, replacements)
                    dst_file.write_text(content, encoding="utf-8")
                except (UnicodeDecodeError, ValueError):
                    shutil.copy2(src_file, dst_file)

        created.append(dest_rel)

    return created


@click.group()
def template() -> None:
    """Manage O3DE templates."""
    pass


@template.command("list")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def list_templates(as_json: bool) -> None:
    """List all registered templates."""
    from o3de_cli.core.resolver import Resolver
    
    resolver = Resolver()
    resolver.resolve()
    templates = resolver.templates
    
    if as_json:
        import json
        items = []
        for name, obj in templates.items():
            items.append({"name": obj.name, "version": obj.version, "path": str(obj.path)})
        click.echo(json.dumps(items, indent=2))
        return
    
    if not templates:
        console.print("[yellow]No templates registered.[/yellow]")
        return
    
    table = Table(title="Registered Templates")
    table.add_column("Name", style="cyan")
    table.add_column("Type", style="green")
    table.add_column("Path", style="dim")
    
    for name, obj in templates.items():
        tpl_type = obj.data.get("template_type") or "project"
        table.add_row(obj.name, tpl_type, str(obj.path))
    
    console.print(table)


@template.command("info")
@click.argument("name")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def info(name: str, as_json: bool) -> None:
    """Show information about a template."""
    from o3de_cli.core.resolver import load_resolved_manifest
    
    try:
        resolved = load_resolved_manifest()
    except Exception:
        if as_json:
            emit_error("No resolved manifest. Run 'manifest resolve' first.", code="NO_MANIFEST")
            return
        console.print("[yellow]No resolved manifest. Run 'manifest resolve' first.[/yellow]")
        raise SystemExit(1)
    
    obj_data = None
    for obj_name, obj_info in resolved.get("objects", {}).items():
        if obj_info.get("type") == "template" and (name in obj_name or name == obj_name):
            obj_data = obj_info
            obj_data["_name"] = obj_name
            break
    
    if not obj_data:
        if as_json:
            emit_error(f"Template not found: {name}", code="NOT_FOUND")
            return
        console.print(f"[red]Template not found:[/red] {name}")
        raise SystemExit(1)
    
    if as_json:
        emit_response(data={
            "name": obj_data["_name"],
            "version": obj_data.get("version", "unknown"),
            "path": obj_data.get("path", "unknown"),
            "display_name": (obj_data.get("display_metadata") or {}).get("display_name"),
            "summary": (obj_data.get("display_metadata") or {}).get("summary"),
        })
        return
    
    console.print(f"\n[bold cyan]{obj_data['_name']}[/bold cyan]")
    console.print(f"  Version:  {obj_data.get('version', 'unknown')}")
    console.print(f"  Path:     {obj_data.get('path', 'unknown')}")
    
    meta = obj_data.get("display_metadata") or {}
    if meta.get("display_name"):
        console.print(f"  Display:  {meta['display_name']}")
    if meta.get("summary"):
        console.print(f"  Summary:  {meta['summary']}")
    
    console.print()


@template.command("create")
@click.argument("name")
@click.option("--path", "-p", type=click.Path(), help="Template path")
@click.option("--source", "-s", type=click.Path(exists=True),
              help="Source directory to create template from")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def create_template(name: str, path: str | None, source: str | None, as_json: bool) -> None:
    """Create a new template from a source directory.

    If --source is given, the source directory's contents are copied into
    the new template.  Otherwise a minimal skeleton is created.
    """
    import json
    import shutil

    tpl_path = Path(path) if path else Path.cwd() / name

    if not as_json:
        console.print(f"[bold]Creating template:[/bold] {name}")

    if tpl_path.exists():
        if as_json:
            emit_error(f"Path already exists: {tpl_path}", code="PATH_EXISTS")
            return
        console.print(f"[red]Path already exists:[/red] {tpl_path}")
        raise SystemExit(1)

    tpl_path.mkdir(parents=True)

    # Copy source contents if provided
    if source:
        src = Path(source)
        for item in src.iterdir():
            dest = tpl_path / item.name
            if item.is_dir():
                shutil.copytree(item, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dest)
        if not as_json:
            console.print(f"[dim]Copied source: {src}[/dim]")
    else:
        # Minimal skeleton
        (tpl_path / "Template").mkdir()

    # Create template.2-0-0.json
    tpl_json = {
        "$schema": "https://canonical.o3de.org/o3de-template-2.0.0.json",
        "$schemaVersion": "2.0.0",
        "template": {
            "name": name,
            "display_name": name.split(".")[-1],
            "version": "1.0.0",
            "description": f"A new O3DE template: {name}",
        },
    }
    with open(tpl_path / "template.2-0-0.json", "w") as f:
        json.dump(tpl_json, f, indent=2)

    if as_json:
        emit_response(data={"name": name, "path": str(tpl_path), "source": source})
        return
    console.print(f"[green]Created template:[/green] {tpl_path}")
    console.print("[dim]Register it with: o3de-pilot template register <path>[/dim]")


@template.command("instance")
@click.argument("template_name")
@click.argument("name")
@click.option("--path", "-p", type=click.Path(), help="Instance output path")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.option("--dry-run", is_flag=True, help="Show what would be created without doing it")
def instance_template(template_name: str, name: str, path: str | None, as_json: bool, dry_run: bool) -> None:
    """Instantiate a template to create a new object.

    Copies the template contents to a new directory, replacing
    placeholder tokens with the instance NAME.
    """
    inst_path = Path(path) if path else Path.cwd() / name

    if not as_json:
        console.print(f"[bold]Instantiating template:[/bold] {template_name} -> {name}")

    if inst_path.exists():
        if as_json:
            emit_error(f"Path already exists: {inst_path}", code="PATH_EXISTS")
            return
        console.print(f"[red]Path already exists:[/red] {inst_path}")
        raise SystemExit(1)

    # Locate the template
    from o3de_cli.core.resolver import Resolver

    resolver = Resolver()
    resolver.resolve()

    tpl_obj = None
    for tpl_name, tpl in resolver.templates.items():
        if template_name in tpl_name or template_name == tpl_name:
            tpl_obj = tpl
            break

    if not tpl_obj or not tpl_obj.path.exists():
        if as_json:
            emit_error(f"Template not found: {template_name}", code="NOT_FOUND")
            return
        console.print(f"[red]Template not found:[/red] {template_name}")
        raise SystemExit(1)

    if dry_run:
        # Preview: list files with tokens replaced in names from manifest
        manifest = _load_template_manifest(tpl_obj.path)
        if manifest is None:
            msg = f"No template manifest found in {tpl_obj.path}"
            if as_json:
                emit_error(msg, code="NO_MANIFEST")
            else:
                console.print(f"[red]{msg}[/red]")
            raise SystemExit(1)
        replacements = _build_replacements(name)
        files = [
            _replace_tokens(entry["file"], replacements)
            for entry in manifest.get("copyFiles", [])
        ]
        plan = {"template": template_name, "instance": name, "path": str(inst_path), "files": files}
        if as_json:
            emit_response(data=plan)
        else:
            console.print("[bold]Dry run — would create:[/bold]")
            console.print(f"  [dim]path:[/dim] {inst_path}")
            for f in files:
                console.print(f"  [dim]  {f}[/dim]")
        return

    # Instantiate with token replacement
    created = instantiate_template(tpl_obj.path, inst_path, name)

    if as_json:
        emit_response(data={"template": template_name, "instance": name, "path": str(inst_path), "files_created": len(created)})
    else:
        console.print(f"[green]Created instance:[/green] {inst_path} ({len(created)} files)")
        console.print("[dim]Register it with: o3de-pilot register <path>[/dim]")


@template.command("register")
@click.argument("path_or_url")
@click.option("--remote", is_flag=True, help="Register a remote URL instead of a local path")
def register_template(path_or_url: str, remote: bool) -> None:
    """Register a template by adding its path to the manifest."""
    import json
    from o3de_cli.core.paths import get_manifest_path

    manifest_path = get_manifest_path()
    if not manifest_path.exists():
        console.print("[red]No manifest found.[/red]")
        raise SystemExit(1)

    with open(manifest_path) as f:
        manifest = json.load(f)

    if remote:
        console.print(f"[bold]Registering remote template:[/bold] {path_or_url}")
        section = manifest.setdefault("remote", {})
        templates_list = section.setdefault("templates", [])
        if path_or_url in templates_list:
            console.print("[yellow]Remote template already registered.[/yellow]")
            return
        templates_list.append(path_or_url)
    else:
        tpl_path = Path(path_or_url).resolve()
        console.print(f"[bold]Registering template:[/bold] {tpl_path}")
        is_tpl = any(
            (tpl_path / f).exists()
            for f in ["template.2-0-0.json", "template.json"]
        )
        if not is_tpl:
            console.print("[red]No template JSON found at this path.[/red]")
            raise SystemExit(1)
        section = manifest.setdefault("local", {})
        templates_list = section.setdefault("templates", [])
        path_str = tpl_path.as_posix()
        if path_str in templates_list:
            console.print("[yellow]Template already registered.[/yellow]")
            return
        templates_list.append(path_str)

    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    console.print(f"[green]Registered template:[/green] {path_or_url}")


@template.command("unregister")
@click.argument("name")
@click.option("--remote", is_flag=True, help="Remove from remote section instead of local")
def unregister_template(name: str, remote: bool) -> None:
    """Unregister a template by removing it from the manifest."""
    import json
    from o3de_cli.core.paths import get_manifest_path

    manifest_path = get_manifest_path()
    if not manifest_path.exists():
        console.print("[red]No manifest found.[/red]")
        raise SystemExit(1)

    with open(manifest_path) as f:
        manifest = json.load(f)

    section_key = "remote" if remote else "local"
    label = "remote template" if remote else "template"
    console.print(f"[bold]Unregistering {label}:[/bold] {name}")

    section = manifest.get(section_key, {})
    templates_list = section.get("templates", [])

    original_len = len(templates_list)
    templates_list = [t for t in templates_list if name not in t]

    if len(templates_list) == original_len:
        console.print(f"[yellow]Template '{name}' not found in {section_key} manifest.[/yellow]")
        return

    section["templates"] = templates_list
    manifest[section_key] = section

    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    console.print(f"[green]Unregistered {label}:[/green] {name}")
