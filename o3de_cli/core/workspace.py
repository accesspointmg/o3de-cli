# O3DE Pilot - Workspace Engine
# SPDX-License-Identifier: Apache-2.0 OR MIT

"""
Workspace Engine for O3DE.

A Workspace is a structured directory tree that mirrors a standard O3DE
repository layout.  Instead of copying files, it creates **real directories**
with **symbolic links** to the original source files.

Layout::

    <workspace>/
    ├── workspace.json          ← metadata + file_owners
    ├── Engines/
    │   └── <engine_name>/      ← real dirs, symlinked files
    ├── Projects/
    │   └── <project_name>/
    ├── Gems/
    │   └── <gem_name>/
    ├── Templates/
    │   └── <template_name>/
    └── Overlays/
        └── <overlay_name>/

Process:
1.  User selects a root object (engine or project).
2.  The solver resolves the full transitive dependency graph.
3.  For every object in the solution the engine:
    a.  Determines its ObjectType → picks the top-level folder.
    b.  Creates *real* sub-directories mirroring the source tree.
    c.  Creates *symlinks* for every file pointing back to the source.
4.  Overlays are applied last — overlay files replace base symlinks and
    ownership transfers.
5.  ``file_owners`` records  ``workspace-relative POSIX path → object name``.
"""

from pathlib import Path
from typing import Optional, Callable
import json
import os
import shutil
import logging

from .models import ObjectType

logger = logging.getLogger("o3de_cli.workspace")


# ObjectType → top-level workspace folder name.
# NOTE: overlays deliberately have no folder — overlay files are composed
# INTO the extended object's tree so the workspace is indistinguishable
# from a monolithic layout.  Overlay membership is recorded in workspace
# metadata (sources.overlays / file ownership), not on disk.
_TYPE_FOLDERS: dict[ObjectType, str] = {
    ObjectType.ENGINE: "Engines",
    ObjectType.PROJECT: "Projects",
    ObjectType.GEM: "Gems",
    ObjectType.TEMPLATE: "Templates",
}

# TEMPORARY (hard-coded cheat): when linking an ENGINE into a workspace,
# skip these top-level subtrees.  They are engine-internal copies of
# objects that the solver composes into the workspace separately
# (Gems/ → workspace Gems/, Templates/ → workspace Templates/,
# AutomatedTesting/ → workspace Projects/).  Remove once the engine
# repo is reworked into a proper hierarchy without embedded objects.
_ENGINE_SKIP_TOPDIRS: set[str] = {"Gems", "Templates", "AutomatedTesting"}


class WorkspaceError(Exception):
    """Error during workspace creation."""
    pass


# Backward-compatible aliases
LayoutError = WorkspaceError


