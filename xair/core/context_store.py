from __future__ import annotations

import json
import os
import threading
from typing import Any

from xair.core.deep_merge import deep_merge
from xair.core.versioning import changed_paths, read_set_version

try:
    import redis
    from redis.exceptions import WatchError
except ImportError:
    redis = None

    class WatchError(Exception):  # type: ignore[no-redef]
        pass


SNAPSHOT_KEY = "xair:snapshot"
ACTUATION_LOG_KEY = "xair:actuations"
_MAX_TX_RETRIES = 32


def _decode(raw: str | None) -> tuple[dict, int, dict[str, int]]:
    if not raw:
        return {}, 0, {}
    doc = json.loads(raw)
    return (
        dict(doc.get("context") or {}),
        int(doc.get("version") or 0),
        {k: int(v) for k, v in (doc.get("path_versions") or {}).items()},
    )


def _encode(context: dict, version: int, path_versions: dict[str, int]) -> str:
    return json.dumps({"context": context, "version": version, "path_versions": path_versions})


def _apply(context: dict, version: int, path_versions: dict[str, int], patch: dict):
    """Merge ``patch``; the global version always advances, a path version only on a value change."""
    version += 1
    path_versions = dict(path_versions)
    for path in changed_paths(context, patch):
        path_versions[path] = version
    return deep_merge(context, patch), version, path_versions


class RedisContextStore:
    """Versioned context snapshot, Redis-backed or in-memory.

    Context, its monotonic global version, and the per-path versions (the
    global version at which each leaf last changed value) live in *one*
    serialized document, so a reader always obtains them written together.
    With Redis, updates are an optimistic WATCH/MULTI transaction on that key,
    which keeps the version monotonic across processes; the in-memory fallback
    protects the same pair with a process lock.
    """

    def __init__(self, url: str | None = None) -> None:
        self._url = url if url is not None else os.environ.get("REDIS_URL", "")
        self._client = None
        self._lock = threading.Lock()
        self._memory: dict[str, Any] = {}
        self._version = 0
        self._path_versions: dict[str, int] = {}
        self._actuations: list[dict] = []
        self._redis_required = bool(self._url)
        self._redis_available = False
        self._ensure_client()

    def _ensure_client(self) -> None:
        """(Re)connect if a client is required but not currently held.

        A client is dropped to None on any failure and only re-created here,
        so a Redis container that is not yet accepting connections at
        process startup (a real race on cold start) does not permanently
        disable the store: every subsequent update/snapshot retries.
        """
        if self._client is not None or not self._url or redis is None:
            return
        try:
            client = redis.from_url(self._url, decode_responses=True)
            client.ping()
            self._client = client
            self._redis_available = True
        except Exception:
            self._client = None
            self._redis_available = False

    def _drop_client(self) -> None:
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

    def update(self, patch: dict) -> int:
        """Deep-merge ``patch`` into the snapshot and advance its version atomically."""
        with self._lock:
            self._ensure_client()
            if self._client is not None:
                try:
                    return self._update_redis(patch)
                except Exception:
                    self._drop_client()
            if self._redis_required:
                # Never advance a private in-memory version while the shared
                # store is unreachable: readers would observe a version that
                # no other process can see. The caller gets the last known
                # version; snapshot() reports the store as untrusted.
                return self._version
            self._memory, self._version, self._path_versions = _apply(
                self._memory, self._version, self._path_versions, patch
            )
            return self._version

    def _update_redis(self, patch: dict) -> int:
        with self._client.pipeline() as pipe:
            for _ in range(_MAX_TX_RETRIES):
                try:
                    pipe.watch(SNAPSHOT_KEY)
                    context, version, pv = _apply(*_decode(pipe.get(SNAPSHOT_KEY)), patch)
                    pipe.multi()
                    pipe.set(SNAPSHOT_KEY, _encode(context, version, pv))
                    pipe.execute()
                    self._memory, self._version, self._path_versions = context, version, pv
                    self._redis_available = True
                    return version
                except WatchError:
                    continue
        raise RuntimeError("context update lost the optimistic race too many times")

    def compare_and_actuate(self, paths: list[str], expected: int, record: dict) -> tuple[bool, int, int]:
        """Commit an actuation record iff the read-set version still equals ``expected``.

        The check and the commit form one transaction on the snapshot key
        (Redis WATCH/MULTI, or the process lock in memory), so every context
        update is ordered either before the check (and the commit is refused)
        or after the commit (and cannot have invalidated the released intent).
        Returns (committed, observed read-set version, global version at commit).
        """
        with self._lock:
            self._ensure_client()
            if self._client is not None:
                try:
                    return self._cas_redis(paths, expected, record)
                except Exception:
                    self._drop_client()
            if self._redis_required:
                return False, -1, self._version
            observed = read_set_version(self._path_versions, paths)
            if observed != expected:
                return False, observed, self._version
            self._actuations.append({**record, "commit_version": self._version})
            return True, observed, self._version

    def _cas_redis(self, paths: list[str], expected: int, record: dict) -> tuple[bool, int, int]:
        with self._client.pipeline() as pipe:
            for _ in range(_MAX_TX_RETRIES):
                try:
                    pipe.watch(SNAPSHOT_KEY)
                    _, version, pv = _decode(pipe.get(SNAPSHOT_KEY))
                    observed = read_set_version(pv, paths)
                    if observed != expected:
                        pipe.unwatch()
                        return False, observed, version
                    pipe.multi()
                    pipe.rpush(ACTUATION_LOG_KEY, json.dumps({**record, "commit_version": version}))
                    pipe.execute()
                    self._redis_available = True
                    return True, observed, version
                except WatchError:
                    continue
        raise RuntimeError("actuation lost the optimistic race too many times")

    def actuation_log(self) -> list[dict]:
        with self._lock:
            if self._client is not None:
                try:
                    return [json.loads(x) for x in self._client.lrange(ACTUATION_LOG_KEY, 0, -1)]
                except Exception:
                    self._drop_client()
            return list(self._actuations)

    def snapshot(self) -> tuple[dict, int, bool]:
        ctx, ver, _, trusted = self.snapshot_full()
        return ctx, ver, trusted

    def snapshot_full(self) -> tuple[dict, int, dict[str, int], bool]:
        """Return (context, version, path_versions, store_trusted) read as one document.

        When Redis is configured but unreachable, store_trusted is False so
        callers must revoke rather than execute on a stale local copy.
        """
        with self._lock:
            self._ensure_client()
            if self._client is not None:
                try:
                    self._memory, self._version, self._path_versions = _decode(self._client.get(SNAPSHOT_KEY))
                    self._redis_available = True
                except Exception:
                    self._drop_client()
            trusted = (not self._redis_required) or self._redis_available
            return dict(self._memory), self._version, dict(self._path_versions), trusted
