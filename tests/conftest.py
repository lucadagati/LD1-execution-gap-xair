"""With a Redis configured, the intent-id table persists across tests; start each test from an empty one."""

import os

import pytest


@pytest.fixture(autouse=True)
def _clear_seen_ids():
    url = os.environ.get("REDIS_URL") or os.environ.get("XAIR_TEST_REDIS_URL") or ""
    if url:
        try:
            import redis
            r = redis.from_url(url, socket_connect_timeout=1)
            for key in r.scan_iter("xair:seen:*"):
                r.delete(key)
        except Exception:
            pass
    yield
