from __future__ import annotations

import re
import time

from xair.core.deep_merge import deep_merge
from xair.core.models import ActionIntent

_EVAL_TIMEOUT_S = 0.005


class ContextValidator:
    """Evaluate preconditions and safety constraints against context snapshot."""

    _PATTERN = re.compile(
        r"^(?P<path>\w+(?:\.\w+)*)\s*"
        r"(?P<op>==|!=|<=|>=|<|>)\s*"
        r"(?:'(?P<sval>[^']*)'"
        r"|\"(?P<dval>[^\"]*)\""
        r"|(?P<bval>true|false|True|False)"
        r"|(?P<nval>-?\d+(?:\.\d+)?))$"
    )

    def __init__(
        self, context: dict | None = None, eval_timeout_s: float = _EVAL_TIMEOUT_S
    ) -> None:
        self._context = context or {}
        self._eval_timeout_s = eval_timeout_s
        self._eval_started_at: float | None = None

    def update_context(self, context: dict) -> None:
        self._context = deep_merge(self._context, context)

    @property
    def context(self) -> dict:
        return dict(self._context)

    def validate(self, intent: ActionIntent) -> tuple[bool, str]:
        self._eval_started_at = time.perf_counter()
        for expr in intent.safety_constraints:
            ok, msg = self._check(expr)
            if not ok:
                return False, f"safety_constraint_failed: {msg}"
        for expr in intent.preconditions:
            ok, msg = self._check(expr)
            if not ok:
                return False, f"precondition_failed: {msg}"
        return True, "context_ok"

    def _timed_out(self) -> bool:
        if self._eval_started_at is None:
            return False
        return (time.perf_counter() - self._eval_started_at) >= self._eval_timeout_s

    def _resolve(self, path: str):
        node = self._context
        for part in path.split("."):
            if not isinstance(node, dict):
                return None
            node = node.get(part)
        return node

    def _check(self, expr: str) -> tuple[bool, str]:
        if self._timed_out():
            return False, "eval_timeout"
        expr = expr.strip()
        if not expr:
            return False, "empty_expression"
        expr = re.sub(r"(?<![!<>=])=(?!=)", "==", expr)
        m = self._PATTERN.match(expr)
        if not m:
            return False, f"unsupported: {expr}"
        path = m.group("path")
        op = m.group("op")
        left = self._resolve(path)
        if m.group("sval") is not None:
            right = m.group("sval")
        elif m.group("dval") is not None:
            right = m.group("dval")
        elif m.group("bval") is not None:
            right = m.group("bval").lower() == "true"
        else:
            nval = m.group("nval")
            right = float(nval) if "." in nval else int(nval)

        # MES snapshots may report booleans as strings; align before comparing.
        if isinstance(right, bool) and isinstance(left, str) and left.lower() in ("true", "false"):
            left = left.lower() == "true"
        elif not isinstance(left, bool) and isinstance(left, (int, float)) and isinstance(right, str):
            try:
                right = float(right) if "." in right else int(right)
            except ValueError:
                pass

        ops = {
            "==": lambda a, b: a == b,
            "!=": lambda a, b: a != b,
            "<": lambda a, b: a < b,
            "<=": lambda a, b: a <= b,
            ">": lambda a, b: a > b,
            ">=": lambda a, b: a >= b,
        }
        if left is None:
            return False, expr
        if ops[op](left, right):
            return True, expr
        return False, expr
