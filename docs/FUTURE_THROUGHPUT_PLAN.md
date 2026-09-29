# Future throughput plan

Status: documented only. This plan is not enabled in production and does not
change the current single-process matcher.

## Goal

Increase face-baseline extraction and unmatched-media matching throughput while
protecting RAM, storage latency, and unrelated services. Accuracy and
reproducibility take precedence over speed.

## Current baseline

The current matcher is effectively single-process. Assigning more CPUs with
`taskset` alone will not make it faster. The existing fingerprint and
embedding caches, checkpoint files, and one-writer evidence-store model remain
the foundation for any future parallel mode.

## Proposed rollout

1. Benchmark a fixed, image-disjoint 1,000-file fixture with one, two, and
   four workers. Record files/second, peak RSS, disk throughput, I/O wait,
   error counts, and exact output parity.
2. Add bounded persistent workers, starting at two. Each worker receives a
   deterministic batch and writes a private temporary result; one coordinator
   performs SQLite/checkpoint writes.
3. Keep model, threshold, quality, and cache signatures identical to the
   single-worker path. Sort merged output by source path before publication.
4. Add adaptive throttling: reduce workers when available RAM, swap activity,
   disk I/O wait, or competing service load crosses configured limits.
5. Promote four workers only if the acceptance gates below pass. Otherwise
   retain two or the current single-worker implementation.

## Resource guardrails

Future worker launches should use bounded process counts and one math thread
per worker:

```text
workers: 2 (initial), maximum 4
OMP_NUM_THREADS=1
OPENBLAS_NUM_THREADS=1
MKL_NUM_THREADS=1
nice: 10
ionice: best-effort, priority 7
```

Workers must reuse fingerprints/embeddings, avoid duplicate directory scans,
and write through one SQLite coordinator. No unrestricted thread or process
fan-out is acceptable. CPU and memory cgroups/systemd limits should be used
where available so the UI, downloads, and other media services retain priority.

## Checkpoint and recovery requirements

Checkpoint every 250–500 files or 2–5 minutes. A checkpoint must contain the
source fingerprint, model/configuration signature, completed batch IDs, error
summary, and output checksum. A restart must process only missing or changed
batches. Partial results must never replace the last complete published run.

## Accuracy acceptance gates

Parallel mode may be enabled only when a held-out fixture demonstrates:

- exact assignment/output parity with the single-worker reference;
- no increase in false matches, deferred faces, or decoder errors;
- at least 1.5x throughput improvement;
- peak RAM within the agreed host budget;
- storage I/O wait below 20%; and
- no material impact on scheduled download or media services.

If any gate fails, disable parallel mode and retain the single-worker path.

## Rollback

Parallel execution must be opt-in and reversible through one configuration
switch. Rollback means stopping new workers, retaining valid checkpoints, and
rerunning the coordinator in single-worker mode. It must not delete the
embedding cache, evidence database, prior face database, or published audit.

## Owner and follow-up

Owner: PicOrg pipeline maintainer. Before implementation, add a benchmark
fixture and regression tests for deterministic merge, restart recovery,
resource throttling, and output parity. Do not adopt a new dependency solely
for parallelism; prefer the existing Python process pool, SQLite, OS priority
controls, and current caches.
