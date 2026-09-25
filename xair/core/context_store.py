from __future__ import annotations

import json
import os
import threading
import time
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
COMMITTED_KEY = "xair:committed"
_MAX_TX_RETRIES = 32

# Atomic authorization commit. Redis runs a script without interleaving any
# other command, so the version check, the clock read, and the append are one
# step with respect to every context update (which is a MULTI/EXEC on KEYS[1]).
_COMMIT_LUA = """
if redis.call('SISMEMBER', KEYS[3], ARGV[1]) == 1 then
  return {'duplicate', -1, -1, '0', '0', -1}
end
local t = redis.call('TIME')
local now_ms = tonumber(t[1]) * 1000 + tonumber(t[2]) / 1000
local age = now_ms - tonumber(ARGV[5])
local bound = tonumber(ARGV[6])
local max_ahead = tonumber(ARGV[7])
local raw = redis.call('GET', KEYS[1])
local version, pv = 0, {}
if raw then
  local doc = cjson.decode(raw)
  version = tonumber(doc['version'] or 0)
  pv = doc['path_versions'] or {}
end
local paths = cjson.decode(ARGV[2])
local observed = 0
for p, v in pairs(pv) do
  for _, r in ipairs(paths) do
    if p == r or string.sub(p, 1, #r + 1) == r .. '.' or string.sub(r, 1, #p + 1) == p .. '.' then
      if tonumber(v) > observed then observed = tonumber(v) end
      break
    end
  end
end
if max_ahead >= 0 and age < -max_ahead then
  return {'future_skew', -1, version, tostring(now_ms), tostring(age), -1}
end
if bound >= 0 and age > bound then
  return {'expired', observed, version, tostring(now_ms), tostring(age), -1}
end
if observed ~= tonumber(ARGV[3]) then
  return {'changed', observed, version, tostring(now_ms), tostring(age), -1}
end
local rec = cjson.decode(ARGV[4])
rec['commit_version'] = version
rec['store_time_ms'] = now_ms
rec['age_at_commit_ms'] = age
local seq = redis.call('RPUSH', KEYS[2], cjson.encode(rec))
redis.call('SADD', KEYS[3], ARGV[1])
return {'committed', observed, version, tostring(now_ms), tostring(age), seq}
"""


