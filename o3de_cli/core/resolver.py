# O3DE CLI - Manifest Resolver
# SPDX-License-Identifier: Apache-2.0 OR MIT

"""
Manifest Resolver.

The resolver:
1. Loads the manifest (o3de_manifest.json)
2. Descends all local object paths, reading their JSON
3. Resolves children from parent objects
4. Flattens everything into resolved_o3de_manifest.json

Resolution also handles:
- Dependency resolution using resolvelib (semver constraints)
- Overlay matching to base objects
- Deduplication of objects found via multiple paths
"""

import hashlib
import json
import logging
import re
from collections.abc import Callable, Container
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from o3de_cli.core.store import RemoteObject, Store

from packaging.specifiers import SpecifierSet
from packaging.version import Version

from .git_utils import (
    get_local_git_branch,
    get_local_git_remote,
)
from .models import (
    Children,
    Engine,
    Gem,
    LocalObjects,
    Manifest,
    O3DEObject,
    ObjectType,
    Overlay,
    Project,
    Remote,
    Repo,
    Template,
    get_object_name,
    get_object_type,
    get_object_version,
)
from .paths import (
    find_object_json,
    get_manifest_path,
    get_object_json_filename,
    get_resolved_manifest_path,
    get_versioned_object_json_filename,
)
from .upgrade import (
    get_schema_version,
    needs_upgrade,
    upgrade_to_latest,
)

logger = logging.getLogger("o3de_cli.resolver")


def compute_file_hash(path: Path) -> str:
    """
    Compute SHA-256 hash of a file.

    Args:
        path: Path to the file

    Returns:
        Hex digest of the file's SHA-256 hash, or empty string if file not readable
    """
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return ""


class ResolverError(Exception):
    """Error during resolution."""

    pass


class DependencyConflict:
    """Represents a version conflict between two dependency requirements."""

    def __init__(
        self,
        dependency_name: str,
        requirer_a: str,
        constraint_a: str,
        requirer_b: str,
        constraint_b: str,
        resolved_version: str | None = None,
    ):
        self.dependency_name = dependency_name
        self.requirer_a = requirer_a
        self.constraint_a = constraint_a
        self.requirer_b = requirer_b
        self.constraint_b = constraint_b
        self.resolved_version = resolved_version

    def __repr__(self) -> str:
        return (
            f"DependencyConflict({self.dependency_name}: "
            f"{self.requirer_a} wants {self.constraint_a}, "
            f"{self.requirer_b} wants {self.constraint_b})"
        )


class ObjectNameVersion:
    """Parsed object name with optional version constraint."""

    def __init__(self, specifier: str):
        """
        Parse a specifier like:
        - "org.o3de.gem.physx" (any version)
        - "org.o3de.gem.physx==1.0.0" (exact version)
        - "org.o3de.gem.physx>=1.0.0" (minimum version)
        - "org.o3de.gem.physx>=1.0.0<2.0.0" (range)
        """
        self.original = specifier

        # Try to parse version constraint
        match = re.match(
            r"^([a-z][a-z0-9_.]+)((?:==|>=|>|<=|<)[0-9.]+(?:(?:<=|<)[0-9.]+)?)?$", specifier
        )

        if match:
            self.name = match.group(1)
            version_part = match.group(2) or ""

            if version_part:
                # Convert to packaging specifier format
                # >=1.0.0<2.0.0 -> >=1.0.0,<2.0.0
                version_part = re.sub(r"(<|<=|>|>=)", r",\1", version_part).lstrip(",")
                self.specifier = SpecifierSet(version_part)
            else:
                self.specifier = SpecifierSet()  # Matches any version
        else:
            self.name = specifier
            self.specifier = SpecifierSet()

    def matches(self, version: str) -> bool:
        """Check if a version matches this constraint."""
        if not self.specifier:
            return True

        try:
            return Version(version) in self.specifier
        except Exception:
            return True  # If version is invalid, accept it

    def __repr__(self) -> str:
        if self.specifier:
            return f"{self.name}{self.specifier}"
        return self.name


class ResolvedObject:
    """A resolved O3DE object with full path and parsed data."""

    def __init__(
        self,
        path: Path,
        object_type: ObjectType,
        name: str,
        version: str,
        data: dict,
    ):
        self.path = path
        self.object_type = object_type
        self.name = name
        self.version = version
        self.data = data

        # Children discovered from this object
        self.children: list["ResolvedObject"] = []

        # Dependencies (parsed from data)
        self.dependencies: list[ObjectNameVersion] = []

        # Optional dependencies (nice-to-have, not required)
        self.optional_dependencies: list[ObjectNameVersion] = []

        # Peer dependencies (must be provided by consumer, warning if missing)
        self.peer_dependencies: list[ObjectNameVersion] = []

        # Overlays that extend this object
        self.overlays: list["ResolvedObject"] = []

        # Parent object that contains this one (set during resolution)
        self.parent: "ResolvedObject" | None = None

        # Properties inherited from parent (property_name -> parent_name)
        # Only set for properties the child did NOT define and got from a parent.
        self.inherited_from: dict[str, str] = {}

    def __repr__(self) -> str:
        return f"ResolvedObject({self.object_type.value}:{self.name}@{self.version})"


# ----------------------------------------------------------------------
# Pure helpers
#
# Small, side-effect-free building blocks used by Resolver. They take
# plain data in and return plain data out (a few also fill a caller-owned
# dict), so each step of resolution can be read and tested on its own.
# ----------------------------------------------------------------------

# Object-type keys in the order the resolver walks them everywhere.
_ALL_TYPE_KEYS = ("engines", "projects", "gems", "templates", "repos", "overlays")

# Filenames too generic to name an object; the parent directory names it instead.
_GENERIC_OBJECT_FILENAMES = frozenset(
    {"gem.json", "template.json", "project.json", "engine.json", "repo.json"}
)


def _is_remote_url(path_str: str) -> bool:
    """True for an http(s) URL, which names a remote repo rather than a local path."""
    return path_str.startswith(("http://", "https://"))


def _resolve_child_url(base_url: str, rel: str) -> str:
    """Resolve a Schema 2.0.0 ``children`` entry against the URL of its document.

    Absolute URLs pass through; anything else is relative to the directory
    the document lives in.
    """
    if _is_remote_url(rel):
        return rel
    return base_url.rsplit("/", 1)[0] + "/" + rel.lstrip("/")


def _child_object_dir(parent_dir: Path, child_rel_path: str) -> Path:
    """Directory of a child object listed in a parent's ``children``.

    Schema 2.0.0 entries name the child's JSON file, so its directory is
    the file's parent; legacy entries name the directory itself.
    """
    rel_path = Path(child_rel_path)
    if rel_path.suffix == ".json":
        return parent_dir / rel_path.parent
    return parent_dir / child_rel_path


def _detect_object_type(
    path: Path, type_names: tuple[str, ...] = ("gem", "project", "engine", "template")
) -> ObjectType | None:
    """Return the first of *type_names* that has an object JSON in *path*, or None."""
    for type_name in type_names:
        try:
            find_object_json(path, type_name)
            return ObjectType(type_name)
        except FileNotFoundError:
            continue
    return None


def _infer_template_type(data: dict, name: str) -> None:
    """Fill an empty ``template.type`` by guessing from the template's name.

    "my_project_template" becomes "project", and so on; an unguessable
    name sets it to "". Mutates *data* in place.
    """
    tpl_header = data.get("template", {})
    if isinstance(tpl_header, dict) and not tpl_header.get("type"):
        inferred = ""
        name_lower = (name or "").lower()
        for kw in ("project", "gem", "engine", "repo"):
            if kw in name_lower:
                inferred = kw
                break
        tpl_header["type"] = inferred


def _display_metadata(obj: "ResolvedObject") -> dict:
    """display_name / summary / icon_path for the resolved manifest.

    Schema 2.0.0 keeps display_name/description inside the type dict
    (e.g. ``gem.display_name``); legacy objects keep display_name/summary
    at the root. Icons are ``icon.relative_path`` (2.0.0) or ``icon_path``.
    """
    display_metadata = {}

    type_data = obj.data.get(obj.object_type.value, {})

    # Try Schema 2.0.0 location first, then legacy
    display_name = type_data.get("display_name") or obj.data.get("display_name")
    description = type_data.get("description") or obj.data.get("summary")

    if display_name:
        display_metadata["display_name"] = display_name
    if description:
        display_metadata["summary"] = description

    icon_data = obj.data.get("icon", {})
    icon_path = icon_data.get("relative_path") if isinstance(icon_data, dict) else None
    if not icon_path:
        icon_path = obj.data.get("icon_path")
    if icon_path:
        display_metadata["icon_path"] = icon_path

    return display_metadata


def _ancestry(obj: "ResolvedObject") -> list[dict]:
    """``[{name, path}, ...]`` from the immediate parent up to the root."""
    parents = []
    current = obj.parent
    while current:
        parents.append(
            {
                "name": current.name,
                "path": current.path.as_posix(),
            }
        )
        current = current.parent
    return parents


def _release_versions(data: dict) -> list[str]:
    """Version names of an object's ``releases`` (``name`` or ``version`` of each)."""
    releases_list = data.get("releases", [])
    release_versions = []
    if releases_list and isinstance(releases_list, list):
        for release in releases_list:
            if isinstance(release, dict):
                version = release.get("name") or release.get("version")
                if version:
                    release_versions.append(version)
    return release_versions


def _all_children(start: list[str], objects: dict[str, "ResolvedObject"]) -> list[dict]:
    """Breadth-first transitive closure of children, as ``[{name, type}, ...]``.

    *start* is the list of direct child names; each name appears once.
    A name that isn't a resolved object gets an empty type.
    """
    result: list[dict] = []
    seen: set[str] = set()
    queue = list(start)
    while queue:
        cn = queue.pop(0)
        if cn in seen:
            continue
        seen.add(cn)
        cobj = objects.get(cn)
        ctype = cobj.object_type.value if cobj else ""
        result.append({"name": cn, "type": ctype})
        if cobj:
            for cc in cobj.children:
                if cc.name not in seen:
                    queue.append(cc.name)
    return result


