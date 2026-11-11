"""Entity profile schema and the safe condition evaluator.

A template item carries a `condition_expr` such as:

    has_inventory
    not has_inventory
    has_borrowings and is_listed
    turnover_inr > 50000000

It is evaluated against the engagement's entity profile. Only a small, whitelisted subset of Python
expression syntax is accepted (names, constants, and/or/not, comparisons, in/not in) - the text is
parsed with `ast` and walked node by node; it is never passed to eval().
"""
import ast
from typing import Any, Mapping, Optional


class ConditionError(ValueError):
    """The condition text is invalid or refers to an unknown profile attribute."""


# --------------------------------------------------------------------------------------------
# Entity profile schema
# --------------------------------------------------------------------------------------------
PROFILE_FIELDS = [
    {"key": "has_inventory", "label": "Holds inventory / stock", "kind": "bool",
     "hint": "Trading or manufacturing. A pure services business does not."},
    {"key": "has_fixed_assets", "label": "Has fixed assets", "kind": "bool",
     "hint": "Block of assets is not nil."},
    {"key": "has_foreign_remittance", "label": "Makes foreign remittances", "kind": "bool",
     "hint": "Drives Form 15CA / 15CB."},
    {"key": "has_borrowings", "label": "Has borrowings", "kind": "bool",
     "hint": "Bank or other term / working-capital loans."},
    {"key": "has_exports", "label": "Exports goods or services", "kind": "bool", "hint": ""},
    {"key": "is_gst_registered", "label": "GST registered", "kind": "bool", "hint": ""},
    {"key": "has_employees", "label": "Has employees on payroll", "kind": "bool",
     "hint": "PF / ESI / TDS on salary."},
    {"key": "has_related_parties", "label": "Has related-party transactions", "kind": "bool", "hint": ""},
    {"key": "is_listed", "label": "Listed entity", "kind": "bool", "hint": "Brings LODR / secretarial compliance."},
    {"key": "turnover_inr", "label": "Annual turnover", "kind": "number",
     "hint": "Entered in INR crore in the editor, stored in INR."},
]
PROFILE_KEYS = [f["key"] for f in PROFILE_FIELDS]
_KIND = {f["key"]: f["kind"] for f in PROFILE_FIELDS}
CRORE = 10_000_000


def default_profile() -> dict:
    return {k: (False if _KIND[k] == "bool" else 0) for k in PROFILE_KEYS}


def normalise_profile(raw: Optional[Mapping[str, Any]]) -> dict:
    """Merge `raw` over the defaults. Unknown keys are dropped; every known key is present."""
    profile = default_profile()
    for key, value in (raw or {}).items():
        kind = _KIND.get(key)
        if kind == "bool":
            if isinstance(value, str):
                value = value.strip().lower() in ("1", "true", "yes", "on")
            profile[key] = bool(value)
        elif kind == "number":
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise ConditionError(f"{key} must be a number.") from exc
            if number < 0:
                raise ConditionError(f"{key} cannot be negative.")
            profile[key] = int(round(number))
    return profile


# --------------------------------------------------------------------------------------------
# Parsing / validation
# --------------------------------------------------------------------------------------------
_CMP_OPS = {
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}


def _parse(expr: str) -> ast.AST:
    try:
        return ast.parse(expr.strip(), mode="eval").body
    except SyntaxError as exc:
        raise ConditionError(f"Invalid condition '{expr}': {exc.msg}") from exc


def _walk(node: ast.AST, names: list) -> None:
    """Reject anything outside the whitelist; collect profile names in order of appearance."""
    if isinstance(node, ast.BoolOp):
        for v in node.values:
            _walk(v, names)
    elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.Not, ast.USub)):
        _walk(node.operand, names)
    elif isinstance(node, ast.Compare):
        if not all(type(op) in _CMP_OPS for op in node.ops):
            raise ConditionError("Unsupported comparison operator.")
        _walk(node.left, names)
        for c in node.comparators:
            _walk(c, names)
    elif isinstance(node, ast.Name):
        if node.id not in PROFILE_KEYS:
            raise ConditionError(f"Unknown entity attribute '{node.id}'.")
        if node.id not in names:
            names.append(node.id)
    elif isinstance(node, ast.Constant):
        if not isinstance(node.value, (bool, int, float, str)):
            raise ConditionError("Unsupported constant in condition.")
    elif isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        for e in node.elts:
            _walk(e, names)
    else:
        raise ConditionError(f"Unsupported syntax in condition: {type(node).__name__}.")


def condition_names(expr: str) -> list:
    names: list = []
    _walk(_parse(expr), names)
    return names


def validate_condition(expr: Optional[str]) -> None:
    if expr and expr.strip():
        condition_names(expr)


# --------------------------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------------------------
def _eval(node: ast.AST, profile: Mapping[str, Any]) -> Any:
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            return all(_eval(v, profile) for v in node.values)
        return any(_eval(v, profile) for v in node.values)
    if isinstance(node, ast.UnaryOp):
        value = _eval(node.operand, profile)
        return (not value) if isinstance(node.op, ast.Not) else -value
    if isinstance(node, ast.Compare):
        left = _eval(node.left, profile)
        for op, comparator in zip(node.ops, node.comparators):
            right = _eval(comparator, profile)
            if not _CMP_OPS[type(op)](left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.Name):
        return profile[node.id]
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return [_eval(e, profile) for e in node.elts]
    raise ConditionError("Unsupported syntax in condition.")  # unreachable after validation


def evaluate(expr: Optional[str], profile: Mapping[str, Any]) -> bool:
    """True when the item applies. An empty condition always applies."""
    if not expr or not expr.strip():
        return True
    tree = _parse(expr)
    _walk(tree, [])
    return bool(_eval(tree, profile))


def _fmt(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return f"{value:,.0f}"
    return str(value)


def explain(expr: str, profile: Mapping[str, Any], result: bool) -> str:
    """Human-readable reason for the outcome, e.g. 'has_inventory is false'."""
    tree = _parse(expr)
    names: list = []
    _walk(tree, names)
    if isinstance(tree, ast.Name):
        return f"{tree.id} is {_fmt(profile[tree.id])}"
    if isinstance(tree, ast.UnaryOp) and isinstance(tree.op, ast.Not) and isinstance(tree.operand, ast.Name):
        return f"{tree.operand.id} is {_fmt(profile[tree.operand.id])}"
    values = ", ".join(f"{n} = {_fmt(profile[n])}" for n in names)
    return f"rule '{expr.strip()}' is {_fmt(result)} ({values})"
