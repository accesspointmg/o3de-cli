# O3DE CLI - Publish Checks
# SPDX-License-Identifier: Apache-2.0 OR MIT

"""The checks behind ``o3de publish validate`` (and ``pack`` / ``push``).

Each check reads an object's JSON (and, for file references, its
directory) and appends to a :class:`Findings`.  Errors block publishing;
warnings are advice, and fail the run only under ``--strict``.  Nothing
here imports Click or prints.

JSON Schema validation is passed in as *schema_validator* so the command
module stays the place it is looked up (the tests patch it there).
"""

import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from o3de_cli.core.models import ObjectType, get_object_type

# Schema files bundled or referenced by URL (SCHEMA_BASE_URL)
SCHEMA_TYPE_MAP = {
    ObjectType.ENGINE: "o3de-engine-2.0.0.json",
    ObjectType.PROJECT: "o3de-project-2.0.0.json",
    ObjectType.GEM: "o3de-gem-2.0.0.json",
    ObjectType.TEMPLATE: "o3de-template-2.0.0.json",
    ObjectType.REPO: "o3de-repo-2.0.0.json",
    ObjectType.OVERLAY: "o3de-overlay-2.0.0.json",
}

# Required fields per object type in Schema 2.0.0
REQUIRED_FIELDS = {
    ObjectType.ENGINE: ["engine"],
    ObjectType.PROJECT: ["project"],
    ObjectType.GEM: ["gem"],
    ObjectType.TEMPLATE: ["template"],
    ObjectType.REPO: ["repo"],
    ObjectType.OVERLAY: ["overlay", "extends"],
}

# Required header fields inside the type dict
REQUIRED_HEADER_FIELDS = ["name", "version"]
# Repos are living indexes, not consumable artifacts: nothing depends on a repo
# by version, so their header only requires a name. Snapshots use 'releases'.
REQUIRED_HEADER_FIELDS_BY_TYPE = {
    "repo": ["name"],
}

# Object JSON base names, in the order a directory is searched
OBJECT_TYPE_NAMES = ("engine", "project", "gem", "template", "repo", "overlay")

# (list key in a release, ((field that needs a checksum, checksum field), ...))
_CHECKSUM_FIELDS = (
    ("downloads", (("source", "source_sha256"), ("lfs", "lfs_sha256"))),
    ("binaries", (("binary", "sha256"),)),
)

SchemaValidator = Callable[[dict, ObjectType], list[str]]


