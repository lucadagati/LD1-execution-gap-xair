from __future__ import annotations

import operator
import re
import time
from typing import Any, Iterable

from xair.core.deep_merge import deep_merge
from xair.core.models import ActionIntent

_EVAL_TIMEOUT_S = 0.005

# Predicate grammar (paper Sec. IV): Path Op Literal, conjunctive.
#   Path    := identifier ('.' identifier)*
#   Op      := '==' | '=' | '!=' | '<' | '<=' | '>' | '>='   ('=' is an alias of '==')
#   Literal := quoted string | number | true | false
_PATTERN = re.compile(
    r"^(?P<path>\w+(?:\.\w+)*)\s*"
    r"(?P<op>==|!=|<=|>=|<|>|=)\s*"
    r"(?:'(?P<sval>[^']*)'"
    r"|\"(?P<dval>[^\"]*)\""
    r"|(?P<bval>true|false|True|False)"
    r"|(?P<nval>-?\d+(?:\.\d+)?))$"
)

_EQUALITY = {"==": operator.eq, "!=": operator.ne}
_ORDERING = {"<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge}


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _resolve(context: dict, path: str) -> Any:
    node: Any = context
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def check_expression(expr: str, context: dict) -> tuple[bool, str]:
    """Evaluate one predicate against ``context``; every failure mode is closed.

    Typing is strict: ordering operators require numeric operands, Booleans
    compare only with Booleans, strings only with strings. A missing path or
    an operand-type mismatch is reported as a failure, never coerced into a
    silently true comparison and never raised to the caller.
    """
    expr = (expr or "").strip()
    if not expr:
        return False, "empty_expression"
    m = _PATTERN.match(expr)
    if not m:
        return False, f"unsupported: {expr}"
    op = "==" if m.group("op") == "=" else m.group("op")
    left = _resolve(context, m.group("path"))

    right: Any
    if m.group("sval") is not None:
        right = m.group("sval")
    elif m.group("dval") is not None:
        right = m.group("dval")
    elif m.group("bval") is not None:
        right = m.group("bval").lower() == "true"
    else:
        nval = m.group("nval")
        right = float(nval) if "." in nval else int(nval)

    if left is None:
        return False, f"missing_path: {expr}"

    # MES snapshots may report Booleans or numbers as strings; align the
    # context value with the literal's type before comparing.
    if isinstance(right, bool) and isinstance(left, str) and left.lower() in ("true", "false"):
        left = left.lower() == "true"
    elif _is_number(right) and isinstance(left, str):
        try:
            left = float(left)
        except ValueError:
            return False, f"ill_typed: {expr}"
    elif _is_number(left) and isinstance(right, str):
        try:
            right = float(right)
        except ValueError:
            return False, f"ill_typed: {expr}"

    if op in _ORDERING:
        if not (_is_number(left) and _is_number(right)):
            return False, f"ill_typed: {expr}"
        return (_ORDERING[op](left, right), expr)

    comparable = (
        (isinstance(left, bool) and isinstance(right, bool))
        or (_is_number(left) and _is_number(right))
        or (isinstance(left, str) and isinstance(right, str))
    )
    if not comparable:
        return False, f"ill_typed: {expr}"
    return (_EQUALITY[op](left, right), expr)


def evaluate_predicates(
    safety_constraints: Iterable[str],
    preconditions: Iterable[str],
    context: dict,
    timeout_s: float = _EVAL_TIMEOUT_S,
) -> tuple[bool, str]:
    """Conjunctive evaluation with a wall-clock bound; stateless and thread-safe."""
    started = time.perf_counter()
    for label, exprs in (("safety_constraint_failed", safety_constraints), ("precondition_failed", preconditions)):
        for expr in exprs:
            if time.perf_counter() - started >= timeout_s:
                return False, "eval_timeout"
            ok, msg = check_expression(expr, context)
            if not ok:
                return False, f"{label}: {msg}"
    return True, "context_ok"


class ContextValidator:
    """Evaluate preconditions and safety constraints against a context snapshot."""

    def __init__(self, context: dict | None = None, eval_timeout_s: float = _EVAL_TIMEOUT_S) -> None:
        self._context = context or {}
        self._eval_timeout_s = eval_timeout_s

    def update_context(self, context: dict) -> None:
        self._context = deep_merge(self._context, context)

    def replace_context(self, context: dict) -> None:
        self._context = dict(context)

    @property
    def context(self) -> dict:
        return dict(self._context)

    def validate(self, intent: ActionIntent, context: dict | None = None) -> tuple[bool, str]:
        """Validate against ``context`` if given, else against the installed snapshot."""
        snapshot = self._context if context is None else context
        return evaluate_predicates(
            intent.safety_constraints, intent.preconditions, snapshot, self._eval_timeout_s
        )
