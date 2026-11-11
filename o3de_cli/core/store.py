# O3DE CLI - Store / Remote Object Fetcher
# SPDX-License-Identifier: Apache-2.0 OR MIT

"""
O3DE Store Interface.

The Store is a tree of repo.json files describing available O3DE objects.
Starting from the manifest's remote repos, we descend all remote links,
caching JSON files locally for offline use.

Store operations:
1. Refresh - Download/update all remote object metadata
2. Search - Find objects by name, tags, type
3. Download - Clone git repo or download release archive
4. Cache - Maintain local cache of remote metadata

Cache structure (~/.o3de/Cache/):
  <sha256_of_url>/
    object.json     - Cached JSON
    metadata.json   - Cache metadata (timestamp, etag, etc)
"""

import hashlib
import json
import logging
import os
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union
from urllib.parse import urlparse

import httpx

from .models import (
    Engine,
    Gem,
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
from .paths import get_cache_path, get_download_path

logger = logging.getLogger("o3de_cli.store")


class StoreError(Exception):
    """Error during store operations."""

    pass


class FetchError(StoreError):
    """Error fetching remote resource."""

    pass


class IntegrityError(StoreError):
    """SHA-256 integrity check failed after download."""

    pass


def compute_sha256(path: Path) -> str:
    """Compute SHA-256 hash of a file or directory.

    For files: hash the file contents directly.
    For directories: hash all files sorted by relative path.

    Returns:
        Hex digest of the SHA-256 hash
    """
    h = hashlib.sha256()
    if path.is_file():
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
    elif path.is_dir():
        for file_path in sorted(path.rglob("*")):
            if file_path.is_file():
                rel = file_path.relative_to(path).as_posix().encode()
                h.update(rel)
                with open(file_path, "rb") as f:
                    for chunk in iter(lambda: f.read(8192), b""):
                        h.update(chunk)
    return h.hexdigest()


def verify_integrity(path: Path, expected_sha256: str) -> bool:
    """Verify a downloaded artifact against an expected SHA-256 hash.

    Args:
        path: Path to file or directory to verify
        expected_sha256: Expected SHA-256 hex digest

    Returns:
        True if hash matches

    Raises:
        IntegrityError: If hash does not match
    """
    actual = compute_sha256(path)
    if actual != expected_sha256:
        raise IntegrityError(
            f"Integrity check failed for {path}:\n"
            f"  expected: {expected_sha256}\n"
            f"  actual:   {actual}"
        )
    logger.info(f"Integrity verified: {path}")
    return True


class RemoteObject:
    """Metadata about a remote object."""

    def __init__(
        self,
        url: str,
        object_type: ObjectType,
        name: str = "",
        version: str = "",
        display_name: str = "",
        summary: str = "",
        description: str = "",
        origin: str = "",
        origin_url: str = "",
        license: str = "",
        license_url: str = "",
        icon_url: str = "",
        icon_relative_path: str = "",
        documentation_url: str = "",
        source_control_url: str | None = None,
        source_control_branch: str | None = None,
        source_control_tag: str | None = None,
        download_url: str | None = None,
        gem_type: str = "",
        tags: list[str] | None = None,
        cached_at: datetime | None = None,
        # Integrity
        source_sha256: str | None = None,
        # Parent repo info for inheritance
        parent_repo_url: str | None = None,
        inherited_source_control_url: str | None = None,
        inherited_source_control_branch: str | None = None,
        # Path of this object within its containing repo (for subdir extraction)
        repo_relative_path: str | None = None,
        # Dependencies (specifier strings for transitive solving)
        dependencies: list[str] | None = None,
    ):
        self.url = url
        self.object_type = object_type
        self.name = name
        self.version = version
        self.display_name = display_name
        self.summary = summary
        self.description = description
        self.origin = origin
        self.origin_url = origin_url
        self.license = license
        self.license_url = license_url
        self.icon_url = icon_url
        self.icon_relative_path = icon_relative_path
        self.documentation_url = documentation_url
        self.source_control_url = source_control_url
        self.source_control_branch = source_control_branch
        self.source_control_tag = source_control_tag
        self.download_url = download_url
        self.gem_type = gem_type
        self.tags = tags or []
        self.cached_at = cached_at
        # Integrity
        self.source_sha256 = source_sha256
        # Parent repo info
        self.parent_repo_url = parent_repo_url
        self.inherited_source_control_url = inherited_source_control_url
        self.inherited_source_control_branch = inherited_source_control_branch
        self.repo_relative_path = repo_relative_path
        # Dependencies (parsed from dependent field for transitive solving)
        self.dependencies: list[str] = dependencies or []

    @property
    def effective_source_control_url(self) -> str | None:
        """Get source control URL - own or inherited from parent."""
        return self.source_control_url or self.inherited_source_control_url

    @property
    def effective_source_control_branch(self) -> str | None:
        """Get source control branch - own or inherited from parent."""
        if self.source_control_url:
            return self.source_control_branch
        return self.inherited_source_control_branch

    def __repr__(self) -> str:
        return f"RemoteObject({self.object_type.value}:{self.name}@{self.version})"


class Cache:
    """Local cache for remote JSON files."""

    def __init__(self, cache_dir: Path | None = None):
        self.cache_dir = cache_dir or get_cache_path()
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _url_to_cache_path(self, url: str) -> Path:
        """Convert URL to cache directory path."""
        url_hash = hashlib.sha256(url.encode()).hexdigest()
        return self.cache_dir / url_hash

    def get(self, url: str) -> dict | None:
        """Get cached JSON for URL, or None if not cached."""
        cache_path = self._url_to_cache_path(url)
        json_path = cache_path / "object.json"

        if json_path.exists():
            try:
                with open(json_path) as f:
                    return json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                logger.warning(f"Failed to read cache for {url}: {e}")

        return None

    def get_metadata(self, url: str) -> dict | None:
        """Get cache metadata for URL."""
        cache_path = self._url_to_cache_path(url)
        meta_path = cache_path / "metadata.json"

        if meta_path.exists():
            try:
                with open(meta_path) as f:
                    return json.load(f)
            except (OSError, json.JSONDecodeError):
                pass

        return None

    def put(self, url: str, data: dict, etag: str | None = None) -> None:
        """Store JSON in cache."""
        cache_path = self._url_to_cache_path(url)
        cache_path.mkdir(parents=True, exist_ok=True)

        # Write object JSON
        json_path = cache_path / "object.json"
        with open(json_path, "w") as f:
            json.dump(data, f, indent=2)

        # Write metadata
        meta_path = cache_path / "metadata.json"
        metadata = {
            "url": url,
            "cached_at": datetime.now(timezone.utc).isoformat(),
            "etag": etag,
        }
        with open(meta_path, "w") as f:
            json.dump(metadata, f, indent=2)

    def is_stale(self, url: str, max_age_hours: int = 24) -> bool:
        """Check if cache entry is stale."""
        meta = self.get_metadata(url)
        if not meta:
            return True

        cached_at = datetime.fromisoformat(meta.get("cached_at", "1970-01-01T00:00:00+00:00"))
        age = datetime.now(timezone.utc) - cached_at
        return age.total_seconds() > (max_age_hours * 3600)

    def clear(self, url: str | None = None) -> int:
        """Clear cache. If url provided, clear only that entry. Returns count cleared."""
        import shutil

        if url:
            cache_path = self._url_to_cache_path(url)
            if cache_path.exists():
                shutil.rmtree(cache_path)
                return 1
            return 0
        else:
            count = 0
            for entry in self.cache_dir.iterdir():
                if entry.is_dir():
                    shutil.rmtree(entry)
                    count += 1
            return count


def get_manifest_remote_urls(manifest_path: Path | None = None) -> list[str]:
    """Collect remote repo URLs from the user manifest.

    Handles Schema 2.0.0 (``remote.repos``), legacy flat ``remotes``,
    and URLs that ended up in ``local.repos`` / root ``repos``.
    """
    from .paths import get_manifest_path

    path = manifest_path or get_manifest_path()
    if not path.exists():
        return []
    try:
        with open(path) as f:
            manifest = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []

    urls: list[str] = []

    remote = manifest.get("remote", {})
    if isinstance(remote, dict):
        urls.extend(u for u in _as_list(remote.get("repos")) if isinstance(u, str))

    for u in manifest.get("remotes", []) or []:
        if isinstance(u, str):
            urls.append(u)

    local = manifest.get("local", {})
    repo_paths = (
        (local.get("repos", []) if isinstance(local, dict) else [])
        or manifest.get("repos", [])
        or []
    )
    for u in repo_paths:
        if isinstance(u, str) and u.startswith(("http://", "https://")):
            urls.append(u)

    # De-dupe, preserve order
    seen: set[str] = set()
    result = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            result.append(u)
    return result


# ----------------------------------------------------------------------
# Remote-object parsing helpers
#
# Pure functions that pull one group of fields out of a fetched object
# document. ``nested`` is the document's type header (``data["gem"]``
# etc. in Schema 2.0.0); legacy documents keep the same keys at the root.
# ----------------------------------------------------------------------

# A refresh_sync queue item: (url, parent_repo_url, inherited_sc_url, inherited_sc_branch)
_WaveItem = tuple[str, str | None, str | None, str | None]


def _header_field(nested: dict, data: dict, key: str, default: str = "") -> str:
    """*key* from the type header, else from the document root, else *default* (first truthy)."""
    return nested.get(key) or data.get(key) or default


def _icon_fields(nested: dict, data: dict) -> tuple[str, str]:
    """``(icon_url, icon_relative_path)`` of a document.

    The icon is an object (``uri``/``url`` + ``relative_path``) or, in
    legacy documents, a plain URL string; flat ``icon_uri``/``icon_url``
    fields are the fallback for the URL.
    """
    icon_data = nested.get("icon") or data.get("icon") or {}
    if isinstance(icon_data, dict):
        icon_url = icon_data.get("uri") or icon_data.get("url") or ""
        icon_relative_path = icon_data.get("relative_path") or ""
    else:
        # Legacy: might be a direct URL string
        icon_url = str(icon_data) if icon_data else ""
        icon_relative_path = ""

    # Fallback to flat fields if icon was not found
    if not icon_url:
        icon_url = _header_field(nested, data, "icon_uri", _header_field(nested, data, "icon_url"))
    return icon_url, icon_relative_path


def _tag_list(data: dict) -> Any:
    """``user_tags``, else ``canonical_tags``; a single string becomes a one-item list."""
    tags = data.get("user_tags") or data.get("canonical_tags") or []
    if isinstance(tags, str):
        tags = [tags]
    return tags


def _release_source_control(latest: Any, url: Any, branch: Any, tag: Any) -> tuple[Any, Any, Any]:
    """Source control from a release's first ``source_controls`` entry with a git URL.

    Its branch and tag override *branch*/*tag* only when set. Returns
    ``(url, branch, tag)`` unchanged if the release has no such entry.
    """
    if isinstance(latest, dict):
        for sc in latest.get("source_controls", []) or []:
            if isinstance(sc, dict) and sc.get("git"):
                return sc["git"], sc.get("branch") or branch, sc.get("tag") or tag
    return url, branch, tag


def _source_control_fields(nested: dict, data: dict) -> tuple[Any, Any, Any]:
    """``(source_control_url, branch, tag)`` of a document.

    The URL comes from ``download_source_uri``, ``repo_uri`` or the
    ``source_control`` block. With no URL there, Schema 2.0.0 releases
    carry immutable acquisition pointers (git url + tag stamped at
    release-cut time), so the latest release's entry is used.
    """
    source_control_data = data.get("source_control", {}) or {}
    source_control_url = (
        _header_field(nested, data, "download_source_uri")
        or _header_field(nested, data, "repo_uri")
        or source_control_data.get("uri")
        or source_control_data.get("git")
    )
    source_control_branch = source_control_data.get("branch") or ""
    source_control_tag = source_control_data.get("tag") or ""

    releases = data.get("releases", [])
    if not source_control_url and isinstance(releases, list) and releases:
        return _release_source_control(
            releases[-1], source_control_url, source_control_branch, source_control_tag
        )
    return source_control_url, source_control_branch, source_control_tag


def _repo_relative_path(url: str, parent_repo_url: str | None) -> str | None:
    """Directory of the document at *url* relative to its containing repo's directory.

    Used to extract a contained object from a repo clone. None when there
    is no parent repo or *url* isn't under the repo's directory.
    """
    if not parent_repo_url:
        return None
    repo_base = parent_repo_url.rsplit("/", 1)[0] + "/"
    if not url.startswith(repo_base):
        return None
    rel = url[len(repo_base) :]
    # Strip the trailing json filename, keep the directory
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


def _download_fields(nested: dict, data: dict) -> tuple[Any, Any]:
    """``(download_url, source_sha256)`` of a document's release archive.

    The URL is ``download_uri`` or the ``download`` block's ``source``;
    the hash comes from the ``download`` block.
    """
    download_data = data.get("download", {}) or {}
    download_url = _header_field(nested, data, "download_uri") or download_data.get("source")
    return download_url, download_data.get("source_sha256")


def _dependency_specs(nested: dict, data: dict) -> list[str]:
    """Dependency specifier strings from ``dependent`` (type header first, then root).

    Schema 2.0: ``{"gems": [...], "engines": [...]}``; non-list values
    and non-string entries are ignored.
    """
    dep_specs: list[str] = []
    dependent = nested.get("dependent", {}) if isinstance(nested, dict) else {}
    if not dependent:
        dependent = data.get("dependent", {})
    if isinstance(dependent, dict):
        for dep_list in dependent.values():
            if isinstance(dep_list, list):
                for dep in dep_list:
                    if isinstance(dep, str):
                        dep_specs.append(dep)
    return dep_specs


def _as_list(value: object) -> list:
    """*value* if it is a list, else an empty list.

    Third-party repo documents sometimes put a string where a list belongs;
    iterating it would yield one bogus "URL" per character.
    """
    return value if isinstance(value, list) else []


def _object_type_or_none(url: str, data: object) -> ObjectType | None:
    """The object type of a fetched document, or None (with a warning) if it is not one.

    Remote repo trees are third-party; one malformed or foreign document must
    not abort a whole refresh.
    """
    if not isinstance(data, dict):
        logger.warning(f"Skipping {url}: not a JSON object")
        return None
    try:
        return get_object_type(data)
    except ValueError as e:
        logger.warning(f"Skipping {url}: not an O3DE object document ({e})")
        return None


class Store:
    """
    O3DE Store interface for browsing and downloading remote objects.

    Usage:
        store = Store()
        await store.refresh()  # Download all remote metadata

        gems = store.search("physics", object_type=ObjectType.GEM)
        await store.download(gems[0], target_path)
    """

    def __init__(
        self,
        cache: Cache | None = None,
        timeout: float = 30.0,
    ):
        self.cache = cache or Cache()
        self.timeout = timeout

        # All discovered remote objects (keyed by type:name, latest version only)
        self.objects: dict[str, RemoteObject] = {}

        # All versions of each object: {"type:name": {"version": RemoteObject}}
        self.versions: dict[str, dict[str, RemoteObject]] = {}

        # URLs we've already visited (to avoid cycles)
        self._visited_urls: set[str] = set()

    async def fetch_json(
        self,
        url: str,
        use_cache: bool = True,
        force_refresh: bool = False,
    ) -> dict:
        """
        Fetch JSON from URL, using cache if available.

        Args:
            url: URL to fetch
            use_cache: Whether to use cached version
            force_refresh: Force download even if cached

        Returns:
            Parsed JSON dict
        """
        # Check cache first
        if use_cache and not force_refresh:
            cached = self.cache.get(url)
            if cached and not self.cache.is_stale(url):
                logger.debug(f"Using cached: {url}")
                return cached

        # Fetch from remote
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(url)
                response.raise_for_status()

                data = response.json()
                etag = response.headers.get("etag")

                # Cache the result
                self.cache.put(url, data, etag)

                return data

        except httpx.HTTPError as e:
            # Try to use stale cache
            if use_cache:
                cached = self.cache.get(url)
                if cached:
                    logger.warning(f"Fetch failed, using stale cache: {url}")
                    return cached

            raise FetchError(f"Failed to fetch {url}: {e}")
        except json.JSONDecodeError as e:
            raise FetchError(f"Invalid JSON at {url}: {e}")

    def fetch_json_sync(
        self,
        url: str,
        use_cache: bool = True,
        force_refresh: bool = False,
    ) -> dict:
        """Synchronous version of fetch_json."""
        with httpx.Client(timeout=self.timeout) as client:
            return self._fetch_json_with_client(
                client, url, use_cache=use_cache, force_refresh=force_refresh
            )

    def _fetch_json_with_client(
        self,
        client: httpx.Client,
        url: str,
        use_cache: bool = True,
        force_refresh: bool = False,
    ) -> dict:
        """Fetch one JSON document through *client*, with caching.

        A fresh cache entry is returned without a request. On a failed
        request a stale cache entry is returned if there is one; otherwise
        FetchError is raised. Shared by ``fetch_json_sync`` and the parallel
        ``refresh_sync``, which reuses one client across many fetches.
        """
        cached = self.cache.get(url) if use_cache else None
        if cached and not force_refresh and not self.cache.is_stale(url):
            logger.debug(f"Using cached: {url}")
            return cached

        try:
            response = client.get(url)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, json.JSONDecodeError) as e:
            if cached:
                logger.warning(f"Fetch failed, using stale cache: {url}")
                return cached
            raise FetchError(f"Failed to fetch {url}: {e}") from e

        self.cache.put(url, data, response.headers.get("etag"))
        return data

    async def refresh(
        self,
        repo_urls: list[str],
        progress_callback: Callable[[str, int, int], None] | None = None,
    ) -> int:
        """
        Refresh store by descending all repo trees.

        Args:
            repo_urls: Starting repo.json URLs
            progress_callback: Optional callback(message, current, total)

        Returns:
            Number of objects discovered
        """
        self._visited_urls.clear()
        self.objects.clear()

        # Queue of URLs to process
        queue = list(repo_urls)
        total = len(queue)
        processed = 0

        while queue:
            url = queue.pop(0)

            if url in self._visited_urls:
                continue

            self._visited_urls.add(url)
            processed += 1

            if progress_callback:
                progress_callback(f"Fetching {urlparse(url).path}", processed, total)

            try:
                data = await self.fetch_json(url)
            except FetchError as e:
                logger.warning(f"Skipping {url}: {e}")
                continue

            # Parse object and extract remote links
            obj_type = _object_type_or_none(url, data)
            if obj_type is None:
                continue
            remote_obj = self._parse_remote_object(url, data, obj_type)

            if remote_obj:
                self._record_remote_object(remote_obj)

            # Queue any remote links
            new_urls = self._extract_remote_urls(data, base_url=url)
            for new_url in new_urls:
                if new_url not in self._visited_urls:
                    queue.append(new_url)
                    total += 1

        if progress_callback:
            progress_callback("Complete", total, total)

        logger.info(f"Store refresh complete: {len(self.objects)} objects")
        return len(self.objects)

    def _record_remote_object(self, remote_obj: RemoteObject) -> None:
        """Add *remote_obj* to ``versions``; make it the ``objects`` entry if it's the newest.

        Both are keyed ``"<type>:<name>"``; a missing version counts as "0.0.0".
        """
        key = f"{remote_obj.object_type.value}:{remote_obj.name}"
        version = remote_obj.version or "0.0.0"

        # Track all versions
        if key not in self.versions:
            self.versions[key] = {}
        self.versions[key][version] = remote_obj

        # Keep latest version in objects dict for backwards compatibility
        if key not in self.objects or self._is_newer_version(version, self.objects[key].version):
            self.objects[key] = remote_obj

    def refresh_sync(
        self,
        repo_urls: list[str],
        progress_callback: Callable[[str, int, int], None] | None = None,
        max_concurrency: int = 32,
    ) -> int:
        """Synchronous version of refresh with parallel fetching.

        Uses wave-based BFS: fetches all URLs at the current level
        concurrently, then discovers child URLs for the next wave.
        """
        self._visited_urls.clear()
        self.objects.clear()

        # Queue items are tuples: (url, parent_repo_url, inherited_sc_url, inherited_sc_branch)
        wave: list[_WaveItem] = [(url, None, None, None) for url in repo_urls]
        total = len(wave)
        processed = 0

        with httpx.Client(timeout=self.timeout) as client:
            while wave:
                # De-duplicate and skip already-visited within this wave
                pending = self._claim_unvisited(wave)
                if not pending:
                    break

                # Parallel fetch all URLs in this wave
                url_to_data = self._fetch_wave(
                    client, [item[0] for item in pending], max_concurrency
                )

                # Process results and build next wave
                next_wave: list[_WaveItem] = []

                for item in pending:
                    url = item[0]
                    processed += 1
                    if progress_callback:
                        progress_callback(f"Fetching {urlparse(url).path}", processed, total)

                    data = url_to_data.get(url)
                    if data is None:
                        continue

                    for child in self._ingest_document(item, data):
                        if child[0] not in self._visited_urls:
                            next_wave.append(child)
                            total += 1

                wave = next_wave

        return len(self.objects)

    def _claim_unvisited(self, wave: list[_WaveItem]) -> list[_WaveItem]:
        """The items of *wave* whose URL is new, marking each URL visited (first one wins)."""
        pending: list[_WaveItem] = []
        for item in wave:
            url = item[0]
            if url not in self._visited_urls:
                self._visited_urls.add(url)
                pending.append(item)
        return pending

    def _fetch_or_none(self, client: httpx.Client, url: str) -> tuple[str, dict | None]:
        """Fetch a single URL, returning (url, data_or_None); a FetchError is logged."""
        try:
            return (url, self._fetch_json_with_client(client, url))
        except FetchError as e:
            logger.warning(f"Skipping {url}: {e}")
            return (url, None)

    def _fetch_wave(
        self, client: httpx.Client, urls: list[str], max_concurrency: int
    ) -> dict[str, dict | None]:
        """Fetch *urls* in parallel through one *client*: ``{url: data_or_None}``."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        url_to_data: dict[str, dict | None] = {}
        with ThreadPoolExecutor(max_workers=min(max_concurrency, len(urls))) as executor:
            futures = {executor.submit(self._fetch_or_none, client, url): url for url in urls}
            for future in as_completed(futures):
                url, data = future.result()
                url_to_data[url] = data
        return url_to_data

    def _ingest_document(self, item: _WaveItem, data: dict) -> list[_WaveItem]:
        """Record the object a fetched document describes; return the queue items it links to.

        Children inherit the source control of the nearest enclosing repo:
        a repo passes on its own URL and source control (falling back to
        what it inherited), any other object passes on what it inherited.
        A document that is not an O3DE object is skipped with a warning.
        """
        url, parent_repo_url, inherited_sc_url, inherited_sc_branch = item

        obj_type = _object_type_or_none(url, data)
        if obj_type is None:
            return []
        remote_obj = self._parse_remote_object(
            url,
            data,
            obj_type,
            parent_repo_url=parent_repo_url,
            inherited_source_control_url=inherited_sc_url,
            inherited_source_control_branch=inherited_sc_branch,
        )

        if remote_obj:
            self._record_remote_object(remote_obj)

        # Determine source control info to pass to children
        child_repo_url: str | None = None
        child_sc_url: str | None = None
        child_sc_branch: str | None = None

        if remote_obj and obj_type == ObjectType.REPO:
            child_repo_url = url
            child_sc_url = remote_obj.source_control_url or inherited_sc_url
            child_sc_branch = remote_obj.source_control_branch or inherited_sc_branch
        else:
            child_repo_url = parent_repo_url
            child_sc_url = inherited_sc_url
            child_sc_branch = inherited_sc_branch

        return [
            (new_url, child_repo_url, child_sc_url, child_sc_branch)
            for new_url in self._extract_remote_urls(data, base_url=url)
        ]

    def _parse_remote_object(
        self,
        url: str,
        data: dict,
        obj_type: ObjectType,
        parent_repo_url: str | None = None,
        inherited_source_control_url: str | None = None,
        inherited_source_control_branch: str | None = None,
    ) -> RemoteObject | None:
        """Parse JSON into RemoteObject.

        Each field is read from the Schema 2.0.0 type header first, then
        from the document root (legacy). Returns None, with a warning, if
        anything in the document can't be parsed.
        """
        try:
            name = get_object_name(data)
            version = get_object_version(data)

            # Try nested structure first (Schema 2.0.0), then flat structure (legacy)
            header_key = obj_type.value if obj_type != ObjectType.MANIFEST else "o3de_manifest"
            nested = data.get(header_key, {})

            display_name = _header_field(nested, data, "display_name", name)
            summary = _header_field(nested, data, "summary")
            description = _header_field(nested, data, "description", summary)
            origin = _header_field(nested, data, "origin")
            origin_url = _header_field(nested, data, "origin_url")
            license_text = _header_field(nested, data, "license")
            license_url = _header_field(
                nested, data, "license_url", _header_field(nested, data, "license_link")
            )
            icon_url, icon_relative_path = _icon_fields(nested, data)
            documentation_url = _header_field(nested, data, "documentation_url")
            gem_type = _header_field(nested, data, "type")
            tags = _tag_list(data)
            source_control_url, source_control_branch, source_control_tag = _source_control_fields(
                nested, data
            )
            repo_relative_path = _repo_relative_path(url, parent_repo_url)

            download_url, source_sha256 = _download_fields(nested, data)
            dep_specs = _dependency_specs(nested, data)

            return RemoteObject(
                url=url,
                object_type=obj_type,
                name=name,
                version=version,
                display_name=display_name,
                summary=summary,
                description=description,
                origin=origin,
                origin_url=origin_url,
                license=license_text,
                license_url=license_url,
                icon_url=icon_url,
                icon_relative_path=icon_relative_path,
                documentation_url=documentation_url,
                source_control_url=source_control_url,
                source_control_branch=source_control_branch,
                source_control_tag=source_control_tag,
                download_url=download_url,
                source_sha256=source_sha256,
                gem_type=gem_type,
                tags=tags,
                cached_at=datetime.now(timezone.utc),
                parent_repo_url=parent_repo_url,
                inherited_source_control_url=inherited_source_control_url,
                inherited_source_control_branch=inherited_source_control_branch,
                repo_relative_path=repo_relative_path,
                dependencies=dep_specs,
            )
        except Exception as e:
            logger.warning(f"Failed to parse object at {url}: {e}")
            return None

    def _extract_remote_urls(self, data: dict, base_url: str = "") -> list[str]:
        """Extract all remote object URLs from JSON.

        Args:
            data: Parsed object JSON
            base_url: URL this JSON was fetched from; used to resolve
                Schema 2.0.0 ``children`` relative paths against the
                repo's own location.
        """
        urls: list[str] = []

        # Check for nested remote structure (manifest / repo 2.0.0 style)
        # remote.* entries are absolute URLs to OTHER objects (federation)
        remote = data.get("remote", {})
        if isinstance(remote, dict):
            for key in ["engines", "projects", "gems", "templates", "repos", "overlays"]:
                urls.extend(u for u in _as_list(remote.get(key)) if isinstance(u, str))

        # Schema 2.0.0: children.* entries are paths relative to this
        # object's JSON location (contained objects)
        children = data.get("children", {})
        if isinstance(children, dict) and base_url:
            base_dir = base_url.rsplit("/", 1)[0] + "/"
            for key in ["engines", "projects", "gems", "templates", "repos", "overlays"]:
                for rel in _as_list(children.get(key)):
                    if not isinstance(rel, str) or not rel:
                        continue
                    if rel.startswith(("http://", "https://")):
                        urls.append(rel)
                    else:
                        urls.append(base_dir + rel.lstrip("/"))

        # Also check top-level arrays (legacy repo.json style)
        for key in ["engines", "projects", "gems", "templates", "repos", "overlays"]:
            top_level = data.get(key, [])
            if isinstance(top_level, list):
                urls.extend(u for u in top_level if isinstance(u, str))

        return urls

    def search(
        self,
        query: str = "",
        object_type: ObjectType | None = None,
        tags: list[str] | None = None,
    ) -> list[RemoteObject]:
        """
        Search for objects in the store.

        Args:
            query: Text to search in name, display_name, description
            object_type: Filter by object type
            tags: Filter by tags (any match)

        Returns:
            List of matching RemoteObjects
        """
        results = []
        query_lower = query.lower()

        for obj in self.objects.values():
            # Filter by type
            if object_type and obj.object_type != object_type:
                continue

            # Filter by query
            if query:
                searchable = f"{obj.name} {obj.display_name} {obj.description}".lower()
                if query_lower not in searchable:
                    continue

            results.append(obj)

        # Sort by relevance (name match first, then alphabetically)
        results.sort(key=lambda o: (0 if query_lower in o.name.lower() else 1, o.name))

        return results

    def get_by_name(self, object_type: ObjectType, name: str) -> RemoteObject | None:
        """Get a specific object by type and name."""
        key = f"{object_type.value}:{name}"
        return self.objects.get(key)

    def get_versions(self, object_type: ObjectType, name: str) -> list[str]:
        """Get all available versions for an object, sorted newest first."""
        key = f"{object_type.value}:{name}"
        versions_dict = self.versions.get(key, {})
        versions = list(versions_dict.keys())
        # Sort by version (try semver-style, fallback to string)
        versions.sort(key=self._version_sort_key, reverse=True)
        return versions

    def get_version(
        self, object_type: ObjectType | str, name: str, version: str
    ) -> RemoteObject | None:
        """Get a specific version of an object."""
        type_str = object_type.value if isinstance(object_type, ObjectType) else object_type
        key = f"{type_str}:{name}"
        versions_dict = self.versions.get(key, {})
        return versions_dict.get(version)

    def _is_newer_version(self, v1: str, v2: str) -> bool:
        """Check if v1 is newer than v2."""
        return self._version_sort_key(v1) > self._version_sort_key(v2)

    def _version_sort_key(self, version: str) -> tuple:
        """Generate a sort key for version strings."""
        # Try to parse as semver-like version
        parts = version.split(".")
        result = []
        for part in parts:
            # Extract leading number
            num = ""
            suffix = ""
            for i, c in enumerate(part):
                if c.isdigit():
                    num += c
                else:
                    suffix = part[i:]
                    break
            result.append((int(num) if num else 0, suffix))
        # Pad to ensure consistent comparison
        while len(result) < 4:
            result.append((0, ""))
        return tuple(result)

    # -- Download helpers -----------------------------------------------------
    #
    # Everything a remote object names (clone URL, ref, archive URL) comes from
    # third-party metadata, so it goes through core.safety first.

    @staticmethod
    def _git_clone(url: str, dest: Path, ref: str | None = None, shallow: bool = True) -> None:
        """Clone *url* into *dest* under the transport and argument rules."""
        import subprocess

        from .safety import git_clone_env, require_safe_git_ref, require_safe_git_url

        require_safe_git_url(url)
        require_safe_git_ref(ref)
        cmd = ["git", "clone"]
        if shallow:
            cmd += ["--depth", "1"]
        if ref:
            cmd += ["--branch", ref]
        # "--" so the URL can never be parsed as an option.
        cmd += ["--", url, str(dest)]
        result = subprocess.run(cmd, capture_output=True, text=True, env=git_clone_env())
        if result.returncode != 0:
            raise StoreError(f"Git clone failed: {result.stderr}")

    @staticmethod
    def _archive_checks(remote_obj: RemoteObject, allow_unverified: bool) -> tuple[str, str | None]:
        """Validate an archive download before any bytes are fetched."""
        from .safety import require_checksum, require_secure_url

        url = remote_obj.download_url or ""
        require_secure_url(url)
        require_checksum(url, remote_obj.source_sha256, allow_unverified)
        return url, remote_obj.source_sha256

    @staticmethod
    def _unpack_verified(archive_path: Path, expected_sha256: str | None, dest: Path) -> None:
        """Check the archive's SHA-256, then unpack it safely, then delete it."""
        from .safety import safe_extract, verify_file_sha256

        try:
            if expected_sha256:
                verify_file_sha256(archive_path, expected_sha256)
            safe_extract(archive_path, dest)
        finally:
            archive_path.unlink(missing_ok=True)

    async def download(
        self,
        remote_obj: RemoteObject,
        target_path: Path,
        prefer_source_control: bool = True,
        progress_callback: Callable[[str, int, int], None] | None = None,
        allow_unverified: bool = False,
    ) -> Path:
        """
        Download a remote object to local disk.

        Args:
            remote_obj: Object to download
            target_path: Where to download (parent directory)
            prefer_source_control: Prefer git clone over archive download
            progress_callback: Progress callback
            allow_unverified: Accept an archive that publishes no SHA-256

        Returns:
            Path to downloaded object

        Raises:
            IntegrityError: If the archive does not match its published SHA-256
            UnsafeSourceError: If the URL, ref or archive is refused
        """
        from .safety import async_download_client

        obj_name = remote_obj.name.split(".")[-1]  # Last segment of reverse domain

        if prefer_source_control and remote_obj.source_control_url:
            clone_path = target_path / obj_name
            if progress_callback:
                progress_callback(f"Cloning {remote_obj.source_control_url}", 0, 1)
            self._git_clone(remote_obj.source_control_url, clone_path, shallow=False)
            if progress_callback:
                progress_callback("Clone complete", 1, 1)
            return clone_path

        if remote_obj.download_url:
            url, expected_sha256 = self._archive_checks(remote_obj, allow_unverified)
            if progress_callback:
                progress_callback(f"Downloading {url}", 0, 1)

            download_dir = get_download_path()
            download_dir.mkdir(parents=True, exist_ok=True)
            archive_path = download_dir / f"{remote_obj.name}.archive"
            async with async_download_client() as client:
                async with client.stream("GET", url) as response:
                    response.raise_for_status()
                    with open(archive_path, "wb") as f:
                        async for chunk in response.aiter_bytes():
                            f.write(chunk)

            extract_path = target_path / obj_name
            self._unpack_verified(archive_path, expected_sha256, extract_path)
            if progress_callback:
                progress_callback("Download complete", 1, 1)
            return extract_path

        raise StoreError(f"No download method available for {remote_obj.name}")

    def download_sync(
        self,
        remote_obj: RemoteObject,
        target_path: Path,
        prefer_source_control: bool = True,
        progress_callback: Callable[[str, int, int], None] | None = None,
        use_version_folders: bool = True,
        allow_unverified: bool = False,
    ) -> Path:
        """
        Synchronous version of download.

        A git clone is not checked against ``source_sha256``: that is the hash
        of the release *archive*, which a clone never produces. Archives are
        checked against it before anything is unpacked.

        Args:
            remote_obj: Object to download
            target_path: Where to download (parent directory)
            prefer_source_control: Prefer git clone over archive download
            progress_callback: Progress callback
            use_version_folders: If True, creates <name>/<version>/ structure
            allow_unverified: Accept an archive that publishes no SHA-256

        Returns:
            Path to downloaded object

        Raises:
            IntegrityError: If the archive does not match its published SHA-256
            UnsafeSourceError: If the URL, ref or archive is refused
        """
        target_path = Path(target_path)
        target_path.mkdir(parents=True, exist_ok=True)

        version = remote_obj.version or "0.0.0"
        # <target>/<name>/<version>/, or straight into <target>/
        obj_folder = target_path / remote_obj.name / version if use_version_folders else target_path

        effective_sc_url = remote_obj.effective_source_control_url
        if prefer_source_control and effective_sc_url:
            return self._clone_object_sync(
                remote_obj, effective_sc_url, obj_folder, progress_callback
            )

        if remote_obj.download_url:
            return self._download_archive_sync(
                remote_obj, obj_folder, progress_callback, allow_unverified
            )

        raise StoreError(f"No download method available for {remote_obj.name}")

    def _clone_object_sync(
        self,
        remote_obj: RemoteObject,
        sc_url: str,
        obj_folder: Path,
        progress_callback: Callable[[str, int, int], None] | None,
    ) -> Path:
        """Get *remote_obj* into *obj_folder* by git clone.

        Prefers an immutable release tag, then a branch. A contained
        object (one with ``repo_relative_path``) is copied out of a clone
        of its whole repo.
        """
        # Prefer an immutable release tag, then a branch.
        ref = remote_obj.source_control_tag or remote_obj.effective_source_control_branch
        # Contained objects (repo children) live in a subdirectory of
        # the repo clone: clone to a temp dir, then copy the subdir.
        subdir = remote_obj.repo_relative_path

        if progress_callback:
            # -1: indeterminate progress (git clone doesn't report size)
            progress_callback(f"Cloning {remote_obj.display_name or remote_obj.name}", -1, 100)

        if subdir:
            self._clone_repo_subdir(sc_url, ref, subdir, obj_folder)
        else:
            obj_folder.parent.mkdir(parents=True, exist_ok=True)
            self._git_clone(sc_url, obj_folder, ref=ref)

        if progress_callback:
            progress_callback("Clone complete", 100, 100)
        return obj_folder

    def _clone_repo_subdir(
        self, sc_url: str, ref: str | None, subdir: str, obj_folder: Path
    ) -> None:
        """Clone the repo at *sc_url* to a temp dir and copy *subdir* of it to *obj_folder*.

        Raises:
            UnsafeSourceError: If *subdir* is absolute or climbs out with ``..``
            StoreError: If *subdir* isn't in the clone or *obj_folder* exists
        """
        import shutil
        import tempfile

        from .paths import rmtree_force
        from .safety import UnsafeSourceError

        if ".." in Path(subdir).parts or Path(subdir).is_absolute():
            raise UnsafeSourceError(f"Refusing repo-relative path {subdir!r}")
        tmp_dir = Path(tempfile.mkdtemp(prefix="o3de_store_"))
        try:
            clone_path = tmp_dir / "repo"
            self._git_clone(sc_url, clone_path, ref=ref)
            src_dir = clone_path / subdir
            if not src_dir.is_dir():
                raise StoreError(f"Object path '{subdir}' not found in repo clone {sc_url}")
            obj_folder.parent.mkdir(parents=True, exist_ok=True)
            if obj_folder.exists():
                raise StoreError(f"Target already exists: {obj_folder}")
            shutil.copytree(src_dir, obj_folder)
        finally:
            # Read-only .git files on Windows need the forced delete
            rmtree_force(tmp_dir)

    def _download_archive_sync(
        self,
        remote_obj: RemoteObject,
        obj_folder: Path,
        progress_callback: Callable[[str, int, int], None] | None,
        allow_unverified: bool,
    ) -> Path:
        """Download *remote_obj*'s release archive, verify it, and unpack it into *obj_folder*."""
        url, expected_sha256 = self._archive_checks(remote_obj, allow_unverified)

        if progress_callback:
            progress_callback(f"Downloading {remote_obj.display_name or remote_obj.name}", 0, 100)

        download_dir = get_download_path()
        download_dir.mkdir(parents=True, exist_ok=True)
        archive_path = download_dir / f"{remote_obj.name}.archive"

        self._stream_to_file(url, archive_path, progress_callback)

        if progress_callback:
            progress_callback("Extracting...", 85, 100)
        obj_folder.parent.mkdir(parents=True, exist_ok=True)
        self._unpack_verified(archive_path, expected_sha256, obj_folder)

        if progress_callback:
            progress_callback("Download complete", 100, 100)
        return obj_folder

    @staticmethod
    def _stream_to_file(
        url: str,
        archive_path: Path,
        progress_callback: Callable[[str, int, int], None] | None,
    ) -> None:
        """Stream *url* into *archive_path*, reporting 0-80% progress (or -1 if size unknown)."""
        from .safety import download_client

        with download_client() as client:
            with client.stream("GET", url) as response:
                response.raise_for_status()
                total_size = int(response.headers.get("content-length", 0))
                downloaded = 0
                with open(archive_path, "wb") as f:
                    for chunk in response.iter_bytes():
                        f.write(chunk)
                        downloaded += len(chunk)
                        if progress_callback:
                            if total_size > 0:
                                pct = int(downloaded * 80 / total_size)  # 0-80% for download
                                progress_callback("Downloading...", pct, 100)
                            else:
                                progress_callback("Downloading...", -1, 100)
