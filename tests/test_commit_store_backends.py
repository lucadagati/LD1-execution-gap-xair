"""The atomic commit's age check behaves identically in memory and in Redis (Lua).

Regression: the Lua script used -1 as "no bound" and skipped the age check for every
negative bound, so a margin larger than the deadline (age bound < 0) committed an
expired intent on Redis while the in-memory store refused it. The Redis backend runs
when ``XAIR_TEST_REDIS_URL`` (or a non-empty ``REDIS_URL``) points to a reachable server.
"""

from __future__ import annotations

import os
import time
import uuid

import pytest

from xair.core.context_store import RedisContextStore
from xair.core.temporal_validator import TemporalValidator
from xair.core.versioning import read_set_version


def _redis_url() -> str | None:
    url = os.environ.get("XAIR_TEST_REDIS_URL") or os.environ.get("REDIS_URL") or ""
    if not url:
        return None
    try:
        import redis
        redis.from_url(url, socket_connect_timeout=1).ping()
        return url
    except Exception:
        return None


BACKENDS = ["memory", pytest.param("redis", marks=pytest.mark.skipif(_redis_url() is None, reason="no Redis"))]


@pytest.fixture
def isolated_keys(monkeypatch):
    """Unique key names for this test, deleted afterwards: never flush a database that may be shared."""
    import xair.core.context_store as cs
    prefix = f"xair-test:{uuid.uuid4().hex}:"
    names = ("SNAPSHOT_KEY", "ACTUATION_LOG_KEY", "COMMITTED_KEY", "PRED_KEY")
    for n in names:
        monkeypatch.setattr(cs, n, prefix + getattr(cs, n))
    yield prefix
    url = _redis_url()
    if url:
        import redis
        r = redis.from_url(url)
        for k in r.scan_iter(match=prefix + "*"):
            r.delete(k)


def _store(backend: str) -> RedisContextStore:
    return RedisContextStore("" if backend == "memory" else _redis_url())


def _commit(store, age_bound_ms, max_ahead_ms=None, decision_offset_ms=-100.0):
    store.update({"line": {"state": "RUN"}})
    v = read_set_version(store.snapshot_full()[2], ["line.state"])
    return store.commit_authorization(f"i-{uuid.uuid4()}", ["line.state"], v,
                                      {"read_set": ["line.state"], "read_set_version": v},
                                      time.time() * 1000.0 + decision_offset_ms, age_bound_ms, max_ahead_ms)


@pytest.mark.parametrize("backend", BACKENDS)
def test_negative_age_bound_is_a_bound_not_no_bound(backend, isolated_keys):
    assert _commit(_store(backend), age_bound_ms=-4900.0)["status"] == "expired"


@pytest.mark.parametrize("backend", BACKENDS)
def test_no_bound_and_a_satisfied_bound_commit(backend, isolated_keys):
    store = _store(backend)
    assert _commit(store, age_bound_ms=None)["status"] == "committed"
    assert _commit(store, age_bound_ms=60000.0)["status"] == "committed"
    assert _commit(store, age_bound_ms=10.0)["status"] == "expired"        # decided 100 ms ago


@pytest.mark.parametrize("backend", BACKENDS)
def test_future_skew_bound(backend, isolated_keys):
    store = _store(backend)
    assert _commit(store, None, max_ahead_ms=0.0, decision_offset_ms=+5000.0)["status"] == "future_skew"
    assert _commit(store, None, max_ahead_ms=None, decision_offset_ms=+5000.0)["status"] == "committed"


@pytest.mark.parametrize("eps", [-1.0, float("inf"), float("nan")])
def test_clock_uncertainty_cannot_widen_the_bounds(eps):
    with pytest.raises(ValueError):
        TemporalValidator(clock_uncertainty_ms=eps)
