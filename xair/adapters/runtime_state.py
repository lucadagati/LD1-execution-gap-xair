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
    ctx, ver, pv, trusted = store.snapshot_full()
    if trusted:
        runtime.install_context_snapshot(ctx, ver, replace=True)
    return ctx, ver, pv, trusted


def update_context_store(patch: dict) -> tuple[int, bool]:
    ver = store.update(patch)
    _, ver_now, trusted = read_snapshot()
    return max(ver, ver_now), trusted