def _mono_ms() -> float:
    """CLOCK_MONOTONIC in ms; shared by every process (and container) on one kernel."""
    return time.perf_counter() * 1000.0


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
        self._lock = threading.RLock()
        self._memory: dict[str, Any] = {}
        self._version = 0
        self._path_versions: dict[str, int] = {}
        self._actuations: list[dict] = []
        self._committed: set[str] = set()
        self._memory_kv: dict[str, str] = {}
        self._commit_script = None
        self.last_io_mono_ms: tuple[float, float] | None = None
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
        return self.update_timed(patch)[0]

    def update_timed(self, patch: dict) -> tuple[int, tuple[float, float] | None]:
        """As ``update``; also returns monotonic (lo, hi) bounds on the commit of the write."""
        with self._lock:
            self.last_io_mono_ms = None
            return self._update_locked(patch), self.last_io_mono_ms

    def _update_locked(self, patch: dict) -> int:
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
        t = _mono_ms()
        self.last_io_mono_ms = (t, t)
        return self._version

    def _update_redis(self, patch: dict) -> int:
        with self._client.pipeline() as pipe:
            for _ in range(_MAX_TX_RETRIES):
                try:
                    pipe.watch(SNAPSHOT_KEY)
                    context, version, pv = _apply(*_decode(pipe.get(SNAPSHOT_KEY)), patch)
                    pipe.multi()
                    pipe.set(SNAPSHOT_KEY, _encode(context, version, pv))
                    lo = _mono_ms()
                    pipe.execute()
                    self.last_io_mono_ms = (lo, _mono_ms())
                    self._memory, self._version, self._path_versions = context, version, pv
                    self._redis_available = True
                    return version
                except WatchError:
                    continue
        raise RuntimeError("context update lost the optimistic race too many times")

    def commit_authorization(self, intent_id: str, paths: list[str], expected: int, record: dict,
                             decision_epoch_ms: float, age_bound_ms: float | None,
                             max_ahead_ms: float | None = None) -> dict:
        """Commit an authorization record iff nu_R still equals ``expected`` and the age bound holds.

        One atomic operation on the store (a Lua script with Redis, the process
        lock in memory) reads the per-path versions, reads the store clock,
        checks the intent's age against ``age_bound_ms``, and appends the
        record to the actuation log. Every context update is therefore ordered
        either before the commit (and the commit is refused) or after it, and
        the temporal bound is checked at the commit instant on the store clock.
        With ``max_ahead_ms`` (the declared clock bound epsilon), a decision
        timestamp more than epsilon ahead of the store clock is refused, so an
        accepted age can underestimate the true age by at most epsilon.
        A second commit for the same intent is refused as a duplicate.

        Returns a dict with ``status`` (committed | changed | expired | duplicate
        | unavailable), ``observed`` (read-set version), ``commit_version``
        (global version at the commit), ``store_time_ms``, ``age_at_commit_ms``,
        ``seq`` (1-based log position), and ``mono_ms`` = (lo, hi), monotonic
        bounds on the commit instant taken around the store call.
        """
        with self._lock:
            self._ensure_client()
            if self._client is not None:
                try:
                    return self._commit_redis(intent_id, paths, expected, record, decision_epoch_ms, age_bound_ms,
                                              max_ahead_ms)
                except Exception:
                    self._drop_client()
            if self._redis_required:
                t = _mono_ms()
                return {"status": "unavailable", "observed": -1, "commit_version": self._version,
                        "store_time_ms": None, "age_at_commit_ms": None, "seq": None, "mono_ms": (t, t)}
            t = _mono_ms()
            now_ms = time.time() * 1000.0
            age = now_ms - decision_epoch_ms
            out = {"observed": read_set_version(self._path_versions, paths), "commit_version": self._version,
                   "store_time_ms": now_ms, "age_at_commit_ms": age, "seq": None, "mono_ms": (t, t)}
            if intent_id in self._committed:
                return {**out, "status": "duplicate"}
            if max_ahead_ms is not None and age < -max_ahead_ms:
                return {**out, "status": "future_skew"}
            if age_bound_ms is not None and age > age_bound_ms:
                return {**out, "status": "expired"}
            if out["observed"] != expected:
                return {**out, "status": "changed"}
            self._actuations.append({**record, "intent_id": intent_id, "commit_version": self._version,
                                     "store_time_ms": now_ms, "age_at_commit_ms": age})
            self._committed.add(intent_id)
            return {**out, "status": "committed", "seq": len(self._actuations)}

    def _commit_redis(self, intent_id, paths, expected, record, decision_epoch_ms, age_bound_ms, max_ahead_ms=None) -> dict:
        if self._commit_script is None:
            self._commit_script = self._client.register_script(_COMMIT_LUA)
        args = [intent_id, json.dumps(list(paths)), int(expected), json.dumps({**record, "intent_id": intent_id}),
                repr(float(decision_epoch_ms)), repr(float(age_bound_ms)) if age_bound_ms is not None else "-1",
                repr(float(max_ahead_ms)) if max_ahead_ms is not None else "-1"]
        lo = _mono_ms()
        res = self._commit_script(keys=[SNAPSHOT_KEY, ACTUATION_LOG_KEY, COMMITTED_KEY], args=args)
        hi = _mono_ms()
        self._redis_available = True
        status, observed, version, store_time, age, seq = res
        return {"status": status, "observed": int(observed), "commit_version": int(version),
                "store_time_ms": float(store_time), "age_at_commit_ms": float(age),
                "seq": int(seq) if int(seq) > 0 else None, "mono_ms": (lo, hi)}

    def kv_get(self, key: str) -> str | None:
        """Small durable value (e.g. a consumer offset), in Redis when configured."""
        with self._lock:
            self._ensure_client()
            if self._client is not None:
                try:
                    return self._client.get(key)
                except Exception:
                    self._drop_client()
            if self._redis_required:
                raise RuntimeError("context store unavailable")
            return self._memory_kv.get(key)

    def kv_set(self, key: str, value: str) -> None:
        with self._lock:
            self._ensure_client()
            if self._client is not None:
                try:
                    self._client.set(key, value)
                    return
                except Exception:
                    self._drop_client()
            if self._redis_required:
                raise RuntimeError("context store unavailable")
            self._memory_kv[key] = value

    def actuation_log(self, start: int = 0) -> list[dict]:
        """Committed records from 0-based position ``start`` on, in commit order."""
        with self._lock:
            self._ensure_client()
            if self._client is not None:
                try:
                    return [json.loads(x) for x in self._client.lrange(ACTUATION_LOG_KEY, start, -1)]
                except Exception:
                    self._drop_client()
            return list(self._actuations[start:])

    def snapshot(self) -> tuple[dict, int, bool]:
        ctx, ver, _, trusted = self.snapshot_full()
        return ctx, ver, trusted

    def snapshot_full(self) -> tuple[dict, int, dict[str, int], bool]:
        return self.snapshot_timed()[:4]

    def snapshot_timed(self) -> tuple[dict, int, dict[str, int], bool, tuple[float, float] | None]:
        """As ``snapshot_full``; also returns monotonic (lo, hi) bounds on the store read."""
        with self._lock:
            self.last_io_mono_ms = None
            out = self._snapshot_locked()
            return (*out, self.last_io_mono_ms)

    def _snapshot_locked(self) -> tuple[dict, int, dict[str, int], bool]:
        """Return (context, version, path_versions, store_trusted) read as one document.

        When Redis is configured but unreachable, store_trusted is False so
        callers must revoke rather than execute on a stale local copy.
        """
        with self._lock:
            self._ensure_client()
            if self._client is not None:
                try:
                    lo = _mono_ms()
                    raw = self._client.get(SNAPSHOT_KEY)
                    self.last_io_mono_ms = (lo, _mono_ms())
                    self._memory, self._version, self._path_versions = _decode(raw)
                    self._redis_available = True
                except Exception:
                    self._drop_client()
            if self._client is None and not self._redis_required:
                t = _mono_ms()
                self.last_io_mono_ms = (t, t)
            trusted = (not self._redis_required) or self._redis_available
            return dict(self._memory), self._version, dict(self._path_versions), trusted
