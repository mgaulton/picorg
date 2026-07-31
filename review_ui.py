#!/usr/bin/env python3
"""Local review UI for unmatched image clusters.

The UI never moves files or edits the identity registry. Explicit decisions
are stored in a separate JSON ledger for later promotion and audit.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import mimetypes
import os
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from flask import Flask, g, jsonify, request, send_file
from werkzeug.exceptions import HTTPException

import picorg_sorter as sorter

LOGGER = logging.getLogger(__name__)


DEFAULT_AUDIT_ROOT = Path("/tmp/picorg_sorted_audit")
DEFAULT_DECISIONS = Path("/opt/picorg/review_decisions.json")
MEDIA_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".mp4", ".mov"}
FAMILIES = {"manual", "metadaily", "reddit_follow", "reddit_subreddit", "pscrape", "review"}
DECISION_STATUSES = {"pending", "confirmed", "rejected", "needs-evidence"}
DEFAULT_REGISTRY = Path("/opt/picorg/project_registry.json")
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8787
DEFAULT_OVERRIDES = Path("/opt/picorg/review_overrides.json")


def latest_audit(audit_root: Path = DEFAULT_AUDIT_ROOT) -> Path:
    files = sorted(audit_root.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not files:
        raise FileNotFoundError(f"No audit JSON files found under {audit_root}")
    return files[0]


def load_audit(path: Path) -> Dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise ValueError(f"Invalid audit payload: {path}")
    return payload


def _cluster_key(result: Dict[str, Any]) -> str:
    title = str(result.get("title") or Path(str(result.get("path") or "")).stem)
    base = sorter.gallery_base_title(title)
    return sorter.normalize_key(base) or sorter.normalize_key(title) or "unlabeled"


def _cluster_id(key: str, paths: Iterable[str]) -> str:
    material = "|".join([key, *sorted(paths)])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def build_clusters(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for result in payload["results"]:
        if not isinstance(result, dict) or result.get("canonical"):
            continue
        path = str(result.get("path") or "")
        if not path:
            continue
        grouped.setdefault(_cluster_key(result), []).append(result)

    clusters: List[Dict[str, Any]] = []
    for key, results in grouped.items():
        paths = [str(item["path"]) for item in results]
        title = str(results[0].get("title") or Path(paths[0]).stem)
        clusters.append(
            {
                "cluster_id": _cluster_id(key, paths),
                "key": key,
                "title": title,
                "count": len(results),
                "paths": paths,
                "sample_paths": paths[:12],
                "expected_identities": sorted(
                    {str(item["expected_identity"]) for item in results if item.get("expected_identity")}
                ),
                "source_roots": sorted({str(item.get("source_root") or "") for item in results}),
                "families": sorted({str(item.get("source_family") or "unknown") for item in results}),
            }
        )
    return sorted(clusters, key=lambda item: (-item["count"], item["title"].casefold()))


def _atomic_json_write(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def load_cluster_index(audit_path: Path, cache_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Load the cluster index from a cache, rebuilding only when the audit changes."""
    cache_path = cache_path or audit_path.with_suffix(".clusters.json")
    stamp = audit_path.stat()
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if cached.get("audit") == {"mtime_ns": stamp.st_mtime_ns, "size": stamp.st_size}:
            clusters = cached.get("clusters")
            if isinstance(clusters, list) and all(isinstance(item, dict) and "paths" in item for item in clusters):
                return clusters
    except (OSError, json.JSONDecodeError):
        pass
    clusters = build_clusters(load_audit(audit_path))
    _atomic_json_write(cache_path, {"schema_version": 1, "audit": {"mtime_ns": stamp.st_mtime_ns, "size": stamp.st_size}, "clusters": clusters})
    return clusters


def _read_overrides(path: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"moves": {}, "removed": []}
    return {"moves": payload.get("moves", {}), "removed": payload.get("removed", [])}


def _apply_overrides(clusters: List[Dict[str, Any]], overrides: Dict[str, Any]) -> None:
    by_id = {item["cluster_id"]: item for item in clusters}
    moves = {str(path): str(target) for path, target in (overrides.get("moves") or {}).items() if str(target) in by_id}
    removed = {str(path) for path in (overrides.get("removed") or [])}
    for cluster in clusters:
        cluster["paths"] = [path for path in cluster.get("paths", []) if path not in removed and moves.get(path, cluster["cluster_id"]) == cluster["cluster_id"]]
    for path, target in moves.items():
        if path not in by_id[target]["paths"]:
            by_id[target]["paths"].append(path)
    for cluster in clusters:
        cluster["paths"] = sorted(set(cluster["paths"]))
        cluster["count"] = len(cluster["paths"])
        cluster["sample_paths"] = cluster["paths"][:12]


