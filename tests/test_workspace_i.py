# Tests for workspace schema, Pydantic model, file ownership and migration
# SPDX-License-Identifier: Apache-2.0 OR MIT

"""I-series tests: workspace schema + model + legacy migration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

SCHEMA_URL = (
    "https://raw.githubusercontent.com/accesspointmg/canonical.o3de.org/main/src/"
    "o3de-workspace-2.0.0.json"
)


def _workspace_validator():
    from o3de_cli.core.schema import WORKSPACE_SCHEMA_FILENAME, build_validator

    return build_validator(WORKSPACE_SCHEMA_FILENAME)


def _current_format(**overrides) -> dict:
    """A workspace.json in the current format (categorised sources)."""
    data = {
        "$schema": SCHEMA_URL,
        "$schemaVersion": "2.0.0",
        "workspace": {"name": "test-build"},
        "created": "2026-05-25T12:00:00",
        "root_object": "/home/user/engine",
        "root_type": "engine",
        "sources": {
            "engines": {"org.o3de.engine.o3de": "/home/user/engine"},
            "projects": {"org.o3de.project.game": "/home/user/project"},
            "gems": {},
            "templates": {},
            "overlays": {"org.o3de.overlay.console": "/home/user/console-overlay"},
        },
        "file_links": {"/home/user/engine/engine.json": "Engines/o3de/engine.json"},
    }
    data.update(overrides)
    return data


# ── I1: Schema validation ──────────────────────────────────────────

class TestWorkspaceSchema:
    """The canonical workspace JSON Schema, as bundled with the package."""

    def test_schema_is_bundled(self):
        from o3de_cli.core.schema import BUNDLED_SCHEMA_DIR, WORKSPACE_SCHEMA_FILENAME

        assert (BUNDLED_SCHEMA_DIR / WORKSPACE_SCHEMA_FILENAME).is_file()
        assert (BUNDLED_SCHEMA_DIR / "SOURCE").is_file()

    def test_schema_shape(self):
        from o3de_cli.core.schema import BUNDLED_SCHEMA_DIR, WORKSPACE_SCHEMA_FILENAME

        data = json.loads(
            (BUNDLED_SCHEMA_DIR / WORKSPACE_SCHEMA_FILENAME).read_text(encoding="utf-8")
        )
        assert data["title"] == "O3DE Workspace Schema 2.0.0"
        for key in ("workspace", "sources", "file_links", "root_object", "overlay_order"):
            assert key in data["properties"]

    def test_validate_valid_workspace(self):
        errors = list(_workspace_validator().iter_errors(_current_format()))
        assert errors == []

    def test_validate_invalid_workspace_missing_workspace(self):
        data = _current_format()
        del data["workspace"]
        assert list(_workspace_validator().iter_errors(data))

    def test_model_output_validates(self):
        """What WorkspaceMeta writes is what the schema accepts."""
        from o3de_cli.commands.workspace import _build_workspace_meta

        meta = _build_workspace_meta(
            name="ws",
            root_path=Path("/e"),
            root_type="engine",
            sources={"engines": {"org.o3de.engine.o3de": "/e"}},
            platforms=["linux"],
            attributions="object",
        )
        data = json.loads(json.dumps(meta.model_dump(by_alias=True, exclude_none=True)))
        errors = [e.message for e in _workspace_validator().iter_errors(data)]
        assert errors == []

    def test_env_override(self, tmp_path, monkeypatch):
        from o3de_cli.core.schema import find_schema_directory

        monkeypatch.setenv("O3DE_SCHEMA_DIR", str(tmp_path))
        assert find_schema_directory() == tmp_path

    def test_env_override_missing_dir_falls_back(self, tmp_path, monkeypatch):
        from o3de_cli.core.schema import BUNDLED_SCHEMA_DIR, find_schema_directory

        monkeypatch.setenv("O3DE_SCHEMA_DIR", str(tmp_path / "nope"))
        assert find_schema_directory() == BUNDLED_SCHEMA_DIR


# ── I1: Pydantic model ─────────────────────────────────────────────

class TestWorkspaceMeta:
    """WorkspaceMeta model round-trip and validation."""

    def test_round_trip(self):
        from o3de_cli.core.models import WorkspaceMeta

        meta = WorkspaceMeta.model_validate(
            _current_format(workspace={"name": "my-build", "version": "1.0.0"})
        )
        assert meta.workspace.name == "my-build"
        assert meta.workspace.version == "1.0.0"
        assert meta.root_type == "engine"
        assert meta.sources.engines == {"org.o3de.engine.o3de": "/home/user/engine"}
        assert meta.sources.overlays == {
            "org.o3de.overlay.console": "/home/user/console-overlay"
        }
        assert meta.file_links["/home/user/engine/engine.json"] == "Engines/o3de/engine.json"

        dumped = meta.model_dump(by_alias=True, exclude_none=True)
        meta2 = WorkspaceMeta.model_validate(dumped)
        assert meta2.workspace.name == meta.workspace.name
        assert meta2.sources == meta.sources
        assert meta2.file_links == meta.file_links

    def test_minimal_valid(self):
        from o3de_cli.core.models import WorkspaceMeta

        meta = WorkspaceMeta.model_validate({
            "$schema": SCHEMA_URL,
            "$schemaVersion": "2.0.0",
            "workspace": {"name": "minimal"},
            "created": "2026-01-01T00:00:00",
        })
        assert meta.workspace.name == "minimal"
        assert meta.sources.engines == {}
        assert meta.sources.overlays == {}
        assert meta.file_links == {}
        assert meta.overrides == {}

    def test_missing_workspace_raises(self):
        from o3de_cli.core.models import WorkspaceMeta
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            WorkspaceMeta.model_validate({
                "$schema": "x",
                "$schemaVersion": "2.0.0",
                "created": "2026-01-01",
            })

    def test_extra_fields_allowed(self):
        from o3de_cli.core.models import WorkspaceMeta
        data = {
            "$schema": "x",
            "$schemaVersion": "2.0.0",
            "workspace": {"name": "test"},
            "created": "2026-01-01",
            "custom_field": "hello",
        }
        meta = WorkspaceMeta.model_validate(data)
        assert meta.workspace.name == "test"


# ── I2: Migration fallback ─────────────────────────────────────────

class TestWorkspaceMigration:
    """Test .workspace.json -> workspace.json fallback."""

    def test_find_new_name_preferred(self, tmp_path):
        from o3de_cli.commands.workspace import _find_workspace_meta, WORKSPACE_META
        (tmp_path / WORKSPACE_META).write_text("{}")
        (tmp_path / ".workspace.json").write_text("{}")
        result = _find_workspace_meta(tmp_path)
        assert result.name == WORKSPACE_META

    def test_find_legacy_fallback(self, tmp_path):
        from o3de_cli.commands.workspace import _find_workspace_meta
        (tmp_path / ".workspace.json").write_text("{}")
        result = _find_workspace_meta(tmp_path)
        assert result.name == ".workspace.json"

    def test_find_none_when_missing(self, tmp_path):
        from o3de_cli.commands.workspace import _find_workspace_meta
        assert _find_workspace_meta(tmp_path) is None

    def test_read_legacy_format(self, tmp_path):
        """The oldest files: a bare source list, no header, no candidates."""
        from o3de_cli.commands.workspace import _read_workspace_meta

        engine = tmp_path / "engine"
        engine.mkdir()
        (engine / "engine.json").write_text("{}")
        project = tmp_path / "MyGame"
        project.mkdir()
        (project / "project.json").write_text("{}")
        ws = tmp_path / "ws"
        ws.mkdir()
        legacy = {"sources": [str(project), str(engine)], "overlays": [], "name": "old-ws"}
        (ws / ".workspace.json").write_text(json.dumps(legacy))

        meta = _read_workspace_meta(ws)
        assert meta is not None
        assert meta.workspace.name == "old-ws"
        assert meta.sources.engines == {"engine": str(engine)}
        assert meta.sources.projects == {"MyGame": str(project)}
        # The first listed source was the root
        assert meta.root_object == str(project)

    def test_legacy_source_without_object_json_is_skipped(self, tmp_path):
        from o3de_cli.core.models import WorkspaceMeta

        meta = WorkspaceMeta.model_validate({
            "workspace": {"name": "w"},
            "created": "",
            "sources": [str(tmp_path / "not-an-object")],
        })
        assert meta.sources.engines == {}
        assert meta.sources.projects == {}

    def test_write_creates_new_name(self, tmp_path):
        from o3de_cli.commands.workspace import (
            _write_workspace_meta, _build_workspace_meta, WORKSPACE_META,
        )
        meta = _build_workspace_meta(
            name="test",
            root_path=Path("/engine"),
            root_type="engine",
            sources={"engines": {"org.o3de.engine.o3de": "/engine"}},
        )
        _write_workspace_meta(tmp_path, meta)
        assert (tmp_path / WORKSPACE_META).exists()
        data = json.loads((tmp_path / WORKSPACE_META).read_text())
        assert data["workspace"]["name"] == "test"
        assert data["sources"]["engines"] == {"org.o3de.engine.o3de": "/engine"}
        assert "$schema" in data
        assert "$schemaVersion" in data


# ── I3: File ownership tracking ────────────────────────────────────

class TestFileOwnership:
    """Core Workspace class tracks file_owners."""

    def test_workspace_has_file_owners(self):
        from o3de_cli.core.workspace import Workspace
        from o3de_cli.core import ObjectType
        ws = Workspace(
            root_path=Path("/tmp/ws"),
            root_object_path=Path("/tmp/engine"),
            root_object_type=ObjectType.ENGINE,
        )
        assert hasattr(ws, "file_owners")
        assert ws.file_owners == {}

    def test_link_populates_owners(self, tmp_path):
        from o3de_cli.core.workspace import Workspace
        from o3de_cli.core import ObjectType

        # Create source files
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.txt").write_text("hello")
        sub = src / "sub"
        sub.mkdir()
        (sub / "b.txt").write_text("world")

        ws_dir = tmp_path / "ws"
        ws_dir.mkdir(parents=True)
        dest = ws_dir / "Engines" / "my-engine"

        ws = Workspace(
            root_path=ws_dir,
            root_object_path=src,
            root_object_type=ObjectType.ENGINE,
        )
        ws._link_object_files(
            source_root=src, dest_root=dest,
            owner_name="my-engine",
        )

        assert "Engines/my-engine/a.txt" in ws.file_owners
        assert ws.file_owners["Engines/my-engine/a.txt"] == "my-engine"
        assert "Engines/my-engine/sub/b.txt" in ws.file_owners
        assert ws.file_owners["Engines/my-engine/sub/b.txt"] == "my-engine"

    def test_overlay_transfers_ownership(self, tmp_path):
        from o3de_cli.core.workspace import Workspace
        from o3de_cli.core import ObjectType

        # Create base + overlay
        base = tmp_path / "base"
        base.mkdir()
        (base / "config.txt").write_text("original")

        overlay = tmp_path / "overlay"
        overlay.mkdir()
        (overlay / "Overlay").mkdir()
        (overlay / "Overlay" / "config.txt").write_text("patched")

        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()

        ws = Workspace(
            root_path=ws_dir,
            root_object_path=base,
            root_object_type=ObjectType.ENGINE,
        )

        dest_base = ws_dir / "Engines" / "base-engine"
        ws._link_object_files(
            source_root=base, dest_root=dest_base,
            owner_name="base-engine",
        )
        assert ws.file_owners["Engines/base-engine/config.txt"] == "base-engine"

        # Overlay composes INTO the base tree and takes ownership there
        ws._apply_overlay(
            overlay, owner_name="console-overlay",
            base_dest_root=dest_base,
        )
        assert ws.file_owners["Engines/base-engine/config.txt"] == "console-overlay"
        assert (dest_base / "config.txt").read_text() == "patched"


# ── J5: ResolvedCandidate & resolved_candidates ────────────────────

class TestResolvedCandidate:
    """ResolvedCandidate model validation."""

    def test_basic_construction(self):
        from o3de_cli.core.models import ResolvedCandidate
        rc = ResolvedCandidate(
            name="org.o3de.engine.o3de", version="1.0.0",
            object_type="engine", status="local", path="/engines/o3de",
        )
        assert rc.name == "org.o3de.engine.o3de"
        assert rc.version == "1.0.0"
        assert rc.object_type == "engine"
        assert rc.status == "local"
        assert rc.path == "/engines/o3de"

    def test_defaults(self):
        from o3de_cli.core.models import ResolvedCandidate
        rc = ResolvedCandidate(name="org.o3de.gem.atom", object_type="gem", status="remote")
        assert rc.version == "0.0.0"
        assert rc.path is None


class TestLegacyCandidatesMigration:
    """Older files listed resolved_candidates; they become categorised sources."""

    def _legacy(self, **extra) -> dict:
        data = {
            "$schema": SCHEMA_URL,
            "$schemaVersion": "2.0.0",
            "workspace": {"name": "solved-ws"},
            "created": "2026-05-31T12:00:00",
            "sources": ["/engine", "/gem"],
            "resolved_candidates": [
                {"name": "org.o3de.engine.o3de", "version": "1.0.0",
                 "object_type": "engine", "status": "local", "path": "/engine"},
                {"name": "org.o3de.gem.atom", "version": "2.0.0",
                 "object_type": "gem", "status": "remote"},
            ],
        }
        data.update(extra)
        return data

    def test_candidates_become_sources(self):
        from o3de_cli.core.models import WorkspaceMeta

        meta = WorkspaceMeta.model_validate(self._legacy())
        assert meta.sources.engines == {"org.o3de.engine.o3de": "/engine"}
        # A remote candidate had no local path yet
        assert meta.sources.gems == {"org.o3de.gem.atom": ""}

    def test_file_owners_become_file_links(self):
        from o3de_cli.core.models import WorkspaceMeta

        meta = WorkspaceMeta.model_validate(
            self._legacy(file_owners={"Engines/o3de/engine.json": "org.o3de.engine.o3de"})
        )
        assert meta.file_links == {
            "/engine/Engines/o3de/engine.json": "Engines/o3de/engine.json"
        }

    def test_migrated_file_round_trips(self, tmp_path):
        from o3de_cli.commands.workspace import _read_workspace_meta, _write_workspace_meta

        (tmp_path / "workspace.json").write_text(json.dumps(self._legacy()))
        meta = _read_workspace_meta(tmp_path)
        _write_workspace_meta(tmp_path, meta)

        written = json.loads((tmp_path / "workspace.json").read_text())
        # Legacy keys are dropped on write; the current format is what remains
        for key in ("resolved_candidates", "file_owners", "overlays"):
            assert key not in written
        assert written["sources"]["engines"] == {"org.o3de.engine.o3de": "/engine"}
        assert list(_workspace_validator().iter_errors(written)) == []
