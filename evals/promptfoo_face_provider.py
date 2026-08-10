"""Promptfoo provider adapter for the local face matcher.

This intentionally invokes a project-owned command supplied by the operator;
it never uploads image bytes to a remote provider.
"""

from __future__ import annotations

import json
import os
import subprocess


def call_api(prompt: str, options: dict | None = None, context: dict | None = None) -> dict:
    del prompt, options
    context = context or {}
    vars_ = context.get("vars", {})
    command = os.environ.get("PICORG_FACE_PAIR_COMMAND")
    if not command:
        return {"output": "unconfigured", "error": "Set PICORG_FACE_PAIR_COMMAND to a local matcher command"}
    result = subprocess.run(
        [command, vars_.get("path_a", ""), vars_.get("path_b", "")],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode:
        return {"output": "error", "error": result.stderr[-1000:]}
    return {"output": result.stdout.strip()}
