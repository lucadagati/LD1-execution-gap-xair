from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from xair.core.context_store import RedisContextStore


class RedisReconnectTests(unittest.TestCase):
    """A Redis container not yet accepting connections at process startup
    (a real race on cold start) must not permanently disable the store: the
    next update/snapshot call has to retry the connection, not just report
    untrusted forever because __init__'s one-shot ping failed."""

    def test_snapshot_retries_connection_after_initial_failure(self) -> None:
        with patch("xair.core.context_store.redis") as mock_redis:
            mock_redis.from_url.side_effect = ConnectionError("not ready yet")
            store = RedisContextStore("redis://127.0.0.1:6399/0")
        self.assertIsNone(store._client)
        _, _, trusted = store.snapshot()
        self.assertFalse(trusted)

        with patch("xair.core.context_store.redis") as mock_redis:
            client = MagicMock()
            client.get.side_effect = [None, None]
            mock_redis.from_url.return_value = client
            _, _, trusted = store.snapshot()
        self.assertTrue(trusted)
        self.assertIsNotNone(store._client)

    def test_update_retries_connection_after_initial_failure(self) -> None:
        with patch("xair.core.context_store.redis") as mock_redis:
            mock_redis.from_url.side_effect = OSError("connection refused")
            store = RedisContextStore("redis://127.0.0.1:6399/0")
        self.assertIsNone(store._client)

        with patch("xair.core.context_store.redis") as mock_redis:
            client = MagicMock()
            client.get.return_value = None
            mock_redis.from_url.return_value = client
            store.update({"line": {"state": "RUN"}})
        self.assertIsNotNone(store._client)
        _, _, trusted = store.snapshot()
        self.assertTrue(trusted)


if __name__ == "__main__":
    unittest.main()
