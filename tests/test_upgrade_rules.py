# O3DE CLI - Legacy (0 -> 1.0.0) upgrade rule tests
# SPDX-License-Identifier: Apache-2.0 OR MIT

"""Unit tests for the field-rule tables behind upgrade_0_to_1."""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from o3de_cli.core import upgrade
from o3de_cli.core.upgrade import (
    _DESCRIPTION_RULES,
    _METADATA_RULES,
    _OPTIONAL,
    _PASSTHROUGH_RULES,
    _TYPE_RULES,
    _apply_field_rules,
    _copied,
    _FieldRule,
    _identity_rules,
    _last_updated,
    upgrade_0_to_1,
)

FIXED_NOW = "2026-01-02T03:04:05+00:00"


class _FixedDateTime:
    @staticmethod
    def now(tz=None):
        return datetime(2026, 1, 2, 3, 4, 5, tzinfo=tz)


class TestApplyFieldRules:
    def test_first_present_source_wins_even_when_falsy(self):
        out: dict = {}
        _apply_field_rules(out, {"b": None, "c": "x"}, (_FieldRule("t", ("a", "b", "c"), "d"),))
        assert out == {"t": None}

    def test_default_when_no_source_present(self):
        out: dict = {}
        _apply_field_rules(out, {}, (_FieldRule("t", ("a",), ""),))
        assert out == {"t": ""}

    def test_optional_rule_is_omitted_when_absent(self):
        out: dict = {}
        _apply_field_rules(out, {"other": 1}, (_FieldRule("t", ("a",)),))
        assert out == {}
        assert _FieldRule("t", ("a",)).default is _OPTIONAL

    def test_writes_in_rule_order_and_shares_values(self):
        tags = ["a"]
        out: dict = {"first": 0}
        _apply_field_rules(out, {"y": tags, "x": 2}, _copied("x", "y"))
        assert list(out) == ["first", "x", "y"]
        assert out["y"] is tags


class TestRuleTables:
    def test_identity_rules(self):
        assert _identity_rules("gem") == (
            _FieldRule("gem_name", ("gem_name",), ""),
            _FieldRule("gem_uri", ("gem_uri", "gem_url", "url", "uri"), ""),
            _FieldRule("gem_type", ("gem_type", "type"), ""),
        )

    def test_type_specific_extras(self):
        engine = [r.target for r in _TYPE_RULES["engine"]]
        project = [r.target for r in _TYPE_RULES["project"]]
        assert engine[3:] == ["O3DEVersion", "O3DEBuildNumber"]
        assert project[3:] == ["project_id", "product_name", "executable_name", "engine"]
        assert "restricted" not in _TYPE_RULES

    def test_manifest_fields_all_default_to_empty(self):
        rules = _TYPE_RULES["manifest"]
        assert rules[0].target == "o3de_manifest_name"
        assert all(r.default == "" and r.sources == (r.target,) for r in rules)

    @pytest.mark.parametrize(
        "table", [_DESCRIPTION_RULES, _METADATA_RULES, _PASSTHROUGH_RULES, *_TYPE_RULES.values()]
    )
    def test_no_table_writes_a_field_twice(self, table):
        targets = [r.target for r in table]
        assert len(targets) == len(set(targets))

    def test_uri_spellings_normalise_to_url(self):
        by_target = {r.target: r.sources for r in _METADATA_RULES}
        for field in ("origin", "license", "icon", "documentation"):
            assert by_target[f"{field}_url"] == (f"{field}_url", f"{field}_uri")


class TestLastUpdated:
    def test_keeps_legacy_value(self):
        assert _last_updated({"last_updated": None}) is None

    def test_generates_utc_now(self):
        with patch.object(upgrade, "datetime", _FixedDateTime):
            assert _last_updated({}) == FIXED_NOW
        assert datetime.fromisoformat(_last_updated({})).tzinfo == timezone.utc


class TestUpgradeZeroToOne:
    def test_gem_field_order(self):
        data = {
            "gem_name": "Foo",
            "url": "https://example.com/foo",
            "type": "Code",
            "display_name": "Foo Gem",
            "license_uri": "https://example.com/license",
            "platforms": ["Windows"],
        }
        with patch.object(upgrade, "datetime", _FixedDateTime):
            out = upgrade_0_to_1(data, "gem")
        assert list(out.items()) == [
            ("$schemaVersion", "1.0.0"),
            ("gem_name", "Foo"),
            ("gem_uri", "https://example.com/foo"),
            ("gem_type", "Code"),
            ("version", "0.0.0"),
            ("display_name", "Foo Gem"),
            ("summary", "Foo Gem"),
            ("last_updated", FIXED_NOW),
            ("license_url", "https://example.com/license"),
            ("platforms", ["Windows"]),
        ]

    def test_manifest_gets_no_descriptive_or_metadata_fields(self):
        out = upgrade_0_to_1(
            {"o3de_manifest_name": "m", "license": "MIT", "version": "1", "gems": ["g"]},
            "manifest",
        )
        assert list(out) == [
            "$schemaVersion",
            *[r.target for r in _TYPE_RULES["manifest"]],
            "gems",
        ]
        assert out["o3de_manifest_name"] == "m"

    def test_type_without_rules_still_gets_common_fields(self):
        out = upgrade_0_to_1({"restricted_name": "r", "last_updated": "then"}, "restricted")
        assert list(out) == ["$schemaVersion", "version", "display_name", "summary", "last_updated"]
        assert out["last_updated"] == "then"

    def test_summary_falls_back_through_description_and_name(self):
        assert upgrade_0_to_1({"description": "d", "name": "n"}, "gem")["summary"] == "d"
        assert upgrade_0_to_1({"name": "n"}, "gem")["summary"] == "n"
