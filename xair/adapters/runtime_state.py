from __future__ import annotations

import os

from xair.core.context_store import RedisContextStore
from xair.core.runtime import XAIRRuntime

store = RedisContextStore(os.environ.get("REDIS_URL", ""))
_ctx, _ver, _ = store.snapshot()
runtime = XAIRRuntime(
    context=_ctx,
    idempotency_retention_s=float(os.environ.get("XAIR_IDEMPOTENCY_RETENTION_S", "3600")),
)


def read_snapshot() -> tuple[dict, int, bool]:
    """Read (context, version, trusted) as one consistent pair from the store."""
    ctx, ver, _, trusted = read_snapshot_full()
    return ctx, ver, trusted


def read_snapshot_full() -> tuple[dict, int, dict[str, int], bool]:
    """Read (context, version, path_versions, trusted) as one document from the store."""
    return read_snapshot_timed()[:4]


def read_snapshot_timed():
    """As ``read_snapshot_full``, plus monotonic (lo, hi) bounds on the store read."""
    ctx, ver, pv, trusted, io = store.snapshot_timed()
    if trusted:
        runtime.install_context_snapshot(ctx, ver, replace=True)
    return ctx, ver, pv, trusted, io


def update_context_store(patch: dict) -> tuple[int, bool]:
    return update_context_store_timed(patch)[:2]


OPA_URL = os.environ.get("OPA_URL", "").rstrip("/")


def _replicate_to_opa(context: dict, version: int, path_versions: dict, trusted: bool) -> None:
    """Write-through into OPA's data store (external-baseline experiments): one
    document with the merged context, its global and per-path versions, and the
    trust flag, so the engine decides on a consistent copy and fails closed when
    XAIR's snapshot is untrusted."""
    import json
    import urllib.request
    doc = {"context": context, "version": version, "path_versions": path_versions, "trusted": bool(trusted)}
    req = urllib.request.Request(f"{OPA_URL}/v1/data/xairstate", data=json.dumps(doc).encode(),
                                 headers={"Content-Type": "application/json"}, method="PUT")
    with urllib.request.urlopen(req, timeout=2):
        pass


_opa_lock = __import__("threading").Lock()
opa_replication_errors = 0
opa_replications = 0


def update_context_store_timed(patch: dict):
    """Apply a context patch; return (version, trusted, monotonic bounds on its commit).

    With a policy engine configured, the update and its replication are
    serialized in this process, so the last copy the engine receives is the
    context after the last update. A failed replication is counted (exported
    by /v1/metrics) and the next update overwrites it; an untrusted snapshot is
    replicated as untrusted, so the engine denies."""
    global opa_replication_errors, opa_replications
    if not OPA_URL:
        ver, io = store.update_timed(patch)
        if store.partial_snapshots:   # shard layout: the update already returned the version; skip the full read
            return ver, (not store.redis_required) or store.redis_available, io
        _, ver_now, trusted = read_snapshot()
        return max(ver, ver_now), trusted, io
    with _opa_lock:
        ver, io = store.update_timed(patch)
        ctx, ver_now, pv, trusted = read_snapshot_full()
        try:
            _replicate_to_opa(ctx, ver_now, pv, trusted)
            opa_replications += 1
        except Exception:
            opa_replication_errors += 1
        return max(ver, ver_now), trusted, io


def read_snapshot_doc() -> dict:
    """Snapshot with path and predicate versions, trust flag, and monotonic read bounds."""
    ctx, ver, pv, trusted, io = read_snapshot_timed()
    return {"context": ctx, "version": ver, "path_versions": pv, "predicate_versions": store.predicate_versions,
            "trusted": trusted, "io": io}
