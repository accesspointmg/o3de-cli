# O3DE CLI - Hooks Execution Engine
# SPDX-License-Identifier: Apache-2.0 OR MIT

"""
Hooks execution engine for O3DE objects.

Objects can define hooks in their JSON metadata:
    "hooks": {
        "post_install": "scripts/setup.py",
        "pre_build": "scripts/prebuild.sh"
    }

The engine runs these scripts with user confirmation (sandboxing/security).
"""

import logging
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("o3de_cli.hooks")


class HookError(Exception):
    """Error during hook execution."""

    pass


class HooksEngine:
    """Execute hooks defined in O3DE object metadata.

    Security: All hooks require explicit user confirmation before execution.
    Hooks run in the object's directory with a timeout.
    """

    def __init__(
        self,
        confirm: bool = True,
        timeout: int = 300,
        dry_run: bool = False,
    ):
        """
        Args:
            confirm: Require user confirmation before running hooks
            timeout: Maximum seconds per hook execution
            dry_run: Show what would run without executing
        """
        self.confirm = confirm
        self.timeout = timeout
        self.dry_run = dry_run
        # stdout+stderr of the most recent execution, for callers to show
        self.last_output = ""

    @staticmethod
    def resolve_script(object_dir: Path, script_path: str) -> Path | None:
        """The hook script inside *object_dir*, or None if it points outside.

        Hook paths come from object metadata, so ``../../elsewhere`` or an
        absolute path must not reach files the object does not contain.
        """
        candidate = Path(script_path)
        if candidate.is_absolute() or script_path.startswith(("/", "\\")):
            return None
        root = object_dir.resolve()
        full = (root / candidate).resolve()
        if full == root or root not in full.parents:
            return None
        return full

    def run_hook(
        self,
        hook_type: str,
        script_path: str,
        object_dir: Path,
        object_name: str,
        confirm_callback: Callable[[str, str, str], bool] | None = None,
    ) -> bool:
        """Run a single hook script.

        Args:
            hook_type: "post_install" or "pre_build"
            script_path: Relative path to script from object_dir
            object_dir: Root directory of the O3DE object
            object_name: Name of the object (for logging)
            confirm_callback: Optional callback for confirmation; if None,
                uses default console prompt. Should return True to proceed.

        Returns:
            True if hook ran successfully (or was skipped in dry-run)
        """
        resolved = self.resolve_script(object_dir, script_path)
        if resolved is None:
            logger.error(
                f"Hook [{hook_type}] for {object_name} refused: "
                f"{script_path!r} is outside the object directory"
            )
            return False
        full_path = resolved

        if not full_path.exists():
            logger.warning(f"Hook script not found: {full_path}")
            return False

        logger.info(f"Hook [{hook_type}] for {object_name}: {full_path}")

        if self.dry_run:
            logger.info(f"Dry-run: would execute {full_path}")
            return True

        if self.confirm:
            if confirm_callback:
                approved = confirm_callback(hook_type, script_path, object_name)
            else:
                approved = self._default_confirm(hook_type, script_path, object_name)
            if not approved:
                logger.info(f"Hook [{hook_type}] skipped by user for {object_name}")
                return False

        return self._execute(full_path, object_dir, hook_type, object_name)

    def run_hooks_for_object(
        self,
        data: dict,
        object_dir: Path,
        object_name: str,
        hook_filter: list[str] | None = None,
        confirm_callback: Callable[[str, str, str], bool] | None = None,
    ) -> dict[str, bool]:
        """Run all hooks defined in an object's data.

        Args:
            data: Object JSON data (raw dict)
            object_dir: Root directory of the O3DE object
            object_name: Name of the object
            hook_filter: Only run these hook types (e.g., ["post_install"])
            confirm_callback: Optional confirmation callback

        Returns:
            Dict of hook_type -> success status
        """
        results: dict[str, bool] = {}

        # Find hooks in data — check inside type dict and root level
        hooks = None
        for type_key in ["engine", "project", "gem", "template", "overlay"]:
            type_data = data.get(type_key, {})
            if isinstance(type_data, dict) and "hooks" in type_data:
                hooks = type_data["hooks"]
                break

        if not hooks:
            hooks = data.get("hooks", {})

        if not hooks or not isinstance(hooks, dict):
            return results

        for hook_type, script_path in hooks.items():
            if hook_filter and hook_type not in hook_filter:
                continue
            if not script_path:
                continue
            results[hook_type] = self.run_hook(
                hook_type,
                script_path,
                object_dir,
                object_name,
                confirm_callback=confirm_callback,
            )

        return results

    def _execute(
        self,
        script_path: Path,
        working_dir: Path,
        hook_type: str,
        object_name: str,
    ) -> bool:
        """Execute a hook script safely."""
        # Determine how to run the script
        suffix = script_path.suffix.lower()
        if suffix == ".py":
            cmd = [sys.executable, str(script_path)]
        elif suffix in (".sh", ".bash"):
            cmd = ["bash", str(script_path)]
        elif suffix in (".bat", ".cmd"):
            cmd = ["cmd", "/c", str(script_path)]
        elif suffix == ".ps1":
            cmd = ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(script_path)]
        else:
            # Try to run directly (executable permission)
            cmd = [str(script_path)]

        logger.info(f"Executing hook [{hook_type}] for {object_name}: {' '.join(cmd)}")
        self.last_output = ""

        try:
            result = subprocess.run(
                cmd,
                cwd=str(working_dir),
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
            self.last_output = (result.stdout or "") + (result.stderr or "")

            if result.stdout:
                logger.info(f"Hook [{hook_type}] stdout:\n{result.stdout}")
            if result.stderr:
                logger.warning(f"Hook [{hook_type}] stderr:\n{result.stderr}")

            if result.returncode != 0:
                logger.error(
                    f"Hook [{hook_type}] for {object_name} failed with code {result.returncode}"
                )
                return False

            logger.info(f"Hook [{hook_type}] for {object_name} completed successfully")
            return True

        except subprocess.TimeoutExpired:
            logger.error(f"Hook [{hook_type}] for {object_name} timed out after {self.timeout}s")
            return False
        except FileNotFoundError:
            logger.error(f"Hook [{hook_type}] interpreter not found for {script_path}")
            return False
        except Exception as e:
            logger.error(f"Hook [{hook_type}] for {object_name} error: {e}")
            return False

    @staticmethod
    def _default_confirm(hook_type: str, script_path: str, object_name: str) -> bool:
        """Default console confirmation prompt."""
        try:
            response = input(
                f"\nHook [{hook_type}] wants to run '{script_path}' "
                f"for {object_name}. Allow? [y/N] "
            )
            return response.strip().lower() in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            return False


# ---------------------------------------------------------------------------
# Running an object's hooks from a command
# ---------------------------------------------------------------------------

HOOK_TYPES = ("post_install", "pre_build")

_OBJECT_TYPES = ("engine", "project", "gem", "template", "overlay", "repo")

# Lines of the script shown before asking whether to run it
_PREVIEW_LINES = 40


class HookPolicy(str, Enum):
    """How a command treats hooks it encounters."""

    PROMPT = "prompt"  # show the script and ask, per hook
    ALWAYS = "always"  # run without asking (--yes-hooks)
    NEVER = "never"  # skip (--no-hooks, or nobody to ask)


def hook_policy(no_hooks: bool, yes_hooks: bool, as_json: bool) -> HookPolicy:
    """Pick the policy for one command invocation.

    Asking needs an interactive terminal and a stdout that is not carrying
    JSON, so without both, hooks are skipped unless --yes-hooks was given.
    """
    if no_hooks:
        return HookPolicy.NEVER
    if yes_hooks:
        return HookPolicy.ALWAYS
    if as_json or not sys.stdin.isatty():
        return HookPolicy.NEVER
    return HookPolicy.PROMPT


@dataclass
class HookOutcome:
    """What happened to one hook."""

    object_name: str
    hook_type: str
    script: str
    status: str  # ran, failed, skipped, declined, refused, missing
    output: str = ""

    def to_dict(self) -> dict:
        return {
            "object": self.object_name,
            "hook": self.hook_type,
            "script": self.script,
            "status": self.status,
        }


def load_object_data(object_dir: Path) -> tuple[dict, str] | None:
    """The object JSON in *object_dir* (2.0.0 sidecar first) and its name."""
    import json

    for type_name in _OBJECT_TYPES:
        for filename in (f"{type_name}.2-0-0.json", f"{type_name}.json"):
            path = object_dir / filename
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                return None
            header = data.get(type_name)
            name = header.get("name") if isinstance(header, dict) else data.get(f"{type_name}_name")
            return data, name or object_dir.name
    return None


def _declared_hooks(data: dict) -> dict:
    for type_name in _OBJECT_TYPES:
        section = data.get(type_name)
        if isinstance(section, dict) and isinstance(section.get("hooks"), dict):
            return section["hooks"]
    hooks = data.get("hooks")
    return hooks if isinstance(hooks, dict) else {}


def _preview(script: Path) -> str:
    try:
        lines = script.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "(unreadable)"
    shown = "\n".join(lines[:_PREVIEW_LINES])
    if len(lines) > _PREVIEW_LINES:
        shown += f"\n... ({len(lines) - _PREVIEW_LINES} more lines)"
    return shown


def run_object_hooks(
    object_dir: Path,
    hook_type: str,
    policy: HookPolicy,
    console: Any = None,
    run_dir: Path | None = None,
) -> list[HookOutcome]:
    """Run *object_dir*'s *hook_type* hook according to *policy*.

    The script must live inside the object. Under PROMPT the script itself
    is shown before asking, since its name alone says nothing about what it
    does. *run_dir* is where it runs (default: the object directory), e.g.
    a workspace's composed copy of the object.
    """
    import click

    loaded = load_object_data(object_dir)
    if loaded is None:
        return []
    data, name = loaded
    script = _declared_hooks(data).get(hook_type)
    if not script or not isinstance(script, str):
        return []

    def say(msg: str) -> None:
        if console is not None:
            console.print(msg)

    engine = HooksEngine(confirm=False)
    full = engine.resolve_script(object_dir, script)
    if full is None:
        say(f"[red]Refusing {hook_type} hook of {name}:[/red] {script!r} is outside the object")
        return [HookOutcome(name, hook_type, script, "refused")]
    if not full.is_file():
        say(f"[yellow]{hook_type} hook of {name} not found:[/yellow] {script}")
        return [HookOutcome(name, hook_type, script, "missing")]

    if policy is HookPolicy.NEVER:
        say(
            f"[yellow]Skipped {hook_type} hook of {name}[/yellow] ({script}). "
            "Re-run with --yes-hooks to run it."
        )
        return [HookOutcome(name, hook_type, script, "skipped")]

    if policy is HookPolicy.PROMPT:
        say(f"\n[bold]{name}[/bold] has a [cyan]{hook_type}[/cyan] hook: {script}")
        say(f"[dim]{'-' * 60}[/dim]")
        if console is not None:
            from rich.markup import escape

            console.print(escape(_preview(full)), highlight=False)
        say(f"[dim]{'-' * 60}[/dim]")
        if not click.confirm(f"Run this {hook_type} script?", default=False):
            return [HookOutcome(name, hook_type, script, "declined")]

    cwd = run_dir or object_dir
    ok = engine._execute(cwd / full.relative_to(object_dir.resolve()), cwd, hook_type, name)
    status = "ran" if ok else "failed"
    if ok:
        say(f"[green]Ran {hook_type} hook of {name}[/green]")
    else:
        say(f"[red]{hook_type} hook of {name} failed[/red]")
        if engine.last_output.strip():
            from rich.markup import escape

            say(escape(engine.last_output.strip()[-2000:]))
    return [HookOutcome(name, hook_type, script, status, engine.last_output)]
