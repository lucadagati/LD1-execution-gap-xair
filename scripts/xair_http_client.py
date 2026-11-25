#!/usr/bin/env python3
"""HTTP client for XAIR Runtime API (used by XAIR actuator gateway)."""

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

    def get_context(self, predicates: list[str] | None = None, intent_id: str | None = None) -> dict:
        """Versioned snapshot; ``intent_id`` adds that intent's lifecycle state to the same response."""
        from urllib.parse import urlencode
        query = {}
        if predicates is not None:
            query["predicates"] = json.dumps(predicates)
        if intent_id is not None:
            query["intent_id"] = intent_id
        return self._request("GET", "/v1/context/snapshot" + ("?" + urlencode(query) if query else ""))

    def report_release(
        self,
        intent_id: str,
        released: bool,
        reason: str,
        context_version: int | None = None,
        read_set_version: int | None = None,
        effect_status: str | None = None,
        effect_detail: str = "",
    ) -> dict:
        """Report the release step (t_m): RELEASED, or WITHHELD by the gate or a release guard.

        ``effect_status`` (confirmed | failed | unknown) reports the effect (t_a)
        in the same request when the gateway already knows it."""
        return self._request(
            "POST",
            f"/v1/intents/{intent_id}/release",
            {"released": released, "reason": reason, "context_version": context_version,
             "read_set_version": read_set_version, "effect_status": effect_status, "effect_detail": effect_detail},
        )

    def report_publication(self, intent_id: str, published: bool, reason: str, **versions) -> dict:
        """Deprecated name of :meth:`report_release`."""
        return self.report_release(intent_id, published, reason, **versions)

    def report_effect(self, intent_id: str, status: str, detail: str = "") -> dict:
        """Report the effect (t_a) of a released intent: confirmed | failed | unknown."""
        return self._request("POST", f"/v1/intents/{intent_id}/effect", {"status": status, "detail": detail})

    def commit(self, intent_id: str, scope: str = "readset", margin_ms: float = 0.0) -> dict:
        return self._request("POST", f"/v1/intents/{intent_id}/commit", {"scope": scope, "margin_ms": margin_ms})

    def withheld(self, intent_id: str, reason: str) -> dict:
        return self._request("POST", f"/v1/intents/{intent_id}/withheld", {"reason": reason})

    def actuations(self, start: int = 0) -> dict:
        return self._request("GET", f"/v1/actuations?start={int(start)}")

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
