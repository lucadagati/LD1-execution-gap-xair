"""The idempotency table survives a restart of XAIR (it is kept in Redis).

Runs when ``XAIR_TEST_REDIS_URL`` (or a non-empty ``REDIS_URL``) points to a reachable server.
"""

from __future__ import annotations

import os
import uuid

import pytest

from xair.core.context_store import RedisContextStore


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


def test_claim_without_redis_always_succeeds():
    store = RedisContextStore("")
    assert store.claim_intent("x", 60)
    assert store.claim_intent("x", 60)


@pytest.mark.skipif(_redis_url() is None, reason="no reachable Redis")
def test_claim_survives_a_new_store_instance():
    url = _redis_url()
    intent_id = f"restart-{uuid.uuid4().hex}"
    first = RedisContextStore(url)
    assert first.claim_intent(intent_id, 60)
    assert not first.claim_intent(intent_id, 60)          # same process: duplicate
    second = RedisContextStore(url)                        # a restarted XAIR
    assert not second.claim_intent(intent_id, 60)          # the table was not lost
    assert second.claim_intent(f"restart-{uuid.uuid4().hex}", 60)


def test_claim_fails_closed_when_redis_is_configured_but_down():
    store = RedisContextStore("redis://127.0.0.1:1/0")
    assert not store.claim_intent("x", 60)
