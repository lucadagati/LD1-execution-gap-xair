from __future__ import annotations

import json
from pathlib import Path

import jsonschema
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from xair.adapters.runtime_state import context_meta, refresh_context, runtime, update_context_store
from xair.core.models import ActionIntent, DecisionOutcome, IntentState

app = FastAPI(title="XAIR Runtime", version="0.1.0-alpha")

_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas" / "action-intent-v1.json"
_SCHEMA = json.loads(_SCHEMA_PATH.read_text())
_VALIDATOR = jsonschema.Draft202012Validator(_SCHEMA, format_checker=jsonschema.FormatChecker())


def _schema_error(body) -> str | None:
    """Return the first AIS v1 schema violation, or None if body is valid."""
    if not isinstance(body, dict):
        return "body_not_an_object"
    errors = sorted(_VALIDATOR.iter_errors(body), key=lambda e: list(e.path))
    if not errors:
        return None
    e = errors[0]
    loc = ".".join(str(p) for p in e.path) or "<root>"
    return f"{loc}: {e.message}"


@app.post("/v1/intents/batch")
def submit_intent_batch(body: list[dict]):
    """Submit concurrent intents; resolve conflicts via coordinator policy."""
    refresh_context()
    schema_errors = {i: err for i, item in enumerate(body) if (err := _schema_error(item))}
    valid_items = [item for i, item in enumerate(body) if i not in schema_errors]
    intents = [ActionIntent.from_dict(item) for item in valid_items]
    # Resolve winners per target_entity: intents on different resources are
    # not in conflict and must not be forced into a false conflict_loser
    # just because they arrived in the same batch.
    by_target: dict[str, list[ActionIntent]] = {}
    for intent in intents:
        by_target.setdefault(intent.payload.target_entity, []).append(intent)
    loser_ids: set[str] = set()
    winner_ids: set[str] = set()
    for group in by_target.values():
        group_winner, group_losers = runtime.coordinator.resolve(group)
        loser_ids.update(l.id for l in group_losers)
        if group_winner:
            winner_ids.add(group_winner.id)
    results = [
        {"id": item.get("id"), "source": item.get("source"), "outcome": "REVOKE", "reason": f"schema_invalid:{err}"}
        for i, item in enumerate(body)
        if (err := schema_errors.get(i))
    ]
    for intent in intents:
        if intent.id in loser_ids:
            runtime.lifecycle.register(intent)
            runtime.lifecycle.transition(
                intent.id, IntentState.REVOKED, DecisionOutcome.REVOKE, "conflict_loser"
            )
            results.append({"id": intent.id, "source": intent.source, "outcome": "REVOKE", "reason": "conflict_loser"})
        elif intent.id in winner_ids:
            record = runtime.process_intent(intent)
            if record.outcome in (DecisionOutcome.EXECUTE, DecisionOutcome.DEGRADE):
                # The batch endpoint has no downstream publish/report step to
                # trigger coordinator.release() later (unlike the adapter's
                # t_p gateway path), so the target-resource lock would
                # otherwise never be freed and every subsequent conflict on
                # the same target would resolve as busy regardless of policy.
                runtime.coordinator.release(record.intent)
            results.append({
                "id": intent.id,
                "source": intent.source,
                "outcome": record.outcome.value if record.outcome else None,
                "reason": record.reason,
            })
    cv = sum(1 for r in results if r.get("outcome") == "EXECUTE" and r.get("source") == "ai"
             and any(x.get("source") == "xr" and x.get("outcome") == "EXECUTE" for x in results))
    winners = [i for i in intents if i.id in winner_ids]
    return {
        "results": results,
        "cv": cv,
        "winner": winners[0].source if len(winners) == 1 else None,
        "winners": [w.source for w in winners],
    }


@app.post("/v1/intents")
def submit_intent(body: dict):
    ver, trusted = refresh_context()
    if not trusted:
        return {
            "id": body.get("id"),
            "state": IntentState.REVOKED.value,
            "outcome": DecisionOutcome.REVOKE.value,
            "reason": "context_store_untrusted",
            "validation_latency_ms": 0.0,
            "context_version": ver,
            "context_trusted": False,
        }
    schema_err = _schema_error(body)
    if schema_err:
        return {
            "id": body.get("id") if isinstance(body, dict) else None,
            "state": IntentState.REVOKED.value,
            "outcome": DecisionOutcome.REVOKE.value,
            "reason": f"schema_invalid:{schema_err}",
            "validation_latency_ms": 0.0,
            "context_version": context_meta()["version"],
            "context_trusted": trusted,
        }
    intent = ActionIntent.from_dict(body)
    # Duplicate ids must return the existing record rather than reprocess:
    # a second pass through process_intent() would re-acquire the target
    # lock (coordinator.acquire) even though the idempotent confirm_publication
    # guard for the *first* pass has already marked it PUBLISH, so the
    # second acquisition would never be released.
    duplicate = runtime.lifecycle.get(intent.id) is not None if intent.id else False
    runtime.submit_intent(intent)
    result = runtime.lifecycle.get(intent.id) if duplicate else runtime.process_intent(intent)
    meta = context_meta()
    return {
        "id": intent.id,
        "state": result.state.value,
        "outcome": result.outcome.value if result.outcome else None,
        "reason": result.reason,
        "validation_latency_ms": result.validation_latency_ms,
        "context_version": meta["version"],
        "context_trusted": meta["store_trusted"],
        "duplicate": duplicate,
    }


class PublicationReport(BaseModel):
    published: bool
    reason: str
    context_version: int | None = None


@app.post("/v1/intents/{intent_id}/publication")
def report_publication(intent_id: str, body: PublicationReport):
    """Adapter-reported outcome of the t_p publication gate (recheck/publish/suppress).

    Closes the lifecycle as EXECUTED or REVOKED; this is the endpoint the
    actuator gateway calls after its own version/predicate recheck, per the
    Reference Model (Sec. V): validation at t_v does not by itself authorize
    release.
    """
    try:
        record = runtime.confirm_publication(
            intent_id, body.published, body.reason, context_version=body.context_version
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="intent not found")
    return {
        "id": intent_id,
        "state": record.state.value,
        "outcome": record.outcome.value if record.outcome else None,
        "publication_decision": record.publication_decision,
        "reason": record.reason,
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
    # An AUTHORIZED intent holds its target lock (coordinator.acquire); a
    # supervisory revoke must free it, or the target stays busy forever.
    runtime.coordinator.release(record.intent)
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
