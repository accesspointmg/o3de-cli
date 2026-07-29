# O3DE Pilot CLI - Composition plan tests
# SPDX-License-Identifier: Apache-2.0 OR MIT

"""Tests for declarative object composition.

The interesting behaviour is all in rule derivation and ordering, which is
where the real bugs were: an over-eager collapse that would have moved an
entire gem into an overlay, and a rule-order warning based on a wrong model of
how filter-repo matches.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from o3de_cli.core.compose import (
    ComposePlan,
    FamilySpec,
    _strip_common_tail,
    collapse_mappings,
    filter_args_for,
    rules_for,
    validate_rule_order,
)


class TestStripCommonTail:
    """Reducing a mapping to the prefixes that actually differ."""

    def test_shared_tail_is_removed(self):
        assert _strip_common_tail("A/x/y.cpp", "B/x/y.cpp") == ("A", "B")

    def test_no_shared_tail_is_left_alone(self):
        assert _strip_common_tail("Gems/PhysX/Core/PhysX5", "Gems/PhysX") == (
            "Gems/PhysX/Core/PhysX5",
            "Gems/PhysX",
        )

    def test_never_strips_everything(self):
        # Identical paths must not reduce to a pair of empty strings.
        src, dst = _strip_common_tail("A/b", "A/b")
        assert src and dst


class TestCollapseMappings:
    """Authoring-time collapse of many file drags into few prefix rules."""

    def test_many_files_one_directory_becomes_one_rule(self):
        entries = [
            ("Gems/Foo/a.cpp", "Gems/Bar/a.cpp"),
            ("Gems/Foo/b.cpp", "Gems/Bar/b.cpp"),
            ("Gems/Foo/sub/c.h", "Gems/Bar/sub/c.h"),
        ]
        assert collapse_mappings(entries) == [("Gems/Foo", "Gems/Bar")]

    def test_order_of_first_appearance_is_preserved(self):
        entries = [("B/x", "Y/x"), ("A/x", "Z/x")]
        assert collapse_mappings(entries) == [("B", "Y"), ("A", "Z")]

    def test_distinct_destinations_stay_distinct(self):
        entries = [("A/x.cpp", "B/x.cpp"), ("A/y.cpp", "C/y.cpp")]
        assert set(collapse_mappings(entries)) == {("A", "B"), ("A", "C")}


class TestRulesFor:
    """Execution takes plan rules literally, payload first."""

    def _family(self) -> FamilySpec:
        return FamilySpec.model_validate(
            {
                "name": "org.o3de.repo.physx",
                "map": [{"from": "Gems/PhysX/Core/PhysX5", "to": "Gems/PhysX"}],
                "create": [
                    {
                        "kind": "overlay",
                        "at": "Overlays/physx.windows",
                        "payload": [
                            {
                                "from": "Gems/PhysX/Core/PhysX5/Source/Platform/Windows",
                                "to": "Source/Platform/Windows",
                            }
                        ],
                    }
                ],
            }
        )

    def test_payload_rules_come_before_map_rules(self):
        rules = rules_for(self._family())
        assert rules[0][1].startswith("Overlays/physx.windows/Overlay/")
        assert rules[-1] == ("Gems/PhysX/Core/PhysX5", "Gems/PhysX")

    def test_payload_destination_is_nested_under_overlay_subfolder(self):
        # The overlay root holds metadata that is never composed; payload must
        # land under Overlay/.
        _src, dst = rules_for(self._family())[0]
        assert dst == "Overlays/physx.windows/Overlay/Source/Platform/Windows"

    def test_execution_does_not_collapse_rules(self):
        # Collapsing this pair would yield
        # Gems/PhysX/Core/PhysX5 -> Overlays/physx.windows/Overlay
        # and move the whole gem into the overlay.
        rules = rules_for(self._family())
        assert ("Gems/PhysX/Core/PhysX5", "Overlays/physx.windows/Overlay") not in rules


class TestValidateRuleOrder:
    """filter-repo applies the first matching rename, so order decides."""

    def test_earlier_broad_rule_makes_later_rule_unreachable(self):
        rules = [("Gems/PhysX", "A"), ("Gems/PhysX/Source/Platform/Windows", "B")]
        warnings = validate_rule_order(rules)
        assert len(warnings) == 1
        assert "unreachable" in warnings[0]

    def test_payload_before_parent_is_not_a_warning(self):
        rules = [
            ("Gems/PhysX/Source/Platform/Windows", "Overlays/w/Overlay/Source/Platform/Windows"),
            ("Gems/PhysX", "Gems/PhysX"),
        ]
        assert validate_rule_order(rules) == []

    def test_renames_are_not_treated_as_chaining(self):
        # A later source resembling an earlier destination is fine: matching is
        # against the original path, so renames do not chain.
        rules = [("Gems/PhysX/Core", "Gems/PhysX"), ("Gems/PhysX/Common", "Gems/PhysXCommon")]
        assert validate_rule_order(rules) == []


class TestFilterArgs:
    def test_path_kept_for_every_rule_and_rename_only_when_differing(self):
        family = FamilySpec.model_validate(
            {
                "name": "f",
                "map": [
                    {"from": "Gems/A", "to": "Gems/B"},
                    {"from": "Gems/C", "to": "Gems/C"},
                ],
            }
        )
        args = filter_args_for(family)
        assert args.count("--path") == 2
        assert args.count("--path-rename") == 1
        assert "Gems/A/:Gems/B/" in args


class TestPlanValidation:
    def test_duplicate_family_names_rejected(self):
        with pytest.raises(ValidationError, match="duplicate family name"):
            ComposePlan.model_validate({"families": [{"name": "dup"}, {"name": "dup"}]})

    def test_path_escape_rejected(self):
        with pytest.raises(ValidationError, match="escape"):
            FamilySpec.model_validate({"name": "f", "map": [{"from": "../outside", "to": "here"}]})

    def test_unknown_object_kind_rejected(self):
        with pytest.raises(ValidationError, match="unknown object kind"):
            FamilySpec.model_validate(
                {"name": "f", "create": [{"kind": "sandwich", "at": "x.json"}]}
            )

    def test_backslashes_normalised(self):
        fam = FamilySpec.model_validate(
            {"name": "f", "map": [{"from": "Gems\\A", "to": "Gems\\B"}]}
        )
        assert fam.map[0].from_ == "Gems/A"

    def test_copy_is_spelled_copy_in_the_plan(self):
        # The field is copy_ in Python because BaseModel has .copy already.
        fam = FamilySpec.model_validate(
            {"name": "f", "copy": [{"from": "L.TXT", "to": ["a/L.TXT", "b/L.TXT"]}]}
        )
        assert len(fam.copy_) == 1
        assert fam.copy_[0].to == ["a/L.TXT", "b/L.TXT"]

    def test_history_defaults_on(self):
        assert FamilySpec.model_validate({"name": "f"}).history is True

    def test_unknown_plan_keys_rejected(self):
        with pytest.raises(ValidationError):
            ComposePlan.model_validate({"familes": []})  # typo, extra="forbid"