def _read_decisions(path: Path) -> Dict[str, Dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    decisions = payload.get("decisions", []) if isinstance(payload, dict) else []
    return {str(item["cluster_id"]): item for item in decisions if isinstance(item, dict) and item.get("cluster_id")}


def _write_decisions(path: Path, decisions: Dict[str, Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": 1, "updated": datetime.now(timezone.utc).isoformat(), "decisions": list(decisions.values())}
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def export_confirmed_decisions(decisions_path: Path, registry_path: Path = DEFAULT_REGISTRY) -> int:
    """Promote only confirmed UI decisions into the project registry."""
    decisions = _read_decisions(decisions_path)
    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    entries = payload.setdefault("entries", [])
    existing = {(str(item.get("family")), sorter.normalize_key(str(item.get("canonical")))): item
                for item in entries if isinstance(item, dict)}
    promoted = 0
    for decision in decisions.values():
        if decision.get("status") != "confirmed":
            continue
        canonical = str(decision.get("identity") or "").strip()
        family = str(decision.get("family") or "review").strip()
        if not canonical or family not in (FAMILIES - {"review"}):
            continue
        aliases = [str(alias).strip() for alias in decision.get("aliases", []) if str(alias).strip()]
        key = (family, sorter.normalize_key(canonical))
        entry = existing.get(key)
        if entry is None:
            entry = {"family": family, "canonical": canonical, "aliases": [], "notes": ""}
            entries.append(entry)
            existing[key] = entry
        entry["aliases"] = sorted(set(entry.get("aliases", [])) | set(aliases))
        note = str(decision.get("notes") or "").strip()
        if note and note not in str(entry.get("notes") or ""):
            entry["notes"] = (str(entry.get("notes") or "").rstrip() + " [review-ui] " + note).strip()
        promoted += 1
    if promoted == 0:
        return 0
    fd, temp_name = tempfile.mkstemp(prefix=f".{registry_path.name}.", dir=str(registry_path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temp_name, registry_path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return promoted


def _public_cluster(cluster: Dict[str, Any], decision: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    result = {key: value for key, value in cluster.items() if key != "paths"}
    result["decision"] = decision
    return result


def create_app(audit_path: Path, decisions_path: Path = DEFAULT_DECISIONS, overrides_path: Path = DEFAULT_OVERRIDES) -> Flask:
    payload = load_audit(audit_path)
    clusters = load_cluster_index(audit_path)
    known_paths = {path for cluster in clusters for path in cluster.get("paths", [])}
    _apply_overrides(clusters, _read_overrides(overrides_path))
    by_id = {cluster["cluster_id"]: cluster for cluster in clusters}
    decisions = _read_decisions(decisions_path)
    allowed_roots = {Path(path).resolve() for cluster in clusters for path in cluster["source_roots"] if path}

    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
    write_lock = threading.RLock()

    @app.before_request
    def request_context() -> None:
        g.request_id = request.headers.get("X-Request-ID", "")[:80] or uuid.uuid4().hex

    @app.after_request
    def response_headers(response):
        response.headers["X-Request-ID"] = g.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'"
        if request.path.startswith("/api/") or request.path == "/media":
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(413)
    def request_too_large(_error):
        return jsonify({"error": "request body is too large", "request_id": g.request_id}), 413

    @app.errorhandler(HTTPException)
    def handle_http_error(error: HTTPException):
        if request.path.startswith("/api/"):
            return jsonify({"error": error.description, "request_id": g.request_id}), error.code
        return error

    @app.errorhandler(Exception)
    def handle_unexpected(error):
        LOGGER.exception("request failed request_id=%s path=%s", g.request_id, request.path, exc_info=error)
        if request.path.startswith("/api/"):
            return jsonify({"error": "internal server error", "request_id": g.request_id}), 500
        return "The review UI encountered an internal error.", 500

    @app.get("/")
    def index():
        return HTML_PAGE

    @app.get("/favicon.ico")
    def favicon():
        return "", 204

    @app.get("/healthz")
    def healthz():
        return jsonify({"status": "ok", "request_id": g.request_id})

    @app.get("/readyz")
    def readyz():
        ready = bool(clusters) and audit_path.is_file()
        return jsonify({"status": "ready" if ready else "not_ready", "clusters": len(clusters), "request_id": g.request_id}), 200 if ready else 503

    @app.get("/api/summary")
    def summary():
        report = payload.get("report") or {}
        compact_report = {key: value for key, value in report.items() if key not in {"gallery_sets", "results"}}
        return jsonify({"audit": str(audit_path), "report": compact_report, "clusters": len(clusters), "decisions": len(decisions), "cache": str(audit_path.with_suffix(".clusters.json"))})

    @app.get("/api/clusters")
    def list_clusters():
        try:
            page = max(int(request.args.get("page", 1)), 1)
            page_size = min(max(int(request.args.get("page_size", request.args.get("limit", 50))), 1), 200)
        except ValueError:
            page, page_size = 1, 50
        query = str(request.args.get("q") or "").strip().casefold()
        status = str(request.args.get("status") or "").strip()
        filtered = [cluster for cluster in clusters if (not query or query in cluster["title"].casefold() or query in cluster["key"].casefold()) and (not status or (decisions.get(cluster["cluster_id"], {}).get("status", "pending") == status))]
        start = (page - 1) * page_size
        return jsonify({
            "total": len(filtered), "page": page, "page_size": page_size,
            "has_next": start + page_size < len(filtered),
            "clusters": [_public_cluster(cluster, decisions.get(cluster["cluster_id"])) for cluster in filtered[start:start + page_size]],
        })

    @app.get("/api/clusters/<cluster_id>")
    def get_cluster(cluster_id: str):
        cluster = by_id.get(cluster_id)
        if cluster is None:
            return jsonify({"error": "unknown cluster"}), 404
        result = _public_cluster(cluster, decisions.get(cluster_id))
        result["paths"] = cluster["paths"]
        return jsonify(result)

    @app.post("/api/clusters/<cluster_id>/members")
    def update_member(cluster_id: str):
        if cluster_id not in by_id:
            return jsonify({"error": "unknown cluster"}), 404
        body = request.get_json(silent=True) or {}
        path = str(body.get("path") or "")
        action = str(body.get("action") or "").strip().lower()
        target_id = str(body.get("target_cluster_id") or cluster_id)
        if path not in known_paths:
            return jsonify({"error": "path is not in the audit index"}), 404
        if action not in {"add", "remove"}:
            return jsonify({"error": "action must be add or remove"}), 400
        overrides = _read_overrides(overrides_path)
        moves = {str(key): str(value) for key, value in (overrides.get("moves") or {}).items()}
        removed = {str(value) for value in (overrides.get("removed") or [])}
        if action == "add":
            if target_id not in by_id:
                return jsonify({"error": "unknown target cluster"}), 404
            removed.discard(path)
            moves[path] = target_id
        else:
            removed.add(path)
            moves.pop(path, None)
        with write_lock:
            _atomic_json_write(overrides_path, {"schema_version": 1, "updated": datetime.now(timezone.utc).isoformat(), "moves": moves, "removed": sorted(removed)})
            _apply_overrides(clusters, {"moves": moves, "removed": removed})
        return jsonify({"saved": True, "path": path, "action": action, "cluster": _public_cluster(by_id[cluster_id], decisions.get(cluster_id))}), 201

    @app.get("/api/identities")
    def identities():
        catalog, _, _, _, _ = sorter.load_identity_catalog()
        return jsonify([{"canonical": item.canonical, "family": item.family} for item in catalog])

    @app.get("/api/decisions")
    def list_decisions():
        counts = {status: sum(item.get("status", "pending") == status for item in decisions.values()) for status in DECISION_STATUSES}
        return jsonify({"counts": counts, "decisions": list(decisions.values())})

    @app.delete("/api/decisions/<cluster_id>")
    def delete_decision(cluster_id: str):
        if cluster_id not in decisions:
            return jsonify({"error": "decision not found"}), 404
        with write_lock:
            del decisions[cluster_id]
            _write_decisions(decisions_path, decisions)
        return jsonify({"deleted": cluster_id})

    @app.get("/api/export-preview")
    def export_preview():
        promotable = [item for item in decisions.values() if item.get("status") == "confirmed" and item.get("family") in (FAMILIES - {"review"})]
        skipped = [item for item in decisions.values() if item not in promotable]
        return jsonify({"promotable": promotable, "skipped": skipped})

    @app.post("/api/decisions")
    def save_decision():
        body = request.get_json(silent=True) or {}
        cluster_id = str(body.get("cluster_id") or "")
        identity = str(body.get("identity") or "").strip()
        family = str(body.get("family") or "review").strip()
        if cluster_id not in by_id:
            return jsonify({"error": "unknown cluster"}), 404
        if not identity or len(identity) > 120 or any(char in identity for char in "\r\n"):
            return jsonify({"error": "identity is required and must be one line"}), 400
        if family not in FAMILIES:
            return jsonify({"error": "invalid family"}), 400
        status = str(body.get("status") or "pending").strip()
        if status not in DECISION_STATUSES:
            return jsonify({"error": "invalid decision status"}), 400
        decision = {
            "cluster_id": cluster_id,
            "identity": identity,
            "family": family,
            "status": status,
            "aliases": [str(alias).strip() for alias in body.get("aliases", []) if str(alias).strip()][:20],
            "notes": str(body.get("notes") or "").strip()[:1000],
            "sample_paths": by_id[cluster_id]["sample_paths"],
            "count": by_id[cluster_id]["count"],
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        with write_lock:
            decisions[cluster_id] = decision
            _write_decisions(decisions_path, decisions)
        return jsonify(decision), 201

    @app.post("/api/decisions/bulk")
    def save_bulk_decisions():
        body = request.get_json(silent=True) or {}
        cluster_ids = [str(item) for item in body.get("cluster_ids", [])]
        unknown = [item for item in cluster_ids if item not in by_id]
        if unknown or not cluster_ids:
            return jsonify({"error": "cluster_ids must identify existing clusters"}), 400
        identity = str(body.get("identity") or "").strip()
        family = str(body.get("family") or "review").strip()
        status = str(body.get("status") or "pending").strip()
        if not identity or family not in FAMILIES or status not in DECISION_STATUSES:
            return jsonify({"error": "identity, family, or status is invalid"}), 400
        for cluster_id in cluster_ids:
            decisions[cluster_id] = {
                "cluster_id": cluster_id,
                "identity": identity,
                "family": family,
                "status": status,
                "aliases": [],
                "notes": str(body.get("notes") or "").strip()[:1000],
                "sample_paths": by_id[cluster_id]["sample_paths"],
                "count": by_id[cluster_id]["count"],
                "saved_at": datetime.now(timezone.utc).isoformat(),
            }
        with write_lock:
            _write_decisions(decisions_path, decisions)
        return jsonify({"saved": len(cluster_ids), "cluster_ids": cluster_ids}), 201

    @app.get("/media")
    def media():
        raw_path = str(request.args.get("path") or "")
        path = Path(raw_path).resolve()
        if not any(path == root or root in path.parents for root in allowed_roots):
            return jsonify({"error": "media path is outside the audit roots"}), 403
        if path.suffix.lower() not in MEDIA_EXTENSIONS or not path.is_file():
            return jsonify({"error": "media not found"}), 404
        return send_file(path, mimetype=mimetypes.guess_type(path.name)[0] or "application/octet-stream")

    return app


HTML_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Picorg candidate review</title>
<style>
body{font:14px system-ui;margin:0;color:#202124;background:#f6f7f9}main{display:grid;grid-template-columns:330px 1fr;min-height:100vh}.side{background:#20252b;color:#f5f7fa;padding:18px;overflow:auto}.side h1{font-size:20px}.cluster{display:block;width:100%;text-align:left;background:#2d343c;color:inherit;border:1px solid #46505a;border-radius:6px;padding:10px;margin:7px 0;cursor:pointer}.cluster.active{border-color:#7cc4ff}.cluster small{display:block;color:#b7c0ca;margin-top:3px}.detail{padding:24px;max-width:1100px}.meta{background:white;padding:12px;border-radius:8px;margin-bottom:16px}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}.grid img{width:100%;height:160px;object-fit:cover;background:#ddd;border-radius:6px}.form{background:white;padding:16px;border-radius:8px;margin-top:16px;display:grid;gap:9px;max-width:650px}input,select,textarea,button{font:inherit;padding:8px}button{cursor:pointer}button:disabled{cursor:wait;opacity:.6}:focus-visible{outline:3px solid #7cc4ff;outline-offset:2px}.status{min-height:22px;color:#176b37}.muted{color:#68737d}@media(max-width:700px){main{display:block}.side{position:static}.detail{padding:14px}.grid{grid-template-columns:repeat(auto-fill,minmax(110px,1fr))}.grid img{height:120px}}@media(prefers-reduced-motion:reduce){*{scroll-behavior:auto!important;transition:none!important}}
</style></head><body><main><aside class="side"><h1>Candidate clusters</h1><div id="summary" class="muted" aria-live="polite">Loading…</div><label for="filter">Search clusters</label><input id="filter" placeholder="Filter clusters" oninput="loadPage(true)"><div id="list" aria-live="polite"></div><div><button id="nextPage" type="button" onclick="loadPage(false)">Next page</button></div></aside><section class="detail" aria-live="polite"><div id="detail"><h2>Select a cluster</h2><p class="muted">Review the images, then record an explicit identity decision.</p></div></section></main>
<script>
let clusters=[], selected=null;
let page=1, hasNext=false;
async function init(){let a=await fetch('/api/summary').then(x=>x.json());document.querySelector('#summary').textContent=`${a.clusters} clusters · ${a.decisions} saved decisions`;await loadPage(true)}
async function loadPage(reset){if(reset){page=1;clusters=[]}let q=encodeURIComponent(document.querySelector('#filter').value);let c=await fetch(`/api/clusters?page=${page}&page_size=50&q=${q}`).then(x=>x.json());clusters=reset?c.clusters:clusters.concat(c.clusters);hasNext=c.has_next;document.querySelector('#summary').textContent=`${c.total} matching clusters · loaded ${clusters.length}`;renderList();if(reset&&clusters[0]){select(clusters[0].cluster_id);page=2}else if(!reset&&hasNext)page++}
function renderList(){let q=document.querySelector('#filter').value.toLowerCase();document.querySelector('#list').innerHTML=clusters.filter(x=>(x.title+' '+x.expected_identities.join(' ')).toLowerCase().includes(q)).map(x=>`<button class="cluster ${selected===x.cluster_id?'active':''}" onclick="select('${x.cluster_id}')"><b>${esc(x.title)}</b><small>${x.count} files · ${x.decision?'assigned: '+esc(x.decision.identity):'unreviewed'}</small></button>`).join('')}
async function select(id){selected=id;renderList();let x=await fetch('/api/clusters/'+id).then(r=>r.json());let images=x.sample_paths.map(p=>`<a href="/media?path=${encodeURIComponent(p)}" target="_blank"><img src="/media?path=${encodeURIComponent(p)}" loading="lazy" title="${esc(p)}"></a>`).join('');document.querySelector('#detail').innerHTML=`<h2>${esc(x.title)}</h2><div class="meta"><b>${x.count} files</b><br>Expected labels: ${esc(x.expected_identities.join(', ')||'none')}<br>Sources: ${esc(x.source_roots.join(', '))}</div><div class="grid">${images}</div><form class="form" onsubmit="save(event)"><label>Identity <input id="identity" required value="${esc(x.decision?.identity||'')}" placeholder="canonical identity"></label><label>Family <select id="family">${['manual','metadaily','reddit_follow','reddit_subreddit','pscrape','review'].map(f=>`<option ${x.decision?.family===f?'selected':''}>${f}</option>`).join('')}</select></label><label>Aliases, one per line<textarea id="aliases" placeholder="optional aliases">${esc((x.decision?.aliases||[]).join('\n'))}</textarea></label><label>Evidence / notes<textarea id="notes" placeholder="Why this assignment is supported">${esc(x.decision?.notes||'')}</textarea></label><button>Save explicit decision</button><div class="status" id="status"></div></form>`}
async function save(e){e.preventDefault();let r=await fetch('/api/decisions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cluster_id:selected,identity:identity.value,family:family.value,aliases:aliases.value.split('\n'),notes:notes.value})});let d=await r.json();status.textContent=r.ok?'Saved decision for '+d.count+' files':d.error;if(r.ok){let x=clusters.find(x=>x.cluster_id===selected);x.decision=d;renderList()}}
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}init();
async function save(e){e.preventDefault();let r=await fetch('/api/decisions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cluster_id:selected,identity:identity.value,family:family.value,status:(document.querySelector('#decisionStatus')||{value:'pending'}).value,aliases:aliases.value.split('\\n'),notes:notes.value})});let d=await r.json();status.textContent=r.ok?'Saved decision for '+d.count+' files':d.error;if(r.ok){let x=clusters.find(x=>x.cluster_id===selected);x.decision=d;renderList()}}
const statusObserver=new MutationObserver(()=>{let form=document.querySelector('.form');if(form&&!document.querySelector('#decisionStatus')){let label=document.createElement('label');label.textContent='Status ';let select=document.createElement('select');select.id='decisionStatus';['pending','needs-evidence','confirmed','rejected'].forEach(v=>{let option=document.createElement('option');option.value=v;option.textContent=v;select.appendChild(option)});label.appendChild(select);form.insertBefore(label,form.children[1])}});
statusObserver.observe(document.querySelector('#detail'),{childList:true});
const memberObserver=new MutationObserver(()=>{let grid=document.querySelector('#detail .grid');if(!grid||document.querySelector('#memberTools'))return;let tools=document.createElement('div');tools.id='memberTools';tools.className='meta';tools.innerHTML='<b>Cluster membership</b><br><select id="targetCluster">'+clusters.map(c=>'<option value="'+c.cluster_id+'">'+esc(c.title)+' ('+c.count+')</option>').join('')+'</select><button onclick="updateMember(\'add\')">Move selected image</button><button onclick="updateMember(\'remove\')">Remove selected image</button><p class="muted">Click an image first, then use these controls.</p>';document.querySelector('#detail').insertBefore(tools,grid);grid.querySelectorAll('a').forEach(a=>a.onclick=()=>{window.memberPath=new URL(a.href).searchParams.get('path');document.querySelector('#memberTools p').textContent='Selected: '+window.memberPath});});
memberObserver.observe(document.querySelector('#detail'),{childList:true,subtree:true});
async function updateMember(action){if(!window.memberPath){alert('Select an image first');return}let body={path:window.memberPath,action};if(action==='add')body.target_cluster_id=document.querySelector('#targetCluster').value||selected;let r=await fetch('/api/clusters/'+selected+'/members',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});let d=await r.json();if(!r.ok){alert(d.error||'Membership update failed');return}select(selected)}
let loadGeneration=0;
async function loadPage(reset){let generation=++loadGeneration;if(reset){page=1;clusters=[];document.querySelector('#list').textContent='Loading…'}let q=encodeURIComponent(document.querySelector('#filter').value);try{let r=await fetch(`/api/clusters?page=${page}&page_size=50&q=${q}`,{headers:{Accept:'application/json'}});let c=await r.json();if(!r.ok)throw new Error(c.error||`Request failed (${r.status})`);if(generation!==loadGeneration)return;clusters=reset?c.clusters:clusters.concat(c.clusters);hasNext=c.has_next;document.querySelector('#summary').textContent=`${c.total} matching clusters · loaded ${clusters.length}`;let next=document.querySelector('#nextPage');next.disabled=!hasNext;next.textContent=hasNext?'Next page':'No more pages';renderList();if(reset&&clusters[0]){select(clusters[0].cluster_id);page=2}else if(!reset&&hasNext)page++}catch(error){if(generation!==loadGeneration)return;document.querySelector('#list').innerHTML=`<p role="alert">${esc(error.message)} <button type="button" onclick="loadPage(${reset})">Retry</button></p>`}}
async function init(){try{let r=await fetch('/api/summary',{headers:{Accept:'application/json'}});let a=await r.json();if(!r.ok)throw new Error(a.error||`Request failed (${r.status})`);document.querySelector('#summary').textContent=`${a.clusters} clusters · ${a.decisions} saved decisions`;await loadPage(true)}catch(error){document.querySelector('#summary').innerHTML=`<span role="alert">${esc(error.message)}</span>`;document.querySelector('#list').innerHTML='<button type="button" onclick="init()">Retry loading</button>'}}
window.addEventListener('unhandledrejection',event=>{let detail=document.querySelector('#detail');if(detail)detail.innerHTML=`<p role="alert">${esc(event.reason?.message||'The request failed.')} <button type="button" onclick="select(selected)">Retry</button></p>`});
</script></body></html>"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, help="audit JSON; defaults to newest file in /tmp/picorg_sorted_audit")
    parser.add_argument("--decisions", type=Path, default=DEFAULT_DECISIONS)
    parser.add_argument("--export-registry", action="store_true", help="promote confirmed decisions into the registry")
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--host", default=DEFAULT_HOST, help="bind address; use 127.0.0.1 for local-only access")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    if args.export_registry:
        print(f"promoted {export_confirmed_decisions(args.decisions, args.registry)} confirmed decisions")
        return 0
    audit_path = args.audit or latest_audit()
    create_app(audit_path, args.decisions).run(host=args.host, port=args.port, debug=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
