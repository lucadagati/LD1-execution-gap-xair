from __future__ import annotations

import functools
import json
import os
import threading
import time
from typing import Any

from xair.core.context_validator import check_expression
from xair.core.deep_merge import deep_merge
from xair.core.versioning import _related, changed_paths, predicate_version, read_set, read_set_version

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
# "hash" layout: predicate versions in their own hash with a per-root index, so an
# update reads only the predicates under the roots it changes and a validation
# only the intent's own; "document" (default) keeps them in the snapshot document.
SEEN_PREFIX = "xair:seen:"
PRED_KEY = "xair:preds"
PRED_ROOT_PREFIX = "xair:predroot:"
VER_KEY = "xair:ver"         # "shard" layout: global version
CTX_KEY = "xair:ctxroots"   # "shard" layout: hash root -> {"c": subtree, "pv": path versions}
PRED_VER_KEY = "xair:predrootver" # hash root -> counter, bumped when a predicate under that root is added or removed (index cache)
PREDICATE_LAYOUT = os.environ.get("XAIR_PREDICATE_LAYOUT", "document")
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
-- 'none' means no bound; any number, including a negative one, is a bound
local bound = ARGV[6]
local max_ahead = ARGV[7]
local shard = ARGV[9] == 'shard'
local raw = nil
local version, pv = 0, {}
if shard then
  version = tonumber(redis.call('GET', KEYS[1]) or 0)
else
  raw = redis.call('GET', KEYS[1])
  if raw then
    local doc = cjson.decode(raw)
    version = tonumber(doc['version'] or 0)
    pv = doc['path_versions'] or {}
  end
end
local paths = cjson.decode(ARGV[2])
local observed = 0
if ARGV[8] == 'truth' then
  -- ARGV[2] lists predicate expressions; their truth-change versions live in the
  -- snapshot document ("document" layout) or in the hash KEYS[4] ("hash" layout)
  local preds = {}
  if ARGV[9] == 'document' and raw then preds = cjson.decode(raw)['predicate_versions'] or {} end
  for _, e in ipairs(paths) do
    local entry = preds[e]
    if ARGV[9] ~= 'document' then
      local h = redis.call('HGET', KEYS[4], e)
      entry = h and cjson.decode(h) or nil
    end
    if entry == nil then observed = -1 break end
    if tonumber(entry[1]) > observed then observed = tonumber(entry[1]) end
  end
