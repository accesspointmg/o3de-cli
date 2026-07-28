# O3DE Pilot CLI - Core Package
# SPDX-License-Identifier: Apache-2.0 OR MIT

"""
Core business logic for O3DE Pilot.

Modules:
    paths - User directory management (~/.o3de, ~/O3DE)
    models - Pydantic models for O3DE objects (Schema 2.0.0)
    workspace - Workspace engine for symlinked build directories
    store - Remote object fetching, caching, search
    resolver - Manifest resolution and dependency handling
    upgrade - Schema migration (0 → 1.0 → 2.0.0)
"""

from .hooks import (
    HookError,
    HooksEngine,
)
from .models import (
    Binary,
    Children,
    Dependencies,
    Deprecated,
    Download,
    Engine,
    Gem,
    Hooks,
    Manifest,
    O3DEObject,
    ObjectType,
    Origin,
    Overlay,
    Project,
    Release,
    Repo,
    Template,
    get_object_name,
    get_object_type,
    get_object_version,
)
from .paths import (
    get_cache_path,
    get_default_gems_path,
    get_default_layouts_path,
    get_default_path_for_type,
    get_default_workspaces_path,
    get_dot_o3de_path,
    get_manifest_path,
    get_o3de_path,
    get_resolved_manifest_path,
    initialize_user_directories,
    to_posix_path,
)
from .resolver import (
    DependencyConflict,
    ObjectNameVersion,
    ResolvedObject,
    Resolver,
    check_files_changed,
    resolve_manifest,
)
from .solver import (
    Candidate,
    CandidateStatus,
    O3DEProvider,
    O3DEReporter,
    OverlayEntry,
    Requirement,
    SolveResult,
    solve_for_workspace,
)
from .store import (
    Cache,
    IntegrityError,
    RemoteObject,
    Store,
    StoreError,
    compute_sha256,
    verify_integrity,
)
from .upgrade import (
    get_schema_version,
    needs_upgrade,
    upgrade_directory,
    upgrade_file,
    upgrade_to_latest,
)
from .workspace import (
    # Backward-compatible aliases
    Layout,
    Workspace,
    create_layout,
    create_workspace,
    detect_root_type,
)

__all__ = [
    # paths
    "get_dot_o3de_path",
    "get_o3de_path",
    "get_manifest_path",
    "get_resolved_manifest_path",
    "get_cache_path",
    "get_default_workspaces_path",
    "get_default_layouts_path",
    "initialize_user_directories",
    "to_posix_path",
    # models
    "ObjectType",
    "O3DEObject",
    "Origin",
    "Children",
    "Dependencies",
    "Deprecated",
    "Hooks",
    "Download",
    "Binary",
    "Release",
    "Engine",
    "Project",
    "Gem",
    "Template",
    "Repo",
    "Overlay",
    "Manifest",
    "get_object_type",
    "get_object_name",
    "get_object_version",
    # workspace
    "Workspace",
    "create_workspace",
    "detect_root_type",
    "Layout",
    "create_layout",
    # store
    "Cache",
    "RemoteObject",
    "Store",
    # resolver
    "Resolver",
    "ResolvedObject",
    "ObjectNameVersion",
    "resolve_manifest",
    "check_files_changed",
    # upgrade
    "get_schema_version",
    "needs_upgrade",
    "upgrade_to_latest",
    "upgrade_file",
    "upgrade_directory",
    # hooks
    "HooksEngine",
    "HookError",
    # solver
    "solve_for_workspace",
    "SolveResult",
    "Candidate",
    "CandidateStatus",
    "Requirement",
    "OverlayEntry",
    "O3DEProvider",
    "O3DEReporter",
]
