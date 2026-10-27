"""Read-only, on-demand comparison against configured generic identity profiles."""

from __future__ import annotations

import sqlite3
import json
import os
from pathlib import Path
from typing import Any, Iterable

import numpy as np


MODEL_ID = "dlib-face-recognition-small-v1"
VECTOR_SIZE = 128
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}


def load_human_mark_notes(path: Path | None = None) -> dict[str, list[dict[str, Any]]]:
    """Read optional human-entered mark notes for reference examples."""
    path = path or Path(os.environ.get(
        "PICORG_VISUAL_MARK_EVIDENCE", "/opt/picorg/.cache/picorg/visual-mark-evidence.json"
    ))
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    by_path = payload.get("by_path", {}) if isinstance(payload, dict) else {}
    if not isinstance(by_path, dict):
        return {}
    return {
        str(media_path): [item for item in values if isinstance(item, dict)]
        for media_path, values in by_path.items()
        if isinstance(values, list)
    }


def load_profiles(
    database: Path,
    prefixes: Iterable[str],
    allowed_identities: Iterable[str] = (),
) -> dict[str, dict[str, Any]]:
    """Load only generic identities selected by the configured picker prefixes."""
    if not database.is_file():
        raise FileNotFoundError(f"face database is unavailable: {database}")
    normalized_prefixes = tuple(value.strip().casefold() for value in prefixes if value.strip())
    allowed_names = {
        str(value).strip().casefold(): str(value).strip()
        for value in allowed_identities
        if str(value).strip()
    }
    if not normalized_prefixes or not allowed_names:
        return {}
    uri = f"{database.resolve().as_uri()}?mode=ro"
    profiles: dict[str, dict[str, Any]] = {}
    with sqlite3.connect(uri, uri=True, timeout=10) as connection:
        names = [
            str(row[0])
            for row in connection.execute(
                "SELECT DISTINCT person_name FROM face_encodings WHERE model_used = ?",
                (MODEL_ID,),
            )
            if str(row[0]).casefold().startswith(normalized_prefixes)
            and str(row[0]).casefold() in allowed_names
        ]
        for offset in range(0, len(names), 400):
            batch = names[offset : offset + 400]
            placeholders = ",".join("?" for _ in batch)
            rows = connection.execute(
                f"SELECT person_name, image_path, encoding, face_quality FROM face_encodings "
                f"WHERE model_used = ? AND person_name IN ({placeholders})",
                (MODEL_ID, *batch),
            )
            for name, raw_path, blob, quality in rows:
                vector = np.frombuffer(blob, dtype=np.float64)
                if vector.shape != (VECTOR_SIZE,):
                    continue
                canonical = allowed_names.get(str(name).casefold(), str(name))
                profile = profiles.setdefault(canonical, {"vectors": [], "paths": [], "qualities": []})
                profile["vectors"].append(vector.copy())
                profile["paths"].append(str(raw_path))
                profile["qualities"].append(max(0.0, min(1.0, float(quality or 0.0))))
    return {name: profile for name, profile in profiles.items() if profile["vectors"]}


def compare_image(path: Path, profiles: dict[str, dict[str, Any]], *, limit: int = 5) -> dict[str, Any]:
    """Compare every detected face with every stored generic profile.

    Returned distances are rankings, not calibrated probabilities or
    assignment decisions. Callers must keep the result review-only.
    """
    if path.suffix.casefold() not in IMAGE_SUFFIXES:
        raise ValueError("on-demand face comparison supports still images only")
    if not profiles:
        return {"face_count": 0, "faces": [], "profile_count": 0}

    import face_recognition

    image = face_recognition.load_image_file(str(path))
    locations = face_recognition.face_locations(image, number_of_times_to_upsample=1, model="hog")
    encodings = face_recognition.face_encodings(
        image,
        known_face_locations=locations,
        num_jitters=1,
        model="small",
    )
    faces = []
    for face_index, encoding in enumerate(encodings):
        ranked = []
        for identity, profile in profiles.items():
            vectors = np.asarray(profile["vectors"], dtype=np.float64)
            distances = np.linalg.norm(vectors - np.asarray(encoding, dtype=np.float64), axis=1)
            best_index = int(np.argmin(distances))
            nearest_count = min(3, len(distances))
            closest_indices = np.argpartition(distances, nearest_count - 1)[:nearest_count]
            examples = [profile["paths"][best_index]]
            # Add one separate high-quality example when available, so the
            # reviewer can check whether the profile itself is coherent.
            quality_order = np.argsort(np.asarray(profile["qualities"]))[::-1]
            for sample_index in quality_order:
                sample_path = profile["paths"][int(sample_index)]
                if sample_path not in examples:
                    examples.append(sample_path)
                    break
            ranked.append({
                "identity": identity,
                "distance": round(float(np.mean(distances[closest_indices])), 5),
                "nearest_distance": round(float(distances[best_index]), 5),
                "scoring_method": f"mean of {nearest_count} nearest references",
                "reference_count": len(profile["paths"]),
                "examples": examples,
            })
        ranked.sort(key=lambda item: (item["distance"], item["identity"].casefold()))
        faces.append({"face_index": face_index, "candidates": ranked[: max(1, min(int(limit), 10))]})
    return {"face_count": len(locations), "faces": faces, "profile_count": len(profiles)}
