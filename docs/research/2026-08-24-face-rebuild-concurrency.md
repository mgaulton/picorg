# Bounded concurrency for face rebuilds

Implementation status: PicOrg dlib extraction now supports bounded workers;
`runweb.sh` defaults to two. The photo_reorg SQLite-writer refactor remains
deferred.

## Question

Can face extraction and database rebuilds be parallelized without saturating the
rotational source disks or reducing matching accuracy?

## Sources

| Path | What it proves | Retrieved |
|---|---|---|
| `face_cluster_unmatched.py:191-257` | PicOrg dlib extraction reads and processes each image in one serial loop; checkpoints rewrite the cache from the parent process. | 2026-08-24 |
| `face_cluster_unmatched.py:260-317` | InsightFace extraction is also serial and owns one model instance. | 2026-08-24 |
| `/opt/photo_reorg/rebuild_face_database.py:145-181` | The photo_reorg rebuild iterates person folders and image files serially. | 2026-08-24 |
| `/opt/photo_reorg/rebuild_face_database.py:208-223` | Each detected face is written through the recognizer during image processing, so naïve concurrent workers would contend on SQLite. | 2026-08-24 |
| `/opt/photo_reorg/config_enhanced_accurate.json:37-42,78-82` | `batch_size` and `parallel_processing` are configured, but the rebuild entrypoint does not consume them. | 2026-08-24 |
| `run_face_group_batches.sh` | Existing batching provides resumability, but runs one batch at a time. | 2026-08-24 |

## Findings

- The primary PicOrg extraction path is CPU-heavy and serial, so a bounded
  process pool can improve throughput. Python threads should not be the first
  choice because dlib/decoder/model state is expensive and process isolation
  makes cache and error handling safer. [S1]
- Concurrent workers must not write the embedding JSON cache. Workers should
  return immutable results to one parent writer, which performs ordered,
  atomic checkpoints. Otherwise concurrent rewrites can lose records or corrupt
  the checkpoint. [S1]
- The photo_reorg rebuild needs a different design: workers may decode and
  detect faces, but one SQLite writer should commit batches in transactions.
  Calling `add_person_face` concurrently would create lock contention and make
  rebuild failure/recovery less deterministic. [S3][S4]
- The configured `parallel_processing` flag is currently aspirational for this
  entrypoint; turning it on alone will not speed the rebuild. [S5]
- Because the reference roots are rotational disks, high concurrency is likely
  to cause seek contention. Start with two workers and benchmark one, two, and
  four; choose the fastest setting that does not increase read errors or reduce
  extraction counts. [S1][S3]
- InsightFace/ONNX Runtime already has internal threading. Multiple model
  processes can oversubscribe CPU and memory, so use one model process first and
  tune runtime intra/inter-op threads before adding more processes. [S2]

## Recommendation

Implement bounded, opt-in concurrency in two stages:

1. **Completed:** `PICORG_FACE_WORKERS`/`--workers` is available in
   `face_cluster_unmatched.py`, defaulting to `1` and capped at `8`. Workers
   only read/decode/detect/embed; the parent owns cache merging, progress
   reporting, and atomic checkpoints. The two-worker smoke test and full
   project test suite pass. A 20-image local benchmark produced identical
   outputs with 1/2/4 workers: 24.81s/17.35s/16.15s, so `runweb.sh` now uses
   two by default.
2. **Compatibility path added:** `picorg_face_database.py` now performs bounded
   extraction with a single atomic SQLite writer and the compatible
   `face_encodings`/`person_metadata` schema. It is opt-in via
   `FACE_DATABASE_BACKEND=picorg`; the legacy photo_reorg backend remains the
   default until held-out parity is demonstrated.

Operational defaults should remain conservative:

```text
dlib on rotational source disks: 2 processes, 1 math thread each
InsightFace CPU: 1 process initially; tune ONNX threads separately
SSD-only staging: benchmark 2 and 4 processes
network/damaged roots: 1 process
```

Use `nice`/`ionice` for background rebuilds and stop competing intake or dedupe
jobs while a rebuild is active. Keep accuracy settings unchanged: do not trade
away face-size filtering, multi-face deferral, quality thresholds, or the
high-jitter confirmation path merely to gain throughput.

## Open gaps

- No controlled one-versus-two-versus-four worker benchmark has been run on the
  current disks.
- The legacy photo_reorg rebuild remains external for rollback/parity; PicOrg's
  native backend must pass the same accuracy and minimum-gallery gates before
  becoming the default.
- GPU availability and ONNX Runtime thread settings were not validated here.

## Verification plan

Run a representative 500-image benchmark with workers `1`, `2`, and `4`,
recording images/sec, RSS, disk wait, errors, embedded count, and no-face count.
Accept a setting only if it improves throughput without changing the output
counts or introducing new decode/database errors.
