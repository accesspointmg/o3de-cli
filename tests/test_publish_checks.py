# O3DE CLI - publish check tests
# SPDX-License-Identifier: Apache-2.0 OR MIT

"""Unit tests for o3de_cli.core.publish_checks.

The checks behind ``publish validate`` run directly on loaded JSON and
object directories, without Click or the console.
"""

import json
from pathlib import Path

import pytest

from o3de_cli.core.models import ObjectType
from o3de_cli.core.publish_checks import (
    Findings,
    check_licenses,
    check_object,
    check_overlay_payload,
    check_relative_paths,
    check_release_integrity,
    check_required_fields,
    check_schema_header,
    deprecation_of,
    find_object_json,
    is_valid_name,
    is_valid_version,
    missing_checksums,
    validate_object_at,
)

GEM_SCHEMA = "https://example.invalid/src/o3de-gem-2.0.0.json"


def _no_schema_errors(data: dict, obj_type: ObjectType) -> list[str]:
    return []


def _write(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# name / version format
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["org.o3de.gem.physx", "a.b", "my_org.x1"])
def test_valid_names(name):
    assert is_valid_name(name)


@pytest.mark.parametrize("name", ["Org.o3de", "single", "a..b", "a.b.", "", 7, None])
def test_invalid_names(name):
    assert not is_valid_name(name)


@pytest.mark.parametrize(
    "version,ok",
    [("1.0.0", True), ("10.20.30", True), ("1.0", False), ("v1.0.0", False), (3, False)],
)
def test_version_format(version, ok):
    assert is_valid_version(version) is ok


# ---------------------------------------------------------------------------
# find_object_json
# ---------------------------------------------------------------------------


class TestFindObjectJson:
    def test_json_file_is_used_as_is(self, tmp_path):
        f = _write(tmp_path / "anything.json", {})
        assert find_object_json(f) == (f, None)

    def test_non_json_file_is_an_error(self, tmp_path):
        f = tmp_path / "notes.txt"
        f.write_text("x")
        path, error = find_object_json(f)
        assert path is None
        assert error == f"Path does not exist or is not a file/directory: {f}"

    def test_versioned_file_beats_legacy(self, tmp_path):
        _write(tmp_path / "gem.json", {})
        versioned = _write(tmp_path / "gem.2-0-0.json", {})
        assert find_object_json(tmp_path) == (versioned, None)

    def test_types_are_searched_in_order(self, tmp_path):
        _write(tmp_path / "gem.2-0-0.json", {})
        engine = _write(tmp_path / "engine.json", {})
        assert find_object_json(tmp_path) == (engine, None)

    def test_empty_directory(self, tmp_path):
        assert find_object_json(tmp_path) == (None, f"No O3DE object JSON found in {tmp_path}")


# ---------------------------------------------------------------------------
# individual checks
# ---------------------------------------------------------------------------


class TestSchemaHeader:
    def test_complete_header_is_quiet(self):
        findings = Findings()
        check_schema_header(
            {"$schema": GEM_SCHEMA, "$schemaVersion": "2.0.0"}, ObjectType.GEM, findings
        )
        assert findings == Findings()

    def test_missing_both(self):
        findings = Findings()
        check_schema_header({}, ObjectType.GEM, findings)
        assert findings.errors == []
        assert [w.split(" —")[0] for w in findings.warnings] == [
            "Missing $schema reference",
            "Missing $schemaVersion",
        ]

    def test_wrong_schema_and_old_version(self):
        findings = Findings()
        data = {"$schema": "http://x/o3de-project-2.0.0.json", "$schemaVersion": "1.0.0"}
        check_schema_header(data, ObjectType.GEM, findings)
        assert findings.warnings == [
            "$schema references 'http://x/o3de-project-2.0.0.json' — expected to contain "
            "'o3de-gem-2.0.0.json'",
            "$schemaVersion is '1.0.0' — 2.0.0 recommended",
        ]


