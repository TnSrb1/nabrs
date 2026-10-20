"""Normalisation of ``{tender_id}`` in the cabinet paths ``/app/tenders/{id}…`` (#632).

A single place for all the tender’s sub-routes: both GET (``routes_tender_ai``) and POST (``routes_tender_ai`` generate, ``routes_my_tenders`` HTMX actions). A raw string from the path in ``CAST(:tender_id AS uuid)`` gives a DataError → 500 (production, 08.10), therefore the id is first brought to the canonical form via :func:`src.domain.ids.canonical_uuid`.

- garbage → ``None`` → the route answers 404 without going to the DB;
- GET with ``urn:uuid:`` / ``{…}`` / UPPER → 301 to the same path with the canonical id (the query is kept) — one canonical link;
- POST with a non-canonical id → a 301 won’t do (the browser would repeat the form as a GET to an address that does not exist), therefore the operation goes with the canonical id inside the route itself.
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import RedirectResponse

from src.domain.ids import canonical_uuid

_PREFIX = "/app/tenders/"


def canonical_tender_id(tender_id: str) -> str | None:
    """The canonical UUID of the tender from the path; ``None`` — garbage (route → 404)."""
    return canonical_uuid(tender_id)


def tender_path_redirect(
    request: Request, tender_id: str, canon: str
) -> RedirectResponse:
    """GET: 301 to the same path (with the tail ``/description`` etc.) and the query; the id is canonical."""
    rest = request.url.path[len(_PREFIX) + len(tender_id) :]
    target = f"{_PREFIX}{canon}{rest}"
    query = request.url.query
    if query:
        target += f"?{query}"
    return RedirectResponse(url=target, status_code=301)
