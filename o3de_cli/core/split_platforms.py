# O3DE CLI - Split an object's PAL platform directories into overlays
# SPDX-License-Identifier: Apache-2.0 OR MIT

"""Planning and execution for ``o3de object split-platforms``.

The command is split in two phases:

* :func:`plan_split` inspects the object (read-only) and returns a
  :class:`SplitPlan` describing every overlay that would be created.
  ``--dry-run`` renders this plan and stops.
* :func:`create_overlays`, :func:`remove_split_dirs` and
  :func:`register_overlays` carry the plan out.

The object-inspection and metadata helpers here (:func:`load_object_json`,
:func:`find_platform_dirs`, :func:`split_overlay_name`,
:func:`build_overlay_meta`, ...) are shared with ``object hoist``.

Nothing in this module depends on Click or Rich; failures are raised as
:class:`ObjectOpError` carrying the CLI error code.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from o3de_cli.core.models import get_object_name, get_object_version

#: Directories never searched for ``Platform/`` payloads.
SPLIT_SKIP_DIRS = {
    ".git",
    ".svn",
    ".vs",
    "__pycache__",
    "build",
    "Cache",
    "External",
    "node_modules",
    "user",
}

#: Directory name that marks a common (platform-agnostic fallback) payload.
COMMON_PLATFORM = "Common"

#: Object type tokens recognised in object names and JSON file names.
_TYPE_TOKENS = ("gem", "project", "engine", "template")


class ObjectOpError(Exception):
    """A split/hoist failure, carrying the CLI error code (``E_*``)."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


# ── Object inspection ───────────────────────────────────────────────


def load_object_json(object_path: Path) -> tuple[dict, str]:
    """Load the object JSON at *object_path*.

    Returns (data, type_token).  Prefers 2.0.0 sidecars.  Raises
    ``FileNotFoundError`` when no object JSON exists, and
    :class:`ObjectOpError` (``E_INVALID_OBJECT``) when the file found is
    not valid JSON or not a JSON object.
    """
    for suffix in ("2-0-0.json", "json"):
        for type_token in ("engine", "project", "gem", "template"):
            candidate = object_path / f"{type_token}.{suffix}"
            if candidate.exists():
                try:
                    with open(candidate, encoding="utf-8-sig") as f:
                        data = json.load(f)
                except (ValueError, OSError) as e:  # JSONDecodeError, UnicodeDecodeError
                    raise ObjectOpError(
                        f"Invalid object JSON {candidate}: {e}", "E_INVALID_OBJECT"
                    ) from None
                if not isinstance(data, dict):
                    raise ObjectOpError(
                        f"Invalid object JSON {candidate}: expected a JSON object, "
                        f"got {type(data).__name__}",
                        "E_INVALID_OBJECT",
                    )
                return data, type_token
    raise FileNotFoundError(f"No engine/project/gem/template JSON at: {object_path}")


def find_platform_dirs(object_root: Path) -> dict[str, list[Path]]:
    """Find PAL ``Platform/<Name>`` directories under *object_root*.

    Returns platform name → list of absolute platform directories.
    Walks the tree skipping VCS/build folders; every child directory of
    a directory literally named ``Platform`` is a platform payload.
    """
    found: dict[str, list[Path]] = {}

    def walk(directory: Path) -> None:
        for child in sorted(directory.iterdir()):
            if not child.is_dir() or child.name in SPLIT_SKIP_DIRS:
                continue
            if child.name == "Platform":
                for plat_dir in sorted(child.iterdir()):
                    if plat_dir.is_dir():
                        found.setdefault(plat_dir.name, []).append(plat_dir)
            else:
                walk(child)

    walk(object_root)
    return found


def check_overlay_name_clash(detected: dict[str, list[Path]], obj_root: Path) -> None:
    """Refuse platform names that differ only in case.

    Overlay names and directories use the lower-cased platform name, so
    ``Platform/Windows`` and ``Platform/windows`` would land in the same
    overlay.  Raises :class:`ObjectOpError` (``E_OVERLAY_NAME_CLASH``)
    naming the clashing directories, relative to *obj_root*.
    """
    by_key: dict[str, list[str]] = {}
    for name in sorted(detected):
        by_key.setdefault(name.lower(), []).append(name)
    clashes = [names for names in by_key.values() if len(names) > 1]
    if not clashes:
        return
    groups = [
        ", ".join(str(d.relative_to(obj_root)) for name in names for d in detected[name])
        for names in clashes
    ]
    raise ObjectOpError(
        "Platform directories differ only in case and would share an overlay: " + "; ".join(groups),
        "E_OVERLAY_NAME_CLASH",
    )


