# O3DE CLI - Which engine subtrees hold embedded objects
# SPDX-License-Identifier: Apache-2.0 OR MIT

"""engine_embedded_topdirs: derived from the engine's declared children."""

from __future__ import annotations

import json
from pathlib import Path

from o3de_cli.core.workspace import _ENGINE_SKIP_TOPDIRS, engine_embedded_topdirs


def _engine(tmp_path: Path, data: dict | None, name: str = "engine.json") -> Path:
    root = tmp_path / "o3de"
    root.mkdir()
    if data is not None:
        (root / name).write_text(json.dumps(data))
    return root


def test_stock_legacy_layout(tmp_path):
    root = _engine(
        tmp_path,
        {
            "engine_name": "o3de",
            "external_subdirectories": ["Gems/Atom", "Gems/PhysX/Core"],
            "templates": ["Templates/DefaultGem"],
            "projects": ["AutomatedTesting"],
        },
    )
    # A child that is itself a top-level dir ("AutomatedTesting") counts too
    assert engine_embedded_topdirs(root) == {"Gems", "Templates", "AutomatedTesting"}


def test_stock_2_0_0_layout(tmp_path):
    root = _engine(
        tmp_path,
        {
            "$schemaVersion": "2.0.0",
            "engine": {"name": "org.o3de.engine.o3de"},
            "children": {
                "gems": ["Gems/Atom/gem.json"],
                "templates": ["Templates/DefaultGem/template.json"],
                "projects": ["AutomatedTesting/project.json"],
            },
        },
        name="engine.2-0-0.json",
    )
    assert engine_embedded_topdirs(root) == {"Gems", "Templates", "AutomatedTesting"}


def test_restructured_engine_skips_nothing(tmp_path):
    """Children kept outside the engine tree leave nothing to skip inside it."""
    root = _engine(
        tmp_path,
        {
            "$schemaVersion": "2.0.0",
            "engine": {"name": "org.o3de.engine.o3de"},
            "children": {
                "gems": ["../../Gems/Atom/gem.json"],
                "projects": ["../../Projects/AutomatedTesting/project.json"],
            },
        },
    )
    assert engine_embedded_topdirs(root) == set()


def test_no_children_declared_falls_back_to_stock_layout(tmp_path):
    root = _engine(tmp_path, {"engine_name": "o3de"})
    assert engine_embedded_topdirs(root) == _ENGINE_SKIP_TOPDIRS


def test_no_engine_json_falls_back(tmp_path):
    assert engine_embedded_topdirs(_engine(tmp_path, None)) == _ENGINE_SKIP_TOPDIRS


def test_unreadable_engine_json_falls_back(tmp_path):
    root = _engine(tmp_path, None)
    (root / "engine.json").write_text("{not json")
    assert engine_embedded_topdirs(root) == _ENGINE_SKIP_TOPDIRS
