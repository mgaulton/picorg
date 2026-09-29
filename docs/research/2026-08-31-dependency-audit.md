# PicOrg dependency reproducibility audit — 2026-08-31

## Scope

This audit covers PicOrg's Python project and its optional review, dlib face,
InsightFace, and UniFace environments. It does not cover model weights,
operating-system packages, or the protected Metadaily/Redditdaily projects.

## Reproducible inputs

- `pyproject.toml` declares the optional environments.
- `uv.lock` is the resolver lockfile and contains the selected versions and
  distribution hashes.
- `requirements-locked.txt` is an all-extras, hash-pinned export of
  `uv.lock`, suitable for pip installations that require a constraints file.
- `verify_dependency_lock.sh` checks both that the lockfile is current and
  that the exported hash manifest has not drifted.

Regenerate the export only after an intentional dependency update:

```bash
uv lock
uv export --frozen --all-extras --format requirements.txt \
  --no-header --no-annotate --output-file requirements-locked.txt
./verify_dependency_lock.sh
```

For an offline or restricted host, set `UV_CACHE_DIR` to a writable local
cache. Installation from the manifest must retain hash checking:

```bash
.venv/bin/pip install --require-hashes -r requirements-locked.txt
```

The manifest includes platform markers and all optional face stacks, so a
smaller environment can instead use `uv sync --frozen --extra review` (or the
specific extras needed) from the same lockfile.

## Vulnerability check

`pip-audit` 2.10.1 was run against the active `.venv` on 2026-08-31 and
reported **no known vulnerabilities**. Re-run after every lockfile update:

```bash
.venv/bin/python -m pip_audit --progress-spinner off
```

This result is a point-in-time package advisory check; it does not validate
model-weight licenses, native system libraries, or the security of downloaded
media. Automatic file moves remain disabled until the held-out face benchmark
passes its accuracy gates.