elseif shard then
  -- the per-path versions of a root live in its entry of the hash KEYS[5]
  local seen = {}
  for _, r in ipairs(paths) do
    local root = string.match(r, '^[^.]+')
    if not seen[root] then
      seen[root] = true
      local h = redis.call('HGET', KEYS[5], root)
      if h then
        for p, v in pairs(cjson.decode(h)['pv'] or {}) do
          for _, r2 in ipairs(paths) do
            if p == r2 or string.sub(p, 1, #r2 + 1) == r2 .. '.' or string.sub(r2, 1, #p + 1) == p .. '.' then
              if tonumber(v) > observed then observed = tonumber(v) end
              break
            end
          end
        end
      end
    end
  end
else
  for p, v in pairs(pv) do
    for _, r in ipairs(paths) do
      if p == r or string.sub(p, 1, #r + 1) == r .. '.' or string.sub(r, 1, #p + 1) == p .. '.' then
        if tonumber(v) > observed then observed = tonumber(v) end
        break
      end
    end
  end
end
if max_ahead ~= 'none' and age < -tonumber(max_ahead) then
  return {'future_skew', -1, version, tostring(now_ms), tostring(age), -1}
end
if bound ~= 'none' and age > tonumber(bound) then
  return {'expired', observed, version, tostring(now_ms), tostring(age), -1}
end
if observed < 0 or tonumber(ARGV[3]) < 0 or observed ~= tonumber(ARGV[3]) then
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


# A registered predicate is retired this long after its last registration; an
# intent still in flight after that fails closed (its predicate version reads
# as -1). A registration refreshes the timestamp only when it is older than half
# the retention, so half the retention must exceed the longest freshness window
# or deadline in use.
PREDICATE_RETENTION_S = float(os.environ.get("XAIR_PREDICATE_RETENTION_S", "3600"))


def _decode_preds(raw: str | None) -> dict[str, list]:
    """{expr: [version, truth, registered_at_epoch_s]}."""
    if not raw:
        return {}
    return {k: [int(v[0]), bool(v[1]), float(v[2]) if len(v) > 2 else 0.0]
            for k, v in (json.loads(raw).get("predicate_versions") or {}).items()}


class _Changed:
    """Changed leaf paths, with their proper prefixes, for O(depth) relatedness tests."""

    def __init__(self, changed: list[str]) -> None:
        self.paths = set(changed)
        self.prefixes = {c[:i] for c in changed for i, ch in enumerate(c) if ch == "."}
        self.roots = {c.split(".", 1)[0] for c in changed}

    def related(self, path: str) -> bool:
        # equal, a changed descendant (path is a prefix of a change), or a changed ancestor
        if path in self.paths or path in self.prefixes:
            return True
        return any(path[:i] in self.paths for i, ch in enumerate(path) if ch == ".")


@functools.lru_cache(maxsize=65536)
def _expr_path(expr: str) -> tuple[str, str]:
    """(path, root segment) of a predicate, parsed once per distinct expression."""
    paths = read_set([expr])
    path = paths[0] if paths else ""
    return path, path.split(".", 1)[0]


@functools.lru_cache(maxsize=65536)
def _expr_roots(expr: str) -> tuple[str, ...]:
    """Root segments of every path a predicate reads."""
    return tuple(sorted({p.split(".", 1)[0] for p in read_set([expr])}))


def _load_roots(raws, roots) -> tuple[dict, dict[str, int]]:
    """Context and path versions from the stored entries of ``roots`` (a missing entry is an empty root)."""
    ctx: dict = {}
    pv: dict[str, int] = {}
    for r, raw in zip(roots, raws):
        if raw is None:
            continue
        e = json.loads(raw)
        ctx[r] = e["c"]
        pv.update({k: int(v) for k, v in (e.get("pv") or {}).items()})
    return ctx, pv


def _group_pv(pv: dict[str, int]) -> dict[str, dict[str, int]]:
    """Path versions grouped by their root segment."""
    out: dict[str, dict[str, int]] = {}
    for k, v in pv.items():
        out.setdefault(k.split(".", 1)[0], {})[k] = v
    return out


def _encode(context: dict, version: int, path_versions: dict[str, int], preds: dict[str, list] | None = None) -> str:
    return json.dumps({"context": context, "version": version, "path_versions": path_versions,
                       "predicate_versions": preds or {}})


def _truth(expr: str, context: dict) -> bool:
    return check_expression(expr, context)[0]


def _apply(context: dict, version: int, path_versions: dict[str, int], patch: dict,
           preds: dict[str, list] | None = None):
    """Merge ``patch``; the global version always advances, a path version only on a value change,
    and a registered predicate's version only when its truth value changes."""
    version += 1
    path_versions = dict(path_versions)
    changed = changed_paths(context, patch)
    for path in changed:
        path_versions[path] = version
    merged = deep_merge(context, patch)
    preds = dict(preds or {})
    if preds:
        horizon = time.time() - PREDICATE_RETENTION_S
        preds = {e: v for e, v in preds.items() if v[2] >= horizon}   # retire unused predicates
    if changed and preds:
        ch = _Changed(changed)
        for expr, entry in preds.items():
            path, root = _expr_path(expr)
            # related paths share their first segment, so other roots are skipped cheaply
            if root in ch.roots and ch.related(path):
                now = _truth(expr, merged)
                if now != entry[1]:
                    preds[expr] = [version, now, entry[2]]
    return merged, version, path_versions, preds


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
        self._preds: dict[str, list] = {}
        self._shard = PREDICATE_LAYOUT == "shard" and bool(self._url)
        self._hash_layout = PREDICATE_LAYOUT in ("hash", "shard") and bool(self._url)
        self.partial_snapshots = self._shard   # a gate read returns only the roots the predicates read
        self._shard_view: tuple[dict, dict] = ({}, {})
        self._last_gc = 0.0
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
        self._memory, self._version, self._path_versions, self._preds = _apply(
            self._memory, self._version, self._path_versions, patch, self._preds
        )
        t = _mono_ms()
        self.last_io_mono_ms = (t, t)
        return self._version

    def _update_redis(self, patch: dict) -> int:
        if self._shard:
            return self._update_redis_shard(patch)
        if self._hash_layout:
            return self._update_redis_hash(patch)
        with self._client.pipeline() as pipe:
            for _ in range(_MAX_TX_RETRIES):
                try:
                    pipe.watch(SNAPSHOT_KEY)
                    raw = pipe.get(SNAPSHOT_KEY)
                    context, version, pv, preds = _apply(*_decode(raw), patch, _decode_preds(raw))
                    pipe.multi()
                    pipe.set(SNAPSHOT_KEY, _encode(context, version, pv, preds))
                    lo = _mono_ms()
                    pipe.execute()
                    self.last_io_mono_ms = (lo, _mono_ms())
                    self._memory, self._version, self._path_versions, self._preds = context, version, pv, preds
                    self._redis_available = True
                    return version
                except WatchError:
                    continue
        raise RuntimeError("context update lost the optimistic race too many times")

    def _update_redis_shard(self, patch: dict) -> int:
        """Update on the "shard" layout: only the roots the patch names are read and rewritten."""
        with self._client.pipeline() as pipe:
            for _ in range(_MAX_TX_RETRIES):
                try:
                    pipe.watch(VER_KEY, PRED_KEY)
                    version = int(pipe.get(VER_KEY) or 0) + 1
                    roots = sorted(patch)
                    ctx, pv = _load_roots(pipe.hmget(CTX_KEY, roots), roots)
                    changed = changed_paths(ctx, patch)
                    for path in changed:
                        pv[path] = version
                    merged = deep_merge(ctx, patch)
                    updates: dict[str, str] = {}
                    if changed:
                        ch = _Changed(changed)
                        cands = self._candidates(ch.roots)
                        if cands:
                            need = sorted({r for e in cands for r in _expr_roots(e)} - set(merged))
                            if need:
                                extra, _ = _load_roots(self._client.hmget(CTX_KEY, need), need)
                                merged = {**extra, **merged}
                            for expr, raw_e in zip(cands, self._client.hmget(PRED_KEY, cands)):
                                if raw_e is None:
                                    continue
                                entry = json.loads(raw_e)
                                if ch.related(_expr_path(expr)[0]):
                                    now = _truth(expr, merged)
                                    if now != bool(entry[1]):
                                        updates[expr] = json.dumps([version, now, entry[2]])
                    pipe.multi()
                    by_root = _group_pv(pv)
                    pipe.hset(CTX_KEY, mapping={r: json.dumps({"c": merged[r], "pv": by_root.get(r, {})})
                                                for r in roots if r in merged})
                    pipe.set(VER_KEY, version)
                    if updates:
                        pipe.hset(PRED_KEY, mapping=updates)
                    lo = _mono_ms()
                    pipe.execute()
                    self.last_io_mono_ms = (lo, _mono_ms())
                    self._version = version
                    self._redis_available = True
                    return version
                except WatchError:
                    continue
        raise RuntimeError("context update lost the optimistic race too many times")

    def _read_shard(self, exprs: list[str]):
        """(version, context and path versions of the roots ``exprs`` read, their predicate entries), in one MULTI/EXEC."""
        roots = sorted({r for e in exprs for r in _expr_roots(e)})
        with self._client.pipeline(transaction=True) as pipe:
            pipe.get(VER_KEY)
            if roots:
                pipe.hmget(CTX_KEY, roots)
            if exprs:
                pipe.hmget(PRED_KEY, exprs)
            out = pipe.execute()
        version = int(out[0] or 0)
        i = 1
        ctx, pv = {}, {}
        if roots:
            ctx, pv = _load_roots(out[i], roots)
            i += 1
        preds = {}
        if exprs:
            preds = {e: [int(v[0]), bool(v[1]), float(v[2])] for e, x in zip(exprs, out[i]) if x is not None
                     for v in [json.loads(x)]}
        return version, ctx, pv, preds

    def _register_redis_shard(self, exprs: list[str]) -> dict[str, list]:
        if time.time() - self._last_gc > 60.0:
            self._last_gc = time.time()
            self.gc_predicates()
        version, ctx, pv, preds = self._read_shard(exprs)
        refresh = time.time() - PREDICATE_RETENTION_S / 2
        if all(e in preds and preds[e][2] >= refresh for e in exprs):
            self._version, self._preds = version, preds
            self._shard_view = (ctx, pv)
            self._redis_available = True
            return {e: list(preds[e]) for e in exprs}
        roots = sorted({r for e in exprs for r in _expr_roots(e)})
        with self._client.pipeline() as pipe:
            for _ in range(_MAX_TX_RETRIES):
                try:
                    pipe.watch(VER_KEY, PRED_KEY)
                    version = int(pipe.get(VER_KEY) or 0)
                    ctx, pv = _load_roots(pipe.hmget(CTX_KEY, roots), roots) if roots else ({}, {})
                    vals = pipe.hmget(PRED_KEY, exprs)
                    cur = {e: json.loads(x) for e, x in zip(exprs, vals) if x is not None}
                    now_s = time.time()
                    new = {e: ([cur[e][0], cur[e][1], now_s] if e in cur else [version, _truth(e, ctx), now_s])
                           for e in exprs}
                    pipe.multi()
                    pipe.hset(PRED_KEY, mapping={e: json.dumps(v) for e, v in new.items()})
                    for e in exprs:
                        pipe.sadd(PRED_ROOT_PREFIX + _expr_path(e)[1], e)
                    for r in {_expr_path(e)[1] for e in exprs if e not in cur}:
                        pipe.hincrby(PRED_VER_KEY, r, 1)
                    pipe.execute()
                    self._version = version
                    self._shard_view = (ctx, pv)
                    self._preds = {e: [int(v[0]), bool(v[1]), float(v[2])] for e, v in new.items()}
                    self._redis_available = True
                    break
                except WatchError:
                    continue
            else:
                raise RuntimeError("predicate registration lost the optimistic race too many times")
        return {e: list(self._preds[e]) for e in exprs}

    def _update_redis_hash(self, patch: dict) -> int:
        with self._client.pipeline() as pipe:
            for _ in range(_MAX_TX_RETRIES):
                try:
                    pipe.watch(SNAPSHOT_KEY, PRED_KEY)
                    raw = pipe.get(SNAPSHOT_KEY)
                    context, version, pv = _decode(raw)
                    changed = changed_paths(context, patch)
                    merged, version, pv, _ = _apply(context, version, pv, patch, None)
                    updates: dict[str, str] = {}
                    if changed:
                        ch = _Changed(changed)
                        # candidates from the process-local index root -> expressions; it is rebuilt only when the
                        # registry version changes, so an update costs one HMGET and not one SMEMBERS per root
                        cands = self._candidates(ch.roots)
                        if cands:
                            for expr, raw_e in zip(cands, self._client.hmget(PRED_KEY, cands)):
                                if raw_e is None:
                                    continue
                                entry = json.loads(raw_e)
                                path, _root = _expr_path(expr)
                                if ch.related(path):
                                    now = _truth(expr, merged)
                                    if now != bool(entry[1]):
                                        updates[expr] = json.dumps([version, now, entry[2]])
                    pipe.multi()
                    pipe.set(SNAPSHOT_KEY, _encode(merged, version, pv, None))
                    if updates:
                        pipe.hset(PRED_KEY, mapping=updates)
                    lo = _mono_ms()
                    pipe.execute()
                    self.last_io_mono_ms = (lo, _mono_ms())
                    self._memory, self._version, self._path_versions = merged, version, pv
                    self._redis_available = True
                    return version
                except WatchError:
                    continue
        raise RuntimeError("context update lost the optimistic race too many times")

    def _candidates(self, roots) -> list[str]:
        """Registered predicates under ``roots``, from a process-local per-root index.

        A root's list is refetched (one SMEMBERS) only when its counter in the
        registry has moved, so an update reads one HMGET of counters and a new
        predicate under another root costs it nothing."""
        rs = sorted(roots)
        vers = self._client.hmget(PRED_VER_KEY, rs)
        idx = self.__dict__.setdefault("_idx", {})
        out: list[str] = []
        for r, v in zip(rs, vers):
            hit = idx.get(r)
            if hit is None or hit[0] != v:
                hit = (v, sorted(self._client.smembers(PRED_ROOT_PREFIX + r)))
                idx[r] = hit
            out += hit[1]
        return out

    def _read_hash(self, exprs: list[str]) -> tuple[str | None, dict[str, list]]:
        """Snapshot document and the entries of ``exprs``, read atomically (MULTI/EXEC)."""
        with self._client.pipeline(transaction=True) as pipe:
            pipe.get(SNAPSHOT_KEY)
            if exprs:
                pipe.hmget(PRED_KEY, exprs)
            out = pipe.execute()
        raw = out[0]
        vals = out[1] if exprs else []
        preds = {e: [int(v[0]), bool(v[1]), float(v[2])] for e, x in zip(exprs, vals) if x is not None
                 for v in [json.loads(x)]}
        return raw, preds

    def _register_redis_hash(self, exprs: list[str]) -> dict[str, list]:
        # retirement runs on every path, at most once a minute; an entry not yet
        # removed stays correct, since every update still re-evaluates it
        if time.time() - self._last_gc > 60.0:
            self._last_gc = time.time()
            self.gc_predicates()
        raw, preds = self._read_hash(exprs)
        refresh = time.time() - PREDICATE_RETENTION_S / 2
        if all(e in preds and preds[e][2] >= refresh for e in exprs):
            self._memory, self._version, self._path_versions = _decode(raw)
            self._preds = preds
            self._redis_available = True
            return {e: list(preds[e]) for e in exprs}
        with self._client.pipeline() as pipe:
            for _ in range(_MAX_TX_RETRIES):
                try:
                    pipe.watch(SNAPSHOT_KEY, PRED_KEY)
                    raw = pipe.get(SNAPSHOT_KEY)
                    context, version, pv = _decode(raw)
                    vals = pipe.hmget(PRED_KEY, exprs)
                    preds = {e: json.loads(x) for e, x in zip(exprs, vals) if x is not None}
                    now_s = time.time()
                    new = {e: ([preds[e][0], preds[e][1], now_s] if e in preds else [version, _truth(e, context), now_s])
                           for e in exprs}
                    pipe.multi()
                    pipe.hset(PRED_KEY, mapping={e: json.dumps(v) for e, v in new.items()})
                    for e in exprs:
                        pipe.sadd(PRED_ROOT_PREFIX + _expr_path(e)[1], e)
                    for r in {_expr_path(e)[1] for e in exprs if e not in preds}:
                        pipe.hincrby(PRED_VER_KEY, r, 1)
                    pipe.execute()
                    self._memory, self._version, self._path_versions = context, version, pv
                    self._preds = {e: [int(v[0]), bool(v[1]), float(v[2])] for e, v in new.items()}
                    self._redis_available = True
                    break
                except WatchError:
                    continue
            else:
                raise RuntimeError("predicate registration lost the optimistic race too many times")
        return {e: list(self._preds[e]) for e in exprs}

    def gc_predicates(self) -> int:
        """Retire predicates not registered within the retention window (hash layout); return how many."""
        if not self._hash_layout or self._client is None:
            return 0
        horizon = time.time() - PREDICATE_RETENTION_S
        removed = 0
        for expr, raw_e in self._client.hscan_iter(PRED_KEY, count=1000):
            expr = expr.decode() if isinstance(expr, bytes) else expr
            if json.loads(raw_e)[2] >= horizon:
                continue
            with self._client.pipeline() as pipe:
                try:
                    pipe.watch(PRED_KEY)
                    cur = pipe.hget(PRED_KEY, expr)
                    if cur is None or json.loads(cur)[2] >= horizon:
                        pipe.unwatch()
                        continue
                    pipe.multi()
                    pipe.hdel(PRED_KEY, expr)
                    pipe.srem(PRED_ROOT_PREFIX + _expr_path(expr)[1], expr)
                    pipe.hincrby(PRED_VER_KEY, _expr_path(expr)[1], 1)
                    pipe.execute()
                    removed += 1
                except WatchError:
                    continue
        return removed

    def snapshot_with_predicates(self, exprs: list[str]):
        """(context, version, path_versions, trusted, io, predicate versions of ``exprs``), read together."""
        with self._lock:
            if self._shard and exprs:
                self._ensure_client()
                if self._client is not None:
                    try:
                        lo = _mono_ms()
                        version, ctx, pv, preds = self._read_shard(list(exprs))
                        io = (lo, _mono_ms())
                        self._version = version
                        self._redis_available = True
                        return ctx, version, pv, True, io, preds
                    except Exception:
                        self._drop_client()
                return {}, self._version, {}, not self._redis_required, None, {}
            if self._hash_layout and not self._shard:
                self._ensure_client()
                if self._client is not None:
                    try:
                        lo = _mono_ms()
                        raw, preds = self._read_hash(list(exprs))
                        io = (lo, _mono_ms())
                        self._memory, self._version, self._path_versions = _decode(raw)
                        self._redis_available = True
                        return dict(self._memory), self._version, dict(self._path_versions), True, io, preds
                    except Exception:
                        self._drop_client()
                return {}, self._version, {}, not self._redis_required, None, {}
            ctx, ver, pv, trusted, io = self.snapshot_timed()
            return ctx, ver, pv, trusted, io, {e: self._preds[e] for e in exprs if e in self._preds}

    def commit_authorization(self, intent_id: str, paths: list[str], expected: int, record: dict,
                             decision_epoch_ms: float, age_bound_ms: float | None,
                             max_ahead_ms: float | None = None, scope: str = "readset") -> dict:
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
                                              max_ahead_ms, scope)
                except Exception:
                    self._drop_client()
            if self._redis_required:
                t = _mono_ms()
                return {"status": "unavailable", "observed": -1, "commit_version": self._version,
                        "store_time_ms": None, "age_at_commit_ms": None, "seq": None, "mono_ms": (t, t)}
            t = _mono_ms()
            now_ms = time.time() * 1000.0
            age = now_ms - decision_epoch_ms
            observed = (predicate_version(self._preds, paths) if scope == "truth"
                        else read_set_version(self._path_versions, paths))
            out = {"observed": observed, "commit_version": self._version,
                   "store_time_ms": now_ms, "age_at_commit_ms": age, "seq": None, "mono_ms": (t, t)}
            if intent_id in self._committed:
                return {**out, "status": "duplicate"}
            if max_ahead_ms is not None and age < -max_ahead_ms:
                return {**out, "status": "future_skew"}
            if age_bound_ms is not None and age > age_bound_ms:
                return {**out, "status": "expired"}
            if out["observed"] < 0 or expected < 0 or out["observed"] != expected:
                return {**out, "status": "changed"}
            self._actuations.append({**record, "intent_id": intent_id, "commit_version": self._version,
                                     "store_time_ms": now_ms, "age_at_commit_ms": age})
            self._committed.add(intent_id)
            return {**out, "status": "committed", "seq": len(self._actuations)}

    def _commit_redis(self, intent_id, paths, expected, record, decision_epoch_ms, age_bound_ms, max_ahead_ms=None,
                      scope="readset") -> dict:
        if self._commit_script is None:
            self._commit_script = self._client.register_script(_COMMIT_LUA)
        args = [intent_id, json.dumps(list(paths)), int(expected), json.dumps({**record, "intent_id": intent_id}),
                repr(float(decision_epoch_ms)), repr(float(age_bound_ms)) if age_bound_ms is not None else "none",
                repr(float(max_ahead_ms)) if max_ahead_ms is not None else "none", scope]
        lo = _mono_ms()
        layout = "shard" if self._shard else "hash" if self._hash_layout else "document"
        res = self._commit_script(keys=[VER_KEY if self._shard else SNAPSHOT_KEY, ACTUATION_LOG_KEY, COMMITTED_KEY,
                                        PRED_KEY, CTX_KEY], args=[*args, layout])
        hi = _mono_ms()
        self._redis_available = True
        status, observed, version, store_time, age, seq = res
        return {"status": status, "observed": int(observed), "commit_version": int(version),
                "store_time_ms": float(store_time), "age_at_commit_ms": float(age),
                "seq": int(seq) if int(seq) > 0 else None, "mono_ms": (lo, hi)}

    @property
    def predicate_versions(self) -> dict[str, list]:
        """Truth-change versions of registered predicates as of the last read or write."""
        return dict(self._preds)

    def register_and_snapshot(self, exprs: list[str]) -> dict:
        """Register ``exprs`` and return the snapshot document read in the same step.

        One store read when the predicates are already registered (the common
        case), a read plus one conditional write otherwise.
        """
        with self._lock:
            self._ensure_client()
            if self._client is not None:
                try:
                    lo = _mono_ms()
                    self._register_redis(exprs)
                    io = (lo, _mono_ms())
                    if self._shard:
                        ctx, pv = self._shard_view
                        return {"context": ctx, "version": self._version, "path_versions": pv,
                                "predicate_versions": {e: self._preds[e] for e in exprs if e in self._preds},
                                "trusted": True, "io": io}
                    return {"context": dict(self._memory), "version": self._version,
                            "path_versions": dict(self._path_versions), "predicate_versions": dict(self._preds),
                            "trusted": True, "io": io}
                except Exception:
                    self._drop_client()
            if self._redis_required:
                return {"context": {}, "version": self._version, "path_versions": {}, "predicate_versions": {},
                        "trusted": False, "io": None}
            for e in exprs:
                if e not in self._preds or self._preds[e][2] < time.time() - PREDICATE_RETENTION_S / 2:
                    old = self._preds.get(e)
                    self._preds[e] = [old[0], old[1], time.time()] if old else [self._version, _truth(e, self._memory), time.time()]
            t = _mono_ms()
            return {"context": dict(self._memory), "version": self._version,
                    "path_versions": dict(self._path_versions), "predicate_versions": dict(self._preds),
                    "trusted": True, "io": (t, t)}

    def register_predicates(self, exprs: list[str]) -> dict[str, list]:
        """Register predicates for truth-change versioning; return their current [version, truth].

        A newly registered predicate starts at the current global version with
        its current truth value. Registration rewrites the snapshot document
        without advancing the global version, in the same optimistic
        transaction as updates, so a concurrent update is either seen by the
        registration or re-evaluates the new predicate itself.
        """
        with self._lock:
            self._ensure_client()
            if self._client is not None:
                try:
                    return self._register_redis(exprs)
                except Exception:
                    self._drop_client()
            if self._redis_required:
                raise RuntimeError("context store unavailable")
            for e in exprs:
                if e not in self._preds or self._preds[e][2] < time.time() - PREDICATE_RETENTION_S / 2:
                    old = self._preds.get(e)
                    self._preds[e] = [old[0], old[1], time.time()] if old else [self._version, _truth(e, self._memory), time.time()]
            return {e: list(self._preds[e]) for e in exprs}

    def _register_redis(self, exprs: list[str]) -> dict[str, list]:
        if self._shard:
            return self._register_redis_shard(exprs)
        if self._hash_layout:
            return self._register_redis_hash(exprs)
        # fast path: one read when every predicate is already registered
        raw = self._client.get(SNAPSHOT_KEY)
        preds = _decode_preds(raw)
        refresh = time.time() - PREDICATE_RETENTION_S / 2
        if all(e in preds and preds[e][2] >= refresh for e in exprs):
            self._memory, self._version, self._path_versions = _decode(raw)
            self._preds = preds
            self._redis_available = True
            return {e: list(preds[e]) for e in exprs}
        with self._client.pipeline() as pipe:
            for _ in range(_MAX_TX_RETRIES):
                try:
                    pipe.watch(SNAPSHOT_KEY)
                    raw = pipe.get(SNAPSHOT_KEY)
                    context, version, pv = _decode(raw)
                    preds = _decode_preds(raw)
                    missing = [e for e in exprs if e not in preds or preds[e][2] < time.time() - PREDICATE_RETENTION_S / 2]
                    if not missing:
                        pipe.unwatch()
                        self._memory, self._version, self._path_versions, self._preds = context, version, pv, preds
                        self._redis_available = True
                        return {e: list(preds[e]) for e in exprs}
                    for e in missing:
                        old = preds.get(e)
                        # a refreshed registration keeps its version: only a truth change advances it
                        preds[e] = [old[0], old[1], time.time()] if old else [version, _truth(e, context), time.time()]
                    pipe.multi()
                    pipe.set(SNAPSHOT_KEY, _encode(context, version, pv, preds))
                    pipe.execute()
                    self._memory, self._version, self._path_versions, self._preds = context, version, pv, preds
                    self._redis_available = True
                    return {e: list(preds[e]) for e in exprs}
                except WatchError:
                    continue
        raise RuntimeError("predicate registration lost the optimistic race too many times")

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

    def claim_intent(self, intent_id: str, ttl_s: float) -> bool:
        """Record that this intent id has been admitted; False if it was already claimed.

        The idempotency table lives in Redis so that it outlives a restart of XAIR: an id
        admitted before the restart is refused afterwards, which keeps "released at most
        once" across restarts. Without Redis the in-process table of the runtime is the
        only record and the claim always succeeds. If Redis is configured but unreachable
        the claim fails, in the same fail-closed way as an untrusted snapshot.
        """
        if not self._url:
            return True
        with self._lock:
            self._ensure_client()
            if self._client is None:
                return False
            try:
                return bool(self._client.set(SEEN_PREFIX + intent_id, "1", nx=True, ex=max(1, int(ttl_s))))
            except Exception:
                self._drop_client()
                return False

    def append_actuation(self, record: dict) -> None:
        """Append a non-authorizing record (e.g. a withheld-release tombstone) to the actuation log."""
        with self._lock:
            self._ensure_client()
            if self._client is not None:
                try:
                    self._client.rpush(ACTUATION_LOG_KEY, json.dumps(record))
                    return
                except Exception:
                    self._drop_client()
            if self._redis_required:
                raise RuntimeError("context store unavailable")
            self._actuations.append(record)

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
                    if self._shard:
                        with self._client.pipeline(transaction=True) as pipe:
                            pipe.get(VER_KEY)
                            pipe.hgetall(CTX_KEY)
                            v, allr = pipe.execute()
                        self.last_io_mono_ms = (lo, _mono_ms())
                        roots = sorted(allr)
                        self._memory, self._path_versions = _load_roots([allr[r] for r in roots], roots)
                        self._version = int(v or 0)
                        self._redis_available = True
                        return dict(self._memory), self._version, dict(self._path_versions), True
                    raw = self._client.get(SNAPSHOT_KEY)
                    self.last_io_mono_ms = (lo, _mono_ms())
                    self._memory, self._version, self._path_versions = _decode(raw)
                    self._preds = _decode_preds(raw)
                    self._redis_available = True
                except Exception:
                    self._drop_client()
            if self._client is None and not self._redis_required:
                t = _mono_ms()
                self.last_io_mono_ms = (t, t)
            trusted = (not self._redis_required) or self._redis_available
            return dict(self._memory), self._version, dict(self._path_versions), trusted
