"""The guard: the service page of a logged-in user leads “To the main page” to the cabinet, not to /login.

07.10 (card-truth): ``message.html`` takes the link from ``home_href``, and without it — the fallback ``/login``. A logged-in user on the 404 of a tender from ``routes_tender_ai._not_found`` got to the login. The global handlers (``main.py::_error_page_context``, #609) already return ``/app``. This guard closes the class entirely: by a predicate it finds ALL the calls of ``TemplateResponse(..., “message.html”, {...})`` in ``src/api/cabinet/`` and requires ``home_href`` in each, EXCEPT the anonymous login screens (``routes_auth.py`` — there ``/login`` is the correct link). A new call without the key will drop the test.
"""

from __future__ import annotations

import ast
import pathlib

CABINET = pathlib.Path(__file__).resolve().parents[2] / "src" / "api" / "cabinet"

#: The files where the page is anonymous: ``/login`` is the correct “main page”.
ANON_FILES = {"routes_auth.py"}


def _message_calls():
    for path in sorted(CABINET.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "TemplateResponse"
            ):
                continue
            if not any(
                isinstance(arg, ast.Constant) and arg.value == "message.html"
                for arg in node.args
            ):
                continue
            ctx = next((a for a in node.args if isinstance(a, ast.Dict)), None)
            yield path, node.lineno, ctx


def test_message_pages_in_cabinet_have_home_href():
    missing = []
    seen = 0
    for path, lineno, ctx in _message_calls():
        seen += 1
        if path.name in ANON_FILES:
            continue
        keys = {
            k.value for k in (ctx.keys if ctx else []) if isinstance(k, ast.Constant)
        }
        if "home_href" not in keys:
            missing.append(f"{path.name}:{lineno}")
    # The predicate must find something: otherwise the guard is empty and passes silently.
    assert seen >= 9  # as of 07.10: 9 calls (routes_auth 2, the other 7 in cabinet)
    assert missing == []
