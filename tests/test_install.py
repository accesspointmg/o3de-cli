# O3DE CLI - package install step tests
# SPDX-License-Identifier: Apache-2.0 OR MIT

"""Unit tests for o3de_cli.core.install, without Click or the console."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from o3de_cli.core.hooks import HookOutcome
from o3de_cli.core.install import (
    dry_run_report,
    failed_hooks,
    find_package,
    hook_warnings,
    install_report,
    install_target,
    select_package,
)
from o3de_cli.core.models import ObjectType
from o3de_cli.core.store import RemoteObject


def _remote(name, version, src=None, dl=None, t=ObjectType.GEM):
    return RemoteObject(
        url="https://r/x",
        object_type=t,
        name=name,
        version=version,
        source_control_url=src,
        download_url=dl,
    )


class TestFindPackage:
    def test_refreshes_from_configured_remotes(self):
        store = MagicMock()
        store.search.return_value = ["hit"]
        with patch("o3de_cli.core.store.get_manifest_remote_urls", return_value=["https://r"]):
            assert find_package(store, "org.p") == ["hit"]
        store.refresh_sync.assert_called_once_with(["https://r"])
        store.search.assert_called_once_with("org.p")

    def test_no_remotes_skips_refresh(self):
        store = MagicMock()
        store.search.return_value = []
        with patch("o3de_cli.core.store.get_manifest_remote_urls", return_value=[]):
            assert find_package(store, "org.p") == []
        store.refresh_sync.assert_not_called()


class TestSelectPackage:
    RESULTS = [_remote("org.other", "9.0.0"), _remote("org.p", "1.0.0"), _remote("org.p", "2.0.0")]

    def test_first_exact_name(self):
        assert select_package(self.RESULTS, "org.p", None) is self.RESULTS[1]

    def test_exact_name_and_version(self):
        assert select_package(self.RESULTS, "org.p", "2.0.0") is self.RESULTS[2]

    def test_falls_back_to_first_result(self):
        assert select_package(self.RESULTS, "org.p", "3.0.0") is self.RESULTS[0]
        assert select_package(self.RESULTS, "org.none", None) is self.RESULTS[0]


class TestInstallTarget:
    def test_explicit_path(self):
        assert install_target(_remote("org.p", "1"), "some/dir") == Path("some/dir")

    def test_default_for_type(self):
        with patch(
            "o3de_cli.core.paths.get_default_path_for_type", return_value=Path("/t")
        ) as default:
            assert install_target(_remote("org.p", "1", t=ObjectType.TEMPLATE), None) == Path("/t")
        default.assert_called_once_with(ObjectType.TEMPLATE)


class TestReports:
    def test_dry_run_prefers_source_control(self):
        obj = _remote("org.p", "1.0.0", src="https://git", dl="https://dl")
        assert dry_run_report(obj, Path("t")) == {
            "dry_run": True,
            "package": "org.p",
            "version": "1.0.0",
            "type": "gem",
            "target": str(Path("t")),
            "source": "https://git",
        }

    def test_dry_run_download_or_neither(self):
        assert dry_run_report(_remote("org.p", "1", dl="https://dl"), Path("t"))["download"] == (
            "https://dl"
        )
        report = dry_run_report(_remote("org.p", "1"), Path("t"))
        assert "source" not in report and "download" not in report

    def test_install_report_and_hooks(self):
        outcomes = [
            HookOutcome("org.p", "post_install", "a.py", "ran"),
            HookOutcome("org.p", "post_install", "b.py", "failed"),
            HookOutcome("org.p", "post_install", "c.py", "skipped"),
        ]
        report = install_report(_remote("org.p", "1.0.0"), Path("d"), True, outcomes)
        assert report["package"] == "org.p"
        assert report["path"] == str(Path("d"))
        assert report["registered"] is True
        assert [h["script"] for h in report["hooks"]] == ["a.py", "b.py", "c.py"]
        assert hook_warnings(outcomes) == [
            "post_install hook failed: b.py",
            "post_install hook skipped: c.py",
        ]
        assert failed_hooks(outcomes) == [outcomes[1]]
