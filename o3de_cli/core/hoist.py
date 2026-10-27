# O3DE CLI - Hoist an object out of its engine repo into a family repo
# SPDX-License-Identifier: Apache-2.0 OR MIT

"""Planning and execution for ``o3de object hoist``.

``hoist`` uses ``git filter-repo`` to project an object's (usually a
gem's) full git history into a new repo-rooted *family* repository::

    o3de-<family>/
      repo.json                      ← repo object (children below)
      Gems/<Gem>/                    ← gem history, platform dirs removed
      Overlays/<family>.<plat>/      ← per-platform overlay objects,
        Overlay/...                    platform file history preserved

The work is split in phases so ``--dry-run`` reports exactly what is
planned:

* :func:`locate_source` and :func:`require_filter_repo` validate the
  source (git root, branch, tooling);
* :func:`plan_hoist` computes the :class:`HoistPlan` — destination
  layout, overlays and the literal ``filter-repo`` argument list —
  without writing anything;
* :func:`execute_hoist` clones, filters, carries LFS content over, and
  commits the generated metadata; :func:`register_family_repo` adds the
  result to the manifest.

Nothing here depends on Click or Rich; failures are raised as
:class:`~o3de_cli.core.split_platforms.ObjectOpError` carrying the CLI
error code.

``o3de_cli.core.compose`` (declarative composition plans) is the planned
successor to this module.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from o3de_cli.core.models import get_object_name, get_object_version
from o3de_cli.core.paths import rmtree_force
from o3de_cli.core.split_platforms import (
    COMMON_PLATFORM,
    ObjectOpError,
    build_overlay_meta,
    check_overlay_name_clash,
    copy_licenses,
    family_repo_name,
    find_platform_dirs,
    object_licenses,
    parse_user_tags,
    register_local_paths,
    split_overlay_name,
    write_json_file,
)

#: Family-repo subdirectory for each object type (anything else → ``Gems``).
TYPE_SUBDIRS = {"gem": "Gems", "template": "Templates", "project": "Projects"}

#: repo.json ``children`` key for each object type (anything else → ``gems``).
CHILD_KEYS = {"gem": "gems", "template": "templates", "project": "projects"}

_CHILD_TYPES = ("engines", "projects", "gems", "templates", "repos", "overlays")

_REPO_SCHEMA = "https://raw.githubusercontent.com/accesspointmg/canonical.o3de.org/main/src/o3de-repo-2.0.0.json"


def run_git(args: list[str], cwd: Path | None = None, env: dict | None = None) -> tuple[int, str]:
    """Run a git command, returning (returncode, combined output)."""
    full_env = {**os.environ, **(env or {})}
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=full_env,
        capture_output=True,
        text=True,
    )
    return result.returncode, (result.stdout + result.stderr).strip()


# ── Source validation ───────────────────────────────────────────────


@dataclass
class HoistSource:
    """Where the object lives in its source git repository."""

    obj_root: Path
    git_root: Path
    gem_rel: str  # obj_root relative to git_root, POSIX
    branch: str


def locate_source(obj_root: Path, branch: str | None) -> HoistSource:
    """Find the git repo enclosing *obj_root* and the branch to hoist from.

    Raises :class:`ObjectOpError` with ``E_NOT_A_GIT_REPO``,
    ``E_NOT_IN_REPO`` or ``E_NO_BRANCH``.
    """
    rc, git_root_str = run_git(["rev-parse", "--show-toplevel"], cwd=obj_root)
    if rc != 0:
        raise ObjectOpError(f"Not inside a git repository: {obj_root}", "E_NOT_A_GIT_REPO")
    git_root = Path(git_root_str)
    try:
        gem_rel = obj_root.relative_to(git_root).as_posix()
    except ValueError:
        raise ObjectOpError(
            f"{obj_root} is not under git root {git_root}", "E_NOT_IN_REPO"
        ) from None

    if branch is None:
        rc, branch = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=git_root)
        if rc != 0 or branch == "HEAD":
            raise ObjectOpError(
                "Cannot determine the source branch (detached HEAD?) — pass --branch",
                "E_NO_BRANCH",
            )
    return HoistSource(obj_root=obj_root, git_root=git_root, gem_rel=gem_rel, branch=branch)


def require_filter_repo() -> None:
    """Raise ``E_NO_FILTER_REPO`` unless the ``git_filter_repo`` module is importable."""
    try:
        import git_filter_repo  # noqa: F401
    except ImportError:
        raise ObjectOpError(
            "git-filter-repo is not installed. Install it with: pip install git-filter-repo",
            "E_NO_FILTER_REPO",
        ) from None


# ── Plan ────────────────────────────────────────────────────────────


@dataclass
class HoistOverlay:
    """One per-platform overlay directory in the family repo."""

    dir_name: str  # e.g. ``widget.windows``, under ``Overlays/``
    platform: str
    name: str  # overlay object name
    skip_reason: str | None = None  # why no overlay will be created, if skipped

    def plan_entry(self) -> dict:
        """The ``--dry-run --json`` entry for this overlay."""
        entry = {
            "dir": f"Overlays/{self.dir_name}",
            "platform": self.platform,
            "overlay": self.name,
        }
        if self.skip_reason is not None:
            entry["reason"] = self.skip_reason
        return entry


@dataclass
class HoistPlan:
    """Everything ``hoist`` would do, computed without writing anything."""

    source: HoistSource
    obj_data: dict
    type_token: str
    base_name: str
    base_version: str
    ov_version: str
    family: str
    repo_name: str
    out_dir: Path
    gem_target: str  # object's path inside the family repo, e.g. ``Gems/Widget``
    precedence: int
    user_tags: list[str]
    common_name: str | None
    overlays: list[HoistOverlay] = field(default_factory=list)  # sorted by dir_name
    #: Platforms whose overlay will not be created (no files on the branch)
    skipped: list[HoistOverlay] = field(default_factory=list)
    renames: list[tuple[str, str]] = field(default_factory=list)  # platform dir renames
    filter_args: list[str] = field(default_factory=list)

    def dry_run_payload(self) -> dict:
        """The ``--dry-run --json`` payload (``skipped`` only when non-empty)."""
        payload = {
            "object": self.base_name,
            "repo": self.repo_name,
            "path": str(self.out_dir),
            "branch": self.source.branch,
            "gem_dir": self.gem_target,
            "overlays": [ov.plan_entry() for ov in self.overlays],
        }
        if self.skipped:
            payload["skipped"] = [ov.plan_entry() for ov in self.skipped]
        return payload


def platform_renames(
    detected: dict[str, list[Path]], source: HoistSource, family: str
) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """Map each platform dir to its overlay payload location.

    Returns (overlay dir name → platform, [(src, dst), ...]) with paths
    relative to the source git root and the family repo respectively.
    Platforms are visited in sorted order.
    """
    overlay_dirs: dict[str, str] = {}
    renames: list[tuple[str, str]] = []
    for plat, dirs in sorted(detected.items()):
        ov_dir = f"{family}.{plat.lower()}"
        overlay_dirs[ov_dir] = plat
        for plat_dir in dirs:
            src = plat_dir.relative_to(source.git_root).as_posix()
            obj_rel = plat_dir.relative_to(source.obj_root).as_posix()
            renames.append((src, f"Overlays/{ov_dir}/Overlay/{obj_rel}"))
    return overlay_dirs, renames


def build_filter_args(gem_rel: str, gem_target: str, renames: list[tuple[str, str]]) -> list[str]:
    """The ``git filter-repo`` argument list for a hoist.

    Keeps the object's history (``--path``) and renames each platform
    dir into its overlay payload.  filter-repo applies renames in order,
    each to the already-renamed path, so the platform renames must
    precede the (optional) object-root rename: a platform path renamed
    into ``Overlays/`` no longer matches the object prefix.
    """
    args = ["--path", f"{gem_rel}/"]
    for src, dst in renames:
        args += ["--path-rename", f"{src}/:{dst}/"]
    if gem_rel != gem_target:
        args += ["--path-rename", f"{gem_rel}/:{gem_target}/"]
    return args


def platforms_without_history(source: HoistSource, detected: dict[str, list[Path]]) -> set[str]:
    """Platforms with no tracked files at the tip of ``source.branch``.

    filter-repo only produces an overlay directory for a platform whose
    dirs hold tracked files on the hoisted branch; a dir that exists in
    the working tree only (untracked, or tracked on another branch)
    yields nothing.  Returns an empty set when the branch tree cannot be
    listed, leaving those overlays planned (execution still skips any
    directory filter-repo did not produce).
    """
    rel_dirs = {
        plat: [d.relative_to(source.git_root).as_posix() for d in dirs]
        for plat, dirs in detected.items()
    }
    all_dirs = [d for dirs in rel_dirs.values() for d in dirs]
    if not all_dirs:
        return set()
    rc, out = run_git(
        ["ls-tree", "-z", "--name-only", source.branch, "--", *all_dirs], cwd=source.git_root
    )
    if rc != 0:
        return set()
    present = set(out.split("\0"))
    return {plat for plat, dirs in rel_dirs.items() if not present.intersection(dirs)}


def plan_hoist(
    source: HoistSource,
    obj_data: dict,
    type_token: str,
    *,
    output: str | None = None,
    repo_name: str | None = None,
    overlay_version: str | None = None,
    tags: Iterable[str] = (),
    precedence: int = 10,
) -> HoistPlan:
    """Plan hoisting the object at ``source.obj_root`` into a family repo.

    Reads the working tree (to find platform dirs) and the branch tree
    (to find which of them have history) but writes nothing.  Raises
    :class:`ObjectOpError` (``E_OVERLAY_NAME_CLASH``) when two platform
    dirs differ only in case.
    """
    obj_root = source.obj_root
    base_name = get_object_name(obj_data) or obj_root.name
    base_version = get_object_version(obj_data)
    family = base_name.split(".")[-1]

    detected = find_platform_dirs(obj_root)
    check_overlay_name_clash(detected, obj_root)
    gem_target = f"{TYPE_SUBDIRS.get(type_token, 'Gems')}/{obj_root.name}"
    overlay_dirs, renames = platform_renames(detected, source, family)
    no_history = platforms_without_history(source, detected)

    overlays: list[HoistOverlay] = []
    skipped: list[HoistOverlay] = []
    for d, p in sorted(overlay_dirs.items()):
        ov = HoistOverlay(dir_name=d, platform=p, name=split_overlay_name(base_name, p))
        if p in no_history:
            ov.skip_reason = f"no tracked files on branch '{source.branch}'"
            skipped.append(ov)
        else:
            overlays.append(ov)

    return HoistPlan(
        source=source,
        obj_data=obj_data,
        type_token=type_token,
        base_name=base_name,
        base_version=base_version,
        ov_version=overlay_version or base_version or "1.0.0",
        family=family,
        repo_name=repo_name or family_repo_name(base_name),
        out_dir=Path(output).resolve() if output else source.git_root.parent / f"o3de-{family}",
        gem_target=gem_target,
        precedence=precedence,
        user_tags=parse_user_tags(tags),
        # Platform overlays depend on the common overlay only if it will exist
        common_name=(
            split_overlay_name(base_name, "common")
            if COMMON_PLATFORM in detected and COMMON_PLATFORM not in no_history
            else None
        ),
        overlays=overlays,
        skipped=skipped,
        renames=renames,
        filter_args=build_filter_args(source.gem_rel, gem_target, renames),
    )


# ── Execution ───────────────────────────────────────────────────────


@dataclass
class HoistResult:
    """What :func:`execute_hoist` produced."""

    overlays: list[str]  # overlay object names actually created
    lfs_objects: int = 0  # LFS objects copied from the source store
    warnings: list[str] = field(default_factory=list)  # e.g. skipped licenses


def clone_source(plan: HoistPlan) -> None:
    """Fresh single-branch clone of the source repo into ``plan.out_dir``.

    filter-repo requires a fresh clone.  LFS smudge is skipped: pointers
    filter fine and the object may not have LFS content at all.
    """
    source = plan.source
    rc, out = run_git(
        [
            "clone",
            "--no-local",
            "--single-branch",
            "--branch",
            source.branch,
            f"file://{source.git_root.as_posix()}",
            str(plan.out_dir),
        ],
        env={"GIT_LFS_SKIP_SMUDGE": "1"},
    )
    if rc != 0:
        raise ObjectOpError(f"Clone failed: {out}", "E_CLONE_FAILED")


def run_filter_repo(plan: HoistPlan) -> None:
    """Run ``git filter-repo`` in the clone (:func:`execute_hoist` removes it on failure)."""
    result = subprocess.run(
        [sys.executable, "-m", "git_filter_repo", *plan.filter_args],
        cwd=plan.out_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ObjectOpError(
            f"filter-repo failed: {result.stdout}{result.stderr}", "E_FILTER_FAILED"
        )


def _lfs_pointer_oid(pointer_file: Path) -> str | None:
    """The sha256 oid recorded in an LFS pointer file, or None if unreadable."""
    try:
        ptr = pointer_file.read_text(encoding="utf-8")
        return next(
            line.split(":", 1)[1].strip()
            for line in ptr.splitlines()
            if line.startswith("oid sha256:")
        )
    except Exception:
        return None


def _lfs_object_path(repo: Path, oid: str) -> Path:
    return repo / ".git" / "lfs" / "objects" / oid[:2] / oid[2:4] / oid


def carry_over_lfs(git_root: Path, out_dir: Path) -> int:
    """Carry LFS patterns and objects from the source repo into the split repo.

    The split repo inherits LFS *pointer* blobs but neither the root
    ``.gitattributes`` (it lived outside the object's paths) nor the LFS
    objects themselves.  Copy the source's ``.gitattributes`` and the
    objects for the files at HEAD from the source's local LFS store so
    checkouts and pushes work.  Returns the number of objects copied.
    """
    src_attributes = git_root / ".gitattributes"
    if src_attributes.is_file() and not (out_dir / ".gitattributes").exists():
        shutil.copy2(src_attributes, out_dir / ".gitattributes")
    rc, lfs_files = run_git(["lfs", "ls-files", "--name-only"], cwd=out_dir)
    if rc != 0 or not lfs_files.strip():
        return 0

    copied = 0
    for rel in lfs_files.splitlines():
        rel = rel.strip()
        if not rel:
            continue
        oid = _lfs_pointer_oid(out_dir / rel)
        if oid is None:
            continue
        src_obj = _lfs_object_path(git_root, oid)
        dst_obj = _lfs_object_path(out_dir, oid)
        if src_obj.is_file() and not dst_obj.exists():
            dst_obj.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_obj, dst_obj)
            copied += 1
    # Historical pointers may lack objects locally; allow pushes
    run_git(["config", "lfs.allowincompletepush", "true"], cwd=out_dir)
    # Materialize the working tree now that objects are present
    run_git(["lfs", "checkout"], cwd=out_dir)
    return copied


def write_overlay_objects(
    plan: HoistPlan, licenses: list[dict], warnings: list[str] | None = None
) -> list[str]:
    """Write ``overlay.json`` (and license copies) into each overlay dir with history.

    :func:`plan_hoist` already leaves out platforms without history; a
    planned overlay whose directory filter-repo still did not produce is
    skipped.  License copies skipped as unsafe are noted in *warnings*.
    Returns the overlay names written.
    """
    gem_dir = plan.out_dir / plan.gem_target
    created: list[str] = []
    for ov in plan.overlays:
        ov_root = plan.out_dir / "Overlays" / ov.dir_name
        if not ov_root.is_dir():
            continue  # platform had no history (nothing to overlay)
        meta = build_overlay_meta(
            ov_name=ov.name,
            plat=ov.platform,
            is_common=ov.platform == COMMON_PLATFORM,
            base_name=plan.base_name,
            base_version=plan.base_version,
            ov_version=plan.ov_version,
            type_token=plan.type_token,
            precedence=plan.precedence,
            origin=plan.obj_data.get("origin"),
            licenses=licenses,
            user_tags=plan.user_tags,
            common_name=plan.common_name,
        )
        write_json_file(ov_root / "overlay.json", meta)
        _note(warnings, copy_licenses(licenses, gem_dir, ov_root))
        created.append(ov.name)
    return created


def _note(warnings: list[str] | None, new: list[str]) -> None:
    """Append the messages in *new* to *warnings* (if given), without duplicates."""
    if warnings is None:
        return
    for message in new:
        if message not in warnings:
            warnings.append(message)


def build_repo_meta(
    plan: HoistPlan, licenses: list[dict], obj_json_name: str, overlay_dirs: list[str]
) -> dict:
    """Build the family ``repo.json``.

    *obj_json_name* is the object JSON file the ``children`` entry points
    at; *overlay_dirs* are the overlay dir names present in the repo.
    """
    display_family = plan.family.capitalize()
    type_token = plan.type_token
    repo_meta: dict = {
        "$schemaVersion": "2.0.0",
        "$schema": _REPO_SCHEMA,
        "repo": {
            "name": plan.repo_name,
            "display_name": f"{display_family} Family Repo",
            "description": (
                f"Family repository for the {display_family} {type_token}: "
                f"the {type_token} plus one overlay per platform, hoisted "
                f"from the engine with full git history."
            ),
            "type": "repo",
        },
    }
    origin = plan.obj_data.get("origin")
    if origin:
        repo_meta["origin"] = origin
    if licenses:
        repo_meta["licenses"] = licenses
    repo_meta["canonical_tags"] = ["Repo"]
    children: dict[str, list[str]] = {key: [] for key in _CHILD_TYPES}
    children["overlays"] = [f"Overlays/{d}/overlay.json" for d in overlay_dirs]
    # Place the object reference under the correct child type
    children[CHILD_KEYS.get(type_token, "gems")].append(f"{plan.gem_target}/{obj_json_name}")
    repo_meta["children"] = children
    repo_meta["remote"] = {key: [] for key in _CHILD_TYPES}
    return repo_meta


def write_repo_metadata(plan: HoistPlan, warnings: list[str] | None = None) -> list[str]:
    """Write overlay objects, root licenses and ``repo.json`` into the split repo.

    Licenses whose path resolves outside the gem (or outside the copy's
    destination) are skipped and noted in *warnings*.  Returns the names
    of the overlays created.
    """
    out_dir = plan.out_dir
    gem_dir = out_dir / plan.gem_target

    # Licenses travel from the gem to each overlay and to the repo root
    licenses = object_licenses(plan.obj_data, gem_dir, warnings=warnings)
    created = write_overlay_objects(plan, licenses, warnings)
    _note(warnings, copy_licenses(licenses, gem_dir, out_dir))

    # Point children at the 2.0.0 sidecar when the gem ships one — the
    # remote crawler fetches the literal child path, and the legacy
    # gem.json would resolve under its legacy short name
    type_token = plan.type_token
    obj_json_name = (
        f"{type_token}.2-0-0.json"
        if (gem_dir / f"{type_token}.2-0-0.json").is_file()
        else f"{type_token}.json"
    )
    present = [ov.dir_name for ov in plan.overlays if (out_dir / "Overlays" / ov.dir_name).is_dir()]
    write_json_file(out_dir / "repo.json", build_repo_meta(plan, licenses, obj_json_name, present))
    return created


def commit_metadata(plan: HoistPlan) -> None:
    """Commit the generated metadata on top of the split history."""
    out_dir = plan.out_dir
    rc, out = run_git(["add", "-A"], cwd=out_dir)
    if rc == 0:
        rc, out = run_git(
            [
                "commit",
                "-m",
                f"Hoist {plan.base_name} into family repo {plan.repo_name}\n\n"
                f"repo.json + per-platform overlay objects laid over the\n"
                f"filter-repo split of the {plan.type_token}'s git history.",
            ],
            cwd=out_dir,
        )
    if rc != 0:
        raise ObjectOpError(f"Metadata commit failed: {out}", "E_COMMIT_FAILED")


def execute_hoist(plan: HoistPlan, on_progress: Callable[[str], None] | None = None) -> HoistResult:
    """Carry out *plan*: clone, filter, carry LFS over, write and commit metadata.

    *on_progress* receives plain-text progress messages.  Raises
    :class:`ObjectOpError` (``E_OUTPUT_EXISTS``, ``E_CLONE_FAILED``,
    ``E_FILTER_FAILED``, ``E_COMMIT_FAILED``).  If any step after the
    existence check fails, everything this run created is removed: the
    output directory and any parent directories the clone made for it.
    """
    progress = on_progress or (lambda _msg: None)
    if plan.out_dir.exists():
        raise ObjectOpError(f"Output path already exists: {plan.out_dir}", "E_OUTPUT_EXISTS")

    created_root = _first_missing_ancestor(plan.out_dir)
    warnings: list[str] = []
    try:
        progress(f"Cloning {plan.source.git_root} (branch {plan.source.branch})...")
        clone_source(plan)
        progress("Filtering history (git filter-repo)...")
        run_filter_repo(plan)

        copied = carry_over_lfs(plan.source.git_root, plan.out_dir)
        if copied:
            progress(f"LFS: carried {copied} objects")

        created = write_repo_metadata(plan, warnings)
        commit_metadata(plan)
    except ObjectOpError as e:
        problem = _remove_created(created_root)
        if problem:
            raise ObjectOpError(f"{e} ({problem})", e.code) from e
        raise
    except BaseException:
        _remove_created(created_root)
        raise
    return HoistResult(overlays=created, lfs_objects=copied, warnings=warnings)


def _first_missing_ancestor(path: Path) -> Path:
    """The topmost of *path* and its ancestors that does not exist yet."""
    root = path
    while not root.parent.exists() and root.parent != root:
        root = root.parent
    return root


def _remove_created(root: Path) -> str | None:
    """Remove *root* (made by this run) if present; a message if that failed."""
    if not root.exists():
        return None
    try:
        rmtree_force(root)
    except OSError as e:
        return f"could not remove partial output {root}: {e}"
    return None


def register_family_repo(out_dir: Path) -> bool:
    """Register the family repo under ``local.repos``; True when newly added.

    Repo-level registration only — the resolver exposes the object and
    its overlays through ``repo.json`` children.
    """
    return bool(register_local_paths("repos", [out_dir.as_posix()]))
