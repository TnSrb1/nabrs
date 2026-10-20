"""The audit of the API (scripts/audit/full_audit.py::audit_api) checks the behavior of /docs,
/redoc, /openapi.json, not a literal: without API_DOCS_ENABLED the documentation is closed.
Counter-fact: an application with an open FastAPI() the audit must mark as INFO.
"""

from __future__ import annotations

from pathlib import Path

from scripts.audit import full_audit

REPO = Path(__file__).resolve().parents[2]


def _docs_findings(root: Path) -> list:
    ctx = full_audit.Ctx(root=root, mode="static", run_tests=False)
    return [
        f
        for f in full_audit.audit_api(ctx)
        if "/docs" in f.title or "OpenAPI" in f.title
    ]


def test_current_tree_reports_docs_closed() -> None:
    findings = _docs_findings(REPO)
    assert [(f.severity, f.title) for f in findings] == [
        (
            full_audit.Severity.OK,
            "Публичные /docs, /redoc, /openapi.json закрыты без API_DOCS_ENABLED",
        )
    ]


def test_open_docs_are_reported(tmp_path: Path) -> None:
    api = tmp_path / "src" / "api"
    api.mkdir(parents=True)
    (api / "main.py").write_text(
        "from fastapi import FastAPI\n\napp = FastAPI()\n", encoding="utf-8"
    )
    findings = _docs_findings(tmp_path)
    assert len(findings) == 1
    assert findings[0].severity == full_audit.Severity.INFO
    assert "/docs" in findings[0].title
    assert "/redoc" in findings[0].title
    assert "/openapi.json" in findings[0].title
