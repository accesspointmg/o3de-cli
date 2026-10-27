# O3DE CLI - JSON Output Helper
# SPDX-License-Identifier: Apache-2.0 OR MIT

"""Standardised JSON response format for ``--json`` output.

Every command that supports ``--json`` follows one contract.

On success stdout is exactly one JSON document and the exit code is 0
(:func:`emit_response`):

.. code-block:: json

    {
        "status": "ok",
        "data": { ... },
        "warnings": []
    }

``data`` is the command's payload (any JSON value, ``null`` when the
command has nothing to report) and ``warnings`` is always a list.

On error stdout is exactly one JSON document and the exit code is 1
(:func:`emit_error` followed by ``raise SystemExit(1)``):

.. code-block:: json

    {
        "status": "error",
        "error": "Human-readable message",
        "code": "E_BUILD_FAILED"
    }

Two optional fields carry detail when a command has more than one
message to report:

``errors``
    Every message, when the command found several (e.g. validation
    errors); ``error`` then summarises or repeats the first one.
``data``
    A structured report behind the failure (e.g. the issues an audit
    found), shaped like the payload the command returns on success.

Nothing else reaches stdout in ``--json`` mode: no Rich spinners, status
lines or prompts.  Diagnostics go to stderr.  ``console.print_json`` is
not used because Rich may add ANSI colour codes, which break parsers.

Unexpected exceptions under ``--json`` are turned into an
``E_INTERNAL`` error by the entry point (:func:`o3de_cli.__main__.main`).
Click usage errors (bad options, exit code 2) are reported by Click on
stderr and leave stdout empty.
"""

from __future__ import annotations

import json
import sys
from typing import Any


def json_response(
    *,
    data: Any = None,
    warnings: list[str] | None = None,
) -> str:
    """Build a success JSON response string.

    Parameters
    ----------
    data:
        Payload — any JSON-serialisable value (``None`` becomes ``null``).
    warnings:
        Optional list of warning strings (always emitted, ``[]`` if none).
    """
    envelope: dict[str, Any] = {
        "status": "ok",
        "data": data,
        "warnings": list(warnings or []),
    }
    return json.dumps(envelope, indent=2, default=str)


def json_error(
    message: str,
    *,
    code: str | None = None,
    errors: list[Any] | None = None,
    data: Any = None,
) -> str:
    """Build an error JSON response string.

    Parameters
    ----------
    message:
        Human-readable error description.
    code:
        Machine-readable error code (e.g. ``E_BUILD_FAILED``).  Commands
        always pass one.
    errors:
        Optional list of every error message, when there were several.
    data:
        Optional structured report behind the failure.
    """
    envelope: dict[str, Any] = {"status": "error", "error": message}
    if code:
        envelope["code"] = code
    if errors:
        envelope["errors"] = errors
    if data is not None:
        envelope["data"] = data
    return json.dumps(envelope, indent=2, default=str)


def emit(text: str) -> None:
    """Write JSON text to stdout and flush."""
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def emit_response(*, data: Any = None, warnings: list[str] | None = None) -> None:
    """Convenience: build and emit a success response."""
    emit(json_response(data=data, warnings=warnings))


def emit_error(
    message: str,
    *,
    code: str | None = None,
    errors: list[Any] | None = None,
    data: Any = None,
) -> None:
    """Convenience: build and emit an error response.

    The caller must still ``raise SystemExit(1)``.
    """
    emit(json_error(message, code=code, errors=errors, data=data))
