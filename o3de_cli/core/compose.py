# O3DE Pilot CLI - Object composition (Chickenator)
# SPDX-License-Identifier: Apache-2.0 OR MIT

"""Declarative composition of object family repositories.

``o3de object hoist`` computes its ``git filter-repo`` arguments from the
working tree at run time and records nothing, so a family repository cannot
be regenerated or updated from upstream afterwards. This module replaces that
with a plan: a file that states which source paths become which destination
paths, what objects to synthesise, and which commit it was resolved against.

Same plan plus same source commit produces the same output, which is what
makes a family repository safe to delete and recreate.

A plan describes three kinds of operation per family, because history rewriting
and metadata generation are genuinely different phases:

``map``
    Source path to destination path. Becomes ``--path`` / ``--path-rename``
    arguments, so git history follows the files.

``create``
    An object that has no source -- the family's ``repo.json``, an
    ``overlay.json`` descriptor. Written in a metadata commit on top of the
    filtered history, so these carry no history by nature. Fields are derived
    where possible rather than authored, because hand-authored object metadata
    is how ``overlay.target`` (which is not in the schema) ended up in 56
    overlay descriptors.

``copy``
    One source fanned out to several destinations, e.g. a gem's licence files
    landing at the repository root and inside every overlay. ``filter-repo``
    cannot express one-to-many in a rename rule, so these also happen in the
    metadata commit.
"""

from __future__ import annotations

import subprocess
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ComposeError(Exception):
    """Raised for malformed plans or unusable source trees."""


# ---------------------------------------------------------------------------
# Plan model
# ---------------------------------------------------------------------------


class SourceSpec(BaseModel):
    """Where a plan is resolved from, and with which tool."""

    model_config = ConfigDict(extra="forbid")

    repo: str = Field(default=".", description="Path to the source repository")
    commit: str = Field(
        default="HEAD",
        description="Commit to filter. Recorded into provenance so a plan can be replayed.",
    )
    filter_repo_version: str | None = Field(
        default=None,
        description=(
            "git-filter-repo version this plan was last run with. Determinism is "
            "per-implementation, so a differing version is worth reporting."
        ),
    )


class MapRule(BaseModel):
    """A source path that becomes a destination path, history included."""

    model_config = ConfigDict(extra="forbid")

    from_: str = Field(alias="from", description="Source path, relative to the source repo")
    to: str = Field(description="Destination path, relative to the family repo root")

    @field_validator("from_", "to")
    @classmethod
    def _clean(cls, v: str) -> str:
        v = v.replace("\\", "/").strip("/")
        if not v:
            raise ValueError("path must not be empty")
        if ".." in PurePosixPath(v).parts:
            raise ValueError(f"path must not escape the repository: {v!r}")
        return v


class CreateSpec(BaseModel):
    """An object synthesised at the destination, with no source."""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(description="Object kind: repo, overlay, gem, project, template")
    at: str = Field(description="Destination path of the object directory or JSON file")
    name: str | None = Field(default=None, description="Canonical name; derived when omitted")
    extends: str | None = Field(
        default=None,
        description='For overlays: the object extended. "derive" infers it from the payload.',
    )
    precedence: int | None = Field(default=None, description="For overlays: apply order")
    platforms: list[str] = Field(
        default_factory=list, description="For overlays: selection criteria"
    )
    children: str | None = Field(
        default=None,
        description='For repos: "derive" computes children from what this family actually maps.',
    )
    payload: list[MapRule] = Field(
        default_factory=list,
        description=(
            "Map rules nested inside a created overlay. Destinations are relative to the "
            "overlay's Overlay/ subfolder, never its root -- the root holds metadata that "
            "is never composed."
        ),
    )

    @field_validator("kind")
    @classmethod
    def _known_kind(cls, v: str) -> str:
        allowed = {"repo", "overlay", "gem", "project", "template", "engine"}
        if v not in allowed:
            raise ValueError(f"unknown object kind {v!r}, expected one of {sorted(allowed)}")
        return v


