# O3DE CLI - Resolver / Store Helper Tests
# SPDX-License-Identifier: Apache-2.0 OR MIT

"""Unit tests for the small helpers split out of Resolver and Store.

Each test pins down one step of resolution (or of remote-object parsing)
on plain data, so the larger end-to-end tests don't have to.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from o3de_cli.core import resolver as R
from o3de_cli.core import store as S
from o3de_cli.core.models import ObjectType
from o3de_cli.core.resolver import ObjectNameVersion, ResolvedObject, Resolver
from o3de_cli.core.store import RemoteObject, Store


def _obj(name, version="1.0.0", object_type=ObjectType.GEM, data=None, path=None):
    return ResolvedObject(
        path=path or Path(f"/objs/{name}"),
        object_type=object_type,
        name=name,
        version=version,
        data=data if data is not None else {},
    )


def _write(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data) if not isinstance(data, str) else data)
    return path


def _gem_json(name, version="1.0.0", **extra):
    return {"$schemaVersion": "2.0.0", "gem": {"name": name, "version": version}, **extra}


@pytest.fixture
def resolver(tmp_path):
    manifest = _write(tmp_path / "o3de_manifest.json", {"$schemaVersion": "2.0.0", "local": {}})
    with patch(
        "o3de_cli.core.resolver.get_resolved_manifest_path",
        return_value=tmp_path / "resolved.json",
    ):
        yield Resolver(manifest_path=manifest)


# ----------------------------------------------------------------------
# Path / URL helpers
# ----------------------------------------------------------------------


class TestUrlAndPathHelpers:
    def test_resolve_child_url_relative(self):
        assert (
            R._resolve_child_url("https://h/repo/repo.json", "gems/a/gem.json")
            == "https://h/repo/gems/a/gem.json"
        )

    def test_resolve_child_url_strips_leading_slash(self):
        assert (
            R._resolve_child_url("https://h/repo/repo.json", "/a.json") == "https://h/repo/a.json"
        )

    def test_resolve_child_url_absolute_passes_through(self):
        assert R._resolve_child_url("https://h/repo.json", "http://x/y.json") == "http://x/y.json"

    def test_child_object_dir_json_entry_uses_parent(self):
        assert R._child_object_dir(Path("/e"), "Gems/A/gem.json") == Path("/e/Gems/A")

    def test_child_object_dir_legacy_entry_is_directory(self):
        assert R._child_object_dir(Path("/e"), "Gems/A") == Path("/e/Gems/A")

    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://x/gems/MyGem/gem.json", "MyGem"),
            ("https://x/gems/MyGem/GEM.JSON", "MyGem"),
            ("https://x/Other.json", "Other.json"),
            ("https://x/gems/MyGem/", "MyGem"),
            ("gem.json", "gem.json"),
        ],
    )
    def test_name_from_object_url(self, url, expected):
        assert R._name_from_object_url(url) == expected

    def test_detect_object_type(self, tmp_path):
        _write(tmp_path / "p" / "project.json", {})
        assert R._detect_object_type(tmp_path / "p") == ObjectType.PROJECT
        (tmp_path / "cmake").mkdir()
        assert R._detect_object_type(tmp_path / "cmake") is None

    def test_detect_object_type_ignores_types_not_asked_for(self, tmp_path):
        _write(tmp_path / "r" / "repo.json", {})
        assert R._detect_object_type(tmp_path / "r") is None


# ----------------------------------------------------------------------
# Object data helpers
# ----------------------------------------------------------------------


class TestObjectDataHelpers:
    @pytest.mark.parametrize(
        "name,expected",
        [
            ("my_project_template", "project"),
            ("CoolGemTemplate", "gem"),
            ("whatever", ""),
        ],
    )
    def test_infer_template_type(self, name, expected):
        data = {"template": {"name": name}}
        R._infer_template_type(data, name)
        assert data["template"]["type"] == expected

    def test_infer_template_type_keeps_existing_type(self):
        data = {"template": {"type": "engine"}}
        R._infer_template_type(data, "my_project_template")
        assert data["template"]["type"] == "engine"

    def test_display_metadata_prefers_2_0_0_header(self):
        obj = _obj(
            "g",
            data={
                "gem": {"display_name": "New", "description": "New desc"},
                "display_name": "Old",
                "summary": "Old summary",
                "icon": {"relative_path": "icon.png"},
                "icon_path": "legacy.png",
            },
        )
        assert R._display_metadata(obj) == {
            "display_name": "New",
            "summary": "New desc",
            "icon_path": "icon.png",
        }

    def test_display_metadata_legacy_fields(self):
        obj = _obj("g", data={"display_name": "Old", "summary": "S", "icon_path": "l.png"})
        assert R._display_metadata(obj) == {
            "display_name": "Old",
            "summary": "S",
            "icon_path": "l.png",
        }

    def test_display_metadata_empty(self):
        assert R._display_metadata(_obj("g", data={"icon": "not-a-dict"})) == {}

    def test_ancestry_runs_from_parent_to_root(self):
        root, mid, leaf = _obj("root"), _obj("mid"), _obj("leaf")
        mid.parent = root
        leaf.parent = mid
        assert [p["name"] for p in R._ancestry(leaf)] == ["mid", "root"]
        assert R._ancestry(root) == []

    def test_release_versions(self):
        data = {"releases": [{"name": "2.0"}, {"version": "1.0"}, "junk", {}, {"name": ""}]}
        assert R._release_versions(data) == ["2.0", "1.0"]
        assert R._release_versions({"releases": "nope"}) == []
        assert R._release_versions({}) == []


# ----------------------------------------------------------------------
# Transitive closures
# ----------------------------------------------------------------------


class TestClosures:
    def test_all_children_breadth_first_without_duplicates(self):
        a, b, c, shared = _obj("a"), _obj("b"), _obj("c"), _obj("shared")
        a.children = [c, shared]
        b.children = [shared]
        objects = {o.name: o for o in (a, b, c, shared)}
        result = R._all_children(["a", "b", "ghost"], objects)
        assert result == [
            {"name": "a", "type": "gem"},
            {"name": "b", "type": "gem"},
            {"name": "ghost", "type": ""},
            {"name": "c", "type": "gem"},
            {"name": "shared", "type": "gem"},
        ]

    def test_all_children_terminates_on_cycles(self):
        a, b = _obj("a"), _obj("b")
        a.children = [b]
        b.children = [a]
        assert [e["name"] for e in R._all_children(["a"], {"a": a, "b": b})] == ["a", "b"]

    def test_all_remotes_walks_sub_remotes_and_lists_advertised_objects(self):
        remote_objects = {
            "r1": {"type": "repo", "children": ["g1", "local.gem"], "remotes": ["r2"]},
            "r2": {"type": "repo", "children": ["g2"], "remotes": ["r1"]},
            "g1": {"type": "gem"},
        }
        objects = {"local.gem": _obj("local.gem", object_type=ObjectType.PROJECT)}
        assert R._all_remotes(["r1", "unknown"], remote_objects, objects) == [
            {"name": "r1", "type": "repo"},
            {"name": "g1", "type": "gem"},
            {"name": "local.gem", "type": "project"},
            {"name": "unknown", "type": "repo"},
            {"name": "r2", "type": "repo"},
            {"name": "g2", "type": ""},
        ]

    def test_all_remotes_empty_start(self):
        assert R._all_remotes([], {}, {}) == []


# ----------------------------------------------------------------------
# Remote repo crawling / remote-object building
# ----------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status, ctype, body):
        self.status_code = status
        self.headers = {"content-type": ctype}
        self._body = body

    def raise_for_status(self):
        if self.status_code != 200:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url, follow_redirects=False):
        self.calls.append(url)
        return self.responses.get(url, FakeResponse(404, "text/html", None))


class TestCrawlHelpers:
    def test_crawl_links_follows_all_link_kinds_and_skips_visited(self):
        data = {
            "repos": ["https://h/legacy.json", 3, "https://h/seen.json"],
            "remote": {"repos": ["https://h/r2.json"], "gems": ["https://h/g.json"]},
            "children": {"gems": ["gems/a/gem.json", "", 7], "repos": ["https://abs/x.json"]},
        }
        links = R._crawl_links("https://h/repo/repo.json", data, {"https://h/seen.json"})
        assert links == [
            "https://h/legacy.json",
            "https://h/g.json",
            "https://h/r2.json",
            "https://h/repo/gems/a/gem.json",
            "https://abs/x.json",
        ]

    def test_fetch_repo_document_success(self):
        client = FakeClient({"https://h/r.json": FakeResponse(200, "application/json", {"a": 1})})
        assert R._fetch_repo_document(client, "https://h/r.json") == (
            "https://h/r.json",
            {"a": 1},
            None,
        )

    def test_fetch_repo_document_retries_directory_url_as_repo_json(self):
        client = FakeClient(
            {"https://h/repo/repo.json": FakeResponse(200, "application/json", {"ok": True})}
        )
        url, data, error = R._fetch_repo_document(client, "https://h/repo/")
        assert (url, data, error) == ("https://h/repo/", {"ok": True}, None)
        assert client.calls == ["https://h/repo/", "https://h/repo/repo.json"]

    def test_fetch_repo_document_reports_errors(self):
        client = FakeClient({})
        url, data, error = R._fetch_repo_document(client, "https://h/missing.json")
        assert data is None
        assert "404" in error
        # A .json URL is not retried as <url>/repo.json
        assert client.calls == ["https://h/missing.json"]

    def test_index_crawled_documents(self):
        crawled = {
            "https://h/new.json": {"repo": {"name": "org.repo", "version": "1.0.0"}},
            "https://h/gem.json": {"gem": {"name": "org.gem", "version": "2.0.0"}},
            "https://h/legacy.json": {"repo_name": "Legacy"},
            "https://h/untyped.json": {"foo": 1},
            "https://h/bad.json": {"repo_name": "x", "_error": "boom"},
        }
        assert R._index_crawled_documents(crawled) == {
            "https://h/new.json": {"name": "org.repo", "type": "repo", "version": "1.0.0"},
            "https://h/gem.json": {"name": "org.gem", "type": "gem", "version": "2.0.0"},
            "https://h/legacy.json": {"name": "Legacy", "type": "repo", "version": "0.0.0"},
            # No recognisable header at all: treated as a legacy repo named by URL
            "https://h/untyped.json": {
                "name": "https://h/untyped.json",
                "type": "repo",
                "version": "",
            },
        }

    def test_url_to_repo_names(self):
        crawled = {
            "https://h/a.json": {"repo": {"name": "org.a"}},
            "https://h/b.json": {"repo_name": "B"},
            "https://h/c.json": {"foo": 1},
            "https://h/err.json": {"_error": "boom"},
        }
        assert R._url_to_repo_names(crawled) == {
            "https://h/a.json": "org.a",
            "https://h/b.json": "B",
            "https://h/c.json": "https://h/c.json",
            "https://h/err.json": "https://h/err.json",
        }

    def test_collect_repo_children_registers_unknown_objects_only(self):
        url = "https://h/repo/repo.json"
        url_index = {"https://h/repo/gems/a/gem.json": {"name": "org.a", "type": "gem"}}
        data = {
            "children": {"gems": ["gems/a/gem.json", "gems/uncrawled/gem.json"]},
            "gems": ["https://x/gems/Foo/gem.json", "https://x/gems/Local/gem.json", 9],
            "gems_data": [
                {"gem_name": "LegacyGem", "version": "1.0", "summary": "S", "repo_uri": "u"},
                {"name": "Foo"},
                {},
                "junk",
            ],
            "templates": ["https://x/t/template.json"],
        }
        remote_objects: dict = {}
        children = R._collect_repo_children(
            url, data, "org.repo", url_index, remote_objects, {"Local"}
        )
        assert children == ["org.a", "Foo", "Local", "LegacyGem", "Foo", "t"]
        assert set(remote_objects) == {"Foo", "LegacyGem", "t"}
        assert remote_objects["Foo"]["type"] == "gem"
        assert remote_objects["Foo"]["repo"] == "org.repo"
        assert remote_objects["t"]["type"] == "template"
        assert remote_objects["LegacyGem"]["version"] == "1.0"
        assert remote_objects["LegacyGem"]["url"] == "u"
        assert remote_objects["LegacyGem"]["display_metadata"] == {
            "display_name": "",
            "summary": "S",
        }

    def test_collect_repo_remotes_names_crawled_urls_and_dedupes(self):
        url_index = {"https://h/r2.json": {"name": "org.r2", "type": "repo"}}
        data = {
            "repos": ["https://h/r2.json", 1, "https://h/r3.json"],
            "remote": {"repos": ["https://h/r2.json", "https://h/r4.json"]},
        }
        assert R._collect_repo_remotes(data, url_index) == [
            "org.r2",
            "https://h/r3.json",
            "https://h/r4.json",
        ]

    def test_repo_entry_display_metadata(self):
        entry = R._repo_entry("u", {"repo": {"display_name": "D", "description": "X"}}, ["c"], [])
        assert entry["display_metadata"] == {"summary": "X", "display_name": "D"}
        assert entry["children"] == ["c"]
        assert entry["status"] == "remote"
        legacy = R._repo_entry("u", {"repo_name": "R", "summary": "S"}, [], [])
        assert legacy["display_metadata"] == {"summary": "S", "display_name": "R"}
        assert R._repo_entry("u", {}, [], [])["display_metadata"] is None

    def test_error_and_document_entries(self):
        err = R._error_repo_entry("https://h/x.json", "boom")
        assert err["status"] == "error"
        assert err["display_metadata"] == {"summary": "Error: boom"}
        doc = R._remote_document_entry(
            "u",
            {"gem": {"display_name": "G", "description": "D"}},
            {"name": "g", "type": "gem", "version": "1"},
        )
        assert doc["display_metadata"] == {"display_name": "G", "summary": "D"}
        assert doc["version"] == "1"
        bare = R._remote_document_entry("u", {"gem": "x"}, {"type": "gem", "version": ""})
        assert bare["display_metadata"] is None

    def test_build_remote_objects_end_to_end(self, resolver):
        resolver._crawled_remotes = {
            "https://h/repo/repo.json": {
                "repo": {"name": "org.repo", "display_name": "Repo"},
                "children": {"gems": ["gems/a/gem.json"]},
                "remote": {"repos": ["https://h/err.json"]},
            },
            "https://h/repo/gems/a/gem.json": {"gem": {"name": "org.a", "version": "1.0.0"}},
            "https://h/err.json": {"repo_name": "https://h/err.json", "_error": "boom"},
        }
        built = resolver._build_remote_objects()
        assert list(built) == ["org.repo", "org.a", "https://h/err.json"]
        assert built["org.repo"]["children"] == ["org.a"]
        assert built["org.repo"]["remotes"] == ["https://h/err.json"]
        assert built["org.a"]["type"] == "gem"
        assert built["https://h/err.json"]["status"] == "error"

    def test_build_remote_objects_skips_locally_resolved_documents(self, resolver):
        resolver.objects["org.a"] = _obj("org.a")
        resolver._crawled_remotes = {"https://h/a.json": {"gem": {"name": "org.a"}}}
        assert resolver._build_remote_objects() == {}


# ----------------------------------------------------------------------
# Resolver steps
# ----------------------------------------------------------------------


class TestResolverSteps:
    def test_partition_root_paths(self, tmp_path):
        (tmp_path / "gem").mkdir()
        _write(tmp_path / "proj" / "project.json", {})
        local = {
            "gems": [str(tmp_path / "gem"), str(tmp_path / "missing")],
            "projects": [str(tmp_path / "proj" / "project.json")],
            "repos": ["https://h/repo.json"],
            "engines": ["https://h/ignored.json"],
        }
        manifest = {"templates": [str(tmp_path / "gem")]}
        roots, stale, remotes = Resolver._partition_root_paths(local, manifest)
        assert roots == [
            (tmp_path / "proj" / "project.json", "projects"),
            (tmp_path / "gem", "gems"),
            (tmp_path / "gem", "templates"),
        ]
        assert stale == [(str(tmp_path / "missing"), "gems")]
        assert remotes == ["https://h/repo.json"]

    def test_prefer_versioned_json(self, tmp_path):
        legacy = _write(tmp_path / "a" / "gem.json", {})
        assert Resolver._prefer_versioned_json(legacy, ObjectType.GEM) == (legacy, False)
        versioned = _write(tmp_path / "a" / "gem.2-0-0.json", {})
        assert Resolver._prefer_versioned_json(legacy, ObjectType.GEM) == (versioned, True)
        assert Resolver._prefer_versioned_json(versioned, ObjectType.GEM) == (versioned, True)
        custom = _write(tmp_path / "b" / "custom.json", {})
        assert Resolver._prefer_versioned_json(custom, ObjectType.GEM) == (custom, False)

    def test_locate_object_json_detects_type(self, resolver, tmp_path):
        repo_json = _write(tmp_path / "r" / "repo.2-0-0.json", {})
        assert resolver._locate_object_json(tmp_path / "r", ObjectType.GEM) == (
            tmp_path / "r",
            repo_json,
            True,
            ObjectType.REPO,
        )

    def test_locate_object_json_missing(self, resolver, tmp_path):
        assert resolver._locate_object_json(tmp_path / "nope", ObjectType.GEM) is None
        (tmp_path / "empty").mkdir()
        assert resolver._locate_object_json(tmp_path / "empty", ObjectType.GEM) is None

    def test_load_object_json_hashes_legacy_file_beside_versioned(self, resolver, tmp_path):
        versioned = _write(tmp_path / "g" / "gem.2-0-0.json", _gem_json("g"))
        legacy = _write(tmp_path / "g" / "gem.json", {"gem_name": "g"})
        data = resolver._load_object_json(versioned, tmp_path / "g", True, ObjectType.GEM)
        assert data["gem"]["name"] == "g"
        assert set(resolver.file_hashes) == {versioned.as_posix(), legacy.as_posix()}

    def test_reconcile_duplicate_keeps_newest(self, resolver, tmp_path):
        existing = _obj("g", "2.0.0", path=tmp_path / "v2")
        resolver._register_object(existing)

        # Same directory again: the existing object
        assert (
            resolver._reconcile_duplicate("g", "2.0.0", tmp_path / "v2", ObjectType.GEM, {})
            is existing
        )
        # Older version: existing wins, older kept as an alternate
        older = _gem_json("g", "1.0.0", dependencies=["dep"])
        assert (
            resolver._reconcile_duplicate("g", "1.0.0", tmp_path / "v1", ObjectType.GEM, older)
            is existing
        )
        assert resolver.objects_all["g"]["1.0.0"].path == tmp_path / "v1"
        assert [d.name for d in resolver.objects_all["g"]["1.0.0"].dependencies] == ["dep"]
        # Newer version: caller replaces it
        assert (
            resolver._reconcile_duplicate("g", "3.0.0", tmp_path / "v3", ObjectType.GEM, {}) is None
        )
        assert resolver.objects_all["g"]["2.0.0"] is existing

    def test_register_object_indexes_by_type(self, resolver):
        engine = _obj("e", object_type=ObjectType.ENGINE)
        resolver._register_object(engine)
        assert resolver.objects["e"] is engine
        assert resolver.engines["e"] is engine
        assert resolver.objects_all["e"]["1.0.0"] is engine
        assert resolver.gems == {}

    def test_compute_dependents_map(self, resolver):
        a, b, c = _obj("a"), _obj("b"), _obj("c")
        a.dependencies = [ObjectNameVersion("c"), ObjectNameVersion("c>=1.0.0")]
        b.dependencies = [ObjectNameVersion("c"), ObjectNameVersion("missing")]
        resolver.objects = {"a": a, "b": b, "c": c}
        assert resolver._compute_dependents_map() == {"c": ["a", "b"]}

    def test_object_remote_names(self):
        obj = _obj("g", data={"remote": {"repos": ["https://h/a.json", "https://h/b.json"]}})
        names = Resolver._object_remote_names(
            obj, {"https://h/a.json": "A", "https://h/b.json": "A"}
        )
        assert names == ["A"]
        assert Resolver._object_remote_names(_obj("g", data={"remote": "x"}), {}) == []

    def test_collect_remote_repo_urls(self, resolver):
        resolver.manifest_remotes = ["https://h/m.json"]
        resolver.objects = {
            "a": _obj("a", data={"remote": {"repos": ["https://h/a.json", "https://h/m.json"]}}),
            "b": _obj("b", data={"remote": "not-a-dict"}),
        }
        assert resolver._collect_remote_repo_urls() == {"https://h/m.json", "https://h/a.json"}

    def test_build_resolved_data_matches_saved_file(self, tmp_path):
        engine_dir = tmp_path / "engine"
        _write(
            engine_dir / "engine.json",
            {
                "$schemaVersion": "2.0.0",
                "engine": {"name": "o3de", "version": "1.0.0"},
                "children": {"gems": ["Gems/A/gem.json"]},
            },
        )
        _write(
            engine_dir / "Gems" / "A" / "gem.json",
            _gem_json("gem.a", releases=[{"name": "1.0.0"}]),
        )
        manifest = _write(
            tmp_path / "o3de_manifest.json",
            {"$schemaVersion": "2.0.0", "local": {"engines": [str(engine_dir)]}},
        )
        resolved_path = tmp_path / "resolved.json"
        with (
            patch("o3de_cli.core.resolver.get_resolved_manifest_path", return_value=resolved_path),
            patch("o3de_cli.core.resolver.get_local_git_remote", return_value=None),
        ):
            resolver = Resolver(manifest_path=manifest)
            resolver.resolve()
            data = resolver._build_resolved_data()
            assert resolver.save() == resolved_path

        saved = json.loads(resolved_path.read_text())
        data["resolved_at"] = saved["resolved_at"]
        assert saved == json.loads(json.dumps(data))
        assert data["objects"]["gem.a"]["parent"] == "o3de"
        assert data["objects"]["gem.a"]["releases"] == ["1.0.0"]
        assert data["objects"]["o3de"]["all_children"] == [{"name": "gem.a", "type": "gem"}]
        assert data["manifest_root"]["children"] == ["o3de"]
        assert data["manifest_root"]["all_remotes"] is None

    def test_save_dry_run_writes_nothing(self, tmp_path):
        manifest = _write(tmp_path / "o3de_manifest.json", {"$schemaVersion": "2.0.0"})
        resolved_path = tmp_path / "resolved.json"
        with patch("o3de_cli.core.resolver.get_resolved_manifest_path", return_value=resolved_path):
            resolver = Resolver(manifest_path=manifest, dry_run=True)
            resolver.resolve()
            assert resolver.save() == resolved_path
        assert not resolved_path.exists()

    def test_plan_installs_picks_newest_matching_version(self):
        store = Store.__new__(Store)
        remotes = [
            RemoteObject(url="1", object_type=ObjectType.GEM, name="dep", version="1.0"),
            RemoteObject(url="2", object_type=ObjectType.GEM, name="dep", version="1.5"),
            RemoteObject(url="3", object_type=ObjectType.GEM, name="dep", version="3.0"),
            RemoteObject(url="4", object_type=ObjectType.GEM, name="dep.other", version="9"),
        ]
        store.search = lambda q: [r for r in remotes if q in r.name]
        plan, not_found = Resolver._plan_installs(
            store, {"dep": ObjectNameVersion("dep<2.0"), "nope": ObjectNameVersion("nope")}
        )
        assert [(spec.name, remote.url) for spec, remote in plan] == [("dep", "2")]
        assert not_found == ["nope"]


# ----------------------------------------------------------------------
# Store: remote-object parsing helpers
# ----------------------------------------------------------------------


class TestStoreParseHelpers:
    def test_header_field_prefers_header_then_root_then_default(self):
        assert S._header_field({"k": "h"}, {"k": "r"}, "k") == "h"
        assert S._header_field({"k": ""}, {"k": "r"}, "k") == "r"
        assert S._header_field({}, {}, "k", "d") == "d"

    def test_icon_fields(self):
        assert S._icon_fields({"icon": {"url": "u", "relative_path": "p"}}, {}) == ("u", "p")
        assert S._icon_fields({}, {"icon": "legacy-url"}) == ("legacy-url", "")
        assert S._icon_fields({"icon": {}}, {"icon_url": "flat"}) == ("flat", "")
        assert S._icon_fields({}, {}) == ("", "")

    def test_tag_list(self):
        assert S._tag_list({"user_tags": "one"}) == ["one"]
        assert S._tag_list({"user_tags": [], "canonical_tags": ["c"]}) == ["c"]
        assert S._tag_list({}) == []

    def test_source_control_fields_own_block(self):
        data = {"source_control": {"git": "g", "branch": "b", "tag": "t"}}
        assert S._source_control_fields({}, data) == ("g", "b", "t")

    def test_source_control_fields_header_uri_wins(self):
        data = {"source_control": {"git": "g"}}
        assert S._source_control_fields({"repo_uri": "r"}, data) == ("r", "", "")

    def test_source_control_fields_fall_back_to_latest_release(self):
        data = {
            "source_control": {"branch": "keep"},
            "releases": [
                {"source_controls": [{"git": "old"}]},
                {"source_controls": [{"tag": "no-git"}, {"git": "new", "tag": "v2"}]},
            ],
        }
        assert S._source_control_fields({}, data) == ("new", "keep", "v2")

    def test_source_control_fields_nothing(self):
        assert S._source_control_fields({}, {"releases": ["junk"]}) == (None, "", "")

    def test_repo_relative_path(self):
        parent = "https://h/repo/repo.json"
        assert S._repo_relative_path("https://h/repo/gems/a/gem.json", parent) == "gems/a"
        assert S._repo_relative_path("https://h/repo/top.json", parent) == ""
        assert S._repo_relative_path("https://else/x.json", parent) is None
        assert S._repo_relative_path("https://h/repo/x.json", None) is None

    def test_download_fields(self):
        data = {"download": {"source": "s", "source_sha256": "abc"}}
        assert S._download_fields({}, data) == ("s", "abc")
        assert S._download_fields({"download_uri": "d"}, data) == ("d", "abc")
        assert S._download_fields({}, {"download": None}) == (None, None)

    def test_dependency_specs(self):
        nested = {"dependent": {"gems": ["a>=1", 5], "engines": "x", "projects": ["p"]}}
        assert S._dependency_specs(nested, {}) == ["a>=1", "p"]
        assert S._dependency_specs({}, {"dependent": {"gems": ["root"]}}) == ["root"]
        assert S._dependency_specs({}, {"dependent": ["not-a-dict"]}) == []

    def test_parse_remote_object_bad_header_returns_none(self):
        store = Store.__new__(Store)
        assert store._parse_remote_object("u", {"gem": "x"}, ObjectType.GEM) is None


# ----------------------------------------------------------------------
# Store: refresh bookkeeping
# ----------------------------------------------------------------------


class TestStoreRefreshSteps:
    @pytest.fixture
    def store(self, tmp_path):
        return Store(cache=S.Cache(tmp_path / "cache"))

    def test_record_remote_object_keeps_newest(self, store):
        old = RemoteObject(url="1", object_type=ObjectType.GEM, name="g", version="1.0.0")
        new = RemoteObject(url="2", object_type=ObjectType.GEM, name="g", version="2.0.0")
        unversioned = RemoteObject(url="3", object_type=ObjectType.GEM, name="g")
        store._record_remote_object(new)
        store._record_remote_object(old)
        store._record_remote_object(unversioned)
        assert store.objects["gem:g"] is new
        assert set(store.versions["gem:g"]) == {"1.0.0", "2.0.0", "0.0.0"}

    def test_claim_unvisited(self, store):
        store._visited_urls.add("a")
        wave = [("a", None, None, None), ("b", None, None, None), ("b", "x", None, None)]
        assert store._claim_unvisited(wave) == [("b", None, None, None)]
        assert store._visited_urls == {"a", "b"}

    def test_ingest_repo_passes_its_source_control_to_children(self, store):
        data = {
            "repo": {"name": "org.repo"},
            "source_control": {"git": "https://git/repo", "branch": "main"},
            "children": {"gems": ["gems/a/gem.json"]},
        }
        children = store._ingest_document(("https://h/repo.json", None, "isc", "ibr"), data)
        assert children == [
            ("https://h/gems/a/gem.json", "https://h/repo.json", "https://git/repo", "main")
        ]
        assert "repo:org.repo" in store.objects

    def test_ingest_non_repo_passes_inherited_context_through(self, store):
        data = {"gem": {"name": "g"}, "remote": {"gems": ["https://h/other.json"]}}
        item = ("https://h/repo/g.json", "https://h/repo.json", "isc", "ibr")
        assert store._ingest_document(item, data) == [
            ("https://h/other.json", "https://h/repo.json", "isc", "ibr")
        ]
        assert store.objects["gem:g"].repo_relative_path == "repo"
