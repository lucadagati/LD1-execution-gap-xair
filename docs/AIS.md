# AIS contract, XAIR lifecycle, and HTTP API

Reference for the Action Intent Schema (`schemas/action-intent-v1.json`, revision 1.1) and the
runtime that enforces it. The rule throughout is: **AIS declares only what XAIR enforces or records.**

## 1. Fields

| Field | Meaning | Enforced / used at |
|---|---|---|
| `id` (UUID) | intent identity | idempotency: a duplicate within the retention window returns the retained record and is never released twice |
| `source` | producer class (`cv`, `ai`, `xr`, `human`, `mes`, `composite`) | audit; tie-break in batch conflicts; **not authenticated** |
| `timestamp_decision` | decision instant `t_d` | anchor of every age check |
| `freshness_window_ms` (`w`) | maximum age at validation `t_v` | validation: older intents are revoked without evaluating predicates |
| `deadline_ms` (`d`, optional) | maximum age at **release** | validation, optimistic gate `t_g`, atomic commit `t_c` (on the store clock), release guard, and version-conditional controller commands (OPC UA controller only); defaults to `w`. It does **not** bound the physical completion of the action |
| `preconditions` (`P`) | contextual predicates declared by the producer | `t_v`, `t_g`/`t_c` (through versions), consumer recheck (optional) |
| `safety_constraints` (`S`) | **hard constraints declared by the producer**, same language as `P` | as `P`; a false or unevaluable constraint revokes. Not certified safety functions |
| `payload.action_type` | requested action | selects operator-mandated predicates (server-side policy, case-insensitive) |
| `payload.target_entity` | resource acted on | at most one authorized, not yet committed or released intent per target (`DELAY` otherwise) |
| `payload.parameters` | action-specific parameters (open object) | passed to the actuator unchanged, except by a degradation policy |
| `payload.degradation_policy` | `none` or `reduce_speed` | `reduce_speed` halves `parameters.speed_factor` (default 1.0); the transformed intent is revalidated under the same id and is the payload returned for release |
| `priority` | integer | batch conflict winner; dequeue order |
| `correlation_id` (optional UUID) | opaque id | recorded for audit only; **not used for coordination** |

Unknown fields are rejected at ingress (`additionalProperties: false` on the intent, the payload and
predicate objects); `parameters` stays open because it is action-specific.

**Predicates** are `Path Op Literal` comparisons (conjunctions only). Evaluation is typed, deterministic,
side-effect free and time-bounded; empty, ill-typed, unparsable or timed-out expressions and missing
paths fail closed.

## 2. Lifecycle

Validation (`t_v`) ends in `AUTHORIZED`, `DELAYED`, `DEGRADED`, `REVOKED` or `EXPIRED`
(`DELAYED`/`DEGRADED` return to `PENDING` under the same id). After that, **authorization, commit,
release and effect are distinct states**:

| State | Entered when | Next |
|---|---|---|
| `AUTHORIZED` | validation passed; nothing released | `COMMITTED`, `RELEASED`, `WITHHELD`, `REVOKED` (supervisory) |
| `COMMITTED` | the atomic commit appended the authorization to the actuation log | `RELEASED`, `WITHHELD` (release guard) |
| `RELEASED` | the gateway reports the middleware or controller call (`t_m`) | `EFFECT_CONFIRMED`, `FAILED`, `UNKNOWN_EFFECT` |
| `WITHHELD` | the gate, the commit, or a release guard refused release (terminal) | — |
| `EFFECT_CONFIRMED` | the actuator/controller reported the effect (`t_a`) (terminal) | — |
| `FAILED` | the actuator/controller reported no effect, e.g. a refused version-conditional command (terminal) | — |
| `UNKNOWN_EFFECT` | released without feedback (e.g. a ROS 2 publication) | `EFFECT_CONFIRMED`, `FAILED` |

