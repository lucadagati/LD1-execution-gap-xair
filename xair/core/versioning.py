"""Per-path context versions and intent read-sets.

The store keeps, next to the global snapshot version, the version at which
each leaf path last changed *value*. An intent's read-set is the set of paths
its predicates reference; its read-set version is the latest change among
paths related to them (equal, ancestor, or descendant). Comparing read-set
versions at the gate instead of the global version means that an update to
an unrelated field, or a rewrite of a field with the same value, does not
invalidate the intent, while any value change on a path it depends on does,
including a change that is undone before the gate (A-B-A).
"""

from __future__ import annotations

import re
from typing import Any, Iterable

_PATH = re.compile(r"^\s*(\w+(?:\.\w+)*)")

# readset: latest value change on the paths the intent reads; global: any update;
# truth: latest change of the truth value of the intent's predicates;
# predicate: no version condition, the gate only re-evaluates the predicates.
VERSION_SCOPES = ("readset", "global", "truth", "predicate")


def leaf_items(patch: dict, prefix: str = "") -> Iterable[tuple[str, Any]]:
    """Yield (dotted_path, value) for every leaf of a nested patch."""
    for key, val in patch.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(val, dict) and val:
            yield from leaf_items(val, path)
        else:
            yield path, val


def get_path(context: dict, path: str) -> Any:
    node: Any = context
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return _MISSING
        node = node[part]
    return node


class _Missing:
    def __repr__(self) -> str:
        return "<missing>"


_MISSING = _Missing()


def same_value(a: Any, b: Any) -> bool:
    """Equality as the strictly typed evaluator sees it: ``1``, ``1.0`` and ``True``
    differ, so a type change on a path is a value change (int and float compare
    by value, as the evaluator treats both as numbers)."""
    num = (int, float)
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, num) and isinstance(b, num):
        return a == b
    return type(a) is type(b) and a == b


def changed_paths(old: dict, patch: dict) -> list[str]:
    """Leaf paths of ``patch`` whose value differs from ``old``.

    An empty object merged into an existing object leaves it unchanged."""
    out = []
    for p, v in leaf_items(patch):
        cur = get_path(old, p)
        if v == {} and isinstance(cur, dict):
            continue
        if not same_value(cur, v):
            out.append(p)
    return out


def read_set(expressions: Iterable[str]) -> list[str]:
    """Context paths referenced by predicate expressions (unparseable ones are skipped)."""
    out: list[str] = []
    for expr in expressions:
        m = _PATH.match(expr or "")
        if m and m.group(1) not in out:
            out.append(m.group(1))
    return out


def _related(a: str, b: str) -> bool:
    return a == b or a.startswith(b + ".") or b.startswith(a + ".")


def read_set_version(path_versions: dict[str, int], paths: Iterable[str]) -> int:
    """Latest value-change version among stored paths related to ``paths`` (0 if none)."""
    paths = list(paths)
    return max((v for p, v in path_versions.items() if any(_related(p, r) for r in paths)), default=0)


def predicate_version(pred_versions: dict[str, list], exprs: Iterable[str]) -> int:
    """Latest truth-change version among ``exprs``; -1 if one is not registered.

    Callers treat -1 as a mismatch, never as a version to compare equal."""
    out = 0
    for e in exprs:
        entry = pred_versions.get(e)
        if entry is None:
            return -1
        out = max(out, int(entry[0]))
    return out
