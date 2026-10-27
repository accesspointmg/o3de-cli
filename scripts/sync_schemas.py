#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0 OR MIT

"""Copy the canonical 2.0.0 JSON schemas into o3de_cli/schemas/.

The CLI ships its own copy of the schemas so that validation works for
anyone who installs it from PyPI. Run this after the schemas change in
canonical.o3de.org, then commit the result:

    python scripts/sync_schemas.py ../canonical.o3de.org

The source commit is recorded in o3de_cli/schemas/SOURCE so it is always
clear which canonical revision a release validates against.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

SCHEMAS = [
    "o3de-engine-2.0.0.json",
    "o3de-gem-2.0.0.json",
    "o3de-manifest-2.0.0.json",
    "o3de-overlay-2.0.0.json",
    "o3de-patterns-2.0.0.json",
    "o3de-project-2.0.0.json",
    "o3de-repo-2.0.0.json",
    "o3de-template-2.0.0.json",
    "o3de-workspace-2.0.0.json",
]

DEST = Path(__file__).resolve().parent.parent / "o3de_cli" / "schemas"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("canonical_repo", type=Path, help="Path to a canonical.o3de.org checkout")
    args = parser.parse_args()

    src_dir = args.canonical_repo / "src"
    missing = [name for name in SCHEMAS if not (src_dir / name).is_file()]
    if missing:
        print(f"Not found in {src_dir}: {', '.join(missing)}", file=sys.stderr)
        return 1

    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=args.canonical_repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "src"],
        cwd=args.canonical_repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if dirty:
        print("canonical.o3de.org/src has uncommitted changes; commit them first.", file=sys.stderr)
        return 1

    DEST.mkdir(exist_ok=True)
    for name in SCHEMAS:
        shutil.copyfile(src_dir / name, DEST / name)
    (DEST / "SOURCE").write_text(
        f"https://github.com/accesspointmg/canonical.o3de.org/tree/{commit}/src\n",
        encoding="utf-8",
    )
    print(f"Copied {len(SCHEMAS)} schemas from canonical.o3de.org@{commit[:10]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
