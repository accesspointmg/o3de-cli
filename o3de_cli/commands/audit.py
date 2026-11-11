# O3DE CLI - Audit Command
# SPDX-License-Identifier: Apache-2.0 OR MIT

"""Dependency tree audit command."""

from contextlib import nullcontext
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.tree import Tree

from o3de_cli.core import (
    Resolver,
    get_manifest_path,
)
from o3de_cli.core.audit_checks import collect_issues
from o3de_cli.core.store import compute_sha256

console = Console()

# Historical private name, kept importable from this module.
_collect_issues = collect_issues


@click.command("audit")
@click.option("--json", "as_json", is_flag=True, help="Output results as JSON")
def audit(as_json: bool) -> None:
    """Audit the dependency tree for issues.

    Scans for:
    - Deprecated objects still in use
    - Missing integrity checksums on releases
    - Unresolved dependencies (missing or version mismatch)
    - Missing peer dependencies
    - Unresolvable optional dependencies
    - Version conflicts between siblings

    Exits 1 when any issue is found.  Under ``--json`` that is an
    ``E_AUDIT_ISSUES`` error whose ``data`` carries the full report.
    """
    from o3de_cli.core.json_output import emit_error, emit_response

    manifest_path = get_manifest_path()
    if not manifest_path.exists():
        if as_json:
            emit_error("No manifest found. Run 'o3de init' first.", code="E_NO_MANIFEST")
            raise SystemExit(1)
        console.print("[red]No manifest found.[/red] Run 'o3de init' first.")
        raise SystemExit(1)

    resolver = Resolver(manifest_path)

    with nullcontext() if as_json else console.status("Resolving manifest..."):
        resolver.resolve()

    issues = _collect_issues(resolver)
    total = sum(len(v) for v in issues.values())

    if as_json:
        report = {"total": total, "issues": issues}
        if total > 0:
            noun = "issue" if total == 1 else "issues"
            emit_error(f"Audit found {total} {noun}", code="E_AUDIT_ISSUES", data=report)
            raise SystemExit(1)
        emit_response(data=report)
        return

    _display_issues(issues)

    if total > 0:
        raise SystemExit(1)


def _display_issues(issues: dict[str, list[dict]]) -> None:
    """Display audit issues in a rich table."""
    total = sum(len(v) for v in issues.values())

    if not issues:
        console.print("[green]No issues found — dependency tree is healthy.[/green]")
        return

    console.print(f"\n[bold red]Found {total} issue(s):[/bold red]\n")

    if "deprecated" in issues:
        table = Table(title="Deprecated Objects", show_lines=True)
        table.add_column("Object", style="cyan")
        table.add_column("Message", style="yellow")
        table.add_column("Replacement", style="green")
        for item in issues["deprecated"]:
            table.add_row(item["object"], item["message"], item.get("replacement", ""))
        console.print(table)
        console.print()

    if "missing_integrity" in issues:
        table = Table(title="Missing Integrity Checksums", show_lines=True)
        table.add_column("Object", style="cyan")
        table.add_column("Release", style="blue")
        table.add_column("Missing Field", style="yellow")
        for item in issues["missing_integrity"]:
            table.add_row(item["object"], item["release"], item["field"])
        console.print(table)
        console.print()

    if "missing_dependencies" in issues:
        table = Table(title="Missing Dependencies", show_lines=True)
        table.add_column("Object", style="cyan")
        table.add_column("Dependency", style="red")
        table.add_column("Reason", style="yellow")
        for item in issues["missing_dependencies"]:
            table.add_row(item["object"], item["dependency"], item["reason"])
        console.print(table)
        console.print()

    if "missing_peer_dependencies" in issues:
        table = Table(title="Missing Peer Dependencies", show_lines=True)
        table.add_column("Object", style="cyan")
        table.add_column("Peer", style="red")
        table.add_column("Reason", style="yellow")
        for item in issues["missing_peer_dependencies"]:
            table.add_row(item["object"], item["peer"], item["reason"])
        console.print(table)
        console.print()

    if "unresolvable_optional" in issues:
        table = Table(title="Unresolvable Optional Dependencies", show_lines=True)
        table.add_column("Object", style="cyan")
        table.add_column("Optional Dep", style="yellow")
        table.add_column("Reason", style="dim")
        for item in issues["unresolvable_optional"]:
            table.add_row(item["object"], item["optional"], item["reason"])
        console.print(table)
        console.print()

    if "conflicts" in issues:
        table = Table(title="Version Conflicts", show_lines=True)
        table.add_column("Dependency", style="red")
        table.add_column("Requirer A", style="cyan")
        table.add_column("Constraint A", style="yellow")
        table.add_column("Requirer B", style="cyan")
        table.add_column("Constraint B", style="yellow")
        table.add_column("Resolved", style="dim")
        for item in issues["conflicts"]:
            table.add_row(
                item["dependency"],
                item["requirer_a"],
                item["constraint_a"],
                item["requirer_b"],
                item["constraint_b"],
                item.get("resolved_version", ""),
            )
        console.print(table)
        console.print()
