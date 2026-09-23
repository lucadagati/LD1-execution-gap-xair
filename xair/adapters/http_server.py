from __future__ import annotations

import json
from pathlib import Path

import jsonschema
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from xair import __version__
from xair.adapters.runtime_state import read_snapshot, runtime, update_context_store
from xair.core.lifecycle import InvalidTransition
from xair.core.models import ActionIntent, DecisionOutcome, IntentState

app = FastAPI(title="XAIR Runtime", version=__version__)

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


def _rejection(intent_id, reason: str, version: int, trusted: bool) -> dict:
    return {
        "id": intent_id,
        "state": IntentState.REVOKED.value,
        "outcome": DecisionOutcome.REVOKE.value,
        "reason": reason,
        "validation_latency_ms": 0.0,
        "context_version": version,
        "context_trusted": trusted,
        "duplicate": False,
    }


@app.post("/v1/intents/batch")
def submit_intent_batch(body: list[dict]):
    """Submit intents in one batch; conflicts on the same target are resolved by policy."""
    ctx, ver, trusted = read_snapshot()
    results = []
    intents: list[ActionIntent] = []
    for item in body:
        err = _schema_error(item)
        if err:
            results.append({"id": item.get("id"), "source": item.get("source"), "outcome": "REVOKE", "reason": f"schema_invalid:{err}"})
        elif not trusted:
            results.append({"id": item.get("id"), "source": item.get("source"), "outcome": "REVOKE", "reason": "context_store_untrusted"})
        else:
            intents.append(ActionIntent.from_dict(item))

    # Resolve winners per target_entity: intents on different resources are
    # not in conflict and must not be forced into a false conflict_loser.
    by_target: dict[str, list[ActionIntent]] = {}
    for intent in intents:
        by_target.setdefault(intent.payload.target_entity, []).append(intent)
    winners: list[ActionIntent] = []
    for group in by_target.values():
        winner, losers = runtime.coordinator.resolve(group)
        for loser in losers:
            record, duplicate = runtime.admit(loser)
            if not duplicate:
                runtime.lifecycle.transition(loser.id, IntentState.REVOKED, DecisionOutcome.REVOKE, "conflict_loser")
            results.append({"id": loser.id, "source": loser.source, "outcome": record.outcome.value if record.outcome else None, "reason": record.reason})
        if winner is not None:
            winners.append(winner)
    for winner in winners:
        record, duplicate = runtime.admit(winner)
        if not duplicate:
            record = runtime.process_intent(winner, context=ctx, context_version=ver)
            if record.state == IntentState.AUTHORIZED:
                # The batch endpoint has no downstream t_p report, so the
                # target lock taken at authorization is released here.
                runtime.coordinator.release(record.intent)
        results.append({
            "id": winner.id,
            "source": winner.source,
            "outcome": record.outcome.value if record.outcome else None,
            "reason": record.reason,
        })
    executed_per_target: dict[str, int] = {}
    for intent in intents:
        rec = runtime.lifecycle.get(intent.id)
        if rec is not None and rec.outcome == DecisionOutcome.EXECUTE:
            key = intent.payload.target_entity
            executed_per_target[key] = executed_per_target.get(key, 0) + 1
    return {
        "results": results,
        # Conflict violations: targets on which more than one intent was authorized.
        "cv": sum(1 for n in executed_per_target.values() if n > 1),
        "winner": winners[0].source if len(winners) == 1 else None,
        "winners": [w.source for w in winners],
        "context_version": ver,
    }


@app.post("/v1/intents")
def submit_intent(body: dict):
    ctx, ver, trusted = read_snapshot()
    if not trusted:
        return _rejection(body.get("id"), "context_store_untrusted", ver, False)
    schema_err = _schema_error(body)
    if schema_err:
        return _rejection(body.get("id"), f"schema_invalid:{schema_err}", ver, trusted)
    intent = ActionIntent.from_dict(body)
    # A duplicate id returns the retained record and is never re-validated,
    # so it cannot re-acquire the target nor trigger a second release.
    record, duplicate = runtime.admit(intent)
    if not duplicate:
        record = runtime.process_intent(intent, context=ctx, context_version=ver)
    return {
        "id": intent.id,
        "state": record.state.value,
        "outcome": record.outcome.value if record.outcome else None,
        "reason": record.reason,
        "validation_latency_ms": record.validation_latency_ms,
        # Version of the snapshot this decision was validated against (v).
        "context_version": record.context_version,
        "context_trusted": trusted,
        "duplicate": duplicate,
    }


class PublicationReport(BaseModel):
    published: bool
    reason: str
    context_version: int | None = None


@app.post("/v1/intents/{intent_id}/publication")
def report_publication(intent_id: str, body: PublicationReport):
    """Adapter-reported outcome of the t_p publication gate (recheck/publish/suppress).

    Closes the lifecycle as EXECUTED or REVOKED. Only an AUTHORIZED intent can
    be closed; a report for any other state is rejected with 409.
    """
    try:
        record = runtime.confirm_publication(
            intent_id, body.published, body.reason, context_version=body.context_version
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="intent not found")
    except InvalidTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc))
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
        "context_version": record.context_version,
        "publication_decision": record.publication_decision,
    }


@app.delete("/v1/intents/{intent_id}")
def revoke_intent(intent_id: str):
    """Supervisory revoke of a non-terminal intent (an executed one cannot be rolled back)."""
    try:
        record = runtime.supervisory_revoke(intent_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="intent not found")
    except InvalidTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"id": intent_id, "state": record.state.value}


@app.get("/v1/metrics")
def metrics():
    return runtime.get_metrics()


@app.get("/v1/context/snapshot")
def get_context_snapshot():
    ctx, ver, trusted = read_snapshot()
    return {"ok": True, "context": ctx, "context_version": ver, "context_trusted": trusted}


@app.post("/v1/context/snapshot")
def context_snapshot(body: dict):
    ver, trusted = update_context_store(body)
    return {"ok": trusted, "context_version": ver, "context_trusted": trusted}
