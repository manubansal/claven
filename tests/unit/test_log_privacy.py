"""Logs at INFO and above must not carry email addresses (CASA; privacy policy).

Prod logs at INFO, and the privacy policy says only message IDs appear in
logs. Users are identified by user_id; correspondents not at all. DEBUG logs
may name addresses, since prod never emits them.
"""

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_LEVELS = {"info", "warning", "error", "exception", "critical"}
_PERSONAL = re.compile(r"email|sender|addr|recipient", re.I)
# Identities that are not users or correspondents.
_ALLOWED = {("claven/routes/auth.py", "_verify_pubsub_token")}  # Pub/Sub's service account


def _names(node):
    """Identifiers and string subscripts a log argument reads, except inside len()/counts."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "len":
        return
    if isinstance(node, ast.Name):
        yield node.id
    elif isinstance(node, ast.Attribute):
        yield node.attr
    elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
        yield node.slice.value
    for child in ast.iter_child_nodes(node):
        yield from _names(child)


def _violations():
    found = []
    for path in sorted((ROOT / "claven").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text())
        for func in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            if (rel, func.name) in _ALLOWED:
                continue
            for call in ast.walk(func):
                if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                        and call.func.attr in _LEVELS
                        and isinstance(call.func.value, ast.Name) and call.func.value.id == "logger"):
                    continue
                values = list(call.args[1:])
                for kw in call.keywords:
                    if kw.arg == "extra" and isinstance(kw.value, ast.Dict):
                        values += [k for k in kw.value.keys if k is not None]
                        values += kw.value.values
                for value in values:
                    hits = [n for n in _names(value) if _PERSONAL.search(n) and not n.endswith("count")]
                    if isinstance(value, ast.Constant) and isinstance(value.value, str) and _PERSONAL.search(value.value):
                        hits.append(value.value)
                    if hits:
                        found.append(f"{rel}:{call.lineno} {func.name}: {', '.join(sorted(set(hits)))}")
    return found


def test_no_email_addresses_in_info_or_higher_logs():
    assert _violations() == []


def test_checker_catches_an_email_argument(tmp_path, monkeypatch):
    """Guard against the scan silently matching nothing."""
    pkg = tmp_path / "claven"
    pkg.mkdir()
    (pkg / "m.py").write_text(
        "def f(session, sender, n):\n"
        "    logger.info('ok %s', len(sender))\n"
        "    logger.debug('fine %s', sender)\n"
        "    logger.warning('bad %s', session['email'])\n"
        "    logger.info('bad', extra={'email': n})\n"
    )
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    assert [v.split(" ", 1)[0] for v in _violations()] == ["claven/m.py:4", "claven/m.py:5"]
