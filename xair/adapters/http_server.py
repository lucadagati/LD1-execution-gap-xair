from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from xair.adapters.runtime_state import context_meta, refresh_context, runtime, update_context_store
from xair.core.models import ActionIntent, DecisionOutcome, IntentState

app = FastAPI(title="XAIR Runtime", version="0.1.0-alpha")


class IntentPayload(BaseModel):
    id: str | None = None
    source: str
    timestamp_decision: str
    freshness_window_ms: int
    deadline_ms: int | None = None
    preconditions: list[dict[str, str]] = Field(default_factory=list)
    safety_constraints: list[dict[str, str]] = Field(default_factory=list)
    payload: dict
    priority: int = 0
    revocable: bool = True
    correlation_id: str | None = None


@app.post("/v1/intents/batch")
def submit_intent_batch(body: list[IntentPayload]):
    """Submit concurrent intents; resolve conflicts via coordinator policy."""
    refresh_context()
    intents = [ActionIntent.from_dict(b.model_dump(exclude_none=True)) for b in body]
    winner, losers = runtime.coordinator.resolve(intents)
    results = []
    loser_ids = {l.id for l in losers}
    for intent in intents:
        if intent.id in loser_ids:
            runtime.lifecycle.register(intent)
            runtime.lifecycle.transition(
                intent.id, IntentState.REVOKED, DecisionOutcome.REVOKE, "conflict_loser"
            )
            results.append({"id": intent.id, "source": intent.source, "outcome": "REVOKE", "reason": "conflict_loser"})
        elif winner and intent.id == winner.id:
            record = runtime.process_intent(intent)
            results.append({
                "id": intent.id,
                "source": intent.source,
                "outcome": record.outcome.value if record.outcome else None,
                "reason": record.reason,
            })
    cv = sum(1 for r in results if r.get("outcome") == "EXECUTE" and r.get("source") == "ai"
             and any(x.get("source") == "xr" and x.get("outcome") == "EXECUTE" for x in results))
    return {"results": results, "cv": cv, "winner": winner.source if winner else None}


@app.post("/v1/intents")
def submit_intent(body: IntentPayload):
    ver, trusted = refresh_context()
    if not trusted:
        return {
            "id": body.id,
            "state": IntentState.REVOKED.value,
            "outcome": DecisionOutcome.REVOKE.value,
            "reason": "context_store_untrusted",
            "validation_latency_ms": 0.0,
            "context_version": ver,
            "context_trusted": False,
        }
    intent = ActionIntent.from_dict(body.model_dump(exclude_none=True))
    runtime.submit_intent(intent)
    result = runtime.process_intent(intent)
    meta = context_meta()
    return {
        "id": intent.id,
        "state": result.state.value,
        "outcome": result.outcome.value if result.outcome else None,
        "reason": result.reason,
        "validation_latency_ms": result.validation_latency_ms,
        "context_version": meta["version"],
        "context_trusted": meta["store_trusted"],
    }


@app.get("/v1/intents/{intent_id}")
def get_intent(intent_id: str):
    record = runtime.lifecycle.get(intent_id)
    if not record:
        raise HTTPException(status_code=404, detail="intent not found")
    return {
        "id": intent_id,
        "state": record.state.value,
        "outcome": record.outcome.value if record.outcome else None,
        "reason": record.reason,
    }


@app.delete("/v1/intents/{intent_id}")
def revoke_intent(intent_id: str):
    record = runtime.lifecycle.get(intent_id)
    if not record:
        raise HTTPException(status_code=404, detail="intent not found")
    runtime.lifecycle.transition(
        intent_id, IntentState.REVOKED, DecisionOutcome.REVOKE, "human_supervisory_revoke"
    )
    return {"id": intent_id, "state": "REVOKED"}


@app.get("/v1/metrics")
def metrics():
    return runtime.get_metrics()


@app.get("/v1/context/snapshot")
def get_context_snapshot():
    refresh_context()
    meta = context_meta()
    return {
        "ok": True,
        "context": runtime.context.context,
        "context_version": meta["version"],
        "context_trusted": meta["store_trusted"],
    }


@app.post("/v1/context/snapshot")
def context_snapshot(body: dict):
    ver = update_context_store(body)
    meta = context_meta()
    return {
        "ok": True,
        "keys": list(runtime.context.context.keys()),
        "context_version": ver,
        "context_trusted": meta["store_trusted"],
    }