class TestRequiredFields:
    def test_missing_type_key_and_overlay_extends(self):
        findings = Findings()
        check_required_fields({}, ObjectType.OVERLAY, findings)
        # A missing type key also leaves its header fields missing.
        assert findings.errors == [
            "Missing required field: 'overlay'",
            "Missing required field: 'extends'",
            "Missing required header field: 'overlay.name'",
            "Missing required header field: 'overlay.version'",
        ]

    def test_missing_header_fields(self):
        findings = Findings()
        check_required_fields({"gem": {}}, ObjectType.GEM, findings)
        assert findings.errors == [
            "Missing required header field: 'gem.name'",
            "Missing required header field: 'gem.version'",
        ]

    def test_repo_needs_only_a_name(self):
        findings = Findings()
        check_required_fields({"repo": {"name": "org.r"}}, ObjectType.REPO, findings)
        assert findings == Findings()

    def test_non_dict_header_skips_header_checks(self):
        findings = Findings()
        check_required_fields({"gem": "text"}, ObjectType.GEM, findings)
        assert findings == Findings()

    def test_format_warnings(self):
        findings = Findings()
        check_required_fields({"gem": {"name": "Bad", "version": "1.0"}}, ObjectType.GEM, findings)
        assert findings.errors == []
        assert findings.warnings[0].startswith("Name 'Bad' doesn't match canonical format")
        assert findings.warnings[1] == "Version '1.0' doesn't match semver format 'X.Y.Z'"

    def test_non_string_name_and_version_warn_instead_of_crashing(self):
        # Regression: a non-string name or version raised TypeError out of
        # the regex match and crashed `publish validate`.
        findings = Findings()
        check_required_fields({"gem": {"name": 7, "version": 3}}, ObjectType.GEM, findings)
        assert findings.errors == []
        assert findings.warnings[0].startswith("Name '7' doesn't match canonical format")
        assert findings.warnings[1] == "Version '3' doesn't match semver format 'X.Y.Z'"


class TestLicenses:
    def test_no_licenses(self, tmp_path):
        findings = Findings()
        check_licenses({}, tmp_path, findings)
        assert findings.warnings == ["No 'licenses' field — recommended for published objects"]

    def test_shipped_license(self, tmp_path):
        (tmp_path / "LICENSE").write_text("x")
        findings = Findings()
        check_licenses({"licenses": [{"relative_path": "LICENSE"}]}, tmp_path, findings)
        assert findings == Findings()

    def test_missing_file_and_url_only(self, tmp_path):
        findings = Findings()
        data = {"licenses": ["text", {"url": "http://x"}, {"relative_path": "NOPE"}]}
        check_licenses(data, tmp_path, findings)
        assert findings.errors == [
            "licenses[2].relative_path 'NOPE' does not exist at the object root"
        ]
        assert findings.warnings[0].startswith("No license file ships with the object")


class TestRelativePaths:
    def test_missing_icon_and_documentation(self, tmp_path):
        findings = Findings()
        data = {"icon": {"relative_path": "i.png"}, "documentation": {"relative_path": "d.md"}}
        check_relative_paths(data, tmp_path, findings)
        assert findings.errors == [
            "icon.relative_path 'i.png' does not exist at the object root",
            "documentation.relative_path 'd.md' does not exist at the object root",
        ]

    def test_present_or_non_dict_entries_pass(self, tmp_path):
        (tmp_path / "i.png").write_text("x")
        findings = Findings()
        check_relative_paths(
            {"icon": {"relative_path": "i.png"}, "documentation": "s"}, tmp_path, findings
        )
        assert findings == Findings()


class TestOverlayPayload:
    def test_only_overlays_are_checked(self, tmp_path):
        findings = Findings()
        check_overlay_payload(ObjectType.GEM, tmp_path, findings)
        assert findings == Findings()

    def test_missing_payload_folder(self, tmp_path):
        findings = Findings()
        check_overlay_payload(ObjectType.OVERLAY, tmp_path, findings)
        assert findings.errors[0].startswith("Overlay has no 'Overlay/' payload folder")

    def test_empty_payload_folder(self, tmp_path):
        (tmp_path / "Overlay").mkdir()
        findings = Findings()
        check_overlay_payload(ObjectType.OVERLAY, tmp_path, findings)
        assert findings.warnings == ["'Overlay/' payload folder is empty"]

    def test_populated_payload_folder(self, tmp_path):
        (tmp_path / "Overlay").mkdir()
        (tmp_path / "Overlay" / "f.txt").write_text("x")
        findings = Findings()
        check_overlay_payload(ObjectType.OVERLAY, tmp_path, findings)
        assert findings == Findings()


