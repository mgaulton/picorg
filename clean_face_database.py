#!/usr/bin/env python3
"""Remove non-canonical identities from a PicOrg-compatible face database."""

from __future__ import annotations

import argparse
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

import picorg_sorter as sorter


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("/opt/photo_reorg/data/high_accuracy_faces.db"))
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--reset", action="store_true", help="delete all face rows after creating the backup")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    backup = args.backup or args.db.with_name(f"{args.db.name}.pre-clean-{datetime.now():%Y%m%d-%H%M%S}")

    _, aliases, canonical_index, _, _ = sorter.load_identity_catalog()
    allowed = {
        key for key, identity in canonical_index.items()
        if not sorter.is_generic_identity_token(identity.canonical)
    }
    allowed.update(
        key for key, identities in aliases.items()
        if len(identities) == 1 and not sorter.is_generic_identity_token(next(iter(identities)).canonical)
    )

    if not args.dry_run:
        shutil.copy2(args.db, backup)
    con = sqlite3.connect(args.db)
    names = [row[0] for row in con.execute("SELECT DISTINCT person_name FROM person_metadata")]
    bad = [str(name) for name in names] if args.reset else []
    if not args.reset:
        for name in names:
            suffix = str(name).split("__", 1)[-1]
            if sorter.normalize_key(suffix) not in allowed:
                bad.append(str(name))
    print(f"identities={len(names)} selected={len(bad)}")
    print("selected_names=" + ",".join(sorted(bad)))
    if not args.dry_run and bad:
        placeholders = ",".join("?" for _ in bad)
        con.execute(f"DELETE FROM face_encodings WHERE person_name IN ({placeholders})", bad)
        con.execute(f"DELETE FROM person_metadata WHERE person_name IN ({placeholders})", bad)
        con.commit()
        con.execute("VACUUM")
    con.close()
    if not args.dry_run:
        print(f"backup={backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