No state means "executed" because an intent was authorized or committed. The record keeps four items
separately: the policy outcome at `t_v` (`EXECUTE`/`DELAY`/`DEGRADE`/`REVOKE`), the lifecycle state,
the release decision (`RELEASE`/`WITHHOLD`), and the effect status. SER and FPR are computed from the
release decision. A supervisory revoke is accepted until the intent is committed or released. The
atomic commit refuses a revoked intent; the optimistic gate reads the intent's state together with its
snapshot at `t_g` and withholds any intent that is not `AUTHORIZED` there, including one XAIR no longer knows (fail closed). In the optimistic mode, a revoke that arrives after
the gate's read, in `(t_g, t_m]`, can no longer stop the release; a release reported afterwards is
rejected with 409 and recorded in the audit trail.

The target lock taken at authorization is released at commit, release, or withholding.

## 3. HTTP API (FastAPI, `xair/adapters/http_server.py`)

| Endpoint | Effect |
|---|---|
| `POST /v1/intents` | schema check, policy, validation at `t_v`; a degraded intent is revalidated in the same request and `authorized_payload` returns the payload to release; resubmitting the id of a `DELAYED` intent revalidates it |
| `POST /v1/intents/batch` | validation only (no release follows); conflicts resolved per target; a stop-class intent is always authorized and preempts the other intents on its target (`preempted_by_stop`) |
| `POST /v1/intents/{id}/commit` | atomic commit at `t_c`; `committed: true` → `COMMITTED`, otherwise `WITHHELD`. `scope` is `readset` or `truth`; `margin_ms` must be finite and ≥ 0 (it can only tighten the deadline), otherwise 422 |
| `POST /v1/intents/{id}/release` | gateway report: `released: true` → `RELEASED` (from `AUTHORIZED`, after the version check; or from `COMMITTED`), `false` → `WITHHELD`; optional `effect_status` reports the effect in the same request |
| `POST /v1/intents/{id}/publication` | deprecated alias of `/release` (`published` = `released`) |
| `POST /v1/intents/{id}/withheld` | only for a `COMMITTED` intent: appends a tombstone to the actuation log and makes it `WITHHELD`; unknown id 404, other states 409, and nothing is written in either case; a replay is idempotent |
| `POST /v1/intents/{id}/effect` | `confirmed` / `failed` / `unknown` → `EFFECT_CONFIRMED` / `FAILED` / `UNKNOWN_EFFECT` |
| `GET /v1/intents/{id}` | state, outcome, release decision, commit sequence, effect status, reason |
| `DELETE /v1/intents/{id}` | supervisory revoke (409 once committed or released) |
| `GET/PUT /v1/policy` | operator policy: mandatory predicates and action class per action type, class-wide predicates (`class:<name>`); with `XAIR_POLICY_TOKEN` set, `PUT` needs `Authorization: Bearer <token>` (401 otherwise); an invalid policy returns 422 and the previous one stays |
| `GET/POST /v1/context/snapshot` | versioned context read / update; `GET ...?intent_id=` also returns that intent's state (`intent_state`), which the optimistic gate uses |
| `GET /v1/actuations` | actuation log (commit order) |

Replays of the same commit, release or effect report are idempotent; a conflicting report returns 409
and is audited (`rejected_report:<state>:<reason>`).

## 4. Running example

`line.state` is the line's run mode, set by the operator (`RUN`, or `PAUSED`, e.g. to clear a jam).
`RESUME` restarts the station's conveyor, which is stopped while the station waits; it is admissible
only in `RUN`, hence `line.state == 'RUN'` (and `gripper.state == 'OPEN'`). The emulated controller
(`scripts/cell_controller.py`) and the OpenPLC program (`docker/openplc/cell_ctl.st`, register 9)
apply the effect `conveyor = RUNNING`, and a pause stops the conveyor; a `RESUME` executed on a paused
line is a stale effect. Regression tests: `tests/test_cell_controller.py`.

## 5. Trust model, action taxonomy, and limits of enforcement