def _all_remotes(
    start: list[str],
    remote_objects: dict[str, dict],
    objects: dict[str, "ResolvedObject"],
) -> list[dict]:
    """Breadth-first walk through remote repos, as ``[{name, type}, ...]``.

    Each remote is listed, followed by the objects it advertises; then
    its own remotes are queued. Unknown remotes are assumed to be repos.
    An advertised object with no remote type takes its local type, if any.
    """
    result: list[dict] = []
    seen: set[str] = set()
    queue = list(start)
    while queue:
        rn = queue.pop(0)
        if rn in seen:
            continue
        seen.add(rn)
        robj = remote_objects.get(rn, {})
        rtype = robj.get("type", "repo")
        result.append({"name": rn, "type": rtype})
        # Add children of this remote (advertised objects)
        for child in robj.get("children", []):
            if child not in seen:
                child_data = remote_objects.get(child, {})
                ctype = child_data.get("type", "")
                if not ctype and child in objects:
                    ctype = objects[child].object_type.value
                result.append({"name": child, "type": ctype})
                seen.add(child)
        # Enqueue sub-remotes for transitive walk
        for sub in robj.get("remotes", []):
            if sub not in seen:
                queue.append(sub)
    return result


def _url_to_repo_names(crawled: dict[str, dict]) -> dict[str, str]:
    """Map each crawled URL to the name it is shown under.

    Errored URLs keep the URL itself as their name.
    """
    url_to_name: dict[str, str] = {}
    for url, data in crawled.items():
        if not data.get("_error"):
            try:
                url_to_name[url] = get_object_name(data) or data.get("repo_name", url)
            except Exception:
                url_to_name[url] = data.get("repo_name", url)
        else:
            url_to_name[url] = url
    return url_to_name


def _index_crawled_documents(crawled: dict[str, dict]) -> dict[str, dict]:
    """Index every successfully crawled document by URL.

    Returns ``{url: {"name", "type", "version"}}`` so 2.0.0 children,
    which are fetched as their own documents, resolve to canonical names.
    A document with no 2.0.0 header is treated as a legacy repo.
    """
    url_index: dict[str, dict] = {}
    for url, data in crawled.items():
        if data.get("_error"):
            continue
        try:
            obj_type = get_object_type(data)
            name = get_object_name(data) or url
            version = get_object_version(data) or ""
        except Exception:
            # Legacy repo without 2.0.0 header
            obj_type = ObjectType.REPO
            name = data.get("repo_name", url)
            version = ""
        url_index[url] = {"name": name, "type": obj_type.value, "version": version}
    return url_index


def _error_repo_entry(url: str, error: Any) -> dict:
    """Cache entry for a remote repo URL that could not be crawled."""
    return {
        "url": url,
        "type": "repo",
        "version": "",
        "children": [],
        "remotes": [],
        "dependencies": [],
        "status": "error",
        "display_metadata": {"summary": f"Error: {error}"},
    }


def _remote_document_entry(url: str, data: dict, idx: dict) -> dict:
    """Cache entry for a crawled non-repo document (a 2.0.0 child of a repo)."""
    header = data.get(idx["type"], {}) if isinstance(data.get(idx["type"]), dict) else {}
    dm = {}
    if header.get("display_name"):
        dm["display_name"] = header["display_name"]
    if header.get("description"):
        dm["summary"] = header["description"]
    return {
        "url": url,
        "type": idx["type"],
        "version": idx["version"],
        "children": [],
        "remotes": [],
        "dependencies": [],
        "status": "remote",
        "display_metadata": dm or None,
    }


def _name_from_object_url(obj_url: str) -> str:
    """Name a remotely advertised object after its URL.

    Uses the file name, or the parent path segment when the file name is
    generic (``.../MyGem/gem.json`` -> ``MyGem``).
    """
    parts = obj_url.rstrip("/").split("/")
    filename = parts[-1] if parts else obj_url
    if filename.lower() in _GENERIC_OBJECT_FILENAMES and len(parts) >= 2:
        return parts[-2]
    return filename


def _children_from_2_0_0(url: str, data: dict, url_index: dict[str, dict]) -> list[str]:
    """Names of a repo's Schema 2.0.0 ``children.*`` that were crawled."""
    children: list[str] = []
    children_2 = data.get("children", {})
    if isinstance(children_2, dict):
        for key in _ALL_TYPE_KEYS:
            for rel in children_2.get(key, []):
                if not isinstance(rel, str) or not rel:
                    continue
                child_idx = url_index.get(_resolve_child_url(url, rel))
                if child_idx:
                    children.append(child_idx["name"])
    return children


def _children_from_url_list(
    collection: str,
    data: dict,
    repo_name: str,
    remote_objects: dict[str, dict],
    local_objects: Container[str],
) -> list[str]:
    """Names of the objects a repo lists as URLs under *collection*.

    Adds a remote entry to *remote_objects* for each one that is neither
    already there nor resolved locally.
    """
    children: list[str] = []
    for obj_url in data.get(collection, []):
        if not isinstance(obj_url, str):
            continue
        child_name = _name_from_object_url(obj_url)
        if child_name not in remote_objects and child_name not in local_objects:
            remote_objects[child_name] = {
                "url": obj_url,
                "type": collection.rstrip("s"),
                "version": "",
                "children": [],
                "remotes": [],
                "dependencies": [],
                "status": "remote",
                "repo": repo_name,
            }
        children.append(child_name)
    return children


def _children_from_legacy_data(
    collection: str,
    data: dict,
    repo_name: str,
    remote_objects: dict[str, dict],
    local_objects: Container[str],
) -> list[str]:
    """Names of the objects a legacy repo describes in ``<collection>_data``.

    Adds a remote entry to *remote_objects* for each one that is neither
    already there nor resolved locally.
    """
    type_singular = collection.rstrip("s")
    children: list[str] = []
    for item in data.get(f"{collection}_data", []):
        if not isinstance(item, dict):
            continue
        child_name = (
            item.get(f"{type_singular}_name") or item.get("name") or item.get("display_name", "")
        )
        if child_name and child_name not in remote_objects and child_name not in local_objects:
            remote_objects[child_name] = {
                "url": item.get("repo_uri", item.get("origin_uri", "")),
                "type": type_singular,
                "version": item.get("version", ""),
                "children": [],
                "remotes": [],
                "dependencies": [],
                "status": "remote",
                "repo": repo_name,
                "display_metadata": {
                    "display_name": item.get("display_name", ""),
                    "summary": item.get("summary", ""),
                }
                if item.get("display_name") or item.get("summary")
                else None,
            }
        if child_name:
            children.append(child_name)
    return children


def _collect_repo_children(
    url: str,
    data: dict,
    repo_name: str,
    url_index: dict[str, dict],
    remote_objects: dict[str, dict],
    local_objects: Container[str],
) -> list[str]:
    """Every object a crawled repo advertises, in listing order.

    2.0.0 ``children`` first, then for each collection its URL list
    followed by its legacy ``<collection>_data`` list. Advertised objects
    not known yet are added to *remote_objects*.
    """
    children = _children_from_2_0_0(url, data, url_index)
    for collection in ("engines", "projects", "gems", "templates"):
        children += _children_from_url_list(
            collection, data, repo_name, remote_objects, local_objects
        )
        children += _children_from_legacy_data(
            collection, data, repo_name, remote_objects, local_objects
        )
    return children


def _collect_repo_remotes(data: dict, url_index: dict[str, dict]) -> list[str]:
    """Names of the repos a repo itself advertises (legacy ``repos`` + ``remote.repos``).

    Crawled URLs are shown by name, others by URL.
    """
    remotes: list[str] = []
    for sub_url in data.get("repos", []):
        if not isinstance(sub_url, str):
            continue
        sub_idx = url_index.get(sub_url)
        remotes.append(sub_idx["name"] if sub_idx else sub_url)
    remote_section = data.get("remote", {})
    if isinstance(remote_section, dict):
        for sub_url in remote_section.get("repos", []):
            if not isinstance(sub_url, str):
                continue
            sub_idx = url_index.get(sub_url)
            rname = sub_idx["name"] if sub_idx else sub_url
            if rname not in remotes:
                remotes.append(rname)
    return remotes


def _repo_entry(url: str, data: dict, children: list[str], remotes: list[str]) -> dict:
    """Cache entry for a successfully crawled remote repo."""
    repo_header = data.get("repo", {}) if isinstance(data.get("repo"), dict) else {}
    display_name = repo_header.get("display_name") or data.get("repo_name", "")
    summary = repo_header.get("description") or data.get("summary", "")
    return {
        "url": url,
        "type": "repo",
        "version": "",
        "children": children,
        "remotes": remotes,
        "dependencies": [],
        "status": "remote",
        "display_metadata": {
            "summary": summary,
            "display_name": display_name,
        }
        if (summary or display_name)
        else None,
    }


def _crawl_links(url: str, data: dict, visited: Container[str]) -> list[str]:
    """URLs a crawled repo document points at that haven't been visited.

    Follows legacy top-level ``repos``, Schema 2.0.0 ``remote.*`` (absolute
    URLs) and Schema 2.0.0 ``children.*`` (relative to *url*'s directory).
    """
    found: list[str] = []
    # Legacy: top-level repos list
    for sub_url in data.get("repos", []):
        if isinstance(sub_url, str) and sub_url not in visited:
            found.append(sub_url)
    # Schema 2.0.0: remote.*
    remote = data.get("remote", {})
    if isinstance(remote, dict):
        for key in _ALL_TYPE_KEYS:
            for sub_url in remote.get(key, []):
                if isinstance(sub_url, str) and sub_url not in visited:
                    found.append(sub_url)
    # Schema 2.0.0: children.*
    children = data.get("children", {})
    if isinstance(children, dict):
        for key in _ALL_TYPE_KEYS:
            for rel in children.get(key, []):
                if not isinstance(rel, str) or not rel:
                    continue
                child_url = _resolve_child_url(url, rel)
                if child_url not in visited:
                    found.append(child_url)
    return found