class Workspace:
    """
    Represents a structured symlinked workspace.

    A workspace has:
    - root_path: Where the workspace is created
    - root_object_path: The engine or project used as the root
    - root_object_type: ENGINE or PROJECT
    - resolved_objects: All objects to include (name → (path, type))
    - overlays: Overlays to apply (in precedence order)
    - file_owners: workspace-relative POSIX path → object name
    - linked_files: workspace-absolute path → source-absolute path
    """

    def __init__(
        self,
        root_path: Path,
        root_object_path: Path,
        root_object_type: ObjectType,
        attributions: str = "workspace",
    ):
        self.root_path = Path(root_path)
        self.root_object_path = Path(root_object_path)
        self.root_object_type = root_object_type

        # Where composed overlays' object metadata is recorded:
        # "workspace" (ledger at <ws>/Overlays/<name>/), "object"
        # (inside the extended object), or "off"
        self.attributions = attributions

        # Resolved objects: name → (source_path, object_type)
        self.resolved_objects: dict[str, tuple[Path, ObjectType]] = {}

        # Overlays to apply: list of (overlay_path, precedence, extends_name)
        # extends_name is the base object name the overlay modifies (or None)
        self.overlays: list[tuple[Path, int, str | None]] = []

        # Files that were linked: workspace_abs_path → source_abs_path
        self.linked_files: dict[Path, Path] = {}

        # File ownership: workspace-relative POSIX path → object name
        self.file_owners: dict[str, str] = {}

        # Excluded patterns (gitignore style)
        self.exclude_patterns: list[str] = [
            ".git",
            ".git/**",
            "__pycache__",
            "**/__pycache__",
            "*.pyc",
            "build/**",
            "Cache/**",
            "*.log",
        ]

    # -- public API ----------------------------------------------------------

    def add_resolved_object(
        self, name: str, path: Path, object_type: ObjectType,
    ) -> None:
        """Add a resolved object to include in the workspace."""
        self.resolved_objects[name] = (Path(path), object_type)

    def add_overlay(
        self, path: Path, precedence: int = 0, extends: str | None = None,
    ) -> None:
        """Add an overlay to apply during workspace creation.

        Args:
            path: Path to overlay source directory.
            precedence: Lower = applied first.
            extends: Base object name this overlay modifies.  When set,
                     overlay files also replace matching symlinks inside
                     the base object's workspace tree.
        """
        self.overlays.append((Path(path), precedence, extends))
        self.overlays.sort(key=lambda x: x[1])

    def create(
        self,
        clean: bool = False,
        progress_callback: Optional[Callable[[str, int, int], None]] = None,
    ) -> "Workspace":
        """Create the workspace directory tree with symlinked files.

        Returns self for chaining.
        """
        if self.root_path.exists():
            if clean:
                logger.info(f"Cleaning existing workspace: {self.root_path}")
                shutil.rmtree(self.root_path)
            else:
                raise WorkspaceError(
                    f"Workspace path already exists: {self.root_path}"
                )

        # Create root and top-level type folders
        self.root_path.mkdir(parents=True, exist_ok=True)
        for folder in _TYPE_FOLDERS.values():
            (self.root_path / folder).mkdir(exist_ok=True)

        # Count total files for progress
        total_files = 0
        for _name, (obj_path, _otype) in self.resolved_objects.items():
            total_files += sum(1 for f in obj_path.rglob("*") if f.is_file())
        current = 0

        # Link each resolved object into its type folder
        for obj_name, (obj_path, obj_type) in self.resolved_objects.items():
            if progress_callback:
                progress_callback(f"Linking {obj_name}", current, total_files)

            folder = _TYPE_FOLDERS.get(obj_type, "Gems")
            # Derive the short directory name from the object name
            short_name = _short_name(obj_name)
            dest_root = self.root_path / folder / short_name

            current = self._link_object_files(
                source_root=obj_path,
                dest_root=dest_root,
                owner_name=obj_name,
                current=current,
                total=total_files,
                progress_callback=progress_callback,
                skip_topdirs=(
                    _ENGINE_SKIP_TOPDIRS
                    if obj_type == ObjectType.ENGINE else None
                ),
            )

        # Apply overlays in precedence order
        for overlay_path, precedence, extends_name in self.overlays:
            if progress_callback:
                progress_callback(
                    f"Applying overlay (precedence {precedence})",
                    current, total_files,
                )
            overlay_name = _object_name_from_path(overlay_path, overlay_path.name)

            # Resolve the base object's workspace tree — overlay files are
            # composed INTO the extended object (no separate Overlays dir)
            base_dest_root: Path | None = None
            if extends_name and extends_name in self.resolved_objects:
                base_path, base_type = self.resolved_objects[extends_name]
                base_folder = _TYPE_FOLDERS.get(base_type, "Gems")
                base_short = _short_name(extends_name)
                base_dest_root = self.root_path / base_folder / base_short

            if base_dest_root is None:
                logger.warning(
                    f"Overlay {overlay_name} extends "
                    f"{extends_name or '<unknown>'} which is not in this "
                    f"workspace — skipping"
                )
                continue

            self._apply_overlay(
                overlay_path,
                owner_name=overlay_name,
                base_dest_root=base_dest_root,
                attributions=self.attributions,
            )

        if progress_callback:
            progress_callback("Complete", total_files, total_files)

        logger.info(
            f"Workspace created: {self.root_path} "
            f"({len(self.linked_files)} files)"
        )
        return self

    # -- internal helpers ----------------------------------------------------

    def _link_object_files(
        self,
        source_root: Path,
        dest_root: Path,
        owner_name: str,
        current: int = 0,
        total: int = 0,
        progress_callback: Optional[Callable] = None,
        skip_topdirs: Optional[set[str]] = None,
    ) -> int:
        """Mirror *source_root* into *dest_root*: real dirs, symlinked files.

        An object's subtree stops at any NESTED OBJECT boundary: a
        subdirectory that is itself the root of another resolved object
        (has its own object json) is composed separately at workspace
        level and must not be duplicated inside its parent's tree.

        Args:
            skip_topdirs: Top-level directory names under *source_root* to
                skip entirely (used for engine-internal Gems/Templates/etc.
                that are composed into the workspace as separate objects).
        """
        # Roots of all OTHER resolved objects that live inside this
        # object's source tree — linking stops at these boundaries.
        nested_roots: set[Path] = set()
        source_resolved = source_root.resolve()
        for _other_name, (other_path, _t) in self.resolved_objects.items():
            other_resolved = Path(other_path).resolve()
            if other_resolved != source_resolved and source_resolved in other_resolved.parents:
                nested_roots.add(other_resolved)

        def _in_nested_root(path: Path) -> bool:
            if not nested_roots:
                return False
            resolved = path.resolve()
            return any(
                resolved == root or root in resolved.parents
                for root in nested_roots
            )

        for entry in source_root.rglob("*"):
            relative = entry.relative_to(source_root)
            if skip_topdirs and relative.parts and relative.parts[0] in skip_topdirs:
                continue
            if self.should_exclude(relative):
                continue
            if _in_nested_root(entry):
                continue

            if entry.is_dir():
                # Create empty dirs so workspace mirrors full structure
                (dest_root / relative).mkdir(parents=True, exist_ok=True)
                continue

            if not entry.is_file():
                continue

            target = dest_root / relative
            self._create_link(entry, target)

            ws_rel = target.relative_to(self.root_path).as_posix()
            self.file_owners[ws_rel] = owner_name

            current += 1
            if progress_callback and current % 100 == 0:
                progress_callback("Linking files", current, total)

        return current

    # Name of the payload subfolder inside an overlay object.  Mirrors the
    # Template/ convention: object metadata (overlay.json, licenses, icon,
    # docs) lives at the object root and NEVER composes; only the payload
    # subfolder's contents are composed onto the extended object.
    OVERLAY_PAYLOAD_DIR = "Overlay"

    def _apply_overlay(
        self,
        overlay_path: Path,
        owner_name: str,
        base_dest_root: Path,
        attributions: str = "workspace",
    ) -> None:
        """Apply an overlay — compose its payload INTO the extended object.

        The overlay's ``Overlay/`` subfolder is the payload: its contents
        are linked onto the base object's workspace tree at the same
        relative paths (replacing matching files, adding new ones).  The
        composed object is indistinguishable from a monolithic one.

        Everything OUTSIDE the payload folder is object metadata
        (overlay.json, licenses, icon, docs).  Depending on *attributions*
        it is linked as a provenance/attribution record:
        - ``workspace`` (default): ``<workspace>/Overlays/<name>/``
        - ``object``: ``<base>/<name>/`` (travels with the object)
        - ``off``: not linked (provenance only in workspace meta)

        An overlay without an ``Overlay/`` payload folder is malformed
        and composes nothing (warning).
        """
        if not overlay_path.exists():
            logger.warning(f"Overlay path does not exist: {overlay_path}")
            return

        payload_root = overlay_path / self.OVERLAY_PAYLOAD_DIR
        if not payload_root.is_dir():
            logger.warning(
                f"Overlay {owner_name} has no '{self.OVERLAY_PAYLOAD_DIR}/' "
                f"payload folder — nothing to compose"
            )
            return

        # 1. Compose the payload into the base object's tree
        for overlay_file in payload_root.rglob("*"):
            if not overlay_file.is_file():
                continue
            relative = overlay_file.relative_to(payload_root)
            if self.should_exclude(relative):
                continue

            base_target = base_dest_root / relative
            if base_target.exists() or base_target.is_symlink():
                logger.debug(f"Overlay replacing in base: {relative}")
            else:
                base_target.parent.mkdir(parents=True, exist_ok=True)
            self._force_link(overlay_file, base_target)
            base_ws_rel = base_target.relative_to(self.root_path).as_posix()
            self.file_owners[base_ws_rel] = owner_name

        # 2. Link object metadata as the attribution record
        if attributions not in ("off", ""):
            if attributions == "object":
                attr_root = base_dest_root / owner_name
            else:
                attr_root = self.root_path / "Overlays" / owner_name
            for meta_file in overlay_path.rglob("*"):
                if not meta_file.is_file():
                    continue
                relative = meta_file.relative_to(overlay_path)
                if relative.parts[0] == self.OVERLAY_PAYLOAD_DIR:
                    continue
                if self.should_exclude(relative):
                    continue
                target = attr_root / relative
                self._force_link(meta_file, target)
                ws_rel = target.relative_to(self.root_path).as_posix()
                self.file_owners[ws_rel] = owner_name

    def _create_link(self, source: Path, target: Path) -> None:
        """Create a symbolic link from *target* → *source*."""
        target.parent.mkdir(parents=True, exist_ok=True)

        if target.exists() or target.is_symlink():
            return

        try:
            try:
                rel_source = os.path.relpath(source, target.parent)
                target.symlink_to(rel_source)
            except ValueError:
                # Different drives on Windows
                target.symlink_to(source)
            self.linked_files[target] = source
        except OSError as e:
            if "privilege" in str(e).lower():
                logger.debug(f"Symlink failed, trying hard link: {target}")
                os.link(source, target)
                self.linked_files[target] = source
            else:
                raise WorkspaceError(
                    f"Failed to create link {target} -> {source}: {e}"
                )

    def _force_link(self, source: Path, target: Path) -> None:
        """Create a symbolic link, removing any existing file first."""
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            target.unlink()
        try:
            try:
                rel_source = os.path.relpath(source, target.parent)
                target.symlink_to(rel_source)
            except ValueError:
                target.symlink_to(source)
            self.linked_files[target] = source
        except OSError as e:
            if "privilege" in str(e).lower():
                logger.debug(f"Symlink failed, trying hard link: {target}")
                os.link(source, target)
                self.linked_files[target] = source
            else:
                raise WorkspaceError(
                    f"Failed to create link {target} -> {source}: {e}"
                )

    def should_exclude(self, relative_path: Path) -> bool:
        """Check if a file should be excluded from workspace."""
        path_str = str(relative_path).replace("\\", "/")
        for pattern in self.exclude_patterns:
            if self._matches_pattern(path_str, pattern):
                return True
        return False

    def _matches_pattern(self, path: str, pattern: str) -> bool:
        """Simple pattern matching (supports * and **)."""
        import fnmatch
        if "**" in pattern:
            parts = pattern.split("**")
            if len(parts) == 2:
                prefix, suffix = parts
                if prefix and not path.startswith(prefix.rstrip("/")):
                    return False
                if suffix and not path.endswith(suffix.lstrip("/")):
                    return False
                return True
        return fnmatch.fnmatch(path, pattern)

    def update(self) -> None:
        """Re-check links and re-apply overlays."""
        if not self.root_path.exists():
            raise WorkspaceError(
                f"Workspace does not exist: {self.root_path}"
            )
        broken = []
        for link_path, source_path in self.linked_files.items():
            if link_path.is_symlink() and not link_path.resolve().exists():
                broken.append(link_path)
        if broken:
            logger.warning(f"Found {len(broken)} broken links")
            for link in broken:
                link.unlink()
                del self.linked_files[link]
        for overlay_path, _precedence, extends_name in self.overlays:
            overlay_name = _object_name_from_path(overlay_path, overlay_path.name)

            base_dest_root: Path | None = None
            if extends_name and extends_name in self.resolved_objects:
                base_path, base_type = self.resolved_objects[extends_name]
                base_folder = _TYPE_FOLDERS.get(base_type, "Gems")
                base_short = _short_name(extends_name)
                base_dest_root = self.root_path / base_folder / base_short

            if base_dest_root is None:
                logger.warning(
                    f"Overlay {overlay_name} extends "
                    f"{extends_name or '<unknown>'} which is not in this "
                    f"workspace — skipping"
                )
                continue

            self._apply_overlay(
                overlay_path, owner_name=overlay_name,
                base_dest_root=base_dest_root,
                attributions=self.attributions,
            )

    def get_stats(self) -> dict:
        """Get workspace statistics."""
        return {
            "root_path": str(self.root_path),
            "root_object": str(self.root_object_path),
            "total_files": len(self.linked_files),
            "resolved_objects": len(self.resolved_objects),
            "overlays": len(self.overlays),
        }

    def get_file_links(self) -> dict[str, str]:
        """Return ``{source_abs_posix: dest_rel_posix}`` from linked_files."""
        result: dict[str, str] = {}
        for dest_abs, src_abs in self.linked_files.items():
            src_posix = src_abs.as_posix()
            dest_rel = dest_abs.relative_to(self.root_path).as_posix()
            result[src_posix] = dest_rel
        return result

    def get_sources_dict(self) -> dict[str, dict[str, str]]:
        """Return categorised ``{type: {name: path}}`` from resolved_objects + overlays."""
        from o3de_cli.core.models import ObjectType as OT
        _type_key = {
            OT.ENGINE: "engines", OT.PROJECT: "projects",
            OT.GEM: "gems", OT.TEMPLATE: "templates",
        }
        cats: dict[str, dict[str, str]] = {
            "engines": {}, "projects": {}, "gems": {},
            "templates": {}, "overlays": {},
        }
        for name, (path, obj_type) in self.resolved_objects.items():
            key = _type_key.get(obj_type, "gems")
            cats[key][name] = str(path)
        for ov_path, _prec, _ext in self.overlays:
            ov_name = _object_name_from_path(ov_path, ov_path.name)
            cats["overlays"][ov_name] = str(ov_path)
        return cats


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _short_name(object_name: str) -> str:
    """Derive a short directory name from a reverse-domain object name.

    ``org.o3de.engine.o3de`` → ``o3de``
    ``com.example.gem.atom``  → ``atom``
    Plain names pass through unchanged.
    """
    if "." in object_name:
        return object_name.rsplit(".", 1)[-1]
    return object_name


