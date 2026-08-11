from __future__ import annotations

import os

from xair.core.context_store import RedisContextStore
from xair.core.runtime import XAIRRuntime

_redis = RedisContextStore() if os.environ.get("REDIS_URL") else RedisContextStore("")
_ctx, _ver, _trusted = _redis.snapshot()
runtime = XAIRRuntime(context=_ctx)
_context_version = _ver
_context_trusted = _trusted


def refresh_context() -> tuple[int, bool]:
    global _context_version, _context_trusted
    ctx, ver, trusted = _redis.snapshot()
    _context_version = ver
    _context_trusted = trusted
    runtime.update_context(ctx)
    return ver, trusted


def update_context_store(context: dict) -> int:
    ver = _redis.update(context)
    refresh_context()
    return ver


def context_meta() -> dict:
    return {
        "version": _context_version,
        "store_trusted": _context_trusted,
        "redis_required": _redis.redis_required,
        "redis_available": _redis.redis_available,
    }
