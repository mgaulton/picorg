#!/usr/bin/env python3
"""Fail-closed validation for a PicOrg-compatible face database."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import picorg_sorter as sorter


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--min-faces", type=int, default=1)
    parser.add_argument("--min-identities", type=int, default=1)
    parser.add_argument(
        "--allow-generic",
        action="store_true",
        help="allow legacy generic identities when validating a rollback database",
    )
    args = parser.parse_args()
    if not args.db.is_file():
        raise SystemExit(f"face database is missing: {args.db}")
    con = sqlite3.connect(args.db)
    try:
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {"face_encodings", "person_metadata"}
        missing = required - tables
        if missing:
            raise SystemExit("face database is missing tables: " + ",".join(sorted(missing)))
        identities = [str(row[0]) for row in con.execute("SELECT DISTINCT person_name FROM person_metadata")]
        faces = int(con.execute("SELECT COUNT(*) FROM face_encodings").fetchone()[0])
    finally:
        con.close()
    generic = sorted(name for name in identities if sorter.is_generic_identity_token(name.split("__", 1)[-1]))
    if generic and not args.allow_generic:
        raise SystemExit("generic identities in face database: " + ",".join(generic[:20]))
    if faces < args.min_faces:
        raise SystemExit(f"face database has only {faces} face encodings (minimum {args.min_faces})")
    if len(identities) < args.min_identities:
        raise SystemExit(f"face database has only {len(identities)} identities (minimum {args.min_identities})")
    print(f"validated face database: identities={len(identities)} faces={faces}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
