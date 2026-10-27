# O3DE CLI - JSON Schema Validation
# SPDX-License-Identifier: Apache-2.0 OR MIT

"""JSON Schema validation against canonical 2.0.0 schemas.

The schemas ship inside the package (``o3de_cli/schemas``, refreshed with
``scripts/sync_schemas.py``), so validation works for anyone who installs the
CLI. Set ``O3DE_SCHEMA_DIR`` to validate against a different copy, such as a
canonical.o3de.org checkout with schema changes in progress.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from o3de_cli.core.models import ObjectType

logger = logging.getLogger(__name__)

# Map object types to their canonical schema filenames
SCHEMA_FILENAMES = {
    ObjectType.ENGINE: "o3de-engine-2.0.0.json",
    ObjectType.PROJECT: "o3de-project-2.0.0.json",
    ObjectType.GEM: "o3de-gem-2.0.0.json",
    ObjectType.TEMPLATE: "o3de-template-2.0.0.json",
    ObjectType.REPO: "o3de-repo-2.0.0.json",
    ObjectType.OVERLAY: "o3de-overlay-2.0.0.json",
}

# Workspace schema filename — not keyed by ObjectType since workspace
# is not an O3DE object type, just a local build artifact.
WORKSPACE_SCHEMA_FILENAME = "o3de-workspace-2.0.0.json"

BUNDLED_SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"


class SchemaValidationError(Exception):
    """Raised when JSON Schema validation fails."""


def find_schema_directory() -> Path | None:
    """Locate the directory holding the canonical schema files.

    ``O3DE_SCHEMA_DIR`` wins when set; otherwise the copy bundled with the
    package is used.
    """
    env_path = os.environ.get("O3DE_SCHEMA_DIR")
    if env_path:
        p = Path(env_path)
        if p.is_dir():
            return p
        logger.warning("O3DE_SCHEMA_DIR is not a directory, using bundled schemas: %s", p)

    if BUNDLED_SCHEMA_DIR.is_dir():
        return BUNDLED_SCHEMA_DIR
    return None


def build_validator(schema_filename: str, schema_dir: Path | None = None) -> Any:
    """Return a Draft 7 validator for *schema_filename*.

    Every schema in the directory is registered so ``$ref`` links between
    them (mostly into ``o3de-patterns-2.0.0.json``) resolve.

    Raises:
        SchemaValidationError: if jsonschema is unavailable or the schema
            cannot be found or loaded.
    """
    try:
        import jsonschema
        import referencing
        import referencing.jsonschema
    except ImportError as e:
        raise SchemaValidationError(
            "jsonschema package not installed — run: pip install jsonschema"
        ) from e

    if schema_dir is None:
        schema_dir = find_schema_directory()
    if schema_dir is None or not schema_dir.is_dir():
        raise SchemaValidationError(
            "Cannot find the canonical schema directory — reinstall o3de-cli or set O3DE_SCHEMA_DIR"
        )

    schema_path = schema_dir / schema_filename
    if not schema_path.exists():
        raise SchemaValidationError(f"Schema file not found: {schema_path}")
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise SchemaValidationError(f"Failed to load schema {schema_path}: {e}") from e

    resources = []
    for json_file in sorted(schema_dir.glob("*.json")):
        try:
            s = json.loads(json_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(s, dict):
            continue
        resource = referencing.Resource.from_contents(
            s, default_specification=referencing.jsonschema.DRAFT7
        )
        resources.append((s.get("$id", f"./{json_file.name}"), resource))

    registry = referencing.Registry().with_resources(resources)
    return jsonschema.Draft7Validator(schema, registry=registry)


def validate_against_schema(
    data: dict,
    obj_type: ObjectType,
    schema_dir: Path | None = None,
) -> list[str]:
    """Validate object data against its canonical JSON Schema.

    Args:
        data: The parsed JSON object data.
        obj_type: The O3DE object type.
        schema_dir: Directory containing the canonical schema files.
                     If None, the bundled schemas (or O3DE_SCHEMA_DIR) are used.

    Returns:
        List of validation error messages (empty = valid).
    """
    schema_filename = SCHEMA_FILENAMES.get(obj_type)
    if not schema_filename:
        return [f"No canonical schema defined for object type: {obj_type.value}"]

    try:
        validator = build_validator(schema_filename, schema_dir)
    except SchemaValidationError as e:
        return [str(e)]

    errors = []
    for error in sorted(validator.iter_errors(data), key=lambda e: list(e.path)):
        path = ".".join(str(p) for p in error.absolute_path) or "(root)"
        errors.append(f"[{path}] {error.message}")
    return errors