class TestReleaseIntegrity:
    def test_missing_checksums_yields_each_gap(self):
        release = {
            "downloads": [
                {"source": "s", "lfs": "l"},
                "skip",
                {"source": "s", "source_sha256": "x"},
            ],
            "binaries": [{"binary": "b"}, {"binary": "b", "sha256": "y"}],
        }
        assert list(missing_checksums(release)) == [
            ("downloads", 0, "source", "source_sha256"),
            ("downloads", 0, "lfs", "lfs_sha256"),
            ("binaries", 0, "binary", "sha256"),
        ]

    def test_warnings(self):
        findings = Findings()
        data = {
            "releases": [
                "skip",
                {"downloads": [{"source": "s", "lfs": "l"}], "binaries": [{"binary": "b"}]},
            ]
        }
        check_release_integrity(data, findings)
        assert findings.warnings == [
            "Release[1].downloads[0]: has 'source' but no 'source_sha256' — "
            "integrity verification won't work",
            "Release[1].downloads[0]: has 'lfs' but no 'lfs_sha256'",
            "Release[1].binaries[0]: has 'binary' but no 'sha256'",
        ]


class TestDeprecation:
    def test_header_wins(self):
        data = {"gem": {"deprecated": "header"}, "deprecated": "top"}
        assert deprecation_of(data, ObjectType.GEM) == "header"

    def test_falls_back_to_top_level(self):
        assert deprecation_of({"gem": "text", "deprecated": "top"}, ObjectType.GEM) == "top"
        assert (
            deprecation_of({"gem": {"deprecated": ""}, "deprecated": "top"}, ObjectType.GEM)
            == "top"
        )

    def test_not_deprecated(self):
        assert not deprecation_of({"gem": {}}, ObjectType.GEM)


# ---------------------------------------------------------------------------
# check_object / validate_object_at
# ---------------------------------------------------------------------------


class TestCheckObject:
    def _good(self, tmp_path: Path) -> dict:
        (tmp_path / "LICENSE").write_text("x")
        return {
            "$schema": GEM_SCHEMA,
            "$schemaVersion": "2.0.0",
            "gem": {"name": "org.test.gem.a", "version": "1.0.0"},
            "origin": "me",
            "licenses": [{"relative_path": "LICENSE"}],
        }

    def test_clean_object(self, tmp_path):
        assert (
            check_object(self._good(tmp_path), ObjectType.GEM, tmp_path, _no_schema_errors)
            == Findings()
        )

    def test_schema_validator_runs_only_with_a_schema_ref(self, tmp_path):
        calls = []

        def validator(data, obj_type):
            calls.append(obj_type)
            return ["bad type"]

        data = self._good(tmp_path)
        assert check_object(data, ObjectType.GEM, tmp_path, validator).errors == [
            "Schema: bad type"
        ]
        del data["$schema"]
        assert check_object(data, ObjectType.GEM, tmp_path, validator).errors == []
        assert calls == [ObjectType.GEM]

    def test_deprecated_object_warns(self, tmp_path):
        data = self._good(tmp_path)
        data["deprecated"] = True
        findings = check_object(data, ObjectType.GEM, tmp_path, _no_schema_errors)
        assert findings.warnings == ["Object is marked deprecated: True"]


class TestValidateObjectAt:
    def test_no_object(self, tmp_path):
        assert validate_object_at(tmp_path, _no_schema_errors).errors == [
            f"No O3DE object JSON found in {tmp_path}"
        ]

    def test_invalid_json(self, tmp_path):
        (tmp_path / "gem.json").write_text("{nope")
        errors = validate_object_at(tmp_path, _no_schema_errors).errors
        assert len(errors) == 1 and errors[0].startswith("Invalid JSON in ")

    def test_unknown_type(self, tmp_path):
        _write(tmp_path / "gem.json", {"something": 1})
        assert validate_object_at(tmp_path, _no_schema_errors).errors == [
            "Cannot determine object type from JSON — missing type key (engine/project/gem/...)"
        ]

    def test_checks_relative_to_the_json_directory(self, tmp_path):
        obj = tmp_path / "obj"
        _write(
            obj / "gem.json",
            {"gem": {"name": "org.a.b", "version": "1.0.0"}, "icon": {"relative_path": "i.png"}},
        )
        (obj / "i.png").write_text("x")
        findings = validate_object_at(obj / "gem.json", _no_schema_errors)
        assert findings.errors == []