def _overlay_extends_name(overlay_path: Path) -> str | None:
    """Read the base object name from an overlay's ``extends`` field.

    Strips any version specifier: ``org.o3de.gem.foo>=1.0.0`` → ``org.o3de.gem.foo``.
    """
    import re

    for candidate in (overlay_path / "overlay.2-0-0.json", overlay_path / "overlay.json"):
        if not candidate.exists():
            continue
        try:
            with open(candidate, encoding="utf-8-sig") as f:
                data = json.load(f)
            extends = data.get("extends", "")
            if extends:
                return re.split(r"[><=~!\s]", extends, 1)[0]
        except Exception:
            continue
    return None


def _object_name_from_path(path: Path, fallback: str) -> str:
    """Read the canonical object name from an object directory's JSON.

    Checks 2.0.0 sidecars first, then native/legacy JSON files.  Returns
    *fallback* when no readable object JSON is found.  This matters for
    installed objects that live in ``<name>/<version>/`` folders, where
    the directory name (e.g. ``1.0.0``) is not the object name.
    """
    from .models import get_object_name

    for suffix in ("2-0-0.json", "json"):
        for obj_type in ("engine", "project", "gem", "template", "overlay", "repo"):
            candidate = path / f"{obj_type}.{suffix}"
            if not candidate.exists():
                continue
            try:
                with open(candidate, encoding="utf-8-sig") as f:
                    data = json.load(f)
                name = get_object_name(data)
                if name:
                    return name
            except Exception:
                continue
    return fallback


