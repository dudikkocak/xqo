"""The small, safe condition language of feed rules ("when" and "clear"). Never eval.

    disk.percent > 90                      fields by dotted name; a missing field is None (and compares False)
    cpu.percent >= 80 and mem.percent > 90
    status != 0 or kind == 'failed'
    'Traceback' in text                    substring test (also on lists); "not in" works too
    severity >= 'error'                    severity names compare by level: debug < info < warning < error < critical
    not running

Only names, numbers, strings, True/False/None, comparisons, and / or / not and parentheses are allowed."""
from __future__ import annotations

import ast
import operator

SEVERITIES = ("debug", "info", "warning", "error", "critical")
_CMP = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt, ast.LtE: operator.le,
        ast.Gt: operator.gt, ast.GtE: operator.ge}


def level(sev) -> int:
    """Index of a severity name (unknown = info)."""
    return SEVERITIES.index(sev) if sev in SEVERITIES else 1


def _name(node) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_name(node.value)}.{node.attr}"
    raise ValueError("expected a field name")


class Expr:
    def __init__(self, text: str):
        self.text = str(text or "").strip()
        if not self.text:
            raise ValueError("empty condition")
        if len(self.text) > 500:
            raise ValueError("condition longer than 500 characters")
        try:
            tree = ast.parse(self.text, mode="eval")
        except SyntaxError as e:
            raise ValueError(f"cannot parse condition {self.text!r}: {e.msg}") from None
        self.tree = tree.body
        self.fields: set[str] = set()
        self._check(self.tree)

    def _check(self, n) -> None:
        if isinstance(n, ast.BoolOp):
            for v in n.values:
                self._check(v)
        elif isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not):
            self._check(n.operand)
        elif isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.USub) and isinstance(n.operand, ast.Constant):
            pass
        elif isinstance(n, ast.Compare):
            for op in n.ops:
                if type(op) not in _CMP and not isinstance(op, (ast.In, ast.NotIn)):
                    raise ValueError(f"{self.text!r}: unsupported comparison")
            for v in (n.left, *n.comparators):
                self._check(v)
        elif isinstance(n, (ast.Name, ast.Attribute)):
            self.fields.add(_name(n))
        elif isinstance(n, ast.Constant):
            if not isinstance(n.value, (int, float, str, bool, type(None))):
                raise ValueError(f"{self.text!r}: unsupported value {n.value!r}")
        else:
            raise ValueError(f"{self.text!r}: only fields, values, comparisons, and/or/not are allowed "
                             f"(found {type(n).__name__})")

    def __call__(self, fields: dict) -> bool:
        try:
            return bool(self._eval(self.tree, fields))
        except Exception:  # a type mismatch (str > int) never fires a rule
            return False

    def _eval(self, n, f: dict):
        if isinstance(n, ast.BoolOp):
            if isinstance(n.op, ast.And):
                return all(self._eval(v, f) for v in n.values)
            return any(self._eval(v, f) for v in n.values)
        if isinstance(n, ast.UnaryOp):
            v = self._eval(n.operand, f)
            return (not v) if isinstance(n.op, ast.Not) else -v
        if isinstance(n, ast.Constant):
            return n.value
        if isinstance(n, (ast.Name, ast.Attribute)):
            return f.get(_name(n))
        left = self._eval(n.left, f)
        for op, right_node in zip(n.ops, n.comparators):
            right = self._eval(right_node, f)
            if isinstance(op, (ast.In, ast.NotIn)):
                if right is None:
                    return False
                ok = str(left) in right if isinstance(right, str) else left in right
                ok = ok if isinstance(op, ast.In) else not ok
            else:
                if left is None or right is None:
                    if type(op) not in (ast.Eq, ast.NotEq):
                        return False
                a, b = left, right
                if self._sev(n.left, right_node, a, b):
                    a, b = level(a), level(b)
                ok = _CMP[type(op)](a, b)
            if not ok:
                return False
            left = right
        return True

    @staticmethod
    def _sev(ln, rn, a, b) -> bool:
        names = {_name(x) for x in (ln, rn) if isinstance(x, (ast.Name, ast.Attribute))}
        return "severity" in names and isinstance(a, str) and isinstance(b, str)