- **Trust model.** Producers are non-malicious but possibly faulty, on a controlled network. Neither
  producers nor context writers are authenticated. Policy changes can be restricted to the operator
  (`XAIR_POLICY_TOKEN`); without a token the policy endpoint is as open as the intent endpoint, and the
  policy guards against omission only. AIS validation checks the consistency of what an intent
  declares, not its completeness. XAIR enforces intent validity; policy compliance holds only if the
  policy is complete and protected; physical safety stays with the safety-rated layer.
- **Action classes.** The operator's policy assigns each action type a class: `stop`, `start`,
  `motion`, `reset`, `shutdown`, or `other` (default). Example:
  `{"RESUME": {"class": "start", "require": ["line.state == 'RUN'"]}, "STOP": {"class": "stop"},
  "class:reset": ["operator.present == true"]}` (a plain list still means mandatory predicates, class
  `other`). Producers cannot set a class: AIS has no such field and rejects unknown fields. A
  `stop`-class intent is authorized on schema validity alone (`risk_reducing_action`): its predicates
  are recorded but not evaluated, age, stale or unavailable context and target conflicts never revoke
  it, it takes no target lock, and the gateway releases it without recheck or commit. The policy
  refuses predicates for the stop class. A stop also fences the intents of its target in flight: on
  authorization XAIR writes `stop.<key>` (the stop time in microseconds; `<key>` is one of
  `XAIR_STOP_BUCKETS` = 64 hash buckets of targets, so the context stays bounded and a stop also fences the
  in-flight intents of the other targets of its bucket) into the context, and every other intent on that
  target gets the predicate `stop.<key> <= <its decision time minus the clock bound ε>` (read-set
  predicate, not registered for truth versions), so a stop issued after the decision falsifies the intent at its
  gate recheck or commit (`XAIR_STOP_FENCE=0` switches this off, as in E27). This path is for supervisory stops; emergency stops stay on
  the safety-rated path and never pass through XAIR. Tests: `tests/test_action_classes.py`.
- **Restarts.** Lifecycle records are kept in XAIR's memory. After a restart the optimistic gate fails closed on intents XAIR no longer knows (`intent_unknown_at_gate`), but the withholding cannot be recorded against the lost record. The idempotency table lives in Redis (`xair:seen:<id>`, set with NX and the retention window as time to live), so an identifier resubmitted after the restart is refused with `duplicate_after_restart` instead of being validated again; with Redis unreachable the claim fails closed. Context, versions, committed authorizations and their duplicate check, and the actuation log also live in the store and survive. The emulated cell controller never reuses a version across restarts (`--state-file`, otherwise a boot-epoch seed); the OpenPLC program still restarts its version at 0, so its version-conditional guarantee holds within one run of the runtime.
- **Limits.** A check on a replica leaves the interval up to the effect; only a check in the
  controller's scan closes it for controller-owned context. The OpenPLC program checks the version but
  not the deadline. The actuation log gives at most one effect per committed authorization only with an
  actuator that persists its deduplication record with the effect; it does not give exactly-once
  physical effects. Certified stops, torque limits and collision envelopes remain in the PLC or robot
  safety controller.

## 6. Versioning and compatibility

The schema `$id` carries the major version (`action-intent-v1`); `x-ais-revision` the revision.

- A **revision** may add optional fields or relax constraints; every intent valid under the previous
  revision stays valid.
- Anything that rejects intents a previous revision accepted, or changes the meaning of a field,
  requires a new major version (`action-intent-v2.json`, new `$id`), served alongside the old one.

Revision 1.1 (this one) is an exception, made before any external release: it removed fields and
values that the prototype accepted but did not honour — `revocable` (accepted and ignored),
`degradation_policy` values `partial_pose` and `hold_position` (accepted and then released with the
original payload) — and closed the objects to unknown fields. The frozen experiment data were produced
with intents that are valid under 1.1, except E8/E9, which carried a harness tag `payload.run` that
the runtime ignored; the harnesses now send it as `payload.parameters.run`.
