# O3DE CLI - Download and extraction safety
# SPDX-License-Identifier: Apache-2.0 OR MIT

"""Rules for fetching and unpacking objects named by remote metadata.

Remote ``repo.json`` trees are written by third parties, so every URL, ref and
archive they name is untrusted input. This module is the one place those
rules live, so the store and the gem packager enforce the same ones:

- archives come over HTTPS (redirects included) and must carry a SHA-256
  that is checked *before* anything is unpacked;
- unpacking never writes outside the destination, and never creates links;
- git clones only use https or ssh, and the URL can never be read as an
  option.

Two environment variables loosen this for development and private setups:

``O3DE_ALLOW_HTTP=1``
    accept plain-HTTP downloads (e.g. a registry on localhost)
``O3DE_ALLOW_UNVERIFIED=1``
    accept archives that publish no SHA-256 (same as ``--allow-unverified``)
"""

from __future__ import annotations

import os
import re
import stat
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

import httpx


class UnsafeSourceError(Exception):
    """A download, archive or clone was refused for safety reasons."""


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def require_secure_url(url: str) -> None:
    """Refuse plain-HTTP URLs unless O3DE_ALLOW_HTTP is set."""
    scheme = urlparse(url).scheme.lower()
    if scheme == "https":
        return
    if scheme == "http" and _env_flag("O3DE_ALLOW_HTTP"):
        return
    if scheme == "http":
        raise UnsafeSourceError(
            f"Refusing to download over plain HTTP: {url}\n"
            "Anyone on the network path could replace the file. Use an https:// URL, "
            "or set O3DE_ALLOW_HTTP=1 for a trusted local registry."
        )
    raise UnsafeSourceError(f"Unsupported download URL scheme {scheme!r}: {url}")


def _refuse_insecure_redirect(response: httpx.Response) -> None:
    if response.is_redirect:
        target = response.headers.get("location", "")
        # Relative redirects stay on the (already checked) origin.
        if urlparse(target).scheme:
            require_secure_url(target)


def download_client(timeout: float = 300) -> httpx.Client:
    """An httpx client for archive downloads.

    Follows redirects (GitHub release assets always redirect) but refuses any
    hop that leaves HTTPS.
    """
    return httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        event_hooks={"response": [_refuse_insecure_redirect]},
    )


def async_download_client(timeout: float = 300) -> httpx.AsyncClient:
    """Async counterpart of :func:`download_client`."""

    async def _hook(response: httpx.Response) -> None:
        _refuse_insecure_redirect(response)

    return httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        event_hooks={"response": [_hook]},
    )


# ---------------------------------------------------------------------------
# Checksums
# ---------------------------------------------------------------------------


def unverified_allowed(explicit: bool = False) -> bool:
    """Whether archives without a published SHA-256 may be installed."""
    return explicit or _env_flag("O3DE_ALLOW_UNVERIFIED")


def require_checksum(url: str, expected_sha256: str | None, allow_unverified: bool) -> None:
    """Refuse a remote archive that publishes no SHA-256.

    Without a checksum there is no way to tell the archive the publisher made
    from one swapped in later on the server.
    """
    if expected_sha256:
        if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
            raise UnsafeSourceError(f"Malformed SHA-256 {expected_sha256!r} for {url}")
        return
    if unverified_allowed(allow_unverified):
        return
    raise UnsafeSourceError(
        f"{url} publishes no SHA-256, so its contents cannot be verified.\n"
        "Pass --allow-unverified (or set O3DE_ALLOW_UNVERIFIED=1) to install it anyway."
    )


def verify_file_sha256(path: Path, expected_sha256: str) -> None:
    """Check *path* against *expected_sha256* before it is used."""
    from o3de_cli.core.store import IntegrityError, compute_sha256

    actual = compute_sha256(path)
    if actual.lower() != expected_sha256.lower():
        raise IntegrityError(
            f"SHA-256 mismatch for {path}:\n  expected: {expected_sha256}\n  actual:   {actual}"
        )


