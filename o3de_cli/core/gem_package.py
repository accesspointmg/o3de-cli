# O3DE Pilot - Gem binary package build/install support
# SPDX-License-Identifier: Apache-2.0 OR MIT

"""
Build and install individual gems as binary packages.

``object build``  — build just a gem's CMake targets inside a composed
workspace build tree (discovered via the CMake File API codemodel).

``object install`` — copy the built artifacts plus the gem's data files
into an install layout under ``~/.o3de/BuiltPackages/<name>-<version>/``
and generate a binary-flavor ``<name>Config.cmake`` that defines IMPORTED
targets mirroring the source build (consumed by the engine fork's
``cmake/Subdirectories.cmake`` when a workspace override marks the gem
``local-binary``).

IMPORTANT: the engine fork's runtime-dependency emulation
(``RuntimeDependencies_common.cmake``) uses the ``MAP_IMPORTED_CONFIG_*``
value VERBATIM as the ``IMPORTED_LOCATION_`` suffix — mapped config names
in generated configs must be UPPERCASE.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from o3de_cli.core.solver import (
    abi_compatible,
    current_platform,
    host_glibc,
    platform_matches,
)

# Configs the O3DE engine defines (CMAKE_CONFIGURATION_TYPES)
O3DE_CONFIGS = ("debug", "profile", "release")

_FILE_API_CLIENT = "client-o3de"

_RUNTIME_EXTS = {".dll", ".so", ".dylib"}
_LINK_EXTS = {".lib", ".a"}

# codemodel types that produce build work
BUILDABLE_TYPES = {
    "STATIC_LIBRARY",
    "MODULE_LIBRARY",
    "SHARED_LIBRARY",
    "OBJECT_LIBRARY",
    "EXECUTABLE",
}


@dataclass
class TargetInfo:
    """A CMake target discovered via the File API codemodel."""

    name: str
    type: str  # e.g. STATIC_LIBRARY, MODULE_LIBRARY, INTERFACE_LIBRARY
    source_dir: Path  # absolute
    artifacts: list[Path] = field(default_factory=list)  # absolute paths


def built_packages_root() -> Path:
    return Path.home() / ".o3de" / "BuiltPackages"


# ---------------------------------------------------------------------------
# CMake File API
# ---------------------------------------------------------------------------

def _api_dir(build_dir: Path) -> Path:
    return build_dir / ".cmake" / "api" / "v1"


def write_file_api_query(build_dir: Path) -> None:
    """Ensure a codemodel-v2 query exists for our client."""
    query = _api_dir(build_dir) / "query" / _FILE_API_CLIENT / "query.json"
    query.parent.mkdir(parents=True, exist_ok=True)
    query.write_text(
        json.dumps({"requests": [{"kind": "codemodel", "version": 2}]}),
        encoding="utf-8",
    )


def _find_codemodel_file(build_dir: Path) -> Optional[Path]:
    reply_dir = _api_dir(build_dir) / "reply"
    if not reply_dir.is_dir():
        return None
    indexes = sorted(reply_dir.glob("index-*.json"))
    if not indexes:
        return None
    with open(indexes[-1], encoding="utf-8") as f:
        index = json.load(f)
    responses = (
        index.get("reply", {})
        .get(_FILE_API_CLIENT, {})
        .get("query.json", {})
        .get("responses", [])
    )
    for resp in responses:
        if resp.get("kind") == "codemodel":
            return reply_dir / resp["jsonFile"]
    return None


def _cached_source_dir(build_dir: Path) -> Optional[Path]:
    """Read CMAKE_HOME_DIRECTORY from an existing CMakeCache.txt."""
    cache = build_dir / "CMakeCache.txt"
    try:
        for line in cache.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("CMAKE_HOME_DIRECTORY:"):
                return Path(line.split("=", 1)[1].strip())
    except OSError:
        pass
    return None


def ensure_codemodel(
    build_dir: Path,
    on_progress: Optional[Callable[[str], None]] = None,
    force: bool = False,
) -> Optional[Path]:
    """Return the codemodel reply path, running a CMake configure to
    produce one if the query was not present during the last configure
    (or unconditionally with *force*, to refresh a stale reply).

    Requires an already-configured build dir (CMakeCache.txt present).
    """
    if not (build_dir / "CMakeCache.txt").exists():
        return None

    write_file_api_query(build_dir)
    codemodel = None if force else _find_codemodel_file(build_dir)
    if codemodel is not None:
        return codemodel

    if on_progress:
        on_progress("Regenerating CMake File API reply (one-time reconfigure)...")
    source_dir = _cached_source_dir(build_dir)
    cmd = ["cmake", "-B", str(build_dir)]
    if source_dir:
        cmd += ["-S", str(source_dir)]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(build_dir),
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"CMake configure failed while generating File API reply:\n"
            f"{result.stdout[-2000:]}\n{result.stderr[-2000:]}"
        )
    return _find_codemodel_file(build_dir)


def load_codemodel_targets(codemodel_file: Path, config: str) -> list[TargetInfo]:
    """Load all targets for *config* from a codemodel-v2 reply."""
    reply_dir = codemodel_file.parent
    with open(codemodel_file, encoding="utf-8") as f:
        cm = json.load(f)

    source_root = Path(cm["paths"]["source"])
    build_root = Path(cm["paths"]["build"])

    configurations = cm.get("configurations", [])
    config_entry = None
    for entry in configurations:
        if entry.get("name", "").lower() == config.lower():
            config_entry = entry
            break
    if config_entry is None and configurations:
        config_entry = configurations[0]
    if config_entry is None:
        return []

    targets: list[TargetInfo] = []
    for tref in config_entry.get("targets", []):
        with open(reply_dir / tref["jsonFile"], encoding="utf-8") as f:
            tj = json.load(f)
        src = Path(tj.get("paths", {}).get("source", "."))
        if not src.is_absolute():
            src = (source_root / src).resolve()
        artifacts = []
        for art in tj.get("artifacts", []):
            ap = Path(art["path"])
            if not ap.is_absolute():
                ap = (build_root / ap).resolve()
            artifacts.append(ap)
        targets.append(
            TargetInfo(
                name=tj["name"],
                type=tj.get("type", ""),
                source_dir=src,
                artifacts=artifacts,
            )
        )
    return targets


def filter_gem_targets(targets: list[TargetInfo], gem_dir: Path) -> list[TargetInfo]:
    """Targets whose source directory lives inside *gem_dir*."""
    gem_dir = gem_dir.resolve()
    result = []
    for t in targets:
        try:
            t.source_dir.relative_to(gem_dir)
        except ValueError:
            continue
        result.append(t)
    return result


# ---------------------------------------------------------------------------
# Gem metadata + alias discovery
# ---------------------------------------------------------------------------

def read_gem_legacy_name(gem_dir: Path) -> str:
    """The legacy ``gem_name`` used as ``${gem_name}`` in the gem's CMake."""
    gem_json = gem_dir / "gem.json"
    try:
        with open(gem_json, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return gem_dir.name
    name = data.get("gem_name")
    if isinstance(name, str) and name:
        return name
    hdr = data.get("gem")
    if isinstance(hdr, dict) and hdr.get("name"):
        return str(hdr["name"])
    return gem_dir.name


_ALIAS_RE = re.compile(
    r"o3de_create_alias\s*\(\s*NAME\s+(\S+)\s+NAMESPACE\s+\S+\s+TARGETS\s+([^)\s]+)",
    re.IGNORECASE,
)


def parse_gem_aliases(gem_dir: Path, gem_name: str) -> dict[str, str]:
    """Parse ``o3de_create_alias`` calls from the gem's CMake files.

    Returns {alias_name: target_name} with ``${gem_name}`` substituted and
    ``Gem::`` namespaces stripped, e.g. {"Stars.Clients": "Stars"}.
    """
    aliases: dict[str, str] = {}
    cmake_files = list(gem_dir.rglob("CMakeLists.txt")) + list(gem_dir.rglob("*.cmake"))
    for path in cmake_files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        text = text.replace("${gem_name}", gem_name)
        for m in _ALIAS_RE.finditer(text):
            alias = m.group(1)
            target = m.group(2).split("::")[-1]
            aliases[alias] = target
    return aliases


# ---------------------------------------------------------------------------
# Config.cmake generation
# ---------------------------------------------------------------------------

def generate_config_cmake(
    canonical_name: str,
    targets: list[TargetInfo],
    aliases: dict[str, str],
    config: str,
    has_include: bool,
) -> str:
    """Generate the binary-flavor package config defining IMPORTED targets."""
    uconf = config.upper()
    other_configs = [c.upper() for c in O3DE_CONFIGS if c.lower() != config.lower()]

    interface_targets = [t for t in targets if t.type == "INTERFACE_LIBRARY"]
    static_targets = [t for t in targets if t.type == "STATIC_LIBRARY"]
    module_targets = [
        t for t in targets if t.type in ("MODULE_LIBRARY", "SHARED_LIBRARY")
    ]

    guard = module_targets[0].name if module_targets else (
        static_targets[0].name if static_targets else canonical_name
    )

    def _runtime_artifact(t: TargetInfo) -> Optional[str]:
        for a in t.artifacts:
            if a.suffix.lower() in _RUNTIME_EXTS:
                return a.name
        return None

    def _link_artifact(t: TargetInfo) -> Optional[str]:
        for a in t.artifacts:
            if a.suffix.lower() in _LINK_EXTS:
                return a.name
        return None

    lines: list[str] = [
        "#",
        "# Copyright (c) Contributors to the Open 3D Engine Project.",
        "# SPDX-License-Identifier: Apache-2.0 OR MIT",
        "#",
        f"# BINARY PACKAGE config for {canonical_name} (prebuilt install layout).",
        "# GENERATED by `o3de object install` — do not edit.",
        "#",
        "# Defines IMPORTED targets mirroring the source build so dependents,",
        "# delayed-load setreg generation, and runtime-dependency copying work",
        "# unchanged.",
        "",
        "# Idempotence: targets may only be defined once per build tree",
        f"if(TARGET {guard})",
        f"    set({canonical_name}_FOUND TRUE)",
        "    return()",
        "endif()",
        "",
        'set(_pkg_root "${CMAKE_CURRENT_LIST_DIR}")',
        'set(_pkg_platform "${PAL_PLATFORM_NAME}")',
        "if(NOT _pkg_platform)",
        f'    set(_pkg_platform "{current_platform()}")',
        "endif()",
        "",
        f"# Only {config} binaries are packaged; map other configs onto {config}.",
        f'set(_pkg_bin "${{_pkg_root}}/bin/${{_pkg_platform}}/{config}")',
        f'set(_pkg_lib "${{_pkg_root}}/lib/${{_pkg_platform}}/{config}")',
        "",
    ]

    map_lines = [
        f"    MAP_IMPORTED_CONFIG_{oc} {uconf}" for oc in other_configs
    ]

    for t in interface_targets:
        lines += [
            "o3de_add_target(",
            f"    NAME {t.name} HEADERONLY IMPORTED",
            "    NAMESPACE Gem",
        ]
        if has_include:
            lines += [
                "    INCLUDE_DIRECTORIES",
                "        INTERFACE",
                "            ${_pkg_root}/Include",
            ]
        lines += [")", ""]

    for t in static_targets:
        lib = _link_artifact(t)
        if not lib:
            continue
        lines += [
            "o3de_add_target(",
            f"    NAME {t.name} STATIC IMPORTED",
            "    NAMESPACE Gem",
        ]
        if has_include:
            lines += [
                "    INCLUDE_DIRECTORIES",
                "        INTERFACE",
                "            ${_pkg_root}/Include",
            ]
        lines += [
            ")",
            f"set_target_properties({t.name} PROPERTIES",
            f"    IMPORTED_CONFIGURATIONS {uconf}",
            f'    IMPORTED_LOCATION "${{_pkg_lib}}/{lib}"',
            f'    IMPORTED_LOCATION_{uconf} "${{_pkg_lib}}/{lib}"',
            *map_lines,
            ")",
            "",
        ]

    for t in module_targets:
        dll = _runtime_artifact(t)
        if not dll:
            continue
        implib = _link_artifact(t) if t.type == "SHARED_LIBRARY" else None
        kind = "SHARED" if t.type == "SHARED_LIBRARY" else "MODULE"
        lines += [
            "o3de_add_target(",
            f"    NAME {t.name} {kind} IMPORTED",
            "    NAMESPACE Gem",
            "    TARGET_PROPERTIES",
            "        GEM_MODULE TRUE",
            ")",
            f"set_target_properties({t.name} PROPERTIES",
            f"    IMPORTED_CONFIGURATIONS {uconf}",
            f'    IMPORTED_LOCATION "${{_pkg_bin}}/{dll}"',
            f'    IMPORTED_LOCATION_{uconf} "${{_pkg_bin}}/{dll}"',
        ]
        if implib:
            lines += [
                f'    IMPORTED_IMPLIB "${{_pkg_lib}}/{implib}"',
                f'    IMPORTED_IMPLIB_{uconf} "${{_pkg_lib}}/{implib}"',
            ]
        lines += [*map_lines, ")", ""]

    if aliases:
        lines.append("# Variant aliases (what o3de_enable_gems looks up per project)")
        defined = {t.name for t in targets}
        for alias, target in sorted(aliases.items()):
            if target not in defined:
                continue
            lines.append(
                f"o3de_create_alias(NAME {alias} NAMESPACE Gem TARGETS Gem::{target})"
            )
        lines.append("")

    lines += [
        f"set({canonical_name}_FOUND TRUE)",
        f'message(STATUS "Using PREBUILT gem {canonical_name} from ${{_pkg_root}}")',
        "",
    ]
    return "\n".join(lines)


def generate_config_version_cmake(canonical_name: str, version: str) -> str:
    return f"""#
# Copyright (c) Contributors to the Open 3D Engine Project.
# SPDX-License-Identifier: Apache-2.0 OR MIT
#
# Version check for the PREBUILT {canonical_name} package.
# GENERATED by `o3de object install` — do not edit.
# Reads the version directly from the packaged gem.json (this package is
# self-contained — no manifest global properties required).

set(PACKAGE_VERSION_COMPATIBLE FALSE)
set(PACKAGE_VERSION_EXACT FALSE)

file(READ "${{CMAKE_CURRENT_LIST_DIR}}/gem.json" _pkg_gem_json)
string(JSON PACKAGE_VERSION ERROR_VARIABLE _pkg_version_error GET "${{_pkg_gem_json}}" version)
if(_pkg_version_error)
    set(PACKAGE_VERSION "{version}")
endif()

if(NOT PACKAGE_FIND_VERSION)
    set(PACKAGE_VERSION_COMPATIBLE TRUE)
    return()
endif()

if(PACKAGE_FIND_VERSION_EXACT)
    if(PACKAGE_VERSION VERSION_EQUAL PACKAGE_FIND_VERSION)
        set(PACKAGE_VERSION_COMPATIBLE TRUE)
        set(PACKAGE_VERSION_EXACT TRUE)
    endif()
elseif(PACKAGE_VERSION VERSION_GREATER_EQUAL PACKAGE_FIND_VERSION)
    set(PACKAGE_VERSION_COMPATIBLE TRUE)
    if(PACKAGE_VERSION VERSION_EQUAL PACKAGE_FIND_VERSION)
        set(PACKAGE_VERSION_EXACT TRUE)
    endif()
endif()
"""


# ---------------------------------------------------------------------------
# Package assembly
# ---------------------------------------------------------------------------

_DATA_DIRS = ("Assets", "Registry")
_INCLUDE_CANDIDATES = ("Include", "Code/Include", "include", "Code/include")


def install_gem_package(
    gem_dir: Path,
    canonical_name: str,
    version: str,
    targets: list[TargetInfo],
    config: str,
    force: bool = False,
    on_progress: Optional[Callable[[str], None]] = None,
) -> Path:
    """Assemble the install layout + generated package configs.

    Returns the package directory.
    """
    def progress(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    dest = built_packages_root() / f"{canonical_name}-{version}"
    if dest.exists():
        if not force:
            raise FileExistsError(str(dest))
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    platform = current_platform()
    bin_dir = dest / "bin" / platform / config
    lib_dir = dest / "lib" / platform / config

    # -- binaries -----------------------------------------------------------
    copied: list[str] = []
    for t in targets:
        for art in t.artifacts:
            ext = art.suffix.lower()
            if ext in _RUNTIME_EXTS:
                out = bin_dir
            elif ext in _LINK_EXTS:
                out = lib_dir
            else:
                continue
            if not art.exists():
                raise FileNotFoundError(
                    f"Artifact missing (build the gem first): {art}"
                )
            out.mkdir(parents=True, exist_ok=True)
            shutil.copy2(art, out / art.name)
            copied.append(art.name)
    progress(f"Copied {len(copied)} binaries: {', '.join(sorted(copied))}")

    # -- metadata + data ----------------------------------------------------
    for pattern in ("gem.json", "gem.*.json", "preview.png"):
        for f in gem_dir.glob(pattern):
            shutil.copy2(f, dest / f.name)
    for d in _DATA_DIRS:
        src = gem_dir / d
        if src.is_dir():
            shutil.copytree(src, dest / d)
            progress(f"Copied {d}/")

    has_include = False
    for cand in _INCLUDE_CANDIDATES:
        src = gem_dir / cand
        if src.is_dir():
            shutil.copytree(src, dest / "Include")
            has_include = True
            progress(f"Copied {cand}/ -> Include/")
            break

    # -- generated package configs ------------------------------------------
    gem_name = read_gem_legacy_name(gem_dir)
    aliases = parse_gem_aliases(gem_dir, gem_name)
    config_text = generate_config_cmake(
        canonical_name, targets, aliases, config, has_include,
    )
    (dest / f"{canonical_name}Config.cmake").write_text(
        config_text, encoding="utf-8",
    )
    (dest / f"{canonical_name}ConfigVersion.cmake").write_text(
        generate_config_version_cmake(canonical_name, version), encoding="utf-8",
    )
    progress(f"Generated {canonical_name}Config.cmake")
    return dest


# ---------------------------------------------------------------------------
# Remote binary download
# ---------------------------------------------------------------------------

def find_release_binary(
    data: dict, version: str, platform: str
) -> Optional[dict]:
    """Find the release binary entry for *version* and *platform*.

    Prefers the release whose name matches *version*; falls back to any
    release carrying a platform-matching binary (mirrors
    ``solver.has_remote_binary``).  Among compatible entries, an exact
    ``<OS>.<ARCH>`` token beats a legacy bare-OS entry, and the highest
    ABI-compatible glibc floor wins.
    """
    releases = data.get("releases", []) or []

    def _glibc_floor(b: dict) -> tuple[int, ...]:
        abi = b.get("abi") or {}
        try:
            return tuple(int(x) for x in str(abi.get("glibc", "0")).split("."))
        except ValueError:
            return (0,)

    def _platform_binary(release: dict) -> Optional[dict]:
        matches = [
            b for b in release.get("binaries", []) or []
            if platform_matches(str(b.get("platform", "")), platform)
            and abi_compatible(b)
        ]
        if not matches:
            return None
        return max(
            matches,
            key=lambda b: (
                str(b.get("platform", "")).lower() == platform.lower(),
                _glibc_floor(b),
            ),
        )

    for release in releases:
        if release.get("name") == version:
            binary = _platform_binary(release)
            if binary:
                return binary
    for release in releases:
        binary = _platform_binary(release)
        if binary:
            return binary
    return None


def _has_package_config(root: Path) -> bool:
    try:
        next(root.rglob("*Config.cmake"))
        return True
    except StopIteration:
        return False


def _fetch_release_archive(
    url: str,
    expected_sha256: Optional[str],
    cache_name: str,
    progress: Callable[[str], None],
) -> Path:
    """Obtain a release archive (local path, file://, or http(s)) and
    verify its SHA-256.  Returns the local archive path."""
    from urllib.parse import urlparse
    from urllib.request import url2pathname

    from o3de_cli.core.paths import get_download_path
    from o3de_cli.core.store import IntegrityError, compute_sha256

    parsed = urlparse(url)
    if parsed.scheme == "file":
        archive_path = Path(url2pathname(parsed.path))
    elif parsed.scheme in ("http", "https"):
        import httpx

        download_dir = get_download_path()
        download_dir.mkdir(parents=True, exist_ok=True)
        archive_path = download_dir / cache_name
        progress(f"Downloading {url}")
        with httpx.Client(timeout=300, follow_redirects=True) as client:
            with client.stream("GET", url) as response:
                response.raise_for_status()
                with open(archive_path, "wb") as f:
                    for chunk in response.iter_bytes():
                        f.write(chunk)
    else:
        archive_path = Path(url)
    if not archive_path.is_file():
        raise FileNotFoundError(f"Release archive not found: {archive_path}")

    if expected_sha256:
        actual = compute_sha256(archive_path)
        if actual != expected_sha256:
            raise IntegrityError(
                f"SHA-256 mismatch for {archive_path}:\n"
                f"  expected: {expected_sha256}\n"
                f"  actual:   {actual}"
            )
        progress("SHA-256 verified")
    return archive_path


def download_remote_binary(
    name: str,
    version: str,
    data: dict,
    platform: Optional[str] = None,
    force: bool = False,
    on_progress: Optional[Callable[[str], None]] = None,
) -> Path:
    """Download + extract a remote binary package into BuiltPackages.

    *data* is the object's JSON (carrying ``releases[].binaries``).
    The archive must contain an install layout (``<name>Config.cmake``
    at its root or below).  Supports ``file://`` URLs and plain local
    paths in addition to http(s).

    Returns the package directory.
    """
    import zipfile

    def progress(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    platform = platform or current_platform()
    dest = built_packages_root() / f"{name}-{version}"
    if dest.is_dir() and _has_package_config(dest) and not force:
        progress(f"Already installed: {dest}")
        return dest

    binary = find_release_binary(data, version, platform)
    if binary is None:
        raise LookupError(
            f"No release binary advertised for {name}@{version} "
            f"on platform {platform}"
        )
    url = binary.get("binary", "")
    expected_sha256 = binary.get("sha256")
    if not url:
        raise LookupError(f"Release binary entry for {name}@{version} has no URL")

    archive_path = _fetch_release_archive(
        url, expected_sha256, f"{name}-{version}-{platform}.zip", progress,
    )

    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    progress(f"Extracting to {dest}")
    with zipfile.ZipFile(archive_path) as zf:
        zf.extractall(dest)

    if not _has_package_config(dest):
        shutil.rmtree(dest)
        raise ValueError(
            f"Archive for {name}@{version} does not contain a package "
            f"config (*Config.cmake) — not a binary install layout"
        )
    return dest


# ---------------------------------------------------------------------------
# Remote source (code release) download
# ---------------------------------------------------------------------------

def find_release_source(data: dict, version: str) -> Optional[dict]:
    """Find the release source-download entry for *version*.

    Prefers the release whose name matches *version*; falls back to any
    release carrying a source download.  Returns the ``downloads[]``
    entry ({source, source_sha256, ...}) or None.
    """
    releases = data.get("releases", []) or []

    def _source(release: dict) -> Optional[dict]:
        for d in release.get("downloads", []) or []:
            if d.get("source"):
                return d
        return None

    for release in releases:
        if release.get("name") == version:
            entry = _source(release)
            if entry:
                return entry
    for release in releases:
        entry = _source(release)
        if entry:
            return entry
    return None


def download_remote_source(
    name: str,
    version: str,
    data: dict,
    force: bool = False,
    on_progress: Optional[Callable[[str], None]] = None,
) -> Path:
    """Download + extract a code (source) release into the default gems path.

    *data* is the object's JSON (carrying ``releases[].downloads``).
    The archive (zip or tar.gz) must contain an object json (gem.json)
    at its root.  Returns the extracted object directory, ready to be
    registered as a plain source object.
    """
    import tarfile
    import zipfile

    from o3de_cli.core.paths import get_default_gems_path

    def progress(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    dest = get_default_gems_path() / f"{name}-{version}"
    if dest.is_dir() and (dest / "gem.json").is_file() and not force:
        progress(f"Already downloaded: {dest}")
        return dest

    entry = find_release_source(data, version)
    if entry is None:
        raise LookupError(
            f"No release source archive advertised for {name}@{version}"
        )
    url = entry.get("source", "")
    expected_sha256 = entry.get("source_sha256")
    if not url:
        raise LookupError(f"Release source entry for {name}@{version} has no URL")

    suffix = ".tar.gz" if url.endswith((".tar.gz", ".tgz")) else ".zip"
    archive_path = _fetch_release_archive(
        url, expected_sha256, f"{name}-{version}-Source{suffix}", progress,
    )

    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    progress(f"Extracting to {dest}")
    if archive_path.name.endswith((".tar.gz", ".tgz")):
        with tarfile.open(archive_path, "r:gz") as tf:
            tf.extractall(dest, filter="data")
    else:
        with zipfile.ZipFile(archive_path) as zf:
            zf.extractall(dest)

    if not (dest / "gem.json").is_file():
        shutil.rmtree(dest)
        raise ValueError(
            f"Archive for {name}@{version} does not contain gem.json at "
            f"its root — not a source release layout"
        )
    return dest


# ---------------------------------------------------------------------------
# Release packaging (zip + sha256 + manifest entry)
# ---------------------------------------------------------------------------

def package_gem_archive(
    package_dir: Path,
    canonical_name: str,
    version: str,
    output_dir: Path,
    platform: Optional[str] = None,
) -> tuple[Path, str]:
    """Zip an installed package layout into a distributable release archive.

    Produces ``<output_dir>/<name>-<version>-<Platform>.zip`` with the
    layout at the archive root (the exact shape ``download_remote_binary``
    consumes).  Returns (zip_path, sha256).
    """
    import zipfile

    platform = platform or current_platform()
    output_dir.mkdir(parents=True, exist_ok=True)
    zip_path = output_dir / f"{canonical_name}-{version}-{platform}.zip"
    if zip_path.exists():
        zip_path.unlink()

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(package_dir.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(package_dir).as_posix())

    from o3de_cli.core.store import compute_sha256

    return zip_path, compute_sha256(zip_path)


# Directory names excluded from source (code-release) archives at any depth
_SOURCE_EXCLUDE_DIRS = {".git", "build", "Cache", "__pycache__", "user"}


def package_gem_source_archive(
    gem_dir: Path,
    canonical_name: str,
    version: str,
    output_dir: Path,
    fmt: str = "zip",
) -> tuple[Path, str]:
    """Archive a gem's source tree into a distributable code-release.

    Produces ``<output_dir>/<name>-<version>-Source.zip`` (or ``.tar.gz``)
    with the gem's files at the archive root.  Platform-independent — no
    build required.  Returns (archive_path, sha256).
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    def files():
        for f in sorted(gem_dir.rglob("*")):
            if not f.is_file() or f.suffix == ".pyc":
                continue
            rel = f.relative_to(gem_dir)
            if any(part in _SOURCE_EXCLUDE_DIRS for part in rel.parts):
                continue
            yield f, rel

    if fmt == "tar.gz":
        import tarfile

        archive_path = output_dir / f"{canonical_name}-{version}-Source.tar.gz"
        if archive_path.exists():
            archive_path.unlink()
        with tarfile.open(archive_path, "w:gz") as tf:
            for f, rel in files():
                tf.add(f, rel.as_posix())
    else:
        import zipfile

        archive_path = output_dir / f"{canonical_name}-{version}-Source.zip"
        if archive_path.exists():
            archive_path.unlink()
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f, rel in files():
                zf.write(f, rel.as_posix())

    from o3de_cli.core.store import compute_sha256

    return archive_path, compute_sha256(archive_path)


def update_release_manifest_source(
    gem_dir: Path,
    version: str,
    url: str,
    sha256: str,
) -> Path:
    """Record a release source (code) archive in the gem's JSON manifest.

    Adds (or replaces) the source download in the ``releases[]`` entry
    named *version* under ``downloads[]`` ({source, source_sha256}).
    Prefers the versioned ``gem.2-0-0.json`` and writes in place so file
    links stay intact.  Returns the path written.
    """
    manifest = gem_dir / "gem.2-0-0.json"
    if not manifest.exists():
        manifest = gem_dir / "gem.json"
    if not manifest.exists():
        raise FileNotFoundError(f"No gem manifest found in {gem_dir}")

    with open(manifest, encoding="utf-8") as f:
        data = json.load(f)

    releases = data.setdefault("releases", [])
    release = next((r for r in releases if r.get("name") == version), None)
    if release is None:
        release = {"name": version}
        releases.append(release)
    downloads = release.setdefault("downloads", [])
    downloads[:] = [d for d in downloads if not d.get("source")]
    downloads.append({"source": url, "source_sha256": sha256})

    # In-place write preserves hard/file links into composed workspaces
    with open(manifest, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)
        f.write("\n")
    return manifest


def update_release_manifest(
    gem_dir: Path,
    version: str,
    url: str,
    sha256: str,
    platform: Optional[str] = None,
) -> Path:
    """Record a release binary in the gem's JSON manifest.

    Adds (or replaces) the ``releases[]`` entry named *version* with a
    binary for *platform*.  Prefers the versioned ``gem.2-0-0.json`` (the
    file the resolver reads) and writes in place so file links stay intact.
    Returns the path written.
    """
    platform = platform or current_platform()
    manifest = gem_dir / "gem.2-0-0.json"
    if not manifest.exists():
        manifest = gem_dir / "gem.json"
    if not manifest.exists():
        raise FileNotFoundError(f"No gem manifest found in {gem_dir}")

    with open(manifest, encoding="utf-8") as f:
        data = json.load(f)

    releases = data.setdefault("releases", [])
    release = next((r for r in releases if r.get("name") == version), None)
    if release is None:
        release = {"name": version, "binaries": []}
        releases.append(release)
    binaries = release.setdefault("binaries", [])
    # Replace any entry this host's platform token would match (including
    # legacy bare-OS entries being upgraded to <OS>.<ARCH> tokens)
    binaries[:] = [
        b for b in binaries
        if not platform_matches(str(b.get("platform", "")), platform)
    ]
    entry: dict = {"platform": platform, "binary": url, "sha256": sha256}
    glibc = host_glibc()
    if glibc is not None and platform.lower().startswith("linux"):
        entry["abi"] = {"glibc": f"{glibc[0]}.{glibc[1]}"}
    binaries.append(entry)

    # In-place write preserves hard/file links into composed workspaces
    with open(manifest, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)
        f.write("\n")
    return manifest
