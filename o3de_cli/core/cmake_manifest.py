# O3DE Pilot - Workspace CMake Manifest Generator
# SPDX-License-Identifier: Apache-2.0 OR MIT

"""
Generates a workspace-scoped ``resolved_o3de_manifest.json`` in the format
consumed by the engine's ``cmake/Manifest.cmake``.

Design: dependency resolution happens ONCE, at workspace compose time
(the solver).  The workspace is the realized solution — so the resolved
manifest written here simply *records* the composed tree.  CMake never
re-resolves; ``find_package`` degrades into a table lookup because:

- ``all_<type>_paths`` point at object JSONs INSIDE the workspace
  (``<ws>/Engines/o3de/engine.json``, ``<ws>/Gems/<gem>/gem.json``, ...)
- each path has a per-path JSON blob with the flattened object fields
  that Manifest.cmake reads into ``O3DE_PATH_<path>_*`` globals
- object directories land on ``CMAKE_PREFIX_PATH`` so the per-object
  ``<name>Config.cmake`` files resolve to workspace copies

The user-level ``~/.o3de/resolved_o3de_manifest.json`` remains in use for
source-tree (non-workspace) builds and is produced by the engine's own
``o3de resolve``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("o3de_cli.cmake_manifest")

CMAKE_MANIFEST_FILENAME = "resolved_o3de_manifest.json"

# workspace folder name → manifest type key fragment
_FOLDER_TO_TYPE = {
    "Engines": "engine",
    "Projects": "project",
    "Gems": "gem",
    "Templates": "template",
    "Overlays": "overlay",
}

# type → object json filenames, versioned preferred
_TYPE_JSON = {
    "engine": ("engine.2-0-0.json", "engine.json"),
    "project": ("project.2-0-0.json", "project.json"),
    "gem": ("gem.2-0-0.json", "gem.json"),
    "template": ("template.2-0-0.json", "template.json"),
    "overlay": ("overlay.2-0-0.json", "overlay.json"),
}


def _find_object_json(obj_dir: Path, type_key: str) -> Path | None:
    for name in _TYPE_JSON.get(type_key, ()):
        p = obj_dir / name
        if p.exists():
            return p
    return None


def _legacy_json_path(obj_dir: Path, type_key: str) -> Path:
    """The path key CMake uses — always the unversioned filename."""
    return obj_dir / _TYPE_JSON[type_key][1]


def _load_object(json_path: Path) -> dict:
    try:
        with open(json_path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"Failed to read {json_path}: {e}")
        return {}


def _header(data: dict, type_key: str) -> dict:
    """Return the Schema 2.0 header dict (or synthesize from legacy)."""
    hdr = data.get(type_key)
    if isinstance(hdr, dict):
        return hdr
    # Legacy fallbacks
    name = (
        data.get(f"{type_key}_name")
        or data.get("gem_name")
        or data.get("project_name")
        or data.get("engine_name")
        or ""
    )
    return {
        "name": name,
        "version": data.get("version", data.get("O3DEVersion", "0.0.0")),
        "display_name": data.get("display_name", ""),
        "description": data.get("summary", ""),
        "type": data.get("type", ""),
        "id": data.get("id", ""),
        "copyright_year": data.get("copyright_year", ""),
        "copyright_text": data.get("copyright_text", ""),
    }


def _dependent_lists(data: dict, type_key: str) -> dict[str, list[str]]:
    """Extract dependent.<type> lists from schema 2.0 (or empty)."""
    dep = data.get("dependent", {})
    if not isinstance(dep, dict):
        dep = {}
    # also allow nested under the type header
    nested = data.get(type_key, {})
    if isinstance(nested, dict) and isinstance(nested.get("dependent"), dict):
        merged = dict(nested["dependent"])
        for k, v in dep.items():
            merged.setdefault(k, v)
        dep = merged
    return {
        "engines": list(dep.get("engines", [])),
        "projects": list(dep.get("projects", [])),
        "gems": list(dep.get("gems", [])),
        "templates": list(dep.get("templates", [])),
        "overlays": list(dep.get("overlays", [])),
    }


def _object_entry(
    json_path_key: str,
    data: dict,
    type_key: str,
    children: dict[str, list[str]],
) -> dict:
    """Build the per-path flattened blob Manifest.cmake expects."""
    hdr = _header(data, type_key)
    dep = _dependent_lists(data, type_key)
    api = data.get("api_versions", {})
    tags = data.get("tags", {})
    if isinstance(tags, dict):
        canonical_tags = tags.get("canonical", data.get("canonical_tags", []))
        user_tags = tags.get("user", data.get("user_tags", []))
    else:
        canonical_tags = data.get("canonical_tags", [])
        user_tags = data.get("user_tags", [])

    entry = {
        "name": hdr.get("name", ""),
        "version": hdr.get("version", "0.0.0"),
        "display_name": hdr.get("display_name", ""),
        "description": hdr.get("description", ""),
        "type": hdr.get("type", ""),
        "id": hdr.get("id", ""),
        "copyright_year": hdr.get("copyright_year", ""),
        "copyright_text": hdr.get("copyright_text", ""),

        "canonical_tags": canonical_tags,
        "user_tags": user_tags,
        "platforms": data.get("platforms", []),

        "child_engine_json_paths": children.get("engines", []),
        "child_project_json_paths": children.get("projects", []),
        "child_gem_json_paths": children.get("gems", []),
        "child_template_json_paths": children.get("templates", []),
        "child_repo_json_paths": [],

        "parent_json_paths": [],

        "dependent_engines": dep["engines"],
        "dependent_projects": dep["projects"],
        "dependent_gems": dep["gems"],
        "dependent_templates": dep["templates"],

        # Artifact form chosen for this object (workspace override):
        # "source" (default) - build from the linked source tree
        # "local-binary" / "remote-binary" - consume a prebuilt install
        #   layout via find_package at binary_config_path (CMake-side
        #   support pending)
        "artifact": "source",
        "binary_config_path": "",
    }

    if type_key == "engine":
        entry.update({
            "O3DEVersion": data.get("O3DEVersion", ""),
            "O3DEBuildNumber": data.get("O3DEBuildNumber", ""),
            "display_version": data.get("display_version", ""),
            "file_version": data.get("file_version", ""),
            "build": data.get("build", ""),
            "api_version_editor": api.get("editor", ""),
            "api_version_framework": api.get("framework", ""),
            "api_version_launcher": api.get("launcher", ""),
            "api_version_tools": api.get("tools", ""),
        })
    if type_key == "project":
        entry.update({
            "product_name": data.get("product_name", ""),
            "executable_name": data.get("executable_name", ""),
            "engine": data.get("engine", ""),
        })
    if type_key == "overlay":
        entry.update({
            "extends": data.get("extends", ""),
            "precedence": data.get("precedence", 0),
            "platform_maps": data.get("platform_maps", []),
            "platform_wart_maps": data.get("platform_wart_maps", []),
        })
    return entry


def generate_cmake_manifest(
    ws_path: Path,
    third_party_path: str = "",
    overrides: dict | None = None,
) -> Path:
    """Write ``<ws>/resolved_o3de_manifest.json`` from the composed tree.

    Scans the workspace type folders (Engines/, Projects/, Gems/,
    Templates/) for object JSONs and produces the CMake-consumable
    resolved manifest with all paths pointing into the workspace.

    Args:
        overrides: Optional {object name -> ObjectOverride-like} mapping;
            objects with a non-source artifact form get their manifest
            entry annotated with ``artifact`` and ``binary_config_path``.

    Returns the path of the written manifest.
    """
    ws_path = Path(ws_path)

    paths: dict[str, list[str]] = {
        "engine": [], "project": [], "gem": [], "template": [], "overlay": [],
    }
    names: dict[str, list[str]] = {
        "engine": [], "project": [], "gem": [], "template": [], "overlay": [],
    }
    entries: dict[str, dict] = {}

    for folder, type_key in (
        ("Engines", "engine"),
        ("Projects", "project"),
        ("Gems", "gem"),
        ("Templates", "template"),
        ("Overlays", "overlay"),
    ):
        type_dir = ws_path / folder
        if not type_dir.is_dir():
            continue
        for obj_dir in sorted(type_dir.iterdir()):
            if not obj_dir.is_dir():
                continue
            json_path = _find_object_json(obj_dir, type_key)
            if json_path is None:
                continue
            data = _load_object(json_path)
            if not data:
                continue
            hdr = _header(data, type_key)
            name = hdr.get("name", "")
            version = hdr.get("version", "0.0.0")
            if not name:
                logger.warning(f"No name in {json_path} — skipping")
                continue

            # CMake keys off the legacy (unversioned) json path
            path_key = _legacy_json_path(obj_dir, type_key).as_posix()

            # children within the workspace: none — composition already
            # flattened everything to workspace level
            entry = _object_entry(path_key, data, type_key, {})

            # Apply artifact-form override annotation
            if overrides and name in overrides:
                override = overrides[name]
                artifact = (
                    override.get("artifact") if isinstance(override, dict)
                    else getattr(override, "artifact", "source")
                ) or "source"
                override_path = (
                    override.get("path") if isinstance(override, dict)
                    else getattr(override, "path", None)
                )
                entry["artifact"] = artifact
                if artifact != "source" and override_path:
                    entry["binary_config_path"] = Path(override_path).as_posix()

            entries[path_key] = entry
            paths[type_key].append(path_key)
            names[type_key].append(f"{name}=={version}")

    resolved: dict = {
        "$comment": (
            # NOTE: no semicolons anywhere in generated content — CMake's
            # unquoted variable expansion splits arguments on ';'
            "Workspace-scoped resolved manifest. Generated at compose "
            "time by o3de-cli and consumed by cmake/Manifest.cmake. All "
            "paths point into the composed workspace. Do not edit."
        ),
        "resolved_at": datetime.now().isoformat(),
        "workspace_path": ws_path.as_posix(),
        "country_code": "",
        "default_engines_path": (ws_path / "Engines").as_posix(),
        "default_projects_path": (ws_path / "Projects").as_posix(),
        "default_gems_path": (ws_path / "Gems").as_posix(),
        "default_templates_path": (ws_path / "Templates").as_posix(),
        "default_repos_path": "",
        "default_overlays_path": (ws_path / "Overlays").as_posix(),
        "default_third_party_path": third_party_path,

        "all_engine_paths": paths["engine"],
        "all_project_paths": paths["project"],
        "all_gem_paths": paths["gem"],
        "all_template_paths": paths["template"],
        "all_repo_paths": [],
        "all_overlay_paths": paths["overlay"],

        "all_engine_names": names["engine"],
        "all_project_names": names["project"],
        "all_gem_names": names["gem"],
        "all_template_names": names["template"],
        "all_repo_names": [],
        "all_overlay_names": names["overlay"],
    }
    resolved.update(entries)

    out_path = ws_path / CMAKE_MANIFEST_FILENAME
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(resolved, f, indent=2)
    logger.info(
        f"Wrote workspace cmake manifest: {out_path} "
        f"({len(entries)} objects)"
    )
    return out_path
