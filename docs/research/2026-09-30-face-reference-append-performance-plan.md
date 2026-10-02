# Face reference append performance plan

Status: resumable worker implemented and active; controlled concurrency benchmark remains pending. Fixed Haar cascade lookup and disabled the incompatible MediaPipe API; read-only initialization confirms Haar, face_recognition, and InsightFace load, and a previously labeled JPEG yields detections.

## Goal

Reduce the time required to append face references while preserving the current detector, identity labels, quality checks, and matching accuracy. Make the append resumable before adding parallel workers.

## Baseline observed on 2026-09-30

At 08:49 UTC, the host status file reported 22,300 of 96,786 paths processed in 46,149 seconds, with 13,039 face detections and no database completion result. This is about 1,740 paths per hour, or roughly 43 hours remaining at that rate.

The process could see CPUs 0–47 and averaged about 488% CPU (roughly 4.9 logical CPUs). It had 327 threads, 3.0 GB resident memory, and a 5.1 GB high-water mark. Host load was about 25 on 48 logical CPUs, with 27 GB memory available and 4.3 GB swap in use. These are time-specific measurements; recheck before tuning concurrency.

## Why each path is expensive

- `append_face_reference_delta.py` loops over paths serially and invokes `_process_image_for_person` once per path.
- `_process_image_for_person` reads the image, runs `detect_faces_multi_model`, and computes an encoding as a validity check.
- For each detection, `HighAccuracyFaceRecognizer.add_person_face` reads the same image again and computes its own encoding and quality score. The first encoding is not passed to the recognizer.
- Each accepted face opens a SQLite connection, writes the encoding and person metadata, and commits separately.
- The detector supports Haar, profile Haar, face-recognition HOG, MediaPipe, and InsightFace. It reads switches from a `face_detection` config section, while the current config’s related switches live under `face_recognition`; verify the effective model set before changing it.
- The append loop has no per-path resume checkpoint. On SIGTERM it restores the database backup made before the run, discarding all additions from that run.

Relevant code: `append_face_reference_delta.py`, `/opt/photo_reorg/rebuild_face_database.py`, `/opt/photo_reorg/comprehensive_face_detection.py`, and `/opt/photo_reorg/high_accuracy_face_recognition.py`.

## Planned changes

1. **Protect the active run.** Captured and validated a SQLite online-backup at the 23,200-path status checkpoint. Restored it after stopping the legacy worker, whose SIGTERM handler reverted the database.
2. **Make detector configuration explicit.** Fixed both detector initializers to resolve Haar cascades from the system OpenCV asset path and reject empty classifiers. The config explicitly disables the incompatible MediaPipe `solutions` API; the recognizer no longer initializes an unused DNN model that is incompatible with installed OpenCV. Existing thresholds are unchanged. A labeled-image detect-and-encode smoke check returns 3 detections and 1 encoding.
3. **Remove repeated image and encoding work.** Implemented a single image decode and one accepted recognizer encoding per retained face. Both detector and recognizer quality thresholds remain applied.
4. **Batch database writes.** Implemented one SQLite writer with transactions every 100 paths; person counts, average quality, and latest stored encoding are updated in the same transaction.
5. **Add safe checkpoints before parallelism.** Implemented a manifest-hash-bound JSONL path journal, database-backed duplicate protection, graceful SIGTERM checkpointing, and retryable `missing`/`error` outcomes. Seeded the resumed worker with 23,200 manifest paths; later graceful checkpoints extended the journal to 23,489 paths. The original checkpoint remains at `.cache/picorg/high_accuracy_faces.db.checkpoint-23200`; it passed `PRAGMA integrity_check`.
6. **Benchmark controlled concurrency.** Deferred until host load falls. A longer interval processed 5,699 paths in 12,724 seconds (about 1,613/hour), slightly below the earlier 1,740/hour baseline; the 100-path sample at 2,280/hour was too short to predict total time. At the longer-run rate, about 66,000 remaining paths project to roughly 41 hours. The host then had load averages 42/54/45 on 48 logical CPUs, 21 GiB available RAM, and `/opt` at 98% full. Do not add workers under that load; recheck capacity after it eases.
7. **Validate accuracy and records.** Pending. Compare the old and new paths on a representative labeled sample and reconcile accepted embeddings, duplicates, identity grouping, missing/error counts, and final database integrity. No thresholds were lowered.

## Acceptance criteria

- A stopped append resumes from its last committed checkpoint and does not lose earlier rows or add duplicate encodings.
- The selected linked paths retain their shared canonical identity IDs.
- The face database passes `PRAGMA integrity_check`; per-identity counts and path coverage reconcile with the append manifest.
- The measured throughput improves on the same sample with no regression in the agreed accuracy measures.
- Worker count and native thread limits are chosen from measured throughput and memory, not from the host’s CPU count alone.

## CPU assessment

More CPU can help only after image-level parallelism is introduced. The current loop is serial across paths, while its model stack already uses multiple CPU threads. The host has 48 logical CPUs available, but the worker’s 327 threads and the host’s existing load make unconstrained process multiplication risky. Remove duplicate work first, cap native threads, then measure two workers before considering more.

## Resume QA findings (2026-09-30)

- The existing photo_reorg face DB had 1,881 rows whose `image_path` pointed through temporary coalescing symlinks. The first consolidation relink only matched literal source paths, so its face DB update summary was empty. The relinker now follows those symlinks, applies the completed move map for sorted media, and stores direct protected-source paths for MetaDaily/RedditDaily references.
- The same relink now canonicalizes confirmed linked identity aliases in `face_encodings` and rebuilds derived `person_metadata`. The live correction updated 1,881 paths and 1,061 labels; integrity and per-person counts passed. A rollback snapshot is `.cache/picorg/high_accuracy_faces.db.before-reference-relink-20260930T1322Z`.
- OpenCV rejects some valid GIF disposal methods. The append worker now falls back to Pillow’s first frame; the previously failing 92-frame GIF decodes successfully with the fallback. Other sampled `.jpg`/`.png` files had no valid image signature in either OpenCV or Pillow and remain recorded as decode errors. One RedditDaily source path was still absent at the last check.
- The configured Haar path previously pointed inside a virtualenv where the cascade XMLs were absent, even though distro assets were installed. Both the comprehensive detector and high-accuracy recognizer now resolve and validate those classifiers. The environment’s MediaPipe package has no `solutions` API, so that optional model is explicitly disabled. The recognizer never called its DNN model; its incompatible model load is removed while HOG and Haar stay active.
- Several sampled `.jpg`/`.png` files and a numbered `.gif` sequence have random bytes rather than image signatures. The GIF fallback now treats unreadable payloads as normal decode failures instead of stack traces. No files were rewritten because no verified source copy was found. The append remains active; do not increase worker concurrency while host load is high and `/opt` is 98% full.