def detect_root_type(path: Path) -> ObjectType:
    """Detect the object type of a root directory.

    Checks Schema 2.0 sidecars first, then falls back to legacy JSON files.

    Raises:
        WorkspaceError: if no object JSON can be found.
    """
    # Schema 2.0 sidecars (preferred), then legacy/native JSON files.
    # gem.json/overlay.json may be either 2.0.0-native or legacy.
    for suffix in ("2-0-0.json", "json"):
        for obj_type in (ObjectType.ENGINE, ObjectType.PROJECT, ObjectType.GEM,
                         ObjectType.TEMPLATE, ObjectType.OVERLAY, ObjectType.REPO):
            if (path / f"{obj_type.value}.{suffix}").exists():
                return obj_type
    raise WorkspaceError(
        f"Cannot determine root object type at: {path}"
    )


def create_workspace(
    target_path: Path,
    root_object_path: Path,
    resolved_objects: dict[str, tuple[Path, ObjectType]],
    overlays: list[tuple[Path, int, str | None]] | list[tuple[Path, int]] | None = None,
    clean: bool = False,
    progress_callback: Optional[Callable[[str, int, int], None]] = None,
    attributions: str = "workspace",
) -> Workspace:
    """Convenience function to create a workspace.

    Args:
        target_path: Where to create the workspace.
        root_object_path: Path to the root engine or project.
        resolved_objects: name → (path, ObjectType) for every dependency.
        overlays: (overlay_path, precedence[, extends_name]) tuples.
        clean: Remove existing workspace first.
        progress_callback: Optional progress callback.
        attributions: Where composed overlays' metadata is recorded
            ("workspace", "object", or "off").

    Returns:
        Created Workspace object.
    """
    root_type = detect_root_type(root_object_path)

    ws = Workspace(target_path, root_object_path, root_type,
                   attributions=attributions)

    # Determine a name for the root object (read from its object JSON;
    # the directory name may be a version folder like "1.0.0")
    root_name = _object_name_from_path(root_object_path, root_object_path.name)
    ws.add_resolved_object(root_name, root_object_path, root_type)

    for name, (path, obj_type) in resolved_objects.items():
        ws.add_resolved_object(name, path, obj_type)

    if overlays:
        for entry in overlays:
            if len(entry) == 3:
                overlay_path, precedence, extends = entry
            else:
                overlay_path, precedence = entry  # type: ignore[misc]
                extends = None
            if extends is None:
                extends = _overlay_extends_name(Path(overlay_path))
            ws.add_overlay(overlay_path, precedence, extends=extends)

    ws = ws.create(clean=clean, progress_callback=progress_callback)

    # Register the workspace in the global manifest so the GUI
    # Workspaces tab (and any other consumer) can discover it.
    _register_in_manifest(target_path)

    return ws


def _register_in_manifest(ws_path: Path) -> None:
    """Append *ws_path* to the manifest's ``workspaces`` list (idempotent)."""
    try:
        from .paths import get_manifest_path
        manifest_path = get_manifest_path()
        with open(manifest_path) as f:
            manifest = json.load(f)
        workspaces = manifest.setdefault("workspaces", [])
        path_str = ws_path.resolve().as_posix()
        if path_str not in workspaces:
            workspaces.append(path_str)
            with open(manifest_path, "w") as f:
                json.dump(manifest, f, indent=2)
    except Exception:
        logger.warning("Failed to register workspace in manifest", exc_info=True)


# Backward-compatible aliases
Layout = Workspace
create_layout = create_workspace