class CopySpec(BaseModel):
    """One source file fanned out to one or more destinations."""

    model_config = ConfigDict(extra="forbid")

    from_: str = Field(alias="from", description="Source file")
    to: list[str] = Field(description="One or more destination paths")


class FamilySpec(BaseModel):
    """One family repository to produce."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Family repository name, e.g. org.o3de.repo.physx")
    history: bool = Field(
        default=True,
        description=(
            "Preserve history with git filter-repo. Disabling it produces a snapshot with "
            "no ancestry to the source, which can never merge from upstream again."
        ),
    )
    map: list[MapRule] = Field(default_factory=list)
    create: list[CreateSpec] = Field(default_factory=list)
    # Named copy_ because BaseModel already has a .copy attribute; the plan
    # file still spells it "copy".
    copy_: list[CopySpec] = Field(default_factory=list, alias="copy")


class ComposePlan(BaseModel):
    """A complete composition plan."""

    model_config = ConfigDict(extra="forbid")

    version: str = Field(default="1", description="Plan format version")
    source: SourceSpec = Field(default_factory=SourceSpec)
    families: list[FamilySpec] = Field(default_factory=list)

    @field_validator("families")
    @classmethod
    def _unique_names(cls, v: list[FamilySpec]) -> list[FamilySpec]:
        seen: set[str] = set()
        for fam in v:
            if fam.name in seen:
                raise ValueError(f"duplicate family name: {fam.name}")
            seen.add(fam.name)
        return v


# ---------------------------------------------------------------------------
# Rule derivation
# ---------------------------------------------------------------------------


def _strip_common_tail(src: str, dst: str) -> tuple[str, str]:
    """Reduce a pair of paths to the prefixes that actually differ.

    ``filter-repo`` renames are prefix rules, so a hundred files dragged from
    one directory to another are a single rule, not a hundred. Trailing path
    components shared by both sides are the part the rule does not need to
    mention.

        A/x/y.cpp -> B/x/y.cpp   becomes   A -> B
        Gems/PhysX/Core/PhysX5 -> Gems/PhysX   is already minimal
    """
    s = PurePosixPath(src).parts
    d = PurePosixPath(dst).parts
    common = 0
    while common < len(s) - 1 and common < len(d) - 1 and s[-1 - common] == d[-1 - common]:
        common += 1
    if common == 0:
        return src, dst
    return "/".join(s[: len(s) - common]), "/".join(d[: len(d) - common])


def collapse_mappings(entries: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Collapse per-file mappings into the minimal set of prefix rules.

    For plan *authoring* only -- a UI that lets someone drag two hundred files
    from one directory to another should write one rule, not two hundred.

    Deliberately not used when executing a plan. Collapsing is only sound when
    the mappings exhaustively cover the source prefix, and an explicit
    directory rule carries no such guarantee: collapsing

        Gems/PhysX/Core/PhysX5/Code/Source/Platform/Windows
            -> Overlays/physx.windows/Overlay/Code/Source/Platform/Windows

    would yield ``Gems/PhysX/Core/PhysX5 -> Overlays/physx.windows/Overlay``
    and move the whole gem into the overlay. A plan's rules are already what
    its author meant, so execution takes them literally.

    Order of first appearance is preserved, because rule order is significant.
    """
    rules: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for src, dst in entries:
        rule = _strip_common_tail(src, dst)
        if rule not in seen:
            seen.add(rule)
            rules.append(rule)
    return rules


def rules_for(family: FamilySpec) -> list[tuple[str, str]]:
    """The literal rule list for a family, in application order.

    Overlay payload rules come first so that a path claimed by an overlay is
    not also claimed by a broader rule covering the object it extends;
    ``filter-repo`` applies the first matching rename, so order decides.
    """
    rules: list[tuple[str, str]] = []
    for spec in family.create:
        for rule in spec.payload:
            rules.append((rule.from_, f"{spec.at.rstrip('/')}/Overlay/{rule.to}"))
    rules.extend((rule.from_, rule.to) for rule in family.map)
    return rules