def _fetch_repo_document(client: Any, url: str) -> tuple[str, dict | None, str | None]:
    """GET one repo document as ``(url, data, error)``; never raises.

    A non-JSON answer for a URL that isn't a ``.json`` file is retried as
    ``<url>/repo.json``.
    """
    try:
        resp = client.get(url, follow_redirects=True)
        if resp.status_code != 200 or "json" not in resp.headers.get("content-type", ""):
            if not url.endswith(".json"):
                alt_url = url.rstrip("/") + "/repo.json"
                alt_resp = client.get(alt_url, follow_redirects=True)
                if alt_resp.status_code == 200:
                    resp = alt_resp
        resp.raise_for_status()
        return (url, resp.json(), None)
    except Exception as e:
        return (url, None, str(e))


class Resolver:
    """
    Resolves the O3DE manifest into a complete, flattened view.

    Usage:
        resolver = Resolver()
        resolved = resolver.resolve()
        resolver.save()
    """

    def __init__(self, manifest_path: Path | None = None, dry_run: bool = False):
        self.manifest_path = manifest_path or get_manifest_path()
        self.resolved_path = get_resolved_manifest_path()
        self.dry_run = dry_run

        # All resolved objects by name
        self.objects: dict[str, ResolvedObject] = {}

        # ALL versions of every object encountered on disk:
        # name -> {version -> ResolvedObject}. Unlike `objects` (which keeps
        # only the newest version per name), this retains alternates so the
        # solver/UI can offer them as override candidates.
        self.objects_all: dict[str, dict[str, ResolvedObject]] = {}

        # Objects by type
        self.engines: dict[str, ResolvedObject] = {}
        self.projects: dict[str, ResolvedObject] = {}
        self.gems: dict[str, ResolvedObject] = {}
        self.templates: dict[str, ResolvedObject] = {}
        self.repos: dict[str, ResolvedObject] = {}
        self.overlays: dict[str, ResolvedObject] = {}

        # Manifest data
        self.manifest_data: dict | None = None

        # File hashes for change detection: path -> hash
        self.file_hashes: dict[str, str] = {}

        # Dependency graph: object_name -> list of (dep_name, dep_version) tuples
        self.dependency_graph: dict[str, list[tuple[str, str]]] = {}

        # Detected conflicts
        self.conflicts: list[DependencyConflict] = []

        # Locked (pinned) transitive dependencies
        self.locked_dependencies: dict[str, dict] = {}

        # Remote repo URLs from the manifest (not local paths)
        self.manifest_remotes: list[str] = []

        # Crawled remote repos: url -> {repo_name, summary, repos: [...], gems: [...], ...}
        self._crawled_remotes: dict[str, dict] = {}

    def resolve(
        self,
        progress_callback: Callable[[str, int, int], None] | None = None,
    ) -> dict[str, ResolvedObject]:
        """
        Resolve the manifest.

        1. Load manifest JSON
        2. Descend all local object paths
        3. Resolve children recursively
        4. Parse dependencies
        5. Match overlays to base objects

        Returns:
            Dict of object_name -> ResolvedObject
        """
        manifest_data = self._load_manifest()
        self._write_manifest_sidecar(manifest_data)

        # Handle both Schema 2.0.0 (local.engines) and legacy (engines at root) formats
        local = manifest_data.get("local", {})
        remote = manifest_data.get("remote", {})

        # Sanitize manifest: deduplicate and validate type assignments
        self._sanitize_and_save_manifest(local)

        # Note: We do NOT convert "restricteds" to "overlays"
        # They are different concepts with no upgrade path

        root_paths, stale_paths, local_remotes = self._partition_root_paths(local, manifest_data)
        self.manifest_remotes.extend(local_remotes)

        # Schema 2.0.0: remote.repos contains repo URLs
        for url in remote.get("repos", []):
            if url not in self.manifest_remotes:
                self.manifest_remotes.append(url)

        # Remove stale paths from manifest
        if stale_paths:
            self._remove_stale_paths(stale_paths)

        self._resolve_root_objects(root_paths, progress_callback)
        self._analyze_resolved_objects()

        if progress_callback:
            total = len(root_paths)
            progress_callback("Complete", total, total)

        logger.info(f"Resolved {len(self.objects)} objects")
        return self.objects

    def _record_file_hash(self, path: Path) -> None:
        """Remember *path*'s SHA-256 for change detection (skipped if unreadable)."""
        file_hash = compute_file_hash(path)
        if file_hash:
            self.file_hashes[path.as_posix()] = file_hash

    def _load_manifest(self) -> dict:
        """Load the manifest into ``manifest_data`` and record its hash.

        Raises:
            ResolverError: If the manifest file does not exist
        """
        if not self.manifest_path.exists():
            raise ResolverError(f"Manifest not found: {self.manifest_path}")

        with open(self.manifest_path) as f:
            self.manifest_data = json.load(f)
        self._record_file_hash(self.manifest_path)
        return self.manifest_data

    def _write_manifest_sidecar(self, manifest_data: dict) -> None:
        """Write the upgraded ``o3de_manifest.2-0-0.json`` beside a legacy manifest.

        The legacy manifest itself is left untouched. Also creates the
        upgraded manifest's default repos/overlays directories. A write
        failure is logged, not raised.
        """
        if not needs_upgrade(manifest_data):
            return
        upgraded_manifest = upgrade_to_latest(manifest_data, "manifest")
        if upgraded_manifest is None:
            return
        sidecar_name = "o3de_manifest.2-0-0.json"
        sidecar_path = self.manifest_path.parent / sidecar_name
        try:
            with open(sidecar_path, "w") as f:
                json.dump(upgraded_manifest, f, indent=2)
            logger.info(f"Created manifest sidecar: {sidecar_path}")
            # Ensure default directories (repos, overlays) exist on disk
            for key in ("repos_path", "overlays_path"):
                dir_str = upgraded_manifest.get("default", {}).get(key, "")
                if dir_str:
                    Path(dir_str).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning(f"Failed to write manifest sidecar: {e}")

    def _sanitize_and_save_manifest(self, local: dict) -> None:
        """Dedupe/retype the manifest's local lists and save it if anything changed."""
        if not self._sanitize_manifest(local):
            return
        try:
            with open(self.manifest_path, "w") as f:
                json.dump(self.manifest_data, f, indent=2)
            logger.info("Sanitized manifest (dedup / type fix)")
        except OSError as e:
            logger.warning(f"Failed to save sanitized manifest: {e}")

    @staticmethod
    def _partition_root_paths(
        local: dict, manifest_data: dict
    ) -> tuple[list[tuple[Path, str]], list[tuple[str, str]], list[str]]:
        """Sort the manifest's registered entries into what to resolve.

        Reads each type list from ``local`` (Schema 2.0.0), falling back to
        the manifest root (legacy). Logs a warning for each stale path.

        Returns:
            ``(root_paths, stale_paths, remote_urls)``: existing local paths
            as ``(Path, type_key)``, missing ones as ``(path_str, type_key)``,
            and http(s) URLs found in the ``repos`` list. URLs in other
            lists are ignored.
        """
        root_paths: list[tuple[Path, str]] = []
        stale_paths: list[tuple[str, str]] = []
        remote_urls: list[str] = []
        for obj_type in _ALL_TYPE_KEYS:
            # Schema 2.0.0: local.engines, local.projects, etc.
            paths = local.get(obj_type, [])
            # Legacy: engines, projects, etc. at root level
            if not paths:
                paths = manifest_data.get(obj_type, []) or []
            for path_str in paths:
                # URLs (http/https) are remote repos, not local paths
                if _is_remote_url(path_str):
                    if obj_type == "repos":
                        remote_urls.append(path_str)
                    continue
                p = Path(path_str)
                # Check if path exists (handle both file and directory paths)
                check_path = p.parent if p.is_file() else p
                if not check_path.exists():
                    stale_paths.append((path_str, obj_type))
                    logger.warning(f"Removing stale path from manifest: {path_str}")
                else:
                    root_paths.append((p, obj_type))
        return root_paths, stale_paths, remote_urls

    def _resolve_root_objects(
        self,
        root_paths: list[tuple[Path, str]],
        progress_callback: Callable[[str, int, int], None] | None,
    ) -> None:
        """Resolve each registered root object (and, recursively, its children)."""
        total = len(root_paths)
        for current, (path, obj_type_str) in enumerate(root_paths, 1):
            if progress_callback:
                progress_callback(f"Resolving {path.name}", current, total)

            self._resolve_object(path, ObjectType(obj_type_str.rstrip("s")))

    def _analyze_resolved_objects(self) -> None:
        """Post-resolution passes over the full object set.

        Inheritance, overlay matching, the dependency DAG and conflict
        detection, then deprecation and peer-dependency warnings.
        """
        # Inherit properties from parents to children
        self._apply_inheritance()

        # Match overlays to base objects
        self._match_overlays()

        # Build dependency DAG and detect conflicts
        self._build_dependency_graph()
        self._detect_conflicts()

        if self.conflicts:
            conflict_msgs = [repr(c) for c in self.conflicts]
            logger.warning(f"Dependency conflicts detected: {conflict_msgs}")

        # Check for deprecated objects
        self._check_deprecations()

        # Check for missing peer dependencies
        self._check_peer_dependencies()

    # ------------------------------------------------------------------
    # Remote repo crawling
    # ------------------------------------------------------------------

    def _crawl_remote_repos(self, urls: list[str]) -> dict[str, dict]:
        """
        Crawl remote repo URLs transitively, returning {url: repo_data}.

        Follows legacy top-level ``repos`` lists, Schema 2.0.0
        ``remote.repos`` (absolute URLs to other repos), and Schema 2.0.0
        ``children.*`` (paths relative to the repo JSON's own location).
        Already-visited URLs are skipped to avoid infinite loops.

        Uses wave-based parallel fetching for speed.
        """
        import httpx

        visited: dict[str, dict] = {}
        wave = list(urls)

        with httpx.Client(timeout=10) as client:
            while wave:
                # De-duplicate against already-visited
                pending = [u for u in wave if u not in visited]
                if not pending:
                    break

                # Mark all as visited (reserve slots)
                for u in pending:
                    visited[u] = {}

                url_to_result = self._fetch_repo_wave(client, pending)

                # Process results, discover next wave
                next_wave: list[str] = []
                for url in pending:
                    data, error = url_to_result[url]
                    if error or data is None:
                        logger.warning(f"Failed to crawl remote repo {url}: {error}")
                        visited[url] = {"repo_name": url, "_error": error or "unknown"}
                        continue

                    visited[url] = data
                    next_wave.extend(_crawl_links(url, data, visited))
                    logger.info(f"Crawled remote repo: {url}")

                wave = next_wave

        return visited

    @staticmethod
    def _fetch_repo_wave(
        client: Any, pending: list[str]
    ) -> dict[str, tuple[dict | None, str | None]]:
        """Fetch every URL in *pending* in parallel: ``{url: (data, error)}``."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        url_to_result: dict[str, tuple[dict | None, str | None]] = {}
        with ThreadPoolExecutor(max_workers=min(32, len(pending))) as executor:
            futures = {executor.submit(_fetch_repo_document, client, u): u for u in pending}
            for future in as_completed(futures):
                url, data, error = future.result()
                url_to_result[url] = (data, error)
        return url_to_result

    def _build_remote_objects(self) -> dict[str, dict]:
        """
        Build resolved-object-style entries for all crawled remote repos.

        Each remote repo becomes a first-class object in the cache with:
        - type: "repo"
        - children: gems/templates/projects/engines advertised by the repo
        - remotes: sub-repos listed in the repo

        Also creates entries for remotely-advertised objects (gems, etc.)
        that aren't locally resolved, so drill-down can display them.

        Returns:
            Dict of object_name -> resolved object dict
        """
        remote_objects: dict[str, dict] = {}
        url_index = _index_crawled_documents(self._crawled_remotes)

        for url, data in self._crawled_remotes.items():
            if data.get("_error"):
                remote_objects[url] = _error_repo_entry(url, data["_error"])
                continue

            idx = url_index[url]
            obj_name = idx["name"]

            # Non-repo documents (2.0.0 children fetched during the crawl):
            # surface as remote objects for drill-down.
            if idx["type"] != "repo":
                if obj_name not in remote_objects and obj_name not in self.objects:
                    remote_objects[obj_name] = _remote_document_entry(url, data, idx)
                continue

            children = _collect_repo_children(
                url, data, obj_name, url_index, remote_objects, self.objects
            )
            remotes = _collect_repo_remotes(data, url_index)
            remote_objects[obj_name] = _repo_entry(url, data, children, remotes)

        return remote_objects

    def get_missing_dependencies(self) -> list[tuple[str, ObjectNameVersion]]:
        """
        Find all dependencies that reference objects not present in the manifest.

        Returns:
            List of (requirer_name, dep_spec) tuples for each missing dep
        """
        missing: list[tuple[str, ObjectNameVersion]] = []
        for name, obj in self.objects.items():
            for dep_spec in obj.dependencies:
                candidate = self.objects.get(dep_spec.name)
                if candidate is None:
                    missing.append((name, dep_spec))
                elif not dep_spec.matches(candidate.version):
                    missing.append((name, dep_spec))
        return missing

    def get_missing_optional_dependencies(self) -> list[tuple[str, ObjectNameVersion]]:
        """
        Find optional dependencies not present in the manifest.

        These are not errors — they are suggestions for additional installs.

        Returns:
            List of (requirer_name, dep_spec) tuples for each missing optional dep
        """
        missing: list[tuple[str, ObjectNameVersion]] = []
        for name, obj in self.objects.items():
            for dep_spec in obj.optional_dependencies:
                candidate = self.objects.get(dep_spec.name)
                if candidate is None:
                    missing.append((name, dep_spec))
                elif not dep_spec.matches(candidate.version):
                    missing.append((name, dep_spec))
        return missing

    def auto_install_missing(
        self,
        store: "Store",
        confirm: bool = False,
        dry_run: bool = False,
        progress_callback: Callable[[str, int, int], None] | None = None,
    ) -> list[dict]:
        """
        Automatically fetch and install missing dependencies from the store.

        This is the npm-style auto-install: after resolve() identifies missing
        deps, this method searches the store for matching remote objects,
        downloads them, registers them in the manifest, and re-resolves.

        Args:
            store: Store instance with refreshed remote catalog
            confirm: If False, raise with a list of what would be installed
                     (the CLI layer uses this to prompt the user or require --yes)
            dry_run: If True, return what would be installed without doing anything
            progress_callback: Optional callback(message, current, total)

        Returns:
            List of dicts describing installed objects:
            [{"name": ..., "version": ..., "type": ..., "path": ..., "source": ...}]

        Raises:
            ResolverError: If confirm=False and there are missing deps (contains
                           the list of missing deps for the caller to present)
        """
        missing = self.get_missing_dependencies()
        if not missing:
            return []

        # Deduplicate: same dep might be required by multiple objects
        unique_missing: dict[str, ObjectNameVersion] = {}
        for _requirer, dep_spec in missing:
            if dep_spec.name not in unique_missing:
                unique_missing[dep_spec.name] = dep_spec

        install_plan, not_found = self._plan_installs(store, unique_missing)

        if not install_plan and not_found:
            logger.warning(f"Could not find remote objects for: {', '.join(not_found)}")
            return []

        # Build the install summary
        plan_summary = [
            {
                "name": remote.name,
                "version": remote.version,
                "type": remote.object_type.value,
                "source": remote.effective_source_control_url or remote.download_url or remote.url,
            }
            for _spec, remote in install_plan
        ]

        if dry_run:
            return plan_summary

        if not confirm:
            raise ResolverError(
                f"Missing {len(install_plan)} dependencies. "
                f"Use --yes to auto-install or --dry-run to preview.\n"
                + "\n".join(f"  {p['name']}@{p['version']} ({p['type']})" for p in plan_summary)
            )

        installed = self._install_planned(store, install_plan, progress_callback)

        if installed:
            logger.info(f"Auto-installed {len(installed)} dependencies")

        return installed

    @staticmethod
    def _plan_installs(
        store: "Store", unique_missing: dict[str, ObjectNameVersion]
    ) -> tuple[list[tuple[ObjectNameVersion, "RemoteObject"]], list[str]]:
        """Pick a remote object for each missing dependency.

        For each one, the newest store object with exactly that name and a
        version the constraint accepts.

        Returns:
            ``(install_plan, not_found)``: ``(spec, remote)`` pairs, and the
            names nothing in the store satisfies.
        """
        install_plan: list[tuple[ObjectNameVersion, RemoteObject]] = []
        not_found: list[str] = []

        for dep_name, dep_spec in unique_missing.items():
            # Search across all object types
            candidates = store.search(dep_name)

            # Find exact name match with compatible version
            best: RemoteObject | None = None
            for candidate in candidates:
                if candidate.name == dep_name:
                    if dep_spec.matches(candidate.version):
                        if best is None or store._is_newer_version(candidate.version, best.version):
                            best = candidate

            if best:
                install_plan.append((dep_spec, best))
            else:
                not_found.append(dep_name)

        return install_plan, not_found

    def _install_planned(
        self,
        store: "Store",
        install_plan: list[tuple[ObjectNameVersion, "RemoteObject"]],
        progress_callback: Callable[[str, int, int], None] | None,
    ) -> list[dict]:
        """Download and register each planned object; a failed download is logged and skipped."""
        from .paths import get_default_path_for_type

        installed: list[dict] = []
        total = len(install_plan)

        for idx, (_dep_spec, remote) in enumerate(install_plan, 1):
            if progress_callback:
                progress_callback(f"Installing {remote.name}@{remote.version}", idx, total)

            target_path = get_default_path_for_type(remote.object_type)

            try:
                # Archives are checked against remote.source_sha256 inside;
                # O3DE_ALLOW_UNVERIFIED is the only way past a missing one here.
                download_path = store.download_sync(remote, target_path)
            except Exception as e:
                logger.error(f"Failed to download {remote.name}: {e}")
                continue

            # Register in the manifest
            self._add_to_manifest(download_path, remote.object_type)

            installed.append(
                {
                    "name": remote.name,
                    "version": remote.version,
                    "type": remote.object_type.value,
                    "path": str(download_path),
                    "source": remote.effective_source_control_url or remote.download_url or "",
                }
            )

        return installed

    def _add_to_manifest(self, obj_path: Path, obj_type: "ObjectType") -> None:
        """Register a downloaded object in the manifest file."""
        import json as _json

        if not self.manifest_path.exists():
            return

        with open(self.manifest_path) as f:
            manifest_data = _json.load(f)

        local = manifest_data.setdefault("local", {})
        type_key = obj_type.value + "s"
        type_list = local.setdefault(type_key, [])

        path_str = obj_path.resolve().as_posix()
        # Avoid duplicates
        resolved_existing = {Path(p).resolve().as_posix() for p in type_list}
        if path_str not in resolved_existing:
            type_list.append(path_str)
            with open(self.manifest_path, "w") as f:
                _json.dump(manifest_data, f, indent=2)
            logger.info(f"Registered {obj_type.value}: {obj_path.name}")

    def _parse_object_dependencies(
        self,
        resolved: "ResolvedObject",
        data: dict,
        expected_type: ObjectType,
    ) -> None:
        """Parse dependent/optional/peer dependency specifiers into *resolved*.

        Schema 2.0.0: type_dict.dependent with type keys: {"gems": ["org.o3de.gem.a>=1.0.0"]}
        Legacy: root-level "dependencies" flat list: ["GemA", "GemB"]
        """
        # Schema 2.0.0: dependent can be inside the type dict (e.g., data["gem"]["dependent"])
        type_key = expected_type.value
        type_data = data.get(type_key, {})
        dependent = type_data.get("dependent", {}) if isinstance(type_data, dict) else {}

        # Also check root level (some formats put it there)
        if not dependent:
            dependent = data.get("dependent", {})

        if isinstance(dependent, dict):
            for dep_list in dependent.values():
                if isinstance(dep_list, list):
                    for dep in dep_list:
                        resolved.dependencies.append(ObjectNameVersion(dep))

        # Also check inside type dict for "dependencies" (Schema 2.0.0 alt)
        type_deps = type_data.get("dependencies", {}) if isinstance(type_data, dict) else {}
        if isinstance(type_deps, dict):
            for dep_list in type_deps.values():
                if isinstance(dep_list, list):
                    for dep in dep_list:
                        resolved.dependencies.append(ObjectNameVersion(dep))

        # Also check legacy format (flat list at root)
        legacy_deps = data.get("dependencies", [])
        if isinstance(legacy_deps, list):
            for dep in legacy_deps:
                if isinstance(dep, str):
                    resolved.dependencies.append(ObjectNameVersion(dep))

        # Parse optional_dependent (nice-to-have deps, not required)
        optional_dep = (
            type_data.get("optional_dependent", {}) if isinstance(type_data, dict) else {}
        )
        if not optional_dep:
            optional_dep = data.get("optional_dependent", {})
        if isinstance(optional_dep, dict):
            for dep_list in optional_dep.values():
                if isinstance(dep_list, list):
                    for dep in dep_list:
                        resolved.optional_dependencies.append(ObjectNameVersion(dep))

        # Parse peer_dependent (must be provided by consumer)
        peer_dep = type_data.get("peer_dependent", {}) if isinstance(type_data, dict) else {}
        if not peer_dep:
            peer_dep = data.get("peer_dependent", {})
        if isinstance(peer_dep, dict):
            for dep_list in peer_dep.values():
                if isinstance(dep_list, list):
                    for dep in dep_list:
                        resolved.peer_dependencies.append(ObjectNameVersion(dep))

    def _resolve_object(self, path: Path, expected_type: ObjectType) -> ResolvedObject | None:
        """Resolve a single object and its children.

        Locates and loads the object's JSON (upgrading a legacy file to a
        2.0.0 sidecar), settles a clash with an already-resolved object of
        the same name, registers the result, then recurses into its
        ``children`` and any O3DE objects in ``external_subdirectories``.

        Returns:
            The resolved object (or the already-resolved one that wins), or
            None if *path* holds no usable object.
        """
        located = self._locate_object_json(path, expected_type)
        if located is None:
            return None
        path, json_path, is_versioned, expected_type = located

        try:
            data = self._load_object_json(json_path, path, is_versioned, expected_type)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"Failed to load {json_path}: {e}")
            return None

        # If legacy file, check if upgrade is needed
        if not is_versioned and needs_upgrade(data):
            logger.info(f"Upgrading legacy schema in {json_path}")
            try:
                upgraded_data = upgrade_to_latest(data)
            except Exception as e:
                # A malformed object must not kill the whole resolve —
                # skip it and keep going (e.g. an empty/invalid json)
                logger.warning(f"Skipping unupgradeable object {json_path}: {e}")
                return None
            if self._write_versioned_object(upgraded_data, path, expected_type):
                data = upgraded_data

        name = get_object_name(data)
        version = get_object_version(data)

        if expected_type == ObjectType.TEMPLATE:
            _infer_template_type(data, name)

        if not name:
            logger.warning(f"No name in {json_path}")
            return None

        # Already resolved (a root that is also a child, or several versions
        # of one object on disk): either keep the existing one, or fall
        # through and let this newer one replace it.
        if name in self.objects:
            kept = self._reconcile_duplicate(name, version, path, expected_type, data)
            if kept is not None:
                return kept

        resolved = ResolvedObject(
            path=path,
            object_type=expected_type,
            name=name,
            version=version,
            data=data,
        )
        self._parse_object_dependencies(resolved, data, expected_type)
        self._register_object(resolved)

        self._resolve_declared_children(resolved, data, path)
        self._resolve_external_subdirectories(resolved, data, path)

        return resolved

    def _locate_object_json(
        self, path: Path, expected_type: ObjectType
    ) -> tuple[Path, Path, bool, ObjectType] | None:
        """Find the JSON file that describes the object at *path*.

        *path* is the object directory or a JSON file in it. The 2.0.0
        versioned file is preferred over the legacy one. If a directory has
        no JSON for *expected_type*, every type is tried in turn.

        Returns:
            ``(object_dir, json_path, is_versioned, object_type)``, or None
            (with a warning) when *path* is missing or holds no object JSON.
        """
        if not path.exists():
            logger.warning(f"Object path does not exist: {path}")
            return None

        # Handle paths pointing directly to JSON files
        if path.is_file() and path.suffix == ".json":
            json_path, is_versioned = self._prefer_versioned_json(path, expected_type)
            # Use parent directory as the object root
            return path.parent, json_path, is_versioned, expected_type

        # Find the object JSON - prefer versioned 2.0.0 file over legacy
        try:
            json_path, is_versioned = find_object_json(path, expected_type.value)
            return path, json_path, is_versioned, expected_type
        except FileNotFoundError:
            pass

        # Try to detect type from existing JSON files
        for type_name in ["engine", "project", "gem", "template", "repo", "overlay"]:
            try:
                json_path, is_versioned = find_object_json(path, type_name)
                return path, json_path, is_versioned, ObjectType(type_name)
            except FileNotFoundError:
                continue

        logger.warning(f"No object JSON found in: {path}")
        return None

    @staticmethod
    def _prefer_versioned_json(json_file: Path, expected_type: ObjectType) -> tuple[Path, bool]:
        """For a registered JSON *file*, return ``(json_path, is_versioned)`` to load.

        A versioned file (``*.2-0-0.json``) is used as-is. For a legacy
        file, whatever ``find_object_json`` picks in the same directory
        wins if it is a different file (and is then treated as versioned).
        """
        if ".2-0-0." in json_file.name or "-2-0-0." in json_file.name:
            return json_file, True
        try:
            found_path, _ = find_object_json(json_file.parent, expected_type.value)
        except FileNotFoundError:
            return json_file, False
        if found_path != json_file:
            return found_path, True
        return json_file, False

    def _load_object_json(
        self, json_path: Path, obj_dir: Path, is_versioned: bool, expected_type: ObjectType
    ) -> Any:
        """Read *json_path* and record its hash for change detection.

        When the versioned file is used, the legacy file beside it is
        hashed too, so an edit to it is still noticed.

        Raises:
            OSError, json.JSONDecodeError: If the file can't be read or parsed
        """
        with open(json_path) as f:
            data = json.load(f)
        self._record_file_hash(json_path)

        if is_versioned:
            legacy_path = obj_dir / get_object_json_filename(expected_type.value)
            if legacy_path.exists():
                self._record_file_hash(legacy_path)
        return data

    @staticmethod
    def _write_versioned_object(
        upgraded_data: Any, obj_dir: Path, expected_type: ObjectType
    ) -> bool:
        """Write upgraded object data to its 2.0.0 versioned file beside the legacy one.

        The legacy file is left untouched. Returns True if the file was
        written; a failure is logged and returns False.
        """
        versioned_filename = get_versioned_object_json_filename(expected_type.value, "2.0.0")
        versioned_path = obj_dir / versioned_filename
        try:
            with open(versioned_path, "w") as f:
                json.dump(upgraded_data, f, indent=2)
            logger.info(f"Created versioned file: {versioned_path}")
            return True
        except OSError as e:
            logger.warning(f"Failed to write versioned file {versioned_path}: {e}")
            return False

    def _reconcile_duplicate(
        self, name: str, version: str, path: Path, expected_type: ObjectType, data: dict
    ) -> ResolvedObject | None:
        """Settle an object whose *name* is already resolved.

        The same directory again returns the existing object (keeping its
        parent chain). Otherwise the newest version wins, consistent with
        the solver: an older or unparseable version returns the existing
        object and is kept in ``objects_all`` as an override candidate; a
        newer one returns None so the caller replaces the existing object,
        which stays in ``objects_all``.
        """
        existing = self.objects[name]
        if existing.path and existing.path.resolve() == path.resolve():
            return existing

        try:
            is_newer = Version(version) > Version(existing.version)
        except Exception:
            is_newer = False

        if not is_newer:
            logger.debug(
                f"Skipping older/duplicate {name}@{version} at {path} "
                f"(keeping {existing.version} at {existing.path})"
            )
            # Retain the alternate version for override candidates
            if version not in self.objects_all.get(name, {}):
                alternate = ResolvedObject(
                    path=path,
                    object_type=expected_type,
                    name=name,
                    version=version,
                    data=data,
                )
                self._parse_object_dependencies(alternate, data, expected_type)
                self.objects_all.setdefault(name, {})[version] = alternate
            return existing

        logger.debug(f"Replacing {name}@{existing.version} with newer {version} at {path}")
        self.objects_all.setdefault(name, {})[existing.version] = existing
        return None

    def _register_object(self, resolved: ResolvedObject) -> None:
        """Index *resolved* by name, by name+version, and in its per-type dict."""
        self.objects[resolved.name] = resolved
        self.objects_all.setdefault(resolved.name, {})[resolved.version] = resolved

        type_dict = {
            ObjectType.ENGINE: self.engines,
            ObjectType.PROJECT: self.projects,
            ObjectType.GEM: self.gems,
            ObjectType.TEMPLATE: self.templates,
            ObjectType.REPO: self.repos,
            ObjectType.OVERLAY: self.overlays,
        }.get(resolved.object_type)

        if type_dict is not None:
            type_dict[resolved.name] = resolved

    def _attach_child(
        self, parent: ResolvedObject, child_path: Path, child_type: ObjectType
    ) -> None:
        """Resolve the object at *child_path* and link it under *parent*."""
        child_resolved = self._resolve_object(child_path, child_type)
        if child_resolved:
            child_resolved.parent = parent
            parent.children.append(child_resolved)

    def _resolve_declared_children(self, parent: ResolvedObject, data: dict, obj_dir: Path) -> None:
        """Resolve the objects listed in *data*'s ``children`` dict.

        Schema 2.0.0: ``{"gems": ["Gems/MyGem/gem.json"], ...}`` with paths
        relative to *obj_dir*. Unknown type keys (e.g. legacy "restricted")
        and non-list values are skipped.
        """
        children = data.get("children", {})
        if not isinstance(children, dict):
            return
        for child_type_str, child_paths in children.items():
            if not isinstance(child_paths, list):
                continue

            # Skip unknown object types (e.g., "restricted" from legacy O3DE)
            try:
                child_type = ObjectType(child_type_str.rstrip("s"))
            except ValueError:
                logger.debug(f"Skipping unknown object type: {child_type_str}")
                continue

            for child_rel_path in child_paths:
                self._attach_child(parent, _child_object_dir(obj_dir, child_rel_path), child_type)

    def _resolve_external_subdirectories(
        self, parent: ResolvedObject, data: dict, obj_dir: Path
    ) -> None:
        """Resolve O3DE objects mistakenly listed in legacy ``external_subdirectories``.

        These should be CMake-only directories, but people often put gem
        paths there. Directories with an object JSON are resolved as
        children; plain CMake directories and missing paths are skipped.
        """
        external_subdirs = data.get("external_subdirectories", [])
        if not isinstance(external_subdirs, list):
            return
        for child_rel_path in external_subdirs:
            child_path = obj_dir / child_rel_path
            if not child_path.exists():
                continue

            detected_type = _detect_object_type(child_path)
            if detected_type:
                self._attach_child(parent, child_path, detected_type)
            else:
                # True external subdirectory - just CMakeLists.txt, not an O3DE object
                # Skip for object resolution (CMake will pick it up during build)
                logger.debug(f"Skipping non-O3DE external subdirectory: {child_path}")

    # Type-key → expected JSON filename inside the registered directory
    _TYPE_JSON = {
        "engines": "engine.json",
        "projects": "project.json",
        "gems": "gem.json",
        "templates": "template.json",
        "repos": "repo.json",
        "overlays": "overlay.json",
    }

    def _sanitize_manifest(self, local: dict) -> bool:
        """Deduplicate entries and validate type assignments in *local*.

        Returns True if the manifest was modified and should be saved.
        """
        dirty = False
        all_type_keys = list(self._TYPE_JSON.keys())

        for type_key in all_type_keys:
            entries = local.get(type_key, [])
            if not entries:
                continue

            # 1. Deduplicate (preserve first occurrence, compare resolved)
            seen: set[str] = set()
            deduped: list[str] = []
            for p in entries:
                resolved = str(Path(p).resolve())
                if resolved in seen:
                    logger.warning(f"Removing duplicate {type_key} entry: {p}")
                    dirty = True
                    continue
                seen.add(resolved)
                deduped.append(p)

            # 2. Validate: the registered path should contain the expected
            #    JSON file for this type list.  If it is in the wrong list,
            #    move it to the correct one.
            expected_json = self._TYPE_JSON[type_key]
            keep: list[str] = []
            for p in deduped:
                obj_dir = Path(p).resolve()
                if obj_dir.suffix == ".json":
                    obj_dir = obj_dir.parent

                if not obj_dir.is_dir():
                    keep.append(p)  # leave stale-path removal to existing code
                    continue

                if (obj_dir / expected_json).exists():
                    keep.append(p)
                    continue

                # Wrong list — figure out the correct one
                moved = False
                for correct_key, correct_json in self._TYPE_JSON.items():
                    if correct_key == type_key:
                        continue
                    if (obj_dir / correct_json).exists():
                        # Move to correct list
                        correct_list = local.setdefault(correct_key, [])
                        if p not in correct_list:
                            correct_list.append(p)
                        logger.warning(
                            f"Moved {p} from {type_key} to {correct_key} (found {correct_json})"
                        )
                        dirty = True
                        moved = True
                        break

                if not moved:
                    # No matching JSON found at all — keep it (let stale
                    # path removal handle it if dir doesn't exist later)
                    keep.append(p)

            if keep != entries:
                local[type_key] = keep
                dirty = True

        return dirty

    def _remove_stale_paths(self, stale_paths: list[tuple[str, str]]) -> None:
        """
        Remove stale paths from the manifest file.

        Args:
            stale_paths: List of (path_str, obj_type) tuples to remove
        """
        if not stale_paths or not self.manifest_path.exists():
            return

        try:
            with open(self.manifest_path) as f:
                manifest = json.load(f)

            local = manifest.get("local", {})
            modified = False

            for path_str, obj_type in stale_paths:
                type_list = local.get(obj_type, [])
                if path_str in type_list:
                    type_list.remove(path_str)
                    modified = True
                    logger.info(f"Removed stale {obj_type.rstrip('s')}: {path_str}")

            if modified:
                with open(self.manifest_path, "w") as f:
                    json.dump(manifest, f, indent=2)
                logger.info(f"Updated manifest: {self.manifest_path}")
        except Exception as e:
            logger.warning(f"Failed to remove stale paths from manifest: {e}")

    def _match_overlays(self) -> None:
        """Match overlays to their base objects."""
        for overlay in self.overlays.values():
            extends = overlay.data.get("extends", "")
            if not extends:
                continue

            # Parse version constraint from extends
            extends_spec = ObjectNameVersion(extends)

            # Find matching base object
            base = self.objects.get(extends_spec.name)
            if base:
                if extends_spec.matches(base.version):
                    base.overlays.append(overlay)
                    logger.debug(f"Matched overlay {overlay.name} -> {base.name}")
                else:
                    logger.warning(
                        f"Overlay {overlay.name} version mismatch: "
                        f"extends {extends_spec} but found {base.version}"
                    )
            else:
                logger.warning(f"Overlay {overlay.name} extends unknown object: {extends}")

    def _build_dependency_graph(self) -> None:
        """
        Build the full dependency DAG with transitive dependencies.

        For each object, compute the full list of transitive dependencies
        (direct deps + deps of deps, ...) and pin them with resolved versions.
        """
        self.dependency_graph = {}
        self.locked_dependencies = {}

        for name, obj in self.objects.items():
            # Walk the full transitive dependency tree
            visited: set[str] = set()
            pinned: list[tuple[str, str]] = []

            self._walk_dependencies(obj, visited, pinned)

            self.dependency_graph[name] = pinned

            # Build locked deps entry (pinned transitive versions)
            if pinned:
                self.locked_dependencies[name] = {
                    dep_name: dep_version for dep_name, dep_version in pinned
                }

    def _walk_dependencies(
        self,
        obj: ResolvedObject,
        visited: set[str],
        pinned: list[tuple[str, str]],
    ) -> None:
        """Recursively walk dependencies, collecting transitive pins."""
        for dep_spec in obj.dependencies:
            if dep_spec.name in visited:
                continue
            visited.add(dep_spec.name)

            # Find matching resolved object
            candidate = self.objects.get(dep_spec.name)
            if candidate and dep_spec.matches(candidate.version):
                pinned.append((candidate.name, candidate.version))
                # Recurse into transitive deps
                self._walk_dependencies(candidate, visited, pinned)

        # Also resolve optional dependencies if they happen to be present locally.
        # Missing optional deps are NOT errors — they are just skipped.
        for dep_spec in obj.optional_dependencies:
            if dep_spec.name in visited:
                continue
            visited.add(dep_spec.name)

            candidate = self.objects.get(dep_spec.name)
            if candidate and dep_spec.matches(candidate.version):
                pinned.append((candidate.name, candidate.version))
                self._walk_dependencies(candidate, visited, pinned)

    def _detect_conflicts(self) -> None:
        """
        Detect version conflicts in the dependency graph.

        A conflict occurs when two objects require different incompatible
        versions of the same dependency (e.g., A wants X>=2.0.0 and B wants X<2.0.0).
        """
        self.conflicts = []

        # Collect all constraints per dependency: dep_name -> [(requirer, spec)]
        all_constraints: dict[str, list[tuple[str, ObjectNameVersion]]] = {}

        for name, obj in self.objects.items():
            for dep_spec in obj.dependencies:
                if dep_spec.name not in all_constraints:
                    all_constraints[dep_spec.name] = []
                all_constraints[dep_spec.name].append((name, dep_spec))

        # Check each dependency that has multiple requirers
        for dep_name, constraints in all_constraints.items():
            if len(constraints) < 2:
                continue

            resolved = self.objects.get(dep_name)
            resolved_version = resolved.version if resolved else None

            # Check all pairs for incompatibility
            for i in range(len(constraints)):
                for j in range(i + 1, len(constraints)):
                    requirer_a, spec_a = constraints[i]
                    requirer_b, spec_b = constraints[j]

                    # Both must have version constraints to conflict
                    if not spec_a.specifier and not spec_b.specifier:
                        continue

                    # Check if there's any version that satisfies both
                    if resolved_version:
                        a_satisfied = spec_a.matches(resolved_version)
                        b_satisfied = spec_b.matches(resolved_version)

                        if not (a_satisfied and b_satisfied):
                            self.conflicts.append(
                                DependencyConflict(
                                    dependency_name=dep_name,
                                    requirer_a=requirer_a,
                                    constraint_a=str(spec_a.specifier) or "*",
                                    requirer_b=requirer_b,
                                    constraint_b=str(spec_b.specifier) or "*",
                                    resolved_version=resolved_version,
                                )
                            )

    def _check_deprecations(self) -> None:
        """Emit warnings for any resolved objects with deprecated status."""
        for name, obj in self.objects.items():
            # Schema 2.0.0: {type: {deprecated: {message, replacement}}}
            type_key = obj.object_type.value
            type_data = obj.data.get(type_key, {})
            deprecated = type_data.get("deprecated")

            # Legacy: deprecated at root level
            if not deprecated:
                deprecated = obj.data.get("deprecated")

            if deprecated:
                if isinstance(deprecated, dict):
                    msg = deprecated.get("message", "This object is deprecated.")
                    replacement = deprecated.get("replacement", "")
                    warning = f"DEPRECATED: {name} — {msg}"
                    if replacement:
                        warning += f" Use {replacement} instead."
                else:
                    warning = f"DEPRECATED: {name} — {deprecated}"
                logger.warning(warning)

    def _check_peer_dependencies(self) -> None:
        """Emit warnings for missing peer dependencies."""
        for name, obj in self.objects.items():
            for peer_spec in obj.peer_dependencies:
                candidate = self.objects.get(peer_spec.name)
                if candidate is None:
                    logger.warning(
                        f"PEER DEPENDENCY: {name} requires peer '{peer_spec}' "
                        f"which is not installed"
                    )
                elif not peer_spec.matches(candidate.version):
                    logger.warning(
                        f"PEER DEPENDENCY: {name} requires peer '{peer_spec}' "
                        f"but found version {candidate.version}"
                    )

    # ------------------------------------------------------------------
    # Property inheritance
    # ------------------------------------------------------------------

    # Properties that a child inherits from its parent when not defined.
    # Each entry is the JSON key in the object's data dict.
    INHERITABLE_PROPERTIES = ("origin", "licenses", "source_control", "documentation")

    def _apply_inheritance(self) -> None:
        """
        Inherit properties from parent objects to children that lack them.

        Inheritable properties (origin, licenses, source_control, documentation)
        propagate down the parent chain.  A child that already defines a property
        keeps its own value; only missing properties are filled in.

        After this method runs, every ResolvedObject's ``data`` dict contains
        the effective value for each inheritable property, and
        ``inherited_from`` records the parent name it came from (if any).
        """
        for obj in self.objects.values():
            if obj.parent is None:
                continue
            self._inherit_from_parent(obj)

    def _inherit_from_parent(self, obj: ResolvedObject) -> None:
        """
        Walk up the parent chain and inherit missing properties.

        For each inheritable property, the first ancestor that defines it
        wins.  The property value is **copied** into ``obj.data`` so that
        downstream consumers see a fully-resolved object, and the parent's
        name is recorded in ``obj.inherited_from``.
        """
        for prop in self.INHERITABLE_PROPERTIES:
            # Check if the child already has this property
            if self._has_property(obj, prop):
                continue

            # Walk parent chain until we find a provider
            ancestor = obj.parent
            while ancestor is not None:
                if self._has_property(ancestor, prop):
                    # Copy value into child's data
                    value = self._get_property(ancestor, prop)
                    obj.data[prop] = value
                    # Track the *original* source – if the ancestor itself
                    # inherited this property, record its ultimate origin
                    # rather than the relay ancestor.
                    source = ancestor.inherited_from.get(prop, ancestor.name)
                    obj.inherited_from[prop] = source
                    logger.debug(f"Inherited '{prop}' for {obj.name} from {source}")
                    break
                ancestor = ancestor.parent

    @staticmethod
    def _has_property(obj: ResolvedObject, prop: str) -> bool:
        """Check if an object defines a property (non-empty)."""
        val = obj.data.get(prop)
        if val is None:
            return False
        # An empty dict/list/string counts as not having the property
        if isinstance(val, (dict, list, str)) and not val:
            return False
        return True

    @staticmethod
    def _get_property(obj: ResolvedObject, prop: str) -> Any:
        """
        Get a deep copy of a property value from an object.

        Uses deep copy to prevent mutations in one object from affecting
        another.
        """
        import copy

        return copy.deepcopy(obj.data.get(prop))

    def get_dependencies_for(self, obj_name: str) -> list[ResolvedObject]:
        """Get all resolved dependencies for an object."""
        obj = self.objects.get(obj_name)
        if not obj:
            return []

        resolved_deps = []
        for dep_spec in obj.dependencies:
            # Find matching object
            for candidate in self.objects.values():
                if candidate.name == dep_spec.name:
                    if dep_spec.matches(candidate.version):
                        resolved_deps.append(candidate)
                        break

        return resolved_deps

    def get_objects_for_layout(
        self,
        root_name: str,
        include_overlays: bool = True,
    ) -> tuple[list[ResolvedObject], list[ResolvedObject]]:
        """
        Get all objects needed for a layout.

        Args:
            root_name: Name of root object (engine or project)
            include_overlays: Include matching overlays

        Returns:
            Tuple of (objects, overlays)
        """
        root = self.objects.get(root_name)
        if not root:
            raise ResolverError(f"Object not found: {root_name}")

        # Collect all dependencies recursively
        visited = set()
        objects = []

        def collect(obj: ResolvedObject):
            if obj.name in visited:
                return
            visited.add(obj.name)
            objects.append(obj)

            for dep in self.get_dependencies_for(obj.name):
                collect(dep)

            for child in obj.children:
                collect(child)

        collect(root)

        # Collect overlays
        overlays = []
        if include_overlays:
            for obj in objects:
                overlays.extend(obj.overlays)

            # Sort by precedence
            overlays.sort(key=lambda o: o.data.get("precedence", 0))

        return objects, overlays

    def save(self) -> Path:
        """
        Save resolved manifest to resolved_o3de_manifest.json.

        Computes and stores:
        - dependents: reverse dependencies (objects that depend on each object)
        - display_metadata: display_name, summary, icon_path from object data
        - git_info: remote_url and current branch for cloned repos
        - parent: reference to parent object that contains this one
        - locked_dependencies: pinned transitive dependency versions
        - conflicts: detected version conflicts (if any)

        In dry-run mode, computes everything but skips writing to disk.

        Returns:
            Path to saved file (even in dry-run mode)
        """
        resolved_data = self._build_resolved_data()

        if self.dry_run:
            logger.info(f"Dry-run: would save resolved manifest to {self.resolved_path}")
            return self.resolved_path

        with open(self.resolved_path, "w") as f:
            json.dump(resolved_data, f, indent=2)

        logger.info(f"Saved resolved manifest: {self.resolved_path}")
        return self.resolved_path

    def _build_resolved_data(self) -> dict[str, Any]:
        """Compute the resolved-manifest document that ``save()`` writes.

        Crawls the remote repos the manifest and objects advertise (the only
        network access) and reads git info for each local object, but
        writes nothing.
        """
        # Use default data as-is; we do NOT convert restricteds_path to overlays_path
        # because restricted and overlay are different concepts
        assert self.manifest_data is not None, "save() requires resolve() first"
        default_data = dict(self.manifest_data.get("default", {}))

        dependents_map = self._compute_dependents_map()
        resolved_data = self._resolved_data_skeleton(default_data)

        remote_objects = self._crawl_and_build_remote_objects()
        url_to_name = _url_to_repo_names(self._crawled_remotes)

        for name, obj in self.objects.items():
            resolved_data["objects"][name] = self._object_entry(
                name, obj, dependents_map, remote_objects, url_to_name
            )

            # Add to type list
            type_key = obj.object_type.value + "s"
            if type_key in resolved_data:
                resolved_data[type_key].append(
                    {
                        "name": name,
                        "path": obj.path.as_posix(),
                        "version": obj.version,
                    }
                )

        # Add remote objects to the cache (repos and their advertised objects)
        for rname, rdata in remote_objects.items():
            if rname not in resolved_data["objects"]:
                resolved_data["objects"][rname] = rdata
                # Add repos to the repos type list
                if rdata.get("type") == "repo":
                    resolved_data["repos"].append(
                        {
                            "name": rname,
                            "url": rdata.get("url", ""),
                            "version": rdata.get("version", ""),
                        }
                    )

        resolved_data["manifest_root"] = self._manifest_root_entry(
            resolved_data["objects"], remote_objects, url_to_name
        )

        # Include locked transitive dependencies for reproducibility
        if self.locked_dependencies:
            resolved_data["locked_dependencies"] = self.locked_dependencies

        # Include detected conflicts as warnings
        if self.conflicts:
            resolved_data["conflicts"] = [
                {
                    "dependency": c.dependency_name,
                    "requirer_a": c.requirer_a,
                    "constraint_a": c.constraint_a,
                    "requirer_b": c.requirer_b,
                    "constraint_b": c.constraint_b,
                    "resolved_version": c.resolved_version,
                }
                for c in self.conflicts
            ]

        return resolved_data

    def _compute_dependents_map(self) -> dict[str, list[str]]:
        """Invert dependencies: ``{object_name: [names of resolved objects that depend on it]}``.

        Only dependencies that are themselves resolved appear as keys.
        """
        dependents_map: dict[str, list[str]] = {}
        for name, obj in self.objects.items():
            for dep in obj.dependencies:
                dep_name = dep.name
                if dep_name in self.objects:
                    if dep_name not in dependents_map:
                        dependents_map[dep_name] = []
                    if name not in dependents_map[dep_name]:
                        dependents_map[dep_name].append(name)
        return dependents_map

    def _resolved_data_skeleton(self, default_data: dict) -> dict[str, Any]:
        """Header fields and empty object/type collections of the resolved manifest."""
        return {
            "$schema": "https://raw.githubusercontent.com/accesspointmg/canonical.o3de.org/main/src/o3de-resolved-manifest-2.0.0.json",
            "$schemaVersion": "2.0.0",
            "resolved_at": __import__("datetime").datetime.now().isoformat(),
            "manifest_path": self.manifest_path.as_posix(),
            # Defaults from manifest (normalized)
            "default": default_data,
            # File hashes for change detection
            "file_hashes": self.file_hashes,
            # All resolved objects with full paths
            "objects": {},
            # By-type lists for convenience
            "engines": [],
            "projects": [],
            "gems": [],
            "templates": [],
            "repos": [],
            "overlays": [],
        }

    def _collect_remote_repo_urls(self) -> set[str]:
        """Every remote repo URL to crawl: the manifest's plus each object's ``remote.repos``."""
        all_remote_urls: set[str] = set(self.manifest_remotes)
        for obj in self.objects.values():
            obj_remote = obj.data.get("remote", {})
            if isinstance(obj_remote, dict):
                for url in obj_remote.get("repos", []):
                    all_remote_urls.add(url)
        return all_remote_urls

    def _crawl_and_build_remote_objects(self) -> dict[str, dict]:
        """Crawl all advertised remote repos (if any) and build their cache entries."""
        all_remote_urls = self._collect_remote_repo_urls()
        if all_remote_urls:
            crawled = self._crawl_remote_repos(list(all_remote_urls))
            self._crawled_remotes.update(crawled)
        return self._build_remote_objects()

    def _git_info(self, obj: ResolvedObject) -> dict:
        """``{remote_url, branch}`` of the git checkout at *obj*'s path (empty if none)."""
        git_info = {}
        remote_url = get_local_git_remote(str(obj.path))
        if remote_url:
            git_info["remote_url"] = remote_url
            branch = get_local_git_branch(str(obj.path))
            if branch:
                git_info["branch"] = branch
        return git_info

    def _object_entry(
        self,
        name: str,
        obj: ResolvedObject,
        dependents_map: dict[str, list[str]],
        remote_objects: dict[str, dict],
        url_to_name: dict[str, str],
    ) -> dict:
        """The resolved-manifest entry for one local object."""
        display_metadata = _display_metadata(obj)
        git_info = self._git_info(obj)
        parents = _ancestry(obj)
        release_versions = _release_versions(obj.data)

        entry: dict[str, Any] = {
            "path": obj.path.as_posix(),
            "type": obj.object_type.value,
            "version": obj.version,
            "children": [c.name for c in obj.children],
            "dependencies": [str(d) for d in obj.dependencies],
            "optional_dependencies": [str(d) for d in obj.optional_dependencies] or None,
            "peer_dependencies": [str(d) for d in obj.peer_dependencies] or None,
            "all_dependencies": [
                {"name": dn, "version": dv} for dn, dv in self.dependency_graph.get(name, [])
            ]
            or None,
            "dependents": dependents_map.get(name, []),
            "overlays": [o.name for o in obj.overlays],
            "parent": obj.parent.name if obj.parent else None,
            "parents": parents,  # Full ancestry: [{name, path}, ...] from immediate parent to root
            "display_metadata": display_metadata if display_metadata else None,
            "git_info": git_info if git_info else None,
            "releases": release_versions if release_versions else None,
            # Inheritable properties (effective values after inheritance)
            "origin": obj.data.get("origin") or None,
            "licenses": obj.data.get("licenses") or None,
            "source_control": obj.data.get("source_control") or None,
            "documentation": obj.data.get("documentation") or None,
            # Which properties were inherited and from whom
            "inherited_from": obj.inherited_from if obj.inherited_from else None,
        }

        # all_children: transitive closure of children
        if obj.children:
            entry["all_children"] = (
                _all_children([c.name for c in obj.children], self.objects) or None
            )

        # remotes: only the repos the object ITSELF advertises in its remote
        # section. Manifest-level remotes belong to the manifest root node
        # only — attaching them to root objects would falsely make every
        # top-level object advertise the user's registered repos.
        remote_names = self._object_remote_names(obj, url_to_name)
        if remote_names:
            entry["remotes"] = remote_names

            # all_remotes = transitive closure through remote chains
            all_remote_set = _all_remotes(remote_names, remote_objects, self.objects)
            if all_remote_set:
                entry["all_remotes"] = all_remote_set

        return entry

    @staticmethod
    def _object_remote_names(obj: ResolvedObject, url_to_name: dict[str, str]) -> list[str]:
        """Names (crawled name, else URL) of the repos in *obj*'s ``remote.repos``, deduplicated."""
        obj_remote = obj.data.get("remote", {})
        remote_names: list[str] = []
        if isinstance(obj_remote, dict):
            for url in obj_remote.get("repos", []):
                rname = url_to_name.get(url, url)
                if rname not in remote_names:
                    remote_names.append(rname)
        return remote_names

    def _manifest_root_entry(
        self,
        objects_out: dict[str, dict],
        remote_objects: dict[str, dict],
        url_to_name: dict[str, str],
    ) -> dict:
        """The ``manifest_root`` node: the tree root above every root-level object.

        Its ``all_remotes`` covers the manifest's own remotes plus every
        remote any entry in *objects_out* (local or remote) lists.
        """
        # Root children = all root-level objects (no parent)
        root_children = [n for n, o in self.objects.items() if o.parent is None]
        # Root remotes = manifest-level repo names
        root_remotes = [url_to_name.get(u, u) for u in self.manifest_remotes]

        root_all_children = _all_children(root_children, self.objects)

        # Aggregate from manifest's own remotes PLUS all descendants' remotes
        all_obj_remote_names: set[str] = set(root_remotes)
        for entry in objects_out.values():
            for rn in entry.get("remotes", []):
                all_obj_remote_names.add(rn)

        root_all_remotes = _all_remotes(list(all_obj_remote_names), remote_objects, self.objects)

        return {
            "name": "o3de_manifest",
            "path": self.manifest_path.as_posix(),
            "children": root_children,
            "all_children": root_all_children if root_all_children else None,
            "remotes": root_remotes,
            "all_remotes": root_all_remotes if root_all_remotes else None,
        }

    def load_resolved(self) -> dict:
        """Load existing resolved manifest."""
        if not self.resolved_path.exists():
            raise ResolverError("No resolved manifest. Run resolve() first.")

        with open(self.resolved_path) as f:
            return json.load(f)


