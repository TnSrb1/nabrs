#!/usr/bin/env python3
"""The static stand of the showcase of templates (/library, /profiles/new, the composition) for the shot.

It renders the pages with the same application on a fake DB (as tests/api), but with
the DATA of production, exported in advance into the stand directory:

- ``templates.json`` — the public templates of ``template_library`` (json_agg of the rows);
- ``counts.json`` — (optional) the figures of the cards in the order of the showcase payloads;
- ``remote_preview.json`` — (optional) ``preview_count`` of the remote package with the examples for the composition page.

    python3 scripts/shots/stand_library.py /tmp/libstand
    (cd /tmp/libstand && python3 -m http.server 8766 --bind 127.0.0.1)
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from src.api import deps  # noqa: E402
from src.api.cabinet import library_counts, preview_service  # noqa: E402
from src.api.cabinet import routes_library  # noqa: E402
from src.api.main import app  # noqa: E402
from tests.api.conftest import FakeSession  # noqa: E402


def _async(value):
    async def _stub(*args, **kwargs):
        return value

    return _stub


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/libstand")
    rows = json.loads((out / "templates.json").read_text(encoding="utf-8"))
    tpls = [SimpleNamespace(**r) for r in rows]
    routes_library.svc.list_public_templates = _async(tpls)
    routes_library.svc.get_template = _async(tpls[0])

    counts_file = out / "counts.json"
    if counts_file.exists():
        library_counts.counts_for = _async(json.loads(counts_file.read_text()))

    preview_file = out / "remote_preview.json"
    if preview_file.exists():
        data = json.loads(preview_file.read_text(encoding="utf-8"))
        result = preview_service.PreviewResult(
            count=data["count"],
            unfound=[],
            examples=[
                preview_service.PreviewExample(
                    id=e["id"],
                    title_parts=[tuple(p) for p in e["title_parts"]],
                    customer_name=e["customer_name"],
                    deadline_at=None,
                )
                for e in data["examples"]
            ],
        )
        routes_library.preview_service.preview_count = _async(result)

    for lang in ("ru", "sr"):
        app.dependency_overrides[deps.get_session_factory] = lambda lang=lang: (
            lambda: FakeSession(user_locale=lang)
        )
        app.dependency_overrides[deps.get_optional_session] = lambda: {
            "uid": "u-1",
            "tid": "t-1",
        }
        client = TestClient(app, follow_redirects=False)
        pages = {
            f"library_{lang}.html": "/library",
            f"start_{lang}.html": "/profiles/new",
            f"detail_remote_{lang}.html": "/library/package/remote",
            f"detail_tpl_{lang}.html": f"/library/template/{tpls[0].id}",
        }
        for name, url in pages.items():
            resp = client.get(url)
            resp.raise_for_status()
            (out / name).write_text(resp.text, encoding="utf-8")
            print(f"{name}: {url} → {resp.status_code}")
    shutil.copytree(ROOT / "src/api/static", out / "static", dirs_exist_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
