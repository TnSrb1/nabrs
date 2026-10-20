"""Static version: ``?v=`` in the address and ``Cache-Control`` by it (TASK static-cache).

The version is the first 10 characters of the sha256 of the file’s content, computed ONCE at the module import (process start). The file changed → the address changed, therefore the browser does not keep the old ``app.css`` with the new SRI in the markup.

With a version in the address the response is ``immutable`` for a year; without a version (direct links, favicon, robots) — an hour, so that an edit of the file without a change of address arrives quickly.
"""

from __future__ import annotations

import hashlib
import pathlib
from urllib.parse import parse_qs

from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

STATIC_DIR = pathlib.Path(__file__).parent / "static"

CACHE_IMMUTABLE = "public, max-age=31536000, immutable"
CACHE_SHORT = "public, max-age=3600"


def file_version(path: pathlib.Path) -> str:
    """The file version: the first 10 hex characters of the sha256 of the content."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:10]


_VERSIONS: dict[str, str] = {
    path.relative_to(STATIC_DIR).as_posix(): file_version(path)
    for path in sorted(STATIC_DIR.rglob("*"))
    if path.is_file()
}


def static_url(rel: str) -> str:
    """``css/app.css`` → ``/static/css/app.css?v=<hash>`` (without ``?v=`` if the file is absent)."""
    rel = rel.lstrip("/")
    version = _VERSIONS.get(rel)
    return f"/static/{rel}?v={version}" if version else f"/static/{rel}"


class VersionedStaticFiles(StaticFiles):
    """StaticFiles with ``Cache-Control``: immutable only with ``?v=`` of the CURRENT version.

    An old ``?v=`` (before an edit of the file) gets a short TTL: the response is already different, and an eternal cache under the old address would fix the outdated content here.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code in (200, 304):
            query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
            requested = query.get("v", [None])[0]
            is_current = requested is not None and requested == _VERSIONS.get(path)
            response.headers["cache-control"] = (
                CACHE_IMMUTABLE if is_current else CACHE_SHORT
            )
        return response
