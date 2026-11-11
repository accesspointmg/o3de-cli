# O3DE CLI - Package Install
# SPDX-License-Identifier: Apache-2.0 OR MIT

"""The steps behind ``o3de registry install`` (and ``o3de install``).

Finding the package in the remote catalog, choosing the match, where it
installs, and the reports the command renders.  Nothing here imports
Click or prints.  The :class:`~o3de_cli.core.store.Store` is passed in:
the command module constructs it, which is where the tests patch it.
"""

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from o3de_cli.core.hooks import HookOutcome
    from o3de_cli.core.store import RemoteObject


def find_package(store: Any, package: str) -> list["RemoteObject"]:
    """Search *store* for *package*, refreshing it from the configured remotes first.

    The refresh is cache-backed, so it is cheap when the metadata is fresh.
    """
    # Looked up at call time: the tests patch it on the store module.
    from o3de_cli.core.store import get_manifest_remote_urls

    remote_urls = get_manifest_remote_urls()
    if remote_urls:
        store.refresh_sync(remote_urls)
    return store.search(package)


def select_package(
    results: list["RemoteObject"], package: str, version: str | None
) -> "RemoteObject":
    """The exact name (and version, if given) match, else the first result."""
    for r in results:
        if r.name == package and (version is None or r.version == version):
            return r
    return results[0]


def install_target(obj: "RemoteObject", install_path: str | None) -> Path:
    """Where *obj* installs: *install_path*, else the default for its type."""
    if install_path:
        return Path(install_path)
    # Looked up at call time: the tests patch it on the paths module.
    from o3de_cli.core.paths import get_default_path_for_type

    return get_default_path_for_type(obj.object_type)


def dry_run_report(obj: "RemoteObject", target_path: Path) -> dict:
    """What ``--dry-run`` reports: the package, its target and where it comes from."""
    data: dict[str, Any] = {
        "dry_run": True,
        "package": obj.name,
        "version": obj.version,
        "type": obj.object_type.value,
        "target": str(target_path),
    }
    if obj.source_control_url:
        data["source"] = obj.source_control_url
    elif obj.download_url:
        data["download"] = obj.download_url
    return data


def install_report(
    obj: "RemoteObject",
    download_path: Path,
    registered: bool,
    outcomes: list["HookOutcome"],
) -> dict:
    """The ``--json`` payload of a completed install."""
    return {
        "package": obj.name,
        "version": obj.version,
        "path": str(download_path),
        "registered": registered,
        "hooks": [o.to_dict() for o in outcomes],
    }


def hook_warnings(outcomes: list["HookOutcome"]) -> list[str]:
    """A warning for every post_install hook that did not run cleanly."""
    return [f"post_install hook {o.status}: {o.script}" for o in outcomes if o.status != "ran"]


def failed_hooks(outcomes: list["HookOutcome"]) -> list["HookOutcome"]:
    """The hooks that ran and failed."""
    return [o for o in outcomes if o.status == "failed"]