def _resolves_inside(path: Path, root: Path) -> bool:
    """True when *path* resolves (following symlinks) to *root* or below it."""
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def object_licenses(obj_data: dict, root: Path, *, warnings: list[str] | None = None) -> list[dict]:
    """The object's ``licenses`` entries whose ``relative_path`` file exists under *root*.

    ``relative_path`` licenses are object-root metadata, so only entries
    whose file is actually present travel with a split-out payload.  An
    entry whose path resolves outside *root* (``..``, an absolute path,
    a symlink out) is dropped, and a message appended to *warnings*.
    """
    licenses = []
    for lic in obj_data.get("licenses", []):
        rel = lic.get("relative_path")
        if not rel:
            continue
        if not _resolves_inside(root / rel, root):
            if warnings is not None:
                warnings.append(
                    f"Skipped license {rel!r}: its path resolves outside the object ({root})"
                )
            continue
        if (root / rel).is_file():
            licenses.append(lic)
    return licenses


def copy_licenses(licenses: list[dict], src_root: Path, dst_root: Path) -> list[str]:
    """Copy each license file from *src_root* to the same relative path under *dst_root*.

    A license whose source or destination resolves outside its root is
    skipped; returns a warning message for each one skipped.
    """
    warnings: list[str] = []
    for lic in licenses:
        rel = lic["relative_path"]
        src, dst = src_root / rel, dst_root / rel
        if not (_resolves_inside(src, src_root) and _resolves_inside(dst, dst_root)):
            warnings.append(
                f"Skipped license {rel!r}: its path resolves outside {src_root} or {dst_root}"
            )
            continue
        dst = dst.resolve()
        # A license may live in a subdirectory (e.g. ``Licenses/MIT.txt``)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    return warnings


# ── Naming and option parsing ───────────────────────────────────────


def split_overlay_name(base_name: str, suffix: str) -> str:
    """Derive an overlay name from *base_name* and a platform *suffix*.

    ``org.o3de.gem.achievementstest`` + ``windows`` →
    ``org.o3de.overlay.achievementstest.windows``.  Falls back to
    appending ``.overlay`` when no type token is present.
    """
    parts = base_name.split(".")
    for i, part in enumerate(parts):
        if part in _TYPE_TOKENS:
            parts[i] = "overlay"
            return ".".join(parts) + f".{suffix.lower()}"
    return base_name + f".overlay.{suffix.lower()}"


def family_repo_name(base_name: str) -> str:
    """Derive a family repo object name from an object name.

    ``org.o3de.gem.achievements`` → ``org.o3de.repo.achievements``.
    """
    parts = base_name.split(".")
    for i, part in enumerate(parts):
        if part in _TYPE_TOKENS:
            parts[i] = "repo"
            return ".".join(parts)
    return base_name + ".repo"


def parse_user_tags(values: Iterable[str]) -> list[str]:
    """Flatten repeatable/comma-separated ``--tags`` values, de-duplicated in order."""
    user_tags: list[str] = []
    for value in values:
        for token in value.split(","):
            token = token.strip()
            if token and token not in user_tags:
                user_tags.append(token)
    return user_tags


def parse_platform_selection(values: Iterable[str]) -> list[str]:
    """Flatten repeatable/comma-separated ``--platforms`` values (order kept, no de-dup)."""
    selected: list[str] = []
    for value in values:
        for token in value.split(","):
            token = token.strip()
            if token:
                selected.append(token)
    return selected


def select_platforms(detected: dict[str, list[Path]], selected: list[str]) -> dict[str, list[Path]]:
    """Restrict *detected* platforms to *selected* (case-insensitive).

    ``Common`` is always kept, because the platform overlays depend on
    it.  Raises :class:`ObjectOpError` (``E_PLATFORM_NOT_FOUND``) when a
    selected platform is not present.
    """
    unknown = [p for p in selected if p.lower() not in {d.lower() for d in detected}]
    if unknown:
        raise ObjectOpError(
            f"Platforms not present in the object: {', '.join(unknown)} "
            f"(detected: {', '.join(sorted(detected))})",
            "E_PLATFORM_NOT_FOUND",
        )
    keep = {p.lower() for p in selected}
    return {
        name: dirs
        for name, dirs in detected.items()
        if name.lower() in keep or name == COMMON_PLATFORM
    }


def platform_order(platforms: Iterable[str]) -> list[str]:
    """Sort platform names with ``Common`` first (it is the dependency target)."""
    return sorted(platforms, key=lambda n: (n != COMMON_PLATFORM, n))


# ── Overlay metadata ────────────────────────────────────────────────


