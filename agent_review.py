#!/usr/bin/env python3
"""Run a report-only local vision-agent quality review.

This helper deliberately does not perform face identification.  It selects a
small, deterministic set of uncertain face-review items, asks a local Ollama
or OpenAI-compatible endpoint for quality/occlusion observations, validates a
strict JSON response, and writes an append-safe audit report.  Agent output
cannot contain identity, decision, marker, or move fields.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


SCHEMA_VERSION = 1
DEFAULT_LIMIT = 50
DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_IMAGE_BYTES = 10 * 1024 * 1024
ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
FORBIDDEN_KEYS = {"identity", "canonical", "person", "name", "marker", "status", "move", "decision"}
OPINION_KEYS = {
    "face_count",
    "occlusion",
    "pose",
    "blur",
    "crop_quality",
    "possible_duplicate",
    "abstain",
    "reason_codes",
}
ENUMS = {
    "occlusion": {"none", "low", "medium", "high", "unknown"},
    "pose": {"frontal", "slight_profile", "profile", "upward", "downward", "unknown"},
    "blur": {"none", "low", "medium", "high", "unknown"},
    "crop_quality": {"good", "usable", "poor", "unknown"},
}


OPINION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "face_count": {"type": "integer", "minimum": 0, "maximum": 20},
        "occlusion": {"type": "string", "enum": sorted(ENUMS["occlusion"])},
        "pose": {"type": "string", "enum": sorted(ENUMS["pose"])},
        "blur": {"type": "string", "enum": sorted(ENUMS["blur"])},
        "crop_quality": {"type": "string", "enum": sorted(ENUMS["crop_quality"])},
        "possible_duplicate": {"type": "boolean"},
        "abstain": {"type": "boolean"},
        "reason_codes": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
    },
    "required": [
        "face_count",
        "occlusion",
        "pose",
        "blur",
        "crop_quality",
        "possible_duplicate",
        "abstain",
        "reason_codes",
    ],
}


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _audit_results(payload: object) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    rows = payload.get("results")
    if not isinstance(rows, list):
        report = payload.get("report")
        rows = report.get("results") if isinstance(report, dict) else None
    return [row for row in rows or [] if isinstance(row, dict) and row.get("path")]


def _numeric(item: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = item.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def uncertainty_score(item: dict[str, Any]) -> float:
    """Rank review-risk without using names or identity labels."""

    score = 0.0
    face_count = _numeric(item, "face_count", "faces")
    if face_count is not None and face_count != 1:
        score += 5.0
    quality = _numeric(item, "face_quality", "quality", "crop_quality_score")
    if quality is not None:
        score += max(0.0, 0.8 - quality) * 5.0
    similarity = _numeric(item, "min_similarity", "similarity", "match_similarity")
    if similarity is not None:
        score += max(0.0, 0.95 - similarity) * 8.0
    margin = _numeric(item, "match_margin", "margin", "similarity_margin")
    if margin is not None:
        score += max(0.0, 0.12 - margin) * 12.0
    cluster_size = _numeric(item, "cluster_size", "count")
    if cluster_size is not None:
        score += min(cluster_size, 500.0) / 100.0
    if item.get("multi_face_deferred") or item.get("low_quality"):
        score += 4.0
    return score


def select_items(rows: Iterable[dict[str, Any]], limit: int = DEFAULT_LIMIT) -> list[dict[str, Any]]:
    """Select a stable, bounded sample, preferring uncertainty and large clusters."""

    if limit < 0:
        raise ValueError("limit must be non-negative")
    candidates = []
    for item in rows:
        path = str(item.get("path") or "")
        digest = str(item.get("sha256") or item.get("face_sha256") or "")
        if len(digest) != 64:
            digest = hashlib.sha256(path.encode("utf-8")).hexdigest()
        candidates.append((uncertainty_score(item), digest, path, item))
    candidates.sort(key=lambda row: (-row[0], row[1], row[2]))
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _, _, path, item in candidates:
        if path in seen:
            continue
        selected.append(item)
        seen.add(path)
        if len(selected) >= limit:
            break
    return selected


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_endpoint(endpoint: str, allow_remote: bool) -> str:
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("endpoint must be an http(s) URL")
    host = parsed.hostname or ""
    if not allow_remote:
        # Hostnames are rejected by default to avoid DNS rebinding and
        # accidental uploads to a remote service. Use a literal loopback
        # address or localhost; --allow-remote is an explicit opt-in.
        if host.casefold() == "localhost":
            return endpoint.rstrip("/")
        try:
            if ipaddress_is_non_loopback(host):
                raise ValueError("refusing non-loopback agent endpoint; pass --allow-remote explicitly")
        except ValueError as exc:
            if "refusing non-loopback" in str(exc):
                raise
            raise ValueError("refusing non-loopback agent endpoint; pass --allow-remote explicitly") from exc
    return endpoint.rstrip("/")


def ipaddress_is_non_loopback(host: str) -> bool:
    import ipaddress

    address = ipaddress.ip_address(host)
    return not address.is_loopback


def _mime_type(path: Path) -> str:
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".bmp": "image/bmp",
        ".tif": "image/tiff",
        ".tiff": "image/tiff",
    }.get(path.suffix.casefold(), "application/octet-stream")


def validate_opinion(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("agent response must be an object")
    forbidden = FORBIDDEN_KEYS & {str(key).casefold() for key in value}
    if forbidden:
        raise ValueError(f"agent response contains forbidden fields: {sorted(forbidden)}")
    unknown = set(value) - OPINION_KEYS
    if unknown:
        raise ValueError(f"agent response contains unknown fields: {sorted(unknown)}")
    required = OPINION_KEYS
    if set(value) != required:
        raise ValueError("agent response must contain exactly the quality schema fields")
    face_count = value["face_count"]
    if not isinstance(face_count, int) or isinstance(face_count, bool) or not 0 <= face_count <= 20:
        raise ValueError("face_count must be an integer from 0 to 20")
    for key, choices in ENUMS.items():
        if value[key] not in choices:
            raise ValueError(f"{key} has an invalid value")
    for key in ("possible_duplicate", "abstain"):
        if not isinstance(value[key], bool):
            raise ValueError(f"{key} must be boolean")
    reasons = value["reason_codes"]
    if not isinstance(reasons, list) or len(reasons) > 12 or not all(isinstance(item, str) for item in reasons):
        raise ValueError("reason_codes must be a list of at most 12 strings")
    return value


def _prompt() -> str:
    return (
        "Inspect this image only for technical review quality. Do not identify, name, or infer any person. "
        "Return exactly the supplied JSON schema. Report face count, occlusion, pose, blur, crop quality, "
        "possible duplicate appearance, and whether to abstain. If uncertain, set abstain=true and explain with "
        "short reason_codes."
    )


def _call_ollama(endpoint: str, model: str, image: bytes, timeout: float) -> dict[str, Any]:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": _prompt(), "images": [base64.b64encode(image).decode("ascii")]}],
        "stream": False,
        "format": OPINION_SCHEMA,
        "options": {"temperature": 0},
    }
    request = Request(endpoint, data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    content = payload.get("message", {}).get("content") if isinstance(payload, dict) else None
    if not isinstance(content, str):
        raise ValueError("Ollama response did not contain message.content")
    return validate_opinion(json.loads(content))


def _call_openai_compatible(endpoint: str, model: str, image: bytes, mime: str, timeout: float) -> dict[str, Any]:
    body = {
        "model": model,
        "temperature": 0,
        "messages": [{"role": "user", "content": [{"type": "text", "text": _prompt()}, {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(image).decode('ascii')}"}}]}],
        "response_format": {"type": "json_schema", "json_schema": {"name": "picorg_quality_opinion", "strict": True, "schema": OPINION_SCHEMA}},
    }
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("LLM_ROUTE_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(endpoint, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    content = payload.get("choices", [{}])[0].get("message", {}).get("content") if isinstance(payload, dict) else None
    if not isinstance(content, str):
        raise ValueError("OpenAI-compatible response did not contain choices[0].message.content")
    return validate_opinion(json.loads(content))


def review_item(item: dict[str, Any], provider: str, endpoint: str | None, model: str, timeout: float, max_image_bytes: int, allow_remote: bool) -> dict[str, Any]:
    path = Path(str(item["path"]))
    started = time.monotonic()
    record: dict[str, Any] = {
        "path": str(path),
        "sha256": None,
        "face_index": item.get("face_index") if isinstance(item.get("face_index"), int) else None,
        "selection_score": round(uncertainty_score(item), 6),
        "provider": provider,
        "model": model,
        "status": "abstain",
        "opinion": None,
        "error": None,
    }
    try:
        if path.suffix.casefold() not in ALLOWED_IMAGE_EXTENSIONS:
            raise ValueError("unsupported image extension")
        if not path.is_file():
            raise FileNotFoundError(str(path))
        if path.stat().st_size > max_image_bytes:
            raise ValueError("image exceeds max_image_bytes")
        record["sha256"] = file_sha256(path)
        if provider == "mock":
            opinion = {
                "face_count": 0,
                "occlusion": "unknown",
                "pose": "unknown",
                "blur": "unknown",
                "crop_quality": "unknown",
                "possible_duplicate": False,
                "abstain": True,
                "reason_codes": ["mock_provider_no_inference"],
            }
        elif provider == "ollama":
            if not endpoint:
                raise ValueError("ollama provider requires endpoint")
            endpoint = _validate_endpoint(endpoint, allow_remote)
            opinion = _call_ollama(endpoint, model, path.read_bytes(), timeout)
        elif provider == "openai-compatible":
            if not endpoint:
                raise ValueError("openai-compatible provider requires endpoint")
            endpoint = _validate_endpoint(endpoint, allow_remote)
            opinion = _call_openai_compatible(endpoint, model, path.read_bytes(), _mime_type(path), timeout)
        else:
            raise ValueError(f"unsupported provider: {provider}")
        record["opinion"] = opinion
        record["status"] = "abstain" if opinion["abstain"] else "observed"
    except (OSError, ValueError, HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        record["error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
    record["latency_ms"] = round((time.monotonic() - started) * 1000.0, 3)
    return record


def run_review(audit: Path, output: Path, provider: str = "mock", endpoint: str | None = None, model: str = "gemma3:4b", limit: int = DEFAULT_LIMIT, timeout: float = DEFAULT_TIMEOUT, max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES, allow_remote: bool = False) -> dict[str, Any]:
    audit_bytes = audit.read_bytes()
    payload = json.loads(audit_bytes.decode("utf-8"))
    selected = select_items(_audit_results(payload), limit)
    records = [review_item(item, provider, endpoint, model, timeout, max_image_bytes, allow_remote) for item in selected]
    report = {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_audit": str(audit),
        "source_audit_sha256": hashlib.sha256(audit_bytes).hexdigest(),
        "source_audit_schema_version": payload.get("schema_version"),
        "provider": provider,
        "model": model,
        "privacy": "local quality observations only; no identity assignment, marker, decision, or file move",
        "identity_assignment": "forbidden",
        "selected": len(selected),
        "records": records,
    }
    _atomic_write(output, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Report-only local AI quality review for uncertain face candidates")
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--provider", choices=("mock", "ollama", "openai-compatible"), default="mock")
    parser.add_argument("--endpoint", help="local Ollama /api/chat or OpenAI-compatible /v1/chat/completions endpoint")
    parser.add_argument("--model", default=os.environ.get("OLLAMA_MODEL", "gemma3:4b"))
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--max-image-bytes", type=int, default=DEFAULT_MAX_IMAGE_BYTES)
    parser.add_argument("--allow-remote", action="store_true", help="allow a non-loopback endpoint (not recommended)")
    args = parser.parse_args()
    if args.limit < 0 or args.timeout <= 0 or args.max_image_bytes <= 0:
        parser.error("limit must be non-negative; timeout and max-image-bytes must be positive")
    report = run_review(args.audit, args.output, args.provider, args.endpoint, args.model, args.limit, args.timeout, args.max_image_bytes, args.allow_remote)
    print(json.dumps({"provider": report["provider"], "selected": report["selected"], "output": str(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
