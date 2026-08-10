#!/usr/bin/env python3
"""Optional InsightFace/SCRFD+ArcFace embedding backend.

This module is deliberately isolated from the default dlib pipeline so model
licenses and accuracy can be evaluated before changing production behavior.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


MODEL_ID = "insightface-buffalo_l-scrfd-arcface"


class InsightFaceBackend:
    def __init__(self, model_name: str = "buffalo_l", providers: list[str] | None = None) -> None:
        try:
            import insightface  # type: ignore
        except ImportError as exc:
            raise RuntimeError("InsightFace backend requires insightface and onnxruntime; install requirements-insightface.txt") from exc
        from insightface.app import FaceAnalysis  # type: ignore

        selected = providers or ["CUDAExecutionProvider", "CPUExecutionProvider"]
        self.app = FaceAnalysis(name=model_name, providers=selected)
        self.app.prepare(ctx_id=0)

    def embed(self, path: Path) -> tuple[list[float] | None, dict[str, Any]]:
        import cv2  # type: ignore

        image = cv2.imread(str(path))
        if image is None:
            raise ValueError(f"unable to decode image: {path}")
        faces = self.app.get(image)
        if not faces:
            return None, {"status": "no_face"}
        if len(faces) != 1:
            return None, {"status": "multi_face_deferred", "face_count": len(faces)}
        face = faces[0]
        embedding = getattr(face, "normed_embedding", None)
        if embedding is None:
            embedding = getattr(face, "embedding", None)
        if embedding is None:
            return None, {"status": "no_embedding"}
        return [float(value) for value in embedding], {"status": "embedded", "face_count": 1}