@dataclass
class Findings:
    """What validating one object found: errors block, warnings advise."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def is_valid_name(name: object) -> bool:
    """Check if name follows canonical naming convention."""
    # Reverse-domain name: dot-separated segments, the first starting with a
    # letter, at least two segments, none empty. Segments cannot contain dots,
    # so there is one way to match any input (the previous pattern let both
    # quantifiers consume dots, which is exponential on near-misses).
    return isinstance(name, str) and bool(re.fullmatch(r"[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+", name))


def is_valid_version(version: object) -> bool:
    """Check if version follows semver format."""
    return isinstance(version, str) and bool(re.match(r"^[0-9]+\.[0-9]+\.[0-9]+$", version))


def find_object_json(target: Path) -> tuple[Path | None, str | None]:
    """The object JSON to validate for *target* (a JSON file or a directory).

    In a directory the versioned 2.0.0 file wins over the legacy one, type
    by type.  Returns ``(path, None)``, or ``(None, error message)``.
    """
    if target.is_file() and target.suffix == ".json":
        return target, None
    if not target.is_dir():
        return None, f"Path does not exist or is not a file/directory: {target}"
    for type_name in OBJECT_TYPE_NAMES:
        for candidate in (target / f"{type_name}.2-0-0.json", target / f"{type_name}.json"):
            if candidate.exists():
                return candidate, None
    return None, f"No O3DE object JSON found in {target}"


def check_schema_header(data: dict, obj_type: ObjectType, findings: Findings) -> None:
    """Warn about a missing or mismatched ``$schema`` / ``$schemaVersion``."""
    schema_ref = data.get("$schema", "")
    if not schema_ref:
        findings.warnings.append(
            "Missing $schema reference — add '$schema' pointing to the 2.0.0 schema URL"
        )
    else:
        expected_schema = SCHEMA_TYPE_MAP.get(obj_type, "")
        if expected_schema and expected_schema not in schema_ref:
            findings.warnings.append(
                f"$schema references '{schema_ref}' — expected to contain '{expected_schema}'"
            )

    schema_version = data.get("$schemaVersion", "")
    if schema_version != "2.0.0":
        if schema_version:
            findings.warnings.append(f"$schemaVersion is '{schema_version}' — 2.0.0 recommended")
        else:
            findings.warnings.append("Missing $schemaVersion — should be '2.0.0'")


def check_required_fields(data: dict, obj_type: ObjectType, findings: Findings) -> None:
    """Error on missing top-level and header fields; warn on name/version format."""
    for field_name in REQUIRED_FIELDS.get(obj_type, []):
        if field_name not in data:
            findings.errors.append(f"Missing required field: '{field_name}'")

    type_key = obj_type.value
    type_data = data.get(type_key, {})
    if not isinstance(type_data, dict):
        return
    for field_name in REQUIRED_HEADER_FIELDS_BY_TYPE.get(type_key, REQUIRED_HEADER_FIELDS):
        if not type_data.get(field_name):
            findings.errors.append(f"Missing required header field: '{type_key}.{field_name}'")

    name = type_data.get("name", "")
    if name and not is_valid_name(name):
        findings.warnings.append(
            f"Name '{name}' doesn't match canonical format "
            "'<creator>.<type>.<identifier>' (e.g., org.o3de.gem.physx)"
        )
    version = type_data.get("version", "")
    if version and not is_valid_version(version):
        findings.warnings.append(f"Version '{version}' doesn't match semver format 'X.Y.Z'")


def check_licenses(data: dict, obj_root: Path, findings: Findings) -> None:
    """Every referenced license file must ship at the object root.

    Objects are distributed standalone, so they cannot rely on parent
    licensing: at least one license file should ship with the object.
    """
    licenses = data.get("licenses", [])
    if not licenses:
        findings.warnings.append("No 'licenses' field — recommended for published objects")
        return
    shipped = False
    for i, lic in enumerate(licenses):
        if not isinstance(lic, dict):
            continue
        rp = lic.get("relative_path")
        if not rp:
            continue
        if (obj_root / rp).exists():
            shipped = True
        else:
            findings.errors.append(
                f"licenses[{i}].relative_path '{rp}' does not exist at the object root"
            )
    if not shipped:
        findings.warnings.append(
            "No license file ships with the object (url-only licenses) — "
            "standalone redistribution requires the license text in the object"
        )


def check_relative_paths(data: dict, obj_root: Path, findings: Findings) -> None:
    """The ``icon`` and ``documentation`` files an object references must exist."""
    for field_name in ("icon", "documentation"):
        entry = data.get(field_name)
        if isinstance(entry, dict):
            rp = entry.get("relative_path")
            if rp and not (obj_root / rp).exists():
                findings.errors.append(
                    f"{field_name}.relative_path '{rp}' does not exist at the object root"
                )


def check_overlay_payload(obj_type: ObjectType, obj_root: Path, findings: Findings) -> None:
    """Overlays must carry their payload in the ``Overlay/`` subfolder.

    This mirrors the Template/ convention: object metadata at the root
    never composes; only ``Overlay/`` contents are composed onto the
    extended object.
    """
    if obj_type != ObjectType.OVERLAY:
        return
    payload_dir = obj_root / "Overlay"
    if not payload_dir.is_dir():
        findings.errors.append(
            "Overlay has no 'Overlay/' payload folder — the payload "
            "composed onto the extended object must live in "
            "'Overlay/'; the object root holds only metadata "
            "(overlay.json, licenses, icon, docs)"
        )
    elif not any(payload_dir.rglob("*")):
        findings.warnings.append("'Overlay/' payload folder is empty")


def missing_checksums(release: dict) -> Iterator[tuple[str, int, str, str]]:
    """The entries of one release that name an artifact but not its checksum.

    Yields ``(list key, index, artifact field, missing checksum field)``,
    e.g. ``("downloads", 0, "source", "source_sha256")``.
    """
    for list_key, pairs in _CHECKSUM_FIELDS:
        for j, entry in enumerate(release.get(list_key, [])):
            if not isinstance(entry, dict):
                continue
            for artifact_field, checksum_field in pairs:
                if entry.get(artifact_field) and not entry.get(checksum_field):
                    yield list_key, j, artifact_field, checksum_field


def check_release_integrity(data: dict, findings: Findings) -> None:
    """Warn about release artifacts published without a checksum."""
    releases = data.get("releases", [])
    if not releases:
        return
    for i, release in enumerate(releases):
        if not isinstance(release, dict):
            continue
        for list_key, j, artifact_field, checksum_field in missing_checksums(release):
            message = (
                f"Release[{i}].{list_key}[{j}]: has '{artifact_field}' but no '{checksum_field}'"
            )
            if artifact_field == "source":
                message += " — integrity verification won't work"
            findings.warnings.append(message)


def deprecation_of(data: dict, obj_type: ObjectType) -> object:
    """The object's ``deprecated`` value: the header's, else the top-level one."""
    type_data = data.get(obj_type.value, {})
    deprecated = type_data.get("deprecated") if isinstance(type_data, dict) else None
    return deprecated or data.get("deprecated")


def check_object(
    data: dict,
    obj_type: ObjectType,
    obj_root: Path,
    schema_validator: SchemaValidator,
) -> Findings:
    """Run every publish check on a loaded object JSON.

    *obj_root* is the directory holding the JSON, against which the
    files it references are checked.  The JSON Schema is checked only
    when the object declares a ``$schema``.
    """
    findings = Findings()
    check_schema_header(data, obj_type, findings)
    if data.get("$schema", ""):
        findings.errors.extend(f"Schema: {se}" for se in schema_validator(data, obj_type))
    check_required_fields(data, obj_type, findings)
    if "origin" not in data:
        findings.warnings.append("Missing 'origin' field — recommended for published objects")
    check_licenses(data, obj_root, findings)
    check_relative_paths(data, obj_root, findings)
    check_overlay_payload(obj_type, obj_root, findings)
    check_release_integrity(data, findings)
    deprecated = deprecation_of(data, obj_type)
    if deprecated:
        findings.warnings.append(f"Object is marked deprecated: {deprecated}")
    return findings


def validate_object_at(target: Path, schema_validator: SchemaValidator) -> Findings:
    """Find, load and check the O3DE object at *target* (a directory or JSON file)."""
    json_path, error = find_object_json(target)
    if json_path is None:
        return Findings(errors=[error or f"No O3DE object JSON found in {target}"])

    try:
        with open(json_path) as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        return Findings(errors=[f"Invalid JSON in {json_path}: {e}"])

    try:
        obj_type = get_object_type(data)
    except Exception:
        return Findings(
            errors=[
                "Cannot determine object type from JSON — missing type key (engine/project/gem/...)"
            ]
        )

    return check_object(data, obj_type, json_path.parent, schema_validator)
