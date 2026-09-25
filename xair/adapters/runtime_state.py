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


def update_context_store_timed(patch: dict):
    """Apply a context patch; return (version, trusted, monotonic bounds on its commit)."""
    ver, io = store.update_timed(patch)
    _, ver_now, trusted = read_snapshot()
    return max(ver, ver_now), trusted, io
