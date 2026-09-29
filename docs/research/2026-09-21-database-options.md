# Database options for PicOrg

## Question

Would replacing SQLite improve PicOrg's durable evidence, embedding-cache, or run-artifact storage without weakening reliability or the current single-host trust boundary?

## Sources

| ID | URL/path | What it proves | Retrieved |
|---|---|---|---|
| S1 | https://sqlite.org/wal.html | WAL permits readers and a writer to proceed concurrently, but only one writer exists; WAL requires one host and needs checkpoint/backup care. | 2026-09-21 |
| S2 | https://sqlite.org/whentouse.html | SQLite is intended for local application storage and is also suitable for low/medium-traffic websites, analysis, and application file formats; client/server engines are preferable when centralization/concurrency/control dominate. | 2026-09-21 |
| S3 | https://www.postgresql.org/docs/current/mvcc.html | PostgreSQL provides MVCC, row-level locking, and concurrency controls for multiple sessions. | 2026-09-21 |
| S4 | https://www.postgresql.org/docs/current/backup.html | PostgreSQL supports dumps, filesystem backups, and continuous archiving/PITR, with corresponding operational requirements. | 2026-09-21 |
| S5 | https://duckdb.org/docs/current/connect/concurrency | DuckDB is primarily a single-process read/write engine; multi-process writes require a remote protocol or application coordination. | 2026-09-21 |
| S6 | https://fly.io/docs/litefs/ | LiteFS replicates SQLite, but remains pre-1.0 and adds distributed lease/backup risks. | 2026-09-21 |
| R1 | `identity_evidence_store.py:23-176` | PicOrg's evidence schema is relational metadata, assignment queues, markers, observations, matches, clusters, and pipeline runs. | 2026-09-21 |
| R2 | `identity_evidence_store.py:179-186` | The store already uses WAL, `synchronous=NORMAL`, and a busy timeout. | 2026-09-21 |
| R3 | `face_embedding_store.py:1-40` | Embeddings are a separate SQLite cache keyed by path/fingerprint, with WAL; it is not the mutable review authority. | 2026-09-21 |
| R4 | `docs/IDENTITY_EVIDENCE_STORE.md` | JSON remains interchange/audit data, SQLite is the mutable authority, and online SQLite backups are already part of the design. | 2026-09-21 |

## Findings

- [R1][R2][R4] PicOrg is a single-host local application with one durable metadata authority, a durable assignment queue, and read-heavy UI/scheduler access. The current schema and WAL configuration already address the normal reader/writer pattern.
- [S1] SQLite WAL gives concurrent readers with a single writer. It is not appropriate for a database file shared over a network filesystem; PicOrg should keep the database on local storage.
- [S2] The project fits SQLite's local application/file-format use case. The database files are modest compared with the media corpus, and the application already serializes state-changing operations.
- [R3] Embeddings are a cache and retrieval input, not the source of truth. Moving them to a server database would not by itself improve face accuracy; vector/index quality and incremental extraction are separate concerns.
- [S3] PostgreSQL would materially improve multi-process/multi-host write concurrency, row-level queue claiming, and future API workers. It would also introduce a server, credentials, migrations, monitoring, backup/restore, and a new network trust boundary.
- [S4] PostgreSQL's PITR and continuous archiving are stronger than a local file backup, but only if PicOrg operates and tests that backup infrastructure.
- [S5] DuckDB is a poor replacement for assignment queues or UI writes; its strengths are bulk analytical queries and reports, not many small concurrent transactions.
- [S6] SQLite replication layers solve a topology problem PicOrg does not currently have and add pre-1.0 operational risk.

## Recommendation

**Keep SQLite as the production database.** Confidence: high. Do not migrate while a face rebuild or matcher is running. Preserve the current split: evidence/queue in `identity_evidence.sqlite3`, embedding cache in `face_embeddings.sqlite3`, immutable audits/manifests on the filesystem, and face retrieval in the existing backend.

Before considering PostgreSQL, measure lock waits, transaction latency, WAL size/checkpoint starvation, and concurrent writers during normal UI plus scheduler operation. If the project becomes multi-host or has sustained competing writers, run a shadow PostgreSQL pilot for metadata only with row-count/checksum parity and a rollback to the SQLite snapshot.

## Open gaps

- No current production lock-wait/transaction-latency time series was inspected in this research.
- PostgreSQL deployment sizing, credential storage, firewall rules, and restore drills are not designed in this repository.
- No benchmark proves that a database change would improve face matching throughput; the current bottleneck is image/embedding extraction and vector search.
