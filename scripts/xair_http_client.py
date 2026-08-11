#!/usr/bin/env python3
"""HTTP client for XAIR Runtime API (used by AdaptiX adapter)."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any


class XAIRHttpClient:
    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url or os.environ.get("XAIR_URL", "http://127.0.0.1:8080")).rstrip("/")

    def _request(self, method: str, path: str, body: dict | None = None, timeout: float = 15.0) -> dict:
        headers = {"Content-Type": "application/json"}
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            method=method,
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            if not raw.strip():
                raise json.JSONDecodeError("empty response body", raw, 0)
            return json.loads(raw)

    def submit_intent(self, intent_dict: dict) -> dict:
        return self._request("POST", "/v1/intents", intent_dict)

    def update_context(self, context: dict) -> dict:
        return self._request("POST", "/v1/context/snapshot", context)

    def get_context(self) -> dict:
        return self._request("GET", "/v1/context/snapshot")

    def metrics(self) -> dict:
        req = urllib.request.Request(f"{self.base_url}/v1/metrics", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def health_ok(self) -> bool:
        try:
            self.metrics()
            return True
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            return False
