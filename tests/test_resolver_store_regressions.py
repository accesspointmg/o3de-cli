# O3DE CLI - Regressions found while refactoring the resolver and store
# SPDX-License-Identifier: Apache-2.0 OR MIT

"""Four bugs found during the resolver/store refactor (#24)."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest


def _store(tmp_path):
    from o3de_cli.core.store import Cache, Store

    store = Store.__new__(Store)
    store.cache = Cache(tmp_path / "cache")
    store.timeout = 5
    store.objects = {}
    store.versions = {}
    store._visited_urls = set()
    return store


GEM = {"$schemaVersion": "2.0.0", "gem": {"name": "org.test.gem.ok", "version": "1.0.0"}}
REPO = {
    "$schemaVersion": "2.0.0",
    "repo": {"name": "org.test.repo.r"},
    "remote": {"gems": ["https://example.com/bad.json", "https://example.com/ok.json"]},
}


class TestRefreshSkipsForeignDocuments:
    """One document with no O3DE type header aborted the whole refresh."""

    def _docs(self, url, **_kw):
        return {
            "https://example.com/repo.json": REPO,
            "https://example.com/bad.json": {"something": "else"},
            "https://example.com/ok.json": GEM,
        }[url]

    def test_refresh_sync(self, tmp_path):
        store = _store(tmp_path)
        with patch.object(
            store, "_fetch_json_with_client", side_effect=lambda c, url, **kw: self._docs(url)
        ):
            store.refresh_sync(["https://example.com/repo.json"])
        assert "gem:org.test.gem.ok" in store.objects

    def test_refresh_async(self, tmp_path):
        import asyncio

        store = _store(tmp_path)

        async def fetch(url, **_kw):
            return self._docs(url)

        with patch.object(store, "fetch_json", side_effect=fetch):
            asyncio.run(store.refresh(["https://example.com/repo.json"]))
        assert "gem:org.test.gem.ok" in store.objects

    def test_non_object_document_is_skipped(self, tmp_path):
        from o3de_cli.core.store import _object_type_or_none

        assert _object_type_or_none("u", ["not", "an", "object"]) is None
        assert _object_type_or_none("u", {"nothing": 1}) is None


class TestStringWhereListBelongs:
    """A string in place of a URL list was iterated one character at a time."""

    def test_store_extract_remote_urls(self, tmp_path):
        store = _store(tmp_path)
        data = {"remote": {"gems": "https://x/gem.json"}, "children": {"gems": "Gems/a/gem.json"}}
        assert store._extract_remote_urls(data, base_url="https://x/repo.json") == []

    def test_resolver_crawl_links(self):
        from o3de_cli.core.resolver import _crawl_links

        data = {
            "repos": "https://x/r.json",
            "remote": {"gems": "https://x/g.json"},
            "children": {"gems": "Gems/a/gem.json"},
        }
        assert _crawl_links("https://x/repo.json", data, set()) == []

    def test_resolver_children_from_2_0_0(self):
        from o3de_cli.core.resolver import _children_from_2_0_0

        assert _children_from_2_0_0("https://x/repo.json", {"children": {"gems": "abc"}}, {}) == []

    def test_as_list(self):
        from o3de_cli.core.resolver import _as_list

        assert _as_list(["a"]) == ["a"]
        assert _as_list("abc") == []
        assert _as_list(None) == []


LEGACY_GEM = {"gem_name": "Widget", "version": "1.2.0", "origin": "Example Co"}


def _upgraded_name() -> str:
    from o3de_cli.core.models import get_object_name
    from o3de_cli.core.upgrade import upgrade_to_latest

    return get_object_name(upgrade_to_latest(dict(LEGACY_GEM)))


class TestUpgradedDataUsedWhenSidecarCannotBeWritten:
    """A read-only checkout resolved legacy objects under their legacy names."""

    def test_same_name_writable_or_not(self, tmp_path):
        from o3de_cli.core.models import ObjectType
        from o3de_cli.core.resolver import Resolver

        gem = tmp_path / "Widget"
        gem.mkdir()
        (gem / "gem.json").write_text(json.dumps(LEGACY_GEM))

        resolver = Resolver.__new__(Resolver)
        with patch.object(Resolver, "_write_versioned_object", return_value=False):
            Resolver.__init__(resolver)
            obj = resolver._resolve_object(gem, ObjectType.GEM)
        assert obj is not None
        assert obj.name == _upgraded_name() != "Widget"


class TestRegisteredLegacyFileIsNotSwapped:
    """A legacy file registered by name next to another legacy file: the other
    one was loaded and flagged versioned, so its upgrade was skipped."""

    def test_registered_file_loaded_as_legacy(self, tmp_path):
        from o3de_cli.core.models import ObjectType
        from o3de_cli.core.resolver import Resolver

        (tmp_path / "custom.json").write_text("{}")
        (tmp_path / "project.json").write_text("{}")
        path, versioned = Resolver._prefer_versioned_json(tmp_path / "custom.json", ObjectType.PROJECT)
        assert (path.name, versioned) == ("custom.json", False)

    def test_versioned_sidecar_still_wins(self, tmp_path):
        from o3de_cli.core.models import ObjectType
        from o3de_cli.core.resolver import Resolver

        (tmp_path / "project.json").write_text("{}")
        (tmp_path / "project.2-0-0.json").write_text("{}")
        path, versioned = Resolver._prefer_versioned_json(tmp_path / "project.json", ObjectType.PROJECT)
        assert (path.name, versioned) == ("project.2-0-0.json", True)