def check_files_changed(resolved_path: Path | None = None) -> tuple[bool, list[str]]:
    """
    Check if any tracked files have changed since last resolution.

    Reads the file_hashes from the resolved manifest and compares against
    current file hashes.

    Args:
        resolved_path: Path to resolved manifest (default: ~/.o3de/resolved_o3de_manifest.json)

    Returns:
        Tuple of (has_changes, list_of_changed_files)
    """
    if resolved_path is None:
        resolved_path = get_resolved_manifest_path()

    if not resolved_path.exists():
        return True, ["resolved manifest not found"]

    try:
        with open(resolved_path) as f:
            resolved_data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return True, ["failed to read resolved manifest"]

    stored_hashes = resolved_data.get("file_hashes", {})
    if not stored_hashes:
        return True, ["no hashes stored"]

    changed_files = []

    for file_path, stored_hash in stored_hashes.items():
        path = Path(file_path)
        if not path.exists():
            changed_files.append(f"deleted: {file_path}")
            continue

        current_hash = compute_file_hash(path)
        if current_hash != stored_hash:
            changed_files.append(file_path)

    return bool(changed_files), changed_files


def resolve_manifest(
    manifest_path: Path | None = None,
    save: bool = True,
    dry_run: bool = False,
    progress_callback: Callable[[str, int, int], None] | None = None,
) -> Resolver:
    """
    Convenience function to resolve the manifest.

    Args:
        manifest_path: Path to manifest (default: ~/.o3de/o3de_manifest.json)
        save: Whether to save resolved manifest
        dry_run: If True, resolve but don't write to disk
        progress_callback: Progress callback

    Returns:
        Resolver with resolved objects
    """
    resolver = Resolver(manifest_path, dry_run=dry_run)
    resolver.resolve(progress_callback)

    if save:
        resolver.save()

    return resolver


