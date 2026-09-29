import array
import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "build_canonical_face_baseline.py"


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    organized = tmp_path / "organized"
    media_dir = organized / "Alice Example"
    media_dir.mkdir(parents=True)
    media = media_dir / "photo.jpg"
    media.write_bytes(b"stable fixture")
    digest = hashlib.sha256(media.read_bytes()).hexdigest()

    evidence = tmp_path / "evidence.sqlite3"
    connection = sqlite3.connect(evidence)
    connection.executescript(
        """
        CREATE TABLE identities(canonical TEXT PRIMARY KEY, display_name TEXT, status TEXT,
            trust_level TEXT, sources_json TEXT, updated_at TEXT);
        CREATE TABLE aliases(alias_key TEXT, identity TEXT, alias TEXT, source TEXT,
            updated_at TEXT, PRIMARY KEY(alias_key, identity, source));
        """
    )
    connection.execute("INSERT INTO identities VALUES ('aliceexample','Alice Example','confirmed','registry_confirmed','[]','now')")
    connection.execute("INSERT INTO aliases VALUES ('aliceexample','aliceexample','Alice Example','test','now')")
    connection.commit()
    connection.close()

    cache = tmp_path / "embeddings.sqlite3"
    connection = sqlite3.connect(cache)
    connection.execute("CREATE TABLE embeddings(path TEXT PRIMARY KEY, fingerprint TEXT, status TEXT, embedding BLOB, metadata_json TEXT, updated_at TEXT)")
    connection.execute(
        "INSERT INTO embeddings VALUES (?,?,?,?,?,?)",
        (str(media), digest, "embedded", array.array("d", [0.1] * 128).tobytes(), '{"face_quality": 0.93}', "now"),
    )
    connection.commit()
    connection.close()
    return organized, evidence, cache, media, digest


def _run(tmp_path: Path, *, dry_run: bool = False) -> tuple[dict, Path, Path]:
    organized, evidence, cache, _media, _digest = _fixture(tmp_path)
    output = tmp_path / "baseline.json"
    hashes = tmp_path / "hashes.json"
    markers = tmp_path / "markers.json"
    command = [
        sys.executable,
        str(SCRIPT),
        "--organized-root", str(organized),
        "--metadaily-root", str(tmp_path / "missing-md"),
        "--redditdaily-root", str(tmp_path / "missing-rd"),
        "--evidence-db", str(evidence),
        "--embedding-cache", str(cache),
        "--output", str(output),
        "--hash-cache", str(hashes),
        "--markers", str(markers),
    ]
    if dry_run:
        command.append("--dry-run")
    subprocess.run(command, check=True, capture_output=True, text=True)
    return json.loads(output.read_text()), evidence, markers


def test_dry_run_uses_cached_content_without_writing_evidence(tmp_path):
    payload, evidence, markers = _run(tmp_path, dry_run=True)
    assert payload["baseline_records"] == 1
    assert not markers.exists()
    connection = sqlite3.connect(evidence)
    assert connection.execute("SELECT count(*) FROM media").fetchone()[0] == 0
    connection.close()


def test_baseline_persists_hash_keyed_observation_and_marker(tmp_path):
    payload, evidence, markers = _run(tmp_path)
    assert payload["baseline_records"] == 1
    connection = sqlite3.connect(evidence)
    assert connection.execute("SELECT count(*) FROM media").fetchone()[0] == 1
    assert connection.execute("SELECT count(*) FROM face_observations").fetchone()[0] == 1
    assert connection.execute("SELECT identity, source FROM face_markers").fetchone() == ("aliceexample", "canonical_baseline")
    connection.close()
    marker_payload = json.loads(markers.read_text())
    assert marker_payload["markers"][0]["sha256"] == payload["records"][0]["sha256"]


def test_generic_or_ambiguous_registry_identity_is_not_trusted_for_baseline(tmp_path):
    evidence = tmp_path / "evidence.sqlite3"
    connection = sqlite3.connect(evidence)
    connection.executescript(
        """
        CREATE TABLE identities(canonical TEXT PRIMARY KEY, display_name TEXT, status TEXT,
            trust_level TEXT, sources_json TEXT, updated_at TEXT);
        CREATE TABLE aliases(alias_key TEXT, identity TEXT, alias TEXT, source TEXT,
            updated_at TEXT, PRIMARY KEY(alias_key, identity, source));
        """
    )
    connection.execute("INSERT INTO identities VALUES ('toronto','Toronto','confirmed','registry_confirmed','[]','now')")
    connection.execute("INSERT INTO aliases VALUES ('toronto','toronto','Toronto','test','now')")
    connection.commit()
    connection.close()
    aliases, trusted, _display = __import__('build_canonical_face_baseline').load_trusted_identities(evidence)
    assert 'toronto' not in trusted
    assert 'toronto' not in aliases