def build_overlay_meta(
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
        "$schema": "https://raw.githubusercontent.com/accesspointmg/canonical.o3de.org/main/src/o3de-overlay-2.0.0.json",
        "overlay": {
            "name": ov_name,
            "version": ov_version,
            "display_name": f"{display_base} {plat}",
            "description": (
                f"Shared platform-common payload for the {display_base} "
                f"{type_token}. Platform overlays depend on this overlay."
                if is_common
                else f"{plat} platform delivery for the {display_base} "
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


def write_json_file(path: Path, data: Any) -> None:
    """Write *data* as indented JSON with a trailing newline."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def ensure_manifest() -> Path | None:
    """Create the user manifest if it does not exist yet.

    Writes the same skeleton ``o3de manifest add`` starts from
    (:func:`o3de_cli.core.paths.new_manifest_data`).  Returns the path
    when it was created, ``None`` when a manifest already existed.
    """
    # Imported here so tests can patch ``o3de_cli.core.paths.get_manifest_path``
    from o3de_cli.core.paths import get_manifest_path, new_manifest_data

    manifest_path = get_manifest_path()
    if manifest_path.exists():
        return None
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(new_manifest_data(), f, indent=2)
    return manifest_path


def register_local_paths(kind: str, paths: list[str]) -> list[str] | None:
    """Append *paths* to ``local.<kind>`` in the user manifest.

    Returns the paths that were newly added, or ``None`` when there is
    no manifest to register into (call :func:`ensure_manifest` first to
    create one).  The manifest is rewritten whenever it exists, even if
    nothing was added.
    """
    # Imported here so tests can patch ``o3de_cli.core.paths.get_manifest_path``
    from o3de_cli.core.paths import get_manifest_path

    manifest_path = get_manifest_path()
    if not (manifest_path and manifest_path.exists()):
        return None
    with open(manifest_path, encoding="utf-8-sig") as f:
        manifest = json.load(f)
    section = manifest.setdefault("local", {})
    entries = section.setdefault(kind, [])
    added: list[str] = []
    for path_str in paths:
        if path_str not in entries:
            entries.append(path_str)
            added.append(path_str)
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    return added


# ── split-platforms plan ────────────────────────────────────────────


@dataclass
class SplitOverlay:
    """One overlay a split would create."""

    platform: str
    name: str
    path: Path
    sources: list[Path]
    files: int
    is_common: bool

    def plan_entry(self) -> dict:
        """The ``--dry-run`` JSON entry for this overlay."""
        return {
            "platform": self.platform,
            "overlay": self.name,
            "path": str(self.path),
            "files": self.files,
            "common": self.is_common,
        }


@dataclass
class SplitPlan:
    """Everything ``split-platforms`` would do, computed without writing anything."""

    obj_root: Path
    type_token: str
    base_name: str
    base_version: str
    ov_version: str
    out_dir: Path
    precedence: int
    origin: dict | None
    licenses: list[dict]
    user_tags: list[str]
    common_name: str | None
    overlays: list[SplitOverlay] = field(default_factory=list)
    #: Platform-specific platforms detected but not selected for this split
    remaining_platforms: list[str] = field(default_factory=list)
    #: Problems that do not stop the split (e.g. skipped licenses)
    warnings: list[str] = field(default_factory=list)

    @property
    def removes_common_source(self) -> bool:
        """Whether ``--remove`` may delete the source's ``Platform/Common`` dirs.

        Only when no platform-specific directory stays behind: the
        platforms left in the tree may still depend on the common code.
        """
        return not self.remaining_platforms

    def kept_common_dirs(self) -> list[str]:
        """Common dirs ``--remove`` leaves in the source, relative to the object root."""
        if self.removes_common_source:
            return []
        return [
            str(d.relative_to(self.obj_root))
            for ov in self.overlays
            if ov.is_common
            for d in ov.sources
        ]

    def dry_run_payload(self) -> dict:
        """The ``--dry-run --json`` payload."""
        return {"object": self.base_name, "overlays": [ov.plan_entry() for ov in self.overlays]}


def plan_split(
    obj_root: Path,
    obj_data: dict,
    type_token: str,
    *,
    platforms: Iterable[str] = (),
    output: str | None = None,
    overlay_version: str | None = None,
    tags: Iterable[str] = (),
    precedence: int = 10,
) -> SplitPlan:
    """Plan a split of *obj_root*'s ``Platform/<Name>`` dirs into overlays.

    Reads the tree but writes nothing.  Raises :class:`ObjectOpError`
    with ``E_NO_PLATFORM_DIRS``, ``E_PLATFORM_NOT_FOUND`` or
    ``E_OVERLAY_NAME_CLASH``.
    """
    base_name = get_object_name(obj_data) or obj_root.name
    base_version = get_object_version(obj_data)

    all_detected = find_platform_dirs(obj_root)
    if not all_detected:
        raise ObjectOpError(
            f"No Platform/<Name> directories found in {obj_root}", "E_NO_PLATFORM_DIRS"
        )
    detected = all_detected
    if platforms:
        detected = select_platforms(all_detected, parse_platform_selection(platforms))
    check_overlay_name_clash(detected, obj_root)

    out_dir = Path(output).resolve() if output else obj_root.parent
    common_name = split_overlay_name(base_name, "common") if COMMON_PLATFORM in detected else None
    warnings: list[str] = []

    plan = SplitPlan(
        obj_root=obj_root,
        type_token=type_token,
        base_name=base_name,
        base_version=base_version,
        ov_version=overlay_version or base_version or "1.0.0",
        out_dir=out_dir,
        precedence=precedence,
        origin=obj_data.get("origin"),
        licenses=object_licenses(obj_data, obj_root, warnings=warnings),
        user_tags=parse_user_tags(tags),
        common_name=common_name,
        remaining_platforms=sorted(
            p for p in all_detected if p not in detected and p != COMMON_PLATFORM
        ),
        warnings=warnings,
    )
    for plat in platform_order(detected):
        ov_name = split_overlay_name(base_name, plat)
        sources = detected[plat]
        files = [f for d in sources for f in sorted(d.rglob("*")) if f.is_file()]
        plan.overlays.append(
            SplitOverlay(
                platform=plat,
                name=ov_name,
                path=out_dir / ov_name,
                sources=sources,
                files=len(files),
                is_common=plat == COMMON_PLATFORM,
            )
        )
    return plan


# ── split-platforms execution ───────────────────────────────────────


def create_overlays(plan: SplitPlan) -> list[dict]:
    """Create every planned overlay object; returns ``{platform, overlay, path}`` entries.

    Refuses (``E_OVERLAY_EXISTS``) before writing anything when any
    overlay path already exists.  A license that cannot be copied safely
    is skipped and noted in ``plan.warnings``.
    """
    for ov in plan.overlays:
        if ov.path.exists():
            raise ObjectOpError(f"Overlay path already exists: {ov.path}", "E_OVERLAY_EXISTS")

    created: list[dict] = []
    for ov in plan.overlays:
        payload_root = ov.path / "Overlay"
        # Copy platform payload preserving the object-relative layout
        for plat_dir in ov.sources:
            shutil.copytree(plat_dir, payload_root / plat_dir.relative_to(plan.obj_root))
        for warning in copy_licenses(plan.licenses, plan.obj_root, ov.path):
            if warning not in plan.warnings:
                plan.warnings.append(warning)

        meta = build_overlay_meta(
            ov_name=ov.name,
            plat=ov.platform,
            is_common=ov.is_common,
            base_name=plan.base_name,
            base_version=plan.base_version,
            ov_version=plan.ov_version,
            type_token=plan.type_token,
            precedence=plan.precedence,
            origin=plan.origin,
            licenses=plan.licenses,
            user_tags=plan.user_tags,
            common_name=plan.common_name,
        )
        write_json_file(ov.path / "overlay.json", meta)
        created.append({"platform": ov.platform, "overlay": ov.name, "path": str(ov.path)})
    return created


def remove_split_dirs(plan: SplitPlan) -> list[str]:
    """Delete the split platform dirs from the source object.

    The common dirs are deleted only when every platform-specific dir is
    split out too (:attr:`SplitPlan.removes_common_source`); otherwise
    they stay for the platforms left behind (see
    :meth:`SplitPlan.kept_common_dirs`).  Prunes a ``Platform`` parent
    left empty.  Returns the removed dirs, relative to the object root.
    """
    removed: list[str] = []
    for ov in plan.overlays:
        if ov.is_common and not plan.removes_common_source:
            continue
        for plat_dir in ov.sources:
            shutil.rmtree(plat_dir)
            removed.append(str(plat_dir.relative_to(plan.obj_root)))
            parent = plat_dir.parent
            if parent.name == "Platform" and not any(parent.iterdir()):
                parent.rmdir()
    return removed


def register_overlays(created: list[dict]) -> list[str]:
    """Register created overlays under ``local.overlays``; returns the names newly registered."""
    added = register_local_paths("overlays", [Path(e["path"]).as_posix() for e in created])
    if not added:
        return []
    return [e["overlay"] for e in created if Path(e["path"]).as_posix() in added]