# ---------------------------------------------------------------------------
# Archives
# ---------------------------------------------------------------------------


def _check_member_path(name: str, dest: Path) -> None:
    pure = PurePosixPath(name.replace("\\", "/"))
    if pure.is_absolute() or re.match(r"^[A-Za-z]:", name) or ".." in pure.parts:
        raise UnsafeSourceError(f"Archive entry escapes the destination: {name!r}")
    target = (dest / Path(*pure.parts)).resolve() if pure.parts else dest.resolve()
    root = dest.resolve()
    if target != root and root not in target.parents:
        raise UnsafeSourceError(f"Archive entry escapes the destination: {name!r}")


def _zip_entry_is_link(info: zipfile.ZipInfo) -> bool:
    mode = info.external_attr >> 16
    return stat.S_ISLNK(mode)


def safe_extract(archive: Path, dest: Path) -> None:
    """Unpack a zip or tar archive into *dest*, refusing anything unsafe.

    Every entry must land inside *dest*. Links, device files and absolute or
    ``..`` paths are rejected before anything is written.
    """
    dest.mkdir(parents=True, exist_ok=True)

    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as zf:
            infos = zf.infolist()
            for info in infos:
                _check_member_path(info.filename, dest)
                if _zip_entry_is_link(info):
                    raise UnsafeSourceError(f"Archive contains a link: {info.filename!r}")
            zf.extractall(dest)
        return

    if tarfile.is_tarfile(archive):
        with tarfile.open(archive) as tf:
            members = tf.getmembers()
            for member in members:
                _check_member_path(member.name, dest)
                if not (member.isfile() or member.isdir()):
                    raise UnsafeSourceError(
                        f"Archive contains a link or special file: {member.name!r}"
                    )
            # The "data" filter (3.12, backported to 3.10.12/3.11.4) also strips
            # setuid bits and the like; the checks above already cover older
            # interpreters, which do not have it.
            kwargs: dict[str, Any] = {}
            if hasattr(tarfile, "data_filter"):
                kwargs["filter"] = "data"
            tf.extractall(dest, members=members, **kwargs)
        return

    raise UnsafeSourceError(f"Not a zip or tar archive: {archive}")


# ---------------------------------------------------------------------------
# Git
# ---------------------------------------------------------------------------

_SCP_LIKE = re.compile(r"^[\w.-]+@[\w.-]+:[^\s]+$")  # git@github.com:org/repo.git


def require_safe_git_url(url: str) -> None:
    """Allow only https and ssh clone URLs.

    Refuses anything git could interpret as an option (a leading ``-``) and
    transports that run commands or read local paths (``ext::``, ``file://``),
    plus unauthenticated ``git://`` and plain ``http://``.
    """
    if not url or url.startswith("-"):
        raise UnsafeSourceError(f"Refusing suspicious git URL: {url!r}")
    scheme = urlparse(url).scheme.lower()
    if scheme in ("https", "ssh"):
        return
    if scheme == "http" and _env_flag("O3DE_ALLOW_HTTP"):
        return
    if not scheme and _SCP_LIKE.match(url):
        return
    raise UnsafeSourceError(
        f"Refusing to clone {url!r}: only https:// and ssh URLs are accepted from remote metadata."
    )


def require_safe_git_ref(ref: str | None) -> None:
    """A branch or tag name from metadata must not look like an option."""
    if ref is not None and (ref.startswith("-") or not ref.strip()):
        raise UnsafeSourceError(f"Refusing suspicious git ref: {ref!r}")


def git_clone_env() -> dict[str, str]:
    """Environment for clones of remote objects.

    Restricts transports for the clone and any submodules, and stops git from
    blocking on a credential prompt.
    """
    env = dict(os.environ)
    allowed = "https:ssh" + (":http" if _env_flag("O3DE_ALLOW_HTTP") else "")
    env["GIT_ALLOW_PROTOCOL"] = allowed
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env
