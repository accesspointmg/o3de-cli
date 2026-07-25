# O3DE Pilot - Extended Workspace Tests
# SPDX-License-Identifier: Apache-2.0 OR MIT

"""Extended tests for o3de_cli.core.workspace module — covers create, link, overlay, update, stats."""

import pytest
from pathlib import Path

from o3de_cli.core.workspace import (
    Workspace,
    WorkspaceError,
    LayoutError,
    create_workspace,
)
from o3de_cli.core.models import ObjectType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ws(tmp_path, **kw):
    engine = tmp_path / "engine"
    engine.mkdir()
    return Workspace(
        root_path=kw.get("root_path", tmp_path / "ws"),
        root_object_path=kw.get("root_object_path", engine),
        root_object_type=kw.get("root_object_type", ObjectType.ENGINE),
    )


def _write(path: Path, text: str = "content"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


# ---------------------------------------------------------------------------
# TestLayoutErrorAlias
# ---------------------------------------------------------------------------

class TestLayoutErrorAlias:
    def test_alias_is_workspace_error(self):
        assert LayoutError is WorkspaceError


# ---------------------------------------------------------------------------
# TestShouldExcludeAdvanced
# ---------------------------------------------------------------------------

class TestShouldExcludeAdvanced:
    """More exclusion pattern scenarios."""

    def test_exclude_build_subdir(self, tmp_path):
        ws = _make_ws(tmp_path)
        assert ws.should_exclude(Path("build/Release/game.exe"))

    def test_exclude_cache_subdir(self, tmp_path):
        ws = _make_ws(tmp_path)
        assert ws.should_exclude(Path("Cache/products/mesh.azmodel"))

    def test_exclude_log_files(self, tmp_path):
        ws = _make_ws(tmp_path)
        assert ws.should_exclude(Path("output.log"))

    def test_exclude_nested_pycache(self, tmp_path):
        ws = _make_ws(tmp_path)
        assert ws.should_exclude(Path("src/__pycache__"))

    def test_include_cmake(self, tmp_path):
        ws = _make_ws(tmp_path)
        assert not ws.should_exclude(Path("CMakeLists.txt"))

    def test_include_source(self, tmp_path):
        ws = _make_ws(tmp_path)
        assert not ws.should_exclude(Path("Code/Source/main.cpp"))


# ---------------------------------------------------------------------------
# TestWorkspaceCreate
# ---------------------------------------------------------------------------

class TestWorkspaceCreateExtended:
    """Test workspace creation and linking."""

    def test_create_links_files(self, tmp_path):
        engine = tmp_path / "engine"
        engine.mkdir()
        _write(engine / "engine.json", '{"engine": {"name": "test"}}')
        _write(engine / "Code" / "main.cpp", "int main() {}")

        ws = Workspace(tmp_path / "ws", engine, ObjectType.ENGINE)
        ws.add_resolved_object("root", engine, ObjectType.ENGINE)
        ws.create()

        assert (tmp_path / "ws").exists()
        assert len(ws.linked_files) >= 1

    def test_create_with_progress(self, tmp_path):
        engine = tmp_path / "engine"
        engine.mkdir()
        _write(engine / "file.txt", "data")

        ws = Workspace(tmp_path / "ws", engine, ObjectType.ENGINE)
        ws.add_resolved_object("root", engine, ObjectType.ENGINE)

        calls = []
        ws.create(progress_callback=lambda msg, cur, tot: calls.append(msg))
        assert any("Complete" in c for c in calls)

    def test_create_excludes_git(self, tmp_path):
        engine = tmp_path / "engine"
        engine.mkdir()
        _write(engine / "src.cpp", "code")
        _write(engine / ".git" / "config", "[core]")

        ws = Workspace(tmp_path / "ws", engine, ObjectType.ENGINE)
        ws.add_resolved_object("root", engine, ObjectType.ENGINE)
        ws.create()

        linked_names = {p.name for p in ws.linked_files.keys()}
        assert "config" not in linked_names  # .git/config excluded
        assert "src.cpp" in linked_names


# ---------------------------------------------------------------------------
# TestOverlayApplication
# ---------------------------------------------------------------------------

class TestOverlayApplication:
    """Test overlay application."""

    def test_overlay_replaces_base_file(self, tmp_path):
        engine = tmp_path / "engine"
        engine.mkdir()
        _write(engine / "data.txt", "base content")

        overlay = tmp_path / "overlay"
        overlay.mkdir()
        _write(overlay / "Overlay" / "data.txt", "overlay content")

        ws = Workspace(tmp_path / "ws", engine, ObjectType.ENGINE)
        ws.add_resolved_object("root", engine, ObjectType.ENGINE)
        ws.add_overlay(overlay, precedence=0, extends="root")
        ws.create()

        # Overlay payload composes INTO the extended object's tree
        target = tmp_path / "ws" / "Engines" / "root" / "data.txt"
        assert target.read_text() == "overlay content"

    def test_overlay_adds_new_file(self, tmp_path):
        engine = tmp_path / "engine"
        engine.mkdir()
        _write(engine / "base.txt", "base")

        overlay = tmp_path / "overlay"
        overlay.mkdir()
        _write(overlay / "Overlay" / "extra.txt", "extra")

        ws = Workspace(tmp_path / "ws", engine, ObjectType.ENGINE)
        ws.add_resolved_object("root", engine, ObjectType.ENGINE)
        ws.add_overlay(overlay, precedence=0, extends="root")
        ws.create()

        assert (tmp_path / "ws" / "Engines" / "root" / "extra.txt").exists()

    def test_overlay_json_skipped(self, tmp_path):
        engine = tmp_path / "engine"
        engine.mkdir()
        _write(engine / "file.txt", "data")

        overlay = tmp_path / "overlay"
        overlay.mkdir()
        _write(overlay / "overlay.json", '{"overlay": {}}')
        _write(overlay / "Overlay" / "real.txt", "real")

        ws = Workspace(tmp_path / "ws", engine, ObjectType.ENGINE)
        ws.add_resolved_object("root", engine, ObjectType.ENGINE)
        ws.add_overlay(overlay, extends="root")
        ws.create()

        # overlay.json never composes into the base object; it lands in
        # the workspace attribution ledger instead
        base_root = tmp_path / "ws" / "Engines" / "root"
        assert not (base_root / "overlay.json").exists()
        assert (base_root / "real.txt").exists()

    def test_overlay_metadata_in_workspace_ledger(self, tmp_path):
        engine = tmp_path / "engine"
        engine.mkdir()
        _write(engine / "file.txt", "data")

        overlay = tmp_path / "overlay"
        overlay.mkdir()
        _write(overlay / "overlay.json", '{"overlay": {"name": "org.test.overlay.x"}}')
        _write(overlay / "LICENSE.TXT", "license text")
        _write(overlay / "Overlay" / "real.txt", "real")

        ws = Workspace(tmp_path / "ws", engine, ObjectType.ENGINE)
        ws.add_resolved_object("root", engine, ObjectType.ENGINE)
        ws.add_overlay(overlay, extends="root")
        ws.create()

        ledger = tmp_path / "ws" / "Overlays" / "org.test.overlay.x"
        assert (ledger / "overlay.json").exists()
        assert (ledger / "LICENSE.TXT").exists()
        # Payload is not duplicated in the ledger
        assert not (ledger / "Overlay").exists()

    def test_overlay_attributions_object_mode(self, tmp_path):
        engine = tmp_path / "engine"
        engine.mkdir()
        _write(engine / "file.txt", "data")

        overlay = tmp_path / "overlay"
        overlay.mkdir()
        _write(overlay / "overlay.json", '{"overlay": {"name": "org.test.overlay.x"}}')
        _write(overlay / "Overlay" / "real.txt", "real")

        ws = Workspace(tmp_path / "ws", engine, ObjectType.ENGINE,
                       attributions="object")
        ws.add_resolved_object("root", engine, ObjectType.ENGINE)
        ws.add_overlay(overlay, extends="root")
        ws.create()

        base_root = tmp_path / "ws" / "Engines" / "root"
        assert (base_root / "org.test.overlay.x" / "overlay.json").exists()
        assert not (tmp_path / "ws" / "Overlays" / "org.test.overlay.x").exists()

    def test_overlay_attributions_off(self, tmp_path):
        engine = tmp_path / "engine"
        engine.mkdir()
        _write(engine / "file.txt", "data")

        overlay = tmp_path / "overlay"
        overlay.mkdir()
        _write(overlay / "overlay.json", '{"overlay": {"name": "org.test.overlay.x"}}')
        _write(overlay / "Overlay" / "real.txt", "real")

        ws = Workspace(tmp_path / "ws", engine, ObjectType.ENGINE,
                       attributions="off")
        ws.add_resolved_object("root", engine, ObjectType.ENGINE)
        ws.add_overlay(overlay, extends="root")
        ws.create()

        assert not (tmp_path / "ws" / "Overlays" / "org.test.overlay.x").exists()
        assert (tmp_path / "ws" / "Engines" / "root" / "real.txt").exists()

    def test_overlay_without_payload_composes_nothing(self, tmp_path):
        engine = tmp_path / "engine"
        engine.mkdir()
        _write(engine / "file.txt", "data")

        overlay = tmp_path / "overlay"
        overlay.mkdir()
        _write(overlay / "stray.txt", "should not compose")

        ws = Workspace(tmp_path / "ws", engine, ObjectType.ENGINE)
        ws.add_resolved_object("root", engine, ObjectType.ENGINE)
        ws.add_overlay(overlay, extends="root")
        ws.create()

        assert not (tmp_path / "ws" / "Engines" / "root" / "stray.txt").exists()

    def test_missing_overlay_warning(self, tmp_path):
        engine = tmp_path / "engine"
        engine.mkdir()
        _write(engine / "file.txt", "data")

        ws = Workspace(tmp_path / "ws", engine, ObjectType.ENGINE)
        ws.add_resolved_object("root", engine, ObjectType.ENGINE)
        ws.add_overlay(tmp_path / "nonexistent", precedence=0, extends="root")
        ws.create()
        # Should not raise — just warns


# ---------------------------------------------------------------------------
# TestWorkspaceUpdate
# ---------------------------------------------------------------------------

class TestWorkspaceUpdate:
    """Test update method."""

    def test_update_on_nonexistent_raises(self, tmp_path):
        ws = _make_ws(tmp_path)
        with pytest.raises(WorkspaceError, match="does not exist"):
            ws.update()

    def test_update_reapplies_overlay(self, tmp_path):
        engine = tmp_path / "engine"
        engine.mkdir()
        _write(engine / "file.txt", "base")

        overlay = tmp_path / "overlay"
        (overlay / "Overlay").mkdir(parents=True)

        ws = Workspace(tmp_path / "ws", engine, ObjectType.ENGINE)
        ws.add_resolved_object("root", engine, ObjectType.ENGINE)
        ws.add_overlay(overlay, extends="root")
        ws.create()

        # Now add a file to the overlay payload and update
        _write(overlay / "Overlay" / "new.txt", "added")
        ws.update()

        assert (tmp_path / "ws" / "Engines" / "root" / "new.txt").exists()


# ---------------------------------------------------------------------------
# TestWorkspaceGetStats
# ---------------------------------------------------------------------------

class TestWorkspaceGetStats:
    """Test get_stats method."""

    def test_stats_structure(self, tmp_path):
        engine = tmp_path / "engine"
        engine.mkdir()
        _write(engine / "a.txt", "a")

        ws = Workspace(tmp_path / "ws", engine, ObjectType.ENGINE)
        ws.add_resolved_object("root", engine, ObjectType.ENGINE)
        ws.create()

        stats = ws.get_stats()
        assert "root_path" in stats
        assert "total_files" in stats
        assert "resolved_objects" in stats
        assert "overlays" in stats
        assert stats["total_files"] == 1
        assert stats["resolved_objects"] == 1
        assert stats["overlays"] == 0


# ---------------------------------------------------------------------------
# TestCreateWorkspaceConvenience
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("mock_manifest")
class TestCreateWorkspaceConvenience:
    """Test create_workspace() convenience function."""

    def test_engine_root(self, tmp_path):
        engine = tmp_path / "engine"
        engine.mkdir()
        _write(engine / "engine.json", '{"engine": {"name": "test"}}')
        _write(engine / "code.cpp", "code")

        ws = create_workspace(
            target_path=tmp_path / "ws",
            root_object_path=engine,
            resolved_objects={},
        )
        assert (tmp_path / "ws").exists()
        assert ws.root_object_type == ObjectType.ENGINE

    def test_project_root(self, tmp_path):
        proj = tmp_path / "project"
        proj.mkdir()
        _write(proj / "project.json", '{"project": {"name": "test"}}')
        _write(proj / "code.cpp", "code")

        ws = create_workspace(
            target_path=tmp_path / "ws",
            root_object_path=proj,
            resolved_objects={},
        )
        assert ws.root_object_type == ObjectType.PROJECT

    def test_unknown_root_raises(self, tmp_path):
        unknown = tmp_path / "unknown"
        unknown.mkdir()

        with pytest.raises(WorkspaceError, match="Cannot determine root object type"):
            create_workspace(
                target_path=tmp_path / "ws",
                root_object_path=unknown,
                resolved_objects={},
            )

    def test_with_overlays(self, tmp_path):
        engine = tmp_path / "engine"
        engine.mkdir()
        _write(engine / "engine.json", '{"engine": {"name": "test"}}')

        overlay = tmp_path / "ov"
        overlay.mkdir()
        _write(overlay / "Overlay" / "patch.txt", "patch")

        ws = create_workspace(
            target_path=tmp_path / "ws",
            root_object_path=engine,
            resolved_objects={},
            overlays=[(overlay, 0, "test")],
        )
        # Overlay files compose INTO the extended object's tree
        assert (tmp_path / "ws" / "Engines" / "test" / "patch.txt").exists()
        assert not (tmp_path / "ws" / "Overlays").exists()

    def test_with_resolved_objects(self, tmp_path):
        engine = tmp_path / "engine"
        engine.mkdir()
        _write(engine / "engine.json", '{"engine": {"name": "test"}}')

        gem = tmp_path / "gem"
        gem.mkdir()
        _write(gem / "gem.json", '{"gem": {"name": "mygem"}}')

        ws = create_workspace(
            target_path=tmp_path / "ws",
            root_object_path=engine,
            resolved_objects={"mygem": (gem, ObjectType.GEM)},
        )
        assert ws.resolved_objects.get("mygem") is not None


# ---------------------------------------------------------------------------
# TestOverlayMerge (J3)
# ---------------------------------------------------------------------------

class TestOverlayMerge:
    """Test that overlays with extends merge into the base object tree."""

    def test_overlay_replaces_base_symlink(self, tmp_path):
        """Overlay payload file replaces matching symlink in base Engines/ tree."""
        engine = tmp_path / "engine"
        engine.mkdir()
        _write(engine / "data.txt", "base content")

        overlay = tmp_path / "overlay"
        overlay.mkdir()
        _write(overlay / "Overlay" / "data.txt", "overlay content")

        ws = Workspace(tmp_path / "ws", engine, ObjectType.ENGINE)
        ws.add_resolved_object("root", engine, ObjectType.ENGINE)
        ws.add_overlay(overlay, precedence=0, extends="root")
        ws.create()

        # Base tree should now point to overlay source
        base_file = tmp_path / "ws" / "Engines" / "root" / "data.txt"
        assert base_file.exists()
        assert base_file.read_text() == "overlay content"

    def test_overlay_adds_new_file_to_base(self, tmp_path):
        """Overlay file that doesn't exist in base is added to base tree."""
        engine = tmp_path / "engine"
        engine.mkdir()
        _write(engine / "existing.txt", "base")

        overlay = tmp_path / "overlay"
        overlay.mkdir()
        _write(overlay / "Overlay" / "new_file.txt", "new from overlay")

        ws = Workspace(tmp_path / "ws", engine, ObjectType.ENGINE)
        ws.add_resolved_object("root", engine, ObjectType.ENGINE)
        ws.add_overlay(overlay, precedence=0, extends="root")
        ws.create()

        # New file should appear in base tree
        added = tmp_path / "ws" / "Engines" / "root" / "new_file.txt"
        assert added.exists()
        assert added.read_text() == "new from overlay"

    def test_overlay_ownership_transferred(self, tmp_path):
        """Replaced base files should be owned by the overlay."""
        engine = tmp_path / "engine"
        engine.mkdir()
        _write(engine / "data.txt", "base")

        overlay = tmp_path / "overlay"
        overlay.mkdir()
        _write(overlay / "Overlay" / "data.txt", "patched")

        ws = Workspace(tmp_path / "ws", engine, ObjectType.ENGINE)
        ws.add_resolved_object("root", engine, ObjectType.ENGINE)
        ws.add_overlay(overlay, precedence=0, extends="root")
        ws.create()

        # Ownership of the base tree file should belong to overlay
        base_rel = "Engines/root/data.txt"
        assert ws.file_owners[base_rel] == "overlay"

    def test_overlay_without_extends_no_merge(self, tmp_path):
        """Overlay without a resolvable extends target is skipped entirely."""
        engine = tmp_path / "engine"
        engine.mkdir()
        _write(engine / "data.txt", "base content")

        overlay = tmp_path / "overlay"
        overlay.mkdir()
        _write(overlay / "Overlay" / "data.txt", "overlay content")

        ws = Workspace(tmp_path / "ws", engine, ObjectType.ENGINE)
        ws.add_resolved_object("root", engine, ObjectType.ENGINE)
        ws.add_overlay(overlay, precedence=0)  # no extends
        ws.create()

        # Base tree should still have base content
        base_file = tmp_path / "ws" / "Engines" / "root" / "data.txt"
        assert base_file.read_text() == "base content"

    def test_higher_precedence_wins(self, tmp_path):
        """When two overlays target the same base file, higher precedence wins."""
        engine = tmp_path / "engine"
        engine.mkdir()
        _write(engine / "data.txt", "base")

        ov1 = tmp_path / "ov_low"
        ov1.mkdir()
        _write(ov1 / "Overlay" / "data.txt", "low precedence")

        ov2 = tmp_path / "ov_high"
        ov2.mkdir()
        _write(ov2 / "Overlay" / "data.txt", "high precedence")

        ws = Workspace(tmp_path / "ws", engine, ObjectType.ENGINE)
        ws.add_resolved_object("root", engine, ObjectType.ENGINE)
        ws.add_overlay(ov1, precedence=10, extends="root")
        ws.add_overlay(ov2, precedence=20, extends="root")
        ws.create()

        # Higher precedence (applied last) should win
        base_file = tmp_path / "ws" / "Engines" / "root" / "data.txt"
        assert base_file.read_text() == "high precedence"

    def test_explicit_order_overrides_precedence(self, tmp_path):
        """Workspace-explicit overlay order (via _relink_object) wins over
        authored precedence: the LAST applied overlay owns conflicts."""
        from o3de_cli.commands.workspace import (
            _relink_object, _build_workspace_meta,
        )

        gem = tmp_path / "gem"
        gem.mkdir()
        _write(gem / "gem.json", '{"gem": {"name": "mygem"}}')
        _write(gem / "data.txt", "base")

        ov1 = tmp_path / "ov1"
        ov1.mkdir()
        _write(ov1 / "overlay.json", '{"overlay": {"name": "org.t.overlay.one"}}')
        _write(ov1 / "Overlay" / "data.txt", "one")

        ov2 = tmp_path / "ov2"
        ov2.mkdir()
        _write(ov2 / "overlay.json", '{"overlay": {"name": "org.t.overlay.two"}}')
        _write(ov2 / "Overlay" / "data.txt", "two")

        ws_path = tmp_path / "ws"
        ws_path.mkdir()
        meta = _build_workspace_meta(
            name="t", root_path=gem, root_type="gem",
            sources={"gems": {"mygem": str(gem)}},
        )

        target = ws_path / "Gems" / "mygem" / "data.txt"

        # Order: one then two → two wins
        _relink_object(ws_path, meta, "mygem", gem, ObjectType.GEM,
                       {"mygem": (gem, ObjectType.GEM)},
                       overlays=[(ov1, 0), (ov2, 1)])
        assert target.read_text() == "two"

        # Reversed order: two then one → one wins
        _relink_object(ws_path, meta, "mygem", gem, ObjectType.GEM,
                       {"mygem": (gem, ObjectType.GEM)},
                       overlays=[(ov2, 0), (ov1, 1)])
        assert target.read_text() == "one"

    def test_overlay_extends_gem(self, tmp_path):
        """Overlay extending a gem replaces files in Gems/ tree."""
        engine = tmp_path / "engine"
        engine.mkdir()
        _write(engine / "engine.txt", "engine")

        gem = tmp_path / "mygem"
        gem.mkdir()
        _write(gem / "code.cpp", "original")

        overlay = tmp_path / "gem_patch"
        overlay.mkdir()
        _write(overlay / "Overlay" / "code.cpp", "patched")

        ws = Workspace(tmp_path / "ws", engine, ObjectType.ENGINE)
        ws.add_resolved_object("root", engine, ObjectType.ENGINE)
        ws.add_resolved_object("mygem", gem, ObjectType.GEM)
        ws.add_overlay(overlay, precedence=0, extends="mygem")
        ws.create()

        gem_file = tmp_path / "ws" / "Gems" / "mygem" / "code.cpp"
        assert gem_file.read_text() == "patched"

    def test_overlay_extends_unknown_object_no_merge(self, tmp_path):
        """Overlay extending an object not in resolved_objects is skipped."""
        engine = tmp_path / "engine"
        engine.mkdir()
        _write(engine / "data.txt", "base")

        overlay = tmp_path / "overlay"
        overlay.mkdir()
        _write(overlay / "data.txt", "overlay")

        ws = Workspace(tmp_path / "ws", engine, ObjectType.ENGINE)
        ws.add_resolved_object("root", engine, ObjectType.ENGINE)
        ws.add_overlay(overlay, precedence=0, extends="nonexistent")
        ws.create()

        # Base tree unchanged
        base_file = tmp_path / "ws" / "Engines" / "root" / "data.txt"
        assert base_file.read_text() == "base"

        # Overlay was skipped — no stray tree
        assert not (tmp_path / "ws" / "Overlays").exists()

    def test_update_reapplies_overlay_merge(self, tmp_path):
        """update() re-applies overlay merge into base tree."""
        engine = tmp_path / "engine"
        engine.mkdir()
        _write(engine / "data.txt", "base")

        overlay = tmp_path / "overlay"
        (overlay / "Overlay").mkdir(parents=True)

        ws = Workspace(tmp_path / "ws", engine, ObjectType.ENGINE)
        ws.add_resolved_object("root", engine, ObjectType.ENGINE)
        ws.add_overlay(overlay, precedence=0, extends="root")
        ws.create()

        # Add a new file to the overlay payload and update
        _write(overlay / "Overlay" / "added.txt", "added via update")
        ws.update()

        # New file should appear in the base tree
        assert (tmp_path / "ws" / "Engines" / "root" / "added.txt").exists()

    def test_create_workspace_convenience_with_extends(self, tmp_path, mock_manifest):
        """create_workspace() passes extends through 3-tuples.

        The root object is keyed by its canonical object name (read from
        its object JSON), and ``extends`` carries the canonical name too.
        """
        engine = tmp_path / "engine"
        engine.mkdir()
        _write(engine / "engine.json", '{"engine": {"name": "test"}}')
        _write(engine / "data.txt", "base")

        overlay = tmp_path / "ov"
        overlay.mkdir()
        _write(overlay / "Overlay" / "data.txt", "patched")

        ws = create_workspace(
            target_path=tmp_path / "ws",
            root_object_path=engine,
            resolved_objects={},
            overlays=[(overlay, 0, "test")],
        )

        base_file = tmp_path / "ws" / "Engines" / "test" / "data.txt"
        assert base_file.read_text() == "patched"
