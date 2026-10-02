import sqlite3

from tools.relink_identity_consolidation_records import identity_indexes, update_sqlite_paths


def test_face_database_relinks_coalesced_paths_and_merges_alias_metadata(tmp_path):
    source = "/mnt/elements16/@mixedpics_sorted/old_folder/photo.jpg"
    target = "/mnt/elements16/@mixedpics_sorted/alice_winterhold/photo.jpg"
    cache_root = tmp_path / "face-references-coalesced-test"
    identity_dir = cache_root / "old_folder"
    identity_dir.mkdir(parents=True)
    cache_link = identity_dir / "0000000_photo.jpg"
    cache_link.symlink_to(source)

    database = tmp_path / "faces.db"
    connection = sqlite3.connect(database)
    connection.executescript("""
        CREATE TABLE face_encodings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_name TEXT NOT NULL,
            encoding BLOB NOT NULL,
            image_path TEXT NOT NULL,
            face_quality REAL NOT NULL,
            model_used TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE person_metadata (
            person_name TEXT PRIMARY KEY,
            total_faces INTEGER DEFAULT 0,
            avg_quality REAL DEFAULT 0.0,
            best_encoding BLOB,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    connection.executemany(
        "INSERT INTO face_encodings(person_name,encoding,image_path,face_quality,model_used) "
        "VALUES(?,?,?,?,?)",
        [
            ("alicewinterhold", b"old", str(cache_link), 0.6, "face_recognition"),
            ("alice_winterhold", b"new", target, 1.0, "face_recognition"),
        ],
    )
    connection.executemany(
        "INSERT INTO person_metadata(person_name,total_faces,avg_quality,best_encoding) VALUES(?,?,?,?)",
        [("alicewinterhold", 1, 0.6, b"old"), ("alice_winterhold", 1, 1.0, b"new")],
    )
    connection.commit()
    connection.close()

    item = {
        "source": source,
        "target": target,
        "status": "proposed",
        "identity_family": "linked",
        "identity_id": "alice_winterhold",
        "source_aliases": {"reddit_users": ["alicewinterhold"]},
    }
    aliases, _ = identity_indexes([item])
    counts = update_sqlite_paths(
        database,
        {source: target},
        aliases,
        face_database=True,
        coalesced_prefix=str(cache_root),
    )

    connection = sqlite3.connect(database)
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute(
        "SELECT person_name,image_path FROM face_encodings ORDER BY id"
    ).fetchall() == [("alice_winterhold", target), ("alice_winterhold", target)]
    assert connection.execute(
        "SELECT total_faces,avg_quality,best_encoding FROM person_metadata WHERE person_name=?",
        ("alice_winterhold",),
    ).fetchone() == (2, 0.8, b"new")
    assert connection.execute("SELECT count(*) FROM person_metadata").fetchone()[0] == 1
    connection.close()
    assert counts["face_encodings.image_path"] == 1
    assert counts["face_encodings.person_name"] == 1
