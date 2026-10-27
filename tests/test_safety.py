# O3DE CLI - Download, extraction, clone and hook safety tests
# SPDX-License-Identifier: Apache-2.0 OR MIT

"""Everything a remote object names is untrusted; these pin down the rules."""

from __future__ import annotations

import hashlib
import io
import json
import stat
import sys
import tarfile
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from click.testing import CliRunner

from o3de_cli.core.safety import (
    UnsafeSourceError,
    download_client,
    require_checksum,
    require_safe_git_ref,
    require_safe_git_url,
    require_secure_url,
    safe_extract,
)

# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class TestSecureUrl:
    def test_https_ok(self):
        require_secure_url("https://example.com/a.zip")

    def test_http_refused(self):
        with pytest.raises(UnsafeSourceError, match="plain HTTP"):
            require_secure_url("http://example.com/a.zip")

    def test_http_allowed_by_env(self, monkeypatch):
        monkeypatch.setenv("O3DE_ALLOW_HTTP", "1")
        require_secure_url("http://localhost:8000/a.zip")

    @pytest.mark.parametrize("url", ["ftp://example.com/a.zip", "file:///etc/passwd", "a.zip"])
    def test_other_schemes_refused(self, url):
        with pytest.raises(UnsafeSourceError):
            require_secure_url(url)

    def test_redirect_to_http_refused(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.scheme == "https":
                return httpx.Response(302, headers={"location": "http://evil.example/a.zip"})
            return httpx.Response(200, content=b"swapped")

        client = download_client()
        client._transport = httpx.MockTransport(handler)
        with pytest.raises(UnsafeSourceError, match="plain HTTP"):
            client.get("https://example.com/a.zip")

    def test_redirect_within_https_followed(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/a.zip":
                return httpx.Response(302, headers={"location": "https://cdn.example/real.zip"})
            return httpx.Response(200, content=b"ok")

        client = download_client()
        client._transport = httpx.MockTransport(handler)
        assert client.get("https://example.com/a.zip").content == b"ok"


class TestChecksumRequired:
    def test_present_ok(self):
        require_checksum("https://x/a.zip", "a" * 64, allow_unverified=False)

    def test_missing_refused(self):
        with pytest.raises(UnsafeSourceError, match="--allow-unverified"):
            require_checksum("https://x/a.zip", None, allow_unverified=False)

    def test_missing_allowed_explicitly(self):
        require_checksum("https://x/a.zip", None, allow_unverified=True)

    def test_missing_allowed_by_env(self, monkeypatch):
        monkeypatch.setenv("O3DE_ALLOW_UNVERIFIED", "1")
        require_checksum("https://x/a.zip", None, allow_unverified=False)

    def test_malformed_refused(self):
        with pytest.raises(UnsafeSourceError, match="Malformed"):
            require_checksum("https://x/a.zip", "abc123", allow_unverified=False)


# ---------------------------------------------------------------------------
# Archives
# ---------------------------------------------------------------------------


def _zip(tmp_path: Path, entries: dict[str, bytes], links: tuple[str, ...] = ()) -> Path:
    path = tmp_path / "a.zip"
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
        for name in links:
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(info, "/etc/passwd")
    return path


def _tar(tmp_path: Path, entries: dict[str, bytes], symlink: str | None = None) -> Path:
    path = tmp_path / "a.tar.gz"
    with tarfile.open(path, "w:gz") as tf:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        if symlink:
            info = tarfile.TarInfo(symlink)
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            tf.addfile(info)
    return path


class TestSafeExtract:
    def test_zip_ok(self, tmp_path):
        dest = tmp_path / "out"
        safe_extract(_zip(tmp_path, {"gem.json": b"{}", "Code/a.cpp": b"x"}), dest)
        assert (dest / "gem.json").is_file()
        assert (dest / "Code" / "a.cpp").is_file()

    @pytest.mark.parametrize("name", ["../evil.txt", "a/../../evil.txt", "/abs.txt", "C:/x.txt"])
    def test_zip_escape_refused_and_nothing_written(self, tmp_path, name):
        dest = tmp_path / "out"
        archive = _zip(tmp_path, {"ok.txt": b"fine", name: b"bad"})
        with pytest.raises(UnsafeSourceError, match="escapes"):
            safe_extract(archive, dest)
        assert not (dest / "ok.txt").exists()
        assert not (tmp_path / "evil.txt").exists()

    def test_zip_symlink_refused(self, tmp_path):
        with pytest.raises(UnsafeSourceError, match="link"):
            safe_extract(_zip(tmp_path, {"a.txt": b"x"}, links=("link",)), tmp_path / "out")

    def test_tar_ok(self, tmp_path):
        dest = tmp_path / "out"
        safe_extract(_tar(tmp_path, {"gem.json": b"{}"}), dest)
        assert (dest / "gem.json").read_bytes() == b"{}"

    def test_tar_escape_refused(self, tmp_path):
        with pytest.raises(UnsafeSourceError, match="escapes"):
            safe_extract(_tar(tmp_path, {"../evil.txt": b"bad"}), tmp_path / "out")
        assert not (tmp_path / "evil.txt").exists()

    def test_tar_symlink_refused(self, tmp_path):
        with pytest.raises(UnsafeSourceError, match="link"):
            safe_extract(_tar(tmp_path, {"a": b"x"}, symlink="passwd"), tmp_path / "out")

    # 3.12+ warns when extracting without a filter; that is the case simulated here
    @pytest.mark.filterwarnings("ignore::DeprecationWarning")
    def test_tar_works_without_data_filter(self, tmp_path, monkeypatch):
        """Python < 3.10.12 / 3.11.4 has no tarfile.data_filter."""
        monkeypatch.delattr(tarfile, "data_filter", raising=False)
        dest = tmp_path / "out"
        safe_extract(_tar(tmp_path, {"gem.json": b"{}"}), dest)
        assert (dest / "gem.json").is_file()

    def test_not_an_archive(self, tmp_path):
        bogus = tmp_path / "x.zip"
        bogus.write_bytes(b"not an archive")
        with pytest.raises(UnsafeSourceError, match="Not a zip or tar"):
            safe_extract(bogus, tmp_path / "out")


# ---------------------------------------------------------------------------
# Git
# ---------------------------------------------------------------------------


class TestGitUrls:
    @pytest.mark.parametrize(
        "url",
        [
            "https://github.com/o3de/o3de.git",
            "ssh://git@github.com/o3de/o3de.git",
            "git@github.com:o3de/o3de.git",
        ],
    )
    def test_allowed(self, url):
        require_safe_git_url(url)

    @pytest.mark.parametrize(
        "url",
        [
            "--upload-pack=touch /tmp/pwned",
            "-oProxyCommand=evil",
            "ext::sh -c touch% /tmp/pwned",
            "file:///home/user/secret-repo",
            "git://github.com/o3de/o3de.git",
            "http://github.com/o3de/o3de.git",
            "/local/path",
            "",
        ],
    )
    def test_refused(self, url):
        with pytest.raises(UnsafeSourceError):
            require_safe_git_url(url)

    def test_ref_like_an_option_refused(self):
        with pytest.raises(UnsafeSourceError):
            require_safe_git_ref("--upload-pack=evil")
        require_safe_git_ref("v1.2.0")
        require_safe_git_ref(None)


# ---------------------------------------------------------------------------
# Store and gem packager enforce the rules before touching the network
# ---------------------------------------------------------------------------


def _store(tmp_path):
    from o3de_cli.core.store import Cache, Store

    store = Store.__new__(Store)
    store.cache = Cache(tmp_path / "cache")
    store.timeout = 5
    store.objects = {}
    store._visited_urls = set()
    return store


def _remote(**kw):
    from o3de_cli.core.models import ObjectType
    from o3de_cli.core.store import RemoteObject

    return RemoteObject(
        url="https://example.com/gem.json",
        object_type=ObjectType.GEM,
        name="org.test.gem.x",
        version="1.0.0",
        **kw,
    )


class TestStoreArchives:
    def test_missing_checksum_refused_before_download(self, tmp_path):
        store = _store(tmp_path)
        with patch("o3de_cli.core.safety.httpx.Client") as client:
            with pytest.raises(UnsafeSourceError, match="no SHA-256"):
                store.download_sync(_remote(download_url="https://example.com/x.zip"), tmp_path)
        client.assert_not_called()

    def test_http_archive_refused(self, tmp_path):
        store = _store(tmp_path)
        with pytest.raises(UnsafeSourceError, match="plain HTTP"):
            store.download_sync(
                _remote(download_url="http://example.com/x.zip", source_sha256="a" * 64),
                tmp_path,
            )

    def test_mismatched_archive_not_unpacked(self, tmp_path):
        from o3de_cli.core.store import IntegrityError

        zip_bytes = _zip(tmp_path, {"gem.json": b"{}"}).read_bytes()
        response = MagicMock()
        response.headers = {}
        response.iter_bytes.return_value = [zip_bytes]
        response.__enter__ = MagicMock(return_value=response)
        response.__exit__ = MagicMock(return_value=False)
        client = MagicMock()
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)
        client.stream.return_value = response

        store = _store(tmp_path)
        obj = _remote(download_url="https://example.com/x.zip", source_sha256="b" * 64)
        target = tmp_path / "install"
        with (
            patch("o3de_cli.core.safety.httpx.Client", return_value=client),
            patch("o3de_cli.core.store.get_download_path", return_value=tmp_path / "dl"),
        ):
            with pytest.raises(IntegrityError):
                store.download_sync(obj, target)
        assert not list(target.rglob("gem.json"))
        assert not list((tmp_path / "dl").iterdir())


class TestGemPackageArchives:
    def test_remote_without_checksum_refused(self, tmp_path):
        from o3de_cli.core.gem_package import _fetch_release_archive

        with pytest.raises(UnsafeSourceError, match="no SHA-256"):
            _fetch_release_archive("https://example.com/x.zip", None, "x.zip", lambda m: None)

    def test_remote_http_refused(self, tmp_path):
        from o3de_cli.core.gem_package import _fetch_release_archive

        with pytest.raises(UnsafeSourceError, match="plain HTTP"):
            _fetch_release_archive("http://example.com/x.zip", "a" * 64, "x.zip", lambda m: None)

    def test_local_file_needs_no_checksum(self, tmp_path):
        from o3de_cli.core.gem_package import _fetch_release_archive

        archive = _zip(tmp_path, {"a": b"x"})
        assert _fetch_release_archive(str(archive), None, "x.zip", lambda m: None) == archive

    def test_local_file_checksum_still_verified(self, tmp_path):
        from o3de_cli.core.gem_package import _fetch_release_archive
        from o3de_cli.core.store import IntegrityError

        archive = _zip(tmp_path, {"a": b"x"})
        with pytest.raises(IntegrityError):
            _fetch_release_archive(str(archive), "c" * 64, "x.zip", lambda m: None)
        good = hashlib.sha256(archive.read_bytes()).hexdigest()
        assert _fetch_release_archive(str(archive), good, "x.zip", lambda m: None) == archive


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------


def _gem_with_hook(tmp_path: Path, script: str, body: str | None = None, hook="post_install"):
    gem = tmp_path / "gem"
    gem.mkdir(exist_ok=True)
    (gem / "gem.json").write_text(
        json.dumps({"gem": {"name": "org.test.gem.hooked", "hooks": {hook: script}}})
    )
    if body is not None:
        target = gem / script
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    return gem


class TestHookPaths:
    @pytest.mark.parametrize("script", ["../outside.py", "/etc/passwd", "a/../../outside.py"])
    def test_script_outside_object_refused(self, tmp_path, script):
        from o3de_cli.core.hooks import HooksEngine

        gem = tmp_path / "gem"
        gem.mkdir()
        (tmp_path / "outside.py").write_text("print('ran')")
        assert HooksEngine.resolve_script(gem, script) is None
        assert HooksEngine(confirm=False).run_hook("post_install", script, gem, "g") is False

    def test_script_inside_object_resolves(self, tmp_path):
        from o3de_cli.core.hooks import HooksEngine

        gem = tmp_path / "gem"
        (gem / "scripts").mkdir(parents=True)
        (gem / "scripts" / "setup.py").write_text("")
        assert HooksEngine.resolve_script(gem, "scripts/setup.py") == (
            gem / "scripts" / "setup.py"
        ).resolve()


class TestHookPolicy:
    def test_flags(self):
        from o3de_cli.core.hooks import HookPolicy, hook_policy

        assert hook_policy(True, False, False) is HookPolicy.NEVER
        assert hook_policy(False, True, True) is HookPolicy.ALWAYS
        # Nobody to ask on a --json stdout
        assert hook_policy(False, False, True) is HookPolicy.NEVER

    def test_non_interactive_skips(self, monkeypatch):
        from o3de_cli.core.hooks import HookPolicy, hook_policy

        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
        assert hook_policy(False, False, False) is HookPolicy.NEVER

    def test_interactive_prompts(self, monkeypatch):
        from o3de_cli.core.hooks import HookPolicy, hook_policy

        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        assert hook_policy(False, False, False) is HookPolicy.PROMPT


class TestRunObjectHooks:
    MARK = "import pathlib; pathlib.Path('ran.txt').write_text('yes')\n"

    def test_never_skips(self, tmp_path):
        from o3de_cli.core.hooks import HookPolicy, run_object_hooks

        gem = _gem_with_hook(tmp_path, "setup.py", self.MARK)
        [outcome] = run_object_hooks(gem, "post_install", HookPolicy.NEVER)
        assert outcome.status == "skipped"
        assert not (gem / "ran.txt").exists()

    def test_always_runs(self, tmp_path):
        from o3de_cli.core.hooks import HookPolicy, run_object_hooks

        gem = _gem_with_hook(tmp_path, "setup.py", self.MARK)
        [outcome] = run_object_hooks(gem, "post_install", HookPolicy.ALWAYS)
        assert outcome.status == "ran"
        assert (gem / "ran.txt").read_text() == "yes"

    def test_prompt_shows_script_and_respects_no(self, tmp_path):
        from rich.console import Console

        from o3de_cli.core.hooks import HookPolicy, run_object_hooks

        gem = _gem_with_hook(tmp_path, "setup.py", self.MARK)
        out = io.StringIO()
        with patch("click.confirm", return_value=False) as confirm:
            [outcome] = run_object_hooks(
                gem, "post_install", HookPolicy.PROMPT, console=Console(file=out)
            )
        confirm.assert_called_once()
        assert outcome.status == "declined"
        assert "pathlib.Path('ran.txt')" in out.getvalue()  # the script was shown
        assert not (gem / "ran.txt").exists()

    def test_failure_reported_with_output(self, tmp_path):
        from o3de_cli.core.hooks import HookPolicy, run_object_hooks

        gem = _gem_with_hook(tmp_path, "setup.py", "import sys; print('boom'); sys.exit(3)\n")
        [outcome] = run_object_hooks(gem, "post_install", HookPolicy.ALWAYS)
        assert outcome.status == "failed"
        assert "boom" in outcome.output

    def test_outside_object_refused_even_when_trusted(self, tmp_path):
        from o3de_cli.core.hooks import HookPolicy, run_object_hooks

        (tmp_path / "evil.py").write_text(self.MARK)
        gem = _gem_with_hook(tmp_path, "../evil.py")
        [outcome] = run_object_hooks(gem, "post_install", HookPolicy.ALWAYS)
        assert outcome.status == "refused"
        assert not (tmp_path / "ran.txt").exists()

    def test_runs_in_run_dir(self, tmp_path):
        from o3de_cli.core.hooks import HookPolicy, run_object_hooks

        gem = _gem_with_hook(tmp_path, "gen.py", self.MARK, hook="pre_build")
        composed = tmp_path / "ws" / "Gems" / "hooked"
        composed.mkdir(parents=True)
        (composed / "gen.py").write_text(self.MARK)
        [outcome] = run_object_hooks(gem, "pre_build", HookPolicy.ALWAYS, run_dir=composed)
        assert outcome.status == "ran"
        assert (composed / "ran.txt").exists()
        assert not (gem / "ran.txt").exists()

    def test_no_hook_declared(self, tmp_path):
        from o3de_cli.core.hooks import HookPolicy, run_object_hooks

        gem = tmp_path / "gem"
        gem.mkdir()
        (gem / "gem.json").write_text(json.dumps({"gem": {"name": "org.test.gem.plain"}}))
        assert run_object_hooks(gem, "post_install", HookPolicy.ALWAYS) == []


# ---------------------------------------------------------------------------
# install, end to end through the command
# ---------------------------------------------------------------------------


class TestInstallCommand:
    def _run(self, tmp_path, args, hook_body=None):
        from o3de_cli.commands.registry import registry

        installed = _gem_with_hook(tmp_path, "setup.py", hook_body or TestRunObjectHooks.MARK)
        obj = _remote(source_control_url="https://github.com/example/gem.git")
        manifest = tmp_path / "o3de_manifest.json"
        manifest.write_text(json.dumps({"local": {}}))
        store = MagicMock()
        store.search.return_value = [obj]
        store.download_sync.return_value = installed
        with (
            patch("o3de_cli.commands.registry.Store", return_value=store),
            patch("o3de_cli.core.store.get_manifest_remote_urls", return_value=[]),
            patch("o3de_cli.commands.manifest.get_manifest_path", return_value=manifest),
        ):
            result = CliRunner().invoke(registry, ["install", "org.test.gem.x", *args])
        return result, installed, json.loads(manifest.read_text()), store

    def test_json_registers_and_skips_hook(self, tmp_path):
        result, installed, manifest, _ = self._run(tmp_path, ["--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)  # stdout is exactly one JSON document
        assert data["data"]["registered"] is True
        assert data["data"]["hooks"][0]["status"] == "skipped"
        assert data["warnings"]
        assert installed.as_posix() in manifest["local"]["gems"]
        assert not (installed / "ran.txt").exists()

    def test_yes_hooks_runs_hook(self, tmp_path):
        result, installed, _, _ = self._run(tmp_path, ["--json", "--yes-hooks"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["data"]["hooks"][0]["status"] == "ran"
        assert (installed / "ran.txt").exists()

    def test_failed_hook_exits_1(self, tmp_path):
        result, _, _, _ = self._run(
            tmp_path, ["--json", "--yes-hooks"], hook_body="raise SystemExit(2)\n"
        )
        assert result.exit_code == 1
        assert json.loads(result.output)["code"] == "E_HOOK_FAILED"

    def test_allow_unverified_passed_through(self, tmp_path):
        _, _, _, store = self._run(tmp_path, ["--json", "--allow-unverified", "--no-hooks"])
        assert store.download_sync.call_args[1]["allow_unverified"] is True

    def test_refused_download_exits_1(self, tmp_path):
        from o3de_cli.commands.registry import registry

        store = MagicMock()
        store.search.return_value = [_remote(download_url="https://example.com/x.zip")]
        store.download_sync.side_effect = UnsafeSourceError("publishes no SHA-256")
        with (
            patch("o3de_cli.commands.registry.Store", return_value=store),
            patch("o3de_cli.core.store.get_manifest_remote_urls", return_value=[]),
        ):
            result = CliRunner().invoke(registry, ["install", "org.test.gem.x", "--json"])
        assert result.exit_code == 1
        assert json.loads(result.output)["code"] == "E_UNSAFE_SOURCE"