def _is_prefix(prefix: str, path: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def validate_rule_order(rules: list[tuple[str, str]]) -> list[str]:
    """Report rules that can never match because an earlier one claims them.

    ``filter-repo`` applies the *first* matching rename to a path, so a rule
    whose source is a prefix of a later rule's source consumes those paths and
    the later rule never sees them. That is why overlay payload rules have to
    precede the rule covering the object they were carved out of.

    Renames do not chain -- matching is against the original path -- so a
    later rule whose source resembles an earlier rule's destination is not a
    problem and is not reported.
    """
    warnings: list[str] = []
    for i, (src_i, dst_i) in enumerate(rules):
        for j, (src_j, _dst_j) in enumerate(rules[i + 1 :], start=i + 1):
            if _is_prefix(src_i, src_j):
                warnings.append(
                    f"rule {j + 1} ({src_j} -> ...) is unreachable: rule {i + 1} "
                    f"({src_i} -> {dst_i}) matches those paths first"
                )
    return warnings


def filter_args_for(family: FamilySpec) -> list[str]:
    """Build the ``git filter-repo`` argument list for a family."""
    rules = rules_for(family)

    args: list[str] = []
    for src, _dst in rules:
        args += ["--path", f"{src}/"]
    for src, dst in rules:
        if src != dst:
            args += ["--path-rename", f"{src}/:{dst}/"]
    return args


# ---------------------------------------------------------------------------
# Source tree inspection
# ---------------------------------------------------------------------------


def list_source_files(repo: str, commit: str) -> list[str]:
    """List every tracked path at *commit*."""
    result = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", commit],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ComposeError(f"cannot list {commit} in {repo}: {result.stderr.strip()}")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def resolve_commit(repo: str, commit: str) -> str:
    """Resolve *commit* to a full object id, so provenance records something exact."""
    result = subprocess.run(["git", "rev-parse", commit], cwd=repo, capture_output=True, text=True)
    if result.returncode != 0:
        raise ComposeError(f"cannot resolve {commit} in {repo}: {result.stderr.strip()}")
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


class FamilyReport(BaseModel):
    """What composing one family would do."""

    name: str
    history: bool
    rules: list[tuple[str, str]] = Field(default_factory=list)
    filter_args: list[str] = Field(default_factory=list)
    mapped_files: int = 0
    missing_sources: list[str] = Field(default_factory=list)
    created: list[str] = Field(default_factory=list)
    copies: int = 0
    warnings: list[str] = Field(default_factory=list)


class PlanReport(BaseModel):
    """What composing a whole plan would do."""

    source_repo: str
    source_commit: str
    families: list[FamilyReport] = Field(default_factory=list)
    unmapped_files: int = 0
    total_source_files: int = 0
    warnings: list[str] = Field(default_factory=list)


def analyze(plan: ComposePlan, only: set[str] | None = None) -> PlanReport:
    """Work out what *plan* would produce, without writing anything.

    Every source file not covered by some family's rules is dropped, because
    ``--path`` is an allowlist. That count is reported prominently: it is the
    most likely way to lose files without noticing.
    """
    repo = plan.source.repo
    commit = resolve_commit(repo, plan.source.commit)
    source_files = list_source_files(repo, commit)

    report = PlanReport(
        source_repo=repo,
        source_commit=commit,
        total_source_files=len(source_files),
    )

    covered: set[str] = set()

    for family in plan.families:
        if only and family.name not in only:
            continue

        rules = rules_for(family)

        fam_report = FamilyReport(
            name=family.name,
            history=family.history,
            rules=rules,
            filter_args=filter_args_for(family),
            created=[f"{spec.kind}:{spec.at}" for spec in family.create],
            copies=sum(len(c.to) for c in family.copy_),
            warnings=validate_rule_order(rules),
        )

        for src, _dst in rules:
            hits = [f for f in source_files if _is_prefix(src, f)]
            if not hits:
                fam_report.missing_sources.append(src)
            covered.update(hits)
            fam_report.mapped_files += len(hits)

        if not family.history:
            fam_report.warnings.append(
                "history is disabled: the result will have no ancestry to the source "
                "and can never merge from upstream"
            )

        report.families.append(fam_report)

    report.unmapped_files = len(source_files) - len(covered)
    return report
