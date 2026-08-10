#!/usr/bin/env python3
"""Export confirmed face pairs as a Promptfoo-compatible JSONL dataset."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def export(pairs: list[dict], output: Path) -> int:
    output.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for pair in pairs:
        rows.append(
            {
                "vars": {
                    "path_a": pair.get("path_a", ""),
                    "path_b": pair.get("path_b", ""),
                    "expected_label": pair.get("label", ""),
                },
                "assert": [
                    {
                        "type": "javascript",
                        "value": "context.vars.expected_label === output.trim()",
                    }
                ],
                "metadata": {
                    "expected_label": pair.get("label", ""),
                    "identity": pair.get("identity", ""),
                    "identity_a": pair.get("identity_a", ""),
                    "identity_b": pair.get("identity_b", ""),
                },
            }
        )
    output.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pairs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    pairs = json.loads(args.pairs.read_text(encoding="utf-8"))
    if not isinstance(pairs, list):
        raise SystemExit("pairs input must be a JSON list")
    print(json.dumps({"output": str(args.output), "rows": export(pairs, args.output)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
