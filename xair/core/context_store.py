from __future__ import annotations

import json
import os
from typing import Any

from xair.core.deep_merge import deep_merge

try:
    import redis
except ImportError:
    redis = None


class RedisContextStore:
    """Redis-backed or in-memory context snapshot with monotonic versioning."""

    def __init__(self, url: str | None = None) -> None:
        self._url = url or os.environ.get("REDIS_URL", "")
        self._client = None
        self._memory: dict[str, Any] = {}
        self._version = 0
        self._redis_required = bool(self._url)
        self._redis_available = False
        if self._url and redis is not None:
            try:
                self._client = redis.from_url(self._url, decode_responses=True)
                self._client.ping()
                self._redis_available = True
            except Exception:
                self._client = None
                self._redis_available = False

    @property
    def enabled(self) -> bool:
        return self._client is not None and self._redis_available

    @property
    def version(self) -> int:
        return self._version

    @property
    def redis_required(self) -> bool:
        return self._redis_required

    @property
    def redis_available(self) -> bool:
        return self._redis_available

    def _bump_version(self) -> int:
        self._version += 1
        if self._client and self._redis_available:
            try:
                self._client.set("xair:context_version", str(self._version))
            except Exception:
                self._redis_available = False
        return self._version

    def update(self, context: dict) -> int:
        self._memory = deep_merge(self._memory, context)
        if self._client and self._redis_available:
            try:
                raw = self._client.get("xair:context")
                merged = json.loads(raw) if raw else {}
                merged = deep_merge(merged, context)
                self._client.set("xair:context", json.dumps(merged))
                self._memory = merged
            except Exception:
                self._redis_available = False
        return self._bump_version()

    def snapshot(self) -> tuple[dict, int, bool]:
        """Return (context, version, store_trusted).

        When Redis is configured but unreachable, store_trusted is False so
        callers must delay or revoke rather than execute on stale memory.
        """
        if self._client:
            try:
                raw = self._client.get("xair:context")
                ver_raw = self._client.get("xair:context_version")
                if raw:
                    self._memory = deep_merge(self._memory, json.loads(raw))
                if ver_raw:
                    self._version = int(ver_raw)
                self._redis_available = True
            except Exception:
                self._redis_available = False

        trusted = (not self._redis_required) or self._redis_available
        return dict(self._memory), self._version, trusted