def load_resolved_manifest(
    force_refresh: bool = False,
    progress_callback: Callable[[str, int, int], None] | None = None,
) -> dict:
    """
    Load the resolved manifest, using cached version if files haven't changed.

    This is the recommended way for GUIs and tools to get resolved data - it
    avoids re-resolving when nothing has changed, dramatically improving
    startup time.

    The returned dict contains precomputed fields for each object:
    - display_metadata: {display_name, summary, icon_path}
    - git_info: {remote_url, branch}
    - parents: [{name, path}, ...] ancestry chain to root
    - dependents: reverse dependencies

    Args:
        force_refresh: If True, re-resolve even if files haven't changed
        progress_callback: Progress callback for resolution

    Returns:
        Dict with resolved manifest data including precomputed fields
    """
    resolved_path = get_resolved_manifest_path()

    # Check if we can use cached version
    if not force_refresh and resolved_path.exists():
        has_changes, changed_files = check_files_changed(resolved_path)
        if not has_changes:
            # Load from cache
            logger.info("Using cached resolved manifest (no file changes)")
            with open(resolved_path) as f:
                return json.load(f)
        else:
            logger.info(f"Re-resolving due to {len(changed_files)} changed files")

    # Resolve fresh
    resolve_manifest(progress_callback=progress_callback)

    # Return the saved data
    with open(resolved_path) as f:
        return json.load(f)
